"""Bounded server-side synthetic workload with complete, atomic schema-2 history."""

from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import text

from astraroll.locking import tenant_lock
from astraroll.models import Tenant

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 8, tzinfo=UTC)


def seed_tenant(factory, namespace: UUID, tenant_index: int, count: int, batch_size=5000):
    if count < 1 or not 1 <= batch_size <= 100000:
        raise ValueError("Positive count and batch size 1..100000 required")
    tenant_id = uuid5(namespace, f"tenant:{tenant_index}")
    with factory.begin() as session:
        tenant_lock(session, tenant_id)
        if session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, name=f"Synthetic {namespace} / {tenant_index}"))
    for offset in range(0, count, batch_size):
        seed_batch(factory, tenant_id, offset, min(count, offset + batch_size))
    return tenant_id


def seed_batch(factory, tenant_id, start, end):
    """No client-side order list. Each bounded transaction inserts source + history."""
    with factory.begin() as session:
        tenant_lock(session, tenant_id)
        return session.scalar(
            text("""
        WITH source AS (
            SELECT md5(CAST(:tenant AS text)||':order:'||i)::uuid AS id,
                CAST(:tenant AS uuid) AS tenant_id,
                md5(CAST(:tenant AS text)||':product:'||(i%40))::uuid AS product_id,
                ((i%100000+1)::numeric/100)::numeric(18,2) AS amount,
                CASE WHEN i%2=0 THEN 'USD' ELSE 'EUR' END AS currency,
                CASE WHEN i%5=0 THEN 'cancelled' WHEN i%3=0 THEN 'placed'
                    ELSE 'shipped' END AS status,
                CAST(:at AS timestamptz)+(i%604800)*interval '1 second' AS created_at
            FROM generate_series(:start, :end-1) i
        ), inserted AS (
            INSERT INTO orders(id,tenant_id,product_id,amount,currency,status,
                version,created_at,updated_at)
            SELECT id,tenant_id,product_id,amount,currency,status,
                CASE WHEN status='placed' THEN 1 ELSE 2 END,created_at,
                created_at+CASE WHEN status='placed' THEN interval '0 second'
                    ELSE interval '1 second' END FROM source
            ON CONFLICT(id) DO NOTHING RETURNING *
        ), snapshots AS (
            SELECT *, to_jsonb(o)||jsonb_build_object('amount',amount::text) AS final,
                to_jsonb(o)||jsonb_build_object('amount',amount::text,'status','placed',
                    'version',1,'updated_at',created_at) AS initial
            FROM inserted o
        ), events AS (
            SELECT *, 1 AS v, 'order.created' AS typ, created_at AS at,
                NULL::jsonb AS old, initial AS new FROM snapshots
            UNION ALL
            SELECT *, 2 AS v, CASE WHEN status='cancelled' THEN 'order.cancelled'
                ELSE 'order.updated' END, updated_at, initial, final
                FROM snapshots WHERE version=2
        ), history AS (
            INSERT INTO outbox_events(id,tenant_id,order_id,order_version,event_type,
                schema_version,payload,occurred_at)
            SELECT md5(id::text||':event:'||v)::uuid,tenant_id,id,v,typ,2,
                jsonb_build_object('schema_version',2,
                    'event_id',md5(id::text||':event:'||v)::uuid,'event_type',typ,
                    'tenant_id',tenant_id,'order_id',id,'order_version',v,'occurred_at',at,
                    'before',old,'after',new),at FROM events RETURNING id
        ) SELECT count(*) FROM history
        """),
            {"tenant": tenant_id, "start": start, "end": end, "at": START},
        )
