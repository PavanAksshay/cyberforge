"""Tests for AstraRoll V2 CSV Ingestion, AI Assistant, and Product Endpoints."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from astraroll.analytics.contracts import (
    AnalyticsQuery,
    Freshness,
    ProductMetrics,
    StatusMetrics,
    SummaryMetrics,
    TopProductsQuery,
    TrendMetrics,
    TrendsQuery,
)
from astraroll.analytics.reader import ReadResult, get_analytics_reader
from astraroll.api import app
from astraroll.config import get_settings
from astraroll.v2.assistant import (
    ask_assistant,
    execute_grounded_intent,
)
from astraroll.v2.ingestion import (
    resolve_product_uuid,
    validate_and_parse_csv,
)


class MockGroundedReader:
    """Mock reader returning deterministic verified metrics."""

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id

    def capabilities(self):
        from astraroll.analytics.reader import ReaderCapabilities

        return ReaderCapabilities(base_granularity="hour")

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]:
        return ReadResult(
            value=SummaryMetrics(
                total_order_count=100,
                sales_amount=Decimal("15000.50"),
            ),
            freshness=Freshness(tenant_revision=42, last_processed_at=datetime.now(UTC)),
        )

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]:
        return ReadResult(
            value=[
                TrendMetrics(
                    bucket_start=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
                    total_order_count=50,
                    sales_amount=Decimal("7500.25"),
                ),
                TrendMetrics(
                    bucket_start=datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
                    total_order_count=50,
                    sales_amount=Decimal("7500.25"),
                ),
            ],
            freshness=Freshness(tenant_revision=42, last_processed_at=datetime.now(UTC)),
        )

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]:
        p1 = uuid4()
        p2 = uuid4()
        return ReadResult(
            value=[
                ProductMetrics(
                    product_id=p1, sales_amount=Decimal("9000.00"), total_order_count=45
                ),
                ProductMetrics(
                    product_id=p2, sales_amount=Decimal("6000.50"), total_order_count=30
                ),
            ],
            freshness=Freshness(tenant_revision=42, last_processed_at=datetime.now(UTC)),
        )

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]:
        return ReadResult(
            value=[
                StatusMetrics(
                    status="shipped", total_order_count=75, sales_amount=Decimal("15000.50")
                ),
                StatusMetrics(status="placed", total_order_count=20, sales_amount=Decimal("0.00")),
                StatusMetrics(
                    status="cancelled", total_order_count=5, sales_amount=Decimal("0.00")
                ),
            ],
            freshness=Freshness(tenant_revision=42, last_processed_at=datetime.now(UTC)),
        )


def test_csv_validation_valid():
    csv_text = """product_id,amount,currency,status,created_at
PROD-A,150.00,USD,shipped,2026-10-15T12:00:00Z
PROD-B,85.50,EUR,placed,2026-10-15T13:00:00Z
"""
    rows, errors = validate_and_parse_csv(csv_text)
    assert len(errors) == 0
    assert len(rows) == 2
    assert rows[0].amount == Decimal("150.00")
    assert rows[0].currency == "USD"
    assert rows[0].status == "shipped"
    assert rows[0].raw_product_id == "PROD-A"
    assert isinstance(rows[0].product_id, UUID)

    assert rows[1].amount == Decimal("85.50")
    assert rows[1].currency == "EUR"
    assert rows[1].status == "placed"


def test_csv_validation_errors():
    csv_text = """product_id,amount,currency,status
,INVALID_AMOUNT,XXX,unknown_status
"""
    rows, errors = validate_and_parse_csv(csv_text)
    assert len(rows) == 0
    assert len(errors) >= 3
    col_names = {e.column for e in errors}
    assert "product_id" in col_names
    assert "amount" in col_names
    assert "currency" in col_names
    assert "status" in col_names


def test_csv_missing_header():
    csv_text = """foo,bar,baz
1,2,3
"""
    rows, errors = validate_and_parse_csv(csv_text)
    assert len(rows) == 0
    assert len(errors) == 1
    assert "Missing required columns" in errors[0].message


def test_resolve_product_uuid_deterministic():
    u1 = resolve_product_uuid("sku-100")
    u2 = resolve_product_uuid("sku-100")
    assert u1 == u2

    raw_uuid = str(uuid4())
    resolved = resolve_product_uuid(raw_uuid)
    assert str(resolved) == raw_uuid


def test_assistant_grounded_summary():
    tenant_id = uuid4()
    reader = MockGroundedReader(tenant_id)

    resp = execute_grounded_intent(
        query_str="What are our total sales and orders for this period?",
        tenant_id=tenant_id,
        reader=reader,
        currency="USD",
    )
    assert resp.intent == "summary"
    assert resp.provider == "grounded_rule_engine"
    assert "/analytics/summary" in resp.endpoints_called
    assert "15,000.50" in resp.reply
    assert "100" in resp.reply
    assert resp.data["summary"]["sales_amount"] == "15000.50"


def test_assistant_grounded_top_products():
    tenant_id = uuid4()
    reader = MockGroundedReader(tenant_id)

    resp = execute_grounded_intent(
        query_str="Show me the top selling products",
        tenant_id=tenant_id,
        reader=reader,
        currency="USD",
    )
    assert resp.intent == "top_products"
    assert "/analytics/top-products" in resp.endpoints_called
    assert "Top Shipped Products" in resp.reply
    assert len(resp.data["top_products"]) == 2


def test_assistant_grounded_status_breakdown():
    tenant_id = uuid4()
    reader = MockGroundedReader(tenant_id)

    resp = execute_grounded_intent(
        query_str="What is our status breakdown between placed, shipped, and cancelled orders?",
        tenant_id=tenant_id,
        reader=reader,
        currency="USD",
    )
    assert resp.intent == "status_breakdown"
    assert "/analytics/status-breakdown" in resp.endpoints_called
    assert "SHIPPED" in resp.reply
    assert "CANCELLED" in resp.reply


def test_assistant_grounded_trends():
    tenant_id = uuid4()
    reader = MockGroundedReader(tenant_id)

    resp = execute_grounded_intent(
        query_str="Show daily trends over time",
        tenant_id=tenant_id,
        reader=reader,
        currency="USD",
    )
    assert resp.intent == "trends"
    assert "/analytics/trends" in resp.endpoints_called
    assert "Daily Trends Overview" in resp.reply
    assert len(resp.data["trends"]) == 2


@pytest.mark.anyio
async def test_ask_assistant_fallback_when_no_groq_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    tenant_id = uuid4()
    reader = MockGroundedReader(tenant_id)

    resp = await ask_assistant(
        query_str="What is our revenue?",
        tenant_id=tenant_id,
        reader=reader,
        currency="USD",
    )
    assert resp.provider == "grounded_rule_engine"
    assert "15,000.50" in resp.reply


def test_ops_summary_endpoint():
    with TestClient(app) as client:
        resp = client.get("/ops/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "operational"
        assert "benchmark_milestone" in data
        assert data["benchmark_milestone"]["total_orders"] == 1000000
        assert data["benchmark_milestone"]["tenants"] == 100


def test_api_assistant_and_csv_endpoints(monkeypatch):
    t_id = uuid4()
    token = secrets.token_urlsafe(32)
    keys_config = [
        {
            "subject": "product-test-user",
            "tenant_ids": [str(t_id)],
            "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "is_admin": False,
        }
    ]
    monkeypatch.setenv("ANALYTICS_API_KEYS_JSON", json.dumps(keys_config))
    get_settings.cache_clear()

    reader = MockGroundedReader(t_id)
    app.dependency_overrides[get_analytics_reader] = lambda: reader

    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Unauthenticated assistant query fails 401
            unauth_resp = client.post(
                f"/tenants/{t_id}/assistant",
                json={"query": "Show revenue summary", "currency": "USD"},
            )
            assert unauth_resp.status_code == 401

            # 2. Wrong tenant query fails 403
            wrong_t_id = uuid4()
            wrong_resp = client.post(
                f"/tenants/{wrong_t_id}/assistant",
                json={"query": "Show revenue summary", "currency": "USD"},
                headers=headers,
            )
            assert wrong_resp.status_code == 403

            # 3. Authorized assistant query succeeds 200
            auth_resp = client.post(
                f"/tenants/{t_id}/assistant",
                json={"query": "What are our total sales and orders?", "currency": "USD"},
                headers=headers,
            )
            assert auth_resp.status_code == 200
            assistant_data = auth_resp.json()
            assert assistant_data["provider"] == "grounded_rule_engine"
            assert "15,000.50" in assistant_data["reply"]
            assert "/analytics/summary" in assistant_data["endpoints_called"]

            # 4. CSV dry-run ingestion via API
            valid_csv = (
                "product_id,amount,currency,status\n"
                "SKU-100,50.00,USD,placed\n"
                "SKU-200,100.00,USD,shipped"
            )
            csv_resp = client.post(
                f"/tenants/{t_id}/ingest/csv?dry_run=true",
                content=valid_csv,
                headers={**headers, "Content-Type": "text/csv"},
            )
            assert csv_resp.status_code == 200
            csv_data = csv_resp.json()
            assert csv_data["dry_run"] is True
            assert csv_data["total_rows"] == 2
            assert csv_data["accepted"] == 2
            assert csv_data["rejected"] == 0
            assert len(csv_data["preview_rows"]) == 2
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_spa_static_serving():
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "AstraRoll Control Center" in resp.text
        assert '<div id="root"></div>' in resp.text


