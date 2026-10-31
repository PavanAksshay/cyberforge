"""Explicit admin diagnostics and fenced reconciliation; never an analytics fallback."""

from collections import defaultdict, deque
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

from fastapi import APIRouter, Depends
from redis import Redis
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from astraroll.aggregation.bootstrap import validate_rollups
from astraroll.aggregation.release_models import AggregateCoverage, OperationalCounter
from astraroll.analytics.auth import require_admin
from astraroll.analytics.contracts import METRIC_SEMANTICS
from astraroll.config import get_settings
from astraroll.db import get_session_factory
from astraroll.locking import tenant_lock

router = APIRouter(prefix="/ops", dependencies=[Depends(require_admin)], tags=["operations"])
latencies = defaultdict(lambda: deque(maxlen=2048))
latency_lock = Lock()


def record_latency(route, elapsed_ms):
    with latency_lock:
        latencies[route].append(elapsed_ms)


def latency_report():
    with latency_lock:
        return {
            path: {
                "samples": len(values),
                **{
                    f"p{p}_ms": sorted(values)[min(len(values) - 1, int(len(values) * p / 100))]
                    for p in (50, 95, 99)
                },
            }
            for path, values in latencies.items()
            if values
        }


def increment(session, name, value=1):
    stmt = insert(OperationalCounter).values(name=name, value=value)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={"value": OperationalCounter.value + value, "updated_at": datetime.now(UTC)},
        )
    )


def reconcile(factory, tenant_id):
    """Source and projections must share a boundary before comparing totals.

    Drift invalidates coverage; only explicit archived rebuild repairs it.
    """
    with factory.begin() as session:
        tenant_lock(session, tenant_id)
        behind = session.scalar(
            text("""SELECT count(*) FROM orders o
            LEFT JOIN order_projections p ON p.tenant_id=o.tenant_id AND p.order_id=o.id
            WHERE o.tenant_id=:tenant AND
                (p.order_id IS NULL OR p.last_applied_version<>o.version)"""),
            {"tenant": tenant_id},
        )
        if behind:
            return {"status": "lagging", "unmatched_orders": behind, "compared": False}
        try:
            validate_rollups(session, tenant_id)
        except ValueError as exc:
            session.execute(
                update(AggregateCoverage)
                .where(AggregateCoverage.tenant_id == tenant_id)
                .values(status="invalid")
            )
            increment(session, "reconciliation_drift")
            return {"status": "drift", "compared": True, "error": str(exc)}
        session.execute(
            update(AggregateCoverage)
            .where(
                AggregateCoverage.tenant_id == tenant_id,
                AggregateCoverage.status == "ready",
                AggregateCoverage.metric_semantics == METRIC_SEMANTICS,
            )
            .values(verified_at=datetime.now(UTC))
        )
        increment(session, "reconciliation_match")
        return {"status": "match", "compared": True}


@router.get("/metrics")
def metrics(tenant_id: UUID):
    """On-demand diagnostics, capped at 5 seconds of PostgreSQL execution per query."""
    with get_session_factory().begin() as session:
        session.execute(text("SET LOCAL statement_timeout='5s'"))
        backlog = (
            session.execute(
                text("""SELECT count(*) AS pending,
            extract(epoch FROM now()-min(occurred_at)) AS oldest_unpublished_seconds
            FROM outbox_events WHERE published_at IS NULL AND tenant_id=:tenant"""),
                {"tenant": tenant_id},
            )
            .mappings()
            .one()
        )
        lag = (
            session.execute(
                text("""SELECT count(*) AS unmatched_orders,
            extract(epoch FROM now()-min(o.updated_at)) AS oldest_unapplied_source_seconds
            FROM orders o LEFT JOIN order_projections p
            ON p.tenant_id=o.tenant_id AND p.order_id=o.id WHERE o.tenant_id=:tenant
            AND (p.order_id IS NULL OR p.last_applied_version<>o.version)"""),
                {"tenant": tenant_id},
            )
            .mappings()
            .one()
        )
        state = (
            session.execute(
                text("""SELECT revision,last_successful_apply_at,
            last_successful_stream_id FROM tenant_processing_state WHERE tenant_id=:tenant"""),
                {"tenant": tenant_id},
            )
            .mappings()
            .first()
        )
        coverage = session.scalars(
            select(AggregateCoverage).where(AggregateCoverage.tenant_id == tenant_id)
        ).all()
        throughput = session.scalar(
            text("""SELECT count(*) FROM processed_events
            WHERE processed_at>=now()-interval '1 minute' AND tenant_id=:tenant"""),
            {"tenant": tenant_id},
        )
        quarantine = session.scalar(text("SELECT count(*) FROM event_quarantine"))
        counters = dict(
            session.execute(select(OperationalCounter.name, OperationalCounter.value)).all()
        )
        result = {
            "tenant_id": tenant_id,
            "outbox": dict(backlog),
            "source_lag": dict(lag),
            "processing": dict(state) if state else None,
            "processed_events_last_minute": throughput,
            "quarantine_global": quarantine,
            "counters_global": counters,
            "api_latency_process_local": latency_report(),
            "coverage": [
                {
                    "currency": c.currency,
                    "status": c.status,
                    "start": c.start,
                    "end": c.end,
                    "verified_at": c.verified_at,
                    "metric_semantics": c.metric_semantics,
                    "job_id": c.job_id,
                }
                for c in coverage
            ],
        }
    settings = get_settings()
    try:
        with Redis.from_url(settings.redis_url, socket_timeout=2) as redis:
            result["redis"] = {
                "stream_length": redis.xlen(settings.order_stream),
                "groups": redis.xinfo_groups(settings.order_stream),
                "used_memory_bytes": redis.info("memory")["used_memory"],
            }
    except Exception:
        result["redis"] = {"status": "unavailable"}
    # Redis byte keys/values need normalization for FastAPI's JSON encoder.
    return result
