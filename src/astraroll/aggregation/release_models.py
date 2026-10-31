"""Durable coverage, resumable rebuild state and audit archives; revision 0004."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from astraroll.db import Base


class BootstrapJob(Base):
    __tablename__ = "bootstrap_jobs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    currencies: Mapped[list] = mapped_column(JSONB)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metric_semantics: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="building")
    cursor: Mapped[UUID | None] = mapped_column()
    copied: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AggregateCoverage(Base):
    __tablename__ = "aggregate_coverage"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("bootstrap_jobs.id"))
    status: Mapped[str] = mapped_column(String(20))
    metric_semantics: Mapped[str] = mapped_column(String(80))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_order_count: Mapped[int] = mapped_column(BigInteger, default=0)


class BootstrapOrder(Base):
    __tablename__ = "bootstrap_orders"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("bootstrap_jobs.id"), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    snapshot: Mapped[dict] = mapped_column(JSONB)


class RebuildArchive(Base):
    __tablename__ = "rebuild_archive"
    __table_args__ = (Index("ix_rebuild_archive_job", "job_id", "table_name"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("bootstrap_jobs.id"))
    table_name: Mapped[str] = mapped_column(String(80))
    row_data: Mapped[dict] = mapped_column(JSONB)


class OperationalCounter(Base):
    __tablename__ = "operational_counters"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
