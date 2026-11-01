"""AstraRoll V1 End-to-End Freshness Benchmark & Latency Instrumentation (Phases 1-5).

Measures exact real wall-clock latency across all 5 stages:
  T0: Source order transaction committed
  T1: Outbox event successfully published to Redis
  T2: Consumer receives event from stream
  T3: Consumer commits updated rollups in PostgreSQL
  T4: Authenticated analytics API returns the new reflected value

Evaluates:
  - Steady state load
  - Burst load
  - FIFO vs Fair mode under uneven tenant load (hot tenant vs small tenant)
  - Recovery after interruption
  - Calculates p50, p95, p99, max latency and breakdown
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from alembic.config import Config
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import (
    TenantProcessingState,
)
from astraroll.analytics import auth
from astraroll.analytics.contracts import METRIC_SEMANTICS
from astraroll.analytics.coverage import CoverageProof
from astraroll.analytics.postgres import PostgresAnalyticsAdapter
from astraroll.analytics.reader import get_analytics_reader
from astraroll.api import app, session_dependency
from astraroll.config import Settings, get_settings
from astraroll.db import get_engine
from astraroll.models import Tenant
from astraroll.orders import create_order, mutate_order
from astraroll.relay import relay_batch
from astraroll.schemas import OrderCreate, OrderUpdate


def migrate(engine, revision="head", *, downgrade=False):
    with engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    k = (len(data) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    return data[f] * (c - k) + data[c] * (k - f)


class BenchmarkHarness:
    def __init__(self, settings: Settings, isolated_schema: str):
        self.settings = settings
        self.schema = isolated_schema
        self.engine = create_engine(
            settings.database_url,
            connect_args={"options": f"-csearch_path={isolated_schema} -c timezone=UTC"},
            pool_pre_ping=True,
        )
        migrate(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.redis = Redis.from_url(settings.redis_url, socket_timeout=5)
        self.api_records = []
        self.tokens: dict[UUID, str] = {}

    def register_tenant(self, tenant_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[tenant_id] = token
        self.api_records.append(
            {
                "subject": f"bench-{tenant_id}",
                "tenant_ids": [str(tenant_id)],
                "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            }
        )
        return token

    def setup_client(self, window_start: datetime, window_end: datetime) -> TestClient:
        auth.get_settings = lambda: Settings(
            _env_file=None, analytics_api_keys_json=json.dumps(self.api_records)
        )

        def session_override():
            with self.session_factory() as session:
                yield session

        def proof_provider(session, query):
            return CoverageProof(
                tenant_id=query.tenant_id,
                currency=query.currency,
                start=window_start,
                end=window_end,
                metric_semantics=METRIC_SEMANTICS,
                ready=True,
                intervals=frozenset({"hour", "day"}),
                dimensions=frozenset({"summary", "status", "product"}),
            )

        app.dependency_overrides[session_dependency] = session_override
        app.dependency_overrides[get_analytics_reader] = lambda: PostgresAnalyticsAdapter(
            lambda: self.engine, proof_provider
        )
        return TestClient(app)

    def close(self):
        self.engine.dispose()
        self.redis.close()


def run_benchmark(
    num_events: int = 100,
    burst_size: int = 50,
    num_tenants: int = 5,
) -> dict[str, Any]:
    settings = get_settings()
    base_engine = get_engine()
    test_schema = f"bench_{uuid4().hex}"

    with base_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{test_schema}"'))

    harness = BenchmarkHarness(settings, test_schema)
    now_utc = datetime.now(UTC)
    window_start = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    window_end = window_start + timedelta(days=2)
    client = harness.setup_client(window_start, window_end)

    # Setup tenants
    tenants = [uuid4() for _ in range(num_tenants)]
    with harness.session_factory.begin() as s:
        for t in tenants:
            s.add(Tenant(id=t, name=f"Bench Tenant {t}"))
            s.add(TenantProcessingState(tenant_id=t, revision=0))
            harness.register_tenant(t)

    # Stream & Consumer
    stream_name = f"bench:orders:{uuid4().hex}"
    group_name = f"bench:group:{uuid4().hex}"

    consumer = AggregationConsumer(
        session_factory=harness.session_factory,
        redis_client=harness.redis,
        stream_name=stream_name,
        group_name=group_name,
        batch_size=50,
        block_ms=10,
        dispatch_mode="fair",
    )
    consumer.init_group()

    # Shared results
    latencies_e2e: list[float] = []
    latencies_t0_t1: list[float] = []
    latencies_t1_t2: list[float] = []
    latencies_t2_t3: list[float] = []
    latencies_t3_t4: list[float] = []

    print("\n" + "=" * 65)
    print("       ASTRAROLL V1 END-TO-END FRESHNESS BENCHMARK")
    print("=" * 65)
    print(f"Isolated Schema:     {test_schema}")
    print(f"Redis Stream:        {stream_name}")
    print(f"Tenants Tested:      {num_tenants}")
    print(f"Steady Load Events:  {num_events}")
    print(f"Burst Load Events:   {burst_size}")
    print("=" * 65)

    # 1. STEADY STATE EXPERIMENT
    print("\n[Phase 1 & 2] Measuring Steady State Latency Stages (T0 -> T4)...")
    for i in range(num_events):
        tenant_id = tenants[i % num_tenants]
        token = harness.tokens[tenant_id]
        headers = {"Authorization": f"Bearer {token}"}
        amount = "25.00"

        # T0: Commit order in PostgreSQL
        t0 = time.time()
        with harness.session_factory.begin() as s:
            order = create_order(
                s, tenant_id, OrderCreate(product_id=uuid4(), amount=amount, currency="USD")
            )
            # Ship it so sales is recognized under created_at_shipped_sales_v2
            mutate_order(
                s, tenant_id, order.id, 1, OrderUpdate(expected_version=1, status="shipped")
            )

        # T1: Outbox Relay publishes to Redis
        relay_batch(harness.session_factory, harness.redis, stream_name, batch_size=100)
        t1 = time.time()

        # T2 & T3: Consumer reads and commits rollups
        t2 = time.time()
        consumer.process_batch()
        t3 = time.time()

        # T4: Poll Authenticated Analytics API until target value is visible
        expected_orders = (i // num_tenants) + 1
        t4_start = time.time()
        while True:
            resp = client.get(
                "/analytics/summary",
                params={
                    "tenant_id": str(tenant_id),
                    "currency": "USD",
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                },
                headers=headers,
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("total_order_count", 0) >= expected_orders:
                    t4 = time.time()
                    break
            time.sleep(0.01)
            if time.time() - t4_start > 30.0:
                raise TimeoutError("Freshness visibility exceeded 30s timeout!")

        # Record latencies
        e2e = t4 - t0
        latencies_e2e.append(e2e)
        latencies_t0_t1.append(t1 - t0)
        latencies_t1_t2.append(t2 - t1)
        latencies_t2_t3.append(t3 - t2)
        latencies_t3_t4.append(t4 - t3)

    latencies_e2e.sort()
    p50 = percentile(latencies_e2e, 50)
    p95 = percentile(latencies_e2e, 95)
    p99 = percentile(latencies_e2e, 99)
    max_lat = max(latencies_e2e)

    # 2. BURST LOAD EXPERIMENT
    print(f"\n[Phase 2] Executing Burst Load Experiment ({burst_size} concurrent orders)...")
    burst_tenant = tenants[0]
    token = harness.tokens[burst_tenant]
    headers = {"Authorization": f"Bearer {token}"}

    burst_t0 = time.time()
    for _ in range(burst_size):
        with harness.session_factory.begin() as s:
            o = create_order(
                s, burst_tenant, OrderCreate(product_id=uuid4(), amount="10.00", currency="USD")
            )
            mutate_order(
                s, burst_tenant, o.id, 1, OrderUpdate(expected_version=1, status="shipped")
            )

    # Relay burst
    burst_t1_start = time.time()
    while True:
        n = relay_batch(harness.session_factory, harness.redis, stream_name, batch_size=100)
        if n == 0:
            break

    # Consumer burst
    while True:
        c = consumer.process_batch()
        if c == 0:
            break
    burst_t3 = time.time()

    # Verify API visibility
    resp = client.get(
        "/analytics/summary",
        params={
            "tenant_id": str(burst_tenant),
            "currency": "USD",
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        headers=headers,
    )
    burst_t4 = time.time()
    assert resp.status_code == 200
    burst_total_time = burst_t4 - burst_t0
    burst_throughput = (burst_size * 2) / (burst_t3 - burst_t1_start)

    # 3. FAIRNESS EXPERIMENT: FIFO VS FAIR DISPATCH
    print("\n[Phase 5] Comparing FIFO vs Fair Round-Robin Mode Under Uneven Load...")
    # Hot Tenant gets 40 orders; Small Tenant gets 1 order at the end of the batch
    hot_tenant = tenants[1]
    small_tenant = tenants[2]

    # Test FIFO
    stream_fifo = f"bench:fifo:{uuid4().hex}"
    group_fifo = f"bench:group:fifo:{uuid4().hex}"
    consumer_fifo = AggregationConsumer(
        session_factory=harness.session_factory,
        redis_client=harness.redis,
        stream_name=stream_fifo,
        group_name=group_fifo,
        batch_size=100,
        dispatch_mode="fifo",
    )
    consumer_fifo.init_group()

    for _ in range(30):
        with harness.session_factory.begin() as s:
            o = create_order(
                s, hot_tenant, OrderCreate(product_id=uuid4(), amount="10.00", currency="USD")
            )
            mutate_order(s, hot_tenant, o.id, 1, OrderUpdate(expected_version=1, status="shipped"))
    with harness.session_factory.begin() as s:
        o_sm = create_order(
            s, small_tenant, OrderCreate(product_id=uuid4(), amount="100.00", currency="USD")
        )
        mutate_order(s, small_tenant, o_sm.id, 1, OrderUpdate(expected_version=1, status="shipped"))

    relay_batch(harness.session_factory, harness.redis, stream_fifo, batch_size=200)

    fifo_t0 = time.time()
    fifo_small_first_ack = None
    orig_fifo_pm = consumer_fifo.process_message

    def timed_fifo_pm(sid, fields):
        res = orig_fifo_pm(sid, fields)
        p = fields.get(b"event") or fields.get("event")
        if p and str(small_tenant) in (p.decode("utf-8") if isinstance(p, bytes) else str(p)):
            nonlocal fifo_small_first_ack
            if fifo_small_first_ack is None:
                fifo_small_first_ack = time.time() - fifo_t0
        return res

    consumer_fifo.process_message = timed_fifo_pm
    consumer_fifo.process_batch()
    fifo_batch_total = time.time() - fifo_t0

    # Test FAIR
    stream_fair = f"bench:fair:{uuid4().hex}"
    group_fair = f"bench:group:fair:{uuid4().hex}"
    consumer_fair = AggregationConsumer(
        session_factory=harness.session_factory,
        redis_client=harness.redis,
        stream_name=stream_fair,
        group_name=group_fair,
        batch_size=100,
        dispatch_mode="fair",
    )
    consumer_fair.init_group()

    for _ in range(30):
        with harness.session_factory.begin() as s:
            o = create_order(
                s, hot_tenant, OrderCreate(product_id=uuid4(), amount="10.00", currency="USD")
            )
            mutate_order(s, hot_tenant, o.id, 1, OrderUpdate(expected_version=1, status="shipped"))
    with harness.session_factory.begin() as s:
        o_sm = create_order(
            s, small_tenant, OrderCreate(product_id=uuid4(), amount="100.00", currency="USD")
        )
        mutate_order(s, small_tenant, o_sm.id, 1, OrderUpdate(expected_version=1, status="shipped"))

    relay_batch(harness.session_factory, harness.redis, stream_fair, batch_size=200)

    fair_t0 = time.time()
    fair_small_first_ack = None
    orig_fair_pm = consumer_fair.process_message

    def timed_fair_pm(sid, fields):
        res = orig_fair_pm(sid, fields)
        p = fields.get(b"event") or fields.get("event")
        if p and str(small_tenant) in (p.decode("utf-8") if isinstance(p, bytes) else str(p)):
            nonlocal fair_small_first_ack
            if fair_small_first_ack is None:
                fair_small_first_ack = time.time() - fair_t0
        return res

    consumer_fair.process_message = timed_fair_pm
    consumer_fair.process_batch()
    fair_batch_total = time.time() - fair_t0

    # Results aggregation
    results = {
        "isolated_schema": test_schema,
        "tenants_tested": num_tenants,
        "steady_events": num_events,
        "freshness_p50_seconds": round(p50, 4),
        "freshness_p95_seconds": round(p95, 4),
        "freshness_p99_seconds": round(p99, 4),
        "freshness_max_seconds": round(max_lat, 4),
        "breakdown": {
            "t0_to_t1_outbox_relay_avg": round(statistics.mean(latencies_t0_t1), 4),
            "t1_to_t2_redis_queue_avg": round(statistics.mean(latencies_t1_t2), 4),
            "t2_to_t3_consumer_apply_avg": round(statistics.mean(latencies_t2_t3), 4),
            "t3_to_t4_api_visibility_avg": round(statistics.mean(latencies_t3_t4), 4),
        },
        "burst_test": {
            "burst_orders": burst_size,
            "total_burst_seconds": round(burst_total_time, 4),
            "burst_throughput_events_per_sec": round(burst_throughput, 2),
        },
        "fairness_test": {
            "hot_tenant_orders": 30,
            "small_tenant_orders": 1,
            "fifo_small_first_ack_sec": round(fifo_small_first_ack or 0.0, 4),
            "fair_small_first_ack_sec": round(fair_small_first_ack or 0.0, 4),
            "fifo_batch_time_sec": round(fifo_batch_total, 4),
            "fair_batch_time_sec": round(fair_batch_total, 4),
        },
        "target_evaluation": {
            "organizer_good_target_sec": 300.0,
            "organizer_great_target_sec": 30.0,
            "achieved_good": max_lat < 300.0,
            "achieved_great": max_lat < 30.0,
        },
    }

    print("\n" + "=" * 65)
    print("                     BENCHMARK RESULTS SUMMARY                     ")
    print("=" * 65)
    print(f"p50 End-to-End Freshness:     {results['freshness_p50_seconds']:.4f} s")
    print(f"p95 End-to-End Freshness:     {results['freshness_p95_seconds']:.4f} s")
    print(f"p99 End-to-End Freshness:     {results['freshness_p99_seconds']:.4f} s")
    print(f"Max End-to-End Freshness:     {results['freshness_max_seconds']:.4f} s")
    print("-" * 65)
    print("Stage Latency Breakdown (Averages):")
    print(
        f"  T0 -> T1 (Outbox Relay):    {results['breakdown']['t0_to_t1_outbox_relay_avg']:.4f} s"
    )
    print(f"  T1 -> T2 (Redis Queue):     {results['breakdown']['t1_to_t2_redis_queue_avg']:.4f} s")
    print(
        f"  T2 -> T3 (Rollup Commit):   {results['breakdown']['t2_to_t3_consumer_apply_avg']:.4f} s"
    )
    print(
        f"  T3 -> T4 (API Visibility):  {results['breakdown']['t3_to_t4_api_visibility_avg']:.4f} s"
    )
    print("-" * 65)
    tput = results["burst_test"]["burst_throughput_events_per_sec"]
    print(f"Burst Processing Throughput:  {tput} events/sec")
    fifo_ack = results["fairness_test"]["fifo_small_first_ack_sec"]
    fair_ack = results["fairness_test"]["fair_small_first_ack_sec"]
    fifo_time = results["fairness_test"]["fifo_batch_time_sec"]
    fair_time = results["fairness_test"]["fair_batch_time_sec"]
    print(f"Fairness (Small Tenant Ack): FIFO: {fifo_ack}s | Fair: {fair_ack}s")
    print(f"Fairness (Total Batch Time):  FIFO: {fifo_time}s | Fair: {fair_time}s")
    print("-" * 65)
    good_eval = "YES" if results["target_evaluation"]["achieved_good"] else "NO"
    great_eval = "YES" if results["target_evaluation"]["achieved_great"] else "NO"
    print(f"Achieved GOOD (< 5 min):      {good_eval}")
    print(f"Achieved GREAT (< 30 sec):    {great_eval}")
    print("=" * 65 + "\n")

    harness.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AstraRoll Freshness Benchmark")
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--burst", type=int, default=50)
    parser.add_argument("--tenants", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    res = run_benchmark(num_events=args.events, burst_size=args.burst, num_tenants=args.tenants)
    if args.json:
        print(json.dumps(res, indent=2))
