"""Transaction-scoped tenant fence shared by writers, consumers and rebuild cutover."""

from uuid import UUID

from sqlalchemy import text


def tenant_lock(session, tenant_id: UUID):
    key = int.from_bytes(tenant_id.bytes[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
