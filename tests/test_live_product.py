"""Live product regressions, including the reported EUR-selected/USD-imported scenario."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis import Redis

from astraroll.analytics.reader import get_analytics_reader
from astraroll.api import app
from astraroll.config import get_settings
from astraroll.v2.ingestion import ingest_csv_data, validate_and_parse_csv


def csv12(currency="USD", date="2026-10-03T10:00:00Z"):
    return "product_id,product_name,amount,currency,status,created_at\n" + "\n".join(
        f"P-{i % 6},Product {i % 6},{'100.00' if i < 5 else '6.97' if i == 5 else '20.00'},"
        f"{currency},{'shipped' if i < 6 else 'placed' if i < 10 else 'cancelled'},{date}"
        for i in range(12)
    )


@pytest.mark.parametrize(
    "amount", ["NaN", "Infinity", "-Infinity", "1.001", "0", "-1", "10000000000000000"]
)
def test_money_rejects_nonfinite_fractional_and_overflow(amount):
    rows, errors = validate_and_parse_csv(f"product_id,amount,currency\nP,{amount},USD")
    assert not rows and errors


def test_csv_ids_are_repeatable_and_tenant_scoped():
    tid = uuid4()
    one, _ = validate_and_parse_csv(csv12(), id_namespace=tid)
    two, _ = validate_and_parse_csv(csv12(), id_namespace=tid)
    other, _ = validate_and_parse_csv(csv12(), id_namespace=uuid4())
    assert [r.order_id for r in one] == [r.order_id for r in two]
    assert not set(r.order_id for r in one) & set(r.order_id for r in other)


def test_mixed_currency_validation_never_sums_money():
    result = ingest_csv_data(
        None,
        uuid4(),
        "product_id,amount,currency,status\nP,10.00,USD,shipped\nQ,20.00,EUR,shipped",
        dry_run=True,
    )
    assert result.total_sales is None and result.total_amount is None
    assert result.totals_by_currency["USD"]["sales_amount"] == "10.00"
    assert result.totals_by_currency["EUR"]["sales_amount"] == "20.00"


@pytest.fixture
def live_product(factory, tenant_id, monkeypatch):
    from astraroll.aggregation.bootstrap import bootstrap
    from astraroll.aggregation.coverage import read_coverage
    from astraroll.v2 import runtime
    from astraroll.v2.adapter import MultiTenantPostgresAnalyticsAdapter

    settings = get_settings()
    monkeypatch.setattr(settings, "deployment_mode", "database_per_tenant")
    monkeypatch.setattr(settings, "order_stream", "test:live:" + uuid4().hex)
    token = "test-live-" + uuid4().hex
    monkeypatch.setattr(
        settings,
        "analytics_api_keys_json",
        SecretStr(
            json.dumps(
                [
                    {
                        "subject": "live-test",
                        "tenant_ids": [str(tenant_id)],
                        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": "2099-01-01T00:00:00Z",
                    }
                ]
            )
        ),
    )

    class Manager:
        def session(self, tid):
            assert str(tid) == str(tenant_id)
            return factory()

        def get_engine(self, tid):
            assert str(tid) == str(tenant_id)
            return factory.kw["bind"]

        def close_all(self):
            pass

    mgr = Manager()
    monkeypatch.setattr(runtime, "get_tenant_engine_manager", lambda: mgr)
    monkeypatch.setattr(
        runtime,
        "get_control_registry",
        lambda: SimpleNamespace(
            get_tenant=lambda tid: SimpleNamespace(
                name="Disposable test", database_name="isolated_fixture", status="active"
            )
        ),
    )
    monkeypatch.setattr(runtime, "get_control_engine", lambda: factory.kw["bind"])
    bootstrap(
        factory,
        tenant_id,
        ["USD", "EUR"],
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 8, tzinfo=UTC),
    )
    app.dependency_overrides[get_analytics_reader] = lambda: MultiTenantPostgresAnalyticsAdapter(
        mgr, read_coverage
    )
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        yield client, tenant_id, settings
    app.dependency_overrides.clear()


@pytest.mark.integration
def test_exact_real_pipeline_parity_duplicate_and_currency(live_product, factory):
    client, tid, settings = live_product
    q = dict(
        tenant_id=str(tid), currency="USD", start="2026-10-01T00:00:00Z", end="2026-10-08T00:00:00Z"
    )
    before = client.get("/analytics/summary", params=q).json()
    # Backlog greater than relay batch, outside selected window. It must remain untouched.
    historical = "product_id,amount,currency,created_at\n" + "\n".join(
        "old,1.00,USD,2025-01-01T00:00:00Z" for _ in range(250)
    )
    old = client.post(f"/tenants/{tid}/ingest/csv?trigger_relay=false", content=historical).json()
    assert old["accepted"] == 250
    res = client.post(f"/tenants/{tid}/ingest/csv", content=csv12())
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["progress"]["stage"] == "AGGREGATED", data
    assert data["progress"]["event_count"] == data["progress"]["processed_count"] == 20
    assert data["totals_by_currency"]["USD"]["sales_amount"] == "506.97"
    after = client.get("/analytics/summary", params=q).json()
    assert after["total_order_count"] - before["total_order_count"] == 12
    assert Decimal(after["sales_amount"]) - Decimal(before["sales_amount"]) == Decimal("506.97")
    eur = client.get("/analytics/summary", params={**q, "currency": "EUR"}).json()
    assert eur["total_order_count"] == 0 and Decimal(eur["sales_amount"]) == 0
    assert client.get("/analytics/verify", params=q).json()["status"] == "MATCH"
    statuses = client.get("/analytics/status-breakdown", params=q).json()["statuses"]
    assert {r["status"]: r["total_order_count"] for r in statuses} == {
        "shipped": 6,
        "placed": 4,
        "cancelled": 2,
    }
    again = client.post(f"/tenants/{tid}/ingest/csv", content=csv12()).json()
    assert again["accepted"] == 0 and again["duplicates"] == 12
    assert client.get("/analytics/summary", params=q).json() == after
    assert (
        client.get("/analytics/summary", params={**q, "tenant_id": str(uuid4())}).status_code == 403
    )
    assert client.post(f"/tenants/{uuid4()}/ingest/csv", content=csv12()).status_code == 403
    assert client.get(f"/tenants/{uuid4()}/status").status_code == 403
    ops = client.get(f"/tenants/{tid}/status").json()
    assert (
        ops["unpublished_outbox"] == 250
        and ops["projection_count"] == 12
        and len(ops["recent_logs"]) == 20
    )
    with Redis.from_url(settings.redis_url) as r:
        assert r.xlen(settings.order_stream) == 20
        assert r.xpending(settings.order_stream, "astraroll-live-imports-v2")["pending"] == 0
    pid = data["preview_rows"][0]["product_id"]
    assert (
        client.get(f"/tenants/{tid}/products", params={"product_ids": pid}).json()[pid][
            "product_name"
        ]
        == "Product 0"
    )
    assert (
        client.put(
            f"/tenants/{tid}/products/{pid}",
            json={"product_name": "Maintained Name", "sku": "SKU-1"},
        ).status_code
        == 200
    )
    assert (
        client.get(f"/tenants/{tid}/products", params={"product_ids": pid}).json()[pid][
            "product_name"
        ]
        == "Maintained Name"
    )
    assert client.get("/analytics/summary", params=q).json() == after


@pytest.mark.integration
def test_redis_failure_is_committed_pending_then_recovers(live_product, monkeypatch):
    client, tid, settings = live_product
    original = settings.redis_url
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    res = client.post(f"/tenants/{tid}/ingest/csv", content=csv12()).json()
    assert res["accepted"] == 12 and res["progress"]["stage"] == "COMMITTED"
    assert res["processing_error"]["stage"] == "PUBLISHED"
    assert not res["progress"]["stages"]["AGGREGATED"]
    monkeypatch.setattr(settings, "redis_url", original)
    recovered = client.post(
        f"/tenants/{tid}/ingest/progress",
        json={"order_ids": res["imported_order_ids"], "advance": True},
    ).json()
    assert recovered["stage"] == "AGGREGATED" and recovered["processing_error"] is None


@pytest.mark.integration
def test_outside_window_commits_without_inventing_visibility(live_product):
    client, tid, _ = live_product
    q = dict(
        tenant_id=str(tid), currency="USD", start="2026-10-01T00:00:00Z", end="2026-10-08T00:00:00Z"
    )
    before = client.get("/analytics/summary", params=q).json()
    res = client.post(
        f"/tenants/{tid}/ingest/csv", content=csv12(date="2026-11-01T00:00:00Z")
    ).json()
    assert res["accepted"] == 12 and res["progress"]["stage"] == "AGGREGATED"
    after = client.get("/analytics/summary", params=q).json()
    assert after["total_order_count"] == before["total_order_count"]
    assert after["sales_amount"] == before["sales_amount"]


def test_disabled_reader_fails_closed_with_operator_reason(monkeypatch):
    from astraroll.analytics.reader import AnalyticsUnavailable, get_analytics_reader

    monkeypatch.setattr(
        get_settings(), "analytics_read_block_reason", "Preserved rollups need reconciliation"
    )
    with pytest.raises(AnalyticsUnavailable, match="need reconciliation"):
        get_analytics_reader().capabilities()


def test_spa_cannot_serve_files_outside_distribution():
    with TestClient(app) as client:
        assert client.get("/%2e%2e/%2e%2e/.env").status_code == 404
        assert client.get("/%2e%2e/package.json").status_code == 404


@pytest.mark.integration
def test_benchmark_write_and_invalid_catalog_are_denied(live_product, monkeypatch):
    client, tid, _ = live_product
    from astraroll.v2 import runtime

    monkeypatch.setattr(
        runtime,
        "get_control_registry",
        lambda: SimpleNamespace(
            get_tenant=lambda _: SimpleNamespace(database_name="v2bench_preserved")
        ),
    )
    response = client.post(f"/tenants/{tid}/ingest/csv", content=csv12())
    assert response.status_code == 409
    assert (
        client.post(
            f"/tenants/{tid}/orders",
            json={"product_id": str(uuid4()), "amount": "1.00", "currency": "USD"},
        ).status_code
        == 409
    )
    assert (
        client.put(f"/tenants/{tid}/products/{uuid4()}", json={"product_name": "   "}).status_code
        == 422
    )

    assert (
        client.put(
            f"/tenants/{tid}/products/{uuid4()}",
            json={"product_name": "Must not alter protected metadata"},
        ).status_code
        == 409
    )
