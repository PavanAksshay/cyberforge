from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from astraroll.schemas import OrderCreate, OrderUpdate, TenantCreate


@pytest.mark.parametrize("value", ["-1", "0.001", "NaN", "Infinity", "10000000000000000"])
def test_amount_rejects_invalid(value):
    with pytest.raises(ValidationError):
        OrderCreate(product_id=uuid4(), currency="USD", amount=value)


def test_decimal_and_tenant_validation():
    order = OrderCreate(product_id=uuid4(), currency="USD", amount="123.45")
    assert order.amount == Decimal("123.45")
    assert order.model_dump(mode="json")["amount"] == "123.45"
    assert TenantCreate(name=" Demo ").name == "Demo"
    with pytest.raises(ValidationError):
        TenantCreate(name="   ")
    with pytest.raises(ValidationError):
        OrderCreate(product_id=uuid4(), amount="1", currency="usd")


@pytest.mark.parametrize(
    "changes",
    [{}, {"amount": None}, {"status": None}, {"status": "placed"}, {"currency": "EUR"}],
)
def test_patch_requires_supported_non_null_change(changes):
    with pytest.raises(ValidationError):
        OrderUpdate(expected_version=1, **changes)
