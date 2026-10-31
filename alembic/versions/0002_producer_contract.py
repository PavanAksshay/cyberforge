"""Freeze product/status contract without inventing historical product or shipping data.

Empty databases upgrade directly. Nonempty databases require an authoritative
product backfill and explicit resolution of legacy completed statuses. See
docs/contracts.md. PostgreSQL transactional DDL rolls back failed preconditions.
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_producer_contract"
down_revision = "0001_producer"
branch_labels = None
depends_on = None


def upgrade():
    # Prevent concurrent legacy writes between validation and constraint enforcement.
    op.execute("LOCK TABLE orders IN ACCESS EXCLUSIVE MODE")
    # IF NOT EXISTS also supports an operator's documented preparatory backfill.
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id UUID")
    op.execute("""
        DO $$
        DECLARE
            missing_products bigint;
            completed_orders bigint;
            unknown_statuses bigint;
        BEGIN
            IF (SELECT atttypid FROM pg_attribute
                WHERE attrelid = 'orders'::regclass AND attname = 'product_id'
                  AND NOT attisdropped) <> 'uuid'::regtype THEN
                RAISE EXCEPTION '0002_producer_contract: product_id must have PostgreSQL type UUID';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_attribute
                       WHERE attrelid = 'orders'::regclass AND attname = 'product_id'
                         AND NOT attisdropped AND atthasdef) THEN
                RAISE EXCEPTION '0002_producer_contract: product_id must not have a DEFAULT';
            END IF;
            SELECT count(*) FILTER (WHERE product_id IS NULL),
                   count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status NOT IN
                       ('created', 'placed', 'shipped', 'cancelled', 'completed'))
            INTO missing_products, completed_orders, unknown_statuses FROM orders;
            IF missing_products > 0 OR completed_orders > 0 OR unknown_statuses > 0 THEN
                RAISE EXCEPTION
                    '0002 blocked: % missing product_id; % legacy completed; % invalid statuses',
                    missing_products, completed_orders, unknown_statuses
                    USING HINT = 'Backfill authoritative products and reviewed statuses. '
                                 'See docs/contracts.md. No products or shipping are inferred.';
            END IF;
        END $$
    """)
    op.drop_constraint("ck_order_status", "orders", type_="check")
    # Documented semantic rename only; do not increment versions or rewrite history.
    op.execute("UPDATE orders SET status = 'placed' WHERE status = 'created'")
    op.create_check_constraint(
        "ck_order_status", "orders", "status IN ('placed', 'shipped', 'cancelled')"
    )
    op.alter_column("orders", "product_id", existing_type=sa.Uuid(), nullable=False)


def downgrade():
    raise RuntimeError(
        "0002_producer_contract is forward-only: downgrade would lose product IDs and "
        "misinterpret shipped status. Keep this revision; use a reviewed forward migration "
        "or restore a verified pre-upgrade backup into a separate database."
    )
