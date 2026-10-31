import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from astraroll.db import Base
from astraroll.models import Order, OutboxEvent
from astraroll.orders import Conflict, create_order, mutate_order
from astraroll.relay import relay_batch
from astraroll.schemas import OrderCreate, OrderEvent, OrderUpdate
from astraroll.seed import seed

pytestmark = pytest.mark.integration


def test_migration_matches_models(factory):
    with factory() as session:
        assert (
            compare_metadata(MigrationContext.configure(session.connection()), Base.metadata) == []
        )


def test_api_lifecycle_and_tenant_isolation(client, factory, tenant_id):
    base = f"/tenants/{tenant_id}/orders"
    response = client.post(base, json={"amount": "12.34", "currency": "USD"})
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["amount"] == "12.34"
    assert order["version"] == 1
    path = f"{base}/{order['id']}"
    assert client.get(path).status_code == 200
    assert client.get(base).json()[0]["id"] == order["id"]
    assert client.get(f"/tenants/{uuid4()}/orders/{order['id']}").status_code == 404
    assert (
        client.patch(
            f"/tenants/{uuid4()}/orders/{order['id']}", json={"expected_version": 1, "amount": "99"}
        ).status_code
        == 404
    )
    response = client.patch(
        path, json={"expected_version": 1, "amount": "19.99", "status": "completed"}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert client.patch(path, json={"expected_version": 1, "amount": "2"}).status_code == 409
    response = client.post(path + "/cancel", json={"expected_version": 2})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["version"] == 3
    assert client.post(path + "/cancel", json={"expected_version": 3}).status_code == 409
    assert client.patch(path, json={"expected_version": 3, "amount": "2"}).status_code == 409
    assert client.patch(path, json={"expected_version": 3, "amount": None}).status_code == 422
    with factory() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.tenant_id == tenant_id)
            .order_by(OutboxEvent.order_version)
        ).all()
        assert len(events) == 3
        assert events[0].payload["before"] is None
        assert events[1].payload["before"]["amount"] == "12.34"
        assert events[1].payload["after"]["amount"] == "19.99"
        for event in events:
            parsed = OrderEvent.model_validate(event.payload)
            assert parsed.after.updated_at.utcoffset().total_seconds() == 0
            assert parsed.order_version == parsed.after.version


def test_tenant_api_and_validation(client):
    response = client.post("/tenants", json={"name": "Example"})
    assert response.status_code == 201
    tenant_id = response.json()["id"]
    assert client.get(f"/tenants/{tenant_id}").json()["name"] == "Example"
    assert client.get(f"/tenants/{uuid4()}").status_code == 404
    assert client.get("/health/ready").status_code == 200
    assert client.post(f"/tenants/{uuid4()}/orders", json={"amount": "1"}).status_code == 404
    assert client.post(f"/tenants/{tenant_id}/orders", json={"amount": "0.001"}).status_code == 422


def test_order_and_event_rollback_together(factory, tenant_id):
    order_id = None
    with pytest.raises(RuntimeError), factory.begin() as session:
        order = create_order(session, tenant_id, OrderCreate(amount="4.56"))
        order_id = order.id
        raise RuntimeError("Injected failure before commit")
    with factory() as session:
        assert session.get(Order, order_id) is None
        assert not session.scalar(select(OutboxEvent.id).where(OutboxEvent.order_id == order_id))


def test_outbox_failure_rolls_back_order(factory, tenant_id):
    with factory.begin() as session:
        order = create_order(session, tenant_id, OrderCreate(amount="1"))
        order_id = order.id
    with pytest.raises(IntegrityError), factory.begin() as session:
        mutate_order(session, tenant_id, order_id, 1, OrderUpdate(expected_version=1, amount="9"))
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.order_id == order_id))
        session.add(
            OutboxEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                order_id=order_id,
                order_version=1,
                event_type=event.event_type,
                schema_version=1,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )
        )
        session.flush()
    with factory() as session:
        order = session.get(Order, order_id)
        assert order.version == 1
        assert order.amount == Decimal("1.00")


def test_concurrent_updates_allow_exactly_one_winner(factory, tenant_id):
    with factory.begin() as session:
        order_id = create_order(session, tenant_id, OrderCreate(amount="1")).id
    barrier = Barrier(2)

    def update(amount):
        barrier.wait(timeout=10)
        try:
            with factory.begin() as session:
                mutate_order(
                    session, tenant_id, order_id, 1, OrderUpdate(expected_version=1, amount=amount)
                )
            return "ok"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["2", "3"])) == ["conflict", "ok"]
    with factory() as session:
        assert session.get(Order, order_id).version == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.order_id == order_id)
            )
            == 2
        )


@pytest.mark.parametrize("amount", ["-1", "NaN"])
def test_database_constraints(factory, tenant_id, amount):
    with factory.begin() as session:
        order_id = create_order(session, tenant_id, OrderCreate(amount="1")).id
    with pytest.raises(IntegrityError), factory.begin() as session:
        session.execute(
            text("UPDATE orders SET amount = CAST(:amount AS numeric) WHERE id = :id"),
            {"id": order_id, "amount": amount},
        )


def test_relay_failure_retry_and_duplicate_contract(factory, tenant_id, redis_client):
    stream = f"test:astraroll:{uuid4()}"
    with factory.begin() as session:
        order_id = create_order(session, tenant_id, OrderCreate(amount="5.25")).id

    class PublishThenFail:
        def xadd(self, name, fields):
            redis_client.xadd(name, fields)
            raise ConnectionError("Injected ambiguous Redis response")

    with pytest.raises(ConnectionError):
        relay_batch(factory, PublishThenFail(), stream, batch_size=1000)
    with factory() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.order_id == order_id))
        assert event.published_at is None
    # Drain existing pending rows too; tests never truncate shared tables or streams.
    while relay_batch(factory, redis_client, stream, batch_size=1000):
        pass
    records = [json.loads(fields[b"event"]) for _, fields in redis_client.xrange(stream)]
    assert records
    assert len({e["event_id"] for e in records}) < len(records)
    with factory() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.order_id == order_id))
        assert event.published_at is not None
        assert event.stream_id
    assert relay_batch(factory, redis_client, stream) == 0


def test_relay_skips_locked_rows(factory, tenant_id, redis_client):
    with factory.begin() as session:
        order_id = create_order(session, tenant_id, OrderCreate(amount="3")).id
    with factory.begin() as locked:
        event = locked.scalar(
            select(OutboxEvent).where(OutboxEvent.order_id == order_id).with_for_update()
        )
        relay_batch(factory, redis_client, f"test:astraroll:{uuid4()}", 1000)
        locked.refresh(event)
        assert event.published_at is None


def test_seed_is_additive_and_repeatable(factory):
    seed_value = uuid4().int
    assert seed(factory, tenants=2, orders_per_tenant=11, seed_value=seed_value, batch_size=4) == 22
    assert seed(factory, tenants=2, orders_per_tenant=11, seed_value=seed_value, batch_size=4) == 0
