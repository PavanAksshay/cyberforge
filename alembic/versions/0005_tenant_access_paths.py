"""Tenant keyset and tenant history access paths; preserve applied history/data."""

from alembic import op

revision = "0005_tenant_access_paths"
down_revision = "0004_coverage_precision"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_orders_tenant_id_keyset", "orders", ["tenant_id", "id"])
    op.create_index(
        "ix_outbox_tenant_order_version",
        "outbox_events",
        ["tenant_id", "order_id", "order_version"],
    )


def downgrade():
    raise RuntimeError("Forward-only performance migration; review index removal separately")
