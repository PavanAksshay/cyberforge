"""Real aggregate/API checks, enabled only in the temporary combined checkout.

Coverage is a test-harness assertion AFTER known input is processed, not an
application provider. Aggregate values always come from B's actual PostgreSQL
reader. No B implementation files or tables are duplicated here.
"""

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from conftest import migrate
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.orm import sessionmaker

aggregation = pytest.importorskip("astraroll.aggregation.models")
consumer_module = pytest.importorskip("astraroll.aggregation.consumer")
backend_module = pytest.importorskip("astraroll.aggregation.reader")
replay_module = pytest.importorskip("astraroll.aggregation.replay")

from astraroll import orders  # noqa: E402
from astraroll.analytics import auth  # noqa: E402
from astraroll.analytics.contracts import METRIC_SEMANTICS  # noqa: E402
from astraroll.analytics.coverage import CoverageProof  # noqa: E402
from astraroll.analytics.postgres import PostgresAnalyticsAdapter  # noqa: E402
from astraroll.analytics.reader import get_analytics_reader  # noqa: E402
from astraroll.api import app, session_dependency  # noqa: E402
from astraroll.config import Settings  # noqa: E402
from astraroll.models import OutboxEvent, Tenant  # noqa: E402
from astraroll.relay import relay_batch  # noqa: E402

pytestmark = pytest.mark.integration
START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=1)
ENDPOINTS = ("summary", "trends", "top-products", "status-breakdown")


@pytest.fixture
def store(migration_engine):
    advertised = getattr(backend_module, "METRIC_SEMANTICS", METRIC_SEMANTICS)
    if advertised != METRIC_SEMANTICS:
        pytest.skip(
            f"B advertises {advertised}; owner approved {METRIC_SEMANTICS}; real reads blocked"
        )
    migrate(migration_engine)
    return sessionmaker(migration_engine, expire_on_commit=False)


@pytest.fixture
def identity(monkeypatch):
    records = []

    def register(tenant):
        token = secrets.token_urlsafe(32)
        records.append(
            {
                "subject": f"subject-{len(records)}",
                "tenant_ids": [str(tenant)],
                "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        )
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: Settings(_env_file=None, analytics_api_keys_json=json.dumps(records)),
        )
        return {"Authorization": f"Bearer {token}"}

    return register


@contextmanager
def wired_client(store, coverage):
    previous = app.dependency_overrides.copy()

    def session_override():
        with store() as session:
            yield session

    app.dependency_overrides[session_dependency] = session_override
    app.dependency_overrides[get_analytics_reader] = lambda: PostgresAnalyticsAdapter(
        lambda: store.kw["bind"], coverage
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def proof(query, ready=True, intervals=frozenset({"hour", "day"})):
    return CoverageProof(
        tenant_id=query.tenant_id,
        currency=query.currency,
        start=START,
        end=END + timedelta(days=1),
        metric_semantics=METRIC_SEMANTICS,
        ready=ready,
        intervals=intervals,
        dimensions={"summary", "status", "product"},
    )


def params(tenant, currency="USD", **changes):
    return {
        "tenant_id": str(tenant),
        "currency": currency,
        "start": START.isoformat(),
        "end": END.isoformat(),
        **changes,
    }


def test_full_pipeline_and_all_four_authenticated_endpoints(
    store, redis_client, identity, monkeypatch
):
    engine = store.kw["bind"]
    current = [START + timedelta(hours=12, minutes=15)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    monkeypatch.setattr(orders, "datetime", Clock)
    stream, group = f"test:api-pipeline:{uuid4()}", f"test:api-group:{uuid4()}"
    worker = consumer_module.AggregationConsumer(
        store, redis_client, stream_name=stream, group_name=group, batch_size=1000, block_ms=1
    )
    worker.init_group()
    ready = set()

    def coverage(session, query):
        return proof(query, (query.tenant_id, query.currency) in ready)

    def drain():
        while relay_batch(store, redis_client, stream, batch_size=1000):
            pass
        while worker.process_batch():
            pass
        assert redis_client.xpending(stream, group)["pending"] == 0

    with wired_client(store, coverage) as client:
        a = UUID(client.post("/tenants", json={"name": "Pipeline A"}).json()["id"])
        b = UUID(client.post("/tenants", json={"name": "Pipeline B"}).json()["id"])
        headers_a, headers_b = identity(a), identity(b)
        # Before known coverage, even valid credentials cannot produce success zeros.
        assert (
            client.get("/analytics/summary", params=params(a), headers=headers_a).status_code == 503
        )

        def create(tenant, product, amount, currency="USD"):
            current[0] = START + timedelta(hours=12, minutes=15)
            response = client.post(
                f"/tenants/{tenant}/orders",
                json={"product_id": str(product), "amount": amount, "currency": currency},
            )
            assert response.status_code == 201, response.text
            drain()
            return response.json()

        def patch(tenant, order, **changes):
            current[0] = END + timedelta(hours=13)
            response = client.patch(
                f"/tenants/{tenant}/orders/{order['id']}",
                json={"expected_version": order["version"], **changes},
            )
            assert response.status_code == 200, response.text
            drain()
            assert response.json()["created_at"] == order["created_at"]
            return response.json()

        # One order is repriced/reassigned while placed, then shipped on the next day.
        first = create(a, UUID(int=9), "10.00")
        first = patch(a, first, amount="25.00", product_id=str(UUID(int=2)))
        patch(a, first, status="shipped")
        patch(a, create(a, UUID(int=2), "25.00"), status="shipped")
        patch(a, create(a, UUID(int=1), "50.00"), status="shipped")
        patch(a, create(a, UUID(int=3), "25.00"), status="shipped")
        patch(a, create(a, UUID(int=3), "25.00"), status="shipped")
        cancelled = create(a, UUID(int=8), "100.00")
        response = client.post(
            f"/tenants/{a}/orders/{cancelled['id']}/cancel", json={"expected_version": 1}
        )
        assert response.status_code == 200
        drain()
        create(a, UUID(int=7), "5.00")
        patch(a, create(a, UUID(int=5), "99.00", "EUR"), status="shipped")
        patch(b, create(b, UUID(int=6), "1000.00"), status="shipped")
        ready.update({(a, "USD"), (a, "EUR"), (b, "USD")})
        # Harness certification: all generated outbox work was drained above. This
        # is NOT inferred from first revision and is NOT a production provider.
        with store() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                )
                == 0
            )
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            bodies = {}
            for endpoint in ENDPOINTS:
                response = client.get(f"/analytics/{endpoint}", params=params(a), headers=headers_a)
                assert response.status_code == 200, response.text
                bodies[endpoint] = response.json()
            summary = bodies["summary"]
            assert summary["total_order_count"] == 7
            assert Decimal(summary["sales_amount"]) == Decimal("150.00")
            assert summary["metric_semantics"] == "created_at_shipped_sales_v2"
            assert summary["freshness"]["tenant_revision"] > 0
            assert summary["freshness"]["checkpoint"] is None
            statuses = bodies["status-breakdown"]["statuses"]
            assert [(s["status"], s["total_order_count"]) for s in statuses] == [
                ("placed", 1),
                ("shipped", 5),
                ("cancelled", 1),
            ]
            assert [Decimal(s["sales_amount"]) for s in statuses] == [
                Decimal(0),
                Decimal(150),
                Decimal(0),
            ]
            products = bodies["top-products"]["products"]
            assert [p["product_id"] for p in products] == [
                str(UUID(int=2)),
                str(UUID(int=3)),
                str(UUID(int=1)),
            ]
            limit = client.get(
                "/analytics/top-products", params=params(a, limit=1), headers=headers_a
            )
            assert limit.json()["products"][0]["product_id"] == str(UUID(int=2))
            assert len(limit.json()["products"]) == 1
            assert bodies["trends"]["buckets"][0]["total_order_count"] == 7
            # Updates on day two do not move the order creation cohort.
            empty = client.get(
                "/analytics/summary",
                params=params(a, start=END.isoformat(), end=(END + timedelta(days=1)).isoformat()),
                headers=headers_a,
            )
            assert empty.json()["total_order_count"] == 0
            hourly = client.get(
                "/analytics/trends", params=params(a, interval="hour"), headers=headers_a
            )
            assert len(hourly.json()["buckets"]) == 24
            assert hourly.json()["buckets"][12]["total_order_count"] == 7
            assert Decimal(
                client.get("/analytics/summary", params=params(a, "EUR"), headers=headers_a).json()[
                    "sales_amount"
                ]
            ) == Decimal("99.00")
            assert Decimal(
                client.get("/analytics/summary", params=params(b), headers=headers_b).json()[
                    "sales_amount"
                ]
            ) == Decimal("1000.00")
            before_denied = len(statements)
            assert (
                client.get("/analytics/summary", params=params(b), headers=headers_a).status_code
                == 403
            )
            assert len(statements) == before_denied
        finally:
            event.remove(engine, "before_cursor_execute", record)
        # Exact query-time table audit: aggregate tables + tenant processing state;
        # no raw orders, outbox, projections or Redis fallback on analytics reads.
        sql = " ".join(statements).lower()
        assert "from orders " not in sql and "join orders " not in sql
        assert "outbox_events" not in sql and "order_projections" not in sql
        assert "hourly_summary_aggregates" in sql
        # Replay after commit and an old version under a new event ID cannot drift totals.
        replay_module.replay_outbox_events(store, redis_client, stream_name=stream)
        with store() as session:
            old = session.scalar(
                select(OutboxEvent.payload).where(
                    OutboxEvent.order_id == UUID(first["id"]), OutboxEvent.order_version == 1
                )
            )
        old["event_id"] = str(uuid4())
        redis_client.xadd(stream, {"event": json.dumps(old)})
        redis_client.xadd(stream, {"event": '{"schema_version":1}'})
        drain()
        for endpoint in ENDPOINTS:
            assert (
                client.get(f"/analytics/{endpoint}", params=params(a), headers=headers_a).json()
                == bodies[endpoint]
            )


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_unready_vs_covered_empty_and_grain(store, identity, endpoint):
    tenant = uuid4()
    headers = identity(tenant)
    with store.begin() as session:
        session.add(Tenant(id=tenant, name="Known empty fixture"))
        session.add(aggregation.TenantProcessingState(tenant_id=tenant, revision=0))
    with wired_client(store, lambda s, q: proof(q, False)) as client:
        assert (
            client.get(f"/analytics/{endpoint}", params=params(tenant), headers=headers).status_code
            == 503
        )
    with wired_client(store, lambda s, q: proof(q)) as client:
        response = client.get(f"/analytics/{endpoint}", params=params(tenant), headers=headers)
        assert response.status_code == 200, response.text
        if endpoint == "summary":
            assert response.json()["total_order_count"] == 0
        assert response.json()["freshness"] == {
            "checkpoint": None,
            "last_processed_at": None,
            "tenant_revision": 0,
        }
    with wired_client(store, lambda s, q: proof(q, intervals=frozenset({"day"}))) as client:
        assert (
            client.get(
                f"/analytics/{endpoint}",
                params=params(tenant, start=(START + timedelta(hours=1)).isoformat()),
                headers=headers,
            ).status_code
            == 422
        )


def test_revision_and_metrics_share_repeatable_read_snapshot(store, identity, monkeypatch):
    tenant = uuid4()
    headers = identity(tenant)
    with store.begin() as session:
        session.add(Tenant(id=tenant, name="Snapshot fixture"))
        session.add(
            aggregation.TenantProcessingState(
                tenant_id=tenant, revision=1, last_successful_apply_at=START
            )
        )
        session.add(
            aggregation.HourlySummaryAggregate(
                tenant_id=tenant,
                currency="USD",
                bucket_start=START,
                order_count=1,
                sales_amount=Decimal("10.00"),
            )
        )
    original = backend_module.PostgresAnalyticsReader.get_summary

    def concurrent_writer(backend, **kwargs):
        assert backend.session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert backend.session.scalar(text("SHOW transaction_read_only")) == "on"
        with store.begin() as writer:
            writer.execute(
                update(aggregation.HourlySummaryAggregate)
                .where(aggregation.HourlySummaryAggregate.tenant_id == tenant)
                .values(sales_amount=Decimal("22.00"))
            )
            writer.execute(
                update(aggregation.TenantProcessingState)
                .where(aggregation.TenantProcessingState.tenant_id == tenant)
                .values(revision=2)
            )
        return original(backend, **kwargs)

    monkeypatch.setattr(backend_module.PostgresAnalyticsReader, "get_summary", concurrent_writer)
    with wired_client(store, lambda s, q: proof(q)) as client:
        response = client.get("/analytics/summary", params=params(tenant), headers=headers)
        assert response.status_code == 200, response.text
        assert Decimal(response.json()["sales_amount"]) == Decimal("10.00")
        assert response.json()["freshness"]["tenant_revision"] == 1
        monkeypatch.setattr(backend_module.PostgresAnalyticsReader, "get_summary", original)
        response = client.get("/analytics/summary", params=params(tenant), headers=headers)
        assert Decimal(response.json()["sales_amount"]) == Decimal("22.00")
        assert response.json()["freshness"]["tenant_revision"] == 2


def test_b_processing_revision_alone_does_not_enable_default_reader(store, identity):
    tenant = uuid4()
    headers = identity(tenant)
    with store.begin() as session:
        session.add(aggregation.TenantProcessingState(tenant_id=tenant, revision=100))
    with wired_client(store, None) as client:
        app.dependency_overrides[get_analytics_reader] = lambda: PostgresAnalyticsAdapter(
            lambda: store.kw["bind"]
        )
        response = client.get("/analytics/summary", params=params(tenant), headers=headers)
        assert response.status_code == 503
        assert "coverage" in response.json()["detail"]["message"]
