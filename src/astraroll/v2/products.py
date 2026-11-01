"""Optional durable control-plane catalog; never modifies historical financial data."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert

metadata = MetaData()
catalog = Table(
    "control_product_catalog",
    metadata,
    Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("product_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("product_name", String(200), nullable=False),
    Column("sku", String(100)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def resolve_products(tenant_id: UUID, product_ids: list[UUID]) -> dict:
    from astraroll.v2.runtime import get_control_engine

    engine = get_control_engine()
    if not product_ids or not inspect(engine).has_table(catalog.name):
        return {}
    with engine.connect() as conn:
        return {
            str(r.product_id): dict(r)
            for r in conn.execute(
                select(catalog).where(
                    catalog.c.tenant_id == tenant_id, catalog.c.product_id.in_(product_ids)
                )
            ).mappings()
        }


def register_product(tenant_id: UUID, product_id: UUID, name: str, sku: str | None):
    from astraroll.v2.runtime import get_control_engine

    engine = get_control_engine()
    catalog.create(engine, checkfirst=True)
    with engine.begin() as conn:
        values = dict(
            tenant_id=tenant_id,
            product_id=product_id,
            product_name=name,
            sku=sku,
            updated_at=datetime.now(UTC),
        )
        conn.execute(
            insert(catalog)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[catalog.c.tenant_id, catalog.c.product_id], set_=values
            )
        )
    return values
