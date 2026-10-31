import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from conftest import migrate
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from astraroll.orders import mutate_order
from astraroll.relay import relay_batch
from astraroll.schemas import OrderEvent, OrderUpdate

pytestmark = pytest.mark.integration


def legacy_order(engine, status="created"):
    """Historical schema-1 data only; never used as a new-contract fixture."""
    tenant, order, event = uuid4(), uuid4(), uuid4()
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    after = {
        "id": str(order),
        "tenant_id": str(tenant),
        "amount": "10.00",
        "currency": "USD",
        "status": status,
        "version": 7,
        "created_at": timestamp.isoformat(),
        "updated_at": timestamp.isoformat(),
    }
    payload = {
        "schema_version": 1,
        "event_id": str(event),
        "event_type": "order.updated",
        "tenant_id": str(tenant),
        "order_id": str(order),
        "order_version": 7,
        "occurred_at": timestamp.isoformat(),
        "before": {**after, "version": 6},
        "after": after,
    }
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id,name) VALUES (:id,'Historical')"), {"id": tenant}
        )
        connection.execute(
            text("""
            INSERT INTO orders
                (id,tenant_id,amount,currency,status,version,created_at,updated_at)
            VALUES (:id,:tenant,10.00,'USD',:status,7,:at,:at)
        """),
            {"id": order, "tenant": tenant, "status": status, "at": timestamp},
        )
        connection.execute(
            text("""
            INSERT INTO outbox_events
                (id,tenant_id,order_id,order_version,event_type,schema_version,payload,occurred_at)
            VALUES (:id,:tenant,:order,7,'order.updated',1,CAST(:payload AS jsonb),:at)
        """),
            {
                "id": event,
                "tenant": tenant,
                "order": order,
                "payload": json.dumps(payload),
                "at": timestamp,
            },
        )
    return order, payload


def current_revision(engine):
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def backfill_product(engine, order):
    product = uuid4()  # Test's authoritative catalog mapping, supplied by the operator.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id UUID"))
        connection.execute(
            text("UPDATE orders SET product_id=:product WHERE id=:id"),
            {"id": order, "product": product},
        )
    return product


def test_fresh_upgrade_0001_then_0002(migration_engine):
    migrate(migration_engine, "0001_producer")
    assert current_revision(migration_engine) == "0001_producer"
    assert "product_id" not in {c["name"] for c in inspect(migration_engine).get_columns("orders")}
    migrate(migration_engine, "0002_producer_contract")
    assert current_revision(migration_engine) == "0002_producer_contract"
    product = next(
        c for c in inspect(migration_engine).get_columns("orders") if c["name"] == "product_id"
    )
    assert product["nullable"] is False
    assert product["default"] is None
    constraint = next(
        c
        for c in inspect(migration_engine).get_check_constraints("orders")
        if c["name"] == "ck_order_status"
    )
    assert "placed" in constraint["sqltext"] and "shipped" in constraint["sqltext"]
    assert "completed" not in constraint["sqltext"]


def test_missing_products_abort_without_partial_ddl(migration_engine):
    migrate(migration_engine, "0001_producer")
    order, payload = legacy_order(migration_engine)
    with pytest.raises(DBAPIError, match="1 missing product_id"):
        migrate(migration_engine)
    assert current_revision(migration_engine) == "0001_producer"
    assert "product_id" not in {c["name"] for c in inspect(migration_engine).get_columns("orders")}
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT status FROM orders WHERE id=:id"), {"id": order})
            == "created"
        )
        assert connection.scalar(text("SELECT payload FROM outbox_events")) == payload


def test_completed_is_never_inferred_as_shipped(migration_engine):
    migrate(migration_engine, "0001_producer")
    order, payload = legacy_order(migration_engine, "completed")
    backfill_product(migration_engine, order)
    with pytest.raises(DBAPIError, match="1 legacy completed"):
        migrate(migration_engine)
    assert current_revision(migration_engine) == "0001_producer"
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT status FROM orders")) == "completed"
        assert connection.scalar(text("SELECT payload FROM outbox_events")) == payload


@pytest.mark.parametrize(
    "old_status,new_status", [("created", "placed"), ("cancelled", "cancelled")]
)
def test_authoritative_backfill_preserves_ids_versions_and_history(
    migration_engine, old_status, new_status
):
    migrate(migration_engine, "0001_producer")
    order, payload = legacy_order(migration_engine, old_status)
    product = backfill_product(migration_engine, order)
    with migration_engine.connect() as connection:
        before = dict(connection.execute(text("SELECT * FROM orders")).mappings().one())
        history = dict(connection.execute(text("SELECT * FROM outbox_events")).mappings().one())
    migrate(migration_engine, "0002_producer_contract")
    with migration_engine.connect() as connection:
        after = dict(connection.execute(text("SELECT * FROM orders")).mappings().one())
        assert after == {**before, "status": new_status, "product_id": product}
        assert (
            dict(connection.execute(text("SELECT * FROM outbox_events")).mappings().one())
            == history
        )
        assert connection.scalar(text("SELECT payload FROM outbox_events")) == payload
    assert current_revision(migration_engine) == "0002_producer_contract"


def test_reviewed_shipping_decision_can_be_migrated(migration_engine):
    migrate(migration_engine, "0001_producer")
    order, payload = legacy_order(migration_engine, "completed")
    backfill_product(migration_engine, order)
    # An explicit operator decision, NOT an automatic migration mapping.
    with migration_engine.begin() as connection:
        connection.execute(text("ALTER TABLE orders DROP CONSTRAINT ck_order_status"))
        connection.execute(
            text("""
            ALTER TABLE orders ADD CONSTRAINT ck_order_status CHECK
                (status IN ('created','completed','cancelled','placed','shipped'))
        """)
        )
        connection.execute(text("UPDATE orders SET status='shipped' WHERE id=:id"), {"id": order})
    migrate(migration_engine)
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT status FROM orders")) == "shipped"
        assert connection.scalar(text("SELECT payload FROM outbox_events")) == payload
        assert connection.scalar(text("SELECT version FROM orders")) == 7


def test_incorrect_preparatory_column_type_fails(migration_engine):
    migrate(migration_engine, "0001_producer")
    with migration_engine.begin() as connection:
        connection.execute(text("ALTER TABLE orders ADD COLUMN product_id TEXT"))
    with pytest.raises(DBAPIError, match="product_id must have PostgreSQL type UUID"):
        migrate(migration_engine)
    assert current_revision(migration_engine) == "0001_producer"


def test_preparatory_product_default_is_rejected(migration_engine):
    migrate(migration_engine, "0001_producer")
    with migration_engine.begin() as connection:
        connection.execute(
            text("""
            ALTER TABLE orders ADD COLUMN product_id UUID
            DEFAULT '47b53ec8-c26c-45d3-8503-76f809b55952'::uuid
        """)
        )
    with pytest.raises(DBAPIError, match="product_id must not have a DEFAULT"):
        migrate(migration_engine)
    assert current_revision(migration_engine) == "0001_producer"


def test_downgrade_refuses_data_loss(migration_engine):
    migrate(migration_engine, "0002_producer_contract")
    with pytest.raises(RuntimeError, match="forward-only"):
        migrate(migration_engine, "0001_producer", downgrade=True)
    assert current_revision(migration_engine) == "0002_producer_contract"
    assert "product_id" in {c["name"] for c in inspect(migration_engine).get_columns("orders")}


def test_mixed_version_history_is_relayed_unchanged(migration_engine, redis_client):
    from pydantic import ValidationError

    migrate(migration_engine, "0001_producer")
    order, legacy_payload = legacy_order(migration_engine)
    product = backfill_product(migration_engine, order)
    migrate(migration_engine)
    factory = sessionmaker(migration_engine, expire_on_commit=False)
    with factory.begin() as session:
        mutate_order(
            session,
            UUID(legacy_payload["tenant_id"]),
            order,
            7,
            OrderUpdate(expected_version=7, amount="11.00"),
        )
    stream = f"test:astraroll:{uuid4()}"
    assert relay_batch(factory, redis_client, stream) == 2
    records = [json.loads(fields[b"event"]) for _, fields in redis_client.xrange(stream)]
    assert records[0] == legacy_payload
    with pytest.raises(ValidationError):
        OrderEvent.model_validate(records[0])
    new_event = OrderEvent.model_validate(records[1])
    assert new_event.schema_version == 2
    assert new_event.order_version == 8
    assert new_event.before.product_id == new_event.after.product_id == product
    assert new_event.before.status == new_event.after.status == "placed"


def test_upgrade_and_downgrade_0003_aggregation(migration_engine):
    migrate(migration_engine, "0003_aggregation")
    assert current_revision(migration_engine) == "0003_aggregation"
    tables = set(inspect(migration_engine).get_table_names())
    expected_tables = {
        "hourly_summary_aggregates",
        "daily_summary_aggregates",
        "hourly_status_aggregates",
        "daily_status_aggregates",
        "hourly_product_aggregates",
        "daily_product_aggregates",
        "order_projections",
        "processed_events",
        "event_quarantine",
        "tenant_processing_state",
    }
    assert expected_tables.issubset(tables)

    # Downgrade back to 0002_producer_contract
    migrate(migration_engine, "0002_producer_contract", downgrade=True)
    assert current_revision(migration_engine) == "0002_producer_contract"
    tables_after = set(inspect(migration_engine).get_table_names())
    assert not expected_tables.intersection(tables_after)
