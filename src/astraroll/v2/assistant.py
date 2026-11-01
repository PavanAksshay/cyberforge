"""AI Analytics Assistant Service for AstraRoll V2.

Features:
- Primary Engine: Groq API with LLM function/tool calling.
- Fallback Engine: Deterministic Grounded Analytics Engine.
- Strict Grounding: Never executes raw SQL; only authorized reader contracts.
- Respects AstraRoll metric semantics: Shipped-only sales, UTC boundaries.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from astraroll.analytics.contracts import (
    AnalyticsQuery,
    TopProductsQuery,
    TrendsQuery,
)
from astraroll.analytics.reader import AnalyticsReader, AnalyticsUnavailable
from astraroll.schemas import Currency

log = logging.getLogger(__name__)


@dataclass
class AssistantResponse:
    reply: str
    intent: str
    provider: str  # "groq" or "grounded_rule_engine"
    model: str
    endpoints_called: list[str]
    data: dict[str, Any]
    freshness: dict[str, Any] | None = None


GROQ_ANALYTICS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_summary",
            "description": (
                "Fetch aggregate financial summary (shipped sales, order count) "
                "for a tenant within a date range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "currency": {
                        "type": "string",
                        "enum": ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"],
                        "default": "USD",
                    },
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight start (e.g. 2026-10-01T00:00:00Z)",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight end (e.g. 2026-11-01T00:00:00Z)",
                    },
                },
                "required": ["currency", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trends",
            "description": "Fetch time-series order counts and sales grouped by interval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "currency": {
                        "type": "string",
                        "enum": ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"],
                        "default": "USD",
                    },
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight start datetime",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight end datetime",
                    },
                    "interval": {
                        "type": "string",
                        "enum": ["day", "hour"],
                        "default": "day",
                    },
                },
                "required": ["currency", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_products",
            "description": "Fetch highest grossing products ranked by shipped sales.",
            "parameters": {
                "type": "object",
                "properties": {
                    "currency": {
                        "type": "string",
                        "enum": ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"],
                        "default": "USD",
                    },
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight start datetime",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight end datetime",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "required": ["currency", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status_breakdown",
            "description": "Fetch distribution across order statuses (placed, shipped, cancelled).",
            "parameters": {
                "type": "object",
                "properties": {
                    "currency": {
                        "type": "string",
                        "enum": ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"],
                        "default": "USD",
                    },
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight start datetime",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 UTC midnight end datetime",
                    },
                },
                "required": ["currency", "start", "end"],
            },
        },
    },
]


def strip_markdown_syntax(text: str) -> str:
    """Remove raw markdown characters so responses are completely clean plain executive text."""
    if not text:
        return ""
    # Strip headers: ###, ##, #
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # Strip blockquotes and alert markers: > [!NOTE], >
    text = re.sub(r"^>\s*(\[!.*?\])?\s*", "", text, flags=re.MULTILINE)
    # Strip bold and italics: **text**, *text*, __text__, _text_
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    # Strip inline backticks: `code`
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Strip code block fences
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    # Replace non-breaking hyphens with standard dash
    text = text.replace("\u2011", "-")
    # Clean multiple empty lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


SYSTEM_PROMPT = """You are the AstraRoll AI Executive Analytics Assistant.
You have access to verified real-time analytics tools for the current tenant.

STRICT GROUNDING RULES:
1. Every financial and operational metric you state MUST come directly from tool outputs.
2. Under AstraRoll V2 semantics, Sales ONLY count orders with status='shipped'.
3. Placed and cancelled orders do NOT count as sales, but contribute to order volume.
4. Always state the currency (e.g. $ USD, € EUR) and time window.
5. Never extrapolate or hallucinate numbers not present in tool results.

STRICT FORMATTING RULE:
Use clean plain text with standard indentation or bullet dashes.
"""


def _default_window(start: str | None, end: str | None, interval: str = "day") -> tuple[str, str]:
    """Provide default 30-day UTC window, strictly aligned to UTC midnight boundaries."""
    now = datetime.now(UTC)
    base_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    end_dt = parse_iso_or_default(end, base_end)
    start_dt = parse_iso_or_default(start, end_dt - timedelta(days=30))

    start_aligned = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_aligned = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    if start_aligned >= end_aligned:
        end_aligned = start_aligned + timedelta(days=1)

    return (
        start_aligned.strftime("%Y-%m-%dT%H:00:00Z"),
        end_aligned.strftime("%Y-%m-%dT%H:00:00Z"),
    )


def parse_iso_or_default(dt_str: str | None, default: datetime) -> datetime:
    if not dt_str:
        return default
    try:
        cleaned = dt_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return default


def execute_grounded_intent(
    query_str: str,
    tenant_id: UUID,
    reader: AnalyticsReader,
    currency: Currency = "USD",
    start: str | None = None,
    end: str | None = None,
) -> AssistantResponse:
    """Deterministic Rule-Based Analytics Assistant (Fallback Engine)."""
    clean_query = query_str.lower().strip()
    start_iso, end_iso = _default_window(start, end)
    q = AnalyticsQuery(tenant_id=tenant_id, currency=currency, start=start_iso, end=end_iso)

    endpoints_called: list[str] = []
    data_payload: dict[str, Any] = {}
    freshness_info: dict[str, Any] | None = None

    is_top_products = any(
        k in clean_query
        for k in [
            "top product",
            "top products",
            "top sell",
            "best sell",
            "popular",
            "ranking",
            "most bought",
            "product ranking",
            "product sales",
        ]
    )
    is_status_breakdown = any(
        k in clean_query
        for k in ["status", "breakdown", "placed", "cancelled", "conversion", "funnel"]
    )
    is_trends = any(
        k in clean_query
        for k in ["trend", "daily", "hourly", "over time", "growth", "history", "chart"]
    )
    is_freshness = any(
        k in clean_query for k in ["freshness", "lag", "latency", "real-time", "sync", "coverage"]
    )

    lines: list[str] = []
    intent = "summary"

    try:
        if is_top_products:
            intent = "top_products"
            top_q = TopProductsQuery(
                tenant_id=tenant_id,
                currency=currency,
                start=start_iso,
                end=end_iso,
                limit=5,
            )
            res = reader.top_products(top_q)
            endpoints_called.append("/analytics/top-products")
            products = res.value
            names = {}
            if hasattr(reader, "engine_manager"):
                from astraroll.v2.products import resolve_products

                names = resolve_products(tenant_id, [p.product_id for p in products])
            data_payload["top_products"] = [
                {
                    "product_id": str(p.product_id),
                    "product_name": names.get(str(p.product_id), {}).get("product_name"),
                    "sku": names.get(str(p.product_id), {}).get("sku"),
                    "sales_amount": str(p.sales_amount),
                    "total_order_count": p.total_order_count,
                }
                for p in products
            ]
            lines.append("### 🏆 Top Shipped Products Ranking")
            lines.append(f"**Window:** `{start_iso}` to `{end_iso}` ({currency})\n")
            if not products:
                lines.append("No shipped products recorded in this window.")
            else:
                lines.append("| Rank | Product ID | Shipped Sales | Shipped Orders |")
                lines.append("| :--- | :--- | :--- | :--- |")
                for rank, p in enumerate(products, 1):
                    amt = f"{currency} {p.sales_amount:,.2f}"
                    label = names.get(str(p.product_id), {}).get(
                        "product_name", "Uncataloged product"
                    )
                    lines.append(
                        f"| #{rank} | {label} ({p.product_id}) | {amt} | {p.total_order_count:,} |"
                    )

        elif is_status_breakdown:
            intent = "status_breakdown"
            res = reader.status_breakdown(q)
            endpoints_called.append("/analytics/status-breakdown")
            statuses = res.value
            data_payload["status_breakdown"] = [
                {
                    "status": s.status,
                    "total_order_count": s.total_order_count,
                    "sales_amount": str(s.sales_amount),
                }
                for s in statuses
            ]
            lines.append("### 🔄 Order Status Lifecycle Breakdown")
            lines.append(f"**Window:** `{start_iso}` to `{end_iso}`\n")
            lines.append("| Status | Orders | Attributed Sales | Semantics |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for s in statuses:
                sem = "Counted" if s.status == "shipped" else "Excluded"
                amt = f"{currency} {s.sales_amount:,.2f}"
                lines.append(
                    f"| **{s.status.upper()}** | {s.total_order_count:,} | `{amt}` | *{sem}* |"
                )

        elif is_trends:
            intent = "trends"
            t_q = TrendsQuery(
                tenant_id=tenant_id,
                currency=currency,
                start=start_iso,
                end=end_iso,
                interval="day",
            )
            res = reader.trends(t_q)
            endpoints_called.append("/analytics/trends")
            buckets = res.value
            data_payload["trends"] = [
                {
                    "bucket_start": b.bucket_start.isoformat(),
                    "total_order_count": b.total_order_count,
                    "sales_amount": str(b.sales_amount),
                }
                for b in buckets
            ]
            lines.append("### 📈 Daily Trends Overview")
            lines.append(f"**Window:** `{start_iso}` to `{end_iso}` ({len(buckets)} buckets)\n")
            sample_buckets = buckets[-7:] if len(buckets) > 7 else buckets
            lines.append("| Date (UTC) | Shipped Sales | Orders |")
            lines.append("| :--- | :--- | :--- |")
            for b in sample_buckets:
                date_str = b.bucket_start.strftime("%Y-%m-%d")
                amt = f"{currency} {b.sales_amount:,.2f}"
                lines.append(f"| `{date_str}` | `{amt}` | {b.total_order_count:,} |")

        else:
            intent = "summary" if not is_freshness else "freshness"
            res = reader.summary(q)
            endpoints_called.append("/analytics/summary")
            summary_metrics = res.value
            if res.freshness:
                freshness_info = {
                    "tenant_revision": res.freshness.tenant_revision,
                    "last_processed_at": (
                        res.freshness.last_processed_at.isoformat()
                        if res.freshness.last_processed_at
                        else None
                    ),
                }
            aov = (
                summary_metrics.sales_amount / Decimal(summary_metrics.total_order_count)
                if summary_metrics.total_order_count > 0
                else Decimal("0.00")
            )
            data_payload["summary"] = {
                "sales_amount": str(summary_metrics.sales_amount),
                "total_order_count": summary_metrics.total_order_count,
                "aov": str(round(aov, 2)),
            }

            sales_str = f"{currency} {summary_metrics.sales_amount:,.2f}"
            aov_str = f"{currency} {aov:,.2f}"
            lines.append("AstraRoll Executive Summary:")
            lines.append(f"Tenant ID: {tenant_id} | Currency: {currency}")
            lines.append(f"Analysis Window: {start_iso} to {end_iso}\n")
            lines.append(f"- Total Shipped Sales: {sales_str}")
            lines.append(f"- Total Orders (All Statuses): {summary_metrics.total_order_count:,}")
            lines.append(f"- Estimated AOV (per order): {aov_str}")

            if is_freshness and freshness_info:
                rev = freshness_info.get("tenant_revision")
                last_proc = freshness_info.get("last_processed_at")
                lines.append("\nFreshness & Pipeline State:")
                lines.append(f"- Rollup Revision: {rev}")
                lines.append(f"- Last Materialized At: {last_proc}")
                lines.append("- SLA Target: Under 30 seconds for real-time visibility.")

        lines.append(
            "\nAstraRoll Metric Semantics: Only shipped orders "
            "contribute to reported sales figures."
        )
        lines.append("Response generated by AstraRoll Grounded Analytics Engine.")

    except AnalyticsUnavailable as exc:
        lines = [
            "### ⚠️ Analytics Unavailable",
            f"The aggregate analytics subsystem reported: *{exc}*.",
            "Please ensure tenant database rollups and durable coverage proof are initialized.",
        ]
        intent = "error"

    return AssistantResponse(
        reply=strip_markdown_syntax("\n".join(lines)),
        intent=intent,
        provider="grounded_rule_engine",
        model="rule-based-grounded-v2",
        endpoints_called=endpoints_called,
        data=data_payload,
        freshness=freshness_info,
    )


async def ask_assistant(
    query_str: str,
    tenant_id: UUID,
    reader: AnalyticsReader,
    currency: Currency = "USD",
    start: str | None = None,
    end: str | None = None,
) -> AssistantResponse:
    """Orchestrate assistant query: Groq API when configured, or deterministic fallback."""
    groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_api_key:
        return execute_grounded_intent(
            query_str=query_str,
            tenant_id=tenant_id,
            reader=reader,
            currency=currency,
            start=start,
            end=end,
        )

    model_name = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    start_iso, end_iso = _default_window(start, end)
    endpoints_called: list[str] = []
    collected_data: dict[str, Any] = {}

    user_prompt = (
        f"Tenant ID: {tenant_id}\n"
        f"Default Currency: {currency}\n"
        f"Default Window: {start_iso} to {end_iso}\n"
        f"Query: {query_str}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            headers = {
                "Authorization": f"Bearer {groq_api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": model_name,
                "messages": messages,
                "tools": GROQ_ANALYTICS_TOOLS,
                "tool_choice": "auto",
                "temperature": 0.1,
            }

            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=body,
            )

            if resp.status_code != 200:
                log.warning(
                    "Groq API returned HTTP %s: %s; falling back", resp.status_code, resp.text
                )
                return execute_grounded_intent(query_str, tenant_id, reader, currency, start, end)

            resp_json = resp.json()
            choice = resp_json["choices"][0]
            message = choice["message"]

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return AssistantResponse(
                    reply=strip_markdown_syntax(message.get("content", "")),
                    intent="general",
                    provider="groq",
                    model=model_name,
                    endpoints_called=[],
                    data={},
                )

            messages.append(message)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
                t_curr = args.get("currency", currency)
                t_start = start if start else args.get("start", start_iso)
                t_end = end if end else args.get("end", end_iso)

                q = AnalyticsQuery(tenant_id=tenant_id, currency=t_curr, start=t_start, end=t_end)

                try:
                    if func_name == "get_summary":
                        endpoints_called.append("/analytics/summary")
                        res = reader.summary(q)
                        tool_output = res.value.model_dump(mode="json")
                        collected_data["summary"] = tool_output

                    elif func_name == "get_trends":
                        endpoints_called.append("/analytics/trends")
                        t_interval = args.get("interval", "day")
                        tq = TrendsQuery(
                            tenant_id=tenant_id,
                            currency=t_curr,
                            start=t_start,
                            end=t_end,
                            interval=t_interval,
                        )
                        res = reader.trends(tq)
                        tool_output = [b.model_dump(mode="json") for b in res.value]
                        collected_data["trends"] = tool_output

                    elif func_name == "get_top_products":
                        endpoints_called.append("/analytics/top-products")
                        t_limit = args.get("limit", 5)
                        tpq = TopProductsQuery(
                            tenant_id=tenant_id,
                            currency=t_curr,
                            start=t_start,
                            end=t_end,
                            limit=t_limit,
                        )
                        res = reader.top_products(tpq)
                        tool_output = [p.model_dump(mode="json") for p in res.value]
                        collected_data["top_products"] = tool_output

                    elif func_name == "get_status_breakdown":
                        endpoints_called.append("/analytics/status-breakdown")
                        res = reader.status_breakdown(q)
                        tool_output = [s.model_dump(mode="json") for s in res.value]
                        collected_data["status_breakdown"] = tool_output
                    else:
                        tool_output = {"error": f"Unknown tool {func_name}"}
                except Exception as tool_err:
                    log.warning("Tool %s execution failed: %s", func_name, tool_err)
                    tool_output = {"error": str(tool_err)}

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(tool_output),
                    }
                )

            second_resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={
                    "model": model_name,
                    "messages": messages,
                    "temperature": 0.2,
                },
            )

            if second_resp.status_code == 200:
                final_content = second_resp.json()["choices"][0]["message"]["content"]
                return AssistantResponse(
                    reply=strip_markdown_syntax(final_content),
                    intent="groq_synthesis",
                    provider="groq",
                    model=model_name,
                    endpoints_called=endpoints_called,
                    data=collected_data,
                )
            else:
                return execute_grounded_intent(query_str, tenant_id, reader, currency, start, end)

    except Exception as exc:
        log.warning("Groq integration exception: %s. Falling back to rule engine.", exc)
        return execute_grounded_intent(query_str, tenant_id, reader, currency, start, end)
