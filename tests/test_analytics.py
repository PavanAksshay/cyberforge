from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from analytics_fake import AggregateView, FakeAnalyticsReader
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import Engine

from astraroll.analytics.auth import AuthenticatedPrincipal, get_authenticated_principal
from astraroll.analytics.contracts import (
    Freshness,
    ProductMetrics,
    StatusBreakdownResponse,
    StatusMetrics,
    SummaryMetrics,
    SummaryResponse,
    TopProductsResponse,
    TrendMetrics,
    TrendsResponse,
)
from astraroll.analytics.reader import (
    AnalyticsUnavailable,
    ReaderCapabilities,
    UnsupportedAnalyticsWindow,
    get_analytics_reader,
)
from astraroll.api import app

ENDPOINTS = ("summary", "trends", "top-products", "status-breakdown")
MODELS = (SummaryResponse, TrendsResponse, TopProductsResponse, StatusBreakdownResponse)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_database_access(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Analytics contract routes must not access the database")

    monkeypatch.setattr(Engine, "connect", fail)
    yield
    assert not calls, "Analytics attempted SQL access, including a possible raw-order scan"


@pytest.fixture
def analytics_client():
    previous = app.dependency_overrides.copy()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.fixture
def scope():
    return {
        "tenant_id": str(uuid4()),
        "start": START.isoformat(),
        "end": END.isoformat(),
        "currency": "USD",
    }


@pytest.fixture
def reader(scope):
    reader = FakeAnalyticsReader()
    tenant = UUID(scope["tenant_id"])
    principal = AuthenticatedPrincipal(subject="verified-test-subject", tenant_ids={tenant})
    app.dependency_overrides[get_authenticated_principal] = lambda: principal
    app.dependency_overrides[get_analytics_reader] = lambda: reader
    yield reader
    app.dependency_overrides.pop(get_authenticated_principal, None)
    app.dependency_overrides.pop(get_analytics_reader, None)


@pytest.fixture
def view(reader, scope):
    value = AggregateView(
        summary=SummaryMetrics(total_order_count=4, sales_amount=Decimal("30.50")),
        trends={
            "day": [
                TrendMetrics(bucket_start=START, total_order_count=4, sales_amount=Decimal("30.50"))
            ]
        },
        products=[
            ProductMetrics(
                product_id=UUID(int=2), total_order_count=1, sales_amount=Decimal("15.25")
            ),
            ProductMetrics(
                product_id=UUID(int=1), total_order_count=3, sales_amount=Decimal("15.25")
            ),
        ],
        statuses=[
            StatusMetrics(status="cancelled", total_order_count=1, sales_amount=Decimal("0.00")),
            StatusMetrics(status="shipped", total_order_count=1, sales_amount=Decimal("20.00")),
            StatusMetrics(status="placed", total_order_count=2, sales_amount=Decimal("10.50")),
        ],
    )
    reader.views[(UUID(scope["tenant_id"]), "USD", START, END)] = value
    return value


@pytest.mark.parametrize("endpoint,model", list(zip(ENDPOINTS, MODELS, strict=True)))
def test_routes_and_response_schemas(analytics_client, scope, reader, view, endpoint, model):
    response = analytics_client.get(f"/analytics/{endpoint}", params=scope)
    assert response.status_code == 200, response.text
    result = model.model_validate_json(response.content)
    assert result.tenant_id == UUID(scope["tenant_id"])
    assert result.start == START and result.end == END
    assert result.currency == "USD"
    assert result.metric_semantics == "created_at_current_state_v1"
    assert result.freshness is None
    assert len(reader.calls) == 1
    assert reader.calls[0][1].tenant_id == result.tenant_id


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_uuid_and_headers_do_not_authenticate(analytics_client, scope, endpoint):
    response = analytics_client.get(
        f"/analytics/{endpoint}", params=scope, headers={"X-Tenant-ID": scope["tenant_id"]}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    response = analytics_client.get(
        f"/analytics/{endpoint}",
        params=scope,
        headers={"Authorization": "Bearer arbitrary-unverified-token"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_authorization_unavailable"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_cross_tenant_denial_before_read(analytics_client, scope, reader, endpoint):
    scope["tenant_id"] = str(uuid4())
    response = analytics_client.get(f"/analytics/{endpoint}", params=scope)
    assert response.status_code == 403
    assert not reader.calls


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_absent_real_reader_is_unavailable(analytics_client, scope, reader, endpoint):
    app.dependency_overrides.pop(get_analytics_reader)
    response = analytics_client.get(f"/analytics/{endpoint}", params=scope)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_unavailable"
    assert not reader.calls


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("missing", ["tenant_id", "start", "end", "currency"])
def test_required_query_context(analytics_client, scope, reader, endpoint, missing):
    del scope[missing]
    assert analytics_client.get(f"/analytics/{endpoint}", params=scope).status_code == 422
    assert not reader.calls


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "invalid"},
        {"currency": "usd"},
        {"currency": ""},
        {"start": "2026-10-03T00:00:00Z"},
        {"end": "2026-09-30T00:00:00Z"},
        {"start": "2026-10-01"},
        {"start": "2026-10-01T00:00:00"},
        {"start": "2026-10-01T00:00:00+05:30"},
        {"start": "1790812800"},
        {"start": "2026-10-01T00:01:00Z"},
        {"end": "2026-10-03T00:00:00.001Z"},
        {"end": "2028-01-01T00:00:00Z"},
        {"timezone": "Asia/Calcutta"},
    ],
)
def test_date_and_query_validation(analytics_client, scope, reader, change):
    assert analytics_client.get("/analytics/summary", params={**scope, **change}).status_code == 422
    assert not reader.calls


@pytest.mark.parametrize("limit", [0, 11, -1, "1.5", "true", "all"])
def test_top_product_limit_validation(analytics_client, scope, reader, limit):
    response = analytics_client.get("/analytics/top-products", params={**scope, "limit": limit})
    assert response.status_code == 422
    assert not reader.calls


@pytest.mark.parametrize("limit", [1, 10])
def test_deterministic_top_products(analytics_client, scope, reader, view, limit):
    response = analytics_client.get("/analytics/top-products", params={**scope, "limit": limit})
    assert response.status_code == 200
    products = response.json()["products"]
    assert products[0]["product_id"] == str(UUID(int=1))
    assert len(products) == min(limit, 2)
    assert all(product["sales_amount"] == "15.25" for product in products)
    assert reader.calls[0][1].limit == limit


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_currency_separation(analytics_client, scope, reader, view, endpoint):
    # There is USD data; a covered empty EUR scope must never inherit it.
    response = analytics_client.get(f"/analytics/{endpoint}", params={**scope, "currency": "EUR"})
    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "EUR"
    assert reader.calls[0][1].currency == "EUR"
    if endpoint == "summary":
        assert body["total_order_count"] == 0 and body["sales_amount"] == "0.00"
    elif endpoint == "top-products":
        assert body["products"] == []
    else:
        rows = body["buckets"] if endpoint == "trends" else body["statuses"]
        assert all(row["total_order_count"] == 0 and row["sales_amount"] == "0.00" for row in rows)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_authorized_tenants_remain_separate(analytics_client, scope, reader, view, endpoint):
    other = uuid4()
    app.dependency_overrides[get_authenticated_principal] = lambda: AuthenticatedPrincipal(
        subject="verified-test-subject", tenant_ids={UUID(scope["tenant_id"]), other}
    )
    response = analytics_client.get(
        f"/analytics/{endpoint}", params={**scope, "tenant_id": str(other)}
    )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(other)
    assert reader.calls[0][1].tenant_id == other
    if endpoint == "summary":
        assert response.json()["total_order_count"] == 0
    elif endpoint == "top-products":
        assert response.json()["products"] == []
    else:
        rows = response.json()["buckets"] if endpoint == "trends" else response.json()["statuses"]
        assert all(row["total_order_count"] == 0 for row in rows)


def test_products_sort_by_decimal_sales_before_id(analytics_client, scope, reader, view):
    view.products = [
        ProductMetrics(
            product_id=UUID(int=1),
            total_order_count=10**16,
            sales_amount=Decimal("999999999999999999999999999999.01"),
        ),
        ProductMetrics(
            product_id=UUID(int=2),
            total_order_count=10**16,
            sales_amount=Decimal("999999999999999999999999999999.02"),
        ),
    ]
    response = analytics_client.get("/analytics/top-products", params={**scope, "limit": 1})
    assert response.status_code == 200
    assert response.json()["products"][0]["product_id"] == str(UUID(int=2))
    assert response.json()["products"][0]["sales_amount"] == "999999999999999999999999999999.02"


def test_hourly_sparse_rows_are_sorted_and_zero_filled(analytics_client, scope, reader, view):
    view.trends["hour"] = [
        TrendMetrics(
            bucket_start=START + timedelta(hours=2),
            total_order_count=2,
            sales_amount=Decimal("2.20"),
        ),
        TrendMetrics(bucket_start=START, total_order_count=1, sales_amount=Decimal("1.10")),
    ]
    response = analytics_client.get("/analytics/trends", params={**scope, "interval": "hour"})
    assert response.status_code == 200
    rows = response.json()["buckets"]
    assert len(rows) == 48
    assert [row["total_order_count"] for row in rows[:3]] == [1, 0, 2]
    assert [row["sales_amount"] for row in rows[:3]] == ["1.10", "0.00", "2.20"]


def test_reader_money_cannot_be_float_or_cancelled_sales():
    with pytest.raises(ValidationError):
        SummaryMetrics(total_order_count=1, sales_amount=1.23)
    with pytest.raises(ValidationError):
        StatusMetrics(status="cancelled", total_order_count=1, sales_amount=Decimal("1.00"))


def test_counts_include_cancelled_but_sales_exclude_them(analytics_client, scope, reader, view):
    summary = analytics_client.get("/analytics/summary", params=scope).json()
    statuses = analytics_client.get("/analytics/status-breakdown", params=scope).json()["statuses"]
    assert [row["status"] for row in statuses] == ["placed", "shipped", "cancelled"]
    assert sum(row["total_order_count"] for row in statuses) == summary["total_order_count"] == 4
    assert sum(Decimal(row["sales_amount"]) for row in statuses) == Decimal(summary["sales_amount"])
    assert statuses[2]["sales_amount"] == "0.00"


def test_empty_success_is_distinct_from_unavailable(analytics_client, scope, reader):
    summary = analytics_client.get("/analytics/summary", params=scope).json()
    assert summary["total_order_count"] == 0 and summary["sales_amount"] == "0.00"
    trends = analytics_client.get("/analytics/trends", params=scope).json()["buckets"]
    assert [row["bucket_start"] for row in trends] == [
        "2026-10-01T00:00:00Z",
        "2026-10-02T00:00:00Z",
    ]
    assert all(row["total_order_count"] == 0 and row["sales_amount"] == "0.00" for row in trends)
    assert analytics_client.get("/analytics/top-products", params=scope).json()["products"] == []
    statuses = analytics_client.get("/analytics/status-breakdown", params=scope).json()["statuses"]
    assert [row["status"] for row in statuses] == ["placed", "shipped", "cancelled"]
    assert all(row["total_order_count"] == 0 for row in statuses)


def test_hourly_and_daily_granularity(analytics_client, scope, reader, view):
    reader.granularity = "day"
    assert analytics_client.get("/analytics/trends", params=scope).status_code == 200
    response = analytics_client.get("/analytics/trends", params={**scope, "interval": "hour"})
    assert response.status_code == 422
    reader.granularity = "hour"
    response = analytics_client.get(
        "/analytics/trends",
        params={
            **scope,
            "interval": "hour",
            "start": "2026-10-01T03:00:00Z",
            "end": "2026-10-01T05:00:00Z",
        },
    )
    assert response.status_code == 200
    assert [row["bucket_start"] for row in response.json()["buckets"]] == [
        "2026-10-01T03:00:00Z",
        "2026-10-01T04:00:00Z",
    ]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_day_reader_rejects_partial_days(analytics_client, scope, reader, endpoint):
    reader.granularity = "day"
    assert (
        analytics_client.get(
            f"/analytics/{endpoint}", params={**scope, "start": "2026-10-01T03:00:00Z"}
        ).status_code
        == 422
    )
    assert not reader.calls


@pytest.mark.parametrize("interval", ["week", "month", "minute", "DAY"])
def test_unsupported_intervals(analytics_client, scope, reader, interval):
    assert (
        analytics_client.get(
            "/analytics/trends", params={**scope, "interval": interval}
        ).status_code
        == 422
    )
    assert not reader.calls


def test_freshness_is_only_passed_from_reader(analytics_client, scope, reader, view):
    assert analytics_client.get("/analytics/summary", params=scope).json()["freshness"] is None
    view.freshness = Freshness(checkpoint="durable-test-checkpoint-7", last_processed_at=START)
    for endpoint in ENDPOINTS:
        body = analytics_client.get(f"/analytics/{endpoint}", params=scope).json()
        assert body["freshness"] == {
            "checkpoint": "durable-test-checkpoint-7",
            "last_processed_at": "2026-10-01T00:00:00Z",
        }


def test_unready_reader_does_not_manufacture_zeroes(analytics_client, scope, reader, monkeypatch):
    def not_ready(query):
        raise AnalyticsUnavailable("Projection bootstrap incomplete")

    monkeypatch.setattr(reader, "summary", not_ready)
    response = analytics_client.get("/analytics/summary", params=scope)
    assert response.status_code == 503
    assert "sales_amount" not in response.json()


def test_reader_can_reject_unsupported_coverage(analytics_client, scope, reader, monkeypatch):
    def unsupported(query):
        raise UnsupportedAnalyticsWindow("Product rollups have daily coverage only")

    monkeypatch.setattr(reader, "top_products", unsupported)
    assert analytics_client.get("/analytics/top-products", params=scope).status_code == 422


def test_incompatible_metric_semantics_are_unavailable(
    analytics_client, scope, reader, monkeypatch
):
    monkeypatch.setattr(
        reader,
        "capabilities",
        lambda: ReaderCapabilities(
            base_granularity="day", metric_semantics="event_flow_different_meaning"
        ),
    )
    assert analytics_client.get("/analytics/summary", params=scope).status_code == 503
    assert not reader.calls


def test_invalid_aggregate_groups_are_not_silently_merged(analytics_client, scope, reader, view):
    view.products.append(view.products[0])
    assert analytics_client.get("/analytics/top-products", params=scope).status_code == 503
    view.statuses.append(view.statuses[0])
    assert analytics_client.get("/analytics/status-breakdown", params=scope).status_code == 503
    view.trends["day"].append(view.trends["day"][0])
    assert analytics_client.get("/analytics/trends", params=scope).status_code == 503


@pytest.mark.parametrize("bucket", [END, START + timedelta(hours=1)])
def test_invalid_grid_trends_are_unavailable(analytics_client, scope, reader, view, bucket):
    view.trends["day"] = [
        TrendMetrics(bucket_start=bucket, total_order_count=1, sales_amount=Decimal("1.00"))
    ]
    assert analytics_client.get("/analytics/trends", params=scope).status_code == 503


def test_openapi_publishes_contracts_and_authorization():
    schema = app.openapi()
    for endpoint in ENDPOINTS:
        route = schema["paths"][f"/analytics/{endpoint}"]["get"]
        parameters = {param["name"]: param for param in route["parameters"]}
        assert all(parameters[key]["required"] for key in ("tenant_id", "currency", "start", "end"))
        assert route["security"] == [{"HTTPBearer": []}]
        assert {"200", "401", "403", "422", "503"} <= route["responses"].keys()
