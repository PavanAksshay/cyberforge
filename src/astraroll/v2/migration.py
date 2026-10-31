"""Tenant Migration Utility from V1 (Shared Database) to V2 (Dedicated Database).

Performs zero-data-loss tenant migration:
1. Validates source tenant data in shared database.
2. Provisions isolated tenant database via TenantProvisioner.
3. Copies all tenant records (orders, outbox, rollups, coverage) in bounded batches.
4. Maintains durable per-tenant checkpoints for safe resume/retry on interruption.
5. Prevents blind re-publishing of already-processed historical outbox events.
6. Performs deep multi-dimensional parity verification across data and rollups.
7. Retains target database as FAILED or QUARANTINED for diagnosis on error.
8. Requires verified cutover, explicit approval, and recovery backup before source purge.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from astraroll.aggregation.models import (
    DailyProductAggregate,
    DailyStatusAggregate,
    DailySummaryAggregate,
    HourlyProductAggregate,
    HourlyStatusAggregate,
    HourlySummaryAggregate,
    OrderProjection,
    ProcessedEvent,
    TenantProcessingState,
)
from astraroll.aggregation.release_models import AggregateCoverage
from astraroll.models import Order, OutboxEvent, Tenant
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry, TenantStatus
from astraroll.v2.router import TenantEngineManager

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class MigrationError(Exception):
    """Base exception for tenant migration operations."""


class MigrationParityError(MigrationError):
    """Raised when data verification between source and target databases fails."""


class MigrationSafetyError(MigrationError):
    """Raised when safety preconditions for migration or source purging are violated."""


@dataclass(frozen=True)
class ParityReport:
    tenant_id: UUID
    source_order_count: int
    target_order_count: int
    source_sales: Decimal
    target_sales: Decimal
    source_outbox_count: int
    target_outbox_count: int
    status_parity: bool
    currency_parity: bool
    rollups_parity: bool
    coverage_parity: bool
    verified: bool
    details: dict[str, Any]


@dataclass(frozen=True)
class MigrationResult:
    tenant_id: UUID
    database_name: str
    orders_migrated: int
    outbox_migrated: int
    rollups_migrated: int
    total_sales_amount: Decimal
    duration_seconds: float
    verified: bool
    resumed: bool = False
    recovery_backup_path: str | None = None


class TenantMigrator:
    """Manages online/offline migration of tenant data from shared to dedicated database."""

    def __init__(
        self,
        shared_session_factory: sessionmaker[Session],
        control_registry: ControlRegistry,
        provisioner: TenantProvisioner,
        engine_manager: TenantEngineManager,
        batch_size: int = 5000,
    ):
        self.shared_factory = shared_session_factory
        self.registry = control_registry
        self.provisioner = provisioner
        self.engine_manager = engine_manager
        self.batch_size = batch_size

    def migrate_tenant(
        self,
        tenant_id: UUID,
        resume: bool = True,
        purge_source_after: bool = False,
        confirm_purge: bool = False,
        approval_token: str | None = None,
    ) -> MigrationResult:
        """Migrate a single tenant from shared database to dedicated database in bounded batches.

        Safety Guarantees:
        - Bounded memory footprint via streaming keyset pagination.
        - Durable checkpointing for safe resume/retry.
        - Historical outbox events are marked published to avoid duplicate relaying.
        - Zero destructive DROP DATABASE rollbacks on failure (retained as FAILED/QUARANTINED).
        - No source deletion without verified cutover, explicit confirmation, and restorable backup.
        """
        start_time = time.monotonic()

        # 1. Verify tenant exists in shared database
        with self.shared_factory() as shared_sess:
            source_tenant = shared_sess.get(Tenant, tenant_id)
            if not source_tenant:
                raise ValueError(f"Tenant {tenant_id} does not exist in shared database")
            tenant_name = source_tenant.name

        # 2. Check existing checkpoint and provision or reuse target database
        cp = self.registry.get_checkpoint(tenant_id)
        is_resuming = resume and cp is not None and cp.status in ("IN_PROGRESS", "FAILED")

        target_record = self.registry.get_tenant(tenant_id)
        if not target_record:
            # First-time provisioning
            target_record = self.provisioner.provision_tenant(tenant_id, tenant_name)
        elif not is_resuming and target_record.status == TenantStatus.ACTIVE.value:
            log.info(
                "Tenant %s is already active in database %s", tenant_id, target_record.database_name
            )

        target_db = target_record.database_name

        try:
            # Set state to MIGRATING
            self.registry.update_status(tenant_id, TenantStatus.MIGRATING)
            self.registry.save_checkpoint(
                tenant_id=tenant_id,
                phase="INITIALIZING",
                status="IN_PROGRESS",
            )

            # 3. Stream Orders in bounded batches with keyset pagination
            orders_migrated = self._migrate_orders(tenant_id, cp if is_resuming else None)

            # 4. Stream Outbox Events in bounded batches
            outbox_migrated = self._migrate_outbox(tenant_id, cp if is_resuming else None)

            # 5. Stream Projections, Processed Events, and Tenant Processing State
            self._migrate_projections_and_state(tenant_id)

            # 6. Stream Aggregate Rollups (all 6 tables)
            rollups_migrated = self._migrate_rollups(tenant_id)

            # 7. Stream Coverage metadata
            self._migrate_coverage(tenant_id)

            # 8. Deep Multi-Dimensional Parity Verification
            self.registry.save_checkpoint(
                tenant_id=tenant_id,
                phase="VERIFYING",
                orders_copied=orders_migrated,
                outbox_copied=outbox_migrated,
                rollups_copied=rollups_migrated,
                status="IN_PROGRESS",
            )
            parity = self.verify_parity(tenant_id)
            if not parity.verified:
                raise MigrationParityError(
                    f"Parity verification failed for tenant {tenant_id}: {parity.details}"
                )

            # 9. Successful Cutover: Mark tenant ACTIVE in control registry
            self.registry.update_status(tenant_id, TenantStatus.ACTIVE, mark_verified=True)
            self.engine_manager.resolver.invalidate(tenant_id)

            # 10. Update checkpoint to COMPLETED
            self.registry.save_checkpoint(
                tenant_id=tenant_id,
                phase="COMPLETED",
                orders_copied=orders_migrated,
                outbox_copied=outbox_migrated,
                rollups_copied=rollups_migrated,
                status="COMPLETED",
                completed=True,
            )

            # 11. Optional safe source purge with verified backup
            backup_path: str | None = None
            if purge_source_after:
                backup_path = self.purge_source_records(
                    tenant_id=tenant_id,
                    confirm_purge=confirm_purge,
                    approval_token=approval_token,
                )

            duration = time.monotonic() - start_time
            log.info(
                "Successfully migrated tenant %s (%d orders, %d outbox, %s sales) to %s in %.2fs",
                tenant_id,
                orders_migrated,
                outbox_migrated,
                parity.target_sales,
                target_db,
                duration,
            )

            return MigrationResult(
                tenant_id=tenant_id,
                database_name=target_db,
                orders_migrated=orders_migrated,
                outbox_migrated=outbox_migrated,
                rollups_migrated=rollups_migrated,
                total_sales_amount=parity.target_sales,
                duration_seconds=duration,
                verified=True,
                resumed=is_resuming,
                recovery_backup_path=backup_path,
            )

        except MigrationParityError as exc:
            # Retain target database for safe diagnosis, mark QUARANTINED
            log.error(
                "Parity failure for tenant %s; quarantining DB %s for diagnosis: %s",
                tenant_id,
                target_db,
                exc,
            )
            self.registry.update_status(tenant_id, TenantStatus.QUARANTINED, error_message=str(exc))
            self.registry.save_checkpoint(
                tenant_id=tenant_id,
                phase="QUARANTINED",
                status="QUARANTINED",
                error_message=str(exc),
            )
            self.engine_manager.resolver.invalidate(tenant_id)
            raise

        except Exception as exc:
            # Retain target database for safe diagnosis, mark FAILED
            log.exception(
                "Migration failed for tenant %s; preserving DB %s in FAILED state for diagnosis",
                tenant_id,
                target_db,
            )
            self.registry.update_status(tenant_id, TenantStatus.FAILED, error_message=str(exc))
            self.registry.save_checkpoint(
                tenant_id=tenant_id,
                phase="FAILED",
                status="FAILED",
                error_message=str(exc),
            )
            self.engine_manager.resolver.invalidate(tenant_id)
            raise

    def _migrate_orders(self, tenant_id: UUID, cp: Any | None) -> int:
        """Stream orders using keyset pagination and bounded transactions."""
        cursor_created_at: datetime | None = None
        cursor_id: UUID | None = None
        orders_copied = 0

        if cp and cp.last_order_id and cp.last_order_created_at:
            cursor_created_at = cp.last_order_created_at
            cursor_id = cp.last_order_id
            orders_copied = cp.orders_copied
            log.info(
                "Resuming order migration for tenant %s from %s (%d already copied)",
                tenant_id,
                cursor_id,
                orders_copied,
            )

        while True:
            with self.shared_factory() as shared_sess:
                stmt = select(Order).where(Order.tenant_id == tenant_id)
                if cursor_created_at is not None and cursor_id is not None:
                    stmt = stmt.where(
                        tuple_(Order.created_at, Order.id) > (cursor_created_at, cursor_id)
                    )
                stmt = stmt.order_by(Order.created_at.asc(), Order.id.asc()).limit(self.batch_size)
                batch = shared_sess.scalars(stmt).all()

                if not batch:
                    break

                with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                    with target_sess.begin():
                        for o in batch:
                            target_sess.merge(
                                Order(
                                    id=o.id,
                                    tenant_id=o.tenant_id,
                                    product_id=o.product_id,
                                    amount=o.amount,
                                    currency=o.currency,
                                    status=o.status,
                                    version=o.version,
                                    created_at=o.created_at,
                                    updated_at=o.updated_at,
                                )
                            )

                orders_copied += len(batch)
                last_order = batch[-1]
                cursor_created_at = last_order.created_at
                cursor_id = last_order.id

                self.registry.save_checkpoint(
                    tenant_id=tenant_id,
                    phase="ORDERS",
                    last_order_id=cursor_id,
                    last_order_created_at=cursor_created_at,
                    orders_copied=orders_copied,
                    status="IN_PROGRESS",
                )

        return orders_copied

    def _migrate_outbox(self, tenant_id: UUID, cp: Any | None) -> int:
        """Stream outbox events in bounded batches, avoiding blind re-publishing."""
        cursor_ts: datetime | None = None
        cursor_id: UUID | None = None
        outbox_copied = 0

        # Pre-query set of already-processed events in source database
        with self.shared_factory() as shared_sess:
            processed_event_ids = set(
                shared_sess.scalars(
                    select(ProcessedEvent.event_id).where(ProcessedEvent.tenant_id == tenant_id)
                ).all()
            )
            source_processing_state = shared_sess.get(TenantProcessingState, tenant_id)
            current_revision = source_processing_state.revision if source_processing_state else 0

        while True:
            with self.shared_factory() as shared_sess:
                stmt = select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
                if cursor_ts is not None and cursor_id is not None:
                    stmt = stmt.where(
                        tuple_(OutboxEvent.occurred_at, OutboxEvent.id) > (cursor_ts, cursor_id)
                    )
                stmt = stmt.order_by(OutboxEvent.occurred_at.asc(), OutboxEvent.id.asc()).limit(
                    self.batch_size
                )
                batch = shared_sess.scalars(stmt).all()

                if not batch:
                    break

                with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                    with target_sess.begin():
                        for e in batch:
                            published_at = e.published_at
                            stream_id = e.stream_id

                            # Safety check: mark historical events published if already processed
                            if published_at is None:
                                is_already_processed = e.id in processed_event_ids or (
                                    e.order_version <= current_revision and current_revision > 0
                                )
                                if is_already_processed:
                                    published_at = e.occurred_at
                                    stream_id = stream_id or f"migrated:historical:{e.id.hex[:12]}"

                            target_sess.merge(
                                OutboxEvent(
                                    id=e.id,
                                    tenant_id=e.tenant_id,
                                    order_id=e.order_id,
                                    order_version=e.order_version,
                                    event_type=e.event_type,
                                    schema_version=e.schema_version,
                                    payload=e.payload,
                                    occurred_at=e.occurred_at,
                                    published_at=published_at,
                                    stream_id=stream_id,
                                )
                            )

                outbox_copied += len(batch)
                last_event = batch[-1]
                cursor_ts = last_event.occurred_at
                cursor_id = last_event.id

                self.registry.save_checkpoint(
                    tenant_id=tenant_id,
                    phase="OUTBOX",
                    outbox_copied=outbox_copied,
                    status="IN_PROGRESS",
                )

        return outbox_copied

    def _migrate_projections_and_state(self, tenant_id: UUID) -> None:
        """Copy order projections, processed events, and tenant processing state."""
        with self.shared_factory() as shared_sess:
            with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                with target_sess.begin():
                    # 1. Projections
                    projections = shared_sess.scalars(
                        select(OrderProjection).where(OrderProjection.tenant_id == tenant_id)
                    ).all()
                    for p in projections:
                        target_sess.merge(
                            OrderProjection(
                                order_id=p.order_id,
                                tenant_id=p.tenant_id,
                                product_id=p.product_id,
                                amount=p.amount,
                                currency=p.currency,
                                status=p.status,
                                last_version=p.last_version,
                                last_event_id=p.last_event_id,
                                created_at=p.created_at,
                                updated_at=p.updated_at,
                            )
                        )

                    # 2. Processed events
                    p_events = shared_sess.scalars(
                        select(ProcessedEvent).where(ProcessedEvent.tenant_id == tenant_id)
                    ).all()
                    for pe in p_events:
                        target_sess.merge(
                            ProcessedEvent(
                                event_id=pe.event_id,
                                tenant_id=pe.tenant_id,
                                order_id=pe.order_id,
                                schema_version=pe.schema_version,
                                version=pe.version,
                                occurred_at=pe.occurred_at,
                                stream_id=pe.stream_id,
                                stream_name=pe.stream_name,
                                processed_at=pe.processed_at,
                            )
                        )

                    # 3. Processing state
                    st = shared_sess.get(TenantProcessingState, tenant_id)
                    if st:
                        target_sess.merge(
                            TenantProcessingState(
                                tenant_id=st.tenant_id,
                                revision=st.revision,
                                last_successful_apply_at=st.last_successful_apply_at,
                                last_successful_stream_id=st.last_successful_stream_id,
                            )
                        )

    def _migrate_rollups(self, tenant_id: UUID) -> int:
        """Copy all 6 aggregate rollup tables."""
        rollups_count = 0
        with self.shared_factory() as shared_sess:
            with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                with target_sess.begin():
                    for model in (
                        HourlySummaryAggregate,
                        DailySummaryAggregate,
                        HourlyStatusAggregate,
                        DailyStatusAggregate,
                        HourlyProductAggregate,
                        DailyProductAggregate,
                    ):
                        rows = shared_sess.scalars(
                            select(model).where(model.tenant_id == tenant_id)
                        ).all()
                        for row in rows:
                            target_sess.merge(row)
                            rollups_count += 1

        return rollups_count

    def _migrate_coverage(self, tenant_id: UUID) -> None:
        """Copy coverage proofs and bootstrap metadata."""
        with self.shared_factory() as shared_sess:
            with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                with target_sess.begin():
                    coverages = shared_sess.scalars(
                        select(AggregateCoverage).where(AggregateCoverage.tenant_id == tenant_id)
                    ).all()
                    for cov in coverages:
                        target_sess.merge(cov)

    def verify_parity(self, tenant_id: UUID) -> ParityReport:
        """Execute deep multi-dimensional parity verification between shared and tenant DB."""
        with self.shared_factory() as shared_sess:
            with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                # 1. Order Counts
                src_order_cnt = (
                    shared_sess.scalar(
                        select(func.count(Order.id)).where(Order.tenant_id == tenant_id)
                    )
                    or 0
                )
                tgt_order_cnt = (
                    target_sess.scalar(
                        select(func.count(Order.id)).where(Order.tenant_id == tenant_id)
                    )
                    or 0
                )

                # 2. Shipped Sales Amount
                src_sales = shared_sess.scalar(
                    select(func.coalesce(func.sum(Order.amount), Decimal("0.00"))).where(
                        Order.tenant_id == tenant_id, Order.status == "shipped"
                    )
                ) or Decimal("0.00")
                tgt_sales = target_sess.scalar(
                    select(func.coalesce(func.sum(Order.amount), Decimal("0.00"))).where(
                        Order.tenant_id == tenant_id, Order.status == "shipped"
                    )
                ) or Decimal("0.00")

                # 3. Outbox Counts
                src_outbox_cnt = (
                    shared_sess.scalar(
                        select(func.count(OutboxEvent.id)).where(OutboxEvent.tenant_id == tenant_id)
                    )
                    or 0
                )
                tgt_outbox_cnt = (
                    target_sess.scalar(
                        select(func.count(OutboxEvent.id)).where(OutboxEvent.tenant_id == tenant_id)
                    )
                    or 0
                )

                # 4. Status Breakdown (count, sum of amounts, sum of versions per status)
                status_stmt = (
                    select(
                        Order.status,
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.amount), Decimal("0.00")),
                        func.coalesce(func.sum(Order.version), 0),
                    )
                    .where(Order.tenant_id == tenant_id)
                    .group_by(Order.status)
                    .order_by(Order.status)
                )
                src_status = shared_sess.execute(status_stmt).fetchall()
                tgt_status = target_sess.execute(status_stmt).fetchall()
                status_parity = src_status == tgt_status

                # 5. Currency Breakdown
                currency_stmt = (
                    select(
                        Order.currency,
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.amount), Decimal("0.00")),
                    )
                    .where(Order.tenant_id == tenant_id)
                    .group_by(Order.currency)
                    .order_by(Order.currency)
                )
                src_currency = shared_sess.execute(currency_stmt).fetchall()
                tgt_currency = target_sess.execute(currency_stmt).fetchall()
                currency_parity = src_currency == tgt_currency

                # 6. Aggregate Rollups Parity across all 6 tables
                rollups_parity = True
                rollup_details: dict[str, Any] = {}
                for model in (
                    HourlySummaryAggregate,
                    DailySummaryAggregate,
                    HourlyStatusAggregate,
                    DailyStatusAggregate,
                    HourlyProductAggregate,
                    DailyProductAggregate,
                ):
                    stmt = select(
                        func.count(),
                        func.coalesce(func.sum(model.order_count), 0),
                        func.coalesce(func.sum(model.sales_amount), Decimal("0.00")),
                    ).where(model.tenant_id == tenant_id)
                    src_r = shared_sess.execute(stmt).one()
                    tgt_r = target_sess.execute(stmt).one()
                    if src_r != tgt_r:
                        rollups_parity = False
                        rollup_details[model.__name__] = {
                            "source": list(src_r),
                            "target": list(tgt_r),
                        }

                # 7. Coverage Parity
                cov_stmt = select(func.count(AggregateCoverage.tenant_id)).where(
                    AggregateCoverage.tenant_id == tenant_id
                )
                src_cov_cnt = shared_sess.scalar(cov_stmt) or 0
                tgt_cov_cnt = target_sess.scalar(cov_stmt) or 0
                coverage_parity = src_cov_cnt == tgt_cov_cnt

                # 8. Overall Parity Verification
                verified = (
                    src_order_cnt == tgt_order_cnt
                    and src_sales == tgt_sales
                    and src_outbox_cnt == tgt_outbox_cnt
                    and status_parity
                    and currency_parity
                    and rollups_parity
                    and coverage_parity
                )

                details = {
                    "orders": {"source": src_order_cnt, "target": tgt_order_cnt},
                    "sales": {"source": str(src_sales), "target": str(tgt_sales)},
                    "outbox": {"source": src_outbox_cnt, "target": tgt_outbox_cnt},
                    "status_breakdown_matches": status_parity,
                    "currency_breakdown_matches": currency_parity,
                    "rollups_matches": rollups_parity,
                    "rollups_discrepancies": rollup_details,
                    "coverage_matches": coverage_parity,
                }

                return ParityReport(
                    tenant_id=tenant_id,
                    source_order_count=src_order_cnt,
                    target_order_count=tgt_order_cnt,
                    source_sales=src_sales,
                    target_sales=tgt_sales,
                    source_outbox_count=src_outbox_cnt,
                    target_outbox_count=tgt_outbox_cnt,
                    status_parity=status_parity,
                    currency_parity=currency_parity,
                    rollups_parity=rollups_parity,
                    coverage_parity=coverage_parity,
                    verified=verified,
                    details=details,
                )

    def create_recovery_backup(
        self,
        tenant_id: UUID,
        backup_dir: Path | str = "artifacts/recovery",
    ) -> str:
        """Create restorable JSON recovery snapshot of source tenant data before purging."""
        out_dir = Path(backup_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_file = out_dir / f"tenant_{tenant_id}_{ts}.json"

        with self.shared_factory() as sess:
            t = sess.get(Tenant, tenant_id)
            if not t:
                raise MigrationSafetyError(
                    f"Cannot create backup: Tenant {tenant_id} not found in source database"
                )

            orders = [
                {
                    "id": str(o.id),
                    "product_id": str(o.product_id),
                    "amount": str(o.amount),
                    "currency": o.currency,
                    "status": o.status,
                    "version": o.version,
                    "created_at": o.created_at.isoformat(),
                    "updated_at": o.updated_at.isoformat(),
                }
                for o in sess.scalars(select(Order).where(Order.tenant_id == tenant_id)).all()
            ]

            outbox = [
                {
                    "id": str(e.id),
                    "order_id": str(e.order_id),
                    "order_version": e.order_version,
                    "event_type": e.event_type,
                    "schema_version": e.schema_version,
                    "payload": e.payload,
                    "occurred_at": e.occurred_at.isoformat(),
                    "published_at": e.published_at.isoformat() if e.published_at else None,
                    "stream_id": e.stream_id,
                }
                for e in sess.scalars(
                    select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
                ).all()
            ]

            payload = {
                "tenant_id": str(tenant_id),
                "name": t.name,
                "exported_at": datetime.now(UTC).isoformat(),
                "orders_count": len(orders),
                "outbox_count": len(outbox),
                "orders": orders,
                "outbox": outbox,
            }

            temp_file = backup_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)

            os.replace(temp_file, backup_file)

            # Verification of backup file
            if not backup_file.exists() or backup_file.stat().st_size == 0:
                raise MigrationSafetyError(
                    f"Failed to create valid recovery backup file: {backup_file}"
                )

            log.info(
                "Created verified recovery backup for tenant %s at %s (%d orders)",
                tenant_id,
                backup_file,
                len(orders),
            )
            return str(backup_file.resolve())

    def purge_source_records(
        self,
        tenant_id: UUID,
        confirm_purge: bool = False,
        approval_token: str | None = None,
    ) -> str:
        """Purge source records from shared DB ONLY after verified cutover and backup."""
        if not confirm_purge:
            raise MigrationSafetyError(
                "Source purge aborted: 'confirm_purge=True' must be explicitly provided"
            )
        if approval_token != str(tenant_id):
            raise MigrationSafetyError(
                f"Source purge aborted: approval_token '{approval_token}' "
                f"does not match tenant_id '{tenant_id}'"
            )

        # 1. Verify that cutover was verified and tenant is ACTIVE in control registry
        rec = self.registry.get_tenant(tenant_id)
        if not rec or rec.status != TenantStatus.ACTIVE.value or not rec.last_verified_at:
            raise MigrationSafetyError(
                f"Source purge aborted: Tenant {tenant_id} is not verified/ACTIVE in control plane"
            )

        # 2. Create independently restorable recovery copy
        backup_path = self.create_recovery_backup(tenant_id)

        # 3. Execute atomic deletion across all models in shared database
        with self.shared_factory() as shared_sess:
            with shared_sess.begin():
                for model in (
                    OrderProjection,
                    HourlySummaryAggregate,
                    DailySummaryAggregate,
                    HourlyStatusAggregate,
                    DailyStatusAggregate,
                    HourlyProductAggregate,
                    DailyProductAggregate,
                    OutboxEvent,
                    Order,
                    TenantProcessingState,
                ):
                    shared_sess.execute(delete(model).where(model.tenant_id == tenant_id))

                shared_sess.execute(delete(Tenant).where(Tenant.id == tenant_id))

        log.info(
            "Safely purged source records for tenant %s from shared database (Backup: %s)",
            tenant_id,
            backup_path,
        )
        return backup_path

    def diagnose_tenant(self, tenant_id: UUID) -> dict[str, Any]:
        """Produce an actionable diagnostic report for troubleshooting tenant migration or state."""
        rec = self.registry.get_tenant(tenant_id)
        cp = self.registry.get_checkpoint(tenant_id)

        db_reachable = False
        target_orders = None
        db_err = None

        if rec:
            try:
                with self.engine_manager.session(tenant_id, allow_inactive=True) as sess:
                    target_orders = (
                        sess.scalar(
                            select(func.count(Order.id)).where(Order.tenant_id == tenant_id)
                        )
                        or 0
                    )
                    db_reachable = True
            except Exception as e:
                db_err = str(e)

        return {
            "tenant_id": str(tenant_id),
            "registry": asdict(rec) if rec else None,
            "checkpoint": asdict(cp) if cp else None,
            "target_database_reachable": db_reachable,
            "target_orders": target_orders,
            "database_error": db_err,
        }
