import os
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from astraroll.analytics.auth import AuthenticatedPrincipal, get_authenticated_principal
from astraroll.api import app, session_dependency
from astraroll.config import get_settings
from astraroll.db import get_engine
from astraroll.models import Tenant


@pytest.fixture(autouse=True)
def clean_test_api_keys(monkeypatch):
    # Never spend a developer's real provider key or transmit test fixtures externally.
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setattr(get_settings(), "groq_api_key", None)
    # Ensure default unit tests that expect unconfigured keys see None
    if "ANALYTICS_API_KEYS_JSON" not in os.environ:
        monkeypatch.setattr(get_settings(), "analytics_api_keys_json", None)


@pytest.fixture(scope="session")
def isolated_engine():
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("Set RUN_INTEGRATION=1 with migrated PostgreSQL and Redis")
    engine = get_engine()
    assert engine.dialect.name == "postgresql", "Integration tests require PostgreSQL"
    # Dedicated schema: never drain a developer's actual outbox or mutate their orders.
    schema = f"test_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(
        engine.url, connect_args={"options": f"-csearch_path={schema} -c timezone=UTC"}
    )
    yield isolated
    isolated.dispose()
    # Retain schema for inspection; no destructive cleanup in shared environments.
    print(f"Integration artifacts retained in schema {schema}")


def migrate(engine, revision="head", *, downgrade=False):
    with engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


@pytest.fixture(scope="session")
def factory(isolated_engine):
    migrate(isolated_engine)
    isolated = isolated_engine
    yield sessionmaker(isolated, expire_on_commit=False)


@pytest.fixture
def migration_engine(factory):
    # Each history test owns its own empty schema, separate from the API suite.
    schema = f"test_migration_{uuid4().hex}"
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(
        engine.url, connect_args={"options": f"-csearch_path={schema} -c timezone=UTC"}
    )
    yield isolated
    isolated.dispose()
    print(f"Migration artifacts retained in schema {schema}")


@pytest.fixture
def tenant_id(factory):
    with factory.begin() as session:
        tenant = Tenant(name=f"Test {uuid4()}")
        session.add(tenant)
        session.flush()
        return tenant.id


@pytest.fixture
def client(factory):
    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_authenticated_principal] = lambda: AuthenticatedPrincipal(
        subject="producer-test-admin", tenant_ids=frozenset(), is_admin=True
    )
    app.dependency_overrides[session_dependency] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def redis_client(factory):
    with Redis.from_url(get_settings().redis_url, socket_timeout=5) as redis:
        redis.ping()
        yield redis
