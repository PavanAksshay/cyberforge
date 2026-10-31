"""Read-only release inspection of existing databases; no provision/reset/reseed."""

import argparse
import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from astraroll.aggregation.bootstrap import canonical_sql, validate_rollups
from astraroll.aggregation.coverage import read_coverage
from astraroll.analytics.contracts import AnalyticsQuery
from astraroll.analytics.coverage import require_coverage
from astraroll.benchmark import SQL
from astraroll.config import get_settings
from astraroll.v2.scale_validation import normalized, readonly_engine, resource_snapshot
from astraroll.workload import END, START


def audit(control_url, output):
    control = readonly_engine(control_url)
    result = {"phase": "inspecting", "targets": []}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with control.connect() as c:
            registry = (
                c.execute(
                    text(
                        "SELECT tenant_id,database_name,status,schema_version,"
                        "last_verified_at FROM control_tenant_registry ORDER BY tenant_id"
                    )
                )
                .mappings()
                .all()
            )
            if len(registry) != 100 or any(r["status"] != "active" for r in registry):
                raise ValueError("Expected exactly 100 ACTIVE registry mappings")
            names = [r["database_name"] for r in registry]
            if len(set(names)) != 100 or any(not n.startswith("v2bench_78b1ee4a_") for n in names):
                raise ValueError("Unexpected benchmark destination mapping")
            result["control_registry"] = [dict(r) for r in registry]
            result["control_checkpoint_rows"] = [
                dict(r)
                for r in c.execute(
                    text("SELECT * FROM control_tenant_migration_checkpoints")
                ).mappings()
            ]
            result["resources_before"] = resource_snapshot(c)
        for i, record in enumerate(registry):
            target = readonly_engine(make_url(control_url).set(database=record["database_name"]))
            try:
                with target.connect().execution_options(isolation_level="REPEATABLE READ") as c:
                    tenant = record["tenant_id"]
                    counts = c.execute(
                        text(
                            "SELECT (SELECT count(*) FROM orders),"
                            "(SELECT count(*) FROM order_projections),"
                            "(SELECT count(*) FROM tenants),"
                            "(SELECT count(*) FROM outbox_events),"
                            "pg_database_size(current_database())"
                        )
                    ).one()
                    if tuple(counts[:3]) != (10000, 10000, 1):
                        raise ValueError("Order/projection/tenant count mismatch")
                    if c.scalar(text("SELECT id FROM tenants")) != tenant:
                        raise ValueError("Registry/physical tenant identity mismatch")
                    revision = c.scalar(text("SELECT version_num FROM alembic_version"))
                    if revision != "0005_tenant_access_paths":
                        raise ValueError("Unexpected target Alembic revision")
                    cp = dict(
                        c.execute(text("SELECT * FROM subset_copy_checkpoint")).mappings().one()
                    )
                    if (
                        cp["copied"] != 10000
                        or cp["phase"] != "verified"
                        or cp["tenant_id"] != tenant
                    ):
                        raise ValueError("Unverified subset checkpoint")
                    mismatch = c.scalar(
                        text(f"""SELECT count(*) FROM orders o
                        FULL JOIN order_projections p ON p.tenant_id=o.tenant_id AND p.order_id=o.id
                        WHERE o.id IS NULL OR p.order_id IS NULL
                        OR p.last_applied_version<>o.version
                        OR (p.canonical_snapshot-'created_at'-'updated_at') IS DISTINCT FROM
                            ({canonical_sql()}-'created_at'-'updated_at')
                        OR (p.canonical_snapshot->>'created_at')::timestamptz
                            IS DISTINCT FROM o.created_at
                        OR (p.canonical_snapshot->>'updated_at')::timestamptz
                            IS DISTINCT FROM o.updated_at""")
                    )
                    if mismatch:
                        raise ValueError("Current canonical projections do not match raw orders")
                    with Session(bind=c) as session:
                        validate_rollups(session, tenant)
                        for currency in ("USD", "EUR"):
                            query = AnalyticsQuery(
                                tenant_id=tenant, currency=currency, start=START, end=END
                            )
                            proof = read_coverage(session, query)
                            for dimension in ("summary", "status", "product"):
                                require_coverage(proof, query, dimension)
                    pending = c.execute(
                        text("""SELECT count(*) AS total,
                        count(*) FILTER(WHERE p.last_applied_version>=e.order_version) AS covered
                        FROM outbox_events e LEFT JOIN order_projections p
                        ON p.tenant_id=e.tenant_id AND p.order_id=e.order_id
                        WHERE e.published_at IS NULL""")
                    ).one()
                    item = {
                        "database": record["database_name"],
                        "tenant_id": str(tenant),
                        "orders": counts[0],
                        "projections": counts[1],
                        "tenant_rows": counts[2],
                        "outbox_events": counts[3],
                        "database_bytes": counts[4],
                        "alembic_revision": revision,
                        "checkpoint": cp,
                        "pending_outbox": pending[0],
                        "pending_projection_covered": pending[1],
                        "rollup_parity": "six exact EXCEPT ALL oracles match",
                        "coverage": [
                            dict(r)
                            for r in c.execute(
                                text("SELECT * FROM aggregate_coverage ORDER BY currency")
                            ).mappings()
                        ],
                        "freshness": [
                            dict(r)
                            for r in c.execute(
                                text("SELECT * FROM tenant_processing_state")
                            ).mappings()
                        ],
                    }
                    if i in (0, 1, 99):
                        params = {"tenant": tenant, "currency": "USD", "start": START, "end": END}
                        item["samples"] = {
                            ep: [dict(r) for r in c.execute(text(sql), params).mappings()]
                            for ep, sql in SQL.items()
                        }
                        item["samples"]["hourly_trends"] = [
                            dict(r)
                            for r in c.execute(
                                text(
                                    SQL["trends"].replace("date_trunc('day'", "date_trunc('hour'")
                                ),
                                params,
                            ).mappings()
                        ]
                        item["samples"]["currency_totals"] = [
                            dict(r)
                            for r in c.execute(
                                text(
                                    "SELECT currency,count(*) AS orders,"
                                    "sum(amount) AS gross_face_amount,"
                                    "sum(CASE WHEN status='shipped' THEN amount ELSE 0 END) "
                                    "AS shipped_sales "
                                    "FROM orders GROUP BY currency ORDER BY currency"
                                )
                            ).mappings()
                        ]
                    if i == 0:
                        inspector = inspect(c)
                        item["schema_inventory"] = {
                            name: {
                                "columns": [
                                    {
                                        "name": col["name"],
                                        "type": str(col["type"]),
                                        "nullable": col["nullable"],
                                    }
                                    for col in inspector.get_columns(name)
                                ],
                                "indexes": inspector.get_indexes(name),
                            }
                            for name in inspector.get_table_names()
                        }
                    result["targets"].append(item)
                    if (i + 1) % 10 == 0:
                        print(f"Audited {i + 1}/100 existing target databases", flush=True)
            finally:
                target.dispose()
            output.write_text(json.dumps(result, default=normalized, indent=2))
        result.update(
            phase="verified",
            databases=100,
            orders=sum(t["orders"] for t in result["targets"]),
            projections=sum(t["projections"] for t in result["targets"]),
            target_bytes=sum(t["database_bytes"] for t in result["targets"]),
            pending_events=sum(t["pending_outbox"] for t in result["targets"]),
            pending_covered=sum(t["pending_projection_covered"] for t in result["targets"]),
        )
    finally:
        output.write_text(json.dumps(result, default=normalized, indent=2))
        control.dispose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.control_database_url:
        parser.error("Explicit CONTROL_DATABASE_URL required")
    audit(settings.control_database_url, args.output)


if __name__ == "__main__":
    main()
