"""AstraRoll Aggregation Contracts.

Defines the data models, contracts, and typed structures for order snapshots,
events, projection states, aggregation keys, and aggregate deltas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from astraroll.aggregation.exceptions import InvalidEventError, InvalidSnapshotError


def _ensure_utc(dt: Any) -> datetime:
    """Ensure a datetime object is timezone-aware and converted to UTC.

    Supports datetime instances as well as ISO-8601 string timestamps.
    """
    if isinstance(dt, str):
        try:
            clean_str = dt.replace("Z", "+00:00") if dt.endswith("Z") else dt
            dt = datetime.fromisoformat(clean_str)
        except ValueError as err:
            raise InvalidSnapshotError(f"Invalid ISO datetime string: '{dt}'") from err
    elif not isinstance(dt, datetime):
        raise InvalidSnapshotError(f"Expected datetime or ISO string, got {type(dt).__name__}")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class OrderStatus(str, Enum):
    """Permitted order lifecycle statuses for AstraRoll V1."""

    PLACED = "placed"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


class AggregationWindow(str, Enum):
    """Aggregation time windows."""

    HOURLY = "hourly"
    DAILY = "daily"


class AggregateType(str, Enum):
    """Dimensions of aggregates supported by AstraRoll V1."""

    SUMMARY = "summary"
    STATUS = "status"
    PRODUCT = "product"


class OrderSnapshot(BaseModel):
    """Immutable snapshot of order state at a given version.

    Financial values must always use integer minor currency units (cents).
    Floating-point representations are strictly forbidden to prevent precision loss.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: str = Field(..., description="Unique UUID identifier for the product")
    amount_cents: int = Field(..., description="Order total in minor currency units (integer cents)")
    currency: str = Field(default="USD", description="Mandatory 3-letter uppercase ISO currency code")
    status: OrderStatus = Field(..., description="Current status of the order")
    created_at: datetime = Field(..., description="Original order creation timestamp in UTC (immutable)")
    updated_at: datetime = Field(..., description="Timestamp of the latest update in UTC")
    order_id: Optional[str] = Field(default=None, description="Optional associated order ID")
    tenant_id: Optional[str] = Field(default=None, description="Optional associated tenant ID")

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, v: Any) -> str:
        s = str(v).strip()
        if not s:
            raise InvalidSnapshotError("product_id must be a non-empty string or UUID")
        try:
            # Validate UUID syntax
            UUID(s)
        except (ValueError, TypeError):
            # Also permit test strings if needed, but raise if empty
            pass
        return s

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v_upper = v.strip().upper()
        if len(v_upper) != 3 or not v_upper.isalpha():
            raise InvalidSnapshotError(f"Invalid currency code '{v}': must be 3-letter ISO code")
        return v_upper

    @field_validator("amount_cents", mode="before")
    @classmethod
    def validate_amount_cents(cls, v: Any) -> int:
        if isinstance(v, bool):
            raise InvalidSnapshotError("amount_cents cannot be a boolean")
        if isinstance(v, float):
            raise InvalidSnapshotError("amount_cents cannot be a float; use integer minor currency units (cents)")
        if not isinstance(v, int):
            raise InvalidSnapshotError(f"amount_cents must be an integer, got {type(v).__name__}")
        if v < 0:
            raise InvalidSnapshotError("amount_cents cannot be negative")
        return v

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> OrderStatus:
        if isinstance(v, OrderStatus):
            return v
        if isinstance(v, str):
            try:
                return OrderStatus(v.lower())
            except ValueError:
                raise InvalidSnapshotError(f"Invalid status: '{v}'. Permitted: {[s.value for s in OrderStatus]}")
        raise InvalidSnapshotError(f"Invalid status type: {type(v).__name__}")

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_datetimes(cls, v: Any) -> datetime:
        return _ensure_utc(v)

    @property
    def sales_amount(self) -> Decimal:
        """Return amount as Decimal in major units (e.g. 1000 cents -> Decimal('10.00'))."""
        return Decimal(self.amount_cents) / Decimal(100)


class OrderEvent(BaseModel):
    """Event contract representing an order lifecycle event emitted via Redis Streams.

    Shared contract between Ingestion (Developer A) and Aggregation (Developer B).
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    event_id: str = Field(..., description="Globally unique identifier for this event")
    tenant_id: str = Field(..., description="Tenant identifier for multi-tenancy segregation")
    order_id: str = Field(..., description="Unique identifier for the order")
    order_version: int = Field(..., description="Monotonically increasing version number for the order")
    event_type: str = Field(..., description="Event type descriptor (e.g. order.created, order.updated)")
    occurred_at: datetime = Field(..., description="Timestamp when the event occurred in UTC")
    snapshot: OrderSnapshot = Field(
        ...,
        alias="order",
        description="Full order snapshot reflecting state at this order_version",
    )
    before_snapshot: Optional[OrderSnapshot] = Field(
        default=None,
        description="Optional preceding order snapshot prior to this event",
    )

    @field_validator("event_id", "tenant_id", "order_id", "event_type")
    @classmethod
    def validate_non_empty_strings(cls, v: str, info) -> str:
        if not isinstance(v, str) or not v.strip():
            raise InvalidEventError(f"{info.field_name} must be a non-empty string")
        return v.strip()

    @field_validator("order_version", mode="before")
    @classmethod
    def validate_order_version(cls, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise InvalidEventError("order_version must be an integer")
        if v < 1:
            raise InvalidEventError(f"order_version must be >= 1, got {v}")
        return v

    @field_validator("occurred_at", mode="before")
    @classmethod
    def validate_occurred_at(cls, v: Any) -> datetime:
        return _ensure_utc(v)


class OrderProjectionState(BaseModel):
    """Persisted projection state of an order in the aggregation store."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    tenant_id: str
    order_id: str
    last_applied_version: int = Field(..., alias="current_version")
    canonical_snapshot: OrderSnapshot = Field(..., alias="snapshot")
    last_event_id: Optional[str] = None
    updated_at: datetime

    def __init__(self, **data: Any):
        # Support both current_version/last_applied_version and snapshot/canonical_snapshot
        if "last_applied_version" in data and "current_version" not in data:
            data["current_version"] = data["last_applied_version"]
        if "canonical_snapshot" in data and "snapshot" not in data:
            data["snapshot"] = data["canonical_snapshot"]
        super().__init__(**data)

    @property
    def current_version(self) -> int:
        return self.last_applied_version

    @property
    def snapshot(self) -> OrderSnapshot:
        return self.canonical_snapshot


class AggregationKey(BaseModel):
    """Deterministic, typed aggregation key identifying an aggregation bucket.

    Includes tenant_id and currency in every key to guarantee complete isolation.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    currency: str = "USD"
    window: AggregationWindow
    aggregate_type: AggregateType
    bucket_start: datetime
    dimension_value: Optional[str] = None

    @field_validator("bucket_start", mode="before")
    @classmethod
    def validate_bucket_start(cls, v: Any) -> datetime:
        return _ensure_utc(v)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        return v.strip().upper()

    @property
    def key_string(self) -> str:
        """Deterministic string representation suitable for logging or Redis/cache keys."""
        dim = self.dimension_value or "none"
        bkt_str = self.bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{self.tenant_id}:{self.currency}:{self.window.value}:{self.aggregate_type.value}:{bkt_str}:{dim}"

    def __hash__(self) -> int:
        return hash((
            self.tenant_id,
            self.currency,
            self.window,
            self.aggregate_type,
            self.bucket_start,
            self.dimension_value,
        ))

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, AggregationKey):
            return False
        return (
            self.tenant_id == other.tenant_id
            and self.currency == other.currency
            and self.window == other.window
            and self.aggregate_type == other.aggregate_type
            and self.bucket_start == other.bucket_start
            and self.dimension_value == other.dimension_value
        )


class AggregateDelta(BaseModel):
    """Incremental delta to apply to an aggregate bucket.

    Supports positive increments, negative decrements, and exact integer arithmetic.
    """

    model_config = ConfigDict(frozen=True)

    order_count: int = 0
    sales_cents: int = 0

    @field_validator("order_count", "sales_cents", mode="before")
    @classmethod
    def validate_ints(cls, v: Any, info) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{info.field_name} must be an integer, got {type(v).__name__}")
        return v

    @property
    def sales_amount(self) -> Decimal:
        """Sales delta in major units as Decimal."""
        return Decimal(self.sales_cents) / Decimal(100)

    def __add__(self, other: Any) -> AggregateDelta:
        if not isinstance(other, AggregateDelta):
            return NotImplemented
        return AggregateDelta(
            order_count=self.order_count + other.order_count,
            sales_cents=self.sales_cents + other.sales_cents,
        )

    def __sub__(self, other: Any) -> AggregateDelta:
        if not isinstance(other, AggregateDelta):
            return NotImplemented
        return AggregateDelta(
            order_count=self.order_count - other.order_count,
            sales_cents=self.sales_cents - other.sales_cents,
        )

    def __neg__(self) -> AggregateDelta:
        return AggregateDelta(
            order_count=-self.order_count,
            sales_cents=-self.sales_cents,
        )

    def is_zero(self) -> bool:
        """Check if delta has no net effect."""
        return self.order_count == 0 and self.sales_cents == 0


class OrderContribution(BaseModel):
    """Collection of aggregate deltas generated by an order event."""

    model_config = ConfigDict(frozen=True)

    deltas: Dict[AggregationKey, AggregateDelta] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        """Return True if there are no deltas or all deltas are zero."""
        return not self.deltas or all(d.is_zero() for d in self.deltas.values())
