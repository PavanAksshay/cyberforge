"""Explicitly invoked target-only live/recovery probe; never provisions databases.

Run only after safe migration and read-only parity verification. Historical outbox
is drained in the selected target first, so bootstrap deduplication is also tested.
"""

import time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from astraroll.aggregation.bootstrap import validate_rollups
from astraroll.aggregation.models import TenantProcessingState
from astraroll.models import Order
from astraroll.v2.consumer import MultiTenantAggregationConsumer
from astraroll.v2.relay import MultiTenantRelay


class FailFirstAck:
    """Inject a crash boundary after the database commit, before the Redis ACK."""

    def __init__(self, redis):
        self.redis = redis

    def __getattr__(self, name):
        return getattr(self.redis, name)

    def xack(self, *args, **kwargs):
        raise RuntimeError("injected commit-before-ACK failure")


def live_probe(manager, redis, client, tenant: UUID, params, timeout_seconds=300):
    """Mutate two existing placed target orders; keep cardinality unchanged.

    client must already carry an actual expiring bearer credential. manager must
    resolve a reviewed dedicated target database, never the retained V1 schema.
    Artifacts preserve the UUID/stream for inspection; nothing is deleted.
    """
    stream, group = f"bench:v2:live:{uuid4().hex}", f"bench:v2:group:{uuid4().hex}"
    relay = MultiTenantRelay(None, manager, redis, stream_name=stream, batch_size=500)
    consumer = MultiTenantAggregationConsumer(
        manager,
        redis,
        stream_name=stream,
        group_name=group,
        batch_size=500,
        block_ms=1,
        min_idle_claim_ms=0,
    )
    consumer.init_group()
    deadline = time.monotonic() + timeout_seconds
    historical = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("Historical relay/deduplication did not finish")
        n = relay.relay_tenant(tenant)
        historical += n
        if n:
            print(f"Live-probe historical events relayed: {historical}", flush=True)
        while consumer.process_batch():
            if time.monotonic() > deadline:
                raise TimeoutError("Historical consumption did not finish")
        if not n:
            break
    with manager.session(tenant) as session:
        validate_rollups(session, tenant)
        order = session.scalar(
            select(Order)
            .where(
                Order.tenant_id == tenant,
                Order.status == "placed",
                Order.currency == params["currency"],
            )
            .order_by(Order.id)
            .limit(1)
        )
        if order is None:
            raise ValueError("Live probe requires an existing placed order")
        order_id, version, amount = order.id, order.version, order.amount
        revision = session.get(TenantProcessingState, tenant).revision
    before = client.get("/analytics/summary", params=params)
    before.raise_for_status()
    begin = time.perf_counter()
    response = client.patch(
        f"/tenants/{tenant}/orders/{order_id}",
        json={
            "expected_version": version,
            "status": "shipped",
        },
    )
    response.raise_for_status()
    if relay.relay_tenant(tenant) != 1:
        raise ValueError("Live event was not published exactly once in this isolated probe")
    messages = redis.xreadgroup(group, consumer.consumer_name, {stream: ">"}, count=1)
    if not messages:
        raise ValueError("Live event missing from stream")
    sid, fields = messages[0][1][0]
    sid = sid.decode() if isinstance(sid, bytes) else sid
    consumer.redis = FailFirstAck(redis)
    try:
        consumer.process_message(sid, fields)
    except RuntimeError as exc:
        if str(exc) != "injected commit-before-ACK failure":
            raise
    else:
        raise ValueError("Recovery injection did not reach commit-before-ACK boundary")
    with manager.session(tenant) as session:
        if session.get(TenantProcessingState, tenant).revision != revision + 1:
            raise ValueError("Live event was not committed before ACK failure")
        if session.get(TenantProcessingState, tenant).last_successful_stream_id != sid:
            raise ValueError("Stored processing checkpoint is not the real Redis string ID")
    if redis.xpending(stream, group)["pending"] != 1:
        raise ValueError("Failed ACK did not retain the pending message")
    consumer.redis = redis
    if consumer.run_claim_cycle(force=True) != 1:
        raise ValueError("Recovery did not reclaim and ACK the pending event")
    after = client.get("/analytics/summary", params=params)
    after.raise_for_status()
    visibility_ms = (time.perf_counter() - begin) * 1000
    if (
        Decimal(after.json()["sales_amount"]) != Decimal(before.json()["sales_amount"]) + amount
        or after.json()["total_order_count"] != before.json()["total_order_count"]
    ):
        raise ValueError("Authenticated API did not expose the exact live delta")
    with manager.session(tenant) as session:
        validate_rollups(session, tenant)
        if session.get(TenantProcessingState, tenant).revision != revision + 1:
            raise ValueError("Redelivery double-applied the live event")
    if redis.xpending(stream, group)["pending"]:
        raise ValueError("Recovery left pending work")
    # Measure an ordinary cycle separately from the injected recovery sequence.
    with manager.session(tenant) as session:
        ordinary = session.scalar(
            select(Order)
            .where(
                Order.tenant_id == tenant,
                Order.status == "placed",
                Order.currency == params["currency"],
            )
            .order_by(Order.id)
            .limit(1)
        )
        if ordinary is None:
            raise ValueError("Ordinary visibility probe needs another placed order")
        next_id, next_version, next_amount = ordinary.id, ordinary.version, ordinary.amount
    ordinary_start = time.perf_counter()
    changed = client.patch(
        f"/tenants/{tenant}/orders/{next_id}",
        json={
            "expected_version": next_version,
            "status": "shipped",
        },
    )
    changed.raise_for_status()
    if relay.relay_tenant(tenant) != 1 or consumer.process_batch() != 1:
        raise ValueError("Ordinary event cycle did not process exactly one event")
    visible = client.get("/analytics/summary", params=params)
    visible.raise_for_status()
    ordinary_ms = (time.perf_counter() - ordinary_start) * 1000
    if (
        Decimal(visible.json()["sales_amount"])
        != Decimal(after.json()["sales_amount"]) + next_amount
        or visible.json()["total_order_count"] != after.json()["total_order_count"]
    ):
        raise ValueError("Ordinary visibility delta mismatch")
    with manager.session(tenant) as session:
        validate_rollups(session, tenant)
        if session.get(TenantProcessingState, tenant).revision != revision + 2:
            raise ValueError("Two live changes did not produce exactly two revisions")
    return {
        "tenant_id": str(tenant),
        "order_id": str(order_id),
        "stream": stream,
        "historical_events_drained": historical,
        "live_events": 2,
        "ordinary_order_id": str(next_id),
        "ordinary_visibility_ms": ordinary_ms,
        "visibility_including_recovery_ms": visibility_ms,
        "recovery": "commit-before-ACK, XAUTOCLAIM, deduplicated, ACKed",
        "scope": "two existing target orders; cardinality unchanged; ordinary and recovery cycles",
    }
