"""Safe isolated-schema tests: no V2 provisioning or database deletion."""

import hashlib
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from conftest import migrate
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from astraroll.aggregation.bootstrap import bootstrap
from astraroll.analytics.auth import get_authenticated_principal
from astraroll.api import app
from astraroll.config import get_settings
from astraroll.db import get_engine
from astraroll.v2.scale_live import live_probe
from astraroll.v2.scale_validation import (
    assert_snapshot,
    compare_api,
    create_plan,
    digest_row,
    snapshot,
    source_schema,
    validate_target,
)
from astraroll.workload import END, START, seed_tenant


@pytest.fixture
def scale_fixture(factory):
    base = get_engine()
    schema = f"bench_{uuid4().hex}"
    with base.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        base.url, connect_args={"options": f"-csearch_path={schema} -c timezone=UTC"}
    )
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    tenant = seed_tenant(factory, uuid4(), 0, 12, 5)
    yield engine, factory, schema, tenant
    engine.dispose()  # Retain this small test schema, like existing release tests.


def test_source_identifier_and_canonical_digest():
    for unsafe in ("public", 'bench_x"; DROP DATABASE postgres', "bench_123"):
        with pytest.raises(ValueError):
            source_schema(unsafe)
    a, b = hashlib.sha256(), hashlib.sha256()
    digest_row(a, {"b": 1, "a": 2})
    digest_row(b, {"a": 2, "b": 1})
    assert a.digest() == b.digest()


def test_forward_0005_preserves_complete_payloads(migration_engine):
    migrate(migration_engine, "0004_coverage_precision")
    tenant = seed_tenant(sessionmaker(migration_engine), uuid4(), 0, 5, 2)
    with migration_engine.connect() as c:
        before = c.execute(text("SELECT id,payload FROM outbox_events ORDER BY id")).all()
    migrate(migration_engine)
    with migration_engine.connect() as c:
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version")) == "0005_tenant_access_paths"
        )
        assert c.execute(text("SELECT id,payload FROM outbox_events ORDER BY id")).all() == before
        assert c.scalar(text("SELECT count(*) FROM orders WHERE tenant_id=:t"), {"t": tenant}) == 5
        names = set(
            c.scalars(text("SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"))
        )
        assert {"ix_orders_tenant_id_keyset", "ix_outbox_tenant_order_version"} <= names


def test_independent_normal_probe_clock_and_samples(scale_fixture, tmp_path):
    import json

    from astraroll.v2.scale_load import normal_probe

    engine, _, schema, tenant = scale_fixture
    with engine.connect() as c:
        order = str(c.scalar(text("SELECT id FROM orders LIMIT 1")))
    (tmp_path / "go").touch()
    normal_probe(
        {
            "schema": schema,
            "tenant": str(tenant),
            "order": order,
            "seconds": 0.2,
            "normal_rate": 20,
        },
        tmp_path,
    )
    result = json.loads((tmp_path / "normal.json").read_text())
    assert result["idle"]["samples"] == result["loaded"]["samples"] == 4
    assert all(v > 0 for v in result["loaded"]["raw_ms"])


def test_consistent_plan_exact_subset_and_changed_snapshot(scale_fixture):
    engine, factory, schema, tenant = scale_fixture
    plan = create_plan(engine.url, schema, 1, 10)
    assert plan["target_orders"] == 10
    assert plan["source_orders"] == 12
    assert plan["new_historical_orders"] == 0
    expected = plan["tenants"][0]
    with engine.connect() as c:
        assert_snapshot(expected, snapshot(c, schema, tenant, 10))
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE orders SET amount=amount+1 WHERE id="
                "(SELECT id FROM orders ORDER BY id LIMIT 1)"
            )
        )
    with engine.connect() as c:
        with pytest.raises(ValueError, match="orders_sha256"):
            assert_snapshot(expected, snapshot(c, schema, tenant, 10))
    with pytest.raises(ValueError, match="shortfall"):
        create_plan(engine.url, schema, 1, 13)


def test_missing_history_and_uncertified_coverage_fail_closed(scale_fixture):
    engine, factory, schema, tenant = scale_fixture
    with engine.connect() as c:
        expected = snapshot(c, schema, tenant, 12)
        with pytest.raises(ValueError, match="Oracle mismatch"):
            validate_target(c, expected, schema)
    bootstrap(factory, tenant, ["USD", "EUR"], START, END, 5)
    with engine.connect() as c:
        assert validate_target(c, expected, schema)["projections"].startswith("exact")
    with engine.begin() as c:
        c.execute(text("UPDATE order_projections SET last_applied_version=999"))
    with engine.connect() as c:
        with pytest.raises(ValueError, match="projection"):
            validate_target(c, expected, schema)


def test_sql_oracles_exact_decimal_and_currency(scale_fixture):
    engine, _, _, tenant = scale_fixture
    params = {"tenant": tenant, "currency": "USD", "start": START, "end": END}
    with engine.connect() as c:
        sales, count = c.execute(
            text(
                "SELECT sum(CASE WHEN status='shipped' THEN amount ELSE 0 END),"
                "count(*) FROM orders WHERE currency='USD'"
            )
        ).one()
        body = {
            "start": START.isoformat(),
            "end": END.isoformat(),
            "tenant_id": str(tenant),
            "currency": "USD",
            "metric_semantics": "created_at_shipped_sales_v2",
            "total_order_count": count,
            "sales_amount": str(sales),
        }
        compare_api(c, body, "summary", params)
        with pytest.raises(ValueError, match="mismatch"):
            compare_api(c, {**body, "sales_amount": "999.99"}, "summary", params)
        with pytest.raises(ValueError, match="currency"):
            compare_api(c, {**body, "currency": "EUR"}, "summary", params)


def test_live_recovery_with_real_bearer_and_cross_tenant_denial(
    scale_fixture,
    redis_client,
    monkeypatch,
):
    engine, factory, _, tenant = scale_fixture
    bootstrap(factory, tenant, ["USD", "EUR"], START, END, 5)

    class Manager:
        def get_engine(self, selected, **kwargs):
            assert UUID(str(selected)) == tenant
            return engine

        @contextmanager
        def session(self, selected, **kwargs):
            assert UUID(str(selected)) == tenant
            with factory() as session:
                yield session

        def close_all(self):
            pass

    import json
    from datetime import UTC, datetime, timedelta

    from astraroll.v2 import runtime

    manager = Manager()
    token = "isolated-schema-test-credential"
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "subject": "scale-test",
                    "tenant_ids": [str(tenant)],
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ]
        ),
    )
    get_settings.cache_clear()
    monkeypatch.setattr(runtime, "get_tenant_engine_manager", lambda: manager)
    assert get_authenticated_principal not in app.dependency_overrides
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            params = {
                "tenant_id": str(tenant),
                "currency": "USD",
                "start": START.isoformat(),
                "end": END.isoformat(),
            }
            for endpoint in ("summary", "trends", "top-products", "status-breakdown"):
                assert (
                    client.get(
                        f"/analytics/{endpoint}", params={**params, "tenant_id": str(UUID(int=1))}
                    ).status_code
                    == 403
                )
                assert (
                    client.get(
                        f"/analytics/{endpoint}",
                        params=params,
                        headers={"Authorization": "Bearer wrong"},
                    ).status_code
                    == 401
                )
            result = live_probe(manager, redis_client, client, tenant, params)
            assert result["live_events"] == 1
            assert "deduplicated" in result["recovery"]
            with engine.connect() as c:
                for currency in ("USD", "EUR"):
                    oracle_params = {
                        "tenant": tenant,
                        "currency": currency,
                        "start": START,
                        "end": END,
                    }
                    for endpoint in ("summary", "trends", "top-products", "status-breakdown"):
                        for interval in ("day", "hour") if endpoint == "trends" else ("day",):
                            request_params = {**params, "currency": currency}
                            if endpoint == "trends":
                                request_params["interval"] = interval
                            response = client.get(f"/analytics/{endpoint}", params=request_params)
                            assert response.status_code == 200
                            compare_api(c, response.json(), endpoint, oracle_params, interval)
    finally:
        get_settings.cache_clear()
