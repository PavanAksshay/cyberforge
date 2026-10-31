"""Central Tenant Registry and Control Plane Models for AstraRoll V2."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Column, DateTime, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Session

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class TenantStatus(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    MIGRATING = "migrating"
    ACTIVE = "active"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    DECOMMISSIONED = "decommissioned"


class ControlBase(DeclarativeBase):
    pass


class TenantRegistryRecord(ControlBase):
    """Metadata record representing a tenant in the control database."""

    __tablename__ = "control_tenant_registry"

    tenant_id = Column(PG_UUID(as_uuid=True), primary_key=True)
    name = Column(String(255), nullable=False)
    database_name = Column(String(255), nullable=False, unique=True, index=True)
    database_alias = Column(String(255), nullable=False)
    status = Column(String(50), nullable=False, default=TenantStatus.PENDING.value, index=True)
    schema_version = Column(String(50), nullable=False, default="0004_coverage_precision")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_verified_at = Column(DateTime(timezone=True), nullable=True)

    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE.value


class TenantMigrationCheckpoint(ControlBase):
    """Durable checkpoint tracking online/offline tenant migration progress."""

    __tablename__ = "control_tenant_migration_checkpoints"

    tenant_id = Column(PG_UUID(as_uuid=True), primary_key=True)
    phase = Column(String(50), nullable=False, default="INIT")
    last_order_id = Column(PG_UUID(as_uuid=True), nullable=True)
    last_order_created_at = Column(DateTime(timezone=True), nullable=True)
    orders_copied = Column(Integer, nullable=False, default=0)
    outbox_copied = Column(Integer, nullable=False, default=0)
    rollups_copied = Column(Integer, nullable=False, default=0)
    status = Column(String(50), nullable=False, default="IN_PROGRESS")
    error_message = Column(Text, nullable=True)
    recovery_backup_path = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)


class ControlRegistry:
    """Manager for the central tenant registry in the control database."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def init_schema(self) -> None:
        """Create the control plane schema tables if they do not exist."""
        ControlBase.metadata.create_all(self.engine)

    def register_tenant(
        self,
        tenant_id: UUID,
        name: str,
        database_name: str,
        database_alias: str | None = None,
        status: TenantStatus = TenantStatus.PENDING,
        schema_version: str = "0004_coverage_precision",
    ) -> TenantRegistryRecord:
        """Register a new tenant in the control database."""
        alias = database_alias or database_name
        with Session(self.engine) as session:
            existing = session.get(TenantRegistryRecord, tenant_id)
            if existing:
                existing.name = name
                existing.database_name = database_name
                existing.database_alias = alias
                existing.status = status.value
                existing.schema_version = schema_version
                existing.error_message = None
                existing.updated_at = datetime.now(UTC)
                session.commit()
                session.refresh(existing)
                session.expunge(existing)
                return existing

            record = TenantRegistryRecord(
                tenant_id=tenant_id,
                name=name,
                database_name=database_name,
                database_alias=alias,
                status=status.value,
                schema_version=schema_version,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)
            return record

    def update_status(
        self,
        tenant_id: UUID,
        status: TenantStatus,
        error_message: str | None = None,
        schema_version: str | None = None,
        mark_verified: bool = False,
    ) -> TenantRegistryRecord:
        """Atomically update a tenant's lifecycle state in the control database."""
        with Session(self.engine) as session:
            record = session.get(TenantRegistryRecord, tenant_id)
            if not record:
                raise ValueError(f"Tenant {tenant_id} not found in control registry")
            record.status = status.value
            record.error_message = error_message
            if schema_version:
                record.schema_version = schema_version
            if mark_verified:
                record.last_verified_at = datetime.now(UTC)
            record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            session.expunge(record)
            return record

    def get_tenant(self, tenant_id: UUID) -> TenantRegistryRecord | None:
        """Fetch tenant record by UUID from control plane."""
        with Session(self.engine) as session:
            record = session.get(TenantRegistryRecord, tenant_id)
            if record:
                session.expunge(record)
            return record

    def get_tenant_by_database(self, database_name: str) -> TenantRegistryRecord | None:
        """Fetch tenant record by physical database name from control plane."""
        with Session(self.engine) as session:
            record = session.scalar(
                select(TenantRegistryRecord).where(
                    TenantRegistryRecord.database_name == database_name
                )
            )
            if record:
                session.expunge(record)
            return record

    def list_active_tenants(self) -> list[TenantRegistryRecord]:
        """List all active tenant records."""
        with Session(self.engine) as session:
            records = session.scalars(
                select(TenantRegistryRecord)
                .where(TenantRegistryRecord.status == TenantStatus.ACTIVE.value)
                .order_by(TenantRegistryRecord.created_at)
            ).all()
            for r in records:
                session.expunge(r)
            return list(records)

    def list_all_tenants(self) -> list[TenantRegistryRecord]:
        """List all tenant records regardless of state."""
        with Session(self.engine) as session:
            records = session.scalars(
                select(TenantRegistryRecord).order_by(TenantRegistryRecord.created_at)
            ).all()
            for r in records:
                session.expunge(r)
            return list(records)

    def get_checkpoint(self, tenant_id: UUID) -> TenantMigrationCheckpoint | None:
        """Retrieve migration checkpoint for a tenant."""
        with Session(self.engine) as session:
            cp = session.get(TenantMigrationCheckpoint, tenant_id)
            if cp:
                session.expunge(cp)
            return cp

    def save_checkpoint(
        self,
        tenant_id: UUID,
        phase: str,
        orders_copied: int = 0,
        outbox_copied: int = 0,
        rollups_copied: int = 0,
        last_order_id: UUID | None = None,
        last_order_created_at: datetime | None = None,
        status: str = "IN_PROGRESS",
        error_message: str | None = None,
        recovery_backup_path: str | None = None,
        completed: bool = False,
    ) -> TenantMigrationCheckpoint:
        """Create or update a durable migration checkpoint."""
        now = datetime.now(UTC)
        with Session(self.engine) as session:
            cp = session.get(TenantMigrationCheckpoint, tenant_id)
            if not cp:
                cp = TenantMigrationCheckpoint(
                    tenant_id=tenant_id,
                    phase=phase,
                    last_order_id=last_order_id,
                    last_order_created_at=last_order_created_at,
                    orders_copied=orders_copied,
                    outbox_copied=outbox_copied,
                    rollups_copied=rollups_copied,
                    status=status,
                    error_message=error_message,
                    recovery_backup_path=recovery_backup_path,
                    started_at=now,
                    updated_at=now,
                    completed_at=now if completed else None,
                )
                session.add(cp)
            else:
                cp.phase = phase
                if last_order_id is not None:
                    cp.last_order_id = last_order_id
                if last_order_created_at is not None:
                    cp.last_order_created_at = last_order_created_at
                cp.orders_copied = orders_copied
                cp.outbox_copied = outbox_copied
                cp.rollups_copied = rollups_copied
                cp.status = status
                if error_message is not None:
                    cp.error_message = error_message
                if recovery_backup_path is not None:
                    cp.recovery_backup_path = recovery_backup_path
                cp.updated_at = now
                if completed:
                    cp.completed_at = now

            session.commit()
            session.refresh(cp)
            session.expunge(cp)
            return cp

    def delete_checkpoint(self, tenant_id: UUID) -> None:
        """Remove migration checkpoint after cleanup."""
        with Session(self.engine) as session:
            cp = session.get(TenantMigrationCheckpoint, tenant_id)
            if cp:
                session.delete(cp)
                session.commit()
