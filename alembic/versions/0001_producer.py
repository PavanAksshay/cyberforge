"""Producer tables only. Developer B owns separate migrations for projections."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001_producer"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_tenant_name"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount >= 0 AND amount <> 'NaN'::numeric", name="ck_order_amount"),
        sa.CheckConstraint("version >= 1", name="ck_order_version"),
        sa.CheckConstraint(
            "status IN ('created', 'completed', 'cancelled')", name="ck_order_status"
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_order_currency"),
    )
    op.create_index("ix_orders_tenant_created", "orders", ["tenant_id", "created_at", "id"])
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "order_id", sa.Uuid(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("order_version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("stream_id", sa.String(64)),
        sa.UniqueConstraint("order_id", "order_version", name="uq_outbox_order_version"),
        sa.CheckConstraint("order_version >= 1", name="ck_outbox_version"),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox_events",
        ["occurred_at", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade():
    op.drop_table("outbox_events")
    op.drop_table("orders")
    op.drop_table("tenants")
