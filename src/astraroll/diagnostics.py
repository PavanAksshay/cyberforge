"""Operational diagnostics for AstraRoll V1 pipeline.

Provides real-time visibility into:
- Last applied event timestamp
- Actual source-to-aggregate delay
- Redis backlog (stream length) and pending messages (PEL)
- Unpublished outbox count and oldest unpublished event age
- Quarantined event count
- Pipeline health status (healthy idle, active processing, stuck, or backlogged)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from astraroll.aggregation.models import (
    EventQuarantine,
    ProcessedEvent,
    TenantProcessingState,
)
from astraroll.models import OutboxEvent


@dataclass(frozen=True)
class PipelineDiagnostics:
    collected_at: str
    tenant_id: str | None
    last_applied_event_at: str | None
    source_to_aggregate_delay_seconds: float | None
    redis_stream_length: int
    redis_pending_messages: int
    unpublished_outbox_count: int
    oldest_unpublished_age_seconds: float | None
    quarantined_event_count: int
    pipeline_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect_diagnostics(
    session: Session,
    redis: Redis,
    stream_name: str,
    group_name: str | None = None,
    tenant_id: UUID | None = None,
) -> PipelineDiagnostics:
    now = datetime.now(UTC)

    # 1. Outbox health
    outbox_stmt = select(
        func.count().label("cnt"),
        func.min(OutboxEvent.occurred_at).label("oldest"),
    ).where(OutboxEvent.published_at.is_(None))
    if tenant_id:
        outbox_stmt = outbox_stmt.where(OutboxEvent.tenant_id == tenant_id)

    outbox_row = session.execute(outbox_stmt).one()
    unpub_count = outbox_row.cnt or 0
    oldest_unpub = outbox_row.oldest
    oldest_unpub_age = (now - oldest_unpub).total_seconds() if oldest_unpub else None

    # 2. Redis stream length & pending
    try:
        stream_len = redis.xlen(stream_name)
    except Exception:
        stream_len = 0

    pending_count = 0
    if group_name:
        try:
            pel = redis.xpending(stream_name, group_name)
            pending_count = pel.get("pending", 0) if isinstance(pel, dict) else 0
        except Exception:
            pending_count = 0

    # 3. Last applied event & processing delay
    if tenant_id:
        t_state = session.scalar(
            select(TenantProcessingState).where(TenantProcessingState.tenant_id == tenant_id)
        )
        last_applied = t_state.last_successful_apply_at if t_state else None
    else:
        last_applied = session.scalar(
            select(func.max(TenantProcessingState.last_successful_apply_at))
        )
        if not last_applied:
            last_applied = session.scalar(select(func.max(ProcessedEvent.occurred_at)))

    delay_sec = None
    if last_applied:
        # Normalize timezone
        if last_applied.tzinfo is None:
            last_applied = last_applied.replace(tzinfo=UTC)
        delay_sec = max(0.0, (now - last_applied).total_seconds())

    # 4. Quarantined count
    quar_stmt = select(func.count()).select_from(EventQuarantine)
    if tenant_id:
        quar_stmt = quar_stmt.where(EventQuarantine.tenant_id == tenant_id)
    quarantine_count = session.scalar(quar_stmt) or 0

    # 5. Derive health state
    if unpub_count > 500 or pending_count > 500:
        status = "backlogged"
    elif oldest_unpub_age is not None and oldest_unpub_age > 60:
        status = "stuck_relay"
    elif pending_count > 0 and delay_sec is not None and delay_sec > 60:
        status = "stuck_consumer"
    elif unpub_count == 0 and pending_count == 0:
        status = "healthy_idle"
    else:
        status = "active_processing"

    return PipelineDiagnostics(
        collected_at=now.isoformat(),
        tenant_id=str(tenant_id) if tenant_id else None,
        last_applied_event_at=last_applied.isoformat() if last_applied else None,
        source_to_aggregate_delay_seconds=round(delay_sec, 3) if delay_sec is not None else None,
        redis_stream_length=stream_len,
        redis_pending_messages=pending_count,
        unpublished_outbox_count=unpub_count,
        oldest_unpublished_age_seconds=round(oldest_unpub_age, 3) if oldest_unpub_age else None,
        quarantined_event_count=quarantine_count,
        pipeline_status=status,
    )


def main() -> None:
    import argparse
    import json

    from astraroll.config import get_settings
    from astraroll.db import get_session_factory

    parser = argparse.ArgumentParser(description="AstraRoll Pipeline Diagnostics")
    parser.add_argument("--stream", type=str, default=None)
    parser.add_argument("--group", type=str, default="astraroll:aggregators")
    parser.add_argument("--tenant", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    stream = args.stream or settings.order_stream
    t_id = UUID(args.tenant) if args.tenant else None

    with get_session_factory()() as session:
        with Redis.from_url(settings.redis_url, socket_timeout=5) as redis:
            diag = collect_diagnostics(session, redis, stream, args.group, t_id)
            if args.json:
                print(json.dumps(diag.to_dict(), indent=2))
            else:
                print("==================================================")
                print("       ASTRAROLL STREAMING PIPELINE DIAGNOSTICS    ")
                print("==================================================")
                print(f"Status:                      {diag.pipeline_status.upper()}")
                print(f"Collected At:                {diag.collected_at}")
                print(f"Tenant Filter:               {diag.tenant_id or 'All'}")
                print(f"Unpublished Outbox Events:   {diag.unpublished_outbox_count}")
                print(
                    f"Oldest Outbox Age:           {diag.oldest_unpublished_age_seconds or 0:.2f}s"
                )
                print(f"Redis Stream Length:         {diag.redis_stream_length}")
                print(f"Redis Pending (PEL):         {diag.redis_pending_messages}")
                print(f"Last Applied Event:          {diag.last_applied_event_at or 'None'}")
                lag = diag.source_to_aggregate_delay_seconds or 0
                print(f"Source-to-Aggregate Lag:     {lag:.2f}s")
                print(f"Quarantined Events:          {diag.quarantined_event_count}")
                print("==================================================")


if __name__ == "__main__":
    main()
