"""Live outbox hints and recovery use the existing reducer without historical drain."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from astraroll.aggregation.bootstrap import bootstrap
from astraroll.aggregation.models import OrderProjection
from astraroll.config import get_settings
from astraroll.models import OutboxEvent
from astraroll.orders import create_order, mutate_order
from astraroll.schemas import OrderCreate, OrderUpdate
from astraroll.v2.live_worker import LiveOutboxWorker, eligible_orders, hint_committed_order

pytestmark = pytest.mark.integration


def test_worker_skips_baseline_history_and_recovers_lost_hint(
    factory, tenant_id, redis_client, monkeypatch, tmp_path
):
    settings = get_settings()
    monkeypatch.setattr(settings, "order_stream", "test:live-worker:" + uuid4().hex)
    old_product = uuid4()
    new_product = uuid4()
    with factory.begin() as s:
        old = create_order(
            s, tenant_id, OrderCreate(product_id=old_product, amount="10.00", currency="USD")
        )
        old_id = old.id
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    bootstrap(factory, tenant_id, ["USD"], start, start + timedelta(days=1))
    with factory.begin() as s:
        new = create_order(
            s, tenant_id, OrderCreate(product_id=new_product, amount="506.97", currency="USD")
        )
        mutate_order(s, tenant_id, new.id, 1, OrderUpdate(expected_version=1, status="shipped"))
        new_id = new.id

    class Manager:
        def session(self, tid):
            assert str(tid) == str(tenant_id)
            return factory()

    health = tmp_path / "worker.json"
    worker = LiveOutboxWorker(Manager(), redis_client, [tenant_id], health)
    # No Redis hint: durable PostgreSQL fallback must recover the new work.
    with factory() as s:
        assert eligible_orders(s, tenant_id) == [new_id]
    worker.tick()
    with factory() as s:
        assert eligible_orders(s, tenant_id) == []
        assert s.get(OrderProjection, (tenant_id, new_id)).last_applied_version == 2
        old_events = s.scalars(select(OutboxEvent).where(OutboxEvent.order_id == old_id)).all()
        assert all(e.published_at is None for e in old_events)
        new_events = s.scalars(select(OutboxEvent).where(OutboxEvent.order_id == new_id)).all()
        assert all(e.published_at is not None for e in new_events)
    assert json.loads(health.read_text())["status"] == "running"
    assert redis_client.xlen(settings.order_stream) == 2
    assert redis_client.xpending(settings.order_stream, "astraroll-live-imports-v2")["pending"] == 0
    worker.tick()
    assert redis_client.xlen(settings.order_stream) == 2


def test_hint_failure_does_not_change_committed_api_semantics(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "deployment_mode", "database_per_tenant")
    monkeypatch.setattr(settings, "live_outbox_hints_enabled", True)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    hint_committed_order(uuid4(), uuid4())  # Best-effort hint must never raise after PG commit.


def test_publication_before_processing_crash_recovers_from_durable_work_receipt(
    factory, tenant_id, redis_client, monkeypatch
):
    from astraroll.v2.consumer import MultiTenantAggregationConsumer
    from astraroll.v2.live_ingestion import advance_import

    settings = get_settings()
    monkeypatch.setattr(settings, "order_stream", "test:pub-crash:" + uuid4().hex)
    monkeypatch.setattr(settings, "live_outbox_hints_enabled", True)
    with factory.begin() as s:
        order = create_order(
            s, tenant_id, OrderCreate(product_id=uuid4(), amount="12.34", currency="USD")
        )
        order_id = order.id

    class Manager:
        def session(self, tid):
            assert str(tid) == str(tenant_id)
            return factory()

    manager = Manager()
    original = MultiTenantAggregationConsumer.process_message

    def crash(*_):
        raise RuntimeError("simulated crash after outbox publication")

    monkeypatch.setattr(MultiTenantAggregationConsumer, "process_message", crash)
    error = advance_import(tenant_id, [order_id], manager, redis_client=redis_client)
    assert error["stage"] == "CONSUMED"
    with factory() as s:
        assert (
            eligible_orders(s, tenant_id) == []
        )  # Already published, so pending scan alone cannot recover.
    monkeypatch.setattr(MultiTenantAggregationConsumer, "process_message", original)
    worker = LiveOutboxWorker(manager, redis_client, [tenant_id])
    worker.tick()
    with factory() as s:
        assert s.get(OrderProjection, (tenant_id, order_id)).last_applied_version == 1
    assert redis_client.llen(settings.order_stream + ":live-work:processing") == 0
    assert redis_client.xpending(settings.order_stream, "astraroll-live-imports-v2")["pending"] == 0
