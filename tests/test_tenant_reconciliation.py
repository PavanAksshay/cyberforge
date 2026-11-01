"""Rebuild corrupt projections/rollups without changing source or outbox history."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from astraroll.aggregation.bootstrap import stage_batch, start_job
from astraroll.aggregation.models import HourlySummaryAggregate, OrderProjection
from astraroll.aggregation.release_models import AggregateCoverage, BootstrapJob
from astraroll.orders import create_order, mutate_order
from astraroll.schemas import OrderCreate, OrderUpdate
from astraroll.v2.reconcile import normalize, repair_tenant, source_snapshot

pytestmark = pytest.mark.integration


def test_repair_corrects_corruption_preserves_all_source_fields_and_resumes(factory, tenant_id):
    with factory.begin() as s:
        order = create_order(
            s, tenant_id, OrderCreate(product_id=uuid4(), amount="506.97", currency="USD")
        )
        mutate_order(s, tenant_id, order.id, 1, OrderUpdate(expected_version=1, status="shipped"))
        s.add(
            OrderProjection(
                tenant_id=tenant_id,
                order_id=order.id,
                last_applied_version=1,
                canonical_snapshot={"broken": True},
                updated_at=datetime.now(UTC),
            )
        )
        s.add(
            HourlySummaryAggregate(
                tenant_id=tenant_id,
                currency="USD",
                bucket_start=datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
                order_count=1,
                sales_amount=Decimal("999.00"),
            )
        )
    with factory() as s:
        before = normalize(source_snapshot(s, tenant_id))
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    job = start_job(factory, tenant_id, ["USD"], start, start + timedelta(days=1))
    assert stage_batch(factory, job, 1) == 1
    record = {"before": before, "job_id": str(job), "phase": "building"}
    checkpoints = []
    repair_tenant(
        factory,
        tenant_id,
        record,
        start,
        start + timedelta(days=1),
        lambda: checkpoints.append(record["phase"]),
        batch_size=1,
    )
    assert record["phase"] == "verified" and record["archived_rows"] >= 2
    assert "verified" in checkpoints
    with factory() as s:
        assert normalize(source_snapshot(s, tenant_id)) == before
        assert s.scalar(
            select(HourlySummaryAggregate.sales_amount).where(
                HourlySummaryAggregate.tenant_id == tenant_id
            )
        ) == Decimal("506.97")
        coverage = s.get(AggregateCoverage, (tenant_id, "USD"))
        assert coverage.status == "ready" and coverage.source_order_count == 1
        activated = s.get(BootstrapJob, job).activated_at
        processed = s.scalar(
            text(
                "SELECT last_successful_apply_at FROM tenant_processing_state WHERE tenant_id=:tid"
            ),
            {"tid": tenant_id},
        )
        assert activated == processed
    revision = record["revision"]
    repair_tenant(
        factory, tenant_id, record, start, start + timedelta(days=1), lambda: None, batch_size=1
    )
    assert record["revision"] == revision


def test_repair_rejects_changed_source_before_touching_coverage(factory, tenant_id):
    with factory() as s:
        before = normalize(source_snapshot(s, tenant_id))
    with factory.begin() as s:
        create_order(s, tenant_id, OrderCreate(product_id=uuid4(), amount="1.00", currency="EUR"))
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    record = {"before": before, "phase": "planned"}
    with pytest.raises(ValueError, match="changed since manifest"):
        repair_tenant(factory, tenant_id, record, start, start + timedelta(days=1), lambda: None)
    assert "job_id" not in record
