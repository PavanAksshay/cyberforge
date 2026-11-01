"""Portfolio authorization, exact cross-database arithmetic and failure semantics."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from astraroll.analytics.contracts import Freshness, TrendMetrics
from astraroll.analytics.portfolio import _overview_slot, get_portfolio_registry
from astraroll.analytics.reader import (
    AnalyticsUnavailable,
    ReaderCapabilities,
    ReadResult,
    get_analytics_reader,
)
from astraroll.api import app
from astraroll.config import get_settings

A, B, C = UUID(int=1), UUID(int=2), UUID(int=3)
PARAMS = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-03T00:00:00Z", "currency": "USD"}


class PortfolioFake:
    def __init__(self):
        self.seen = []
        self.fail = set()

    def capabilities(self):
        return ReaderCapabilities(base_granularity="hour")

    def trends(self, query):
        self.seen.append((query.tenant_id, query.currency))
        if query.tenant_id in self.fail:
            raise AnalyticsUnavailable("private database diagnostic")
        return ReadResult(
            [
                TrendMetrics(
                    bucket_start=query.start,
                    total_order_count=2,
                    sales_amount=Decimal("0.10") if query.currency == "USD" else Decimal("0.30"),
                ),
                TrendMetrics(
                    bucket_start=query.start + timedelta(days=1),
                    total_order_count=1,
                    sales_amount=Decimal("0.20"),
                ),
            ],
            Freshness(tenant_revision=query.tenant_id.int),
        )


@pytest.fixture
def portfolio_client(monkeypatch):
    records = [SimpleNamespace(tenant_id=t, name=f"Store {t.int}") for t in (C, B, A)]
    fake = PortfolioFake()
    registry = SimpleNamespace(list_active_tenants=lambda: records)
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "analytics_api_keys_json",
        SecretStr(
            json.dumps(
                [
                    {
                        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                        "subject": key,
                        "tenant_ids": [str(t) for t in ids],
                        "is_admin": admin,
                        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    }
                    for key, ids, admin in [
                        ("multi", [A, B], False),
                        ("single", [A], False),
                        ("admin", [], True),
                        ("none", [UUID(int=9)], False),
                    ]
                ]
            )
        ),
    )
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_portfolio_registry] = lambda: registry
    app.dependency_overrides[get_analytics_reader] = lambda: fake
    with TestClient(app) as client:
        yield client, fake
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def fetch(client, key="multi", **params):
    return client.get(
        "/analytics/portfolio", params=PARAMS | params, headers={"Authorization": f"Bearer {key}"}
    )


def test_exact_totals_buckets_and_deterministic_ties(portfolio_client):
    client, fake = portfolio_client
    response = fetch(client)
    assert response.status_code == 200
    result = response.json()
    assert result["totals"] == {"total_order_count": 6, "sales_amount": "0.60"}
    assert [b["sales_amount"] for b in result["buckets"]] == ["0.20", "0.40"]
    assert [r["tenant_id"] for r in result["tenants"]] == [str(A), str(B)]
    assert result["consistency"] == "independent_tenant_snapshots"
    assert result["tenants"][0]["freshness"]["tenant_revision"] == 1
    assert response.headers["cache-control"] == "no-store"
    assert {t for t, _ in fake.seen} == {A, B}


def test_membership_and_admin_scope(portfolio_client):
    client, fake = portfolio_client
    assert fetch(client, "single").json()["tenant_count"] == 1
    assert {t for t, _ in fake.seen} == {A}
    fake.seen.clear()
    assert fetch(client, "admin").json()["tenant_count"] == 3
    assert {t for t, _ in fake.seen} == {A, B, C}


def test_missing_or_invalid_credentials_never_read(portfolio_client):
    client, fake = portfolio_client
    assert client.get("/analytics/portfolio", params=PARAMS).status_code == 401
    assert fetch(client, "invalid").status_code == 401
    assert fake.seen == []


def test_currency_separation(portfolio_client):
    client, fake = portfolio_client
    assert fetch(client, currency="EUR").json()["totals"]["sales_amount"] == "1.00"
    assert {currency for _, currency in fake.seen} == {"EUR"}


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-01"},
        {"end": "2026-10-01T00:00:00Z"},
        {"start": "2026-10-01T00:30:00Z"},
        {"start": "2026-10-01T00:00:00+05:30"},
        {"start": "2026-10-01T01:00:00Z"},
        {"interval": "week"},
        {"currency": "XXXX"},
        {"end": "2028-10-01T00:00:00Z"},
        {"interval": "hour", "end": "2027-01-01T00:00:00Z"},
    ],
)
def test_invalid_windows_fail_before_reads(portfolio_client, params):
    client, fake = portfolio_client
    assert fetch(client, **params).status_code == 422
    assert fake.seen == []


def test_partial_failure_withholds_totals_and_never_leaks_diagnostics(portfolio_client):
    client, fake = portfolio_client
    fake.fail.add(B)
    result = fetch(client).json()
    assert result["complete"] is False
    assert result["ready_tenant_count"] == 1
    assert result["totals"] is None and result["buckets"] is None
    assert result["tenants"][0]["metrics"]["total_order_count"] == 3
    assert result["tenants"][1]["metrics"] is None
    assert "private database" not in json.dumps(result)


def test_empty_membership_and_all_unavailable(portfolio_client):
    client, fake = portfolio_client
    result = fetch(client, "none").json()
    assert result["tenant_count"] == 0 and result["complete"] is True
    assert result["totals"]["total_order_count"] == 0
    assert fake.seen == []
    fake.fail.update([A, B])
    assert fetch(client).json()["totals"] is None


def test_process_fanout_guard(portfolio_client):
    client, fake = portfolio_client
    assert _overview_slot.acquire(blocking=False)
    try:
        result = fetch(client)
        assert result.status_code == 429 and result.headers["retry-after"] == "5"
        assert fake.seen == []
    finally:
        _overview_slot.release()


def test_covered_empty_and_hourly_buckets(portfolio_client, monkeypatch):
    client, fake = portfolio_client
    monkeypatch.setattr(fake, "trends", lambda query: ReadResult([], None))
    result = fetch(client, interval="hour").json()
    assert result["complete"] is True and len(result["buckets"]) == 48
    assert result["totals"] == {"total_order_count": 0, "sales_amount": "0.00"}
    assert all(row["freshness"] is None for row in result["tenants"])


def test_bounded_tenant_count_before_database_reads(portfolio_client):
    client, fake = portfolio_client
    app.dependency_overrides[get_portfolio_registry] = lambda: SimpleNamespace(
        list_active_tenants=lambda: [
            SimpleNamespace(tenant_id=UUID(int=i), name=f"Store {i}") for i in range(1, 202)
        ]
    )
    assert fetch(client, "admin").status_code == 503
    assert fake.seen == []
