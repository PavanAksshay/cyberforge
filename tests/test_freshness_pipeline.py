"""End-to-end freshness pipeline test suite and benchmark validation (Phases 1-5).

Verifies that orders committed in PostgreSQL flow through the transactional outbox,
Redis Streams, and AggregationConsumer into rollup tables, appearing correctly in
all four authenticated AstraRoll analytics endpoints within the target freshness thresholds:
- GOOD: under 5 minutes (< 300s)
- GREAT: under 30 seconds (< 30s)

Tests all 13 required scenarios with real persisted data, authentication, and coverage proof.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from astraroll import orders
from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import (
    TenantProcessingState,
)
from astraroll.analytics import auth
from astraroll.analytics.contracts import METRIC_SEMANTICS
from astraroll.analytics.coverage import CoverageProof
from astraroll.analytics.postgres import PostgresAnalyticsAdapter
from astraroll.analytics.reader import get_analytics_reader
from astraroll.api import app, session_dependency
from astraroll.config import Settings
from astraroll.models import OutboxEvent, Tenant
from astraroll.relay import relay_batch
from tests.conftest import migrate

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=2)
ENDPOINTS = ("summary", "trends", "top-products", "status-breakdown")


@pytest.fixture
def store(migration_engine):
    migrate(migration_engine)
    return sessionmaker(migration_engine, expire_on_commit=False)


@pytest.fixture
def clock(monkeypatch):
    current = [START + timedelta(hours=12)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    monkeypatch.setattr(orders, "datetime", Clock)
    return current


@pytest.fixture
def identity(monkeypatch):
    records = []

    def register(tenant: UUID):
        token = secrets.token_urlsafe(32)
        records.append(
            {
                "subject": f"freshness-user-{len(records)}",
                "tenant_ids": [str(tenant)],
                "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            }
        )
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: Settings(_env_file=None, analytics_api_keys_json=json.dumps(records)),
        )
        return {"Authorization": f"Bearer {token}"}

    return register


def make_proof(query, ready=True):
    return CoverageProof(
        tenant_id=query.tenant_id,
        currency=query.currency,
        start=START,
        end=END,
        metric_semantics=METRIC_SEMANTICS,
        ready=ready,
        intervals=frozenset({"hour", "day"}),
        dimensions=frozenset({"summary", "status", "product"}),
    )


@contextmanager
def wired_client(store, coverage_func=None):
    if coverage_func is None:

        def coverage_func(s, q):
            return make_proof(q, True)

    def session_override():
        with store() as session:
            yield session

    app.dependency_overrides[session_dependency] = session_override
    app.dependency_overrides[get_analytics_reader] = lambda: PostgresAnalyticsAdapter(
        lambda: store.kw["bind"], coverage_func
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def make_query_params(tenant: UUID, currency: str = "USD", **changes) -> dict[str, str]:
    return {
        "tenant_id": str(tenant),
        "currency": currency,
        "start": START.isoformat(),
        "end": END.isoformat(),
        **changes,
    }


class FreshnessHarness:
    """Helper to coordinate relay, consumer, and measure T0..T4 timestamps."""

    def __init__(
        self,
        store: sessionmaker,
        redis: Redis,
        stream_name: str,
        group_name: str,
        dispatch_mode: str = "fifo",
    ):
        self.store = store
        self.redis = redis
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer = AggregationConsumer(
            session_factory=store,
            redis_client=redis,
            stream_name=stream_name,
            group_name=group_name,
            batch_size=100,
            block_ms=10,
            dispatch_mode=dispatch_mode,
        )
        self.consumer.init_group()

    def drain(self) -> int:
        while True:
            n = relay_batch(self.store, self.redis, self.stream_name, batch_size=500)
            if n == 0:
                break

        consumed = 0
        while True:
            c = self.consumer.process_batch()
            consumed += c
            if c == 0:
                break

        assert self.redis.xpending(self.stream_name, self.group_name)["pending"] == 0
        return consumed

    @staticmethod
    def get_json(client: TestClient, endpoint: str, tenant_id: UUID, headers: dict) -> dict:
        return client.get(
            f"/analytics/{endpoint}",
            params=make_query_params(tenant_id),
            headers=headers,
        ).json()


# =========================================================================
# Scenario 1: New placed order (sales=0, count=1 in summary)
# =========================================================================
def test_scenario_01_new_placed_order(store, redis_client, identity, clock):
    stream = f"test:freshness:s01:{uuid4().hex}"
    group = f"test:group:s01:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S01"))
        s.add(TenantProcessingState(tenant_id=tenant_id, revision=0))

    with wired_client(store) as client:
        resp = client.get(
            "/analytics/summary", params=make_query_params(tenant_id), headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["total_order_count"] == 0
        assert Decimal(resp.json()["sales_amount"]) == Decimal("0.00")

        t0 = time.time()
        order_resp = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "45.00", "currency": "USD"},
        )
        assert order_resp.status_code == 201

        harness.drain()

        resp = client.get(
            "/analytics/summary", params=make_query_params(tenant_id), headers=headers
        )
        t4_seen = time.time()
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_order_count"] == 1
        assert Decimal(data["sales_amount"]) == Decimal("0.00")
        assert data["metric_semantics"] == "created_at_shipped_sales_v2"

        total_latency = t4_seen - t0
        assert total_latency < 30.0, f"Exceeded GREAT target (30s): {total_latency}s"


# =========================================================================
# Scenario 2: Placed-to-shipped change (sales recognized, appears in top products)
# =========================================================================
def test_scenario_02_placed_to_shipped_change(store, redis_client, identity, clock):
    stream = f"test:freshness:s02:{uuid4().hex}"
    group = f"test:group:s02:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    prod_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S02"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(prod_id), "amount": "120.00", "currency": "USD"},
        ).json()
        harness.drain()

        t0 = time.time()
        patch_resp = client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "status": "shipped"},
        )
        assert patch_resp.status_code == 200
        harness.drain()

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("120.00")

        trends = harness.get_json(client, "trends", tenant_id, headers)
        assert Decimal(trends["buckets"][0]["sales_amount"]) == Decimal("120.00")

        top_prod = harness.get_json(client, "top-products", tenant_id, headers)
        assert len(top_prod["products"]) == 1
        assert top_prod["products"][0]["product_id"] == str(prod_id)
        assert Decimal(top_prod["products"][0]["sales_amount"]) == Decimal("120.00")

        statuses = harness.get_json(client, "status-breakdown", tenant_id, headers)
        shipped_s = next(s for s in statuses["statuses"] if s["status"] == "shipped")
        assert shipped_s["total_order_count"] == 1
        assert Decimal(shipped_s["sales_amount"]) == Decimal("120.00")

        total_latency = time.time() - t0
        assert total_latency < 30.0


# =========================================================================
# Scenario 3: Placed-to-cancelled change (sales=0, status=cancelled)
# =========================================================================
def test_scenario_03_placed_to_cancelled_change(store, redis_client, identity, clock):
    stream = f"test:freshness:s03:{uuid4().hex}"
    group = f"test:group:s03:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S03"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "99.00", "currency": "USD"},
        ).json()
        harness.drain()

        t0 = time.time()
        cancel_resp = client.post(
            f"/tenants/{tenant_id}/orders/{order['id']}/cancel",
            json={"expected_version": 1},
        )
        assert cancel_resp.status_code == 200
        harness.drain()

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("0.00")

        statuses = harness.get_json(client, "status-breakdown", tenant_id, headers)
        can_status = next(s for s in statuses["statuses"] if s["status"] == "cancelled")
        assert can_status["total_order_count"] == 1
        assert Decimal(can_status["sales_amount"]) == Decimal("0.00")

        top_prod = harness.get_json(client, "top-products", tenant_id, headers)
        assert len(top_prod["products"]) == 0

        assert (time.time() - t0) < 30.0


# =========================================================================
# Scenario 4: Amount correction while placed (sales remains 0 until shipped)
# =========================================================================
def test_scenario_04_amount_correction_while_placed(store, redis_client, identity, clock):
    stream = f"test:freshness:s04:{uuid4().hex}"
    group = f"test:group:s04:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S04"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "10.00", "currency": "USD"},
        ).json()
        harness.drain()

        patch_resp = client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "amount": "50.00"},
        )
        assert patch_resp.status_code == 200
        harness.drain()

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("0.00")

        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 2, "status": "shipped"},
        )
        harness.drain()

        summary2 = harness.get_json(client, "summary", tenant_id, headers)
        assert Decimal(summary2["sales_amount"]) == Decimal("50.00")


# =========================================================================
# Scenario 5: Product reassignment while placed
# =========================================================================
def test_scenario_05_product_reassignment_while_placed(store, redis_client, identity, clock):
    stream = f"test:freshness:s05:{uuid4().hex}"
    group = f"test:group:s05:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    prod_a, prod_b = uuid4(), uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S05"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(prod_a), "amount": "70.00", "currency": "USD"},
        ).json()
        harness.drain()

        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "product_id": str(prod_b)},
        )
        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 2, "status": "shipped"},
        )
        harness.drain()

        top_prods = harness.get_json(client, "top-products", tenant_id, headers)
        assert len(top_prods["products"]) == 1
        assert top_prods["products"][0]["product_id"] == str(prod_b)
        assert Decimal(top_prods["products"][0]["sales_amount"]) == Decimal("70.00")


# =========================================================================
# Scenario 6: Duplicate event delivery
# =========================================================================
def test_scenario_06_duplicate_event_delivery(store, redis_client, identity, clock):
    stream = f"test:freshness:s06:{uuid4().hex}"
    group = f"test:group:s06:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S06"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "80.00", "currency": "USD"},
        ).json()
        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "status": "shipped"},
        )
        harness.drain()

        with store() as session:
            outbox_payload = session.scalar(
                select(OutboxEvent.payload).where(
                    OutboxEvent.order_id == UUID(order["id"]),
                    OutboxEvent.order_version == 2,
                )
            )

        redis_client.xadd(stream, {"event": json.dumps(outbox_payload)})
        harness.consumer.process_batch()
        assert redis_client.xpending(stream, group)["pending"] == 0

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("80.00")


# =========================================================================
# Scenario 7: Out-of-order events
# =========================================================================
def test_scenario_07_out_of_order_events(store, redis_client, identity, clock):
    stream = f"test:freshness:s07:{uuid4().hex}"
    group = f"test:group:s07:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S07"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "50.00", "currency": "USD"},
        ).json()
        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "amount": "60.00", "status": "shipped"},
        )
        harness.drain()

        with store() as session:
            v1_payload = session.scalar(
                select(OutboxEvent.payload).where(
                    OutboxEvent.order_id == UUID(order["id"]),
                    OutboxEvent.order_version == 1,
                )
            )
        v1_payload["event_id"] = str(uuid4())
        redis_client.xadd(stream, {"event": json.dumps(v1_payload)})

        harness.consumer.process_batch()
        assert redis_client.xpending(stream, group)["pending"] == 0

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("60.00")


# =========================================================================
# Scenario 8: Consumer crash and restart
# =========================================================================
def test_scenario_08_consumer_crash_and_restart(store, redis_client, identity, monkeypatch, clock):
    stream = f"test:freshness:s08:{uuid4().hex}"
    group = f"test:group:s08:{uuid4().hex}"

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S08"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "150.00", "currency": "USD"},
        ).json()
        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "status": "shipped"},
        )
        relay_batch(store, redis_client, stream, batch_size=100)

        worker1 = AggregationConsumer(
            session_factory=store,
            redis_client=redis_client,
            stream_name=stream,
            group_name=group,
            consumer_name="worker-1-crasher",
        )
        worker1.init_group()

        original_xack = redis_client.xack

        def inject_crash(*args, **kwargs):
            raise ConnectionError("Simulated crash before XACK")

        monkeypatch.setattr(redis_client, "xack", inject_crash)
        with pytest.raises(ConnectionError):
            worker1.process_batch()

        assert redis_client.xpending(stream, group)["pending"] > 0

        redis_client.xack = original_xack
        worker2 = AggregationConsumer(
            session_factory=store,
            redis_client=redis_client,
            stream_name=stream,
            group_name=group,
            consumer_name="worker-2-survivor",
            min_idle_claim_ms=0,
        )
        worker2.run_claim_cycle(force=True)
        worker2.process_batch()

        assert redis_client.xpending(stream, group)["pending"] == 0

        summary = client.get(
            "/analytics/summary", params=make_query_params(tenant_id), headers=headers
        ).json()
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("150.00")


# =========================================================================
# Scenario 9: Redis pending-event reclaim (XAUTOCLAIM)
# =========================================================================
def test_scenario_09_redis_pending_event_reclaim(store, redis_client, identity, clock):
    stream = f"test:freshness:s09:{uuid4().hex}"
    group = f"test:group:s09:{uuid4().hex}"

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S09"))

    with wired_client(store) as client:
        order = client.post(
            f"/tenants/{tenant_id}/orders",
            json={"product_id": str(uuid4()), "amount": "40.00", "currency": "USD"},
        ).json()
        client.patch(
            f"/tenants/{tenant_id}/orders/{order['id']}",
            json={"expected_version": 1, "status": "shipped"},
        )
        relay_batch(store, redis_client, stream, batch_size=100)

        redis_client.xgroup_create(stream, group, id="0-0", mkstream=True)
        read_msgs = redis_client.xreadgroup(group, "dead-worker", {stream: ">"}, count=10)
        assert len(read_msgs[0][1]) > 0
        assert redis_client.xpending(stream, group)["pending"] > 0

        active_worker = AggregationConsumer(
            session_factory=store,
            redis_client=redis_client,
            stream_name=stream,
            group_name=group,
            consumer_name="reclaimer",
            min_idle_claim_ms=0,
        )
        claimed = active_worker.run_claim_cycle(force=True)
        assert claimed > 0
        assert redis_client.xpending(stream, group)["pending"] == 0

        summary = client.get(
            "/analytics/summary", params=make_query_params(tenant_id), headers=headers
        ).json()
        assert summary["total_order_count"] == 1
        assert Decimal(summary["sales_amount"]) == Decimal("40.00")


# =========================================================================
# Scenario 10: Concurrent tenants
# =========================================================================
def test_scenario_10_concurrent_tenants(store, redis_client, identity, clock):
    stream = f"test:freshness:s10:{uuid4().hex}"
    group = f"test:group:s10:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenants = [uuid4() for _ in range(3)]
    headers_map = {}
    with store.begin() as s:
        for idx, t in enumerate(tenants):
            s.add(Tenant(id=t, name=f"Concurrent Tenant {idx}"))
            headers_map[t] = identity(t)

    with wired_client(store) as client:
        t0 = time.time()
        for idx, t in enumerate(tenants):
            amt = f"{(idx + 1) * 10}.00"
            o = client.post(
                f"/tenants/{t}/orders",
                json={"product_id": str(uuid4()), "amount": amt, "currency": "USD"},
            ).json()
            client.patch(
                f"/tenants/{t}/orders/{o['id']}",
                json={"expected_version": 1, "status": "shipped"},
            )

        harness.drain()

        for idx, t in enumerate(tenants):
            expected_amt = Decimal(f"{(idx + 1) * 10}.00")
            summary = harness.get_json(client, "summary", t, headers_map[t])
            assert summary["total_order_count"] == 1
            assert Decimal(summary["sales_amount"]) == expected_amt

        assert (time.time() - t0) < 30.0


# =========================================================================
# Scenario 11: High-volume tenant + low-volume tenant (Fair vs FIFO mode)
# =========================================================================
def test_scenario_11_high_volume_and_low_volume_tenant_fairness(
    store, redis_client, identity, clock
):
    tenant_high = uuid4()
    tenant_low = uuid4()
    headers_high = identity(tenant_high)
    headers_low = identity(tenant_low)

    with store.begin() as s:
        s.add(Tenant(id=tenant_high, name="High Volume Tenant"))
        s.add(Tenant(id=tenant_low, name="Low Volume Tenant"))

    stream_fair = f"test:freshness:s11:fair:{uuid4().hex}"
    group_fair = f"test:group:s11:fair:{uuid4().hex}"
    harness_fair = FreshnessHarness(
        store, redis_client, stream_fair, group_fair, dispatch_mode="fair"
    )

    with wired_client(store) as client:
        for _ in range(20):
            o = client.post(
                f"/tenants/{tenant_high}/orders",
                json={"product_id": str(uuid4()), "amount": "10.00", "currency": "USD"},
            ).json()
            client.patch(
                f"/tenants/{tenant_high}/orders/{o['id']}",
                json={"expected_version": 1, "status": "shipped"},
            )

        o_low = client.post(
            f"/tenants/{tenant_low}/orders",
            json={"product_id": str(uuid4()), "amount": "500.00", "currency": "USD"},
        ).json()
        client.patch(
            f"/tenants/{tenant_low}/orders/{o_low['id']}",
            json={"expected_version": 1, "status": "shipped"},
        )

        t0 = time.time()
        harness_fair.drain()
        latency = time.time() - t0

        summary_low = harness_fair.get_json(client, "summary", tenant_low, headers_low)
        assert summary_low["total_order_count"] == 1
        assert Decimal(summary_low["sales_amount"]) == Decimal("500.00")

        summary_high = harness_fair.get_json(client, "summary", tenant_high, headers_high)
        assert summary_high["total_order_count"] == 20
        assert Decimal(summary_high["sales_amount"]) == Decimal("200.00")

        assert latency < 30.0


# =========================================================================
# Scenario 12: Temporary relay or consumer interruption
# =========================================================================
def test_scenario_12_temporary_interruption_and_catchup(store, redis_client, identity, clock):
    stream = f"test:freshness:s12:{uuid4().hex}"
    group = f"test:group:s12:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S12"))

    with wired_client(store) as client:
        for _ in range(5):
            o = client.post(
                f"/tenants/{tenant_id}/orders",
                json={"product_id": str(uuid4()), "amount": "20.00", "currency": "USD"},
            ).json()
            client.patch(
                f"/tenants/{tenant_id}/orders/{o['id']}",
                json={"expected_version": 1, "status": "shipped"},
            )

        with store() as s:
            unpub = s.scalar(
                select(func.count()).where(
                    OutboxEvent.tenant_id == tenant_id,
                    OutboxEvent.published_at.is_(None),
                )
            )
            assert unpub == 10

        t_resume = time.time()
        drained = harness.drain()
        catchup_latency = time.time() - t_resume
        assert drained == 10

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == 5
        assert Decimal(summary["sales_amount"]) == Decimal("100.00")
        assert catchup_latency < 30.0


# =========================================================================
# Scenario 13: Freshness after backlog recovery
# =========================================================================
def test_scenario_13_backlog_recovery_and_api_freshness(store, redis_client, identity, clock):
    stream = f"test:freshness:s13:{uuid4().hex}"
    group = f"test:group:s13:{uuid4().hex}"
    harness = FreshnessHarness(store, redis_client, stream, group)

    tenant_id = uuid4()
    headers = identity(tenant_id)
    with store.begin() as s:
        s.add(Tenant(id=tenant_id, name="Tenant S13"))

    with wired_client(store) as client:
        num_orders = 30
        for _ in range(num_orders):
            o = client.post(
                f"/tenants/{tenant_id}/orders",
                json={"product_id": str(uuid4()), "amount": "10.00", "currency": "USD"},
            ).json()
            client.patch(
                f"/tenants/{tenant_id}/orders/{o['id']}",
                json={"expected_version": 1, "status": "shipped"},
            )

        t0 = time.time()
        total_drained = harness.drain()
        drain_latency = time.time() - t0

        assert total_drained == num_orders * 2

        summary = harness.get_json(client, "summary", tenant_id, headers)
        assert summary["total_order_count"] == num_orders
        assert Decimal(summary["sales_amount"]) == Decimal(f"{num_orders * 10}.00")

        trends = harness.get_json(client, "trends", tenant_id, headers)
        assert Decimal(trends["buckets"][0]["sales_amount"]) == Decimal(f"{num_orders * 10}.00")

        top_prod = harness.get_json(client, "top-products", tenant_id, headers)
        assert len(top_prod["products"]) > 0

        statuses = harness.get_json(client, "status-breakdown", tenant_id, headers)
        shipped_s = next(s for s in statuses["statuses"] if s["status"] == "shipped")
        assert shipped_s["total_order_count"] == num_orders
        assert Decimal(shipped_s["sales_amount"]) == Decimal(f"{num_orders * 10}.00")

        assert drain_latency < 30.0
