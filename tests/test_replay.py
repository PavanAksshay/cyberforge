"""Integration tests for Outbox Disaster Replay with live Redis and PostgreSQL."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import HourlySummaryAggregate
from astraroll.aggregation.reader import PostgresAnalyticsReader
from astraroll.aggregation.replay import replay_outbox_events
from astraroll.models import Order, OutboxEvent, Tenant

pytestmark = pytest.mark.integration


def test_outbox_disaster_replay_and_idempotence(factory, redis_client):
    tenant_id = uuid4()
    order_1_id = uuid4()
    product_id = uuid4()
    stream_name = f"test:replay:stream:{uuid4().hex}"
    group_name = f"test:replay:group:{uuid4().hex}"

    now_utc = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)

    # 1. Insert order and 2 outbox events in PostgreSQL (v1: placed $50, v2: shipped $50)
    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Replay Test Tenant"))
        session.flush()

        session.add(
            Order(
                id=order_1_id,
                tenant_id=tenant_id,
                product_id=product_id,
                amount=Decimal("50.00"),
                currency="USD",
                status="shipped",
                version=2,
                created_at=now_utc,
                updated_at=now_utc,
            )
        )
        session.flush()

        # Event 1: order 1 placed
        session.add(
            OutboxEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                order_id=order_1_id,
                order_version=1,
                event_type="order.created",
                schema_version=2,
                occurred_at=now_utc,
                payload={
                    "schema_version": 2,
                    "event_id": str(uuid4()),
                    "event_type": "order.created",
                    "tenant_id": str(tenant_id),
                    "order_id": str(order_1_id),
                    "order_version": 1,
                    "occurred_at": now_utc.isoformat(),
                    "before": None,
                    "after": {
                        "id": str(order_1_id),
                        "tenant_id": str(tenant_id),
                        "product_id": str(product_id),
                        "amount": "50.00",
                        "currency": "USD",
                        "status": "placed",
                        "version": 1,
                        "created_at": now_utc.isoformat(),
                        "updated_at": now_utc.isoformat(),
                    },
                },
            )
        )

        # Event 2: order 1 shipped
        session.add(
            OutboxEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                order_id=order_1_id,
                order_version=2,
                event_type="order.updated",
                schema_version=2,
                occurred_at=now_utc,
                payload={
                    "schema_version": 2,
                    "event_id": str(uuid4()),
                    "event_type": "order.updated",
                    "tenant_id": str(tenant_id),
                    "order_id": str(order_1_id),
                    "order_version": 2,
                    "occurred_at": now_utc.isoformat(),
                    "before": {
                        "id": str(order_1_id),
                        "tenant_id": str(tenant_id),
                        "product_id": str(product_id),
                        "amount": "50.00",
                        "currency": "USD",
                        "status": "placed",
                        "version": 1,
                        "created_at": now_utc.isoformat(),
                        "updated_at": now_utc.isoformat(),
                    },
                    "after": {
                        "id": str(order_1_id),
                        "tenant_id": str(tenant_id),
                        "product_id": str(product_id),
                        "amount": "50.00",
                        "currency": "USD",
                        "status": "shipped",
                        "version": 2,
                        "created_at": now_utc.isoformat(),
                        "updated_at": now_utc.isoformat(),
                    },
                },
            )
        )

    # 2. Replay outbox events into Redis stream
    replayed = replay_outbox_events(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        tenant_id=tenant_id,
    )
    assert replayed == 2

    # 3. Consume from Redis stream
    consumer = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
    )
    consumer.init_group()
    processed = consumer.process_batch()
    assert processed == 2

    # Verify aggregates via PostgresAnalyticsReader
    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        summary = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc),
        )
        assert summary.total_orders == 1
        assert summary.total_sales == Decimal("50.00")
        assert summary.average_order_value == Decimal("50.00")

    # 4. DISASTER RECOVERY TEST: Replay outbox a second time!
    replayed_second = replay_outbox_events(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        tenant_id=tenant_id,
    )
    assert replayed_second == 2

    # Process replayed batch
    processed_second = consumer.process_batch()
    assert processed_second == 2

    # Verify aggregates did NOT double count!
    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        summary_after_replay = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc),
        )
        # TOTALS MUST REMAIN IDENTICAL!
        assert summary_after_replay.total_orders == 1
        assert summary_after_replay.total_sales == Decimal("50.00")
        assert summary_after_replay.average_order_value == Decimal("50.00")
