import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
