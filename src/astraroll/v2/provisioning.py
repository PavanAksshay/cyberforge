"""Tenant Provisioning, Migration Orchestrator, and Lifecycle Management for AstraRoll V2."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from astraroll.aggregation.models import TenantProcessingState
from astraroll.db import Base
from astraroll.models import Tenant
from astraroll.v2.registry import ControlRegistry, TenantRegistryRecord, TenantStatus

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def make_tenant_db_name(tenant_id: UUID, prefix: str = "astraroll_t_") -> str:
    """Generate a valid, deterministic PostgreSQL database name from tenant UUID."""
    compact = tenant_id.hex
    return f"{prefix}{compact}"


class TenantProvisioner:
    """Orchestrates atomic, idempotent provisioning and migration of dedicated tenant databases."""

    def __init__(
        self,
        registry: ControlRegistry,
        admin_database_url: str,
        tenant_database_prefix: str = "astraroll_t_",
    ):
        self.registry = registry
        self.admin_url = make_url(admin_database_url)
        self.tenant_database_prefix = tenant_database_prefix
        # Administrative connection to manage database creation/deletion
        admin_connect_url = self.admin_url.set(database="postgres").render_as_string(
            hide_password=False
        )
        self.admin_engine = create_engine(
            admin_connect_url,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
            connect_args={"options": "-c timezone=UTC"},
        )

    def _database_exists(self, db_name: str) -> bool:
        """Check if a database exists on the PostgreSQL cluster."""
        with self.admin_engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
                {"dbname": db_name},
            )
            return result.scalar() is not None

    def _create_database(self, db_name: str) -> None:
        """Create a dedicated PostgreSQL database idempotently."""
        if not self._database_exists(db_name):
            with self.admin_engine.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                log.info("Created PostgreSQL database %s", db_name)

    def _drop_database(self, db_name: str) -> None:
        """Forcefully terminate connections and drop the database."""
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            log.info("Dropped PostgreSQL database %s", db_name)

    def _apply_schema(self, tenant_url: str, tenant_id: UUID, name: str) -> None:
        """Apply all tables, indexes and initial state to the tenant database."""
        engine = create_engine(
            tenant_url,
            pool_pre_ping=True,
            connect_args={"options": "-c timezone=UTC"},
        )
        try:
            # Create all domain and aggregate tables from current metadata (up to 0004)
            Base.metadata.create_all(engine)

            # Seed initial tenant metadata and processing state in the tenant's own DB
            with Session(engine) as session:
                existing_tenant = session.get(Tenant, tenant_id)
                if not existing_tenant:
                    session.add(Tenant(id=tenant_id, name=name))

                existing_state = session.get(TenantProcessingState, tenant_id)
                if not existing_state:
                    session.add(
                        TenantProcessingState(
                            tenant_id=tenant_id,
                            revision=0,
                            last_successful_apply_at=datetime.now(UTC),
                        )
                    )
                session.commit()
        finally:
            engine.dispose()

    def provision_tenant(
        self,
        tenant_id: UUID,
        name: str,
        database_alias: str | None = None,
    ) -> TenantRegistryRecord:
        """Provision a dedicated tenant database with atomic state transitions and rollback.

        State Transitions:
          PENDING -> PROVISIONING -> MIGRATING -> ACTIVE
          (Rollback on failure: Drops database and transitions to FAILED)
        """
        db_name = make_tenant_db_name(tenant_id, self.tenant_database_prefix)
        alias = database_alias or db_name

        # 1. Register tenant in control registry as PENDING
        record = self.registry.register_tenant(
            tenant_id=tenant_id,
            name=name,
            database_name=db_name,
            database_alias=alias,
            status=TenantStatus.PENDING,
        )

        db_created = False
        try:
            # 2. Transition to PROVISIONING and create physical database
            self.registry.update_status(tenant_id, TenantStatus.PROVISIONING)
            self._create_database(db_name)
            db_created = True

            # 3. Transition to MIGRATING and apply DDL schema
            self.registry.update_status(tenant_id, TenantStatus.MIGRATING)
            tenant_url = self.admin_url.set(database=db_name).render_as_string(hide_password=False)
            self._apply_schema(tenant_url, tenant_id, name)

            # 4. Transition to ACTIVE
            record = self.registry.update_status(
                tenant_id,
                TenantStatus.ACTIVE,
                schema_version="0004_coverage_precision",
                mark_verified=True,
            )
            log.info("Tenant %s (%s) successfully provisioned in active state", tenant_id, name)
            return record

        except Exception as exc:
            log.exception("Provisioning failed for tenant %s; rolling back", tenant_id)
            if db_created:
                try:
                    self._drop_database(db_name)
                except Exception as drop_err:
                    log.error("Failed to drop database %s during rollback: %s", db_name, drop_err)
            self.registry.update_status(
                tenant_id,
                TenantStatus.FAILED,
                error_message=str(exc),
            )
            raise

    def deprovision_tenant(self, tenant_id: UUID, drop_database: bool = False) -> None:
        """Decommission a tenant and optionally drop its database."""
        record = self.registry.get_tenant(tenant_id)
        if not record:
            return

        self.registry.update_status(tenant_id, TenantStatus.DECOMMISSIONED)
        if drop_database:
            self._drop_database(record.database_name)
        log.info("Tenant %s decommissioned (drop_db=%s)", tenant_id, drop_database)

    def provision_tenants_concurrently(
        self,
        tenants: list[tuple[UUID, str]],
        max_workers: int = 5,
    ) -> list[TenantRegistryRecord]:
        """Concurrently onboard multiple tenants with bounded worker parallelism."""
        results: list[TenantRegistryRecord] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_tenant = {
                executor.submit(self.provision_tenant, t_id, name): (t_id, name)
                for t_id, name in tenants
            }
            for future in as_completed(future_to_tenant):
                t_id, name = future_to_tenant[future]
                try:
                    record = future.result()
                    results.append(record)
                except Exception as exc:
                    log.error("Failed to provision tenant %s (%s): %s", t_id, name, exc)
                    raise
        return results

    def dispose(self) -> None:
        """Dispose admin engine."""
        self.admin_engine.dispose()
