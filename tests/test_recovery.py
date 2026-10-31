"""Integration tests for Redis 7 Recovery, Failure Modes, and Concurrency (Phase 2).

Tests:
1. Commit-before-ACK crash: worker commits PostgreSQL transaction but crashes before XACK;
   recovered worker deduplicates via processed_events and ACKs without double mutations.
2. Abandoned pending messages: message read into PEL by worker-1 and abandoned;
   worker-2 reclaims via XAUTOCLAIM, processes, commits, and ACKs.
3. XAUTOCLAIM cursor progression: batched claiming advances claim_cursor through PEL
   and completes the loop back to '0-0'.
4. Duplicate and out-of-order events: stream delivers duplicates and stale versions;
   aggregates remain strictly idempotent.
5. Concurrent workers: multiple consumers process stream messages in parallel without
   deadlocks, lost updates, or duplicate counting.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import (
    HourlySummaryAggregate,
    OrderProjection,
)
from astraroll.models import Tenant

pytestmark = pytest.mark.integration


def _create_order_event_payload(
    tenant_id: str,
    order_id: str,
    event_id: str,
    version: int,
    amount: str = "50.00",
    status: str = "placed",
    product_id: str | None = None,
    created_at: str = "2026-10-31T10:00:00Z",
) -> dict:
    pid = product_id or str(uuid4())
    before = None
    if version > 1:
        before = {
            "id": order_id,
            "tenant_id": tenant_id,
            "product_id": pid,
            "amount": amount,
            "currency": "USD",
            "status": "placed",
            "version": version - 1,
            "created_at": created_at,
            "updated_at": created_at,
        }
    return {
        "schema_version": 2,
        "event_id": event_id,
        "event_type": "order.created" if version == 1 else "order.updated",
        "tenant_id": tenant_id,
        "order_id": order_id,
        "order_version": version,
        "occurred_at": created_at,
        "before": before,
        "after": {
            "id": order_id,
            "tenant_id": tenant_id,
            "product_id": pid,
            "amount": amount,
            "currency": "USD",
            "status": status,
            "version": version,
            "created_at": created_at,
            "updated_at": created_at,
        },
    }


def test_recovery_commit_before_ack_crash(factory, redis_client):
    """Worker commits PostgreSQL transaction but crashes before XACK.

    On recovery, the message remains in the PEL and is redelivered.
    The processor recognizes it as already processed, skips aggregate mutation,
    and acknowledges the message.
    """
    tenant_id = uuid4()
    order_id = uuid4()
    event_id = uuid4()
    stream_name = f"test:rec:crash:{uuid4().hex}"
    group_name = f"test:rec:group:{uuid4().hex}"

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Crash Recovery Tenant"))

    payload = _create_order_event_payload(
        tenant_id=str(tenant_id),
        order_id=str(order_id),
        event_id=str(event_id),
        version=1,
        amount="75.00",
        status="shipped",
    )
    redis_client.xadd(stream_name, {"event": json.dumps(payload)})

    consumer = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="worker-crash",
    )
    consumer.init_group()

    # Step 1: Simulate crash immediately after PostgreSQL commit (before xack)
    crash_err = RuntimeError("Simulated network crash before XACK")
    with patch.object(redis_client, "xack", side_effect=crash_err):
        with pytest.raises(RuntimeError, match="Simulated network crash before XACK"):
            consumer.process_batch()

    # Verify message is STILL unacknowledged in PEL
    pel_info = redis_client.xpending(stream_name, group_name)
    assert pel_info["pending"] == 1

    # Verify DB transaction did commit
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
        assert summary.sales_amount == Decimal("75.00")

    # Step 2: Recovered worker claims / reads the pending message
    recovered_consumer = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="worker-recovered",
        min_idle_claim_ms=0,
    )
    # Run claim cycle to reclaim the abandoned message from worker-crash
    recovered_count = recovered_consumer.run_claim_cycle(force=True)
    assert recovered_count == 1

    # Verify message is now acknowledged and PEL is empty
    pel_after = redis_client.xpending(stream_name, group_name)
    assert pel_after["pending"] == 0

    # Verify DB aggregates DID NOT double count!
    with factory() as session:
        summary_after = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
            )
        )
        assert summary_after.order_count == 1
        assert summary_after.sales_amount == Decimal("75.00")


def test_recovery_abandoned_pending_messages(factory, redis_client):
    """Worker 1 reads a message into its PEL and dies before processing.

    Worker 2 uses XAUTOCLAIM with min_idle_claim_ms to reclaim and process the message.
    """
    tenant_id = uuid4()
    order_id = uuid4()
    event_id = uuid4()
    stream_name = f"test:rec:abandon:{uuid4().hex}"
    group_name = f"test:rec:group:{uuid4().hex}"

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Abandon Tenant"))

    payload = _create_order_event_payload(
        tenant_id=str(tenant_id),
        order_id=str(order_id),
        event_id=str(event_id),
        version=1,
        amount="120.00",
        status="shipped",
    )
    redis_client.xadd(stream_name, {"event": json.dumps(payload)})

    # Initialize consumer group
    redis_client.xgroup_create(stream_name, group_name, id="0-0", mkstream=True)

    # Worker 1 reads the message using XREADGROUP, placing it in worker-1's PEL
    raw_reads = redis_client.xreadgroup(
        groupname=group_name,
        consumername="worker-1",
        streams={stream_name: ">"},
        count=1,
    )
    assert len(raw_reads[0][1]) == 1
    # Worker 1 crashes immediately without committing or calling XACK

    # Verify PEL shows 1 pending message owned by worker-1
    pel = redis_client.xpending(stream_name, group_name)
    assert pel["pending"] == 1

    # Worker 2 starts up
    worker_2 = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="worker-2",
        min_idle_claim_ms=0,
    )

    # Worker 2 claims abandoned message
    claimed = worker_2.run_claim_cycle(force=True)
    assert claimed == 1

    # Message is now ACKed; PEL is empty
    assert redis_client.xpending(stream_name, group_name)["pending"] == 0

    # PostgreSQL has aggregate
    with factory() as session:
        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
            )
        )
        assert summary.order_count == 1
        assert summary.sales_amount == Decimal("120.00")


def test_recovery_xautoclaim_cursor_progression(factory, redis_client):
    """XAUTOCLAIM cursor advances progressively through batches until returning to '0-0'."""
    tenant_id = uuid4()
    stream_name = f"test:rec:cursor:{uuid4().hex}"
    group_name = f"test:rec:group:{uuid4().hex}"

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Cursor Tenant"))

    # Publish 5 messages
    for i in range(1, 6):
        payload = _create_order_event_payload(
            tenant_id=str(tenant_id),
            order_id=str(uuid4()),
            event_id=str(uuid4()),
            version=1,
            amount="10.00",
            status="shipped",
        )
        redis_client.xadd(stream_name, {"event": json.dumps(payload)})

    # Initialize group and read all 5 with worker-abandoned to leave them in PEL
    redis_client.xgroup_create(stream_name, group_name, id="0-0", mkstream=True)
    redis_client.xreadgroup(group_name, "worker-abandoned", {stream_name: ">"}, count=5)
    assert redis_client.xpending(stream_name, group_name)["pending"] == 5

    # Worker 2 with batch_size=2
    worker_2 = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="worker-progressive",
        batch_size=2,
        min_idle_claim_ms=0,
    )
    assert worker_2.claim_cursor == "0-0"

    # Claim cycle 1: claims 2 messages, cursor advances
    claimed_1 = worker_2.run_claim_cycle(force=True)
    assert claimed_1 == 2
    # Cursor should have progressed past "0-0"
    assert worker_2.claim_cursor != "0-0"

    # Claim cycle 2: claims next 2 messages, cursor advances
    claimed_2 = worker_2.run_claim_cycle(force=True)
    assert claimed_2 == 2
    assert worker_2.claim_cursor != "0-0"

    # Claim cycle 3: claims last 1 message, cursor wraps back to "0-0"
    claimed_3 = worker_2.run_claim_cycle(force=True)
    assert claimed_3 == 1
    assert worker_2.claim_cursor == "0-0"

    # All 5 messages processed and ACKed
    assert redis_client.xpending(stream_name, group_name)["pending"] == 0

    with factory() as session:
        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
            )
        )
        assert summary.order_count == 5
        assert summary.sales_amount == Decimal("50.00")


def test_recovery_duplicate_and_out_of_order_events(factory, redis_client):
    """Duplicate event_id or out-of-order versions are handled idempotently."""
    tenant_id = uuid4()
    order_id = uuid4()
    event_1_id = uuid4()
    event_2_id = uuid4()
    stream_name = f"test:rec:dup:{uuid4().hex}"
    group_name = f"test:rec:group:{uuid4().hex}"

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Dup Tenant"))

    consumer = AggregationConsumer(
        session_factory=factory,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
    )
    consumer.init_group()

    # Step 1: Send v1 (placed $100)
    p1 = _create_order_event_payload(
        str(tenant_id), str(order_id), str(event_1_id), version=1, amount="100.00", status="placed"
    )
    redis_client.xadd(stream_name, {"event": json.dumps(p1)})
    consumer.process_batch()

    # Step 2: Send exact same event_1 again (duplicate event delivery)
    redis_client.xadd(stream_name, {"event": json.dumps(p1)})
    consumer.process_batch()

    # Step 3: Send v2 (shipped $100)
    p2 = _create_order_event_payload(
        str(tenant_id), str(order_id), str(event_2_id), version=2, amount="100.00", status="shipped"
    )
    redis_client.xadd(stream_name, {"event": json.dumps(p2)})
    consumer.process_batch()

    # Step 4: Send stale v1 with a new event_id (out-of-order event delivery)
    p_stale = _create_order_event_payload(
        str(tenant_id), str(order_id), str(uuid4()), version=1, amount="100.00", status="placed"
    )
    redis_client.xadd(stream_name, {"event": json.dumps(p_stale)})
    consumer.process_batch()

    # Total must be strictly 1 order of $100, exactly once
    with factory() as session:
        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
            )
        )
        assert summary.order_count == 1
        assert summary.sales_amount == Decimal("100.00")


def test_recovery_concurrent_workers(factory, redis_client):
    """Multiple concurrent consumer workers process stream without deadlocks or corruption."""
    tenant_id = uuid4()
    stream_name = f"test:rec:concurrent:{uuid4().hex}"
    group_name = f"test:rec:group:{uuid4().hex}"
    num_orders = 20

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Concurrent Tenant"))

    # Publish 20 orders
    for i in range(num_orders):
        payload = _create_order_event_payload(
            tenant_id=str(tenant_id),
            order_id=str(uuid4()),
            event_id=str(uuid4()),
            version=1,
            amount="25.00",
            status="shipped",
        )
        redis_client.xadd(stream_name, {"event": json.dumps(payload)})

    # Initialize consumer group
    redis_client.xgroup_create(stream_name, group_name, id="0-0", mkstream=True)

    def worker_loop(name: str):
        worker = AggregationConsumer(
            session_factory=factory,
            redis_client=redis_client,
            stream_name=stream_name,
            group_name=group_name,
            consumer_name=name,
            batch_size=5,
            block_ms=200,
        )
        total = 0
        while True:
            n = worker.process_batch()
            if n == 0:
                break
            total += n
        return total

    # Run two workers in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_loop, "worker-A")
        f2 = executor.submit(worker_loop, "worker-B")
        processed_a = f1.result()
        processed_b = f2.result()

    assert processed_a + processed_b == num_orders

    # Verify no pending messages
    assert redis_client.xpending(stream_name, group_name)["pending"] == 0

    # Verify total aggregate in PostgreSQL
    with factory() as session:
        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
            )
        )
        assert summary.order_count == num_orders
        assert summary.sales_amount == Decimal(f"{num_orders * 25}.00")
