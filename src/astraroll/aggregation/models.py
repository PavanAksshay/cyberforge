"""SQLAlchemy ORM models for AstraRoll Aggregation Engine (Phase 2).

Defines:
- 6 durable aggregate tables (hourly/daily summary, status, product) with check constraints
  preventing negative order counts and sales amounts.
- order_projections table storing per-order canonical snapshot and last applied version.
- processed_events table for durable idempotency deduplication.
- event_quarantine table for logging and inspecting malformed/unsupported events.
- tenant_processing_state table tracking per-tenant revision and freshness apply timestamp.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from astraroll.db import Base


class HourlySummaryAggregate(Base):
    """Hourly order count and sales rollup aggregated per tenant and currency."""

    __tablename__ = "hourly_summary_aggregates"
    __table_args__ = (
        CheckConstraint("order_count >= 0", name="ck_hourly_summary_order_count"),
        CheckConstraint(
            "sales_amount >= 0 AND sales_amount <> 'NaN'::numeric",
            name="ck_hourly_summary_sales_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_hourly_summary_currency"),
        Index("ix_hourly_summary_lookup", "tenant_id", "currency", "bucket_start"),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    order_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 2), default=Decimal("0.00"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class DailySummaryAggregate(Base):
    """Daily order count and sales rollup aggregated per tenant and currency."""

    __tablename__ = "daily_summary_aggregates"
    __table_args__ = (
        CheckConstraint("order_count >= 0", name="ck_daily_summary_order_count"),
        CheckConstraint(
            "sales_amount >= 0 AND sales_amount <> 'NaN'::numeric",
            name="ck_daily_summary_sales_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_daily_summary_currency"),
        Index("ix_daily_summary_lookup", "tenant_id", "currency", "bucket_start"),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    order_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 2), default=Decimal("0.00"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class HourlyStatusAggregate(Base):
    """Hourly order count and sales rollup per tenant, currency, and status."""

    __tablename__ = "hourly_status_aggregates"
    __table_args__ = (
        CheckConstraint("order_count >= 0", name="ck_hourly_status_order_count"),
        CheckConstraint(
            "sales_amount >= 0 AND sales_amount <> 'NaN'::numeric",
            name="ck_hourly_status_sales_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_hourly_status_currency"),
        CheckConstraint(
            "status IN ('placed', 'shipped', 'cancelled')", name="ck_hourly_status_status"
        ),
        Index("ix_hourly_status_lookup", "tenant_id", "currency", "bucket_start", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    order_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 2), default=Decimal("0.00"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class DailyStatusAggregate(Base):
    """Daily order count and sales rollup per tenant, currency, and status."""

    __tablename__ = "daily_status_aggregates"
    __table_args__ = (
        CheckConstraint("order_count >= 0", name="ck_daily_status_order_count"),
        CheckConstraint(
            "sales_amount >= 0 AND sales_amount <> 'NaN'::numeric",
            name="ck_daily_status_sales_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_daily_status_currency"),
        CheckConstraint(
            "status IN ('placed', 'shipped', 'cancelled')", name="ck_daily_status_status"
        ),
        Index("ix_daily_status_lookup", "tenant_id", "currency", "bucket_start", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    order_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 2), default=Decimal("0.00"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class HourlyProductAggregate(Base):
    """Hourly order count and sales rollup per tenant, currency, and product_id."""

    __tablename__ = "hourly_product_aggregates"
    __table_args__ = (
        CheckConstraint("order_count >= 0", name="ck_hourly_product_order_count"),
        CheckConstraint(
            "sales_amount >= 0 AND sales_amount <> 'NaN'::numeric",
            name="ck_hourly_product_sales_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_hourly_product_currency"),
        Index("ix_hourly_product_lookup", "tenant_id", "currency", "bucket_start", "product_id"),
        Index(
            "ix_hourly_product_sales",
            "tenant_id",
            "currency",
            "bucket_start",
            "sales_amount",
            "order_count",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    product_id: Mapped[UUID] = mapped_column(primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    order_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 2), default=Decimal("0.00"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class DailyProductAggregate(Base):
    """Daily order count and sales rollup per tenant, currency, and product_id."""

    __tablename__ = "daily_product_aggregates"
    __table_args__ = (
        CheckConstraint("order_count >= 0", name="ck_daily_product_order_count"),
        CheckConstraint(
            "sales_amount >= 0 AND sales_amount <> 'NaN'::numeric",
            name="ck_daily_product_sales_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_daily_product_currency"),
        Index("ix_daily_product_lookup", "tenant_id", "currency", "bucket_start", "product_id"),
        Index(
            "ix_daily_product_sales",
            "tenant_id",
            "currency",
            "bucket_start",
            "sales_amount",
            "order_count",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    product_id: Mapped[UUID] = mapped_column(primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    order_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 2), default=Decimal("0.00"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class OrderProjection(Base):
    """Last applied state and canonical snapshot for each order."""

    __tablename__ = "order_projections"
    __table_args__ = (
        CheckConstraint("last_applied_version >= 0", name="ck_order_projection_version"),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    order_id: Mapped[UUID] = mapped_column(primary_key=True)
    last_applied_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    canonical_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    last_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcessedEvent(Base):
    """Durable record of processed event IDs for exact once deduplication."""

    __tablename__ = "processed_events"
    __table_args__ = (
        Index("ix_processed_events_order", "tenant_id", "order_id", "order_version"),
        Index("ix_processed_events_time", "processed_at"),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    order_id: Mapped[UUID] = mapped_column(nullable=False)
    order_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )


class EventQuarantine(Base):
    """Quarantine store for unparseable, unsupported-schema, or malformed events."""

    __tablename__ = "event_quarantine"
    __table_args__ = (
        Index("ix_quarantine_created", "quarantined_at"),
        Index("ix_quarantine_order", "tenant_id", "order_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    stream_name: Mapped[str] = mapped_column(String(120), nullable=False)
    stream_id: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    quarantined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    schema_version: Mapped[int | None] = mapped_column(nullable=True)
    tenant_id: Mapped[UUID | None] = mapped_column(nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(nullable=True)


class TenantProcessingState(Base):
    """Tenant processing revision and last successful apply timestamp for freshness tracking."""

    __tablename__ = "tenant_processing_state"

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_successful_apply_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_successful_stream_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
