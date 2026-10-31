"""Forward-only coverage/rebuild audit storage and wider aggregate totals.

Existing source, outbox and materialized records are retained. No coverage row is
created automatically: old materializations must be explicitly rebuilt/verified.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_coverage_precision"
down_revision = "0003_aggregation"
branch_labels = None
depends_on = None


def upgrade():
    for grain in ("hourly", "daily"):
        for dimension in ("summary", "status", "product"):
            op.alter_column(
                f"{grain}_{dimension}_aggregates",
                "sales_amount",
                existing_type=sa.Numeric(18, 2),
                type_=sa.Numeric(38, 2),
            )
    op.create_table(
        "bootstrap_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("currencies", JSONB(), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_semantics", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cursor", sa.Uuid()),
        sa.Column("copied", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "aggregate_coverage",
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("bootstrap_jobs.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("metric_semantics", sa.String(80), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("source_order_count", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "bootstrap_orders",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("bootstrap_jobs.id"), primary_key=True),
        sa.Column("order_id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
    )
    op.create_table(
        "rebuild_archive",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("bootstrap_jobs.id"), nullable=False),
        sa.Column("table_name", sa.String(80), nullable=False),
        sa.Column("row_data", JSONB(), nullable=False),
    )
    op.create_index("ix_rebuild_archive_job", "rebuild_archive", ["job_id", "table_name"])
    op.create_table(
        "operational_counters",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_processed_events_time", "processed_events", ["processed_at"])


def downgrade():
    raise RuntimeError(
        "0004 is forward-only: narrowing totals or deleting rebuild archives is unsafe"
    )
