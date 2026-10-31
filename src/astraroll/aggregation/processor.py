"""Transactional Canonical-State Reconciler and Event Processor (Phase 2).

Implements Section 11 reconciliation semantics:
- Durable deduplication via processed_events.
- Row-level lock acquisition on order_projections (FOR UPDATE).
- Authoritative PostgreSQL order lookup for canonical state truth.
- Differential contribution computation via compute_contribution_delta.
- Atomic aggregate upserting.
- Atomic projection updating and tenant revision increment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from astraroll.aggregation.contracts import (
    AggregateDelta,
    AggregationKey,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
)
from astraroll.aggregation.contribution import compute_contribution_delta
from astraroll.aggregation.repository import AggregateRepository
from astraroll.models import Order


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of processing an individual order event."""

    applied: bool
    is_duplicate: bool = False
    is_out_of_order: bool = False
    event_id: Optional[str] = None
    order_id: Optional[str] = None
    tenant_id: Optional[str] = None
    tenant_revision: Optional[int] = None
    deltas: Dict[AggregationKey, AggregateDelta] = field(default_factory=dict)
    error_message: Optional[str] = None


class OrderEventProcessor:
    """Processes order events against PostgreSQL with durable deduplication and locking."""

    def __init__(self, session: Session):
        self.session = session
        self.repo = AggregateRepository(session)

    def process_event(
        self,
        event: OrderEvent,
        stream_id: Optional[str] = None,
    ) -> ProcessResult:
        """Process a validated OrderEvent in a transactional boundary.

        1. Checks processed_events for duplicate event_id.
        2. Locks the order_projections row FOR UPDATE.
        3. Detects out-of-order / superseded versions.
        4. Reconciles with authoritative Order row in PostgreSQL.
        5. Computes differential aggregate deltas.
        6. Applies deltas atomically to the 6 aggregate tables.
        7. Updates order_projections state.
        8. Records processed_events idempotency entry.
        9. Increments tenant revision and freshness timestamp.
        """
        event_uuid = UUID(event.event_id)
        tenant_uuid = UUID(event.tenant_id)
        order_uuid = UUID(event.order_id)

        # 1. Exact-once check: processed_events deduplication
        if self.repo.is_event_processed(event_uuid):
            return ProcessResult(
                applied=False,
                is_duplicate=True,
                event_id=event.event_id,
                order_id=event.order_id,
                tenant_id=event.tenant_id,
            )

        # 2. Acquire row-level lock on order projection
        projection = self.repo.lock_or_create_projection(tenant_uuid, order_uuid)

        # 3. Out-of-order check against projection version
        if projection.last_applied_version >= event.order_version:
            # Mark processed so we don't re-evaluate this stale event
            self.repo.record_processed_event(
                event_id=event_uuid,
                tenant_id=tenant_uuid,
                order_id=order_uuid,
                order_version=event.order_version,
            )
            return ProcessResult(
                applied=False,
                is_out_of_order=True,
                event_id=event.event_id,
                order_id=event.order_id,
                tenant_id=event.tenant_id,
            )

        # 4. Canonical order lookup from PostgreSQL
        canonical_stmt = select(Order).where(
            Order.id == order_uuid,
            Order.tenant_id == tenant_uuid,
        )
        canonical_order = self.session.scalar(canonical_stmt)

        if canonical_order is not None and canonical_order.version >= event.order_version:
            # Construct target snapshot from authoritative PostgreSQL order
            amount_cents = int((canonical_order.amount * Decimal(100)).to_integral_exact())
            target_snapshot = OrderSnapshot(
                product_id=str(canonical_order.product_id),
                amount_cents=amount_cents,
                currency=canonical_order.currency,
                status=OrderStatus(canonical_order.status),
                created_at=canonical_order.created_at,
                updated_at=canonical_order.updated_at,
                order_id=str(canonical_order.id),
                tenant_id=str(canonical_order.tenant_id),
            )
            target_version = canonical_order.version
        else:
            # Fallback to the snapshot in the event payload
            target_snapshot = event.snapshot
            target_version = event.order_version

        # 5. Extract previous projection snapshot if present
        old_snapshot: Optional[OrderSnapshot] = None
        if projection.last_applied_version > 0 and projection.canonical_snapshot:
            try:
                old_snapshot = OrderSnapshot.model_validate(projection.canonical_snapshot)
            except Exception:
                old_snapshot = None

        # 6. Calculate differential deltas
        deltas = compute_contribution_delta(
            old_snapshot=old_snapshot,
            new_snapshot=target_snapshot,
            tenant_id=str(tenant_uuid),
        )

        # 7. Apply aggregate deltas atomically
        self.repo.apply_aggregate_deltas(deltas)

        # 8. Update projection row
        self.repo.save_projection(
            tenant_id=tenant_uuid,
            order_id=order_uuid,
            version=target_version,
            snapshot=target_snapshot.model_dump(mode="json"),
            last_event_id=event_uuid,
        )

        # 9. Record processed event for durable deduplication
        self.repo.record_processed_event(
            event_id=event_uuid,
            tenant_id=tenant_uuid,
            order_id=order_uuid,
            order_version=event.order_version,
        )

        # 10. Increment tenant revision and update freshness
        new_rev = self.repo.update_tenant_processing_state(
            tenant_id=tenant_uuid,
            stream_id=stream_id,
        )

        return ProcessResult(
            applied=True,
            is_duplicate=False,
            is_out_of_order=False,
            event_id=event.event_id,
            order_id=event.order_id,
            tenant_id=event.tenant_id,
            tenant_revision=new_rev,
            deltas=deltas,
        )
