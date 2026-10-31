import hashlib
import json
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from analytics_fake import FakeAnalyticsReader
from fastapi.testclient import TestClient

from astraroll.analytics import auth
from astraroll.analytics.reader import get_analytics_reader
from astraroll.api import app
from astraroll.config import Settings


@pytest.fixture
def authenticated_app(monkeypatch):
    tenant = uuid4()
    token = secrets.token_urlsafe(32)
    record = {
        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "subject": "server-subject",
        "tenant_ids": [str(tenant)],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }

    def configure(records):
        settings = Settings(_env_file=None, analytics_api_keys_json=json.dumps(records))
        monkeypatch.setattr(auth, "get_settings", lambda: settings)

    configure([record])
    reader = FakeAnalyticsReader()
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_analytics_reader] = lambda: reader
    query = {
        "tenant_id": str(tenant),
        "currency": "USD",
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-02T00:00:00Z",
    }
    with TestClient(app) as client:
        yield client, token, record, query, reader, configure
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.mark.parametrize("endpoint", ["summary", "trends", "top-products", "status-breakdown"])
def test_actual_api_key_verification_and_tenant_denial(authenticated_app, endpoint):
    client, token, record, query, reader, _ = authenticated_app
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/analytics/{endpoint}", params=query, headers=headers).status_code == 200
    assert len(reader.calls) == 1
    response = client.get(
        f"/analytics/{endpoint}", params={**query, "tenant_id": str(uuid4())}, headers=headers
    )
    assert response.status_code == 403
    assert len(reader.calls) == 1
    assert token not in response.text and record["key_sha256"] not in response.text


@pytest.mark.parametrize("invalid", ["wrong-token", "Bearer-looking-but-not-valid", "x" * 1025])
def test_unknown_tokens_are_rejected_before_reader(authenticated_app, invalid):
    client, _, _, query, reader, _ = authenticated_app
    response = client.get(
        "/analytics/summary", params=query, headers={"Authorization": f"Bearer {invalid}"}
    )
    assert response.status_code == 401 and not reader.calls
    assert response.headers["www-authenticate"] == "Bearer"


def test_expired_and_revoked_keys(authenticated_app):
    client, token, record, query, reader, configure = authenticated_app
    configure([{**record, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}])
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/analytics/summary", params=query, headers=headers).status_code == 401
    # Removing a key revokes it. Another valid registry entry keeps the verifier configured.
    configure([{**record, "key_sha256": hashlib.sha256(secrets.token_bytes(32)).hexdigest()}])
    assert client.get("/analytics/summary", params=query, headers=headers).status_code == 401
    assert not reader.calls


@pytest.mark.parametrize("records", [[], "malformed", [{"key_sha256": "bad"}]])
def test_bad_registry_is_unavailable_not_allow_all(authenticated_app, records):
    client, token, _, query, reader, configure = authenticated_app
    configure(records)
    response = client.get(
        "/analytics/summary", params=query, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_authorization_unavailable"
    assert not reader.calls


def test_duplicate_digest_registry_is_ambiguous_and_denied(authenticated_app):
    client, token, record, query, reader, configure = authenticated_app
    configure([record, {**record, "tenant_ids": [str(uuid4())]}])
    response = client.get(
        "/analytics/summary", params=query, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 503 and not reader.calls


def test_client_headers_cannot_supply_membership(authenticated_app):
    client, token, _, query, reader, _ = authenticated_app
    other = str(uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": other,
        "X-Subject": "admin",
        "X-Tenant-IDs": other,
    }
    assert (
        client.get(
            "/analytics/summary", params={**query, "tenant_id": other}, headers=headers
        ).status_code
        == 403
    )
    assert not reader.calls


def test_key_issuer_outputs_matching_digest_and_expiry():
    tenant = str(uuid4())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "astraroll.analytics.issue_key",
            "--subject",
            "issuer-test",
            "--tenant",
            tenant,
            "--hours",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    secret = lines[0].split(": ", 1)[1]
    record = auth.ApiKeyRecord.model_validate_json(lines[2])
    assert len(secret) >= 43
    assert hashlib.sha256(secret.encode()).hexdigest() == record.key_sha256
    assert record.expires_at > datetime.now(UTC)
    assert record.tenant_ids == {UUID(tenant)}
