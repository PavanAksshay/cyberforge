import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from astraroll.api import app
from astraroll.config import get_settings
from astraroll.v2 import runtime


def test_relay_requires_explicit_scope_before_connections():
    result = subprocess.run(
        [sys.executable, "-m", "astraroll.v2.workers", "relay", "--once"],
        env={**os.environ, "CONTROL_DATABASE_URL": "postgresql://unreachable"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "no implicit backlog drain" in result.stderr


def test_dormant_metadata_cache_retains_no_connections_and_rechecks_status():
    from types import SimpleNamespace

    import pytest

    from astraroll.v2.router import TenantDatabaseResolver, TenantEngineManager, TenantInactiveError

    a, b = uuid4(), uuid4()
    records = {
        a: SimpleNamespace(database_name="a", status="active"),
        b: SimpleNamespace(database_name="b", status="active"),
    }
    resolver = TenantDatabaseResolver(
        SimpleNamespace(get_tenant=lambda t: records[t]), "postgresql+psycopg://localhost/postgres"
    )
    manager = TenantEngineManager(resolver, max_active_pools=1, max_dormant_engines=1)
    try:
        first = manager.get_engine(a)
        manager.get_engine(b)
        assert first.pool.checkedin() == first.pool.checkedout() == 0
        assert manager.active_pool_count == 1 and len(manager._dormant_engines) == 1
        records[a].status = "failed"
        with pytest.raises(TenantInactiveError):
            manager.get_engine(a)
        records[a].status = "active"
        assert manager.get_engine(a) is first
        manager.get_engine(b)
        records[a].database_name = "new_a"
        assert manager.get_engine(a) is not first
        assert manager.active_pool_count == 1 and len(manager._dormant_engines) <= 1
    finally:
        manager.close_all()
        assert not manager._dormant_engines


def test_late_connection_return_does_not_leave_an_idle_dormant_connection(factory):
    from types import SimpleNamespace

    from sqlalchemy import event, text

    from astraroll.v2.router import TenantDatabaseResolver, TenantEngineManager

    a, b = uuid4(), uuid4()
    url = factory.kw["bind"].url
    registry = SimpleNamespace(
        get_tenant=lambda _: SimpleNamespace(database_name=url.database, status="active")
    )
    manager = TenantEngineManager(
        TenantDatabaseResolver(registry, url.render_as_string(hide_password=False)),
        max_active_pools=1,
    )
    first = manager.get_engine(a)
    returns = []
    event.listen(
        first, "checkin", lambda dbapi, record: returns.append(record.dbapi_connection is None)
    )
    try:
        with first.connect() as c:
            c.execute(text("SELECT 1"))
        manager.get_engine(b)
        # Simulate a caller obtaining its connection just after its engine was evicted.
        with first.connect() as c:
            c.execute(text("SELECT 1"))
        assert returns[-1] is True
        assert manager.get_engine(a) is first
        with first.connect() as c:
            c.execute(text("SELECT 1"))
        assert returns[-1] is False
        assert manager.active_pool_count == 1
    finally:
        manager.close_all()


def test_cold_control_database_failure_returns_sanitized_503(monkeypatch):
    tenant = uuid4()
    token = "release-control-fault-test"
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql+psycopg://unused/postgres")
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "subject": "fault-test",
                    "tenant_ids": [str(tenant)],
                    "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ]
        ),
    )

    def unavailable():
        raise OperationalError("SELECT 1", {}, Exception("private-driver-secret"))

    runtime.get_tenant_engine_manager.cache_clear()
    runtime.get_tenant_resolver.cache_clear()
    get_settings.cache_clear()
    monkeypatch.setattr(runtime, "get_control_registry", unavailable)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/analytics/summary",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "tenant_id": str(tenant),
                    "currency": "USD",
                    "start": "2026-10-01T00:00:00Z",
                    "end": "2026-10-08T00:00:00Z",
                },
            )
            assert response.status_code == 503, response.text
            assert "private-driver-secret" not in response.text
    finally:
        runtime.get_tenant_engine_manager.cache_clear()
        runtime.get_tenant_resolver.cache_clear()
        get_settings.cache_clear()


def test_v2_admin_tenant_create_returns_timestamp_and_applied_revision(factory, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    token = "admin-create-test"
    monkeypatch.setenv("DEPLOYMENT_MODE", "database_per_tenant")
    monkeypatch.setenv(
        "CONTROL_DATABASE_URL", factory.kw["bind"].url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS_JSON",
        json.dumps(
            [
                {
                    "subject": "admin-create",
                    "tenant_ids": [],
                    "is_admin": True,
                    "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ]
        ),
    )
    names = (
        "get_tenant_provisioner",
        "get_tenant_engine_manager",
        "get_tenant_resolver",
        "get_control_registry",
        "get_control_engine",
    )
    for name in names:
        getattr(runtime, name).cache_clear()
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/tenants",
                json={"name": "Release new tenant"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201, response.text
            assert datetime.fromisoformat(response.json()["created_at"]).tzinfo is not None
            tenant = UUID(response.json()["id"])
            record = runtime.get_control_registry().get_tenant(tenant)
            e = create_engine(
                make_url(get_settings().control_database_url).set(database=record.database_name)
            )
            with e.connect() as c:
                assert (
                    c.scalar(text("SELECT version_num FROM alembic_version"))
                    == "0005_tenant_access_paths"
                )
            e.dispose()
    finally:
        for name in names:
            method = getattr(runtime, name)
            if method.cache_info().currsize and name == "get_control_engine":
                method().dispose()
            method.cache_clear()
        get_settings.cache_clear()
