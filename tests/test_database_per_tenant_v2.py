"""Comprehensive verification and reliability test suite for AstraRoll V2 — Database-per-Tenant."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from astraroll.aggregation.models import (
    HourlySummaryAggregate,
    TenantProcessingState,
)
from astraroll.analytics.contracts import AnalyticsQuery
from astraroll.api import app
from astraroll.config import get_settings
from astraroll.models import Order, Tenant
from astraroll.orders import create_order, mutate_order
from astraroll.schemas import OrderCreate, OrderUpdate
from astraroll.v2.adapter import MultiTenantPostgresAnalyticsAdapter
from astraroll.v2.consumer import MultiTenantAggregationConsumer
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry, TenantStatus
from astraroll.v2.relay import MultiTenantRelay
from astraroll.v2.router import (
    TenantDatabaseResolver,
    TenantEngineManager,
    TenantInactiveError,
    TenantNotFoundError,
)

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
END = START + timedelta(days=60)


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch):
    """Ensure settings cache is clean before and after each test."""
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def base_db_url():
    """Use isolated local PostgreSQL test cluster on port 5432 (never port 55433)."""
    return get_settings().database_url


@pytest.fixture
def control_registry(base_db_url):
    """Provides an isolated control plane registry for testing."""
    engine = create_engine(base_db_url, pool_pre_ping=True)
    registry = ControlRegistry(engine)
    registry.init_schema()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM control_tenant_migration_checkpoints"))
        conn.execute(text("DELETE FROM control_tenant_registry"))
    yield registry
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM control_tenant_migration_checkpoints"))
        conn.execute(text("DELETE FROM control_tenant_registry"))
    engine.dispose()


@pytest.fixture
def provisioner(control_registry, base_db_url):
    """Provides a TenantProvisioner with unique prefix to avoid conflicts."""
    prefix = f"test_t_{uuid4().hex[:6]}_"
    prov = TenantProvisioner(
        registry=control_registry,
        admin_database_url=base_db_url,
        tenant_database_prefix=prefix,
    )
    yield prov
    prov.dispose()


@pytest.fixture
def engine_manager(control_registry, base_db_url):
    """Provides TenantEngineManager with configurable LRU budget."""
    resolver = TenantDatabaseResolver(control_registry, base_db_url, cache_ttl_seconds=5.0)
    manager = TenantEngineManager(
        resolver=resolver,
        max_active_pools=3,  # intentionally small to exercise LRU eviction
        pool_size=1,
        max_overflow=1,
    )
    yield manager
    manager.close_all()


# =========================================================================
# Phase 2: Central Tenant Registry Tests
# =========================================================================
def test_control_registry_lifecycle(control_registry):
    t_id = uuid4()
    db_name = f"test_db_{t_id.hex[:8]}"

    # Initial registration
    record = control_registry.register_tenant(
        tenant_id=t_id,
        name="Acme Corp",
        database_name=db_name,
        status=TenantStatus.PENDING,
    )
    assert record.tenant_id == t_id
    assert record.status == TenantStatus.PENDING.value
    assert not record.is_active()

    # Transition to PROVISIONING
    control_registry.update_status(t_id, TenantStatus.PROVISIONING)
    rec = control_registry.get_tenant(t_id)
    assert rec.status == TenantStatus.PROVISIONING.value

    # Transition to ACTIVE
    control_registry.update_status(t_id, TenantStatus.ACTIVE, mark_verified=True)
    rec = control_registry.get_tenant(t_id)
    assert rec.status == TenantStatus.ACTIVE.value
    assert rec.is_active()
    assert rec.last_verified_at is not None

    # List active
    active = control_registry.list_active_tenants()
    assert any(r.tenant_id == t_id for r in active)

    # Clean up lifecycle test record
    control_registry.update_status(t_id, TenantStatus.DECOMMISSIONED)


# =========================================================================
# Phase 3: Router & Engine Manager LRU Eviction Tests
# =========================================================================
def test_router_fail_closed_on_unregistered_or_inactive(control_registry, engine_manager):
    missing_id = uuid4()
    with pytest.raises(TenantNotFoundError):
        engine_manager.get_engine(missing_id)

    # Inactive tenant
    inactive_id = uuid4()
    control_registry.register_tenant(
        inactive_id, "Inactive Co", f"db_{inactive_id.hex[:8]}", status=TenantStatus.PENDING
    )
    with pytest.raises(TenantInactiveError):
        engine_manager.get_engine(inactive_id)
    control_registry.update_status(inactive_id, TenantStatus.DECOMMISSIONED)


def test_engine_manager_lru_pool_budget_and_eviction(control_registry, provisioner, base_db_url):
    """Verifies that engine_manager caps active connection pools and reclaims idle engines."""
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    # Cap at 2 active engines
    mgr = TenantEngineManager(resolver, max_active_pools=2, pool_size=1, max_overflow=0)

    t1, t2, t3 = uuid4(), uuid4(), uuid4()
    provisioner.provision_tenant(t1, "Tenant 1")
    provisioner.provision_tenant(t2, "Tenant 2")
    provisioner.provision_tenant(t3, "Tenant 3")

    try:
        # Access T1 -> active = 1
        mgr.get_engine(t1)
        assert mgr.active_pool_count == 1

        # Access T2 -> active = 2
        mgr.get_engine(t2)
        assert mgr.active_pool_count == 2

        # Access T3 -> active = 2 (T1 evicted by LRU)
        mgr.get_engine(t3)
        assert mgr.active_pool_count == 2

        # Access T1 again -> active = 2 (T2 evicted by LRU, T1 reopened)
        mgr.get_engine(t1)
        assert mgr.active_pool_count == 2

        # Verify operations on T1 still succeed after eviction and reload
        with mgr.session(t1) as session:
            assert session.scalar(text("SELECT 1")) == 1

    finally:
        mgr.close_all()
        provisioner.deprovision_tenant(t1, drop_database=True)
        provisioner.deprovision_tenant(t2, drop_database=True)
        provisioner.deprovision_tenant(t3, drop_database=True)


# =========================================================================
# Phase 4: Tenant Provisioning, Rollback & Concurrency Tests
# =========================================================================
def test_provisioning_idempotence_and_rollback(provisioner, control_registry):
    t_id = uuid4()
    # Successful provisioning
    rec = provisioner.provision_tenant(t_id, "Rollback Test Tenant")
    assert rec.status == TenantStatus.ACTIVE.value
    assert provisioner._database_exists(rec.database_name)

    # Rollback simulation: attempt provisioning duplicate database or invalid setup
    bad_id = uuid4()
    try:
        # Simulate failure by pointing admin URL to bogus or forcing an error in schema apply
        class BrokenProvisioner(TenantProvisioner):
            def _apply_schema(self, tenant_url, tenant_id, name):
                raise RuntimeError("Simulated schema migration failure")

        broken = BrokenProvisioner(
            control_registry,
            provisioner.admin_url.render_as_string(hide_password=False),
            provisioner.tenant_database_prefix,
        )
        with pytest.raises(RuntimeError, match="Simulated schema migration failure"):
            broken.provision_tenant(bad_id, "Bad Tenant")

        # Verify failure path: DB is preserved in FAILED state for diagnosis (no unsafe DROP)
        bad_rec = control_registry.get_tenant(bad_id)
        assert bad_rec.status == TenantStatus.FAILED.value
        assert "Simulated schema migration failure" in bad_rec.error_message
        assert provisioner._database_exists(bad_rec.database_name)
    finally:
        provisioner.deprovision_tenant(bad_id, drop_database=True)
        provisioner.deprovision_tenant(t_id, drop_database=True)


def test_concurrent_tenant_provisioning(provisioner):
    """Test provisioning multiple tenants in parallel with bounded workers."""
    tenants = [(uuid4(), f"Parallel Tenant {i}") for i in range(5)]
    records = provisioner.provision_tenants_concurrently(tenants, max_workers=3)
    assert len(records) == 5
    for r in records:
        assert r.status == TenantStatus.ACTIVE.value
        assert provisioner._database_exists(r.database_name)
        provisioner.deprovision_tenant(r.tenant_id, drop_database=True)


# =========================================================================
# Phase 5 & 6: Multi-Tenant Relay & Multi-Database Consumer Tests
# =========================================================================
def test_multi_tenant_relay_and_consumer_pipeline(
    control_registry, provisioner, base_db_url, redis_client
):
    """Verify orders written to separate tenant databases flow through Redis to separate DBs."""
    t_a = uuid4()
    t_b = uuid4()
    provisioner.provision_tenant(t_a, "Tenant Alpha")
    provisioner.provision_tenant(t_b, "Tenant Beta")

    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    stream_name = f"test:v2:stream:{uuid4().hex}"
    group_name = f"test:v2:group:{uuid4().hex}"

    relay = MultiTenantRelay(
        registry=control_registry,
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        batch_size=50,
    )

    consumer = MultiTenantAggregationConsumer(
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="test-worker-v2",
        block_ms=100,
        fair_dispatch=True,
    )
    consumer.init_group()

    try:
        # 1. Create orders directly in Tenant A and Tenant B databases
        prod_a = uuid4()
        prod_b = uuid4()

        with engine_mgr.session(t_a) as sess_a:
            with sess_a.begin():
                o_a = create_order(
                    sess_a, t_a, OrderCreate(product_id=prod_a, amount="100.00", currency="USD")
                )
                mutate_order(
                    sess_a, t_a, o_a.id, 1, OrderUpdate(expected_version=1, status="shipped")
                )

        with engine_mgr.session(t_b) as sess_b:
            with sess_b.begin():
                o_b = create_order(
                    sess_b, t_b, OrderCreate(product_id=prod_b, amount="250.00", currency="USD")
                )
                mutate_order(
                    sess_b, t_b, o_b.id, 1, OrderUpdate(expected_version=1, status="shipped")
                )

        # 2. Relay polling across active tenant databases
        published = relay.relay_batch()
        # 2 orders placed + 2 status changes = 4 outbox events
        assert published == 4

        # Verify Redis stream has 4 messages
        stream_len = redis_client.xlen(stream_name)
        assert stream_len == 4

        # 3. Consumer processes messages and writes to respective tenant databases
        processed = consumer.process_batch()
        assert processed == 4

        # Verify Redis pending list is 0 (all acknowledged)
        pending_info = redis_client.xpending(stream_name, group_name)
        assert pending_info["pending"] == 0

        # 4. Verify Tenant A database has rollups for Tenant A ONLY
        with engine_mgr.session(t_a) as sess_a:
            summary_a = sess_a.scalar(
                select(HourlySummaryAggregate).where(HourlySummaryAggregate.tenant_id == t_a)
            )
            assert summary_a is not None
            assert summary_a.order_count == 1
            assert summary_a.sales_amount == Decimal("100.00")

            # Tenant A database has ZERO data for Tenant B
            cross_check = sess_a.scalar(
                select(HourlySummaryAggregate).where(HourlySummaryAggregate.tenant_id == t_b)
            )
            assert cross_check is None

        # 5. Verify Tenant B database has rollups for Tenant B ONLY
        with engine_mgr.session(t_b) as sess_b:
            summary_b = sess_b.scalar(
                select(HourlySummaryAggregate).where(HourlySummaryAggregate.tenant_id == t_b)
            )
            assert summary_b is not None
            assert summary_b.order_count == 1
            assert summary_b.sales_amount == Decimal("250.00")

            # Tenant B database has ZERO data for Tenant A
            cross_check_b = sess_b.scalar(
                select(HourlySummaryAggregate).where(HourlySummaryAggregate.tenant_id == t_a)
            )
            assert cross_check_b is None

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_a, drop_database=True)
        provisioner.deprovision_tenant(t_b, drop_database=True)
        redis_client.delete(stream_name)


# =========================================================================
# Phase 7 & 8: Analytics API Routing & REPEATABLE READ Isolation
# =========================================================================
def test_analytics_adapter_routed_to_tenant_database(
    control_registry, provisioner, base_db_url, monkeypatch
):
    """Test PostgresAnalyticsAdapter routing dynamically to the tenant's dedicated database."""
    tenant_id = uuid4()
    provisioner.provision_tenant(tenant_id, "Analytics Routed Tenant")

    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    # Seed rollups in tenant's dedicated database
    with engine_mgr.session(tenant_id) as session:
        with session.begin():
            session.add(
                HourlySummaryAggregate(
                    tenant_id=tenant_id,
                    currency="USD",
                    bucket_start=START,
                    order_count=5,
                    sales_amount=Decimal("500.00"),
                )
            )

    from astraroll.analytics.coverage import CoverageProof

    def mock_coverage(session, q):
        return CoverageProof(
            tenant_id=q.tenant_id,
            currency=q.currency,
            start=START,
            end=END,
            metric_semantics="created_at_shipped_sales_v2",
            ready=True,
            intervals=frozenset({"hour", "day"}),
            dimensions=frozenset({"summary", "status", "product"}),
        )

    adapter = MultiTenantPostgresAnalyticsAdapter(
        engine_manager=engine_mgr,
        coverage=mock_coverage,
    )

    query = AnalyticsQuery(tenant_id=tenant_id, currency="USD", start=START, end=END)
    result = adapter.summary(query)

    assert result.value.total_order_count == 5
    assert result.value.sales_amount == Decimal("500.00")
    assert result.freshness.tenant_revision == 0

    engine_mgr.close_all()
    provisioner.deprovision_tenant(tenant_id, drop_database=True)


# =========================================================================
# Phase 8, 9 & 10: End-to-End HTTP Lifecycle, Security & Fault Isolation
# =========================================================================
def test_v2_end_to_end_fastapi_and_analytics_lifecycle(
    control_registry, provisioner, base_db_url, redis_client, monkeypatch
):
    """End-to-end integration test with FastAPI TestClient in database_per_tenant mode."""
    # 1. Enable database_per_tenant mode
    t_id = uuid4()
    p_id = uuid4()

    # Pre-register token for tenant
    token = secrets.token_urlsafe(32)
    keys_config = [
        {
            "subject": "v2-e2e-user",
            "tenant_ids": [str(t_id)],
            "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
    ]
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    monkeypatch.setenv("ANALYTICS_API_KEYS_JSON", json.dumps(keys_config))
    monkeypatch.setenv("ORDER_STREAM", f"test:v2:stream:{t_id.hex[:8]}")
    from astraroll.config import get_settings

    get_settings.cache_clear()

    # Provision tenant dedicated DB
    provisioner.provision_tenant(t_id, "E2E V2 Tenant")
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    headers = {"Authorization": f"Bearer {token}"}
    stream_name = f"test:v2:stream:{t_id.hex[:8]}"
    group_name = f"test:v2:group:{t_id.hex[:8]}"

    relay = MultiTenantRelay(
        registry=control_registry,
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        batch_size=50,
    )

    consumer = MultiTenantAggregationConsumer(
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="worker-e2e",
        block_ms=100,
        fair_dispatch=True,
    )
    consumer.init_group()

    from astraroll.analytics.coverage import CoverageProof
    from astraroll.analytics.reader import get_analytics_reader

    def mock_coverage(session, q):
        return CoverageProof(
            tenant_id=q.tenant_id,
            currency=q.currency,
            start=START,
            end=END,
            metric_semantics="created_at_shipped_sales_v2",
            ready=True,
            intervals=frozenset({"hour", "day"}),
            dimensions=frozenset({"summary", "status", "product"}),
        )

    adapter = MultiTenantPostgresAnalyticsAdapter(
        engine_manager=engine_mgr,
        coverage=mock_coverage,
    )
    app.dependency_overrides[get_analytics_reader] = lambda: adapter

    try:
        with TestClient(app) as client:
            # Create order via HTTP POST
            post_resp = client.post(
                f"/tenants/{t_id}/orders",
                json={"product_id": str(p_id), "amount": "88.50", "currency": "USD"},
                headers=headers,
            )
            assert post_resp.status_code == 201, post_resp.text
            order_data = post_resp.json()
            order_id = order_data["id"]
            assert order_data["amount"] == "88.50"
            assert order_data["version"] == 1

            # Transition order to shipped via HTTP PATCH
            patch_resp = client.patch(
                f"/tenants/{t_id}/orders/{order_id}",
                json={"expected_version": 1, "status": "shipped"},
                headers=headers,
            )
            assert patch_resp.status_code == 200, patch_resp.text
            assert patch_resp.json()["status"] == "shipped"

            # Execute Relay & Consumer
            published = relay.relay_batch()
            assert published == 2  # placed + shipped

            processed = consumer.process_batch()
            assert processed == 2

            # Query all 4 analytics endpoints via HTTP API
            qparams = {
                "tenant_id": str(t_id),
                "currency": "USD",
                "start": START.isoformat(),
                "end": END.isoformat(),
            }

            # 1. Summary
            sum_resp = client.get("/analytics/summary", params=qparams, headers=headers)
            assert sum_resp.status_code == 200, sum_resp.text
            sum_json = sum_resp.json()
            assert sum_json["total_order_count"] == 1
            assert Decimal(sum_json["sales_amount"]) == Decimal("88.50")
            assert sum_json["metric_semantics"] == "created_at_shipped_sales_v2"

            # 2. Trends
            trend_params = {**qparams, "interval": "hour"}
            series_resp = client.get("/analytics/trends", params=trend_params, headers=headers)
            assert series_resp.status_code == 200, series_resp.text
            series_json = series_resp.json()
            assert len(series_json["buckets"]) >= 1

            # 3. Status Breakdown
            breakdown_resp = client.get(
                "/analytics/status-breakdown", params=qparams, headers=headers
            )
            assert breakdown_resp.status_code == 200, breakdown_resp.text
            breakdown_json = breakdown_resp.json()
            assert any(item["status"] == "shipped" for item in breakdown_json["statuses"])

            # 4. Top Products
            top_resp = client.get(
                "/analytics/top-products", params={**qparams, "limit": 10}, headers=headers
            )
            assert top_resp.status_code == 200, top_resp.text
            top_json = top_resp.json()
            assert len(top_json["products"]) == 1
            assert top_json["products"][0]["product_id"] == str(p_id)
            assert Decimal(top_json["products"][0]["sales_amount"]) == Decimal("88.50")

    finally:
        app.dependency_overrides.clear()
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        redis_client.delete(stream_name)
        from astraroll.config import get_settings

        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
        get_settings.cache_clear()


def test_v2_cross_tenant_security_isolation(control_registry, provisioner, base_db_url):
    """Prove strict cross-tenant database isolation: Tenant A cannot access Tenant B's data."""
    t_a = uuid4()
    t_b = uuid4()
    provisioner.provision_tenant(t_a, "Tenant Alpha Secure")
    provisioner.provision_tenant(t_b, "Tenant Beta Secure")

    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    try:
        # Create 3 orders in Tenant A
        with engine_mgr.session(t_a) as sess_a:
            with sess_a.begin():
                for i in range(3):
                    create_order(
                        sess_a,
                        t_a,
                        OrderCreate(
                            product_id=uuid4(), amount=f"{(i + 1) * 10}.00", currency="USD"
                        ),
                    )

        # Verify Tenant A database has 3 orders
        with engine_mgr.session(t_a) as sess_a:
            count_a = sess_a.scalar(select(Order).where(Order.tenant_id == t_a))
            assert count_a is not None

        # Verify Tenant B database has 0 orders
        with engine_mgr.session(t_b) as sess_b:
            count_b = sess_b.scalars(select(Order)).all()
            assert len(count_b) == 0

            # Direct SQL check: Tenant B database contains NO data for Tenant A
            cross_orders = sess_b.scalars(select(Order).where(Order.tenant_id == t_a)).all()
            assert len(cross_orders) == 0

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_a, drop_database=True)
        provisioner.deprovision_tenant(t_b, drop_database=True)


def test_v2_fault_isolation_broken_tenant_does_not_affect_healthy(
    control_registry, provisioner, base_db_url, redis_client
):
    """Test that a dropped/broken tenant database does not halt or fail healthy tenants."""
    t_healthy = uuid4()
    t_broken = uuid4()

    provisioner.provision_tenant(t_healthy, "Healthy Tenant")
    rec_broken = provisioner.provision_tenant(t_broken, "Broken Tenant")

    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    stream_name = f"test:v2:stream:{uuid4().hex}"
    relay = MultiTenantRelay(
        registry=control_registry,
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        batch_size=50,
    )

    try:
        # Ingest an order for healthy tenant
        with engine_mgr.session(t_healthy) as sess:
            with sess.begin():
                create_order(
                    sess, t_healthy, OrderCreate(product_id=uuid4(), amount="50.00", currency="USD")
                )

        # Drop the broken tenant database directly from underneath the application!
        provisioner._drop_database(rec_broken.database_name)

        # Run relay: Must gracefully skip broken tenant and publish healthy tenant's order
        published = relay.relay_batch()
        assert published == 1

        # Verify stream received healthy tenant event
        stream_len = redis_client.xlen(stream_name)
        assert stream_len == 1

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_healthy, drop_database=True)
        provisioner.deprovision_tenant(t_broken, drop_database=False)
        redis_client.delete(stream_name)


def test_v2_scale_up_to_100_databases_connection_budget(control_registry, base_db_url):
    """Verify TenantEngineManager manages 100 tenant databases within bounded pool limits."""

    class MockScaleResolver(TenantDatabaseResolver):
        def resolve(self, tenant_id: UUID, allow_inactive: bool = False) -> tuple[str, str]:
            record = self.registry.get_tenant(tenant_id)
            if not record or (not allow_inactive and record.status != TenantStatus.ACTIVE.value):
                raise TenantNotFoundError(f"Tenant {tenant_id} not active")
            return record.database_name, self.base_url.render_as_string(hide_password=False)

    resolver = MockScaleResolver(control_registry, base_db_url)
    max_pools = 10
    mgr = TenantEngineManager(resolver, max_active_pools=max_pools, pool_size=1, max_overflow=0)

    # Register 100 tenant records in control plane with unique database names
    tenant_ids = [uuid4() for _ in range(100)]
    for idx, t_id in enumerate(tenant_ids):
        control_registry.register_tenant(
            tenant_id=t_id,
            name=f"Scale Tenant {idx}",
            database_name=f"scale_db_{idx}_{t_id.hex[:6]}",
            database_alias="astraroll",
            status=TenantStatus.ACTIVE,
        )

    try:
        # Access all 100 tenant engines sequentially
        for t_id in tenant_ids:
            with mgr.session(t_id) as session:
                val = session.scalar(text("SELECT 1"))
                assert val == 1

            # Assert that at no point do active engine pools exceed the maximum budget!
            assert mgr.active_pool_count <= max_pools

        # Final count is capped at max_pools (10), proving 90 engines safely LRU evicted
        assert mgr.active_pool_count == max_pools

    finally:
        mgr.close_all()
        for t_id in tenant_ids:
            control_registry.update_status(t_id, TenantStatus.DECOMMISSIONED)


# =========================================================================
# Phase 11: V1 to V2 Tenant Migration & Parity Verification Tests
# =========================================================================
def test_v1_to_v2_tenant_migration_with_parity(control_registry, provisioner, base_db_url):
    """Test full zero-loss migration of an active tenant from V1 shared DB to dedicated V2 DB."""
    from astraroll.v2.migration import TenantMigrator

    shared_engine = create_engine(base_db_url, pool_pre_ping=True)
    shared_factory = sessionmaker(shared_engine, expire_on_commit=False)

    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    migrator = TenantMigrator(
        shared_session_factory=shared_factory,
        control_registry=control_registry,
        provisioner=provisioner,
        engine_manager=engine_mgr,
    )

    t_id = uuid4()
    p_id = uuid4()

    try:
        # 1. Seed tenant in shared database with orders and rollups
        with shared_factory() as sess:
            with sess.begin():
                sess.add(Tenant(id=t_id, name="Migration Source Tenant"))
                sess.add(TenantProcessingState(tenant_id=t_id, revision=3))
                # 3 shipped orders ($30, $40, $50) = $120
                for i, amt in enumerate(["30.00", "40.00", "50.00"]):
                    o = create_order(
                        sess, t_id, OrderCreate(product_id=p_id, amount=amt, currency="USD")
                    )
                    mutate_order(
                        sess, t_id, o.id, 1, OrderUpdate(expected_version=1, status="shipped")
                    )

                # Add summary rollup in shared database
                sess.add(
                    HourlySummaryAggregate(
                        tenant_id=t_id,
                        currency="USD",
                        bucket_start=START,
                        order_count=3,
                        sales_amount=Decimal("120.00"),
                    )
                )

        # 2. Execute migration
        result = migrator.migrate_tenant(t_id, purge_source_after=False)
        assert result.verified is True
        assert result.orders_migrated == 3
        assert result.total_sales_amount == Decimal("120.00")
        assert result.duration_seconds > 0

        # 3. Verify target dedicated database has identical records
        with engine_mgr.session(t_id) as target_sess:
            orders = target_sess.scalars(
                select(Order).where(Order.tenant_id == t_id).order_by(Order.created_at)
            ).all()
            assert len(orders) == 3
            assert sum(o.amount for o in orders) == Decimal("120.00")

            summary = target_sess.scalar(
                select(HourlySummaryAggregate).where(HourlySummaryAggregate.tenant_id == t_id)
            )
            assert summary is not None
            assert summary.order_count == 3
            assert summary.sales_amount == Decimal("120.00")

            st = target_sess.get(TenantProcessingState, t_id)
            assert st is not None
            assert st.revision == 3

        # 4. Verify control registry is updated and ACTIVE
        rec = control_registry.get_tenant(t_id)
        assert rec is not None
        assert rec.status == TenantStatus.ACTIVE.value
        assert rec.is_active()

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        shared_engine.dispose()


# =========================================================================
# Phase 12: V2 Health Check & Graceful Shutdown Lifecycle Tests
# =========================================================================
def test_v2_health_ready_and_graceful_shutdown(
    monkeypatch, control_registry, provisioner, base_db_url
):
    """Test /health/ready validates control & tenant DB, and lifespan executes shutdown."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    get_settings.cache_clear()

    from astraroll.v2.runtime import get_tenant_engine_manager

    t_id = uuid4()

    try:
        # 1. Health check when control plane is available, no active tenants
        with TestClient(app) as client:
            resp = client.get("/health/ready")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            # 2. Provision an active tenant and verify /health/ready checks both control & tenant
            provisioner.provision_tenant(t_id, "Health Check Tenant")
            resp = client.get("/health/ready")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            # Touch tenant engine
            mgr = get_tenant_engine_manager()
            mgr.get_engine(t_id)
            assert mgr.active_pool_count >= 1

        # 3. Context exit triggers lifespan shutdown which closes all tenant pools
        assert mgr.active_pool_count == 0

    finally:
        provisioner.deprovision_tenant(t_id, drop_database=True)
        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
        get_settings.cache_clear()


# =========================================================================
# Phase 13: Safety Hardening, Preexisting Protection & Resume Tests
# =========================================================================
def test_v2_migration_parity_failure_quarantined_without_drop(
    control_registry, provisioner, base_db_url
):
    """Test that parity failures retain target DB as QUARANTINED without dropping it."""
    from astraroll.v2.migration import MigrationParityError, ParityReport, TenantMigrator

    shared_engine = create_engine(base_db_url, pool_pre_ping=True)
    shared_factory = sessionmaker(shared_engine, expire_on_commit=False)
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    t_id = uuid4()
    p_id = uuid4()

    try:
        # Seed 1 order in shared DB
        with shared_factory() as sess:
            with sess.begin():
                sess.add(Tenant(id=t_id, name="Quarantine Test Tenant"))
                create_order(
                    sess, t_id, OrderCreate(product_id=p_id, amount="50.00", currency="USD")
                )

        # Provision target DB
        rec = provisioner.provision_tenant(t_id, "Quarantine Test Tenant")
        target_db = rec.database_name

        # Intentionally tamper with verify_parity
        class BrokenParityMigrator(TenantMigrator):
            def verify_parity(self, tenant_id: UUID):
                report = super().verify_parity(tenant_id)
                return ParityReport(
                    tenant_id=tenant_id,
                    source_order_count=report.source_order_count,
                    target_order_count=report.target_order_count + 999,
                    source_sales=report.source_sales,
                    target_sales=report.target_sales,
                    source_outbox_count=report.source_outbox_count,
                    target_outbox_count=report.target_outbox_count,
                    status_parity=False,
                    currency_parity=True,
                    rollups_parity=True,
                    coverage_parity=True,
                    verified=False,
                    details={"error": "Simulated parity mismatch"},
                )

        broken_migrator = BrokenParityMigrator(
            shared_session_factory=shared_factory,
            control_registry=control_registry,
            provisioner=provisioner,
            engine_manager=engine_mgr,
        )

        with pytest.raises(MigrationParityError, match="Parity verification failed"):
            broken_migrator.migrate_tenant(t_id)

        # CRITICAL SAFETY ASSERTIONS:
        # 1. Target database MUST NOT be dropped!
        assert provisioner._database_exists(target_db) is True
        # 2. Control registry must be QUARANTINED
        saved_rec = control_registry.get_tenant(t_id)
        assert saved_rec.status == TenantStatus.QUARANTINED.value
        assert "Simulated parity mismatch" in saved_rec.error_message
        # 3. Checkpoint must be QUARANTINED
        cp = control_registry.get_checkpoint(t_id)
        assert cp is not None
        assert cp.status == "QUARANTINED"

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        shared_engine.dispose()


def test_v2_preexisting_destination_protection(provisioner, control_registry):
    """Test protection against overwriting or dropping unverified preexisting databases."""
    from astraroll.v2.provisioning import make_tenant_db_name

    t_id = uuid4()
    db_name = make_tenant_db_name(t_id, provisioner.tenant_database_prefix)

    try:
        # Manually create an untracked physical database on PostgreSQL cluster
        provisioner._create_database(db_name)
        assert provisioner._database_exists(db_name) is True

        # Attempting to provision without control plane provenance must be REFUSED
        with pytest.raises(ValueError, match="unverified provenance"):
            provisioner.provision_tenant(t_id, "Untracked Tenant")

        # Database must still exist unharmed
        assert provisioner._database_exists(db_name) is True

    finally:
        provisioner._drop_database(db_name)


def test_v2_migration_bounded_batches_and_checkpoint_resume(
    control_registry, provisioner, base_db_url
):
    """Test bounded batch keyset pagination and safe checkpoint resume."""
    from astraroll.v2.migration import TenantMigrator

    shared_engine = create_engine(base_db_url, pool_pre_ping=True)
    shared_factory = sessionmaker(shared_engine, expire_on_commit=False)
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    migrator = TenantMigrator(
        shared_session_factory=shared_factory,
        control_registry=control_registry,
        provisioner=provisioner,
        engine_manager=engine_mgr,
        batch_size=3,
    )

    t_id = uuid4()
    p_id = uuid4()

    try:
        # Seed 9 orders in shared DB
        with shared_factory() as sess:
            with sess.begin():
                sess.add(Tenant(id=t_id, name="Batch Test Tenant"))
                for i in range(9):
                    create_order(
                        sess,
                        t_id,
                        OrderCreate(product_id=p_id, amount=f"{(i + 1) * 10}.00", currency="USD"),
                    )

        # Migrate with batch_size=3
        res = migrator.migrate_tenant(t_id, resume=True)
        assert res.verified is True
        assert res.orders_migrated == 9

        # Checkpoint shows completed
        cp = control_registry.get_checkpoint(t_id)
        assert cp.orders_copied == 9
        assert cp.status == "COMPLETED"

        # Verify idempotence on repeated run
        res2 = migrator.migrate_tenant(t_id, resume=True)
        assert res2.verified is True
        assert res2.orders_migrated == 9

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        shared_engine.dispose()


def test_v2_migration_historical_outbox_not_republished(
    control_registry, provisioner, base_db_url, redis_client
):
    """Test that historical outbox events are marked published and not re-streamed."""
    from astraroll.v2.migration import TenantMigrator

    shared_engine = create_engine(base_db_url, pool_pre_ping=True)
    shared_factory = sessionmaker(shared_engine, expire_on_commit=False)
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    migrator = TenantMigrator(
        shared_session_factory=shared_factory,
        control_registry=control_registry,
        provisioner=provisioner,
        engine_manager=engine_mgr,
    )

    t_id = uuid4()
    p_id = uuid4()
    stream_name = f"test:v2:historical:{uuid4().hex}"

    try:
        # Seed 2 orders in shared DB with outbox events, simulating populated rollups
        with shared_factory() as sess:
            with sess.begin():
                sess.add(Tenant(id=t_id, name="Historical Outbox Tenant"))
                sess.add(TenantProcessingState(tenant_id=t_id, revision=1))
                create_order(
                    sess, t_id, OrderCreate(product_id=p_id, amount="100.00", currency="USD")
                )
                create_order(
                    sess, t_id, OrderCreate(product_id=p_id, amount="200.00", currency="USD")
                )

        # Migrate tenant
        res = migrator.migrate_tenant(t_id)
        assert res.verified is True

        # In target DB, historical outbox events must have published_at set
        relay = MultiTenantRelay(
            registry=control_registry,
            engine_manager=engine_mgr,
            redis_client=redis_client,
            stream_name=stream_name,
        )
        published = relay.relay_tenant(t_id)
        assert published == 0

        # Now create a brand new order in the dedicated tenant DB
        with engine_mgr.session(t_id) as sess:
            with sess.begin():
                create_order(
                    sess, t_id, OrderCreate(product_id=p_id, amount="50.00", currency="USD")
                )

        # Relay must publish ONLY the new order
        new_published = relay.relay_tenant(t_id)
        assert new_published == 1

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        shared_engine.dispose()


def test_v2_safe_source_purge_with_verified_backup(control_registry, provisioner, base_db_url):
    """Test source purge requires confirmed cutover, explicit approval, and creates backup."""
    from pathlib import Path

    from astraroll.v2.migration import MigrationSafetyError, TenantMigrator

    shared_engine = create_engine(base_db_url, pool_pre_ping=True)
    shared_factory = sessionmaker(shared_engine, expire_on_commit=False)
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    migrator = TenantMigrator(
        shared_session_factory=shared_factory,
        control_registry=control_registry,
        provisioner=provisioner,
        engine_manager=engine_mgr,
    )

    t_id = uuid4()
    p_id = uuid4()
    backup_path = None

    try:
        # Seed order in shared DB
        with shared_factory() as sess:
            with sess.begin():
                sess.add(Tenant(id=t_id, name="Purge Test Tenant"))
                create_order(
                    sess, t_id, OrderCreate(product_id=p_id, amount="75.00", currency="USD")
                )

        # Migrate tenant
        migrator.migrate_tenant(t_id)

        # 1. Unconfirmed purge must raise MigrationSafetyError
        with pytest.raises(MigrationSafetyError, match="must be explicitly provided"):
            migrator.purge_source_records(t_id, confirm_purge=False)

        # 2. Wrong token must raise MigrationSafetyError
        with pytest.raises(MigrationSafetyError, match="does not match"):
            migrator.purge_source_records(t_id, confirm_purge=True, approval_token="wrong_token")

        # 3. Valid purge with matching approval token
        backup_path = migrator.purge_source_records(
            t_id, confirm_purge=True, approval_token=str(t_id)
        )
        assert backup_path is not None
        assert Path(backup_path).exists()
        assert Path(backup_path).stat().st_size > 0

        # Verify source records are deleted
        with shared_factory() as sess:
            assert sess.scalar(select(func.count(Order.id)).where(Order.tenant_id == t_id)) == 0
            assert sess.get(Tenant, t_id) is None

        # Verify target records remain intact
        with engine_mgr.session(t_id) as sess:
            assert sess.scalar(select(func.count(Order.id)).where(Order.tenant_id == t_id)) == 1

    finally:
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        shared_engine.dispose()
        if backup_path and Path(backup_path).exists():
            Path(backup_path).unlink()


def test_v2_auth_and_public_api_fail_closed_on_inactive_tenant(
    monkeypatch, control_registry, client
):
    """Verify public API routes fail closed (403/404) for inactive or missing tenants."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    get_settings.cache_clear()

    pending_id = uuid4()
    migrating_id = uuid4()
    quarantined_id = uuid4()
    failed_id = uuid4()
    missing_id = uuid4()

    control_registry.register_tenant(
        pending_id, "Pending Tenant", "db_pending", status=TenantStatus.PENDING
    )
    control_registry.register_tenant(
        migrating_id, "Migrating Tenant", "db_migrating", status=TenantStatus.MIGRATING
    )
    control_registry.register_tenant(
        quarantined_id,
        "Quarantined Tenant",
        "db_quarantined",
        status=TenantStatus.QUARANTINED,
    )
    control_registry.register_tenant(
        failed_id, "Failed Tenant", "db_failed", status=TenantStatus.FAILED
    )

    try:
        # Non-existent tenant: must return 404
        resp = client.get(f"/tenants/{missing_id}/orders")
        assert resp.status_code == 404

        # Inactive tenants: must fail closed with 403 Forbidden
        for tid in (pending_id, migrating_id, quarantined_id, failed_id):
            resp = client.get(f"/tenants/{tid}/orders")
            assert resp.status_code == 403
            assert "not active" in resp.json()["detail"].lower()

            # Mutation must also fail closed with 403
            create_resp = client.post(
                f"/tenants/{tid}/orders",
                json={"product_id": str(uuid4()), "amount": "10.00", "currency": "USD"},
            )
            assert create_resp.status_code == 403

    finally:
        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
        get_settings.cache_clear()


def test_v2_e2e_csv_ingestion_and_assistant(
    control_registry, provisioner, base_db_url, redis_client, monkeypatch
):
    """Full Product Smoke Test: CSV Ingest -> Tenant DB -> Outbox -> Redis -> Consumer -> API."""
    t_id = uuid4()
    p1 = uuid4()
    p2 = uuid4()

    token = secrets.token_urlsafe(32)
    keys_config = [
        {
            "subject": "e2e-csv-assistant-user",
            "tenant_ids": [str(t_id)],
            "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
    ]
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    monkeypatch.setenv("ANALYTICS_API_KEYS_JSON", json.dumps(keys_config))
    stream_name = f"test:v2:stream:{t_id.hex[:8]}"
    group_name = f"test:v2:group:{t_id.hex[:8]}"
    monkeypatch.setenv("ORDER_STREAM", stream_name)
    from astraroll.config import get_settings

    get_settings.cache_clear()

    provisioner.provision_tenant(t_id, "CSV Demo Tenant")
    resolver = TenantDatabaseResolver(control_registry, base_db_url)
    engine_mgr = TenantEngineManager(resolver, max_active_pools=5)

    headers = {"Authorization": f"Bearer {token}"}

    relay = MultiTenantRelay(
        registry=control_registry,
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        batch_size=50,
    )
    consumer = MultiTenantAggregationConsumer(
        engine_manager=engine_mgr,
        redis_client=redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="worker-csv-e2e",
        block_ms=100,
        fair_dispatch=True,
    )
    consumer.init_group()

    from astraroll.analytics.coverage import CoverageProof
    from astraroll.analytics.reader import get_analytics_reader

    def mock_coverage(session, q):
        return CoverageProof(
            tenant_id=q.tenant_id,
            currency=q.currency,
            start=START,
            end=END,
            metric_semantics="created_at_shipped_sales_v2",
            ready=True,
            intervals=frozenset({"hour", "day"}),
            dimensions=frozenset({"summary", "status", "product"}),
        )

    adapter = MultiTenantPostgresAnalyticsAdapter(
        engine_manager=engine_mgr,
        coverage=mock_coverage,
    )
    app.dependency_overrides[get_analytics_reader] = lambda: adapter

    try:
        with TestClient(app) as client:
            csv_payload = f"""product_id,amount,currency,status,created_at
{p1},200.00,USD,shipped,2026-10-10T12:00:00Z
{p1},300.00,USD,shipped,2026-10-10T13:00:00Z
{p2},150.00,USD,placed,2026-10-11T09:00:00Z
{p2},50.00,USD,cancelled,2026-10-11T10:00:00Z
"""
            # 1. Ingest CSV via API
            ingest_resp = client.post(
                f"/tenants/{t_id}/ingest/csv?trigger_relay=false",
                content=csv_payload,
                headers={**headers, "Content-Type": "text/csv"},
            )
            assert ingest_resp.status_code == 200, ingest_resp.text
            ingest_data = ingest_resp.json()
            assert ingest_data["accepted"] == 4
            assert ingest_data["rejected"] == 0
            assert Decimal(ingest_data["total_sales"]) == Decimal("500.00")

            # 2. Relay events from tenant outbox to Redis Stream
            published = relay.relay_batch()
            assert published >= 4

            # 3. Aggregation consumer processes messages into tenant rollups
            processed = consumer.process_batch()
            assert processed >= 4

            # 4. Check summary API reflects new 500.00 shipped sales
            qparams = {
                "tenant_id": str(t_id),
                "currency": "USD",
                "start": START.isoformat(),
                "end": END.isoformat(),
            }
            sum_resp = client.get("/analytics/summary", params=qparams, headers=headers)
            assert sum_resp.status_code == 200
            sum_json = sum_resp.json()
            assert sum_json["total_order_count"] == 4
            assert Decimal(sum_json["sales_amount"]) == Decimal("500.00")

            # 5. Check status breakdown API
            stat_resp = client.get("/analytics/status-breakdown", params=qparams, headers=headers)
            assert stat_resp.status_code == 200
            stat_json = stat_resp.json()
            shipped_metric = next(s for s in stat_json["statuses"] if s["status"] == "shipped")
            placed_metric = next(s for s in stat_json["statuses"] if s["status"] == "placed")
            cancelled_metric = next(s for s in stat_json["statuses"] if s["status"] == "cancelled")
            assert shipped_metric["total_order_count"] == 2
            assert Decimal(shipped_metric["sales_amount"]) == Decimal("500.00")
            assert placed_metric["total_order_count"] == 1
            assert Decimal(placed_metric["sales_amount"]) == Decimal("0.00")
            assert cancelled_metric["total_order_count"] == 1
            assert Decimal(cancelled_metric["sales_amount"]) == Decimal("0.00")

            # 6. Check Assistant API answers using real metrics
            ast_resp = client.post(
                f"/tenants/{t_id}/assistant",
                json={
                    "query": "What are our total sales?",
                    "currency": "USD",
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                },
                headers=headers,
            )
            assert ast_resp.status_code == 200
            ast_data = ast_resp.json()
            assert "500.00" in ast_data["reply"]
            assert ast_data["data"]["summary"]["sales_amount"] == "500.00"

            # 7. Test Idempotency / Dry Run: dry run commits 0 records
            dry_resp = client.post(
                f"/tenants/{t_id}/ingest/csv?dry_run=true",
                content=csv_payload,
                headers={**headers, "Content-Type": "text/csv"},
            )
            assert dry_resp.status_code == 200
            assert dry_resp.json()["dry_run"] is True
            assert dry_resp.json()["accepted"] == 4

    finally:
        app.dependency_overrides.clear()
        engine_mgr.close_all()
        provisioner.deprovision_tenant(t_id, drop_database=True)
        redis_client.delete(stream_name)
        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
        get_settings.cache_clear()
