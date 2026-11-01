"""Manifest-scoped offline migration; retain source and failed destinations forever.

Uses reviewed Pavan control registry/admin create primitives, never whole-tenant
migration or deprovision. Checkpoints commit with bounded order/history batches.
"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import UUID

import psutil
from alembic.config import Config
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from astraroll.aggregation.bootstrap import bootstrap
from astraroll.aggregation.release_models import BootstrapJob
from astraroll.config import get_settings
from astraroll.host_resources import system_commit_available
from astraroll.models import Order, OutboxEvent, Tenant
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry, TenantStatus
from astraroll.v2.scale_validation import (
    EVENT_COLUMNS,
    ORDER_COLUMNS,
    assert_snapshot,
    normalized,
    readonly_engine,
    resource_snapshot,
    snapshot,
    source_schema,
    validate_target,
)
from astraroll.workload import END, START


def writable_engine(url):
    return create_engine(
        url, pool_size=2, max_overflow=0, connect_args={"options": "-c timezone=UTC"}
    )


class MigrationResourcePause(RuntimeError):
    """Resource guard pause after durable source/history checkpoints."""


def check_resources():
    if psutil.virtual_memory().available < 768 * 1024**2:
        raise MigrationResourcePause("Migration paused: available RAM below 768 MiB")
    commit = system_commit_available()
    if commit is not None and commit < 1024**3:
        raise MigrationResourcePause("Migration paused: commit below 1 GiB")
    for drive, minimum in (
        (("C:/", 1024**3), ("D:/", 10 * 1024**3)) if os.name == "nt" else (("/", 10 * 1024**3),)
    ):
        if psutil.disk_usage(drive).free < minimum:
            raise MigrationResourcePause(f"Migration paused: insufficient disk on {drive}")


class SubsetMigration:
    def __init__(self, source_url, control_url, manifest):
        self.plan = json.loads(Path(manifest).read_text())
        if self.plan["target_orders"] != 1000000 or len(self.plan["tenants"]) != 100:
            raise ValueError("Locked manifest requires exactly 100 x 10000")
        if any(t["orders"] != 10000 for t in self.plan["tenants"]):
            raise ValueError("Manifest tenant count differs from 10000")
        self.fingerprint = hashlib.sha256(Path(manifest).read_bytes()).hexdigest()
        self.source = readonly_engine(source_url)
        self.control = writable_engine(control_url)
        self.registry = ControlRegistry(self.control)
        self.registry.init_schema()
        self.provisioner = TenantProvisioner(self.registry, control_url)
        self.control_url = make_url(control_url)

    def close(self):
        self.provisioner.dispose()
        self.source.dispose()
        self.control.dispose()

    def migrate(self, expected, batch_size=1000):
        check_resources()
        tenant = UUID(expected["tenant_id"])
        name = f"v2bench_{self.fingerprint[:8]}_{tenant.hex}"
        start = time.perf_counter()
        with self.control.connect() as lease:
            if not lease.scalar(text("SELECT pg_try_advisory_lock(771471405024922)")):
                raise RuntimeError("Another subset migration owns this cluster lease")
            try:
                return self._migrate(expected, tenant, name, batch_size, start)
            finally:
                lease.execute(text("SELECT pg_advisory_unlock(771471405024922)"))

    def _migrate(self, expected, tenant, name, batch_size, start):
        if not 1 <= batch_size <= 5000:
            raise ValueError("Batch size must be 1..5000")
        exists = self.provisioner._database_exists(name)
        record = self.registry.get_tenant(tenant)
        if exists and (record is None or record.database_name != name):
            raise ValueError("Existing destination has no matching ownership record")
        if record and record.database_name != name:
            raise ValueError("Tenant already belongs to another destination")
        source_schema(self.plan["source_schema"])
        schema = self.plan["source_schema"]
        target = writable_engine(self.control_url.set(database=name))
        try:
            with self.source.connect().execution_options(isolation_level="REPEATABLE READ") as src:
                assert_snapshot(
                    expected, snapshot(src, schema, tenant, 10000, UUID(expected["last_order_id"]))
                )
                tenant_row = dict(
                    src.execute(
                        text(f'SELECT id,name,created_at FROM "{schema}".tenants WHERE id=:id'),
                        {"id": tenant},
                    )
                    .mappings()
                    .one()
                )
                if not record:
                    self.registry.register_tenant(
                        tenant, tenant_row["name"], name, status=TenantStatus.PENDING
                    )
                if not exists:
                    self.provisioner._create_database(name)
                else:
                    # Refuse unrelated/pre-existing contents, even with a registry record.
                    with target.connect() as c:
                        if not c.scalar(
                            text("SELECT to_regclass('public.subset_copy_checkpoint')")
                        ):
                            raise ValueError("Existing target lacks manifest provenance marker")
                        marker = c.execute(
                            text("SELECT manifest_sha,tenant_id FROM subset_copy_checkpoint")
                        ).one()
                        if marker != (self.fingerprint, tenant):
                            raise ValueError("Target manifest provenance differs")
                self.registry.update_status(tenant, TenantStatus.MIGRATING)
                with target.begin() as c:
                    cfg = Config("alembic.ini")
                    cfg.attributes["connection"] = c
                    command.upgrade(cfg, "head")
                    c.execute(
                        text("""CREATE TABLE IF NOT EXISTS subset_copy_checkpoint (
                        singleton int PRIMARY KEY CHECK(singleton=1), manifest_sha text NOT NULL,
                        tenant_id uuid NOT NULL,cursor uuid,copied int NOT NULL DEFAULT 0,
                        phase text NOT NULL DEFAULT 'copying',job_id uuid)""")
                    )
                    c.execute(
                        text("""INSERT INTO subset_copy_checkpoint(singleton,manifest_sha,tenant_id)
                        VALUES(1,:sha,:tenant) ON CONFLICT(singleton) DO NOTHING"""),
                        {"sha": self.fingerprint, "tenant": tenant},
                    )
                    if not c.scalar(text("SELECT count(*) FROM tenants")):
                        c.execute(insert(Tenant), tenant_row)
                while True:
                    check_resources()
                    with target.begin() as dst:
                        cp = (
                            dst.execute(text("SELECT * FROM subset_copy_checkpoint FOR UPDATE"))
                            .mappings()
                            .one()
                        )
                        if cp["copied"] == 10000:
                            break
                        where = "tenant_id=:tenant AND id<=:cutoff"
                        params = {
                            "tenant": tenant,
                            "cutoff": UUID(expected["last_order_id"]),
                            "batch": batch_size,
                        }
                        if cp["cursor"]:
                            where += " AND id>:cursor"
                            params["cursor"] = cp["cursor"]
                        orders = [
                            dict(r)
                            for r in src.execute(
                                text(
                                    f'SELECT {",".join(ORDER_COLUMNS)} FROM "{schema}".orders '
                                    f"WHERE {where} ORDER BY id LIMIT :batch"
                                ),
                                params,
                            ).mappings()
                        ]
                        if not orders:
                            raise ValueError("Source cohort exhausted before expected count")
                        events = [
                            dict(r)
                            for r in src.execute(
                                text(
                                    f"SELECT {','.join(EVENT_COLUMNS)} "
                                    f'FROM "{schema}".outbox_events '
                                    "WHERE tenant_id=:tenant AND order_id=ANY(:ids) "
                                    "ORDER BY order_id,order_version"
                                ),
                                {"tenant": tenant, "ids": [r["id"] for r in orders]},
                            ).mappings()
                        ]
                        dst.execute(insert(Order), orders)
                        dst.execute(insert(OutboxEvent), events)
                        dst.execute(
                            text("""UPDATE subset_copy_checkpoint SET cursor=:cursor,
                            copied=copied+:n WHERE singleton=1"""),
                            {"cursor": orders[-1]["id"], "n": len(orders)},
                        )
                with target.connect() as c:
                    assert_snapshot(expected, snapshot(c, "public", tenant, 10000))
                factory = sessionmaker(target, expire_on_commit=False)
                with factory() as session:
                    job = session.query(BootstrapJob).filter_by(tenant_id=tenant).first()
                    job_id = job.id if job else None
                    ready = job is not None and job.status == "ready"
                if not ready:
                    bootstrap(
                        factory,
                        tenant,
                        sorted(expected["currencies"]),
                        START,
                        END,
                        batch_size=batch_size,
                        job_id=job_id,
                    )
                with target.connect().execution_options(isolation_level="REPEATABLE READ") as c:
                    verified = validate_target(c, expected)
                with target.begin() as c:
                    c.execute(text("UPDATE subset_copy_checkpoint SET phase='verified'"))
                self.registry.update_status(
                    tenant,
                    TenantStatus.ACTIVE,
                    schema_version="0005_tenant_access_paths",
                    mark_verified=True,
                )
                return {"database": name, "seconds": time.perf_counter() - start, **verified}
        except Exception as exc:
            if self.registry.get_tenant(tenant):
                self.registry.update_status(
                    tenant, TenantStatus.FAILED, error_message=type(exc).__name__
                )
            raise  # No cleanup/drop/purge, including parity errors.
        finally:
            target.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.control_database_url:
        parser.error("Use an explicitly provisioned dedicated CONTROL_DATABASE_URL")
    runner = SubsetMigration(settings.database_url, settings.control_database_url, args.manifest)
    if not 0 <= args.start_index < args.limit <= 100:
        parser.error("Limit must be 1..100; begin with 1 for the pilot")
    result = {
        "targets": [],
        "phase": "running",
        "new_historical_orders": 0,
        "start_index": args.start_index,
        "end_index": args.limit,
        "batch_size": args.batch_size,
        "resource_retries": 0,
    }
    with runner.control.connect() as conn:
        result["resources_before"] = resource_snapshot(conn)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for expected in runner.plan["tenants"][args.start_index : args.limit]:
            for attempt in range(3):
                try:
                    verified = runner.migrate(expected, batch_size=args.batch_size)
                    break
                except MigrationResourcePause:
                    if attempt == 2:
                        raise
                    result["resource_retries"] += 1
                    # Recycle only this runner's idle connections after transaction exit.
                    runner.source.dispose()
                    runner.control.dispose()
                    print("Resource pause: retained checkpoints; cooling down 5s", flush=True)
                    time.sleep(5)
            result["targets"].append(verified)
            output.write_text(json.dumps(result, default=normalized, indent=2))
            print(
                f"Verified {args.start_index + len(result['targets'])}/{args.limit}: "
                f"{verified['database']}",
                flush=True,
            )
        result["phase"] = "verified"
    except Exception as exc:
        result.update(phase="failed_retained", error_type=type(exc).__name__)
        raise
    finally:
        with runner.control.connect() as conn:
            result["resources_after"] = resource_snapshot(conn)
        output.write_text(json.dumps(result, default=normalized, indent=2))
        runner.close()


if __name__ == "__main__":
    main()
