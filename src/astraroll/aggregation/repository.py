"""Durable repository for AstraRoll Aggregation Engine.

Implements atomic PostgreSQL upserts (INSERT ... ON CONFLICT DO UPDATE) for:
- 6 aggregate tables (hourly/daily summary, status, product).
- order_projections table with row-level locking (SELECT FOR UPDATE).
- processed_events table for durable idempotency.
- event_quarantine table for logging unprocessable messages.
- tenant_processing_state table for atomic revision and freshness tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from astraroll.aggregation.contracts import (
    AggregateDelta,
    AggregateType,
    AggregationKey,
    AggregationWindow,
)
from astraroll.aggregation.models import (
    DailyProductAggregate,
    DailyStatusAggregate,
    DailySummaryAggregate,
    EventQuarantine,
    HourlyProductAggregate,
    HourlyStatusAggregate,
    HourlySummaryAggregate,
    OrderProjection,
    ProcessedEvent,
    TenantProcessingState,
)


class AggregateRepository:
    """PostgreSQL repository providing atomic upserts and queries for aggregation data."""

    def __init__(self, session: Session):
        self.session = session

    def is_event_processed(self, event_id: UUID) -> bool:
        """Check if an event_id has already been committed in processed_events."""
        stmt = select(func.count()).select_from(ProcessedEvent).where(ProcessedEvent.event_id == event_id)
        count = self.session.scalar(stmt)
        return (count or 0) > 0

    def record_processed_event(
        self,
        event_id: UUID,
        tenant_id: UUID,
        order_id: UUID,
        order_version: int,
    ) -> None:
        """Insert event_id into processed_events to guarantee exact-once processing."""
        stmt = insert(ProcessedEvent).values(
            event_id=event_id,
            tenant_id=tenant_id,
            order_id=order_id,
            order_version=order_version,
            processed_at=func.now(),
        ).on_conflict_do_nothing(index_elements=["event_id"])
        self.session.execute(stmt)

    def lock_or_create_projection(
        self,
        tenant_id: UUID,
        order_id: UUID,
    ) -> OrderProjection:
        """Ensure a projection placeholder exists and lock the row FOR UPDATE."""
        # 1. Insert placeholder if not present
        placeholder_stmt = insert(OrderProjection).values(
            tenant_id=tenant_id,
            order_id=order_id,
            last_applied_version=0,
            canonical_snapshot={},
            last_event_id=None,
            updated_at=func.now(),
        ).on_conflict_do_nothing(index_elements=["tenant_id", "order_id"])
        self.session.execute(placeholder_stmt)

        # 2. SELECT ... FOR UPDATE
        select_stmt = (
            select(OrderProjection)
            .where(
                OrderProjection.tenant_id == tenant_id,
                OrderProjection.order_id == order_id,
            )
            .with_for_update()
        )
        projection = self.session.scalar(select_stmt)
        if projection is None:
            raise RuntimeError(f"Failed to acquire lock on projection ({tenant_id}, {order_id})")
        return projection

    def save_projection(
        self,
        tenant_id: UUID,
        order_id: UUID,
        version: int,
        snapshot: Dict[str, Any],
        last_event_id: Optional[UUID] = None,
    ) -> None:
        """Update the locked order projection with the latest canonical snapshot."""
        stmt = (
            insert(OrderProjection)
            .values(
                tenant_id=tenant_id,
                order_id=order_id,
                last_applied_version=version,
                canonical_snapshot=snapshot,
                last_event_id=last_event_id,
                updated_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "order_id"],
                set_={
                    "last_applied_version": version,
                    "canonical_snapshot": snapshot,
                    "last_event_id": last_event_id,
                    "updated_at": func.now(),
                },
            )
        )
        self.session.execute(stmt)

    def apply_aggregate_deltas(self, deltas: Dict[AggregationKey, AggregateDelta]) -> None:
        """Apply a batch of aggregation deltas atomically across the 6 aggregate tables.

        Skips zero deltas and uses PostgreSQL ON CONFLICT DO UPDATE to increment counters.
        """
        for key, delta in deltas.items():
            if delta.is_zero():
                continue

            tenant_uuid = UUID(key.tenant_id)
            sales_dec = Decimal(delta.sales_cents) / Decimal(100)

            if key.aggregate_type == AggregateType.SUMMARY:
                if key.window == AggregationWindow.HOURLY:
                    self._upsert_hourly_summary(tenant_uuid, key.currency, key.bucket_start, delta.order_count, sales_dec)
                else:
                    self._upsert_daily_summary(tenant_uuid, key.currency, key.bucket_start, delta.order_count, sales_dec)

            elif key.aggregate_type == AggregateType.STATUS:
                status_val = str(key.dimension_value)
                if key.window == AggregationWindow.HOURLY:
                    self._upsert_hourly_status(tenant_uuid, key.currency, status_val, key.bucket_start, delta.order_count, sales_dec)
                else:
                    self._upsert_daily_status(tenant_uuid, key.currency, status_val, key.bucket_start, delta.order_count, sales_dec)

            elif key.aggregate_type == AggregateType.PRODUCT:
                product_uuid = UUID(str(key.dimension_value))
                if key.window == AggregationWindow.HOURLY:
                    self._upsert_hourly_product(tenant_uuid, key.currency, product_uuid, key.bucket_start, delta.order_count, sales_dec)
                else:
                    self._upsert_daily_product(tenant_uuid, key.currency, product_uuid, key.bucket_start, delta.order_count, sales_dec)

    def _upsert_hourly_summary(
        self, tenant_id: UUID, currency: str, bucket_start: datetime, count_delta: int, sales_delta: Decimal
    ) -> None:
        init_count = max(0, count_delta)
        init_sales = max(Decimal("0.00"), sales_delta)
        stmt = insert(HourlySummaryAggregate).values(
            tenant_id=tenant_id,
            currency=currency,
            bucket_start=bucket_start,
            order_count=init_count,
            sales_amount=init_sales,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "currency", "bucket_start"],
            set_={
                "order_count": HourlySummaryAggregate.order_count + count_delta,
                "sales_amount": HourlySummaryAggregate.sales_amount + sales_delta,
                "updated_at": func.now(),
            },
        )
        self.session.execute(stmt)

    def _upsert_daily_summary(
        self, tenant_id: UUID, currency: str, bucket_start: datetime, count_delta: int, sales_delta: Decimal
    ) -> None:
        init_count = max(0, count_delta)
        init_sales = max(Decimal("0.00"), sales_delta)
        stmt = insert(DailySummaryAggregate).values(
            tenant_id=tenant_id,
            currency=currency,
            bucket_start=bucket_start,
            order_count=init_count,
            sales_amount=init_sales,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "currency", "bucket_start"],
            set_={
                "order_count": DailySummaryAggregate.order_count + count_delta,
                "sales_amount": DailySummaryAggregate.sales_amount + sales_delta,
                "updated_at": func.now(),
            },
        )
        self.session.execute(stmt)

    def _upsert_hourly_status(
        self, tenant_id: UUID, currency: str, status: str, bucket_start: datetime, count_delta: int, sales_delta: Decimal
    ) -> None:
        init_count = max(0, count_delta)
        init_sales = max(Decimal("0.00"), sales_delta)
        stmt = insert(HourlyStatusAggregate).values(
            tenant_id=tenant_id,
            currency=currency,
            status=status,
            bucket_start=bucket_start,
            order_count=init_count,
            sales_amount=init_sales,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "currency", "status", "bucket_start"],
            set_={
                "order_count": HourlyStatusAggregate.order_count + count_delta,
                "sales_amount": HourlyStatusAggregate.sales_amount + sales_delta,
                "updated_at": func.now(),
            },
        )
        self.session.execute(stmt)

    def _upsert_daily_status(
        self, tenant_id: UUID, currency: str, status: str, bucket_start: datetime, count_delta: int, sales_delta: Decimal
    ) -> None:
        init_count = max(0, count_delta)
        init_sales = max(Decimal("0.00"), sales_delta)
        stmt = insert(DailyStatusAggregate).values(
            tenant_id=tenant_id,
            currency=currency,
            status=status,
            bucket_start=bucket_start,
            order_count=init_count,
            sales_amount=init_sales,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "currency", "status", "bucket_start"],
            set_={
                "order_count": DailyStatusAggregate.order_count + count_delta,
                "sales_amount": DailyStatusAggregate.sales_amount + sales_delta,
                "updated_at": func.now(),
            },
        )
        self.session.execute(stmt)

    def _upsert_hourly_product(
        self, tenant_id: UUID, currency: str, product_id: UUID, bucket_start: datetime, count_delta: int, sales_delta: Decimal
    ) -> None:
        init_count = max(0, count_delta)
        init_sales = max(Decimal("0.00"), sales_delta)
        stmt = insert(HourlyProductAggregate).values(
            tenant_id=tenant_id,
            currency=currency,
            product_id=product_id,
            bucket_start=bucket_start,
            order_count=init_count,
            sales_amount=init_sales,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "currency", "product_id", "bucket_start"],
            set_={
                "order_count": HourlyProductAggregate.order_count + count_delta,
                "sales_amount": HourlyProductAggregate.sales_amount + sales_delta,
                "updated_at": func.now(),
            },
        )
        self.session.execute(stmt)

    def _upsert_daily_product(
        self, tenant_id: UUID, currency: str, product_id: UUID, bucket_start: datetime, count_delta: int, sales_delta: Decimal
    ) -> None:
        init_count = max(0, count_delta)
        init_sales = max(Decimal("0.00"), sales_delta)
        stmt = insert(DailyProductAggregate).values(
            tenant_id=tenant_id,
            currency=currency,
            product_id=product_id,
            bucket_start=bucket_start,
            order_count=init_count,
            sales_amount=init_sales,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "currency", "product_id", "bucket_start"],
            set_={
                "order_count": DailyProductAggregate.order_count + count_delta,
                "sales_amount": DailyProductAggregate.sales_amount + sales_delta,
                "updated_at": func.now(),
            },
        )
        self.session.execute(stmt)

    def quarantine_event(
        self,
        stream_name: str,
        stream_id: str,
        raw_payload: str,
        reason: str,
        event_id: Optional[UUID] = None,
        schema_version: Optional[int] = None,
        tenant_id: Optional[UUID] = None,
        order_id: Optional[UUID] = None,
    ) -> UUID:
        """Persist a failed event into the event_quarantine table."""
        record = EventQuarantine(
            stream_name=stream_name,
            stream_id=stream_id,
            raw_payload=raw_payload,
            reason=reason,
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )
        self.session.add(record)
        self.session.flush()
        return record.id

    def update_tenant_processing_state(
        self,
        tenant_id: UUID,
        stream_id: Optional[str] = None,
    ) -> int:
        """Atomically increment tenant revision and record last_successful_apply_at."""
        stmt = (
            insert(TenantProcessingState)
            .values(
                tenant_id=tenant_id,
                revision=1,
                last_successful_apply_at=func.now(),
                last_successful_stream_id=stream_id,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={
                    "revision": TenantProcessingState.revision + 1,
                    "last_successful_apply_at": func.now(),
                    "last_successful_stream_id": stream_id or TenantProcessingState.last_successful_stream_id,
                },
            )
            .returning(TenantProcessingState.revision)
        )
        new_rev = self.session.scalar(stmt)
        return int(new_rev or 1)
