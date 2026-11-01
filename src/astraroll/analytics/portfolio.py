"""Authorized cross-database overview; reads existing rollups without new tables."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import BoundedSemaphore
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError

from astraroll.analytics.auth import AuthenticatedPrincipal, get_authenticated_principal
from astraroll.analytics.contracts import (
    METRIC_SEMANTICS,
    Contract,
    Freshness,
    Interval,
    SummaryMetrics,
    TrendMetrics,
    TrendsQuery,
)
from astraroll.analytics.reader import (
    AnalyticsReader,
    AnalyticsUnavailable,
    UnsupportedAnalyticsWindow,
    get_analytics_reader,
)
from astraroll.analytics.service import shape_trends, validate_capabilities
from astraroll.config import get_settings
from astraroll.schemas import Currency
from astraroll.v2.registry import ControlRegistry

router = APIRouter(prefix="/analytics", tags=["portfolio"])
# Bound fan-out across concurrent overview requests in each API process.
_overview_slot = BoundedSemaphore(1)
MAX_TENANTS = 200


class PortfolioQuery(Contract):
    start: str
    end: str
    currency: Currency
    interval: Interval = "day"


class TenantOverview(Contract):
    tenant_id: UUID
    name: str
    status: Literal["ready", "unavailable"]
    metrics: SummaryMetrics | None = None
    freshness: Freshness | None = None
    error: str | None = None


class PortfolioResponse(Contract):
    start: datetime
    end: datetime
    currency: Currency
    interval: Interval
    metric_semantics: str = METRIC_SEMANTICS
    consistency: Literal["independent_tenant_snapshots"] = "independent_tenant_snapshots"
    complete: bool
    tenant_count: int
    ready_tenant_count: int
    totals: SummaryMetrics | None
    buckets: list[TrendMetrics] | None
    tenants: list[TenantOverview]
    read_started_at: datetime
    read_completed_at: datetime
    retrieval_ms: float


def get_portfolio_registry() -> ControlRegistry:
    if get_settings().deployment_mode != "database_per_tenant":
        raise HTTPException(503, "Portfolio overview requires database-per-tenant mode")
    from astraroll.v2.runtime import get_control_registry

    return get_control_registry()


def portfolio_query(
    start: str = Query(...),
    end: str = Query(...),
    currency: Currency = Query(...),
    interval: Interval = Query("day"),
) -> PortfolioQuery:
    return PortfolioQuery(start=start, end=end, currency=currency, interval=interval)


def read_portfolio(
    query: PortfolioQuery,
    principal: AuthenticatedPrincipal,
    registry: ControlRegistry,
    reader: AnalyticsReader,
) -> PortfolioResponse:
    # Reuse the exact single-tenant UTC/window validators before touching databases.
    from fastapi.exceptions import RequestValidationError
    from pydantic import ValidationError

    try:
        template = TrendsQuery(tenant_id=UUID(int=0), **query.model_dump())
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    if (
        template.interval == "hour"
        and (template.end - template.start).total_seconds() > 1000 * 3600
    ):
        raise HTTPException(422, "Portfolio hourly windows cannot exceed 1000 buckets; use daily")
    started, clock = datetime.now(UTC), perf_counter()
    try:
        records = registry.list_active_tenants()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Tenant registry unavailable") from exc
    # Filter before any tenant database access; never accept a caller-supplied DB name.
    records = [r for r in records if principal.is_admin or r.tenant_id in principal.tenant_ids]
    if len(records) > MAX_TENANTS:
        raise HTTPException(503, "Portfolio tenant limit exceeded")

    def one(record):
        scoped = template.model_copy(update={"tenant_id": record.tenant_id})
        try:
            validate_capabilities(scoped, reader.capabilities())
            result = reader.trends(scoped)
            rows = shape_trends(scoped, result.value)
            metrics = SummaryMetrics(
                total_order_count=sum(r.total_order_count for r in rows),
                sales_amount=sum((r.sales_amount for r in rows), Decimal("0.00")),
            )
            return TenantOverview(
                tenant_id=record.tenant_id,
                name=record.name,
                status="ready",
                metrics=metrics,
                freshness=result.freshness,
            ), rows
        except (AnalyticsUnavailable, UnsupportedAnalyticsWindow):
            return TenantOverview(
                tenant_id=record.tenant_id,
                name=record.name,
                status="unavailable",
                error="Rollups unavailable or selected window not covered",
            ), None

    # One repeatable-read snapshot per tenant: totals come from that same trend read.
    # Two workers cap DB pressure; no raw-order fallback or cross-DB snapshot claim.
    merged = shape_trends(template, [])
    tenant_rows = []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="portfolio") as pool:
        for row, rows in pool.map(one, records):
            tenant_rows.append(row)
            if rows is None:
                continue
            merged = [
                TrendMetrics(
                    bucket_start=a.bucket_start,
                    total_order_count=a.total_order_count + b.total_order_count,
                    sales_amount=a.sales_amount + b.sales_amount,
                )
                for a, b in zip(merged, rows, strict=True)
            ]
    complete = all(row.status == "ready" for row in tenant_rows)
    totals, buckets = None, None
    if complete:
        buckets = merged
        totals = SummaryMetrics(
            total_order_count=sum(r.total_order_count for r in buckets),
            sales_amount=sum((r.sales_amount for r in buckets), Decimal("0.00")),
        )
    tenants = sorted(
        tenant_rows,
        key=lambda row: (
            row.metrics is None,
            -row.metrics.sales_amount if row.metrics else Decimal(0),
            -row.metrics.total_order_count if row.metrics else 0,
            row.tenant_id.int,
        ),
    )
    return PortfolioResponse(
        start=template.start,
        end=template.end,
        currency=query.currency,
        interval=query.interval,
        complete=complete,
        tenant_count=len(tenants),
        ready_tenant_count=sum(r.status == "ready" for r in tenants),
        totals=totals,
        buckets=buckets,
        tenants=tenants,
        read_started_at=started,
        read_completed_at=datetime.now(UTC),
        retrieval_ms=round((perf_counter() - clock) * 1000, 2),
    )


@router.get("/portfolio", response_model=PortfolioResponse)
def portfolio(
    response: Response,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    query: Annotated[PortfolioQuery, Depends(portfolio_query)],
    registry: Annotated[ControlRegistry, Depends(get_portfolio_registry)],
    reader: Annotated[AnalyticsReader, Depends(get_analytics_reader)],
):
    response.headers["Cache-Control"] = "no-store"
    if not _overview_slot.acquire(blocking=False):
        raise HTTPException(
            429, "Portfolio refresh already running; retry shortly", headers={"Retry-After": "5"}
        )
    try:
        return read_portfolio(query, principal, registry, reader)
    finally:
        _overview_slot.release()
