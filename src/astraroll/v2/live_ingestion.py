"""Bounded live import processing. Each stage is read from durable state, never a timer."""

import json
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from redis import Redis
from sqlalchemy import select

from astraroll.aggregation.models import OrderProjection, ProcessedEvent
from astraroll.config import get_settings
from astraroll.models import Order, OutboxEvent
from astraroll.v2.consumer import MultiTenantAggregationConsumer

GROUP = "astraroll-live-imports-v2"


def advance_import(tenant_id: UUID, order_ids: list[UUID], manager, redis_client=None):
    """Publish only this bounded cohort, bypassing covered historical backlog.

    FORCE claims real stream entries into the group's PEL before processing/ACK.
    Later normal stream reads are harmless durable event-id/version duplicates.
    No source events outside this explicitly authorized cohort are touched.
    """
    settings = get_settings()
    client = redis_client or Redis.from_url(
        settings.redis_url, socket_timeout=2, socket_connect_timeout=2
    )
    stage = "PUBLISHED"
    deadline = monotonic() + 5
    try:
        consumer = MultiTenantAggregationConsumer(
            manager,
            client,
            settings.order_stream,
            group_name=GROUP,
            consumer_name="live-import",
            block_ms=1,
        )
        consumer.init_group()
        with manager.session(tenant_id) as session, session.begin():
            events = session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == tenant_id,
                    OutboxEvent.order_id.in_(order_ids),
                    OutboxEvent.published_at.is_(None),
                )
                .order_by(OutboxEvent.order_id, OutboxEvent.order_version)
                .limit(200)
                .with_for_update(skip_locked=True)
            ).all()
            for event in events:
                sid = client.xadd(settings.order_stream, {"event": json.dumps(event.payload)})
                event.stream_id = sid.decode() if isinstance(sid, bytes) else sid
                event.published_at = datetime.now(UTC)
        stage = "CONSUMED"
        with manager.session(tenant_id) as session:
            events = session.execute(
                select(OutboxEvent.id, OutboxEvent.stream_id)
                .where(
                    OutboxEvent.tenant_id == tenant_id,
                    OutboxEvent.order_id.in_(order_ids),
                    OutboxEvent.published_at.is_not(None),
                    ~select(ProcessedEvent.event_id)
                    .where(ProcessedEvent.event_id == OutboxEvent.id)
                    .exists(),
                )
                .order_by(OutboxEvent.order_id, OutboxEvent.order_version)
                .limit(200)
            ).all()
        for event_id, sid in events:
            if monotonic() > deadline:
                break
            messages = client.xclaim(
                settings.order_stream, GROUP, consumer.consumer_name, 0, [sid], force=True
            )
            if not messages:
                raise RuntimeError(f"Published event {event_id} missing from Redis")
            for stream_id, fields in messages:
                payload = json.loads(fields.get(b"event") or fields.get("event"))
                if payload.get("event_id") != str(event_id):
                    raise RuntimeError("Redis event identity mismatch")
                consumer.process_message(
                    stream_id.decode() if isinstance(stream_id, bytes) else stream_id, fields
                )
        return None
    except Exception as exc:
        # Commit remains durable on broker/processor failure; return observed failure stage.
        return {"stage": stage, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}
    finally:
        if redis_client is None:
            client.close()


def import_progress(tenant_id: UUID, order_ids: list[UUID], manager):
    with manager.session(tenant_id) as session:
        orders = session.scalars(
            select(Order).where(Order.tenant_id == tenant_id, Order.id.in_(order_ids))
        ).all()
        events = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == tenant_id, OutboxEvent.order_id.in_(order_ids)
            )
        ).all()
        processed = set(
            session.scalars(
                select(ProcessedEvent.event_id).where(
                    ProcessedEvent.tenant_id == tenant_id, ProcessedEvent.order_id.in_(order_ids)
                )
            )
        )
        projections = {
            p.order_id: p.last_applied_version
            for p in session.scalars(
                select(OrderProjection).where(
                    OrderProjection.tenant_id == tenant_id, OrderProjection.order_id.in_(order_ids)
                )
            )
        }
        committed = len(orders) == len(set(order_ids)) and bool(orders)
        published = committed and bool(events) and all(e.published_at for e in events)
        consumed = published and all(e.id in processed for e in events)
        aggregated = consumed and all(projections.get(o.id, 0) >= o.version for o in orders)
        stages = {
            "VALIDATED": committed,
            "COMMITTED": committed,
            "PUBLISHED": published,
            "CONSUMED": consumed,
            "AGGREGATED": aggregated,
        }
        return {
            "stage": next((k for k in reversed(stages) if stages[k]), "PENDING"),
            "stages": stages,
            "order_count": len(orders),
            "event_count": len(events),
            "published_count": sum(bool(e.published_at) for e in events),
            "processed_count": len(processed),
            "last_source_event_at": max((e.occurred_at for e in events), default=None),
            "orders": [
                {
                    "order_id": o.id,
                    "currency": o.currency,
                    "amount": str(o.amount),
                    "status": o.status,
                    "created_at": o.created_at,
                }
                for o in orders
            ],
        }
