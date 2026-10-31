"""Multi-Database Aggregation Consumer for AstraRoll V2.

Consumes order events from Redis Streams and applies incremental rollups to each
tenant's dedicated PostgreSQL database with exact transaction boundaries and XACK.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from redis import Redis

from astraroll.aggregation.adapter import adapt_stream_message
from astraroll.aggregation.consumer import DEFAULT_GROUP, DEFAULT_STREAM, fair_messages
from astraroll.aggregation.processor import OrderEventProcessor
from astraroll.aggregation.repository import AggregateRepository
from astraroll.v2.router import TenantDatabaseUnavailable, TenantEngineManager, TenantRouterError

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class MultiTenantAggregationConsumer:
    """Worker consuming Redis Streams and writing to tenant-specific databases."""

    def __init__(
        self,
        engine_manager: TenantEngineManager,
        redis_client: Redis,
        stream_name: str = DEFAULT_STREAM,
        group_name: str = DEFAULT_GROUP,
        consumer_name: str | None = None,
        batch_size: int = 50,
        block_ms: int = 1000,
        min_idle_claim_ms: int = 30000,
        fair_dispatch: bool = True,
        dispatch_mode: Literal["fifo", "fair"] | None = None,
    ):
        self.engine_manager = engine_manager
        self.redis = redis_client
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or f"consumer-v2-{uuid4().hex[:8]}"
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.min_idle_claim_ms = min_idle_claim_ms
        if dispatch_mode is not None:
            self.fair_dispatch = dispatch_mode == "fair"
        else:
            self.fair_dispatch = fair_dispatch
        self.dispatch_mode: Literal["fifo", "fair"] = "fair" if self.fair_dispatch else "fifo"
        self.last_claim_ts: float = 0.0
        self.claim_cursor: str = "0-0"

    def init_group(self) -> None:
        """Create consumer group idempotently."""
        try:
            self.redis.xgroup_create(
                name=self.stream_name,
                groupname=self.group_name,
                id="0-0",
                mkstream=True,
            )
            log.info("Created consumer group %s on stream %s", self.group_name, self.stream_name)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                log.debug("Consumer group %s already exists", self.group_name)
            else:
                raise

    def process_message(self, stream_id: str, fields: dict[bytes | str, bytes | str]) -> bool:
        """Process a single event: adapt, route to tenant DB, commit, then XACK."""
        payload_raw = fields.get(b"event") or fields.get("event")
        if payload_raw is None:
            log.warning("Message %s missing 'event' field", stream_id)
            self.redis.xack(self.stream_name, self.group_name, stream_id)
            return True

        adapted = adapt_stream_message(stream_id, payload_raw, self.stream_name)

        if adapted.should_quarantine:
            log.warning("Quarantining message %s: %s", stream_id, adapted.quarantine_reason)
            if adapted.tenant_id is not None:
                try:
                    with self.engine_manager.session(adapted.tenant_id) as session:
                        with session.begin():
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
                except Exception as exc:
                    log.warning("Could not persist quarantine to tenant DB: %s", exc)
            self.redis.xack(self.stream_name, self.group_name, stream_id)
            return True

        assert adapted.event is not None
        tenant_id = adapted.event.tenant_id

        # Route to the tenant's dedicated database
        try:
            with self.engine_manager.session(tenant_id) as session:
                with session.begin():
                    processor = OrderEventProcessor(session)
                    processor.process_event(adapted.event, stream_id=stream_id)

            # Strictly XACK after tenant database transaction commits
            self.redis.xack(self.stream_name, self.group_name, stream_id)
            return True
        except (TenantRouterError, TenantDatabaseUnavailable) as exc:
            log.warning(
                "Tenant routing/database error for message %s (tenant %s): %s; "
                "leaving unacknowledged for reclaim",
                stream_id,
                tenant_id,
                exc,
            )
            return False
        except Exception as exc:
            log.exception(
                "Failed to process event %s for tenant %s (stream_id %s): %s",
                adapted.event.event_id,
                tenant_id,
                stream_id,
                exc,
            )
            raise

    def run_claim_cycle(self, force: bool = False) -> int:
        """Autoclaim abandoned messages across worker crashes."""
        now = time.monotonic()
        if not force and (now - self.last_claim_ts < 5.0):
            return 0
        self.last_claim_ts = now

        claimed_count = 0
        try:
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
            msg_iter = fair_messages(messages) if self.fair_dispatch else messages
            for stream_id, fields in msg_iter:
                sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                if self.process_message(sid, fields):
                    claimed_count += 1
        except Exception as exc:
            log.warning("XAUTOCLAIM cycle error: %s", exc)

        return claimed_count

    def process_batch(self) -> int:
        """Read and process a batch of messages using XREADGROUP."""
        self.run_claim_cycle()

        try:
            streams_res = self.redis.xreadgroup(
                groupname=self.group_name,
                consumername=self.consumer_name,
                streams={self.stream_name: ">"},
                count=self.batch_size,
                block=self.block_ms,
            )
        except Exception as exc:
            if "NOGROUP" in str(exc):
                self.init_group()
                return 0
            raise

        if not streams_res:
            return 0

        messages: list[tuple[str, dict[bytes | str, bytes | str]]] = []
        for _stream, msg_list in streams_res:
            for stream_id, fields in msg_list:
                sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                messages.append((sid, fields))

        processed = 0
        msg_iter = fair_messages(messages) if self.fair_dispatch else messages
        for stream_id, fields in msg_iter:
            if self.process_message(stream_id, fields):
                processed += 1

        return processed
