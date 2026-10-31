from decimal import Decimal

import pytest
from pydantic import ValidationError

from astraroll.schemas import OrderCreate, OrderUpdate, TenantCreate


@pytest.mark.parametrize("value", ["-1", "0.001", "NaN", "Infinity", "10000000000000000"])
def test_amount_rejects_invalid(value):
    with pytest.raises(ValidationError):
        OrderCreate(amount=value)


def test_decimal_and_tenant_validation():
    order = OrderCreate(amount="123.45")
    assert order.amount == Decimal("123.45")
    assert order.model_dump(mode="json")["amount"] == "123.45"
    assert TenantCreate(name=" Demo ").name == "Demo"
    with pytest.raises(ValidationError):
        TenantCreate(name="   ")
    with pytest.raises(ValidationError):
        OrderCreate(amount="1", currency="usd")


@pytest.mark.parametrize(
    "changes",
    [{}, {"amount": None}, {"status": None}, {"status": "cancelled"}, {"currency": "EUR"}],
)
def test_patch_requires_supported_non_null_change(changes):
    with pytest.raises(ValidationError):
        OrderUpdate(expected_version=1, **changes)
