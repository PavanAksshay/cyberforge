"""Check durable readiness before releasing the repaired 100-tenant product."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url

from astraroll.config import get_settings
from astraroll.v2.reconcile import utc_engine


def release_readiness(control_url, state_path: Path):
    if not state_path.exists():
        return False, "Tenant analytics reconciliation has not been verified yet"
    state = json.loads(state_path.read_text())
    complete = sum(r["phase"] == "verified" for r in state["tenants"].values())
    if len(state["tenants"]) != 100 or complete != 100:
        return False, f"Tenant analytics reconciliation in progress: {complete}/100 verified"
    control = utc_engine(control_url)
    try:
        with control.connect() as c:
            actual = {
                str(t): d
                for t, d in c.execute(
                    text("""SELECT tenant_id,database_name
                FROM control_tenant_registry WHERE status='active' """)
                ).all()
            }
        if actual != {t: r["database"] for t, r in state["tenants"].items()}:
            return False, "Registry differs from the verified tenant manifest"
        for tid, record in state["tenants"].items():
            engine = utc_engine(make_url(control_url).set(database=record["database"]))
            try:
                with engine.connect() as c:
                    rows = (
                        c.execute(
                            text("""SELECT c.currency,c.status,c.start,c."end",c.verified_at,
                      c.source_order_count,j.status AS job_status,j.tenant_id AS job_tenant
                      FROM aggregate_coverage c JOIN bootstrap_jobs j ON j.id=c.job_id
                      WHERE c.tenant_id=:tid AND c.job_id=:job"""),
                            {"tid": UUID(tid), "job": UUID(record["job_id"])},
                        )
                        .mappings()
                        .all()
                    )
                expected = set(record["before"]["currencies"])
                if {r.currency for r in rows} != expected:
                    return False, f"Coverage currency mismatch for {tid}"
                for row in rows:
                    if (
                        row.status != "ready"
                        or row.job_status != "ready"
                        or row.job_tenant != UUID(tid)
                        or row.start != datetime.fromisoformat(state["start"])
                        or row.end != datetime.fromisoformat(state["end"])
                        or row.verified_at is None
                        or (datetime.now(UTC) - row.verified_at).total_seconds()
                        > get_settings().coverage_max_age_seconds
                    ):
                        return False, f"Coverage is incomplete or stale for {tid}"
                if sum(r.source_order_count for r in rows) != record["before"]["orders"]:
                    return False, f"Coverage count mismatch for {tid}"
            finally:
                engine.dispose()
        return True, "All 100 tenant databases have verified durable coverage"
    finally:
        control.dispose()
