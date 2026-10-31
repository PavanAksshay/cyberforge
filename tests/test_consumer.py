"""Integration tests for AggregationConsumer with live Redis and PostgreSQL."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import (
    EventQuarantine,
    HourlySummaryAggregate,
    OrderProjection,
)
from astraroll.models import Tenant

pytestmark = pytest.mark.integration


def test_consumer_processes_stream_message_and_xacks(factory, redis_client):
    tenant_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    event_id = uuid4()
    stream_name = f"test:stream:{uuid4().hex}"
    group_name = f"test:group:{uuid4().hex}"

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Consumer Test Tenant"))

    # Publish V2 order.created event to Redis stream
    payload = {
        "schema_version": 2,
        "event_id": str(event_id),
        "event_type": "order.created",
        "tenant_id": str(tenant_id),
        "order_id": str(order_id),
        "order_version": 1,
        "occurred_at": "2026-10-31T14:00:00Z",
        "before": None,
        "after": {
            "id": str(order_id),
            "tenant_id": str(tenant_id),
            "product_id": str(product_id),
            "amount": "85.00",
            "currency": "USD",
            "status": "placed",
            "version": 1,
            "created_at": "2026-10-31T14:00:00Z",
            "updated_at": "2026-10-31T14:00:00Z",
        },
    }
    msg_id = redis_client.xadd(stream_name, {"event": json.dumps(payload)})

    consumer = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="test-worker-1",
    )
    consumer.init_group()

    # Process batch
    count = consumer.process_batch()
    assert count == 1

    # Verify message is acknowledged (PEL is empty)
    pending = redis_client.xpending(stream_name, group_name)
    assert pending["pending"] == 0

    # Verify PostgreSQL DB state
    with factory() as session:
        proj = session.get(OrderProjection, (tenant_id, order_id))
        assert proj is not None
        assert proj.last_applied_version == 1

        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
            )
        )
        assert summary is not None
        assert summary.order_count == 1


def test_consumer_quarantines_invalid_message_and_xacks(factory, redis_client):
    tenant_id = uuid4()
    event_id = uuid4()
    stream_name = f"test:stream:{uuid4().hex}"
    group_name = f"test:group:{uuid4().hex}"

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Quarantine Test Tenant"))

    # Publish V1 (unsupported schema) event
    v1_payload = {
        "schema_version": 1,
        "event_id": str(event_id),
        "tenant_id": str(tenant_id),
        "order_id": str(uuid4()),
        "order_version": 1,
    }
    redis_client.xadd(stream_name, {"event": json.dumps(v1_payload)})

    consumer = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
    )
    consumer.init_group()

    count = consumer.process_batch()
    assert count == 1

    # Message is XACKed so it doesn't block the stream
    pending = redis_client.xpending(stream_name, group_name)
    assert pending["pending"] == 0

    # Quarantine record exists in DB
    with factory() as session:
        q = session.scalar(
            select(EventQuarantine).where(EventQuarantine.event_id == event_id)
        )
        assert q is not None
        assert "Unsupported schema_version: 1" in q.reason
