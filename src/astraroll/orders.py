"""Producer domain services. Call within a caller-owned Session.begin() transaction."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from astraroll.models import Order, OutboxEvent, Tenant
from astraroll.schemas import OrderCreate, OrderEvent, OrderRead, OrderUpdate


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def require_tenant(session: Session, tenant_id: UUID):
    if session.get(Tenant, tenant_id) is None:
        raise NotFound("Tenant not found")


def find_order(session: Session, tenant_id: UUID, order_id: UUID, *, lock=False) -> Order:
    query = select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id)
    if lock:
        query = query.with_for_update()
    order = session.scalar(query.execution_options(populate_existing=True))
    if order is None:
        raise NotFound("Order not found")
    return order


def record_event(session: Session, order: Order, event_type: str, before: OrderRead | None):
    session.flush()
    event = OrderEvent(
        event_id=uuid4(),
        event_type=event_type,
        tenant_id=order.tenant_id,
        order_id=order.id,
        order_version=order.version,
        occurred_at=order.updated_at,
        before=before,
        after=OrderRead.model_validate(order),
    )
    session.add(
        OutboxEvent(
            id=event.event_id,
            tenant_id=order.tenant_id,
            order_id=order.id,
            order_version=order.version,
            event_type=event_type,
            schema_version=1,
            payload=event.model_dump(mode="json"),
            occurred_at=order.updated_at,
        )
    )
    session.flush()


def create_order(session: Session, tenant_id: UUID, data: OrderCreate) -> Order:
    require_tenant(session, tenant_id)
    now = datetime.now(UTC)
    order = Order(
        id=uuid4(),
        tenant_id=tenant_id,
        amount=data.amount,
        currency=data.currency,
        status="created",
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(order)
    record_event(session, order, "order.created", None)
    return order


def mutate_order(
    session: Session,
    tenant_id: UUID,
    order_id: UUID,
    expected_version: int,
    update: OrderUpdate | None = None,
) -> Order:
    order = find_order(session, tenant_id, order_id, lock=True)
    if order.version != expected_version:
        raise Conflict(f"Version conflict: current version is {order.version}")
    if order.status == "cancelled":
        raise Conflict("Cancelled orders are terminal")
    before = OrderRead.model_validate(order)
    if update is None:
        order.status = "cancelled"
        event_type = "order.cancelled"
    else:
        if update.amount is not None:
            order.amount = update.amount
        if update.status is not None:
            order.status = update.status
        event_type = "order.updated"
    order.version += 1
    order.updated_at = datetime.now(UTC)
    record_event(session, order, event_type, before)
    return order
