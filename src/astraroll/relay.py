"""At-least-once outbox publisher. No consumers, rollups, or cache logic."""

import argparse
import json
import logging
import signal
from datetime import UTC, datetime
from threading import Event

from redis import Redis
from sqlalchemy import select

from astraroll.config import get_settings
from astraroll.db import get_session_factory
from astraroll.models import OutboxEvent

log = logging.getLogger(__name__)


def relay_batch(session_factory, redis: Redis, stream: str, batch_size: int = 100) -> int:
    # Keep locks through XADD and DB commit. Failure rolls back publication markers;
    # successful XADD followed by a crash/commit failure is intentionally redelivered.
    with session_factory.begin() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        for event in events:
            stream_id = redis.xadd(
                stream, {"event": json.dumps(event.payload, separators=(",", ":"))}
            )
            event.stream_id = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
            event.published_at = datetime.now(UTC)
        return len(events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Publish at most one batch")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5) as redis:
        while not stop.is_set():
            try:
                count = relay_batch(
                    get_session_factory(), redis, settings.order_stream, settings.relay_batch_size
                )
                if count:
                    log.info("Published %s outbox events", count)
            except Exception:
                log.exception("Relay failed; unpublished rows will be retried")
                if args.once:
                    raise
                stop.wait(max(settings.relay_poll_seconds, 1))
                continue
            if args.once:
                return
            if not count:
                stop.wait(settings.relay_poll_seconds)


if __name__ == "__main__":
    main()
