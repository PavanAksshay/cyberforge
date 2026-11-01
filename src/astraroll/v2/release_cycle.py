"""Bounded historical review and live E2E using actual V2 worker subprocesses."""

import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text

from astraroll.aggregation.bootstrap import validate_rollups
from astraroll.aggregation.models import TenantProcessingState
from astraroll.models import Order, OutboxEvent
from astraroll.v2.consumer import MultiTenantAggregationConsumer
from astraroll.v2.scale_live import FailFirstAck


def run_cycle(manager, redis, client, plan, environment, output):
    output = Path(output)
    stream = f"bench:v2:release:{uuid4().hex}"
    group = f"release:{uuid4().hex}"
    env = {**environment, "ORDER_STREAM": stream}
    first, second = [UUID(t["tenant_id"]) for t in plan["tenants"][:2]]

    def worker(kind, *args):
        begin = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "astraroll.v2.workers", kind, "--group", group, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with (output / "workers.log").open("a") as log:
            log.write(f"{kind}: exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}\n")
        if proc.returncode:
            raise RuntimeError("V2 worker failed; inspect workers.log")
        return time.perf_counter() - begin

    def state(tenant):
        with manager.session(tenant) as session:
            validate_rollups(session, tenant)
            return {
                "orders": session.scalar(text("SELECT count(*) FROM orders")),
                "revision": session.get(TenantProcessingState, tenant).revision,
                "last_stream_id": session.get(
                    TenantProcessingState, tenant
                ).last_successful_stream_id,
                "pending": session.scalar(
                    text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
                ),
            }

    before = state(second)
    expected = min(100, before["pending"])
    if expected != 100:
        raise ValueError("Backlog review requires 100 existing pending historical events")
    worker("relay", "--tenant-id", str(second), "--batch-size", "100", "--once")
    if redis.xlen(stream) != 100:
        raise ValueError("Bounded sample published an unexpected event count")
    for _, fields in redis.xrange(stream):
        event = json.loads(fields[b"event"])
        if event["tenant_id"] != str(second):
            raise ValueError("Tenant-scoped relay published another tenant")
    worker("consumer", "--batch-size", "100", "--once")
    after = state(second)
    if after["orders"] != before["orders"] or after["revision"] != before["revision"]:
        raise ValueError("Historical backlog changed already bootstrapped financial state")
    if after["pending"] != before["pending"] - 100 or redis.xpending(stream, group)["pending"]:
        raise ValueError("Bounded history was not genuinely published/ACKed")

    def query():
        response = client.get(
            "/analytics/summary",
            params={
                "tenant_id": str(first),
                "currency": "USD",
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-08T00:00:00Z",
            },
        )
        response.raise_for_status()
        return response.json()

    def mutate():
        with manager.session(first) as session:
            order = session.scalar(
                select(Order)
                .where(Order.status == "placed", Order.currency == "USD")
                .order_by(Order.id)
                .limit(1)
            )
            if order is None:
                raise ValueError("No legal placed order available")
            oid, version, amount = order.id, order.version, order.amount
        response = client.patch(
            f"/tenants/{first}/orders/{oid}",
            json={"expected_version": version, "status": "shipped"},
        )
        response.raise_for_status()
        with manager.session(first) as session:
            row = session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.order_id == oid, OutboxEvent.order_version == version + 1
                )
            )
            payload = row.payload
        return oid, amount, payload

    from decimal import Decimal

    original, original_state = query(), state(first)
    if original_state["pending"]:
        raise ValueError("Live test requires the already-drained pilot tenant")
    begin = time.perf_counter()
    oid, amount, payload = mutate()
    worker("relay", "--tenant-id", str(first), "--batch-size", "1", "--once")
    worker("consumer", "--batch-size", "100", "--once")
    ordinary = query()
    ordinary_ms = (time.perf_counter() - begin) * 1000
    if (
        Decimal(ordinary["sales_amount"]) != Decimal(original["sales_amount"]) + amount
        or ordinary["total_order_count"] != original["total_order_count"]
    ):
        raise ValueError("Actual-worker ordinary update did not match the exact expected delta")
    revision = state(first)["revision"]
    redis.xadd(stream, {"event": json.dumps(payload)})
    worker("consumer", "--once")
    if state(first)["revision"] != revision or query()["sales_amount"] != ordinary["sales_amount"]:
        raise ValueError("Duplicate event double-applied")
    begin = time.perf_counter()
    recovered_id, recovered_amount, _ = mutate()
    worker("relay", "--tenant-id", str(first), "--batch-size", "1", "--once")
    message = redis.xreadgroup(group, "release-crash-probe", {stream: ">"}, count=1)[0][1][0]
    consumer = MultiTenantAggregationConsumer(
        manager, FailFirstAck(redis), stream_name=stream, group_name=group, block_ms=1
    )
    try:
        stream_id, fields = message
        stream_id = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
        consumer.process_message(stream_id, fields)
    except RuntimeError as exc:
        if str(exc) != "injected commit-before-ACK failure":
            raise
    else:
        raise ValueError("Commit-before-ACK injection failed")
    if redis.xpending(stream, group)["pending"] != 1:
        raise ValueError("ACK failure did not preserve a pending event")
    worker("consumer", "--idle-claim-ms", "0", "--once")
    recovered = query()
    recovery_ms = (time.perf_counter() - begin) * 1000
    final = state(first)
    if final["last_stream_id"] != stream_id:
        raise ValueError("Recovered checkpoint does not equal the real Redis string ID")
    if (
        final["revision"] != revision + 1
        or final["orders"] != 10000
        or Decimal(recovered["sales_amount"])
        != Decimal(ordinary["sales_amount"]) + recovered_amount
        or redis.xpending(stream, group)["pending"]
    ):
        raise ValueError("Subprocess reclaim/recovery failed exactness")
    elapsed = worker("consumer", "--run-seconds", "2")
    return {
        "stream": stream,
        "group": group,
        "bounded_history_events": 100,
        "backlog_tenant": str(second),
        "backlog_before": before,
        "backlog_after": after,
        "ordinary_order": str(oid),
        "recovery_order": str(recovered_id),
        "ordinary_visibility_including_worker_startup_ms": ordinary_ms,
        "recovery_visibility_including_worker_startup_ms": recovery_ms,
        "duplicate_deduplicated": True,
        "pending_after_recovery": 0,
        "timed_worker_clean_exit_seconds": elapsed,
        "live_orders_created": 0,
        "scope": "One tenant/100 historical events; two existing-order updates; private stream",
    }
