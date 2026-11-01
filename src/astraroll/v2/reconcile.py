"""Resumable, source-preserving repair of existing database-per-tenant analytics.

Uses the existing bootstrap/reducer contract. Never creates/drops databases, changes
orders/outbox/processed events, or copies bulk files. Old derived rows remain archived.
"""

import argparse
import json
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import UUID

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from astraroll.aggregation.bootstrap import (
    activate_job,
    canonical_sql,
    stage_batch,
    start_job,
)
from astraroll.aggregation.models import TenantProcessingState
from astraroll.aggregation.release_models import BootstrapJob
from astraroll.config import get_settings
from astraroll.locking import tenant_lock
from astraroll.v2.subset_migration import check_resources

LEASE = 771471405024922


def utc_engine(url, pool_size=1):
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "application_name": "astraroll_derived_repair",
            "options": "-c timezone=UTC -c work_mem=4MB -c statement_timeout=180000",
        },
    )


def source_snapshot(session, tenant_id):
    """All order/outbox fields are hashed, in stable UUID order, under UTC."""
    args = {"tenant": tenant_id}
    orders = (
        session.execute(
            text("""SELECT count(*) AS orders,
      min(created_at) AS first_created_at,max(created_at) AS last_created_at,
      array_agg(DISTINCT currency ORDER BY currency) AS currencies,
      coalesce(sum(amount),0)::text AS face_amount,
      md5(string_agg(md5(to_jsonb(o)::text),'' ORDER BY id)) AS orders_hash
      FROM orders o WHERE tenant_id=:tenant"""),
            args,
        )
        .mappings()
        .one()
    )
    events = (
        session.execute(
            text("""SELECT count(*) AS outbox,
      md5(string_agg(md5(to_jsonb(e)::text),'' ORDER BY id)) AS outbox_hash
      FROM outbox_events e WHERE tenant_id=:tenant"""),
            args,
        )
        .mappings()
        .one()
    )
    tenants = session.scalar(text("SELECT count(*) FROM tenants WHERE id=:tenant"), args)
    if tenants != 1:
        raise ValueError("Physical tenant metadata does not match registry")
    return {**dict(orders), **dict(events)}


def save_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=lambda x: x.isoformat()), encoding="utf-8")
    tmp.replace(path)


def prepare(control, path):
    with control.connect() as c:
        rows = c.execute(
            text("""SELECT tenant_id,database_name FROM control_tenant_registry
           WHERE status='active' ORDER BY tenant_id""")
        ).all()
    if len(rows) != 100:
        raise ValueError(f"Expected exactly 100 active tenants, found {len(rows)}")
    if path.exists():
        state = json.loads(path.read_text())
        if {str(t): d for t, d in rows} != {t: r["database"] for t, r in state["tenants"].items()}:
            raise ValueError("Registry changed since repair manifest")
        return state
    state = {"version": 1, "started_at": datetime.now(UTC).isoformat(), "tenants": {}}
    earliest = latest = None
    for i, (tid, database) in enumerate(rows, 1):
        check_resources()
        if not database.startswith("v2bench_") or not database.endswith(tid.hex):
            raise ValueError("Refusing unexpected destination database")
        e = utc_engine(control.url.set(database=database))
        try:
            with sessionmaker(e)() as s, s.begin():
                s.execute(text("SET TRANSACTION READ ONLY"))
                snapshot = source_snapshot(s, tid)
                active = s.scalar(
                    select(BootstrapJob.id).where(
                        BootstrapJob.tenant_id == tid,
                        BootstrapJob.status.in_(["building", "catchup"]),
                    )
                )
                if active:
                    raise ValueError(f"Resume/review existing rebuild job {active} first")
            first, last = snapshot["first_created_at"], snapshot["last_created_at"]
            if first is not None:
                earliest = min(earliest, first) if earliest else first
                latest = max(latest, last) if latest else last
            state["tenants"][str(tid)] = {
                "database": database,
                "before": normalize(snapshot),
                "phase": "planned",
            }
        finally:
            e.dispose()
        if i % 10 == 0:
            print(f"Preflight {i}/100: source and outbox fingerprints recorded", flush=True)
    now = datetime.now(UTC)
    start = (earliest or now).replace(hour=0, minute=0, second=0, microsecond=0)
    end = max(latest or now, now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    if end - start > timedelta(days=366):
        raise ValueError("Data exceeds supported maximum analytics window")
    state.update(start=start.isoformat(), end=end.isoformat())
    save_state(path, state)
    return state


def normalize(snapshot):
    return json.loads(json.dumps(snapshot, default=lambda x: x.isoformat()))


def repair_tenant(factory, tid, record, start, end, checkpoint, batch_size=5000):
    """Called only for an explicitly authorized existing tenant, resumable by job ID."""
    check_resources()
    with factory() as s:
        before = source_snapshot(s, tid)
    if normalize(before) != normalize(record["before"]):
        raise ValueError("Source orders/outbox changed since manifest; refusing activation")
    job_id = UUID(record["job_id"]) if record.get("job_id") else None
    if job_id is None:
        job_id = start_job(
            factory, tid, record["before"]["currencies"] or ["USD", "EUR"], start, end
        )
        record.update(job_id=str(job_id), phase="building")
        checkpoint()
    started = time.perf_counter()
    while True:
        check_resources()
        copied = stage_batch(factory, job_id, batch_size)
        with factory() as s:
            job = s.get(BootstrapJob, job_id)
            record.update(copied=job.copied, phase=job.status)
        checkpoint()
        if not copied:
            break
    check_resources()
    activate_job(factory, job_id, batch_size=batch_size)
    with factory.begin() as s:
        tenant_lock(s, tid)
        after = source_snapshot(s, tid)
        if normalize(before) != normalize(after):
            raise ValueError("Source preservation check failed")
        mismatched = s.scalar(
            text(f"""SELECT EXISTS(SELECT 1 FROM orders o
          LEFT JOIN order_projections p ON p.tenant_id=o.tenant_id AND p.order_id=o.id
          WHERE o.tenant_id=:tenant AND (p.order_id IS NULL OR p.last_applied_version<>o.version
            OR p.canonical_snapshot IS DISTINCT FROM {canonical_sql()})) OR EXISTS(
          SELECT 1 FROM order_projections p LEFT JOIN orders o
            ON o.id=p.order_id AND o.tenant_id=p.tenant_id
          WHERE p.tenant_id=:tenant AND o.id IS NULL)"""),
            {"tenant": tid},
        )
        if mismatched:
            raise ValueError("Canonical projection validation failed")
        job = s.get(BootstrapJob, job_id)
        if job.status != "ready" or job.activated_at is None:
            raise ValueError("Bootstrap did not durably activate")
        processing = s.get(TenantProcessingState, tid)
        # A completed rebuild is a genuine materialization; retain the real stream ID.
        if (
            processing.last_successful_apply_at is None
            or processing.last_successful_apply_at < job.activated_at
        ):
            processing.last_successful_apply_at = job.activated_at
        archive_count = s.scalar(
            text("SELECT count(*) FROM rebuild_archive WHERE job_id=:job"), {"job": job_id}
        )
        revision = processing.revision
    record.update(
        phase="verified",
        verified_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=round(time.perf_counter() - started, 3),
        after=normalize(after),
        revision=revision,
        archived_rows=archive_count,
        checks=[
            "all order/outbox fields preserved",
            "canonical projections exact",
            "six rollups exact (bootstrap SQL EXCEPT ALL)",
            "durable coverage ready",
        ],
    )
    checkpoint()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".runtime/tenant-analytics-repair.json"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--workers", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("limit must be 1..100")
    settings = get_settings()
    url = settings.control_database_url or settings.database_url
    control = utc_engine(url, pool_size=2)
    if make_url(url).database != "v2bench_control_78b1ee4a":
        raise ValueError("Explicit benchmark control profile required")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with control.connect() as lease:
        if not lease.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LEASE}):
            raise ValueError("Another migration/rebuild owns the cluster lease")
        try:
            state = prepare(control, args.state)
            if args.prepare_only:
                return
            checkpoint_lock = Lock()

            def checkpoint():
                with checkpoint_lock:
                    save_state(args.state, state)

            def run_one(item):
                tid, record = item
                e = utc_engine(control.url.set(database=record["database"]))
                try:
                    repair_tenant(
                        sessionmaker(e, expire_on_commit=False),
                        UUID(tid),
                        record,
                        datetime.fromisoformat(state["start"]),
                        datetime.fromisoformat(state["end"]),
                        checkpoint,
                    )
                    complete = sum(r["phase"] == "verified" for r in state["tenants"].values())
                    print(
                        f"Verified {complete}/100: {record['database']} | "
                        f"{record['before']['orders']} orders | "
                        f"{record['elapsed_seconds']}s",
                        flush=True,
                    )
                except Exception as exc:
                    record["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    checkpoint()
                    raise
                finally:
                    e.dispose()

            todo = iter(
                [(tid, r) for tid, r in state["tenants"].items() if r["phase"] != "verified"][
                    : args.limit
                ]
            )
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                pending = set()
                for _ in range(args.workers):
                    item = next(todo, None)
                    if item is not None:
                        pending.add(pool.submit(run_one, item))
                try:
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            future.result()
                            item = next(todo, None)
                            if item is not None:
                                pending.add(pool.submit(run_one, item))
                except Exception:
                    for future in pending:
                        future.cancel()
                    raise

        finally:
            lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LEASE})
    control.dispose()


if __name__ == "__main__":
    main()
