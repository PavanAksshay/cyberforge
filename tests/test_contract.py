import json
import re
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from astraroll.config import Settings
from astraroll.models import Order, OutboxEvent
from astraroll.orders import create_order
from astraroll.relay import relay_batch
from astraroll.schemas import EVENT_SCHEMA_VERSION, OrderCreate, OrderEvent
from astraroll.seed import seed


def test_documented_event_is_exact_frozen_schema():
    data = json.loads(Path("docs/order-event-v2.json").read_text())
    markdown = Path("docs/contracts.md").read_text(encoding="utf-8")
    documented = re.search(r"```json\n(.*?)\n```", markdown, flags=re.DOTALL)
    assert json.loads(documented.group(1)) == data
    event = OrderEvent.model_validate(data)
    assert event.schema_version == EVENT_SCHEMA_VERSION == 2
    assert event.before.product_id != event.after.product_id
    assert event.before.status == "placed"
    assert event.after.status == "shipped"


@pytest.mark.parametrize("legacy_version", [1, None, 3])
def test_v2_parser_rejects_legacy_missing_and_unknown_versions(legacy_version):
    data = json.loads(Path("docs/order-event-v2.json").read_text())
    if legacy_version is None:
        del data["schema_version"]
    else:
        data["schema_version"] = legacy_version
    with pytest.raises(ValidationError):
        OrderEvent.model_validate(data)


@pytest.mark.integration
@pytest.mark.parametrize("product", ["omit", None, "not-a-uuid", 123, ""])
def test_required_valid_product_in_api(client, tenant_id, factory, product):
    body = {"amount": "12.00", "currency": "USD"}
    if product != "omit":
        body["product_id"] = product
    base = f"/tenants/{tenant_id}/orders"
    assert client.post(base, json=body).status_code == 422
    with factory() as session:
        assert not session.scalar(select(Order.id).where(Order.tenant_id == tenant_id))
        assert not session.scalar(select(OutboxEvent.id).where(OutboxEvent.tenant_id == tenant_id))


@pytest.mark.integration
def test_required_currency_and_server_controlled_status(client, tenant_id):
    base = f"/tenants/{tenant_id}/orders"
    body = {"product_id": str(uuid4()), "amount": "1.00"}
    assert client.post(base, json=body).status_code == 422
    for status in ("placed", "shipped", "cancelled"):
        assert (
            client.post(base, json={**body, "currency": "USD", "status": status}).status_code == 422
        )


@pytest.mark.integration
@pytest.mark.parametrize("status", ["shipped", "cancelled"])
def test_edits_and_transition_can_commit_together(client, tenant_id, status):
    base = f"/tenants/{tenant_id}/orders"
    original = client.post(
        base, json={"product_id": str(uuid4()), "amount": "1.00", "currency": "USD"}
    ).json()
    new_product = str(uuid4())
    response = client.patch(
        f"{base}/{original['id']}",
        json={
            "expected_version": 1,
            "product_id": new_product,
            "amount": "2.50",
            "status": status,
        },
    )
    assert response.status_code == 200
    assert response.json()["product_id"] == new_product
    assert response.json()["amount"] == "2.50"
    assert response.json()["status"] == status
    assert response.json()["version"] == 2


@pytest.mark.integration
@pytest.mark.parametrize("status", ["shipped", "cancelled"])
def test_terminal_transitions_and_complete_snapshots(client, factory, tenant_id, status):
    original_product, new_product = str(uuid4()), str(uuid4())
    base = f"/tenants/{tenant_id}/orders"
    response = client.post(
        base, json={"product_id": original_product, "amount": "10.00", "currency": "EUR"}
    )
    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "placed"
    assert created["product_id"] == original_product
    path = f"{base}/{created['id']}"
    updated = client.patch(
        path,
        json={"expected_version": 1, "amount": "20.50", "product_id": new_product},
    ).json()
    assert updated["version"] == 2
    assert updated["status"] == "placed"
    assert updated["created_at"] == created["created_at"]
    terminal = client.patch(path, json={"expected_version": 2, "status": status})
    assert terminal.status_code == 200
    assert terminal.json()["status"] == status
    assert terminal.json()["version"] == 3
    for change in (
        {"amount": "1"},
        {"product_id": original_product},
        {"status": "shipped"},
        {"status": "cancelled"},
    ):
        response = client.patch(path, json={"expected_version": 3, **change})
        assert response.status_code == 409
    assert client.post(path + "/cancel", json={"expected_version": 3}).status_code == 409
    # Version validation precedes terminal-state validation.
    stale = client.patch(path, json={"expected_version": 2, "amount": "1"})
    assert stale.status_code == 409
    assert "Version conflict" in stale.json()["detail"]
    with factory() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.tenant_id == tenant_id)
            .order_by(OutboxEvent.order_version)
        ).all()
        assert len(events) == 3
        assert events[0].payload["before"] is None
        assert events[0].payload["after"] == created
        assert events[1].payload["before"] == created
        assert events[1].payload["after"] == updated
        assert events[2].payload["before"] == updated
        assert events[2].payload["after"] == terminal.json()
        assert events[2].event_type == (
            "order.cancelled" if status == "cancelled" else "order.updated"
        )
        fields = {
            "id",
            "tenant_id",
            "product_id",
            "amount",
            "currency",
            "status",
            "version",
            "created_at",
            "updated_at",
        }
        for event in events:
            assert event.schema_version == event.payload["schema_version"] == 2
            assert set(event.payload["after"]) == fields
            assert isinstance(event.payload["after"]["amount"], str)
            assert (
                OrderEvent.model_validate(event.payload)
                .after.updated_at.utcoffset()
                .total_seconds()
                == 0
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        {"currency": "EUR"},
        {"created_at": "2025-01-01T00:00:00Z"},
        {"tenant_id": str(uuid4())},
        {"version": 99},
        {"status": "placed"},
        {"product_id": "bad"},
        {"product_id": None},
        {"amount": None},
        {"amount": "99", "status": "invalid"},
        {"product_id": str(uuid4()), "currency": "EUR"},
        {"status": "cancelled", "amount": "-1"},
    ],
)
def test_invalid_mutations_are_atomic_and_eventless(client, factory, tenant_id, change):
    base = f"/tenants/{tenant_id}/orders"
    original = client.post(
        base, json={"product_id": str(uuid4()), "amount": "1.00", "currency": "USD"}
    ).json()
    path = f"{base}/{original['id']}"
    assert client.patch(path, json={"expected_version": 1, **change}).status_code == 422
    assert client.get(path).json() == original
    with factory() as session:
        events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
        ).all()
        assert len(events) == 1


@pytest.mark.integration
def test_other_existing_tenant_cannot_read_or_mutate(client, factory):
    a = client.post("/tenants", json={"name": "A"}).json()["id"]
    b = client.post("/tenants", json={"name": "B"}).json()["id"]
    order = client.post(
        f"/tenants/{a}/orders", json={"product_id": str(uuid4()), "amount": "1", "currency": "USD"}
    ).json()
    path = f"/tenants/{b}/orders/{order['id']}"
    assert client.get(path).status_code == 404
    assert client.get(f"/tenants/{b}/orders").json() == []
    assert (
        client.patch(path, json={"expected_version": 1, "product_id": str(uuid4())}).status_code
        == 404
    )
    assert client.post(path + "/cancel", json={"expected_version": 1}).status_code == 404
    with factory() as session:
        assert session.get(Order, UUID(order["id"])).version == 1


@pytest.mark.integration
def test_relay_frozen_transport_and_retry_identity(factory, tenant_id, redis_client, monkeypatch):
    monkeypatch.delenv("ORDER_STREAM", raising=False)
    settings = Settings(_env_file=None)
    assert settings.order_stream == "astraroll:orders:v1"
    destination = f"test:astraroll:{uuid4()}"
    calls = []

    class Transport:
        fail = True

        def xadd(self, stream, fields):
            calls.append((stream, fields))
            result = redis_client.xadd(destination, fields)
            if self.fail:
                self.fail = False
                raise ConnectionError("Ambiguous publication")
            return result

    with factory.begin() as session:
        create_order(
            session, tenant_id, OrderCreate(product_id=uuid4(), amount="1", currency="USD")
        )
    transport = Transport()
    with pytest.raises(ConnectionError):
        relay_batch(factory, transport, settings.order_stream)
    while relay_batch(factory, transport, settings.order_stream):
        pass
    assert calls
    by_id = defaultdict(list)
    for stream, fields in calls:
        assert stream == "astraroll:orders:v1"
        assert set(fields) == {"event"}
        event = json.loads(fields["event"])
        OrderEvent.model_validate(event)
        by_id[event["event_id"]].append(event)
    repeated = [copies for copies in by_id.values() if len(copies) > 1]
    assert repeated
    assert all(copies[0] == copies[1] for copies in repeated)


@pytest.mark.integration
def test_seed_products_currencies_statuses_and_tenants(factory):
    seed_value = uuid4().int
    seed(factory, tenants=2, orders_per_tenant=12, seed_value=seed_value)
    with factory() as session:
        from astraroll.models import Tenant

        tenants = session.scalars(
            select(Tenant).where(Tenant.name.like(f"Demo {seed_value}-%"))
        ).all()
        products = []
        for tenant in tenants:
            orders = session.scalars(select(Order).where(Order.tenant_id == tenant.id)).all()
            assert len(orders) == 12
            assert {order.status for order in orders} == {"placed", "shipped", "cancelled"}
            assert {order.currency for order in orders} == {"USD", "EUR"}
            products.append({order.product_id for order in orders})
            events = session.scalars(
                select(OutboxEvent).where(OutboxEvent.tenant_id == tenant.id)
            ).all()
            for event in events:
                assert OrderEvent.model_validate(event.payload).tenant_id == tenant.id
        assert len(products) == 2
        assert products[0].isdisjoint(products[1])
