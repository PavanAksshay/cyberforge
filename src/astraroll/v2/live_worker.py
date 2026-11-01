"""Live-only outbox orchestration, reusing the existing Redis consumer/reducer.

Hints accelerate committed producer writes; a bounded round-robin outbox scan
recovers lost hints/outages. Projection-covered historical events are not drained.
"""

import argparse
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from redis import Redis
from sqlalchemy import func, select

from astraroll.aggregation.models import OrderProjection
from astraroll.aggregation.release_models import BootstrapJob
from astraroll.config import get_settings
from astraroll.models import OutboxEvent
from astraroll.v2.live_ingestion import advance_import, import_progress

log = logging.getLogger(__name__)


def hints_key():
    return get_settings().order_stream + ":live-work"


def hint_committed_order(tenant_id, order_id):
    hint_committed_orders(tenant_id, [order_id])


def hint_committed_orders(tenant_id, order_ids):
    settings = get_settings()
    if settings.deployment_mode != "database_per_tenant" or not settings.live_outbox_hints_enabled:
        return
    try:
        hints = [
            json.dumps({"tenant_id": str(tenant_id), "order_id": str(oid)}) for oid in order_ids
        ]
        if hints:
            with Redis.from_url(
                settings.redis_url, socket_timeout=0.2, socket_connect_timeout=0.2
            ) as redis:
                redis.rpush(hints_key(), *hints)
    except Exception:
        log.warning(
            "Live hint unavailable; committed outbox will be scanned for tenant %s", tenant_id
        )


def eligible_orders(session, tenant_id, limit=20):
    p, e = OrderProjection, OutboxEvent
    baseline = session.scalar(
        select(func.max(BootstrapJob.activated_at)).where(
            BootstrapJob.tenant_id == tenant_id, BootstrapJob.status == "ready"
        )
    )
    query = (
        select(e.order_id)
        .outerjoin(p, (p.tenant_id == e.tenant_id) & (p.order_id == e.order_id))
        .where(
            e.tenant_id == tenant_id,
            e.published_at.is_(None),
            (p.order_id.is_(None)) | (p.last_applied_version < e.order_version),
        )
    )
    if baseline is not None:
        query = query.where(e.occurred_at >= baseline)
    return list(session.scalars(query.distinct().order_by(e.order_id).limit(limit)))


def read_worker_health():
    path = get_settings().live_worker_health_path
    if not path:
        return "unknown (no worker heartbeat configured)"
    try:
        data = json.loads(Path(path).read_text())
        age = (datetime.now(UTC) - datetime.fromisoformat(data["heartbeat_at"])).total_seconds()
        if age > 15:
            return f"stale worker heartbeat ({int(age)}s ago)"
        return (
            f"{data['status']} · heartbeat {max(0, int(age))}s ago · "
            f"{data['completed_cohorts']} cohorts"
        )
    except (OSError, ValueError, KeyError):
        return "unknown (worker heartbeat unavailable)"


class LiveOutboxWorker:
    def __init__(self, manager, redis, tenant_ids, health_path=None):
        self.manager = manager
        self.redis = redis
        self.tenants = list(tenant_ids)
        self.allowed = set(self.tenants)
        self.cursor = 0
        self.completed = 0
        self.health_path = Path(health_path) if health_path else None
        self.events = deque(maxlen=20)
        self.last_health_write = 0

    def tick(self):
        grouped = defaultdict(set)
        receipts = defaultdict(list)
        busy = hints_key() + ":processing"
        # LMOVE retains each work receipt until its PostgreSQL projection is complete.
        raw = list(self.redis.lrange(busy, 0, 19))
        for _ in range(20 - len(raw)):
            item = self.redis.lmove(hints_key(), busy, "LEFT", "RIGHT")
            if item is None:
                break
            raw.append(item)
        for item in raw:
            try:
                hint = json.loads(item)
                tid = UUID(hint["tenant_id"])
                ids = [UUID(oid) for oid in hint.get("order_ids", [hint.get("order_id")])]
                if tid not in self.allowed or not 1 <= len(ids) <= 20:
                    raise ValueError("Hint scope or batch invalid")
                grouped[tid].update(ids)
                receipts[tid].append(item)
            except (ValueError, KeyError, TypeError):
                self.redis.lrem(busy, 1, item)
                log.warning("Invalid work hint ignored; PostgreSQL remains authoritative")
        if self.tenants:
            tid = self.tenants[self.cursor % len(self.tenants)]
            self.cursor += 1
            with self.manager.session(tid) as session:
                ids = set(eligible_orders(session, tid)) - grouped[tid]
            if ids:
                item = json.dumps({"tenant_id": str(tid), "order_ids": [str(i) for i in ids]})
                # Persist recovery identity BEFORE publishing any source outbox marker.
                self.redis.rpush(busy, item)
                grouped[tid].update(ids)
                receipts[tid].append(item)
        for tid, ids in grouped.items():
            if not ids:
                continue
            error = advance_import(
                tid, list(ids), self.manager, redis_client=self.redis, register_recovery=False
            )
            state = import_progress(tid, list(ids), self.manager)
            self.events.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "tenant_id": str(tid),
                    "component": "LiveOutboxWorker",
                    "severity": "ERROR" if error else "INFO",
                    "stage": state["stage"],
                    "order_count": state["order_count"],
                    "error": error,
                }
            )
            if error:
                raise RuntimeError(f"{error['stage']}: {error['message']}")
            if state["stage"] == "AGGREGATED":
                for item in receipts[tid]:
                    self.redis.lrem(busy, 1, item)
                self.completed += 1
        self.health("running")

    def health(self, status, error=None, force=False):
        if not self.health_path or (not force and time.monotonic() - self.last_health_write < 5):
            return
        data = {
            "pid": os.getpid(),
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "status": status,
            "completed_cohorts": self.completed,
            "error": error,
            "recent_events": list(self.events),
        }
        tmp = self.health_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.health_path)
        self.last_health_write = time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-seconds", type=float, default=0)
    args = parser.parse_args()
    from astraroll.v2.restoration import release_readiness
    from astraroll.v2.runtime import get_control_registry, get_tenant_engine_manager

    settings = get_settings()
    if settings.deployment_mode != "database_per_tenant":
        raise ValueError("V2 worker requires V2 profile")
    ready, reason = release_readiness(
        settings.control_database_url, Path(".runtime/tenant-analytics-repair.json")
    )
    if not ready:
        raise ValueError(reason)
    ids = [r.tenant_id for r in get_control_registry().list_active_tenants()]
    manager = get_tenant_engine_manager()
    redis = Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
    worker = LiveOutboxWorker(manager, redis, ids, settings.live_worker_health_path)
    deadline = time.monotonic() + args.run_seconds if args.run_seconds else float("inf")
    try:
        while time.monotonic() < deadline:
            try:
                worker.tick()
            except Exception as exc:
                worker.health("degraded", f"{type(exc).__name__}: {str(exc)[:180]}", force=True)
                log.warning("Live worker retry: %s", type(exc).__name__)
            time.sleep(0.1)
    finally:
        worker.health("stopped", force=True)
        redis.close()
        manager.close_all()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
