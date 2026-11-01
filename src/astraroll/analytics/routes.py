from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from astraroll.analytics.auth import TenantContext, get_authorized_tenant
from astraroll.analytics.contracts import (
    AnalyticsQuery,
    Interval,
    StatusBreakdownResponse,
    SummaryResponse,
    TopProductsQuery,
    TopProductsResponse,
    TrendsQuery,
    TrendsResponse,
)
from astraroll.analytics.reader import (
    AnalyticsReader,
    AnalyticsUnavailable,
    ReadResult,
    UnsupportedAnalyticsWindow,
    get_analytics_reader,
)
from astraroll.analytics.service import (
    shape_products,
    shape_statuses,
    shape_trends,
    validate_capabilities,
)
from astraroll.schemas import Currency

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    responses={
        401: {"description": "Verified identity required"},
        403: {"description": "Identity cannot access requested tenant"},
        503: {"description": "Identity verifier or trustworthy aggregate reader unavailable"},
    },
)
Reader = Annotated[AnalyticsReader, Depends(get_analytics_reader)]
Tenant = Annotated[TenantContext, Depends(get_authorized_tenant)]


def _parse_query_params[T](request: Request, model_cls: type[T]) -> T:
    try:
        return model_cls.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def get_summary_query(
    request: Request,
    tenant_id: UUID = Query(...),
    currency: Currency = Query(...),
    start: str = Query(...),
    end: str = Query(...),
) -> AnalyticsQuery:
    return _parse_query_params(request, AnalyticsQuery)


def get_trends_query(
    request: Request,
    tenant_id: UUID = Query(...),
    currency: Currency = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    interval: Interval = Query("day"),
) -> TrendsQuery:
    return _parse_query_params(request, TrendsQuery)


def get_top_products_query(
    request: Request,
    tenant_id: UUID = Query(...),
    currency: Currency = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    limit: int = Query(10, ge=1, le=10),
) -> TopProductsQuery:
    return _parse_query_params(request, TopProductsQuery)


def get_status_breakdown_query(
    request: Request,
    tenant_id: UUID = Query(...),
    currency: Currency = Query(...),
    start: str = Query(...),
    end: str = Query(...),
) -> AnalyticsQuery:
    return _parse_query_params(request, AnalyticsQuery)


def read[T](
    query: AnalyticsQuery,
    tenant: TenantContext,
    reader: AnalyticsReader,
    operation: Callable[[], ReadResult[T]],
) -> ReadResult[T]:
    if tenant.tenant_id != query.tenant_id:
        raise HTTPException(403, "Tenant access denied")
    try:
        validate_capabilities(query, reader.capabilities())
        return operation()
    except UnsupportedAnalyticsWindow as exc:
        raise HTTPException(
            422, {"code": "unsupported_analytics_window", "message": str(exc)}
        ) from exc
    except AnalyticsUnavailable as exc:
        raise HTTPException(503, {"code": "analytics_unavailable", "message": str(exc)}) from exc


def response_fields(query: AnalyticsQuery, result: ReadResult):
    return {
        "tenant_id": query.tenant_id,
        "currency": query.currency,
        "start": query.start,
        "end": query.end,
        "freshness": result.freshness,
    }


@router.get("/summary", response_model=SummaryResponse)
def summary(
    query: Annotated[AnalyticsQuery, Depends(get_summary_query)], tenant: Tenant, reader: Reader
):
    result = read(query, tenant, reader, lambda: reader.summary(query))
    return SummaryResponse(**response_fields(query, result), **result.value.model_dump())


@router.get("/trends", response_model=TrendsResponse)
def trends(
    query: Annotated[TrendsQuery, Depends(get_trends_query)], tenant: Tenant, reader: Reader
):
    def operation():
        result = reader.trends(query)
        return ReadResult(shape_trends(query, result.value), result.freshness)

    result = read(query, tenant, reader, operation)
    return TrendsResponse(
        **response_fields(query, result), interval=query.interval, buckets=result.value
    )


@router.get("/top-products", response_model=TopProductsResponse)
def top_products(
    query: Annotated[TopProductsQuery, Depends(get_top_products_query)],
    tenant: Tenant,
    reader: Reader,
):
    def operation():
        result = reader.top_products(query)
        return ReadResult(shape_products(result.value, query.limit), result.freshness)

    result = read(query, tenant, reader, operation)
    return TopProductsResponse(
        **response_fields(query, result), limit=query.limit, products=result.value
    )


@router.get("/status-breakdown", response_model=StatusBreakdownResponse)
def status_breakdown(
    query: Annotated[AnalyticsQuery, Depends(get_status_breakdown_query)],
    tenant: Tenant,
    reader: Reader,
):
    def operation():
        result = reader.status_breakdown(query)
        return ReadResult(shape_statuses(result.value), result.freshness)

    result = read(query, tenant, reader, operation)
    return StatusBreakdownResponse(**response_fields(query, result), statuses=result.value)
