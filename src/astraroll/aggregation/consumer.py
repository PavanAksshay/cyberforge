"""Redis Streams Aggregation Consumer Worker (Phase 2).

Consumes from stream `astraroll:orders:v1` under consumer group `astraroll:aggregators`.
Guarantees:
- Consumer group creation at ID 0-0 (mkstream=True).
- Version-aware event parsing and V2 envelope enforcement.
- Quarantine handling: records bad/V1 messages into event_quarantine before XACK.
- Transactional reconciliation and aggregate updates in PostgreSQL.
- XACK strictly *after* successful database commit.
- XAUTOCLAIM background recovery for abandoned messages.
- Graceful shutdown handling SIGINT and SIGTERM.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from threading import Event
from typing import Any
from uuid import uuid4

from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy.orm import Session, sessionmaker

from astraroll.aggregation.adapter import adapt_stream_message
from astraroll.aggregation.processor import OrderEventProcessor
from astraroll.aggregation.repository import AggregateRepository
from astraroll.config import get_settings
from astraroll.db import get_session_factory

log = logging.getLogger("astraroll.consumer")

DEFAULT_STREAM = "astraroll:orders:v1"
DEFAULT_GROUP = "astraroll:aggregators"


class AggregationConsumer:
    """Worker that reads orders from Redis Streams and applies rollups into PostgreSQL."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        redis_client: Redis,
        stream_name: str = DEFAULT_STREAM,
        group_name: str = DEFAULT_GROUP,
        consumer_name: str | None = None,
        batch_size: int = 50,
        block_ms: int = 1000,
        min_idle_claim_ms: int = 30000,
    ):
        self.session_factory = session_factory
        self.redis = redis_client
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or f"consumer-{uuid4().hex[:8]}"
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.min_idle_claim_ms = min_idle_claim_ms
        self.last_claim_ts: float = 0.0
        self.claim_cursor: str = "0-0"

    def init_group(self) -> None:
        """Create consumer group if it does not already exist, starting at '0-0'."""
        try:
            self.redis.xgroup_create(
                name=self.stream_name,
                groupname=self.group_name,
                id="0-0",
                mkstream=True,
            )
            log.info(
                "Created consumer group '%s' on stream '%s' at 0-0",
                self.group_name,
                self.stream_name,
            )
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                log.debug("Consumer group '%s' already exists", self.group_name)
            else:
                raise

    def process_message(self, stream_id_raw: Any, fields: dict[Any, Any]) -> bool:
        """Process a single Redis Streams message.

        Returns True if processing/quarantine succeeded and message was acknowledged.
        """
        stream_id = (
            stream_id_raw.decode("utf-8")
            if isinstance(stream_id_raw, bytes)
            else str(stream_id_raw)
        )

        # Look for field 'event'
        payload_raw = fields.get(b"event") or fields.get("event")
        if payload_raw is None:
            log.warning("Message %s missing 'event' field; quarantining", stream_id)
            with self.session_factory() as session:
                repo = AggregateRepository(session)
                repo.quarantine_event(
                    stream_name=self.stream_name,
                    stream_id=stream_id,
                    raw_payload=str(fields),
                    reason="Missing 'event' field in stream message",
                )
                session.commit()
            self.redis.xack(self.stream_name, self.group_name, stream_id)
            return True

        # Parse and adapt message
        adapted = adapt_stream_message(stream_id, payload_raw, self.stream_name)

        if adapted.should_quarantine:
            log.warning("Quarantining message %s: %s", stream_id, adapted.quarantine_reason)
            with self.session_factory() as session:
                repo = AggregateRepository(session)
                repo.quarantine_event(
                    stream_name=self.stream_name,
                    stream_id=stream_id,
                    raw_payload=adapted.raw_payload,
                    reason=adapted.quarantine_reason or "Unknown quarantine reason",
                    event_id=adapted.event_id,
                    schema_version=adapted.schema_version,
                    tenant_id=adapted.tenant_id,
                    order_id=adapted.order_id,
                )
                session.commit()
            # Quarantine is durable in DB; acknowledge message so it won't be redelivered
            self.redis.xack(self.stream_name, self.group_name, stream_id)
            return True

        assert adapted.event is not None

        # Execute transactional reconciliation and aggregation in PostgreSQL
        with self.session_factory() as session:
            try:
                processor = OrderEventProcessor(session)
                result = processor.process_event(adapted.event, stream_id=stream_id)
                session.commit()
            except Exception as exc:
                session.rollback()
                log.exception(
                    "Failed to process event %s (stream_id %s): %s",
                    adapted.event.event_id,
                    stream_id,
                    exc,
                )
                raise

        # Strictly acknowledge message AFTER database transaction commits
        self.redis.xack(self.stream_name, self.group_name, stream_id)
        log.debug(
            "Acknowledged message %s (event_id=%s, applied=%s)",
            stream_id,
            adapted.event.event_id,
            result.applied,
        )
        return True

    def run_claim_cycle(self, force: bool = False) -> int:
        """Autoclaim abandoned messages that have been idle longer than min_idle_claim_ms.

        Uses progressive claim_cursor so full batches of pending messages are systematically
        processed without starvation or infinite looping on the head of the PEL.
        """
        now = time.monotonic()
        # Throttle claim checks in background loop
        if not force and (now - self.last_claim_ts < 5.0):
            return 0
        self.last_claim_ts = now

        claimed_count = 0
        try:
            # xautoclaim returns (next_start_id, messages, [deleted_ids])
            res = self.redis.xautoclaim(
                name=self.stream_name,
                groupname=self.group_name,
                consumername=self.consumer_name,
                min_idle_time=self.min_idle_claim_ms,
                start_id=self.claim_cursor,
                count=self.batch_size,
            )
            if isinstance(res, (list, tuple)) and len(res) > 0:
                next_c = res[0]
                self.claim_cursor = (
                    next_c.decode("utf-8") if isinstance(next_c, bytes) else str(next_c)
                )

            messages = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else []
            for stream_id, fields in messages:
                if self.process_message(stream_id, fields):
                    claimed_count += 1
            if claimed_count > 0:
                log.info(
                    "Recovered and processed %d abandoned messages via XAUTOCLAIM (cursor: %s)",
                    claimed_count,
                    self.claim_cursor,
                )
        except Exception as exc:
            log.warning("XAUTOCLAIM cycle error: %s", exc)

        return claimed_count

    def process_batch(self) -> int:
        """Read and process a batch of new messages using XREADGROUP."""
        # 1. Recovery check for abandoned messages
        self.run_claim_cycle()

        # 2. Read new messages
        try:
            streams_res = self.redis.xreadgroup(
                groupname=self.group_name,
                consumername=self.consumer_name,
                streams={self.stream_name: ">"},
                count=self.batch_size,
                block=self.block_ms,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                self.init_group()
                return 0
            raise

        if not streams_res:
            return 0

        processed_count = 0
        for stream_key, messages in streams_res:
            for stream_id, fields in messages:
                if self.process_message(stream_id, fields):
                    processed_count += 1

        return processed_count

    def run_loop(self, stop_event: Event) -> None:
        """Continuous execution loop until stop_event is set."""
        self.init_group()
        log.info(
            "AggregationConsumer '%s' running on stream '%s'", self.consumer_name, self.stream_name
        )

        while not stop_event.is_set():
            try:
                self.process_batch()
            except Exception as exc:
                if stop_event.is_set():
                    break
                log.error("Error in consumer loop: %s", exc, exc_info=True)
                time.sleep(1.0)

        log.info("AggregationConsumer '%s' shut down cleanly", self.consumer_name)


def main() -> None:
    """CLI entrypoint for astraroll-consumer."""
    parser = argparse.ArgumentParser(description="AstraRoll Aggregation Consumer Worker")
    parser.add_argument("--once", action="store_true", help="Process at most one batch and exit")
    parser.add_argument("--stream", type=str, default=DEFAULT_STREAM, help="Redis stream name")
    parser.add_argument("--group", type=str, default=DEFAULT_GROUP, help="Consumer group name")
    parser.add_argument("--consumer", type=str, default=None, help="Consumer instance name")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size per read")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    settings = get_settings()

    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    session_factory = get_session_factory()
    with Redis.from_url(
        settings.redis_url, socket_timeout=5, socket_connect_timeout=5
    ) as redis_client:
        consumer = AggregationConsumer(
            session_factory=session_factory,
            redis_client=redis_client,
            stream_name=args.stream,
            group_name=args.group,
            consumer_name=args.consumer,
            batch_size=args.batch_size,
        )
        if args.once:
            consumer.init_group()
            consumer.process_batch()
        else:
            consumer.run_loop(stop)


if __name__ == "__main__":
    main()
