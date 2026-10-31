import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, Field, StringConstraints, TypeAdapter, ValidationError

from astraroll.analytics.contracts import Contract
from astraroll.config import get_settings

bearer = HTTPBearer(auto_error=False)


class AuthenticatedPrincipal(Contract):
    subject: str = Field(min_length=1)
    tenant_ids: frozenset[UUID]


class TenantContext(Contract):
    tenant_id: UUID
    subject: str


class ApiKeyRecord(Contract):
    key_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    tenant_ids: frozenset[UUID] = Field(min_length=1)
    expires_at: AwareDatetime


def invalid_credentials():
    return HTTPException(
        401, "Invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"}
    )


def get_authenticated_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedPrincipal:
    """Verify server-issued opaque API keys against trusted digest/membership configuration."""
    if credentials is None:
        raise HTTPException(
            401, "Verified identity required", headers={"WWW-Authenticate": "Bearer"}
        )
    configured = get_settings().analytics_api_keys_json
    try:
        records = TypeAdapter(list[ApiKeyRecord]).validate_json(
            configured.get_secret_value() if configured else "[]"
        )
        if not records or len({r.key_sha256 for r in records}) != len(records):
            raise ValueError("Missing or ambiguous credential registry")
    except (ValidationError, ValueError):
        raise HTTPException(
            503,
            detail={
                "code": "analytics_authorization_unavailable",
                "message": "Analytics credential verifier is not configured correctly",
            },
        ) from None
    if len(credentials.credentials) > 1024:
        raise invalid_credentials()
    candidate = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    matched = None
    for record in records:
        if hmac.compare_digest(candidate, record.key_sha256):
            matched = record
    if matched is None or matched.expires_at <= datetime.now(UTC):
        raise invalid_credentials()
    return AuthenticatedPrincipal(subject=matched.subject, tenant_ids=matched.tenant_ids)


def get_authorized_tenant(
    tenant_id: Annotated[UUID, Query()],
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
) -> TenantContext:
    if tenant_id not in principal.tenant_ids:
        raise HTTPException(403, "Tenant access denied")
    return TenantContext(tenant_id=tenant_id, subject=principal.subject)
