"""Release-specific durability, security and historical migration regressions."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import migrate
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text, update

from astraroll.aggregation.bootstrap import activate_job, bootstrap, stage_batch, start_job
from astraroll.aggregation.coverage import read_coverage
from astraroll.aggregation.models import HourlySummaryAggregate, OrderProjection
from astraroll.aggregation.release_models import AggregateCoverage, BootstrapJob, RebuildArchive
from astraroll.analytics import auth
from astraroll.analytics.contracts import AnalyticsQuery
from astraroll.analytics.reader import AnalyticsUnavailable
from astraroll.api import app, session_dependency
from astraroll.config import Settings
from astraroll.models import Order, OutboxEvent, Tenant
from astraroll.operations import reconcile
from astraroll.orders import create_order, mutate_order
from astraroll.schemas import OrderCreate, OrderUpdate

pytestmark = pytest.mark.integration
START = datetime(2026, 10, 31, tzinfo=UTC)
END = START + timedelta(days=2)


def source(factory, tenant, *, amount="10.00", ship=False):
    with factory.begin() as session:
        order = create_order(
            session, tenant, OrderCreate(product_id=uuid4(), amount=amount, currency="USD")
        )
        if ship:
            mutate_order(
                session, tenant, order.id, 1, OrderUpdate(expected_version=1, status="shipped")
            )
        return order.id


def query(tenant):
    return AnalyticsQuery(tenant_id=tenant, currency="USD", start=START, end=END)


def test_interrupted_bootstrap_resume_live_changes_and_archive(factory, tenant_id):
    order = source(factory, tenant_id)
    job = start_job(factory, tenant_id, ["USD", "EUR"], START, END)
    assert stage_batch(factory, job, 1) == 1
    with factory() as session:
        assert session.get(BootstrapJob, job).copied == 1
        with pytest.raises(AnalyticsUnavailable):
            read_coverage(session, query(tenant_id))
    # Change an already-enumerated UUID; add another UUID behind or ahead of cursor.
    with factory.begin() as session:
        mutate_order(
            session,
            tenant_id,
            order,
            1,
            OrderUpdate(expected_version=1, amount="25.00", status="shipped"),
        )
    source(factory, tenant_id, ship=True)
    bootstrap(factory, tenant_id, ["USD", "EUR"], START, END, 1, job)
    assert reconcile(factory, tenant_id)["status"] == "match"
    with factory() as session:
        assert read_coverage(session, query(tenant_id)).ready
        assert session.scalar(
            select(func.sum(HourlySummaryAggregate.sales_amount)).where(
                HourlySummaryAggregate.tenant_id == tenant_id
            )
        ) == Decimal("35.00")
        source_count = session.scalar(
            select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id)
        )
        outbox_count = session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
        )
        assert source_count == 2 and outbox_count == 4
    # Explicit repair preserves historical derived rows and source/outbox history.
    with factory.begin() as session:
        session.execute(
            update(HourlySummaryAggregate)
            .where(HourlySummaryAggregate.tenant_id == tenant_id)
            .values(sales_amount=999)
        )
        projection = session.get(OrderProjection, (tenant_id, order))
        projection.canonical_snapshot = {"corrupt": True}
    assert reconcile(factory, tenant_id)["status"] == "drift"
    with factory() as session:
        with pytest.raises(AnalyticsUnavailable):
            read_coverage(session, query(tenant_id))
    repaired = bootstrap(factory, tenant_id, ["USD", "EUR"], START, END)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RebuildArchive)
                .where(RebuildArchive.job_id == repaired)
            )
            > 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.tenant_id == tenant_id)
            )
            == outbox_count
        )
        assert (
            session.get(OrderProjection, (tenant_id, order)).canonical_snapshot["status"]
            == "shipped"
        )
    assert reconcile(factory, tenant_id)["status"] == "match"


def test_activation_failure_rolls_back_and_can_resume(factory, tenant_id, monkeypatch):
    source(factory, tenant_id, ship=True)
    job = start_job(factory, tenant_id, ["USD"], START, END)
    while stage_batch(factory, job, 1):
        pass
    from astraroll.aggregation import bootstrap as module

    original = module.validate_rollups

    def fail(*args):
        raise ValueError("injected oracle failure")

    monkeypatch.setattr(module, "validate_rollups", fail)
    with pytest.raises(ValueError, match="oracle failure"):
        activate_job(factory, job)
    with factory() as session:
        assert session.get(BootstrapJob, job).status == "catchup"
        assert session.get(AggregateCoverage, (tenant_id, "USD")).status == "building"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OrderProjection)
                .where(OrderProjection.tenant_id == tenant_id)
            )
            == 0
        )
    monkeypatch.setattr(module, "validate_rollups", original)
    activate_job(factory, job)
    assert reconcile(factory, tenant_id)["status"] == "match"


def test_empty_stale_semantics_and_lagging_coverage(factory, tenant_id):
    bootstrap(factory, tenant_id, ["USD"], START, END)
    with factory() as session:
        assert read_coverage(session, query(tenant_id)).ready
    source(factory, tenant_id)
    assert reconcile(factory, tenant_id) == {
        "status": "lagging",
        "unmatched_orders": 1,
        "compared": False,
    }
    with factory.begin() as session:
        session.get(AggregateCoverage, (tenant_id, "USD")).verified_at = START - timedelta(
            days=1000
        )
    with factory() as session:
        with pytest.raises(AnalyticsUnavailable, match="stale"):
            read_coverage(session, query(tenant_id))
    with factory.begin() as session:
        c = session.get(AggregateCoverage, (tenant_id, "USD"))
        c.verified_at = datetime.now(UTC)
        c.metric_semantics = "legacy"
    with factory() as session:
        with pytest.raises(AnalyticsUnavailable):
            read_coverage(session, query(tenant_id))


def test_forward_upgrade_preserves_historical_totals_without_certifying(migration_engine):
    migrate(migration_engine, "0003_aggregation")
    tenant = uuid4()
    with migration_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants(id,name) VALUES (:id,'Legacy totals')"), {"id": tenant}
        )
        conn.execute(
            text("""INSERT INTO hourly_summary_aggregates
            (tenant_id,currency,bucket_start,order_count,sales_amount)
            VALUES (:id,'USD',:at,1,100)"""),
            {"id": tenant, "at": START},
        )
    migrate(migration_engine, "0004_coverage_precision")
    with migration_engine.connect() as conn:
        assert conn.scalar(text("SELECT sales_amount FROM hourly_summary_aggregates")) == 100
        assert conn.scalar(text("SELECT count(*) FROM aggregate_coverage")) == 0
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0004_coverage_precision"
        )
    col = next(
        c
        for c in inspect(migration_engine).get_columns("hourly_summary_aggregates")
        if c["name"] == "sales_amount"
    )
    assert col["type"].precision == 38
    with pytest.raises(RuntimeError, match="forward-only"):
        migrate(migration_engine, "0003_aggregation", downgrade=True)


def test_all_producer_routes_enforce_real_identity(factory, monkeypatch):
    a, b = uuid4(), uuid4()
    with factory.begin() as session:
        session.add_all([Tenant(id=a, name="Auth A"), Tenant(id=b, name="Auth B")])
    order = source(factory, b)
    token = secrets.token_urlsafe(32)
    record = {
        "subject": "member-a",
        "tenant_ids": [str(a)],
        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }

    def configure(records):
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: Settings(_env_file=None, analytics_api_keys_json=json.dumps(records)),
        )

    configure([record])
    previous = app.dependency_overrides.copy()
    calls = []

    def database():
        calls.append(True)
        with factory() as session:
            yield session

    app.dependency_overrides[session_dependency] = database
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            for method, path, body in (
                ("get", f"/tenants/{b}", None),
                ("get", f"/tenants/{b}/orders", None),
                ("get", f"/tenants/{b}/orders/{order}", None),
                (
                    "post",
                    f"/tenants/{b}/orders",
                    {"product_id": str(uuid4()), "amount": "1.00", "currency": "USD"},
                ),
                (
                    "patch",
                    f"/tenants/{b}/orders/{order}",
                    {"expected_version": 1, "amount": "2.00"},
                ),
                ("post", f"/tenants/{b}/orders/{order}/cancel", {"expected_version": 1}),
                ("post", "/tenants", {"name": "Unauthorized"}),
                ("get", f"/ops/metrics?tenant_id={b}", None),
            ):
                kwargs = {"json": body} if body is not None else {}
                response = client.request(method, path, headers=headers, **kwargs)
                assert response.status_code == 403, response.text
                assert not calls
                for bad in ({}, {"Authorization": "Basic bad"}, {"Authorization": "Bearer wrong"}):
                    assert client.request(method, path, headers=bad, **kwargs).status_code == 401
                    assert not calls
            assert client.get(f"/tenants/{a}", headers=headers).status_code == 200
            calls.clear()
            for records in ([{**record, "expires_at": START.isoformat()}], []):
                configure(records)
                assert client.get(f"/tenants/{a}", headers=headers).status_code in (401, 503)
                assert not calls
            configure([{**record, "is_admin": True}])
            assert (
                client.post("/tenants", json={"name": "Authorized"}, headers=headers).status_code
                == 201
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_bounded_sql_workload_has_complete_valid_history(factory):
    from astraroll.schemas import OrderEvent
    from astraroll.workload import seed_tenant

    namespace = uuid4()
    tenant = seed_tenant(factory, namespace, 0, 31, 7)
    with factory() as session:
        histories = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.tenant_id == tenant)
            .order_by(OutboxEvent.order_id, OutboxEvent.order_version)
        ).all()
        by_order = {}
        for item in histories:
            payload = OrderEvent.model_validate(item.payload)
            assert isinstance(item.payload["after"]["amount"], str)
            by_order.setdefault(item.order_id, []).append(payload)
        assert len(by_order) == 31
        for order_id, versions in by_order.items():
            assert versions[0].order_version == 1 and versions[0].after.status == "placed"
            assert versions[0].before is None
            if len(versions) == 2:
                assert versions[1].before == versions[0].after
                assert versions[1].after.status in ("shipped", "cancelled")
            assert versions[-1].after.version == session.get(Order, order_id).version
    seed_tenant(factory, namespace, 0, 31, 7)
    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.tenant_id == tenant)
        ) == len(histories)


def test_activation_fences_concurrent_writer_then_live_consumer_converges(
    factory, tenant_id, redis_client, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from astraroll.aggregation import bootstrap as module
    from astraroll.aggregation.consumer import AggregationConsumer
    from astraroll.aggregation.replay import replay_outbox_events

    source(factory, tenant_id, ship=True)
    job = start_job(factory, tenant_id, ["USD"], START, END)
    while stage_batch(factory, job, 1):
        pass
    original = module.validate_rollups
    started = Event()
    future = []
    with ThreadPoolExecutor(1) as pool:

        def write():
            started.set()
            return source(factory, tenant_id, ship=True)

        def during_activation(session, tenant):
            future.append(pool.submit(write))
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future[0].result(timeout=0.1)
            original(session, tenant)

        monkeypatch.setattr(module, "validate_rollups", during_activation)
        activate_job(factory, job)
        future[0].result(timeout=5)
    assert reconcile(factory, tenant_id)["status"] == "lagging"
    stream, group = f"test:cutover:{uuid4()}", f"test:cutover-group:{uuid4()}"
    replay_outbox_events(factory, redis_client, stream, tenant_id=tenant_id)
    worker = AggregationConsumer(
        factory, redis_client, stream_name=stream, group_name=group, block_ms=1
    )
    worker.init_group()
    while worker.process_batch():
        pass
    assert reconcile(factory, tenant_id)["status"] == "match"
    with factory() as session:
        assert (
            session.scalar(
                select(func.sum(HourlySummaryAggregate.order_count)).where(
                    HourlySummaryAggregate.tenant_id == tenant_id
                )
            )
            == 2
        )


def test_admin_metrics_distinguishes_missing_stream_and_unapplied_source(
    factory, tenant_id, redis_client, monkeypatch
):
    from astraroll import operations
    from astraroll.analytics.auth import AuthenticatedPrincipal, get_authenticated_principal

    source(factory, tenant_id)
    stream, group = f"test:ops:{uuid4()}", f"test:ops-group:{uuid4()}"
    monkeypatch.setattr(operations, "get_session_factory", lambda: factory)
    original = operations.get_settings()
    monkeypatch.setattr(
        operations, "get_settings", lambda: original.model_copy(update={"order_stream": stream})
    )
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_authenticated_principal] = lambda: AuthenticatedPrincipal(
        subject="ops-test", tenant_ids=frozenset(), is_admin=True
    )
    try:
        with TestClient(app) as client:
            response = client.get("/ops/metrics", params={"tenant_id": str(tenant_id)})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["redis"]["status"] == "reachable"
            assert body["redis"]["stream_exists"] is False
            assert body["source_lag"]["unmatched_orders"] == 1
            assert body["processing"] is None and body["coverage"] == []
            redis_client.xgroup_create(stream, group, id="0-0", mkstream=True)
            redis_client.xadd(stream, {"event": "{}"})
            redis_client.xreadgroup(group, "ops-pending", {stream: ">"}, count=1)
            body = client.get("/ops/metrics", params={"tenant_id": str(tenant_id)}).json()
            assert body["redis"]["stream_exists"] is True
            assert body["redis"]["oldest_pending"][0]["oldest_pending_stream_age_seconds"] >= 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
