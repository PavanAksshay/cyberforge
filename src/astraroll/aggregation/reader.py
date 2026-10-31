"""PostgreSQL implementation of AnalyticsReader for AstraRoll Aggregation Engine (Phase 2).

Provides high-performance analytics queries reading exclusively from pre-aggregated
durable PostgreSQL rollup tables (never querying raw orders table).

Methods implemented:
- get_summary(tenant_id, currency, from_ts, to_ts, granularity="hour")
- get_trends(tenant_id, currency, from_ts, to_ts, interval="hour")
- get_top_products(tenant_id, currency, from_ts, to_ts, granularity="hour", limit=10)
- get_status_breakdown(tenant_id, currency, from_ts, to_ts, granularity="hour")
- get_freshness(tenant_id)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Protocol, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
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
    last_applied_at: Optional[datetime]
    is_initialized: bool


class AnalyticsReader(Protocol):
    """Protocol defining the analytics reader contract agreed with Developer A."""

    def get_summary(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
    ) -> SummaryData: ...

    def get_trends(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        interval: str = "hour",
    ) -> List[TrendPoint]: ...

    def get_top_products(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
        limit: int = 10,
    ) -> List[TopProductItem]: ...

    def get_status_breakdown(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
    ) -> List[StatusBreakdownItem]: ...

    def get_freshness(self, tenant_id: UUID) -> FreshnessData: ...


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_daily(granularity: str) -> bool:
    g = granularity.strip().lower()
    return g in ("day", "daily")


class PostgresAnalyticsReader:
    """PostgreSQL implementation of AnalyticsReader querying durable aggregate tables."""

    def __init__(self, session: Session):
        self.session = session

    def get_summary(
        self,
        tenant_id: UUID,
        currency: str,
        from_ts: datetime,
        to_ts: datetime,
        granularity: str = "hour",
    ) -> SummaryData:
        """Calculate total orders, total recognized sales, and accurate AOV over [from_ts, to_ts).

        AOV denominator is strictly based on shipped orders count (obtained from status aggregates).
        """
        from_utc = _ensure_utc(from_ts)
        to_utc = _ensure_utc(to_ts)
        curr = currency.strip().upper()
        use_daily = _is_daily(granularity)

        # 1. Query total orders and sales from summary table
        summary_model = DailySummaryAggregate if use_daily else HourlySummaryAggregate
        summary_stmt = (
            select(
                func.coalesce(func.sum(summary_model.order_count), 0).label("total_orders"),
                func.coalesce(func.sum(summary_model.sales_amount), Decimal("0.00")).label("total_sales"),
            )
            .where(
                summary_model.tenant_id == tenant_id,
                summary_model.currency == curr,
                summary_model.bucket_start >= from_utc,
                summary_model.bucket_start < to_utc,
            )
        )
        row = self.session.execute(summary_stmt).one()
        total_orders = int(row.total_orders)
        total_sales = Decimal(str(row.total_sales))

        # 2. Query recognized shipped sales and shipped orders count from status table
        status_model = DailyStatusAggregate if use_daily else HourlyStatusAggregate
        status_stmt = (
            select(
                func.coalesce(func.sum(status_model.order_count), 0).label("shipped_orders"),
                func.coalesce(func.sum(status_model.sales_amount), Decimal("0.00")).label("shipped_sales"),
            )
            .where(
                status_model.tenant_id == tenant_id,
                status_model.currency == curr,
                status_model.status == "shipped",
                status_model.bucket_start >= from_utc,
                status_model.bucket_start < to_utc,
            )
        )
        status_row = self.session.execute(status_stmt).one()
        shipped_orders = int(status_row.shipped_orders)
        shipped_sales = Decimal(str(status_row.shipped_sales))

        # AOV calculation: recognized shipped sales / shipped order count
        if shipped_orders > 0:
            aov = (shipped_sales / Decimal(shipped_orders)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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
    ) -> List[TrendPoint]:
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
    ) -> List[TopProductItem]:
        """Rank products by sales_amount DESC, order_count DESC, product_id ASC.

        Excludes products whose recognized sales_amount is zero (placed-only / cancelled-only products).
        """
        from_utc = _ensure_utc(from_ts)
        to_utc = _ensure_utc(to_ts)
        curr = currency.strip().upper()
        bounded_limit = max(1, min(limit, 100))
        product_model = DailyProductAggregate if _is_daily(granularity) else HourlyProductAggregate

        sum_sales = func.sum(product_model.sales_amount).label("total_sales")
        sum_orders = func.sum(product_model.order_count).label("total_orders")

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
            .having(sum_sales > Decimal("0.00"))  # Exclude zero-sales products
            .order_by(
                desc(sum_sales),
                desc(sum_orders),
                product_model.product_id.asc(),
            )
            .limit(bounded_limit)
        )

        rows = self.session.execute(stmt).all()
        result: List[TopProductItem] = []
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
    ) -> List[StatusBreakdownItem]:
        """Return breakdown for all 3 statuses (placed, shipped, cancelled).

        Always includes all 3 statuses even if aggregate rows are absent, defaulting to 0.
        """
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
        result: List[StatusBreakdownItem] = []
        for st in canonical_statuses:
            if st in mapped:
                cnt, sls = mapped[st]
            else:
                cnt, sls = 0, Decimal("0.00")
            result.append(
                StatusBreakdownItem(
                    status=st,
                    order_count=cnt,
                    sales_amount=sls,
                )
            )
        return result

    def get_freshness(self, tenant_id: UUID) -> FreshnessData:
        """Fetch tenant processing revision and apply timestamp.

        If uninitialized, returns is_initialized=False and revision=0 without inventing timestamps.
        """
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
