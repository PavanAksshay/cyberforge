"""Outbox Disaster Replay CLI for AstraRoll Aggregation Engine (Phase 2).

Replays historical events from the PostgreSQL outbox_events table into Redis Streams
(stream: astraroll:orders:v1, field: event) in deterministic chronological order.

Because Developer B's aggregation processor maintains exact-once idempotency (via processed_events)
and monotonically increasing version ordering (via order_projections), replaying the outbox
stream is completely safe and guarantees convergence after stream loss or corruption.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from typing import Optional
from uuid import UUID

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from astraroll.config import get_settings
from astraroll.db import get_session_factory
from astraroll.models import OutboxEvent

log = logging.getLogger("astraroll.replay")


def replay_outbox_events(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    stream_name: str = "astraroll:orders:v1",
    tenant_id: Optional[UUID] = None,
    from_ts: Optional[datetime] = None,
    to_ts: Optional[datetime] = None,
    batch_size: int = 100,
    dry_run: bool = False,
) -> int:
    """Read events from outbox_events and publish to Redis Streams.

    Ordered strictly by (occurred_at ASC, id ASC).
    Returns total number of replayed events.
    """
    total_replayed = 0
    offset = 0

    with session_factory() as session:
        while True:
            stmt = select(OutboxEvent).order_by(OutboxEvent.occurred_at.asc(), OutboxEvent.id.asc())

            if tenant_id is not None:
                stmt = stmt.where(OutboxEvent.tenant_id == tenant_id)
            if from_ts is not None:
                stmt = stmt.where(OutboxEvent.occurred_at >= from_ts)
            if to_ts is not None:
                stmt = stmt.where(OutboxEvent.occurred_at < to_ts)

            stmt = stmt.offset(offset).limit(batch_size)
            events = session.scalars(stmt).all()

            if not events:
                break

            for event in events:
                payload_str = json.dumps(event.payload, separators=(",", ":"))
                if not dry_run:
                    redis_client.xadd(stream_name, {"event": payload_str})
                total_replayed += 1

            offset += len(events)
            log.info("Replay progress: %d events processed so far...", total_replayed)

    log.info("Replay complete. Total events processed: %d (dry_run=%s)", total_replayed, dry_run)
    return total_replayed


def main() -> None:
    """CLI entrypoint for astraroll-replay-outbox."""
    parser = argparse.ArgumentParser(description="Replay outbox events into Redis Streams")
    parser.add_argument("--stream", type=str, default="astraroll:orders:v1", help="Target Redis stream")
    parser.add_argument("--tenant-id", type=str, default=None, help="Optional filter by tenant UUID")
    parser.add_argument("--from-ts", type=str, default=None, help="Optional ISO datetime start boundary")
    parser.add_argument("--to-ts", type=str, default=None, help="Optional ISO datetime end boundary")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch query size")
    parser.add_argument("--dry-run", action="store_true", help="Scan and count without publishing to Redis")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = get_settings()

    tenant_uuid = UUID(args.tenant_id) if args.tenant_id else None
    from_dt = datetime.fromisoformat(args.from_ts) if args.from_ts else None
    if from_dt and from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(args.to_ts) if args.to_ts else None
    if to_dt and to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=timezone.utc)

    session_factory = get_session_factory()
    with Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5) as redis_client:
        replay_outbox_events(
            session_factory=session_factory,
            redis_client=redis_client,
            stream_name=args.stream,
            tenant_id=tenant_uuid,
            from_ts=from_dt,
            to_ts=to_dt,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
