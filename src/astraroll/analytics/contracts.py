from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from astraroll.schemas import Currency, Status

Interval = Literal["hour", "day"]
METRIC_SEMANTICS = "created_at_current_state_v1"
MAX_WINDOW = timedelta(days=366)
Count = Annotated[int, Field(strict=True, ge=0)]
Money = Annotated[Decimal, Field(strict=True, ge=0, decimal_places=2, allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnalyticsQuery(Contract):
    tenant_id: UUID
    start: AwareDatetime
    end: AwareDatetime
    currency: Currency

    @field_validator("start", "end", mode="before")
    @classmethod
    def iso_timestamp(cls, value):
        if isinstance(value, str) and "T" not in value:
            raise ValueError("Use an ISO 8601 UTC timestamp, not a date or epoch number")
        return value

    @field_validator("start", "end")
    @classmethod
    def exact_utc_hour(cls, value: datetime):
        if value.utcoffset() != timedelta(0):
            raise ValueError("Boundaries must explicitly use UTC (Z or +00:00)")
        if value.minute or value.second or value.microsecond:
            raise ValueError("Boundaries must align to complete UTC hours")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_bounded_window(self):
        if self.end <= self.start:
            raise ValueError("end must be later than start; the window is [start, end)")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError("Analytics windows cannot exceed 366 days")
        return self


class TrendsQuery(AnalyticsQuery):
    interval: Interval = "day"

    @model_validator(mode="after")
    def whole_days(self):
        if self.interval == "day" and (self.start.hour or self.end.hour):
            raise ValueError("Daily trends require UTC midnight boundaries")
        return self


class TopProductsQuery(AnalyticsQuery):
    limit: int = Field(default=10, ge=1, le=10)


class Freshness(Contract):
    # Supplied by a durable reader snapshot, never synthesized from request time.
    checkpoint: str | None = Field(default=None, min_length=1)
    last_processed_at: AwareDatetime | None = None

    @field_validator("last_processed_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None):
        return value.astimezone(UTC) if value is not None else None


class SummaryMetrics(Contract):
    total_order_count: Count
    sales_amount: Money


class TrendMetrics(SummaryMetrics):
    bucket_start: AwareDatetime


class ProductMetrics(SummaryMetrics):
    product_id: UUID
    total_order_count: Annotated[int, Field(strict=True, ge=1)]


class StatusMetrics(SummaryMetrics):
    status: Status

    @model_validator(mode="after")
    def cancelled_sales_are_excluded(self):
        if self.status == "cancelled" and self.sales_amount != 0:
            raise ValueError("Cancelled orders contribute zero sales")
        return self


class AnalyticsResponse(Contract):
    tenant_id: UUID
    start: AwareDatetime
    end: AwareDatetime
    currency: Currency
    metric_semantics: Literal["created_at_current_state_v1"] = METRIC_SEMANTICS
    freshness: Freshness | None = None


class SummaryResponse(AnalyticsResponse, SummaryMetrics):
    pass


class TrendsResponse(AnalyticsResponse):
    interval: Interval
    buckets: list[TrendMetrics]


class TopProductsResponse(AnalyticsResponse):
    limit: int = Field(ge=1, le=10)
    products: list[ProductMetrics] = Field(max_length=10)


class StatusBreakdownResponse(AnalyticsResponse):
    statuses: list[StatusMetrics] = Field(min_length=3, max_length=3)
