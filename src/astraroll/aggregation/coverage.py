from datetime import UTC, datetime

from astraroll.aggregation.release_models import AggregateCoverage, BootstrapJob
from astraroll.analytics.contracts import METRIC_SEMANTICS, AnalyticsQuery
from astraroll.analytics.coverage import CoverageProof
from astraroll.analytics.reader import AnalyticsUnavailable
from astraroll.config import get_settings


def read_coverage(session, query: AnalyticsQuery) -> CoverageProof:
    row = session.get(AggregateCoverage, (query.tenant_id, query.currency))
    if row is None or row.status != "ready" or row.verified_at is None:
        raise AnalyticsUnavailable("Tenant/currency baseline is not initialized")
    job = session.get(BootstrapJob, row.job_id)
    if (
        job is None
        or job.status != "ready"
        or row.metric_semantics != METRIC_SEMANTICS
        or job.tenant_id != query.tenant_id
        or job.metric_semantics != METRIC_SEMANTICS
    ):
        raise AnalyticsUnavailable("Coverage generation or metric semantics is invalid")
    age = (datetime.now(UTC) - row.verified_at).total_seconds()
    if age > get_settings().coverage_max_age_seconds:
        raise AnalyticsUnavailable("Coverage verification is stale; reconcile or rebuild")
    return CoverageProof(
        tenant_id=row.tenant_id,
        currency=row.currency,
        start=row.start,
        end=row.end,
        metric_semantics=row.metric_semantics,
        ready=True,
        intervals={"hour", "day"},
        dimensions={"summary", "status", "product"},
    )
