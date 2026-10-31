from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from astraroll.analytics.contracts import Contract

bearer = HTTPBearer(auto_error=False)


class AuthenticatedPrincipal(Contract):
    subject: str = Field(min_length=1)
    tenant_ids: frozenset[UUID]


class TenantContext(Contract):
    tenant_id: UUID
    subject: str


def get_authenticated_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedPrincipal:
    """Integration boundary: replace with real credential verification and membership lookup.

    Deliberately never trust a tenant header, query parameter, or unverified token.
    No development allow-all authentication provider is installed.
    """
    if credentials is None:
        raise HTTPException(
            401, "Verified identity required", headers={"WWW-Authenticate": "Bearer"}
        )
    raise HTTPException(
        503,
        detail={
            "code": "analytics_authorization_unavailable",
            "message": "Analytics identity verifier is not integrated",
        },
    )


def get_authorized_tenant(
    tenant_id: Annotated[UUID, Query()],
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
) -> TenantContext:
    if tenant_id not in principal.tenant_ids:
        raise HTTPException(403, "Tenant access denied")
    return TenantContext(tenant_id=tenant_id, subject=principal.subject)
