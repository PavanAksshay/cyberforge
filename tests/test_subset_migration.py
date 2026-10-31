"""Target-only interruption/resume checks; never drop databases or alter V1."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from astraroll.config import get_settings
from astraroll.v2 import subset_migration as module
from astraroll.v2.scale_validation import create_plan
from astraroll.workload import seed_tenant


def test_locked_manifest_rejects_wrong_total(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"target_orders": 10000, "tenants": []}))
    with pytest.raises(ValueError, match="100 x 10000"):
        module.SubsetMigration("unused", "unused", path)


def test_v2_worker_launch_fails_closed_before_connections():
    from types import SimpleNamespace

    from astraroll.v2.workers import require_v2

    for mode, url in (("shared", "postgresql://unused"), ("database_per_tenant", None)):
        with pytest.raises(ValueError, match="explicit CONTROL_DATABASE_URL"):
            require_v2(SimpleNamespace(deployment_mode=mode, control_database_url=url))
    require_v2(
        SimpleNamespace(
            deployment_mode="database_per_tenant", control_database_url="postgresql://control"
        )
    )


def test_hardened_failure_handlers_never_drop_or_purge():
    import ast
    import inspect

    from astraroll.v2.migration import TenantMigrator
    from astraroll.v2.provisioning import TenantProvisioner

    for owner in (TenantMigrator, TenantProvisioner, module.SubsetMigration):
        tree = ast.parse(inspect.getsource(owner))
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            for node in ast.walk(handler):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {
                        "_drop_database",
                        "deprovision_tenant",
                        "purge_source_records",
                    }


def test_cached_engine_denies_inactive_tenant_and_normalizes_uuid():
    from types import SimpleNamespace

    from astraroll.v2.router import TenantDatabaseResolver, TenantEngineManager, TenantInactiveError

    tenant = uuid4()
    record = SimpleNamespace(database_name="never_connected", status="active")
    registry = SimpleNamespace(get_tenant=lambda _: record)
    manager = TenantEngineManager(
        TenantDatabaseResolver(registry, "postgresql+psycopg://localhost/postgres")
    )
    try:
        first = manager.get_engine(tenant)
        assert manager.get_engine(str(tenant)) is first
        assert manager.active_pool_count == 1
        record.status = "failed"
        with pytest.raises(TenantInactiveError):
            manager.get_engine(tenant)
    finally:
        manager.close_all()


def test_subset_checkpoint_is_atomic_and_resume_retains_target(factory, tmp_path, monkeypatch):
    # Existing test fixture owns an isolated schema in the disposable test database.
    engine = factory.kw["bind"]
    # create_plan accepts only bench UUID schemas; test via a dedicated bounded fixture.
    from conftest import migrate
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    bench = f"bench_{uuid4().hex}"
    with engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{bench}"'))
    fixture = create_engine(
        engine.url, connect_args={"options": f"-csearch_path={bench} -c timezone=UTC"}
    )
    migrate(fixture)
    tenant = seed_tenant(sessionmaker(fixture), uuid4(), 0, 10000, 1000)
    plan = create_plan(engine.url, bench, 1, 10000)
    selected = plan["tenants"][0]
    plan["tenants"] += [{**selected, "tenant_id": str(uuid4())} for _ in range(99)]
    plan["target_orders"] = 1000000
    path = tmp_path / "fixture-plan.json"
    path.write_text(json.dumps(plan, default=module.normalized))
    runner = module.SubsetMigration(engine.url, get_settings().database_url, path)
    calls = 0
    real_check = module.check_resources

    def pause():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected safe pause")
        real_check()

    monkeypatch.setattr(module, "check_resources", pause)
    try:
        with pytest.raises(RuntimeError, match="injected safe pause"):
            runner.migrate(selected)
        record = runner.registry.get_tenant(tenant)
        assert record.status == "failed"
        assert runner.provisioner._database_exists(record.database_name)
        target = module.writable_engine(runner.control_url.set(database=record.database_name))
        with target.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM orders")) == 1000
            assert c.scalar(text("SELECT copied FROM subset_copy_checkpoint")) == 1000
            assert c.scalar(text("SELECT sum(version) FROM orders")) == c.scalar(
                text("SELECT count(*) FROM outbox_events")
            )
        target.dispose()
        monkeypatch.setattr(module, "check_resources", real_check)
        result = runner.migrate(selected)
        assert result["snapshot"]["orders"] == 10000
        assert runner.registry.get_tenant(tenant).status == "active"
        # Successful retry is idempotent and uses the existing target/proof generation.
        assert runner.migrate(selected)["snapshot"] == result["snapshot"]
    finally:
        runner.close()
        fixture.dispose()
