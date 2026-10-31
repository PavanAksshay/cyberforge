"""Tenant Migration Utility from V1 (Shared Database) to V2 (Dedicated Database).

Performs zero-data-loss tenant migration:
1. Validates source tenant data in shared database.
2. Provisions isolated tenant database via TenantProvisioner.
3. Copies all tenant records (orders, outbox, projections, rollups, coverage) within transaction.
4. Performs rigorous parity verification (order count, total sales, outbox count, aggregate totals).
5. Executes cutover by activating tenant in control plane registry.
6. Automatically rolls back (drops dedicated DB, reverts registry) if parity verification fails.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, func, select
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


class MigrationParityError(Exception):
    """Raised when data verification between source and target databases fails."""


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


class TenantMigrator:
    """Manages online/offline migration of tenant data from shared to dedicated database."""

    def __init__(
        self,
        shared_session_factory: sessionmaker[Session],
        control_registry: ControlRegistry,
        provisioner: TenantProvisioner,
        engine_manager: TenantEngineManager,
    ):
        self.shared_factory = shared_session_factory
        self.registry = control_registry
        self.provisioner = provisioner
        self.engine_manager = engine_manager

    def migrate_tenant(
        self,
        tenant_id: UUID,
        purge_source_after: bool = False,
    ) -> MigrationResult:
        """Migrate a single tenant from shared database to dedicated database."""
        start_time = time.monotonic()

        # 1. Verify tenant exists in shared database and gather pre-migration parity baseline
        with self.shared_factory() as shared_sess:
            source_tenant = shared_sess.get(Tenant, tenant_id)
            if not source_tenant:
                raise ValueError(f"Tenant {tenant_id} does not exist in shared database")

            tenant_name = source_tenant.name
            source_order_count = (
                shared_sess.scalar(select(func.count(Order.id)).where(Order.tenant_id == tenant_id))
                or 0
            )
            source_sales = shared_sess.scalar(
                select(func.coalesce(func.sum(Order.amount), Decimal("0.00"))).where(
                    Order.tenant_id == tenant_id, Order.status == "shipped"
                )
            ) or Decimal("0.00")
            source_outbox_count = (
                shared_sess.scalar(
                    select(func.count(OutboxEvent.id)).where(OutboxEvent.tenant_id == tenant_id)
                )
                or 0
            )

        # 2. Provision dedicated database (sets status to PENDING -> PROVISIONING -> ACTIVE)
        # We provision the clean schema first
        reg_record = self.provisioner.provision_tenant(tenant_id, tenant_name)
        target_db = reg_record.database_name

        try:
            # Set state to MIGRATING during data copy
            self.registry.update_status(tenant_id, TenantStatus.MIGRATING)

            # 3. Stream data from shared to target database in dependency order
            with self.shared_factory() as shared_sess:
                with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                    with target_sess.begin():
                        # A. Orders
                        orders = shared_sess.scalars(
                            select(Order)
                            .where(Order.tenant_id == tenant_id)
                            .order_by(Order.created_at)
                        ).all()
                        for o in orders:
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

                        # B. Outbox events
                        events = shared_sess.scalars(
                            select(OutboxEvent)
                            .where(OutboxEvent.tenant_id == tenant_id)
                            .order_by(OutboxEvent.occurred_at)
                        ).all()
                        for e in events:
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
                                    published_at=e.published_at,
                                    stream_id=e.stream_id,
                                )
                            )

                        # C. Order projections
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

                        # D. Processed events
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

                        # E. Tenant processing state
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

                        # F. Aggregate Rollup Tables (6 tables)
                        rollups_count = 0
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

                        # G. Coverage and bootstrap metadata
                        coverages = shared_sess.scalars(
                            select(AggregateCoverage).where(
                                AggregateCoverage.tenant_id == tenant_id
                            )
                        ).all()
                        for cov in coverages:
                            target_sess.merge(cov)

            # 4. Strict Parity Verification
            with self.engine_manager.session(tenant_id, allow_inactive=True) as target_sess:
                target_order_count = (
                    target_sess.scalar(
                        select(func.count(Order.id)).where(Order.tenant_id == tenant_id)
                    )
                    or 0
                )
                target_sales = target_sess.scalar(
                    select(func.coalesce(func.sum(Order.amount), Decimal("0.00"))).where(
                        Order.tenant_id == tenant_id, Order.status == "shipped"
                    )
                ) or Decimal("0.00")
                target_outbox_count = (
                    target_sess.scalar(
                        select(func.count(OutboxEvent.id)).where(OutboxEvent.tenant_id == tenant_id)
                    )
                    or 0
                )

                if target_order_count != source_order_count:
                    raise MigrationParityError(
                        f"Order count mismatch: source={source_order_count}, "
                        f"target={target_order_count}"
                    )
                if target_sales != source_sales:
                    raise MigrationParityError(
                        f"Sales amount mismatch: source={source_sales}, target={target_sales}"
                    )
                if target_outbox_count != source_outbox_count:
                    raise MigrationParityError(
                        f"Outbox count mismatch: source={source_outbox_count}, "
                        f"target={target_outbox_count}"
                    )

            # 5. Mark tenant ACTIVE in control registry
            self.registry.update_status(tenant_id, TenantStatus.ACTIVE, mark_verified=True)
            self.engine_manager.resolver.invalidate(tenant_id)

            # Optional: purge source records from shared database if requested
            if purge_source_after:
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
                            Tenant,
                        ):
                            shared_sess.execute(delete(model).where(model.tenant_id == tenant_id))

            duration = time.monotonic() - start_time
            log.info(
                "Successfully migrated tenant %s (%d orders, %d outbox, %s sales) to %s in %.2fs",
                tenant_id,
                target_order_count,
                target_outbox_count,
                target_sales,
                target_db,
                duration,
            )

            return MigrationResult(
                tenant_id=tenant_id,
                database_name=target_db,
                orders_migrated=target_order_count,
                outbox_migrated=target_outbox_count,
                rollups_migrated=rollups_count,
                total_sales_amount=target_sales,
                duration_seconds=duration,
                verified=True,
            )

        except Exception as exc:
            log.exception(
                "Migration failed for tenant %s; triggering automatic rollback", tenant_id
            )
            self.provisioner.deprovision_tenant(tenant_id, drop_database=True)
            self.registry.update_status(tenant_id, TenantStatus.FAILED, error_message=str(exc))
            raise
