"""Run against the isolated combined checkout; never copy B into dev-a to enable these."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, update

consumer_module = pytest.importorskip("astraroll.aggregation.consumer")
adapter_module = pytest.importorskip("astraroll.aggregation.adapter")
backend_module = pytest.importorskip("astraroll.aggregation.reader")
models_module = pytest.importorskip("astraroll.aggregation.models")
processor_module = pytest.importorskip("astraroll.aggregation.processor")

HourlySummaryAggregate = models_module.HourlySummaryAggregate
OrderProjection = models_module.OrderProjection
OrderEventProcessor = processor_module.OrderEventProcessor

manifest = Path("combined-check-manifest.json")
LEGACY_B = manifest.exists() and json.loads(manifest.read_text())["b_commit"].startswith("36a0aeb")

from astraroll.analytics.contracts import METRIC_SEMANTICS, AnalyticsQuery  # noqa: E402
from astraroll.analytics.postgres import PostgresAnalyticsAdapter  # noqa: E402
from astraroll.analytics.reader import AnalyticsUnavailable  # noqa: E402
from astraroll.models import OutboxEvent, Tenant  # noqa: E402
from astraroll.orders import create_order, mutate_order  # noqa: E402
from astraroll.schemas import OrderCreate, OrderUpdate  # noqa: E402


def test_real_backend_advertisement_checked_before_connection():
    reader = PostgresAnalyticsAdapter(lambda: pytest.fail("Unexpected SQL connection"))
    if getattr(backend_module, "METRIC_SEMANTICS", METRIC_SEMANTICS) != METRIC_SEMANTICS:
        query = AnalyticsQuery(
            tenant_id=uuid4(),
            currency="USD",
            start=datetime(2026, 10, 1, tzinfo=UTC),
            end=datetime(2026, 10, 2, tzinfo=UTC),
        )
        with pytest.raises(AnalyticsUnavailable, match="advertised metric contract"):
            reader.summary(query)
    else:
        assert reader.capabilities().metric_semantics == METRIC_SEMANTICS


@pytest.mark.xfail(
    LEGACY_B, strict=True, reason="B 36a0aeb rounds fractional cents; B-owned adapter fix pending"
)
def test_b_quarantines_fractional_cents():
    payload = json.loads(Path("docs/order-event-v2.json").read_text())
    payload["after"]["amount"] = "12.345"
    assert adapter_module.adapt_stream_message("1-0", json.dumps(payload)).should_quarantine


@pytest.mark.xfail(
    LEGACY_B, strict=True, reason="B 36a0aeb discards the returned XAUTOCLAIM scan cursor"
)
def test_b_recovery_advances_scan_cursor(monkeypatch):
    starts = []

    class Redis:
        def xautoclaim(self, **kwargs):
            starts.append(kwargs["start_id"])
            return [b"50-0", [], []]

    consumer = consumer_module.AggregationConsumer(None, Redis())
    timestamps = iter((100.0, 106.0))
    monkeypatch.setattr(consumer_module.time, "monotonic", lambda: next(timestamps))
    consumer.run_claim_cycle()
    consumer.run_claim_cycle()
    assert starts == ["0-0", "50-0"]


@pytest.mark.integration
def test_redis7_abandoned_event_is_claimed_committed_and_acknowledged(factory, redis_client):
    if int(redis_client.info("server")["redis_version"].split(".")[0]) < 7:
        pytest.skip("Redis 7+ required; Redis 5 cannot verify XAUTOCLAIM")
    tenant = uuid4()
    with factory.begin() as session:
        session.add(Tenant(id=tenant, name="Recovery integration"))
        session.flush()
        order = create_order(
            session, tenant, OrderCreate(product_id=uuid4(), amount="10.00", currency="USD")
        )
        payload = session.scalar(
            select(OutboxEvent.payload).where(OutboxEvent.order_id == order.id)
        )
    stream, group = f"test:recovery:{uuid4()}", f"test:recovery-group:{uuid4()}"
    worker = consumer_module.AggregationConsumer(
        factory,
        redis_client,
        stream_name=stream,
        group_name=group,
        consumer_name="survivor",
        min_idle_claim_ms=0,
    )
    worker.init_group()
    redis_client.xadd(stream, {"event": json.dumps(payload)})
    assert redis_client.xreadgroup(group, "abandoned-worker", {stream: ">"}, count=1)
    assert redis_client.xpending(stream, group)["pending"] == 1
    assert worker.run_claim_cycle() == 1
    assert redis_client.xpending(stream, group)["pending"] == 0
    # A second replay is still harmless at the durable consumer boundary.
    redis_client.xadd(stream, {"event": json.dumps(payload)})
    assert worker.process_batch() == 1
    assert redis_client.xpending(stream, group)["pending"] == 0


@pytest.mark.integration
@pytest.mark.xfail(
    LEGACY_B,
    strict=True,
    reason="B 36a0aeb silently resets invalid prior snapshots and double counts",
)
def test_b_invalid_projection_fails_without_double_counting(factory):
    tenant = uuid4()
    with factory.begin() as session:
        session.add(Tenant(id=tenant, name="Corruption integration"))
        session.flush()
        order = create_order(
            session, tenant, OrderCreate(product_id=uuid4(), amount="100.00", currency="USD")
        )
        order_id = order.id

    def event_at(session, version):
        payload = session.scalar(
            select(OutboxEvent.payload).where(
                OutboxEvent.order_id == order_id, OutboxEvent.order_version == version
            )
        )
        return adapter_module.adapt_stream_message(f"{version}-0", json.dumps(payload)).event

    with factory.begin() as session:
        OrderEventProcessor(session).process_event(event_at(session, 1))
    with factory.begin() as session:
        session.execute(
            update(OrderProjection)
            .where(OrderProjection.tenant_id == tenant, OrderProjection.order_id == order_id)
            .values(canonical_snapshot={})
        )
        mutate_order(
            session, tenant, order_id, 1, OrderUpdate(expected_version=1, status="shipped")
        )
    try:
        with factory.begin() as session:
            OrderEventProcessor(session).process_event(event_at(session, 2))
    except Exception:
        pass  # A safe rollback is acceptable; silent re-initialization is not.
    with factory() as session:
        row = session.scalar(
            select(HourlySummaryAggregate).where(HourlySummaryAggregate.tenant_id == tenant)
        )
        assert row.order_count == 1


@pytest.mark.integration
def test_redis7_commit_before_ack_crash_recovers_without_drift(factory, redis_client, monkeypatch):
    if int(redis_client.info("server")["redis_version"].split(".")[0]) < 7:
        pytest.skip("Redis 7+ required for crash recovery verification")
    tenant = uuid4()
    with factory.begin() as session:
        session.add(Tenant(id=tenant, name="Commit-before-ACK integration"))
        session.flush()
        order = create_order(
            session, tenant, OrderCreate(product_id=uuid4(), amount="10.00", currency="USD")
        )
        payload = session.scalar(
            select(OutboxEvent.payload).where(OutboxEvent.order_id == order.id)
        )
    stream, group = f"test:ack-crash:{uuid4()}", f"test:ack-group:{uuid4()}"
    worker = consumer_module.AggregationConsumer(
        factory,
        redis_client,
        stream_name=stream,
        group_name=group,
        consumer_name="crashing-worker",
        min_idle_claim_ms=0,
        block_ms=1,
    )
    worker.init_group()
    redis_client.xadd(stream, {"event": json.dumps(payload)})
    original_ack = redis_client.xack

    def crash(*args, **kwargs):
        raise ConnectionError("Injected crash after PostgreSQL commit, before XACK")

    monkeypatch.setattr(redis_client, "xack", crash)
    with pytest.raises(ConnectionError):
        worker.process_batch()
    assert redis_client.xpending(stream, group)["pending"] == 1
    with factory() as session:
        assert (
            session.scalar(
                select(HourlySummaryAggregate.order_count).where(
                    HourlySummaryAggregate.tenant_id == tenant
                )
            )
            == 1
        )
    monkeypatch.setattr(redis_client, "xack", original_ack)
    replacement = consumer_module.AggregationConsumer(
        factory,
        redis_client,
        stream_name=stream,
        group_name=group,
        consumer_name="replacement",
        min_idle_claim_ms=0,
        block_ms=1,
    )
    assert replacement.run_claim_cycle() == 1
    assert redis_client.xpending(stream, group)["pending"] == 0
    with factory() as session:
        assert (
            session.scalar(
                select(HourlySummaryAggregate.order_count).where(
                    HourlySummaryAggregate.tenant_id == tenant
                )
            )
            == 1
        )
