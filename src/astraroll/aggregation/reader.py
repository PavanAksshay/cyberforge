"""PostgreSQL implementation of AnalyticsReader for AstraRoll Aggregation Engine (Phase 2).

Provides high-performance analytics queries reading exclusively from pre-aggregated
durable PostgreSQL rollup tables (never querying raw orders table).

Supports:
- Developer A's AnalyticsReader Protocol:
  - capabilities()
  - summary(query)
  - trends(query)
  - top_products(query)
  - status_breakdown(query)
- Developer B's internal reader methods:
  - get_summary(tenant_id, currency, from_ts, to_ts, granularity="hour")
  - get_trends(tenant_id, currency, from_ts, to_ts, interval="hour")
  - get_top_products(tenant_id, currency, from_ts, to_ts, granularity="hour", limit=10)
  - get_status_breakdown(tenant_id, currency, from_ts, to_ts, granularity="hour")
  - get_freshness(tenant_id)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from astraroll.aggregation.models import (
    DailyProductAggregate,
    DailyStatusAggregate,
    DailySummaryAggregate,
    HourlyProductAggregate,
    HourlyStatusAggregate,
    HourlySummaryAggregate,
    TenantProcessingState,
)

Interval = Literal["hour", "day"]
METRIC_SEMANTICS = "created_at_shipped_sales_v2"
MAX_WINDOW = timedelta(days=366)
Count = Annotated[int, Field(strict=True, ge=0)]
Money = Annotated[Decimal, Field(strict=True, ge=0, decimal_places=2, allow_inf_nan=False)]


class AnalyticsUnavailable(Exception):
    """Reader not integrated, source not ready, or uninitialized tenant coverage."""


class UnsupportedAnalyticsWindow(Exception):
    """The installed aggregate reader cannot answer this exact window/interval."""


class Freshness(BaseModel):
    """Tenant processing freshness and revision metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint: str | None = Field(default=None, min_length=1)
    last_processed_at: AwareDatetime | None = None

    @field_validator("last_processed_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


@dataclass(frozen=True)
class ReaderCapabilities:
    base_granularity: Interval
    metric_semantics: str = METRIC_SEMANTICS


@dataclass(frozen=True)
class ReadResult[T]:
    value: T
    freshness: Freshness | None = None


class SummaryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_order_count: Count
    sales_amount: Money


class TrendMetrics(SummaryMetrics):
    bucket_start: AwareDatetime


class ProductMetrics(SummaryMetrics):
    product_id: UUID
    total_order_count: Annotated[int, Field(strict=True, ge=1)]


class StatusMetrics(SummaryMetrics):
    status: str

    @model_validator(mode="after")
    def cancelled_sales_are_excluded(self) -> StatusMetrics:
        if self.status == "cancelled" and self.sales_amount != Decimal("0.00"):
            raise ValueError("Cancelled orders contribute zero sales")
        return self


class AnalyticsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    start: AwareDatetime
    end: AwareDatetime
    currency: str

    @model_validator(mode="after")
    def validate_window(self) -> AnalyticsQuery:
        if self.end <= self.start:
            raise ValueError("end must be later than start; window is [start, end)")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError("Analytics windows cannot exceed 366 days")
        return self


class TrendsQuery(AnalyticsQuery):
    interval: Interval = "day"


class TopProductsQuery(AnalyticsQuery):
    limit: int = Field(default=10, ge=1, le=10)


class SummaryData(BaseModel):
    """Aggregate summary response model."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    currency: str
    total_orders: int
    total_sales: Decimal
    average_order_value: Decimal


class TrendPoint(BaseModel):
    """Single time bucket in a trends time series."""

    model_config = ConfigDict(frozen=True)

    bucket_start: datetime
    order_count: int
    sales_amount: Decimal


class TopProductItem(BaseModel):
    """Ranked product by recognized sales."""

    model_config = ConfigDict(frozen=True)

    rank: int
    product_id: UUID
    order_count: int
    sales_amount: Decimal


class StatusBreakdownItem(BaseModel):
    """Aggregate totals for a specific order status."""

    model_config = ConfigDict(frozen=True)

    status: str
    order_count: int
    sales_amount: Decimal


class FreshnessData(BaseModel):
    """Tenant processing freshness and revision metadata."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    revision: int
    last_applied_at: datetime | None
    is_initialized: bool


class AnalyticsReader(Protocol):
    """Protocol defining the analytics reader contract."""

    def capabilities(self) -> ReaderCapabilities: ...

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]: ...

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]: ...

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]: ...

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]: ...


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_daily(granularity: str) -> bool:
    g = granularity.strip().lower()
    return g in ("day", "daily")


class PostgresAnalyticsReader:
    """PostgreSQL implementation of AnalyticsReader querying durable aggregate tables."""

    def __init__(self, session: Session):
        self.session = session

    # -------------------------------------------------------------------------
    # Shared readiness & freshness check
    # -------------------------------------------------------------------------
    def check_readiness(self, tenant_id: UUID) -> Freshness:
        """Verify tenant is initialized and return current snapshot freshness.

        Raises AnalyticsUnavailable if tenant has no state or has never successfully
        applied an event.
        """
        stmt = select(TenantProcessingState).where(TenantProcessingState.tenant_id == tenant_id)
        state = self.session.scalar(stmt)

        if state is None or state.last_successful_apply_at is None:
            raise AnalyticsUnavailable(
                f"Analytics uninitialized or no trustworthy coverage for tenant {tenant_id}"
            )

        return Freshness(
            checkpoint=str(state.revision),
            last_processed_at=state.last_successful_apply_at,
        )

    # -------------------------------------------------------------------------
    # Developer A AnalyticsReader Protocol Implementation
    # -------------------------------------------------------------------------
    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            base_granularity="hour",
            metric_semantics=METRIC_SEMANTICS,
        )

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]:
        freshness = self.check_readiness(query.tenant_id)
        from_utc = _ensure_utc(query.start)
        to_utc = _ensure_utc(query.end)
        curr = query.currency.strip().upper()

        stmt = select(
            func.coalesce(func.sum(HourlySummaryAggregate.order_count), 0).label("total_orders"),
            func.coalesce(func.sum(HourlySummaryAggregate.sales_amount), Decimal("0.00")).label(
                "total_sales"
            ),
        ).where(
            HourlySummaryAggregate.tenant_id == query.tenant_id,
            HourlySummaryAggregate.currency == curr,
            HourlySummaryAggregate.bucket_start >= from_utc,
            HourlySummaryAggregate.bucket_start < to_utc,
        )
        row = self.session.execute(stmt).one()
        return ReadResult(
            value=SummaryMetrics(
                total_order_count=int(row.total_orders),
                sales_amount=Decimal(str(row.total_sales)),
            ),
            freshness=freshness,
        )

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]:
        freshness = self.check_readiness(query.tenant_id)
        from_utc = _ensure_utc(query.start)
        to_utc = _ensure_utc(query.end)
        curr = query.currency.strip().upper()

        if query.interval == "day":
            if from_utc.hour or to_utc.hour:
                raise UnsupportedAnalyticsWindow("Daily trends require UTC midnight boundaries")
            model = DailySummaryAggregate
        else:
            model = HourlySummaryAggregate

        stmt = (
            select(
                model.bucket_start,
                model.order_count,
                model.sales_amount,
            )
            .where(
                model.tenant_id == query.tenant_id,
                model.currency == curr,
                model.bucket_start >= from_utc,
                model.bucket_start < to_utc,
            )
            .order_by(model.bucket_start.asc())
        )
        rows = self.session.execute(stmt).all()
        return ReadResult(
            value=[
                TrendMetrics(
                    bucket_start=r.bucket_start,
                    total_order_count=int(r.order_count),
                    sales_amount=Decimal(str(r.sales_amount)),
                )
                for r in rows
            ],
            freshness=freshness,
        )

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]:
        freshness = self.check_readiness(query.tenant_id)
        from_utc = _ensure_utc(query.start)
        to_utc = _ensure_utc(query.end)
        curr = query.currency.strip().upper()

        sum_sales = func.coalesce(
            func.sum(HourlyProductAggregate.sales_amount), Decimal("0.00")
        ).label("total_sales")
        sum_orders = func.coalesce(func.sum(HourlyProductAggregate.order_count), 0).label(
            "total_orders"
        )

        stmt = (
            select(
                HourlyProductAggregate.product_id,
                sum_orders,
                sum_sales,
            )
            .where(
                HourlyProductAggregate.tenant_id == query.tenant_id,
                HourlyProductAggregate.currency == curr,
                HourlyProductAggregate.bucket_start >= from_utc,
                HourlyProductAggregate.bucket_start < to_utc,
            )
            .group_by(HourlyProductAggregate.product_id)
            .having(sum_orders >= 1)
            .order_by(
                desc(sum_sales),
                HourlyProductAggregate.product_id.asc(),
            )
            .limit(query.limit)
        )
        rows = self.session.execute(stmt).all()
        return ReadResult(
            value=[
                ProductMetrics(
                    product_id=r.product_id,
                    total_order_count=int(r.total_orders),
                    sales_amount=Decimal(str(r.total_sales)),
                )
                for r in rows
            ],
            freshness=freshness,
        )

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]:
        freshness = self.check_readiness(query.tenant_id)
        from_utc = _ensure_utc(query.start)
        to_utc = _ensure_utc(query.end)
        curr = query.currency.strip().upper()

        stmt = (
            select(
                HourlyStatusAggregate.status,
                func.sum(HourlyStatusAggregate.order_count).label("total_orders"),
                func.sum(HourlyStatusAggregate.sales_amount).label("total_sales"),
            )
            .where(
                HourlyStatusAggregate.tenant_id == query.tenant_id,
                HourlyStatusAggregate.currency == curr,
                HourlyStatusAggregate.bucket_start >= from_utc,
                HourlyStatusAggregate.bucket_start < to_utc,
            )
            .group_by(HourlyStatusAggregate.status)
        )
        rows = self.session.execute(stmt).all()
        mapped = {r.status: (int(r.total_orders), Decimal(str(r.total_sales))) for r in rows}

        result = [
            StatusMetrics(
                status=st,
                total_order_count=mapped.get(st, (0, Decimal("0.00")))[0],
                sales_amount=mapped.get(st, (0, Decimal("0.00")))[1],
            )
            for st in ("placed", "shipped", "cancelled")
        ]
        return ReadResult(value=result, freshness=freshness)

    # -------------------------------------------------------------------------
    # Internal Reader Methods
    # -------------------------------------------------------------------------
    def get_summary(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
    ) -> SummaryData:
        """Calculate total orders, recognized sales, and AOV over [from_ts, to_ts)."""
        from_utc = _ensure_utc(from_ts)
        to_utc = _ensure_utc(to_ts)
        curr = currency.strip().upper()
        use_daily = _is_daily(granularity)

        summary_model = DailySummaryAggregate if use_daily else HourlySummaryAggregate
        summary_stmt = select(
            func.coalesce(func.sum(summary_model.order_count), 0).label("total_orders"),
            func.coalesce(func.sum(summary_model.sales_amount), Decimal("0.00")).label(
                "total_sales"
            ),
        ).where(
            summary_model.tenant_id == tenant_id,
            summary_model.currency == curr,
            summary_model.bucket_start >= from_utc,
            summary_model.bucket_start < to_utc,
        )
        row = self.session.execute(summary_stmt).one()
        total_orders = int(row.total_orders)
        total_sales = Decimal(str(row.total_sales))

        if total_orders > 0:
            aov = (total_sales / Decimal(total_orders)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            aov = Decimal("0.00")

        return SummaryData(
            tenant_id=tenant_id,
            currency=curr,
            total_orders=total_orders,
            total_sales=total_sales,
            average_order_value=aov,
        )

    def get_trends(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        interval: str = "hour",
    ) -> list[TrendPoint]:
        """Fetch chronological time series points over [from_ts, to_ts)."""
        from_utc = _ensure_utc(from_ts)
        to_utc = _ensure_utc(to_ts)
        curr = currency.strip().upper()
        summary_model = DailySummaryAggregate if _is_daily(interval) else HourlySummaryAggregate

        stmt = (
            select(
                summary_model.bucket_start,
                summary_model.order_count,
                summary_model.sales_amount,
            )
            .where(
                summary_model.tenant_id == tenant_id,
                summary_model.currency == curr,
                summary_model.bucket_start >= from_utc,
                summary_model.bucket_start < to_utc,
            )
            .order_by(summary_model.bucket_start.asc())
        )

        rows = self.session.execute(stmt).all()
        return [
            TrendPoint(
                bucket_start=r.bucket_start,
                order_count=int(r.order_count),
                sales_amount=Decimal(str(r.sales_amount)),
            )
            for r in rows
        ]

    def get_top_products(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
        limit: int = 10,
    ) -> list[TopProductItem]:
        """Rank products by sales_amount DESC, product_id ASC.

        Includes products with zero sales as long as they have at least 1 order.
        """
        from_utc = _ensure_utc(from_ts)
        to_utc = _ensure_utc(to_ts)
        curr = currency.strip().upper()
        bounded_limit = max(1, min(limit, 100))
        product_model = DailyProductAggregate if _is_daily(granularity) else HourlyProductAggregate

        sum_sales = func.coalesce(func.sum(product_model.sales_amount), Decimal("0.00")).label(
            "total_sales"
        )
        sum_orders = func.coalesce(func.sum(product_model.order_count), 0).label("total_orders")

        stmt = (
            select(
                product_model.product_id,
                sum_orders,
                sum_sales,
            )
            .where(
                product_model.tenant_id == tenant_id,
                product_model.currency == curr,
                product_model.bucket_start >= from_utc,
                product_model.bucket_start < to_utc,
            )
            .group_by(product_model.product_id)
            .having(sum_sales > Decimal("0.00"))
            .order_by(
                desc(sum_sales),
                desc(sum_orders),
                product_model.product_id.asc(),
            )
            .limit(bounded_limit)
        )

        rows = self.session.execute(stmt).all()
        result: list[TopProductItem] = []
        for idx, r in enumerate(rows, start=1):
            result.append(
                TopProductItem(
                    rank=idx,
                    product_id=r.product_id,
                    order_count=int(r.total_orders),
                    sales_amount=Decimal(str(r.total_sales)),
                )
            )
        return result

    def get_status_breakdown(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
    ) -> list[StatusBreakdownItem]:
        """Return breakdown for all 3 statuses (placed, shipped, cancelled)."""
        from_utc = _ensure_utc(from_ts)
        to_utc = _ensure_utc(to_ts)
        curr = currency.strip().upper()
        status_model = DailyStatusAggregate if _is_daily(granularity) else HourlyStatusAggregate

        stmt = (
            select(
                status_model.status,
                func.sum(status_model.order_count).label("total_orders"),
                func.sum(status_model.sales_amount).label("total_sales"),
            )
            .where(
                status_model.tenant_id == tenant_id,
                status_model.currency == curr,
                status_model.bucket_start >= from_utc,
                status_model.bucket_start < to_utc,
            )
            .group_by(status_model.status)
        )

        rows = self.session.execute(stmt).all()
        mapped = {r.status: (int(r.total_orders), Decimal(str(r.total_sales))) for r in rows}

        canonical_statuses = ["placed", "shipped", "cancelled"]
        result: list[StatusBreakdownItem] = []
        for st in canonical_statuses:
            cnt, sls = mapped.get(st, (0, Decimal("0.00")))
            result.append(
                StatusBreakdownItem(
                    status=st,
                    order_count=cnt,
                    sales_amount=sls,
                )
            )
        return result

    def get_freshness(self, tenant_id: UUID) -> FreshnessData:
        """Fetch tenant processing revision and apply timestamp."""
        stmt = select(TenantProcessingState).where(TenantProcessingState.tenant_id == tenant_id)
        state = self.session.scalar(stmt)

        if state is None:
            return FreshnessData(
                tenant_id=tenant_id,
                revision=0,
                last_applied_at=None,
                is_initialized=False,
            )

        return FreshnessData(
            tenant_id=tenant_id,
            revision=state.revision,
            last_applied_at=state.last_successful_apply_at,
            is_initialized=True,
        )
