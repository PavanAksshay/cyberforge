import json

from astraroll.aggregation.consumer import fair_messages


def message(identity, tenant):
    return identity, {b"event": json.dumps({"tenant_id": tenant}).encode()}


def test_round_robin_preserves_each_tenant_order_and_invalid_messages():
    messages = [
        message("a1", "a"),
        message("a2", "a"),
        message("a3", "a"),
        message("b1", "b"),
        message("b2", "b"),
        ("bad", {b"event": b"{"}),
    ]
    dispatched = list(fair_messages(messages))
    assert [row[0] for row in dispatched] == ["a1", "b1", "bad", "a2", "b2", "a3"]
    assert sorted(row[0] for row in dispatched) == sorted(row[0] for row in messages)
    assert list(fair_messages([])) == []


def test_invalid_or_nonobject_envelope_is_still_dispatched():
    messages = [("1", {}), ("2", {b"event": b"[]"}), ("3", {b"event": b"null"})]
    assert list(fair_messages(messages)) == messages
