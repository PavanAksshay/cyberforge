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

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
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
from astraroll.analytics.contracts import (
    METRIC_SEMANTICS as METRIC_SEMANTICS,
)
from astraroll.analytics.contracts import (
    AnalyticsQuery,
    Freshness,
    ProductMetrics,
    StatusMetrics,
    SummaryMetrics,
    TopProductsQuery,
    TrendMetrics,
    TrendsQuery,
)
from astraroll.analytics.reader import (
    AnalyticsReader as AnalyticsReader,
)
from astraroll.analytics.reader import (
    AnalyticsUnavailable as AnalyticsUnavailable,
)
from astraroll.analytics.reader import (
    ReaderCapabilities,
    ReadResult,
)


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

    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(base_granularity="hour")

    def check_readiness(self, query: AnalyticsQuery) -> Freshness:
        from astraroll.aggregation.coverage import read_coverage
        from astraroll.analytics.coverage import require_coverage

        proof = read_coverage(self.session, query)
        require_coverage(proof, query, "summary")
        state = self.get_freshness(query.tenant_id)
        return Freshness(tenant_revision=state.revision, last_processed_at=state.last_applied_at)

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]:
        freshness = self.check_readiness(query)
        data = self.get_summary(query.tenant_id, query.currency, query.start, query.end)
        return ReadResult(
            SummaryMetrics(total_order_count=data.total_orders, sales_amount=data.total_sales),
            freshness,
        )

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]:
        freshness = self.check_readiness(query)
        return ReadResult(
            [
                TrendMetrics(
                    bucket_start=row.bucket_start,
                    total_order_count=row.order_count,
                    sales_amount=row.sales_amount,
                )
                for row in self.get_trends(
                    query.tenant_id, query.currency, query.start, query.end, query.interval
                )
            ],
            freshness,
        )

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]:
        freshness = self.check_readiness(query)
        return ReadResult(
            [
                ProductMetrics(
                    product_id=row.product_id,
                    total_order_count=row.order_count,
                    sales_amount=row.sales_amount,
                )
                for row in self.get_top_products(
                    query.tenant_id, query.currency, query.start, query.end, limit=query.limit
                )
            ],
            freshness,
        )

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]:
        freshness = self.check_readiness(query)
        return ReadResult(
            [
                StatusMetrics(
                    status=row.status,
                    total_order_count=row.order_count,
                    sales_amount=row.sales_amount,
                )
                for row in self.get_status_breakdown(
                    query.tenant_id, query.currency, query.start, query.end
                )
            ],
            freshness,
        )

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

        status_model = DailyStatusAggregate if use_daily else HourlyStatusAggregate
        shipped_count = self.session.scalar(
            select(func.coalesce(func.sum(status_model.order_count), 0)).where(
                status_model.tenant_id == tenant_id,
                status_model.currency == curr,
                status_model.status == "shipped",
                status_model.bucket_start >= from_utc,
                status_model.bucket_start < to_utc,
            )
        )
        if shipped_count > 0:
            aov = (total_sales / Decimal(shipped_count)).quantize(
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
        """Rank positive sales DESC, count DESC, UUID ASC before LIMIT.

        Zero-sales groups are excluded.
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
