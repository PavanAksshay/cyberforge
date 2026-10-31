"""Aggregation Engine tables (Phase 2).

Creates:
- 6 durable aggregate rollup tables:
    - hourly_summary_aggregates, daily_summary_aggregates
    - hourly_status_aggregates, daily_status_aggregates
    - hourly_product_aggregates, daily_product_aggregates
- order_projections
- processed_events
- event_quarantine
- tenant_processing_state
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0003_aggregation"
down_revision = "0002_producer_contract"
branch_labels = None
depends_on = None


def upgrade():
    # 1. hourly_summary_aggregates
    op.create_table(
        "hourly_summary_aggregates",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sales_amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("order_count >= 0", name="ck_hourly_summary_order_count"),
        sa.CheckConstraint("sales_amount >= 0 AND sales_amount <> 'NaN'::numeric", name="ck_hourly_summary_sales_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_hourly_summary_currency"),
    )
    op.create_index("ix_hourly_summary_lookup", "hourly_summary_aggregates", ["tenant_id", "currency", "bucket_start"])

    # 2. daily_summary_aggregates
    op.create_table(
        "daily_summary_aggregates",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sales_amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("order_count >= 0", name="ck_daily_summary_order_count"),
        sa.CheckConstraint("sales_amount >= 0 AND sales_amount <> 'NaN'::numeric", name="ck_daily_summary_sales_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_daily_summary_currency"),
    )
    op.create_index("ix_daily_summary_lookup", "daily_summary_aggregates", ["tenant_id", "currency", "bucket_start"])

    # 3. hourly_status_aggregates
    op.create_table(
        "hourly_status_aggregates",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("status", sa.String(16), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sales_amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("order_count >= 0", name="ck_hourly_status_order_count"),
        sa.CheckConstraint("sales_amount >= 0 AND sales_amount <> 'NaN'::numeric", name="ck_hourly_status_sales_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_hourly_status_currency"),
        sa.CheckConstraint("status IN ('placed', 'shipped', 'cancelled')", name="ck_hourly_status_status"),
    )
    op.create_index("ix_hourly_status_lookup", "hourly_status_aggregates", ["tenant_id", "currency", "bucket_start", "status"])

    # 4. daily_status_aggregates
    op.create_table(
        "daily_status_aggregates",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("status", sa.String(16), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sales_amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("order_count >= 0", name="ck_daily_status_order_count"),
        sa.CheckConstraint("sales_amount >= 0 AND sales_amount <> 'NaN'::numeric", name="ck_daily_status_sales_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_daily_status_currency"),
        sa.CheckConstraint("status IN ('placed', 'shipped', 'cancelled')", name="ck_daily_status_status"),
    )
    op.create_index("ix_daily_status_lookup", "daily_status_aggregates", ["tenant_id", "currency", "bucket_start", "status"])

    # 5. hourly_product_aggregates
    op.create_table(
        "hourly_product_aggregates",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("product_id", sa.Uuid(), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sales_amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("order_count >= 0", name="ck_hourly_product_order_count"),
        sa.CheckConstraint("sales_amount >= 0 AND sales_amount <> 'NaN'::numeric", name="ck_hourly_product_sales_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_hourly_product_currency"),
    )
    op.create_index("ix_hourly_product_lookup", "hourly_product_aggregates", ["tenant_id", "currency", "bucket_start", "product_id"])
    op.create_index("ix_hourly_product_sales", "hourly_product_aggregates", ["tenant_id", "currency", "bucket_start", "sales_amount", "order_count"])

    # 6. daily_product_aggregates
    op.create_table(
        "daily_product_aggregates",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("product_id", sa.Uuid(), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sales_amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("order_count >= 0", name="ck_daily_product_order_count"),
        sa.CheckConstraint("sales_amount >= 0 AND sales_amount <> 'NaN'::numeric", name="ck_daily_product_sales_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_daily_product_currency"),
    )
    op.create_index("ix_daily_product_lookup", "daily_product_aggregates", ["tenant_id", "currency", "bucket_start", "product_id"])
    op.create_index("ix_daily_product_sales", "daily_product_aggregates", ["tenant_id", "currency", "bucket_start", "sales_amount", "order_count"])

    # 7. order_projections
    op.create_table(
        "order_projections",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), primary_key=True),
        sa.Column("last_applied_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("canonical_snapshot", JSONB, nullable=False),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("last_applied_version >= 0", name="ck_order_projection_version"),
    )

    # 8. processed_events
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("order_version", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_processed_events_order", "processed_events", ["tenant_id", "order_id", "order_version"])

    # 9. event_quarantine
    op.create_table(
        "event_quarantine",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stream_name", sa.String(120), nullable=False),
        sa.Column("stream_id", sa.String(64), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_quarantine_created", "event_quarantine", ["quarantined_at"])
    op.create_index("ix_quarantine_order", "event_quarantine", ["tenant_id", "order_id"])

    # 10. tenant_processing_state
    op.create_table(
        "tenant_processing_state",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_successful_apply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_stream_id", sa.String(64), nullable=True),
    )


def downgrade():
    op.drop_table("tenant_processing_state")
    op.drop_index("ix_quarantine_order", table_name="event_quarantine")
    op.drop_index("ix_quarantine_created", table_name="event_quarantine")
    op.drop_table("event_quarantine")
    op.drop_index("ix_processed_events_order", table_name="processed_events")
    op.drop_table("processed_events")
    op.drop_table("order_projections")
    op.drop_index("ix_daily_product_sales", table_name="daily_product_aggregates")
    op.drop_index("ix_daily_product_lookup", table_name="daily_product_aggregates")
    op.drop_table("daily_product_aggregates")
    op.drop_index("ix_hourly_product_sales", table_name="hourly_product_aggregates")
    op.drop_index("ix_hourly_product_lookup", table_name="hourly_product_aggregates")
    op.drop_table("hourly_product_aggregates")
    op.drop_index("ix_daily_status_lookup", table_name="daily_status_aggregates")
    op.drop_table("daily_status_aggregates")
    op.drop_index("ix_hourly_status_lookup", table_name="hourly_status_aggregates")
    op.drop_table("hourly_status_aggregates")
    op.drop_index("ix_daily_summary_lookup", table_name="daily_summary_aggregates")
    op.drop_table("daily_summary_aggregates")
    op.drop_index("ix_hourly_summary_lookup", table_name="hourly_summary_aggregates")
    op.drop_table("hourly_summary_aggregates")
