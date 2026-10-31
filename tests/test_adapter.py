"""Unit tests for the Redis Streams event adapter."""

import json
from uuid import uuid4

from astraroll.aggregation.adapter import adapt_stream_message
from astraroll.aggregation.contracts import OrderStatus


def create_valid_v2_payload():
    tenant_id = str(uuid4())
    order_id = str(uuid4())
    product_id = str(uuid4())
    event_id = str(uuid4())
    return {
        "schema_version": 2,
        "event_id": event_id,
        "event_type": "order.updated",
        "tenant_id": tenant_id,
        "order_id": order_id,
        "order_version": 2,
        "occurred_at": "2026-10-31T08:00:00Z",
        "before": {
            "id": order_id,
            "tenant_id": tenant_id,
            "product_id": product_id,
            "amount": "10.00",
            "currency": "USD",
            "status": "placed",
            "version": 1,
            "created_at": "2026-10-31T07:00:00Z",
            "updated_at": "2026-10-31T07:00:00Z",
        },
        "after": {
            "id": order_id,
            "tenant_id": tenant_id,
            "product_id": product_id,
            "amount": "12.50",
            "currency": "USD",
            "status": "shipped",
            "version": 2,
            "created_at": "2026-10-31T07:00:00Z",
            "updated_at": "2026-10-31T08:00:00Z",
        },
    }


def test_valid_v2_payload_adapts_cleanly():
    payload = create_valid_v2_payload()
    res = adapt_stream_message("1-0", json.dumps(payload))

    assert res.should_quarantine is False
    assert res.event is not None
    assert res.event.event_id == payload["event_id"]
    assert res.event.tenant_id == payload["tenant_id"]
    assert res.event.order_id == payload["order_id"]
    assert res.event.order_version == 2
    assert res.event.snapshot.amount_cents == 1250
    assert res.event.snapshot.status == OrderStatus.SHIPPED
    assert res.event.snapshot.currency == "USD"
    assert res.event.snapshot.product_id == payload["after"]["product_id"]
    assert res.event.before_snapshot is not None
    assert res.event.before_snapshot.amount_cents == 1000
    assert res.event.before_snapshot.status == OrderStatus.PLACED


def test_v1_payload_is_quarantined():
    payload = create_valid_v2_payload()
    payload["schema_version"] = 1
    res = adapt_stream_message("2-0", json.dumps(payload))

    assert res.should_quarantine is True
    assert "Unsupported schema_version: 1" in res.quarantine_reason
    assert res.event is None
    assert res.schema_version == 1
    assert str(res.event_id) == payload["event_id"]


def test_malformed_json_is_quarantined():
    res = adapt_stream_message("3-0", "{broken-json")

    assert res.should_quarantine is True
    assert "Malformed JSON" in res.quarantine_reason
    assert res.event is None


def test_float_amount_is_quarantined():
    payload = create_valid_v2_payload()
    # Float in payload
    payload["after"]["amount"] = 12.50  # python float
    res = adapt_stream_message("4-0", json.dumps(payload))

    assert res.should_quarantine is True
    assert "amount must not be a float" in res.quarantine_reason


def test_negative_amount_is_quarantined():
    payload = create_valid_v2_payload()
    payload["after"]["amount"] = "-5.00"
    res = adapt_stream_message("5-0", json.dumps(payload))

    assert res.should_quarantine is True
    assert "cannot be negative" in res.quarantine_reason


def test_invalid_uuid_product_id_is_quarantined():
    payload = create_valid_v2_payload()
    payload["after"]["product_id"] = "not-a-uuid"
    res = adapt_stream_message("6-0", json.dumps(payload))

    assert res.should_quarantine is True
    assert "Invalid 'after' snapshot" in res.quarantine_reason


def test_invalid_status_is_quarantined():
    payload = create_valid_v2_payload()
    payload["after"]["status"] = "unknown_status"
    res = adapt_stream_message("7-0", json.dumps(payload))

    assert res.should_quarantine is True
    assert "Invalid order status" in res.quarantine_reason


def test_fractional_cents_amount_is_quarantined():
    payload = create_valid_v2_payload()
    # Amount with 3 decimal places must be quarantined, not rounded
    payload["after"]["amount"] = "12.345"
    res = adapt_stream_message("8-0", json.dumps(payload))

    assert res.should_quarantine is True
    assert "exceeds 2 decimal places" in res.quarantine_reason
