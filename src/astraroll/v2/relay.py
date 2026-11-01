"""Multi-Tenant Outbox Relay Worker for AstraRoll V2.

Polls active tenant databases in fair round-robin fashion and publishes outbox events
to Redis Streams with at-least-once delivery guarantees.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from redis import Redis
from sqlalchemy import select

from astraroll.models import OutboxEvent
from astraroll.v2.registry import ControlRegistry
from astraroll.v2.router import TenantDatabaseUnavailable, TenantEngineManager

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class MultiTenantRelay:
    """Outbox publisher polling across all active tenant databases.

    Features:
    - Round-robin fair polling across active tenants to prevent starvation.
    - Fault isolation: database errors on one tenant do not block or delay other tenants.
    - Atomically updates published_at and stream_id within each tenant's dedicated database.
    """

    def __init__(
        self,
        registry: ControlRegistry,
        engine_manager: TenantEngineManager,
        redis_client: Redis,
        stream_name: str = "astraroll:orders:v1",
        batch_size: int = 100,
    ):
        self.registry = registry
        self.engine_manager = engine_manager
        self.redis = redis_client
        self.stream_name = stream_name
        self.batch_size = batch_size
        self._tenant_cursor: int = 0

    def relay_tenant(self, tenant_id: UUID, batch_size: int | None = None) -> int:
        """Publish unpublished outbox events for a single tenant database."""
        limit = batch_size or self.batch_size
        published = 0
        try:
            with self.engine_manager.session(tenant_id) as session:
                with session.begin():
                    events = session.scalars(
                        select(OutboxEvent)
                        .where(OutboxEvent.published_at.is_(None))
                        .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    ).all()

                    for event in events:
                        stream_id = self.redis.xadd(
                            self.stream_name,
                            {"event": json.dumps(event.payload, separators=(",", ":"))},
                        )
                        event.stream_id = (
                            stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                        )
                        event.published_at = datetime.now(UTC)
                        published += 1

            return published
        except TenantDatabaseUnavailable as exc:
            log.warning("Skipping unavailable tenant %s during relay: %s", tenant_id, exc)
            return 0
        except Exception as exc:
            log.warning("Relay error for tenant %s: %s", tenant_id, exc)
            return 0

    def relay_batch(self, batch_size: int | None = None) -> int:
        """Execute one polling pass across all active tenant databases in round-robin order."""
        active_tenants = self.registry.list_active_tenants()
        if not active_tenants:
            return 0

        total_published = 0
        # Rotate starting index to maintain fair scheduling across poll iterations
        n = len(active_tenants)
        start_idx = self._tenant_cursor % n
        self._tenant_cursor = (self._tenant_cursor + 1) % n

        ordered_tenants = active_tenants[start_idx:] + active_tenants[:start_idx]
        for record in ordered_tenants:
            count = self.relay_tenant(record.tenant_id, batch_size=batch_size)
            total_published += count

        return total_published
