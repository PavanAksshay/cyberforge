from dataclasses import dataclass
from typing import Protocol

from astraroll.analytics.contracts import (
    METRIC_SEMANTICS,
    AnalyticsQuery,
    Freshness,
    Interval,
    ProductMetrics,
    StatusMetrics,
    SummaryMetrics,
    TopProductsQuery,
    TrendMetrics,
    TrendsQuery,
)


class AnalyticsUnavailable(Exception):
    """Reader not integrated, source not ready, or no trustworthy coverage available."""


class UnsupportedAnalyticsWindow(Exception):
    """The installed aggregate reader cannot answer this exact window/interval."""


@dataclass(frozen=True)
class ReaderCapabilities:
    base_granularity: Interval
    metric_semantics: str = METRIC_SEMANTICS


@dataclass(frozen=True)
class ReadResult[T]:
    value: T
    freshness: Freshness | None = None


class AnalyticsReader(Protocol):
    """Tenant/currency-scoped aggregate reads, never a raw orders fallback.

    Every result and its optional checkpoint must describe one consistent durable
    snapshot. Empty results mean ready, covered, genuinely zero data. Raise
    AnalyticsUnavailable for uninitialized, missing or untrustworthy rollups.
    See docs/analytics.md for field semantics and B's integration requirements.
    """

    def capabilities(self) -> ReaderCapabilities: ...

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]: ...

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]:
        """Unique UTC bucket rows; omitted buckets represent known zeros."""
        ...

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]:
        """Return the true global top N (or a complete candidate set), including ties.

        Exclude zero sales; apply sales DESC, count DESC, UUID ASC before SQL LIMIT.
        API reordering cannot
        repair a candidate set that omitted a winning product at the cutoff.
        """
        ...

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]:
        """Unique status rows; omitted statuses represent known zeros."""
        ...


class UnavailableAnalyticsReader:
    def __init__(self, reason="Aggregate reader is not integrated"):
        self.reason = reason

    def capabilities(self) -> ReaderCapabilities:
        raise AnalyticsUnavailable(self.reason)

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]:
        raise AnalyticsUnavailable(self.reason)

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]:
        raise AnalyticsUnavailable(self.reason)

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]:
        raise AnalyticsUnavailable(self.reason)

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]:
        raise AnalyticsUnavailable(self.reason)


def get_analytics_reader() -> AnalyticsReader:
    from astraroll.config import get_settings

    settings = get_settings()
    if settings.analytics_read_block_reason:
        return UnavailableAnalyticsReader(settings.analytics_read_block_reason)
    if settings.analytics_backend == "postgres":
        if settings.deployment_mode == "database_per_tenant":
            from astraroll.v2.runtime import get_v2_analytics_adapter

            return get_v2_analytics_adapter()

        # Optional integration imports stay off the default application startup path.
        from astraroll.analytics.postgres import configured_reader

        return configured_reader()
    return UnavailableAnalyticsReader()
