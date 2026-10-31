"""Reproducible isolated-schema SQL/API benchmark; generated data stays outside git.

Uses a real Uvicorn HTTP server, PostgreSQL and Redis. Bootstrap read benchmarks
and bounded live update throughput are reported separately. No destructive reset.
"""

import argparse
import csv
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
from alembic.config import Config
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from astraroll.aggregation.bootstrap import bootstrap
from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.config import get_settings
from astraroll.db import get_engine
from astraroll.models import Order
from astraroll.operations import reconcile
from astraroll.orders import mutate_order
from astraroll.schemas import OrderUpdate
from astraroll.workload import END, START, seed_tenant

ENDPOINTS = ("summary", "trends", "top-products", "status-breakdown")
WHERE = "tenant_id=:tenant AND currency=:currency AND created_at>=:start AND created_at<:end"
SALES = "coalesce(sum(CASE WHEN status='shipped' THEN amount ELSE 0 END),0)"
SQL = {
    "summary": f"SELECT count(*) AS total_order_count,{SALES} AS sales_amount "
    f"FROM orders WHERE {WHERE}",
    "trends": f"SELECT date_trunc('day',created_at) AS bucket_start,count(*) AS total_order_count,"
    f"{SALES} AS sales_amount FROM orders WHERE {WHERE} GROUP BY 1 ORDER BY 1",
    "top-products": f"SELECT product_id,count(*) AS total_order_count,{SALES} AS sales_amount "
    f"FROM orders WHERE {WHERE} GROUP BY product_id HAVING {SALES}>0 "
    "ORDER BY sales_amount DESC,total_order_count DESC,product_id ASC LIMIT 10",
    "status-breakdown": f"SELECT status,count(*) AS total_order_count,{SALES} AS sales_amount "
    f"FROM orders WHERE {WHERE} GROUP BY status ORDER BY status",
}


def percentiles(values):
    values = sorted(values)
    return {
        f"p{p}_ms": values[min(len(values) - 1, int(len(values) * p / 100))] for p in (50, 95, 99)
    }


def canonical(rows):
    return [
        {
            k: format(v, ".2f")
            if k == "sales_amount"
            else str(v)
            if k == "product_id"
            else v.isoformat()
            if isinstance(v, datetime)
            else v
            for k, v in row.items()
        }
        for row in rows
    ]


def postgres_cpu():
    totals = {}
    for process in psutil.process_iter(["name", "cpu_times", "create_time"]):
        try:
            if (process.info["name"] or "").lower().startswith("postgres"):
                cpu = process.info["cpu_times"]
                totals[(process.pid, process.info["create_time"])] = cpu.user + cpu.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return totals


def cpu_delta(before, after):
    # Exited processes are excluded: a documented lower bound, never a negative delta.
    return sum(max(0, after[key] - before[key]) for key in before.keys() & after.keys())


def run(args):
    if args.orders < args.tenants or args.orders % args.tenants:
        raise ValueError("Orders must be positive and divisible by tenants")
    if not 1 <= args.concurrency <= 64 or args.samples < 10:
        raise ValueError("Concurrency 1..64 and at least 10 samples required")
    if not 1 <= args.seed_workers <= 8 or not 1 <= args.bootstrap_workers <= 8:
        raise ValueError("Seed/bootstrap workers must be 1..8")
    namespace = uuid4()
    schema = f"bench_{namespace.hex}"
    artifact = Path(args.output) / schema
    artifact.mkdir(parents=True, exist_ok=False)
    base = get_engine()
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        base.url, connect_args={"options": f"-csearch_path={schema} -c timezone=UTC"}
    )
    with engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    factory = sessionmaker(engine, expire_on_commit=False)
    report = {
        "schema": schema,
        "namespace": str(namespace),
        "requested_orders": args.orders,
        "tenants": args.tenants,
        "metric_semantics": "created_at_shipped_sales_v2",
        "started_at": datetime.now(UTC).isoformat(),
        "runtime": {},
        "configuration": vars(args),
        "limitations": [
            "Synthetic uniform tenant/cohort workload; warm queries; same-machine HTTP.",
            "Seed/bootstrap throughput is separate from live relay/consumer throughput.",
            "CPU is a lower bound for surviving host PostgreSQL PIDs, not exclusively this schema.",
            "No worker concurrency/fairness claim is inferred from API concurrency.",
        ],
    }
    with engine.connect() as conn:
        report["runtime"]["postgres"] = conn.scalar(text("SELECT version()"))
    tenants = []
    seeded = time.perf_counter()

    def seed_one(i):
        tenant = seed_tenant(factory, namespace, i, args.orders // args.tenants, args.batch_size)
        print(f"Seeded tenant {i + 1}/{args.tenants}", flush=True)
        return tenant

    with ThreadPoolExecutor(args.seed_workers) as pool:
        tenants = list(pool.map(seed_one, range(args.tenants)))
    report["seed_seconds"] = time.perf_counter() - seeded
    with engine.begin() as conn:
        conn.execute(text("ANALYZE orders"))
        report["actual_orders"] = conn.scalar(text("SELECT count(*) FROM orders"))
        report["outbox_events"] = conn.scalar(text("SELECT count(*) FROM outbox_events"))
    built = time.perf_counter()

    def build_one(item):
        i, tenant = item
        bootstrap(factory, tenant, ["USD", "EUR"], START, END, args.batch_size)
        print(f"Bootstrapped tenant {i + 1}/{args.tenants}", flush=True)

    with ThreadPoolExecutor(args.bootstrap_workers) as pool:
        list(pool.map(build_one, enumerate(tenants)))
    report["bootstrap_seconds"] = time.perf_counter() - built
    for tenant in tenants:
        assert reconcile(factory, tenant)["status"] == "match"
    token = secrets.token_urlsafe(32)
    records = [
        {
            "subject": "benchmark",
            "is_admin": True,
            "tenant_ids": [str(t) for t in tenants],
            "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        }
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    api_url = base.url.update_query_dict({"options": f"-csearch_path={schema} -c timezone=UTC"})
    environment = {
        **os.environ,
        "DATABASE_URL": api_url.render_as_string(hide_password=False),
        "ANALYTICS_BACKEND": "postgres",
        "ANALYTICS_COVERAGE_PROVIDER": "astraroll.aggregation.coverage:read_coverage",
        "ANALYTICS_API_KEYS_JSON": json.dumps(records),
    }
    log = (artifact / "api.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "astraroll.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=environment,
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = []
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=60,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            for _ in range(100):
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Benchmark API did not become ready; inspect api.log")

            def api_query(endpoint, tenant):
                response = client.get(
                    f"/analytics/{endpoint}",
                    params={
                        "tenant_id": str(tenant),
                        "currency": "USD",
                        "start": START.isoformat(),
                        "end": END.isoformat(),
                    },
                )
                response.raise_for_status()
                return response.json()

            # Complete matching oracle for all tenants and all four query shapes.
            for tenant in tenants:
                for endpoint in ENDPOINTS:
                    params = {"tenant": tenant, "currency": "USD", "start": START, "end": END}
                    with engine.connect() as conn:
                        expected = canonical(conn.execute(text(SQL[endpoint]), params).mappings())
                    body = api_query(endpoint, tenant)
                    keys = {
                        "trends": "buckets",
                        "top-products": "products",
                        "status-breakdown": "statuses",
                    }
                    actual = [body] if endpoint == "summary" else body[keys[endpoint]]
                    if endpoint == "trends":
                        actual = [r for r in actual if r["total_order_count"]]
                    actual = (
                        [{k: row[k] for k in expected[0]} for row in actual] if expected else actual
                    )
                    # Normalize timestamps parsed from either ISO representation.
                    if endpoint == "trends":
                        for rows in (actual, expected):
                            for row in rows:
                                row["bucket_start"] = datetime.fromisoformat(
                                    row["bucket_start"]
                                ).isoformat()
                    if endpoint == "status-breakdown":
                        actual = sorted(actual, key=lambda r: r["status"])
                    assert actual == expected, (tenant, endpoint, actual, expected)
            report["correctness"] = {"tenants": len(tenants), "query_shapes": 4, "all_match": True}
            report["endpoint_examples"] = {
                endpoint: api_query(endpoint, tenants[0]) for endpoint in ENDPOINTS
            }
            report["queries"] = {}
            for endpoint in ENDPOINTS:
                # Warm both paths equally before recorded observations.
                for t in tenants[:2]:
                    api_query(endpoint, t)

                def sql_sample(i):
                    begin = time.perf_counter()
                    with engine.connect() as conn:
                        conn.execute(
                            text(SQL[endpoint]),
                            {
                                "tenant": tenants[i % len(tenants)],
                                "currency": "USD",
                                "start": START,
                                "end": END,
                            },
                        ).all()
                    return (time.perf_counter() - begin) * 1000

                def api_sample(i):
                    begin = time.perf_counter()
                    api_query(endpoint, tenants[i % len(tenants)])
                    return (time.perf_counter() - begin) * 1000

                result = {}
                for label, operation in (("sql", sql_sample), ("api", api_sample)):
                    begin, cpu = time.perf_counter(), postgres_cpu()
                    with ThreadPoolExecutor(args.concurrency) as pool:
                        values = list(pool.map(operation, range(args.samples)))
                    elapsed = time.perf_counter() - begin
                    result[label] = {
                        **percentiles(values),
                        "requests_per_second": args.samples / elapsed,
                        "postgres_cpu_seconds_lower_bound": cpu_delta(cpu, postgres_cpu()),
                    }
                    raw.extend(
                        {"endpoint": endpoint, "path": label, "sample": i, "ms": v}
                        for i, v in enumerate(values)
                    )
                report["queries"][endpoint] = result
            # Indexed normal read baseline vs concurrent analytics HTTP load.
            with engine.connect() as conn:
                oid = conn.scalar(text("SELECT id FROM orders LIMIT 1"))

            def normal_samples():
                values = []
                for _ in range(args.samples):
                    begin = time.perf_counter()
                    with engine.connect() as conn:
                        conn.execute(
                            text("SELECT id,version FROM orders WHERE id=:id"), {"id": oid}
                        ).one()
                    values.append((time.perf_counter() - begin) * 1000)
                return percentiles(values)

            baseline = normal_samples()
            with ThreadPoolExecutor(args.concurrency) as pool:
                futures = [
                    pool.submit(api_query, "summary", tenants[i % len(tenants)])
                    for i in range(args.samples * 2)
                ]
                loaded = normal_samples()
                for future in futures:
                    future.result()
            report["normal_query"] = {
                "idle": baseline,
                "with_analytics": loaded,
                "p95_slowdown_percent": (loaded["p95_ms"] / baseline["p95_ms"] - 1) * 100,
            }
            report["operations"] = client.get(
                "/ops/metrics", params={"tenant_id": str(tenants[0])}
            ).json()
            # Bounded live changes: bootstrap includes history, replaying the entire
            # bulk outbox is intentionally a separate operational exercise.
            settings = get_settings()
            with Redis.from_url(settings.redis_url, socket_timeout=30) as redis:
                report["runtime"]["redis"] = redis.info("server")["redis_version"]
                stream, group = f"bench:live:{namespace}", f"bench:group:{namespace}"
                worker = AggregationConsumer(
                    factory,
                    redis,
                    stream_name=stream,
                    group_name=group,
                    batch_size=1000,
                    block_ms=1,
                )
                worker.init_group()
                # Publish only newly created corrections, leaving bulk history durable/unpublished.
                boundary = datetime.now(UTC)
                with factory() as session:
                    placed = session.query(Order).filter(Order.status == "placed").limit(100).all()
                begin = time.perf_counter()
                for order in placed:
                    with factory.begin() as session:
                        mutate_order(
                            session,
                            order.tenant_id,
                            order.id,
                            order.version,
                            OrderUpdate(expected_version=order.version, status="shipped"),
                        )
                written_at = time.perf_counter()
                # Use bounded filtered replay for this run, never drain an unrelated outbox.
                from astraroll.aggregation.replay import replay_outbox_events

                n = replay_outbox_events(factory, redis, stream, from_ts=boundary, batch_size=1000)
                published_at = time.perf_counter()
                while worker.process_batch():
                    pass
                elapsed = time.perf_counter() - begin
                report["live_update"] = {
                    "events": n,
                    "seconds": elapsed,
                    "events_per_second": n / elapsed,
                    "source_write_seconds": written_at - begin,
                    "publish_seconds": published_at - written_at,
                    "consumer_seconds": time.perf_counter() - published_at,
                    "consumer_events_per_second": n / (time.perf_counter() - published_at),
                    "commit_to_drain_seconds_upper_bound": elapsed,
                    "pending": redis.xpending(stream, group)["pending"],
                    "stream_length": redis.xlen(stream),
                    "redis_memory_bytes": redis.info("memory")["used_memory"],
                }
                with engine.connect() as conn:
                    lags = conn.scalars(
                        text("""SELECT extract(epoch FROM p.processed_at-o.occurred_at)*1000
                        FROM processed_events p JOIN outbox_events o ON p.event_id=o.id
                        WHERE o.occurred_at>=:boundary"""),
                        {"boundary": boundary},
                    ).all()
                report["live_update"]["source_commit_to_apply"] = percentiles(
                    [float(v) for v in lags]
                )
                for tenant in {o.tenant_id for o in placed}:
                    assert reconcile(factory, tenant)["status"] == "match"
            with engine.connect() as conn:
                report["table_sizes_bytes"] = dict(
                    conn.execute(
                        text("""SELECT relname,
                    pg_total_relation_size(c.oid) FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=:schema AND c.relkind='r'"""),
                        {"schema": schema},
                    ).all()
                )
            report["completed_at"] = datetime.now(UTC).isoformat()
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()
        (artifact / "report.json").write_text(json.dumps(report, indent=2, default=str))
        if raw:
            with (artifact / "samples.csv").open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(raw[0]))
                writer.writeheader()
                writer.writerows(raw)
    print(f"Report: {artifact / 'report.json'}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=1000000)
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed-workers", type=int, default=1)
    parser.add_argument("--bootstrap-workers", type=int, default=1)
    parser.add_argument("--output", default="artifacts")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
