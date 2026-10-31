from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from astraroll.analytics.contracts import METRIC_SEMANTICS, AnalyticsQuery
from astraroll.analytics.coverage import CoverageProof, require_coverage, unavailable_coverage
from astraroll.analytics.reader import AnalyticsUnavailable, UnsupportedAnalyticsWindow


@pytest.fixture
def query():
    return AnalyticsQuery(
        tenant_id=uuid4(),
        currency="USD",
        start=datetime(2026, 10, 1, tzinfo=UTC),
        end=datetime(2026, 10, 2, tzinfo=UTC),
    )


def proof_for(query, **changes):
    return CoverageProof(
        tenant_id=query.tenant_id,
        currency=query.currency,
        start=query.start,
        end=query.end,
        metric_semantics=METRIC_SEMANTICS,
        **{
            "ready": True,
            "intervals": {"hour", "day"},
            "dimensions": {"summary", "status", "product"},
            **changes,
        },
    )


def test_missing_durable_provider_never_declares_readiness(query):
    with pytest.raises(AnalyticsUnavailable):
        unavailable_coverage(None, query)


def test_matching_ready_proof_can_be_used(query):
    require_coverage(proof_for(query), query, "summary")


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", uuid4()),
        ("currency", "EUR"),
        ("ready", False),
        ("metric_semantics", "different-metrics"),
        ("dimensions", {"product"}),
    ],
)
def test_unready_or_wrong_scope_is_unavailable(query, field, value):
    original = proof_for(query)
    changed = original.model_copy(update={field: value})
    with pytest.raises(AnalyticsUnavailable):
        require_coverage(changed, query, "summary")


def test_outside_known_retention_is_unsupported(query):
    proof = proof_for(query).model_copy(update={"start": query.start + timedelta(hours=1)})
    with pytest.raises(UnsupportedAnalyticsWindow):
        require_coverage(proof, query, "summary")
