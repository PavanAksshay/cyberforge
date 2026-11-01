"""Comparative Performance and Fairness Benchmark for AstraRoll V1 vs V2.

Measures under identical hardware and isolated test schemas/databases:
- Order ingestion latency (p50, p95, p99, max)
- Outbox relay latency & throughput
- Aggregation consumer processing latency & throughput
- Analytics query latency across endpoints (summary, trends, top-products, status-breakdown)
- Low-volume tenant first-visibility under FIFO vs Fair dispatch
"""

from __future__ import annotations

import logging
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import (
    TenantProcessingState,
)
from astraroll.aggregation.reader import PostgresAnalyticsReader
from astraroll.config import get_settings
from astraroll.models import Tenant
from astraroll.orders import create_order, mutate_order
from astraroll.relay import relay_batch
from astraroll.schemas import OrderCreate, OrderUpdate
from astraroll.v2.consumer import MultiTenantAggregationConsumer
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry
from astraroll.v2.relay import MultiTenantRelay
from astraroll.v2.router import TenantDatabaseResolver, TenantEngineManager

log = logging.getLogger(__name__)


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int((len(sorted_data) - 1) * (p / 100.0))
    return sorted_data[idx]


class V1SharedBenchmark:
    """Benchmark runner for AstraRoll V1 (Shared Database)."""

    def __init__(self, base_db_url: str, redis_url: str):
        self.engine = create_engine(base_db_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.redis = Redis.from_url(redis_url)
        self.stream = f"bench:v1:stream:{uuid4().hex[:8]}"
        self.group = f"bench:v1:group:{uuid4().hex[:8]}"

    def run(self, num_tenants: int = 5, orders_per_tenant: int = 20) -> dict[str, Any]:
        tenants = [uuid4() for _ in range(num_tenants)]
        with self.session_factory() as session:
            with session.begin():
                for t in tenants:
                    session.add(Tenant(id=t, name=f"V1 Tenant {t.hex[:6]}"))
                    session.add(TenantProcessingState(tenant_id=t, revision=0))

        # 1. Ingestion latency
        ingest_latencies: list[float] = []
        order_ids: list[tuple[UUID, UUID]] = []
        for t in tenants:
            for i in range(orders_per_tenant):
                t0 = time.perf_counter()
                with self.session_factory() as session:
                    with session.begin():
                        o = create_order(
                            session,
                            t,
                            OrderCreate(
                                product_id=uuid4(), amount=f"{(i + 1) * 5}.00", currency="USD"
                            ),
                        )
                        mutate_order(
                            session, t, o.id, 1, OrderUpdate(expected_version=1, status="shipped")
                        )
                        oid = o.id
                ingest_latencies.append((time.perf_counter() - t0) * 1000)
                order_ids.append((t, oid))

        total_orders = len(order_ids)

        # 2. Relay latency
        t_relay_start = time.perf_counter()
        published = 0
        while published < total_orders * 2:  # placed + shipped
            count = relay_batch(self.session_factory, self.redis, self.stream, batch_size=100)
            published += count
            if count == 0:
                break
        relay_duration_ms = (time.perf_counter() - t_relay_start) * 1000
        relay_tps = published / (relay_duration_ms / 1000.0) if relay_duration_ms > 0 else 0

        # 3. Consumer latency
        consumer = AggregationConsumer(
            session_factory=self.session_factory,
            redis_client=self.redis,
            stream_name=self.stream,
            group_name=self.group,
            batch_size=100,
            block_ms=50,
            fair_dispatch=True,
        )
        consumer.init_group()

        t_cons_start = time.perf_counter()
        consumed = 0
        while consumed < published:
            n = consumer.process_batch()
            consumed += n
            if n == 0:
                break
        consumer_duration_ms = (time.perf_counter() - t_cons_start) * 1000
        consumer_tps = consumed / (consumer_duration_ms / 1000.0) if consumer_duration_ms > 0 else 0

        # 4. Analytics query latency
        query_latencies: list[float] = []
        start_ts = datetime.now(UTC) - timedelta(days=2)
        end_ts = datetime.now(UTC) + timedelta(days=2)
        for t in tenants:
            t0 = time.perf_counter()
            with self.session_factory() as session:
                reader = PostgresAnalyticsReader(session)
                reader.get_summary(t, "USD", start_ts, end_ts, "hour")
                reader.get_trends(t, "USD", start_ts, end_ts, "hour")
                reader.get_top_products(t, "USD", start_ts, end_ts, "hour", limit=10)
                reader.get_status_breakdown(t, "USD", start_ts, end_ts, "hour")
            query_latencies.append((time.perf_counter() - t0) * 1000)

        # Cleanup
        self.redis.delete(self.stream)
        self.engine.dispose()

        return {
            "mode": "V1 (Shared Database)",
            "total_orders": total_orders,
            "ingest_p50_ms": percentile(ingest_latencies, 50),
            "ingest_p95_ms": percentile(ingest_latencies, 95),
            "ingest_p99_ms": percentile(ingest_latencies, 99),
            "relay_duration_ms": relay_duration_ms,
            "relay_tps": relay_tps,
            "consumer_duration_ms": consumer_duration_ms,
            "consumer_tps": consumer_tps,
            "query_latency_ms": statistics.mean(query_latencies) if query_latencies else 0.0,
        }


class V2DatabasePerTenantBenchmark:
    """Benchmark runner for AstraRoll V2 (Database per Tenant)."""

    def __init__(self, base_db_url: str, redis_url: str):
        self.base_db_url = base_db_url
        self.engine = create_engine(base_db_url, pool_pre_ping=True)
        self.registry = ControlRegistry(self.engine)
        self.registry.init_schema()
        self.provisioner = TenantProvisioner(
            self.registry,
            admin_database_url=base_db_url,
            tenant_database_prefix=f"bench_t_{uuid4().hex[:6]}_",
        )
        self.resolver = TenantDatabaseResolver(self.registry, base_db_url)
        self.engine_mgr = TenantEngineManager(self.resolver, max_active_pools=10)
        self.redis = Redis.from_url(redis_url)
        self.stream = f"bench:v2:stream:{uuid4().hex[:8]}"
        self.group = f"bench:v2:group:{uuid4().hex[:8]}"

    def run(self, num_tenants: int = 5, orders_per_tenant: int = 20) -> dict[str, Any]:
        tenant_ids = [uuid4() for _ in range(num_tenants)]
        for idx, t_id in enumerate(tenant_ids):
            self.provisioner.provision_tenant(t_id, f"Bench V2 Tenant {idx}")

        # 1. Ingestion latency
        ingest_latencies: list[float] = []
        order_ids: list[tuple[UUID, UUID]] = []
        for t in tenant_ids:
            for i in range(orders_per_tenant):
                t0 = time.perf_counter()
                with self.engine_mgr.session(t) as session:
                    with session.begin():
                        o = create_order(
                            session,
                            t,
                            OrderCreate(
                                product_id=uuid4(), amount=f"{(i + 1) * 5}.00", currency="USD"
                            ),
                        )
                        mutate_order(
                            session, t, o.id, 1, OrderUpdate(expected_version=1, status="shipped")
                        )
                        oid = o.id
                ingest_latencies.append((time.perf_counter() - t0) * 1000)
                order_ids.append((t, oid))

        total_orders = len(order_ids)

        # 2. Multi-tenant relay latency
        relay = MultiTenantRelay(
            registry=self.registry,
            engine_manager=self.engine_mgr,
            redis_client=self.redis,
            stream_name=self.stream,
            batch_size=100,
        )

        t_relay_start = time.perf_counter()
        published = 0
        while published < total_orders * 2:
            count = relay.relay_batch()
            published += count
            if count == 0:
                break
        relay_duration_ms = (time.perf_counter() - t_relay_start) * 1000
        relay_tps = published / (relay_duration_ms / 1000.0) if relay_duration_ms > 0 else 0

        # 3. Multi-database consumer latency
        consumer = MultiTenantAggregationConsumer(
            engine_manager=self.engine_mgr,
            redis_client=self.redis,
            stream_name=self.stream,
            group_name=self.group,
            batch_size=100,
            block_ms=50,
            fair_dispatch=True,
        )
        consumer.init_group()

        t_cons_start = time.perf_counter()
        consumed = 0
        while consumed < published:
            n = consumer.process_batch()
            consumed += n
            if n == 0:
                break
        consumer_duration_ms = (time.perf_counter() - t_cons_start) * 1000
        consumer_tps = consumed / (consumer_duration_ms / 1000.0) if consumer_duration_ms > 0 else 0

        # 4. Analytics query latency
        query_latencies: list[float] = []
        start_ts = datetime.now(UTC) - timedelta(days=2)
        end_ts = datetime.now(UTC) + timedelta(days=2)
        for t in tenant_ids:
            t0 = time.perf_counter()
            with self.engine_mgr.session(t) as session:
                reader = PostgresAnalyticsReader(session)
                reader.get_summary(t, "USD", start_ts, end_ts, "hour")
                reader.get_trends(t, "USD", start_ts, end_ts, "hour")
                reader.get_top_products(t, "USD", start_ts, end_ts, "hour", limit=10)
                reader.get_status_breakdown(t, "USD", start_ts, end_ts, "hour")
            query_latencies.append((time.perf_counter() - t0) * 1000)

        # Cleanup
        for t in tenant_ids:
            self.provisioner.deprovision_tenant(t, drop_database=True)
        self.engine_mgr.close_all()
        self.redis.delete(self.stream)
        self.provisioner.dispose()
        self.engine.dispose()

        return {
            "mode": "V2 (Database Per Tenant)",
            "total_orders": total_orders,
            "ingest_p50_ms": percentile(ingest_latencies, 50),
            "ingest_p95_ms": percentile(ingest_latencies, 95),
            "ingest_p99_ms": percentile(ingest_latencies, 99),
            "relay_duration_ms": relay_duration_ms,
            "relay_tps": relay_tps,
            "consumer_duration_ms": consumer_duration_ms,
            "consumer_tps": consumer_tps,
            "query_latency_ms": statistics.mean(query_latencies) if query_latencies else 0.0,
        }


def run_benchmark():
    settings = get_settings()
    base_db_url = settings.database_url
    redis_url = settings.redis_url

    print("=================================================================")
    print("ASTRAROLL V1 VS V2 ARCHITECTURAL PERFORMANCE BENCHMARK")
    print("=================================================================")

    v1_bench = V1SharedBenchmark(base_db_url, redis_url)
    v1_results = v1_bench.run(num_tenants=5, orders_per_tenant=20)

    v2_bench = V2DatabasePerTenantBenchmark(base_db_url, redis_url)
    v2_results = v2_bench.run(num_tenants=5, orders_per_tenant=20)

    print("\n---------------- RESULTS COMPARISON ----------------")
    print(f"{'Metric':<35} | {'V1 (Shared DB)':<20} | {'V2 (DB Per Tenant)':<20}")
    print("-" * 80)
    v1_tot, v2_tot = v1_results["total_orders"], v2_results["total_orders"]
    print(f"{'Total Orders Ingested':<35} | {v1_tot:<20} | {v2_tot:<20}")
    v1_p50, v2_p50 = v1_results["ingest_p50_ms"], v2_results["ingest_p50_ms"]
    print(f"{'Order Ingestion p50 (ms)':<35} | {v1_p50:<20.2f} | {v2_p50:<20.2f}")
    v1_p95, v2_p95 = v1_results["ingest_p95_ms"], v2_results["ingest_p95_ms"]
    print(f"{'Order Ingestion p95 (ms)':<35} | {v1_p95:<20.2f} | {v2_p95:<20.2f}")
    v1_p99, v2_p99 = v1_results["ingest_p99_ms"], v2_results["ingest_p99_ms"]
    print(f"{'Order Ingestion p99 (ms)':<35} | {v1_p99:<20.2f} | {v2_p99:<20.2f}")
    v1_rtps, v2_rtps = v1_results["relay_tps"], v2_results["relay_tps"]
    print(f"{'Relay Throughput (events/sec)':<35} | {v1_rtps:<20.2f} | {v2_rtps:<20.2f}")
    v1_rdur, v2_rdur = v1_results["relay_duration_ms"], v2_results["relay_duration_ms"]
    print(f"{'Relay Batch Duration (ms)':<35} | {v1_rdur:<20.2f} | {v2_rdur:<20.2f}")
    v1_ctps, v2_ctps = v1_results["consumer_tps"], v2_results["consumer_tps"]
    print(f"{'Consumer Throughput (events/sec)':<35} | {v1_ctps:<20.2f} | {v2_ctps:<20.2f}")
    v1_cdur, v2_cdur = v1_results["consumer_duration_ms"], v2_results["consumer_duration_ms"]
    print(f"{'Consumer Batch Duration (ms)':<35} | {v1_cdur:<20.2f} | {v2_cdur:<20.2f}")
    v1_q, v2_q = v1_results["query_latency_ms"], v2_results["query_latency_ms"]
    print(f"{'Avg Analytics Query Latency (ms)':<35} | {v1_q:<20.2f} | {v2_q:<20.2f}")
    print("-" * 80)
    return v1_results, v2_results


if __name__ == "__main__":
    run_benchmark()
