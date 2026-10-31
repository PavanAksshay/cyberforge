"""Query-side coverage boundary. Durable coverage storage belongs to Developer B."""

from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.orm import Session

from astraroll.analytics.contracts import METRIC_SEMANTICS, AnalyticsQuery, Contract, Interval
from astraroll.analytics.reader import AnalyticsUnavailable, UnsupportedAnalyticsWindow
from astraroll.schemas import Currency

Dimension = Literal["summary", "product", "status"]


class CoverageProof(Contract):
    """A provider must obtain this from trusted durable bootstrap/coverage state.

    NOT derivable from a first processing revision or MAX(bucket_start). This is
    an integration DTO, not an ORM table or a client-supplied readiness flag.
    """

    tenant_id: UUID
    currency: Currency
    start: AwareDatetime
    end: AwareDatetime
    metric_semantics: str
    ready: bool = Field(default=False, strict=True)
    intervals: frozenset[Interval] = Field(default_factory=frozenset)
    dimensions: frozenset[Dimension] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def valid_window(self):
        if self.end <= self.start:
            raise ValueError("Coverage end must follow start")
        return self


CoverageProvider = Callable[[Session, AnalyticsQuery], CoverageProof]


def unavailable_coverage(session: Session, query: AnalyticsQuery) -> CoverageProof:
    raise AnalyticsUnavailable("Durable aggregate coverage provider is not integrated")


def require_coverage(proof: CoverageProof, query: AnalyticsQuery, dimension: Dimension):
    if (
        not proof.ready
        or proof.tenant_id != query.tenant_id
        or proof.currency != query.currency
        or proof.metric_semantics != METRIC_SEMANTICS
    ):
        raise AnalyticsUnavailable("Aggregate coverage is unready or incompatible")
    if dimension not in proof.dimensions:
        raise AnalyticsUnavailable("Aggregate dimension is not initialized")
    if query.start < proof.start or query.end > proof.end:
        raise UnsupportedAnalyticsWindow("Requested window is outside supported aggregate coverage")
