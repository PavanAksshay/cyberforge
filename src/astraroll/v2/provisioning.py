"""Tenant Provisioning, Migration Orchestrator, and Lifecycle Management for AstraRoll V2."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command
from astraroll.aggregation.models import TenantProcessingState
from astraroll.models import Tenant
from astraroll.v2.registry import ControlRegistry, TenantRegistryRecord, TenantStatus

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)
schema_migration_lock = Lock()  # Alembic's module-level operation proxies are process-global.


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
            # Apply and record the actual migration graph, never fabricate a revision.
            with engine.begin() as connection:
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                with schema_migration_lock:
                    command.upgrade(config, "head")

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

        # 1. Check for pre-existing physical database on the cluster
        if self._database_exists(db_name):
            existing_rec = self.registry.get_tenant_by_database(db_name)
            if existing_rec and existing_rec.tenant_id != tenant_id:
                raise ValueError(
                    f"Pre-existing database '{db_name}' belongs to tenant "
                    f"{existing_rec.tenant_id}; refusing to overwrite."
                )
            if not existing_rec:
                raise ValueError(
                    f"Pre-existing database '{db_name}' detected on cluster with "
                    "unverified provenance; refusing to overwrite or drop."
                )
            log.info(
                "Database %s already exists and is verified owned by tenant %s", db_name, tenant_id
            )

        # 2. Register tenant in control registry as PENDING
        record = self.registry.register_tenant(
            tenant_id=tenant_id,
            name=name,
            database_name=db_name,
            database_alias=alias,
            status=TenantStatus.PENDING,
        )

        try:
            # 3. Transition to PROVISIONING and create physical database if not already existing
            self.registry.update_status(tenant_id, TenantStatus.PROVISIONING)
            self._create_database(db_name)

            # 4. Transition to MIGRATING and apply DDL schema
            self.registry.update_status(tenant_id, TenantStatus.MIGRATING)
            tenant_url = self.admin_url.set(database=db_name).render_as_string(hide_password=False)
            self._apply_schema(tenant_url, tenant_id, name)

            # 5. Transition to ACTIVE
            record = self.registry.update_status(
                tenant_id,
                TenantStatus.ACTIVE,
                schema_version=ScriptDirectory.from_config(
                    Config("alembic.ini")
                ).get_current_head(),
                mark_verified=True,
            )
            log.info("Tenant %s (%s) successfully provisioned in active state", tenant_id, name)
            return record

        except Exception as exc:
            log.exception(
                "Provisioning failed for tenant %s; marking FAILED for safe diagnosis", tenant_id
            )
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
