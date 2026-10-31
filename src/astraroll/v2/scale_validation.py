"""Read-only V2 scale planning/verification. Never provisions or drops a database.

Migration is deliberately external: consume only Pavan's reviewed hardened tool.
The manifest records a deterministic subset, not a claim that all V1 data migrated.
"""

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import psutil
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from astraroll.aggregation.bootstrap import canonical_sql, validate_rollups
from astraroll.aggregation.coverage import read_coverage
from astraroll.analytics.contracts import (
    METRIC_SEMANTICS,
    AnalyticsQuery,
    StatusBreakdownResponse,
    SummaryResponse,
    TopProductsResponse,
    TrendsResponse,
)
from astraroll.analytics.coverage import require_coverage
from astraroll.benchmark import ENDPOINTS, SQL, canonical, percentiles
from astraroll.config import get_settings
from astraroll.host_resources import system_commit_available
from astraroll.workload import END, START

BATCH_SIZE = 1000
ORDER_COLUMNS = (
    "id",
    "tenant_id",
    "product_id",
    "amount",
    "currency",
    "status",
    "version",
    "created_at",
    "updated_at",
)
EVENT_COLUMNS = (
    "id",
    "tenant_id",
    "order_id",
    "order_version",
    "event_type",
    "schema_version",
    "payload",
    "occurred_at",
    "published_at",
    "stream_id",
)


def normalized(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(type(value).__name__)


def digest_row(digest, row):
    digest.update(json.dumps(dict(row), sort_keys=True, default=normalized).encode())
    digest.update(b"\n")


def readonly_engine(url):
    url = make_url(url)
    options = url.query.get("options", "")
    return create_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"options": f"{options} -c timezone=UTC -c default_transaction_read_only=on"},
    )


def source_schema(value):
    if not re.fullmatch(r"bench_[0-9a-f]{32}", value):
        raise ValueError("Source must be an explicit isolated bench_<32 hex> schema")
    return value


def snapshot(conn, schema, tenant, limit, cutoff=None):
    """Bounded keyset reads and full-field digests, including complete event history."""
    if schema != "public":
        source_schema(schema)
    if not 1 <= limit <= 100000:
        raise ValueError("Selection size must be 1..100000")
    table = f'"{schema}".orders'
    event_table = f'"{schema}".outbox_events'
    digest, events_digest = hashlib.sha256(), hashlib.sha256()
    cursor, count, event_count = None, 0, 0
    by_currency = defaultdict(lambda: {"count": 0, "shipped_sales": Decimal("0.00")})
    statuses = Counter()
    while count < limit:
        conditions = "tenant_id=:tenant"
        params = {"tenant": tenant, "batch": min(BATCH_SIZE, limit - count)}
        if cursor is not None:
            conditions += " AND id>:cursor"
            params["cursor"] = cursor
        if cutoff is not None:
            conditions += " AND id<=:cutoff"
            params["cutoff"] = cutoff
        rows = (
            conn.execute(
                text(
                    f"SELECT {','.join(ORDER_COLUMNS)} FROM {table} WHERE {conditions} "
                    "ORDER BY id LIMIT :batch"
                ),
                params,
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        ids = [row["id"] for row in rows]
        for row in rows:
            digest_row(digest, row)
            statuses[row["status"]] += 1
            metric = by_currency[row["currency"]]
            metric["count"] += 1
            if row["status"] == "shipped":
                metric["shipped_sales"] += row["amount"]
        events = (
            conn.execute(
                text(
                    f"SELECT {','.join(EVENT_COLUMNS)} FROM {event_table} "
                    "WHERE tenant_id=:tenant AND order_id=ANY(:ids) ORDER BY order_id,order_version"
                ),
                {"tenant": tenant, "ids": ids},
            )
            .mappings()
            .all()
        )
        versions = Counter(row["order_id"] for row in events)
        last_version = {}
        for item in events:
            last_version[item["order_id"]] = item["order_version"]
        if any(
            versions[row["id"]] != row["version"] or last_version.get(row["id"]) != row["version"]
            for row in rows
        ):
            raise ValueError("Selected order has missing/extra event history")
        for row in events:
            digest_row(events_digest, row)
        cursor = ids[-1]
        count += len(rows)
        event_count += len(events)
    if count != limit:
        raise ValueError(f"Insufficient selected orders: expected {limit}, got {count}")
    return {
        "tenant_id": str(tenant),
        "orders": count,
        "outbox_events": event_count,
        "last_order_id": str(cursor),
        "orders_sha256": digest.hexdigest(),
        "outbox_sha256": events_digest.hexdigest(),
        "statuses": dict(statuses),
        "currencies": {
            k: {**v, "shipped_sales": str(v["shipped_sales"])} for k, v in by_currency.items()
        },
    }


def resource_snapshot(conn):
    """Whole-cluster counters, explicitly not per-tenant attribution."""
    return {
        "at": datetime.now(UTC).isoformat(),
        "physical_available_bytes": psutil.virtual_memory().available,
        "system_commit_available_bytes": system_commit_available(),
        "host_cpu_percent": psutil.cpu_percent(),
        "drives": {
            drive: psutil.disk_usage(drive)._asdict()
            for drive in (("C:/", "D:/") if os.name == "nt" else ("/",))
        },
        "connections": [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT datname,state,count(*) AS connections "
                    "FROM pg_stat_activity GROUP BY 1,2"
                )
            ).mappings()
        ],
        "settings": dict(
            conn.execute(
                text(
                    "SELECT name,setting FROM pg_settings WHERE name IN "
                    "('max_connections','superuser_reserved_connections','max_wal_size',"
                    "'checkpoint_timeout','wal_compression','fsync','full_page_writes',"
                    "'synchronous_commit','shared_buffers')"
                )
            ).all()
        ),
        "wal": dict(conn.execute(text("SELECT * FROM pg_stat_wal")).mappings().one()),
        "wal_directory_bytes": conn.scalar(text("SELECT sum(size) FROM pg_ls_waldir()")),
    }


def create_plan(url, schema, tenants=100, per_tenant=10000):
    source_schema(schema)
    engine = readonly_engine(url)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                distribution = conn.execute(
                    text(
                        f'SELECT tenant_id,count(*) FROM "{schema}".orders '
                        "GROUP BY tenant_id ORDER BY tenant_id"
                    )
                ).all()
                if len(distribution) != tenants:
                    raise ValueError(
                        f"Expected {tenants} source tenants, found {len(distribution)}"
                    )
                if any(count < per_tenant for _, count in distribution):
                    raise ValueError(
                        "Source tenant shortfall; generation requires separate provenance"
                    )
                return {
                    "format_version": 1,
                    "metric_semantics": METRIC_SEMANTICS,
                    "source_schema": schema,
                    "source_orders": sum(n for _, n in distribution),
                    "source_distribution": {str(t): n for t, n in distribution},
                    "selection": "first order UUIDs ASC per tenant; complete matching histories",
                    "snapshot_at": conn.scalar(text("SELECT transaction_timestamp()")),
                    "snapshot_id": conn.scalar(text("SELECT pg_current_snapshot()::text")),
                    "snapshot_note": "Manifest-only MVCC snapshot; not reusable after exit",
                    "new_historical_orders": 0,
                    "target_orders": tenants * per_tenant,
                    "resources": resource_snapshot(conn),
                    "tenants": [
                        snapshot(conn, schema, tenant, per_tenant) for tenant, _ in distribution
                    ],
                }
    finally:
        engine.dispose()


def assert_snapshot(expected, actual):
    for key in (
        "tenant_id",
        "orders",
        "outbox_events",
        "last_order_id",
        "orders_sha256",
        "outbox_sha256",
        "statuses",
        "currencies",
    ):
        if expected[key] != actual[key]:
            raise ValueError(f"Snapshot parity mismatch: {key}")


def validate_target(conn, expected, schema="public"):
    tenant = UUID(expected["tenant_id"])
    counts = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM tenants),"
            "(SELECT count(*) FROM outbox_events)"
        )
    ).one()
    if tuple(counts) != (expected["orders"], 1, expected["outbox_events"]):
        raise ValueError("Dedicated database cardinality/tenant isolation mismatch")
    actual = snapshot(conn, schema, tenant, expected["orders"])
    assert_snapshot(expected, actual)
    with Session(bind=conn) as session:
        validate_rollups(session, tenant)
        for currency in expected["currencies"]:
            query = AnalyticsQuery(tenant_id=tenant, currency=currency, start=START, end=END)
            proof = read_coverage(
                session,
                AnalyticsQuery(
                    tenant_id=tenant,
                    currency=currency,
                    start=START,
                    end=END,
                ),
            )
            if not proof.ready:
                raise ValueError("Coverage is not certified")
            for dimension in ("summary", "status", "product"):
                require_coverage(proof, query, dimension)
    mismatch = conn.scalar(
        text(f"""SELECT count(*) FROM orders o
        FULL JOIN order_projections p ON p.tenant_id=o.tenant_id AND p.order_id=o.id
        WHERE o.id IS NULL OR p.order_id IS NULL
        OR p.last_applied_version<>o.version
        OR (p.canonical_snapshot-'created_at'-'updated_at') IS DISTINCT FROM
            ({canonical_sql()}-'created_at'-'updated_at')
        OR (p.canonical_snapshot->>'created_at')::timestamptz IS DISTINCT FROM o.created_at
        OR (p.canonical_snapshot->>'updated_at')::timestamptz IS DISTINCT FROM o.updated_at""")
    )
    if mismatch:
        raise ValueError("Canonical projection/source parity mismatch")
    return {
        "snapshot": actual,
        "rollups": "six exact SQL matches",
        "coverage": "ready",
        "projections": "exact version/content matches",
        "database_bytes": conn.scalar(text("SELECT pg_database_size(current_database())")),
    }


def resolve_targets(conn, plan):
    rows = (
        conn.execute(text("SELECT tenant_id,database_name,status FROM control_tenant_registry"))
        .mappings()
        .all()
    )
    mapped = {str(row["tenant_id"]): row for row in rows}
    selected, seen = [], set()
    for tenant in plan["tenants"]:
        row = mapped.get(tenant["tenant_id"])
        if row is None or row["status"] != "active":
            raise ValueError(f"Target tenant is absent/inactive: {tenant['tenant_id']}")
        name = row["database_name"]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) or name in seen:
            raise ValueError("Target database name is invalid or duplicated")
        if name in ("postgres", "template0", "template1", conn.engine.url.database):
            raise ValueError("Target must be a separate dedicated database")
        seen.add(name)
        selected.append((tenant, name))
    return selected


def verify_plan(source_url, control_url, plan):
    if plan["metric_semantics"] != METRIC_SEMANTICS or plan["format_version"] != 1:
        raise ValueError("Unsupported plan contract")
    source, control = readonly_engine(source_url), readonly_engine(control_url)
    results = []
    try:
        with source.connect().execution_options(isolation_level="REPEATABLE READ") as src:
            with control.connect() as ctl:
                targets = resolve_targets(ctl, plan)
            for expected, name in targets:
                tenant = UUID(expected["tenant_id"])
                # Detect changed selected rows/history before certifying migration parity.
                assert_snapshot(
                    expected,
                    snapshot(
                        src,
                        plan["source_schema"],
                        tenant,
                        expected["orders"],
                        UUID(expected["last_order_id"]),
                    ),
                )
                target = readonly_engine(make_url(control_url).set(database=name))
                try:
                    with target.connect().execution_options(isolation_level="REPEATABLE READ") as c:
                        results.append({"database": name, **validate_target(c, expected)})
                finally:
                    target.dispose()
        return {
            "phase": "verified",
            "databases": len(results),
            "migrated_orders": sum(r["snapshot"]["orders"] for r in results),
            "new_historical_orders": 0,
            "targets": results,
        }
    finally:
        source.dispose()
        control.dispose()


def compare_api(conn, body, endpoint, params, interval="day"):
    response_type = {
        "summary": SummaryResponse,
        "trends": TrendsResponse,
        "top-products": TopProductsResponse,
        "status-breakdown": StatusBreakdownResponse,
    }[endpoint]
    validated = response_type.model_validate_json(json.dumps(body))
    if validated.start != params["start"] or validated.end != params["end"]:
        raise ValueError("API time window mismatch")
    sql = SQL[endpoint]
    if endpoint == "trends" and interval == "hour":
        sql = sql.replace("date_trunc('day'", "date_trunc('hour'")
    expected = canonical(conn.execute(text(sql), params).mappings())
    keys = {"trends": "buckets", "top-products": "products", "status-breakdown": "statuses"}
    actual = [body] if endpoint == "summary" else body[keys[endpoint]]
    if endpoint == "trends":
        if body["interval"] != interval:
            raise ValueError("API trend interval mismatch")
        by_bucket = {datetime.fromisoformat(row["bucket_start"]): row for row in expected}
        expected = []
        bucket = params["start"]
        while bucket < params["end"]:
            expected.append(
                by_bucket.get(
                    bucket,
                    {
                        "bucket_start": bucket.isoformat(),
                        "total_order_count": 0,
                        "sales_amount": "0.00",
                    },
                )
            )
            bucket += timedelta(hours=1) if interval == "hour" else timedelta(days=1)
    if endpoint == "status-breakdown":
        actual = sorted(actual, key=lambda row: row["status"])
        expected_by_status = {row["status"]: row for row in expected}
        expected = [
            expected_by_status.get(s, {"status": s, "total_order_count": 0, "sales_amount": "0.00"})
            for s in ("cancelled", "placed", "shipped")
        ]
    fields = set(expected[0]) if expected else set()
    actual = [{k: row[k] for k in fields} for row in actual]
    for rows in (expected, actual):
        for row in rows:
            if "sales_amount" in row:
                row["sales_amount"] = Decimal(row["sales_amount"])
            if "bucket_start" in row:
                row["bucket_start"] = datetime.fromisoformat(row["bucket_start"])
    if actual != expected or body["metric_semantics"] != METRIC_SEMANTICS:
        raise ValueError(f"Exact API/SQL mismatch: {endpoint}/{interval}")
    if str(body["tenant_id"]) != str(params["tenant"]) or body["currency"] != params["currency"]:
        raise ValueError("API tenant/currency mismatch")


def reader_query_plans(engine, params):
    """Explain actual reader SELECTs, separately from raw-order oracle plans."""
    from astraroll.aggregation.reader import PostgresAnalyticsReader

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session:
            reader = PostgresAnalyticsReader(session)
            args = (params["tenant"], params["currency"], params["start"], params["end"])
            reader.get_summary(*args)
            reader.get_trends(*args, "day")
            reader.get_trends(*args, "hour")
            reader.get_top_products(*args)
            reader.get_status_breakdown(*args)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    plans = []
    with engine.connect() as conn:
        for statement, parameters in statements:
            if re.search(r"\bFROM\s+(?:public\.)?orders\b", statement, re.IGNORECASE):
                raise ValueError("Analytics reader unexpectedly scans raw orders")
            plans.append(
                {
                    "sql": statement,
                    "plan": conn.exec_driver_sql(
                        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement,
                        parameters,
                    ).scalar(),
                }
            )
    return plans


def measure_api(control_url, plan, api_url, token, samples=200, concurrency=4):
    """Real authenticated HTTP; all tenants/currencies/shapes checked before sampling."""
    if not 1 <= concurrency <= 16 or samples < len(plan["tenants"]):
        raise ValueError("Use 1..16 users and at least one sample per selected tenant")
    control = readonly_engine(control_url)
    try:
        with control.connect() as c:
            targets = resolve_targets(c, plan)
        with httpx.Client(
            base_url=api_url, timeout=30, headers={"Authorization": f"Bearer {token}"}
        ) as client:

            def request(endpoint, tenant, currency="USD", interval="day"):
                params = {
                    "tenant_id": tenant["tenant_id"],
                    "currency": currency,
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                }
                if endpoint == "trends":
                    params["interval"] = interval
                response = client.get(f"/analytics/{endpoint}", params=params)
                response.raise_for_status()
                return response.json()

            plans = {}
            aggregate_plans = []
            for tenant, name in targets:
                engine = readonly_engine(make_url(control_url).set(database=name))
                try:
                    with engine.connect() as c:
                        for currency in tenant["currencies"]:
                            params = {
                                "tenant": UUID(tenant["tenant_id"]),
                                "currency": currency,
                                "start": START,
                                "end": END,
                            }
                            for endpoint in ENDPOINTS:
                                for interval in (
                                    ("day", "hour") if endpoint == "trends" else ("day",)
                                ):
                                    compare_api(
                                        c,
                                        request(endpoint, tenant, currency, interval),
                                        endpoint,
                                        params,
                                        interval,
                                    )
                            if not plans:
                                plans = {
                                    ep: c.execute(
                                        text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + SQL[ep]),
                                        params,
                                    ).scalar()
                                    for ep in ENDPOINTS
                                }
                                aggregate_plans = reader_query_plans(engine, params)
                finally:
                    engine.dispose()
            metrics = {}
            for endpoint in ENDPOINTS:

                def sample(i):
                    begin = time.perf_counter()
                    request(endpoint, targets[i % len(targets)][0])
                    return (time.perf_counter() - begin) * 1000

                begin = time.perf_counter()
                with ThreadPoolExecutor(concurrency) as pool:
                    values = list(pool.map(sample, range(samples)))
                metrics[endpoint] = {
                    **percentiles(values),
                    "samples": len(values),
                    "requests_per_second": samples / (time.perf_counter() - begin),
                    "p95_under_200ms": percentiles(values)["p95_ms"] < 200,
                    "raw_ms": values,
                }
            return {
                "phase": "measured",
                "concurrency": concurrency,
                "oracle_tenants": len(targets),
                "queries": metrics,
                "sql_plans": plans,
                "aggregate_reader_plans": aggregate_plans,
                "limitations": [
                    "Same-host HTTP unless external API URL supplied",
                    "Direct SQL plans describe first tenant/currency only",
                    "No migration/live-event claim follows from read-only measurement",
                ],
            }
    finally:
        control.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "verify", "measure"))
    parser.add_argument("--source-schema")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tenants", type=int, default=100)
    parser.add_argument("--orders-per-tenant", type=int, default=10000)
    parser.add_argument("--api-url")
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    settings = get_settings()
    if args.command == "plan":
        if not args.source_schema:
            parser.error("plan requires --source-schema")
        if Path(args.manifest).exists():
            parser.error("Manifest exists; choose a new path to preserve provenance")
        result = create_plan(
            settings.database_url, args.source_schema, args.tenants, args.orders_per_tenant
        )
        Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(args.manifest).write_text(json.dumps(result, default=normalized, indent=2))
    else:
        plan = json.loads(Path(args.manifest).read_text())
        control_url = settings.control_database_url or settings.database_url
        if args.command == "verify":
            result = verify_plan(settings.database_url, control_url, plan)
        else:
            if not args.api_url or not os.environ.get("BENCHMARK_BEARER_TOKEN"):
                parser.error("measure requires --api-url and BENCHMARK_BEARER_TOKEN")
            result = measure_api(
                control_url,
                plan,
                args.api_url,
                os.environ["BENCHMARK_BEARER_TOKEN"],
                args.samples,
                args.concurrency,
            )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, default=normalized, indent=2))
    print(f"{args.command} complete; retained report: {args.output}")


if __name__ == "__main__":
    main()
