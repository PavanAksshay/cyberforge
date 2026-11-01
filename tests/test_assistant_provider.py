"""Provider configuration, grounding and honest failure handling without external calls."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from astraroll.config import Settings, get_settings
from astraroll.v2 import assistant
from tests.test_v2_product_features import MockGroundedReader

pytestmark = pytest.mark.anyio


async def test_key_loaded_from_dotenv_and_redacted(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=private-test-key\nGROQ_MODEL=test-model\n")
    settings = Settings(_env_file=env)
    assert settings.groq_api_key.get_secret_value() == "private-test-key"
    assert "private-test-key" not in repr(settings)
    assert settings.groq_model == "test-model"


def configure(monkeypatch, handler):
    monkeypatch.setattr(get_settings(), "groq_api_key", SecretStr("test-only-provider-key"))
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        assistant.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )


async def test_tool_retrieval_synthesis_and_server_owned_scope(monkeypatch):
    tid = uuid4()
    reader = MockGroundedReader(tid)
    calls = []
    summary = reader.summary

    def read(query):
        assert query.tenant_id == tid and query.currency == "USD"
        assert query.start.isoformat().startswith("2026-10-01")
        return summary(query)

    reader.summary = read

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            assert body["tool_choice"] == "required"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "test-call",
                        "type": "function",
                        "function": {
                            "name": "get_summary",
                            "arguments": json.dumps(
                                {
                                    "currency": "EUR",
                                    "start": "2025-01-01T00:00:00Z",
                                    "end": "2025-02-01T00:00:00Z",
                                }
                            ),
                        },
                    }
                ],
            }
        else:
            assert body["tool_choice"] == "auto"
            tool = json.loads(body["messages"][-1]["content"])
            assert tool["currency"] == "USD" and tool["data"]["sales_amount"] == "15000.50"
            assert tool["freshness"]["tenant_revision"] is not None
            message = {"content": "USD shipped sales are 15,000.50 from October 1 to October 8."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    configure(monkeypatch, provider)
    result = await assistant.ask_assistant(
        "Show revenue", tid, reader, "USD", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z"
    )
    assert result.provider == "groq" and not result.provider_error
    assert result.data["summary"]["sales_amount"] == "15000.50"
    assert result.endpoints_called == ["/analytics/summary"] and result.freshness


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_provider_failure_is_explicit_and_does_not_leak_response(monkeypatch, status):
    configure(
        monkeypatch, lambda _: httpx.Response(status, json={"error": "PRIVATE PROVIDER DETAIL"})
    )
    tid = uuid4()
    result = await assistant.ask_assistant("Revenue?", tid, MockGroundedReader(tid))
    assert result.provider == "grounded_rule_engine" and result.provider_error
    assert "PRIVATE" not in str(result) and "15,000.50" in result.reply


async def test_empty_or_ungrounded_provider_answer_is_not_accepted(monkeypatch):
    configure(
        monkeypatch,
        lambda _: httpx.Response(
            200, json={"choices": [{"message": {"content": "Invented revenue: 999999"}}]}
        ),
    )
    tid = uuid4()
    result = await assistant.ask_assistant("Revenue?", tid, MockGroundedReader(tid))
    assert result.provider == "grounded_rule_engine" and result.provider_error
    assert "999999" not in result.reply


async def test_model_can_request_a_second_lookup_before_synthesis(monkeypatch):
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) < 3:
            tool = "get_summary" if len(calls) == 1 else "get_status_breakdown"
            assert body["tool_choice"] == ("required" if len(calls) == 1 else "auto")
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(len(calls)),
                        "type": "function",
                        "function": {"name": tool, "arguments": "{}"},
                    }
                ],
            }
        else:
            assert len([m for m in body["messages"] if m["role"] == "tool"]) == 2
            message = {"content": "Shipped sales are USD 15,000.50; 100 orders in the window."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    configure(monkeypatch, provider)
    tid = uuid4()
    result = await assistant.ask_assistant("Sales and status?", tid, MockGroundedReader(tid))
    assert result.provider == "groq" and not result.provider_error
    assert set(result.data) == {"summary", "status_breakdown"}


async def test_repeated_tool_requests_stop_at_a_bounded_limit(monkeypatch):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": str(len(calls)),
                                    "type": "function",
                                    "function": {
                                        "name": "get_summary",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    configure(monkeypatch, provider)
    tid = uuid4()
    result = await assistant.ask_assistant("Revenue?", tid, MockGroundedReader(tid))
    assert len(calls) == 5
    assert result.provider == "grounded_rule_engine" and "limit" in result.provider_error
