from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Amount = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Status = Literal["placed", "shipped", "cancelled"]
EVENT_SCHEMA_VERSION = 2


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TenantCreate(InputModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    created_at: AwareDatetime


class OrderCreate(InputModel):
    product_id: UUID
    amount: Amount
    currency: Currency


class OrderUpdate(InputModel):
    expected_version: int = Field(ge=1)
    amount: Amount | None = None
    product_id: UUID | None = None
    status: Literal["shipped", "cancelled"] | None = None

    @model_validator(mode="after")
    def require_change(self):
        changes = self.model_fields_set - {"expected_version"}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError("Provide a non-null amount, product_id or status")
        return self


class OrderCancel(InputModel):
    expected_version: int = Field(ge=1)


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    product_id: UUID
    amount: Decimal
    currency: str
    status: Status
    version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime


class OrderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2]
    event_id: UUID
    event_type: Literal["order.created", "order.updated", "order.cancelled"]
    tenant_id: UUID
    order_id: UUID
    order_version: int = Field(ge=1)
    occurred_at: AwareDatetime
    before: OrderRead | None
    after: OrderRead
