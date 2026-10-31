"""Validate aggregate responses and shape query results; no persistence or reducers."""

from datetime import timedelta
from decimal import Decimal

from astraroll.analytics.contracts import (
    METRIC_SEMANTICS,
    AnalyticsQuery,
    Interval,
    ProductMetrics,
    StatusMetrics,
    TrendMetrics,
    TrendsQuery,
)
from astraroll.analytics.reader import (
    AnalyticsUnavailable,
    ReaderCapabilities,
    UnsupportedAnalyticsWindow,
)


def validate_capabilities(query: AnalyticsQuery, capabilities: ReaderCapabilities):
    if capabilities.metric_semantics != METRIC_SEMANTICS or capabilities.base_granularity not in (
        "hour",
        "day",
    ):
        raise AnalyticsUnavailable("Reader metric contract is incompatible")
    interval: Interval | None = query.interval if isinstance(query, TrendsQuery) else None
    if interval == "hour" and capabilities.base_granularity != "hour":
        raise UnsupportedAnalyticsWindow("Hourly trends require hourly aggregate coverage")
    if capabilities.base_granularity == "day" and (query.start.hour or query.end.hour):
        raise UnsupportedAnalyticsWindow("Daily aggregates require UTC midnight boundaries")


def shape_trends(query: TrendsQuery, rows: list[TrendMetrics]) -> list[TrendMetrics]:
    step = timedelta(hours=1) if query.interval == "hour" else timedelta(days=1)
    by_bucket = {}
    for row in rows:
        at = row.bucket_start
        if (
            at.utcoffset() != timedelta(0)
            or not query.start <= at < query.end
            or (at - query.start) % step != timedelta(0)
            or at in by_bucket
        ):
            raise AnalyticsUnavailable("Reader returned invalid or duplicate trend buckets")
        by_bucket[at] = row
    buckets = []
    at = query.start
    while at < query.end:
        buckets.append(
            by_bucket.get(at)
            or TrendMetrics(bucket_start=at, total_order_count=0, sales_amount=Decimal("0.00"))
        )
        at += step
    return buckets


def shape_products(rows: list[ProductMetrics], limit: int) -> list[ProductMetrics]:
    if len({row.product_id for row in rows}) != len(rows):
        raise AnalyticsUnavailable("Reader returned duplicate product groups")
    eligible = (row for row in rows if row.sales_amount > 0)
    return sorted(
        eligible,
        key=lambda row: (
            row.sales_amount.copy_negate(),
            -row.total_order_count,
            row.product_id.int,
        ),
    )[:limit]


def shape_statuses(rows: list[StatusMetrics]) -> list[StatusMetrics]:
    by_status = {row.status: row for row in rows}
    if len(by_status) != len(rows):
        raise AnalyticsUnavailable("Reader returned duplicate status groups")
    return [
        by_status.get(status)
        or StatusMetrics(status=status, total_order_count=0, sales_amount=Decimal("0.00"))
        for status in ("placed", "shipped", "cancelled")
    ]
