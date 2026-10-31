"""Deterministic synthetic input; additive UUID namespace, bounded transactions."""

import argparse
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text

from astraroll.db import get_session_factory
from astraroll.models import Order, Tenant
from astraroll.orders import mutate_order, record_event
from astraroll.schemas import OrderUpdate


def seed(session_factory, *, tenants=3, orders_per_tenant=100, seed_value=42, batch_size=500):
    if min(tenants, orders_per_tenant, batch_size) < 1:
        raise ValueError("Counts and batch size must be positive")
    namespace = uuid5(NAMESPACE_URL, f"astraroll:seed:v1:{seed_value}")
    inserted = 0
    for tenant_index in range(tenants):
        tenant_id = uuid5(namespace, f"tenant:{tenant_index}")
        with session_factory.begin() as session:
            # Serialize concurrent seeders for this tenant without touching real orders.
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key(tenant_id)}
            )
            if session.get(Tenant, tenant_id) is None:
                session.add(Tenant(id=tenant_id, name=f"Demo {seed_value}-{tenant_index}"))
        for start in range(0, orders_per_tenant, batch_size):
            with session_factory.begin() as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key(tenant_id)}
                )
                for index in range(start, min(start + batch_size, orders_per_tenant)):
                    order_id = uuid5(tenant_id, f"order:{index}")
                    if session.scalar(select(Order.id).where(Order.id == order_id)):
                        continue
                    rng = random.Random(f"{seed_value}:{tenant_index}:{index}")
                    timestamp = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(
                        seconds=rng.randrange(365 * 86400)
                    )
                    order = Order(
                        id=order_id,
                        tenant_id=tenant_id,
                        # Five synthetic catalog products per tenant; never used for backfills.
                        product_id=uuid5(tenant_id, f"product:{index % 5}"),
                        amount=Decimal(rng.randrange(1, 1000000)) / 100,
                        currency="USD" if index % 2 == 0 else "EUR",
                        status="placed",
                        version=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(order)
                    record_event(session, order, "order.created", None)
                    # Include corrections and cancellations through production services.
                    if index % 5 == 0:
                        mutate_order(
                            session,
                            tenant_id,
                            order_id,
                            order.version,
                            OrderUpdate(
                                expected_version=order.version,
                                amount=order.amount + Decimal("1.00"),
                                product_id=uuid5(tenant_id, f"product:{(index + 1) % 5}"),
                            ),
                        )
                    if index % 10 == 0:
                        mutate_order(session, tenant_id, order_id, order.version)
                    elif index % 3 == 0:
                        mutate_order(
                            session,
                            tenant_id,
                            order_id,
                            order.version,
                            OrderUpdate(expected_version=order.version, status="shipped"),
                        )
                    inserted += 1
    return inserted


def advisory_key(value: UUID) -> int:
    return int.from_bytes(value.bytes[:8], "big", signed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=int, default=3)
    parser.add_argument("--orders-per-tenant", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    count = seed(
        get_session_factory(),
        tenants=args.tenants,
        orders_per_tenant=args.orders_per_tenant,
        seed_value=args.seed,
        batch_size=args.batch_size,
    )
    print(f"Inserted {count} orders (existing deterministic IDs skipped)")


if __name__ == "__main__":
    main()
