from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from astraroll.analytics import postgres
from astraroll.analytics.auth import AuthenticatedPrincipal, get_authenticated_principal
from astraroll.analytics.contracts import AnalyticsQuery
from astraroll.analytics.postgres import PostgresAnalyticsAdapter
from astraroll.analytics.reader import (
    AnalyticsUnavailable,
    ReaderCapabilities,
    get_analytics_reader,
)
from astraroll.api import app
from astraroll.config import Settings


def query():
    return AnalyticsQuery(
        tenant_id=uuid4(),
        currency="USD",
        start=datetime(2026, 10, 1, tzinfo=UTC),
        end=datetime(2026, 10, 2, tzinfo=UTC),
    )


def test_invalid_coverage_provider_configuration_fails_closed(monkeypatch):
    settings = Settings(
        _env_file=None,
        analytics_backend="postgres",
        analytics_coverage_provider="nonexistent_provider:coverage",
    )
    monkeypatch.setattr(postgres, "get_settings", lambda: settings)
    with pytest.raises(AnalyticsUnavailable):
        postgres.configured_reader().capabilities()


def test_optional_b_package_is_not_required_for_application_startup(monkeypatch):
    def absent(name):
        raise ModuleNotFoundError("No aggregation installation")

    monkeypatch.setattr(postgres.importlib, "import_module", absent)
    reader = PostgresAnalyticsAdapter(lambda: None)
    with pytest.raises(AnalyticsUnavailable, match="package is unavailable"):
        reader._reader_class()
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_advertised_b_semantics_mismatch_is_rejected_before_sql(monkeypatch, tmp_path):
    monkeypatch.setattr(
        postgres.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            METRIC_SEMANTICS="created_at_current_state_v1", PostgresAnalyticsReader=object
        ),
    )
    reader = PostgresAnalyticsAdapter(
        lambda: pytest.fail("No SQL allowed for incompatible metrics")
    )
    with pytest.raises(AnalyticsUnavailable, match="advertised metric contract"):
        reader.capabilities()
    unreviewed = tmp_path / "reader.py"
    unreviewed.write_text("# unreviewed module")
    monkeypatch.setattr(
        postgres.importlib,
        "import_module",
        lambda name: SimpleNamespace(__file__=str(unreviewed), PostgresAnalyticsReader=object),
    )
    with pytest.raises(AnalyticsUnavailable, match="has not been reviewed"):
        reader.capabilities()


def test_database_errors_are_sanitized_before_http_response(monkeypatch):
    scoped = query()

    def failing_engine():
        raise OperationalError("password=do-not-expose SQL details", {}, Exception("private host"))

    reader = PostgresAnalyticsAdapter(failing_engine)
    # Only the failure-mapping boundary is under test: no successful aggregate
    # response or assertion of sales semantics is manufactured here.
    monkeypatch.setattr(reader, "capabilities", lambda: ReaderCapabilities(base_granularity="hour"))
    monkeypatch.setattr(reader, "_reader_class", lambda: None)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_authenticated_principal] = lambda: AuthenticatedPrincipal(
        subject="verified-failure-test", tenant_ids={scoped.tenant_id}
    )
    app.dependency_overrides[get_analytics_reader] = lambda: reader
    try:
        with TestClient(app) as client:
            response = client.get("/analytics/summary", params=scoped.model_dump(mode="json"))
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "code": "analytics_unavailable",
            "message": "Aggregate database is unavailable",
        }
        assert "do-not-expose" not in response.text and "private host" not in response.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
