"""Precomputed aggregate fixtures only. Never installed as the application reader."""

from dataclasses import dataclass, field
from decimal import Decimal

from astraroll.analytics.contracts import (
    Freshness,
    ProductMetrics,
    StatusMetrics,
    SummaryMetrics,
    TrendMetrics,
)
from astraroll.analytics.reader import ReaderCapabilities, ReadResult


@dataclass
class AggregateView:
    summary: SummaryMetrics = field(
        default_factory=lambda: SummaryMetrics(total_order_count=0, sales_amount=Decimal("0.00"))
    )
    trends: dict[str, list[TrendMetrics]] = field(default_factory=dict)
    products: list[ProductMetrics] = field(default_factory=list)
    statuses: list[StatusMetrics] = field(default_factory=list)
    freshness: Freshness | None = None


class FakeAnalyticsReader:
    def __init__(self, granularity="hour"):
        self.granularity = granularity
        self.views = {}
        self.calls = []

    def capabilities(self):
        return ReaderCapabilities(base_granularity=self.granularity)

    def view(self, operation, query):
        self.calls.append((operation, query))
        key = (query.tenant_id, query.currency, query.start, query.end)
        return self.views.get(key, AggregateView())

    def summary(self, query):
        view = self.view("summary", query)
        return ReadResult(view.summary, view.freshness)

    def trends(self, query):
        view = self.view("trends", query)
        return ReadResult(view.trends.get(query.interval, []), view.freshness)

    def top_products(self, query):
        view = self.view("top_products", query)
        return ReadResult(view.products, view.freshness)

    def status_breakdown(self, query):
        view = self.view("status_breakdown", query)
        return ReadResult(view.statuses, view.freshness)
