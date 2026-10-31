"""Integration tests for OrderEventProcessor against PostgreSQL."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from astraroll.aggregation.contracts import (
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
)
from astraroll.aggregation.models import (
    HourlyProductAggregate,
    HourlyStatusAggregate,
    HourlySummaryAggregate,
    OrderProjection,
    ProcessedEvent,
    TenantProcessingState,
)
from astraroll.aggregation.processor import OrderEventProcessor
from astraroll.models import Order, Tenant

pytestmark = pytest.mark.integration


def test_order_event_processor_lifecycle(factory):
    tenant_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    event_1_id = uuid4()
    event_2_id = uuid4()
    now_utc = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))

    # Step 1: Process v1 'placed' ($100.00)
    with factory() as session:
        processor = OrderEventProcessor(session)
        evt_1 = OrderEvent(
            event_id=str(event_1_id),
            tenant_id=str(tenant_id),
            order_id=str(order_id),
            order_version=1,
            event_type="order.created",
            occurred_at=now_utc,
            snapshot=OrderSnapshot(
                product_id=str(product_id),
                amount_cents=10000,
                currency="USD",
                status=OrderStatus.PLACED,
                created_at=now_utc,
                updated_at=now_utc,
                order_id=str(order_id),
                tenant_id=str(tenant_id),
            ),
        )
        res_1 = processor.process_event(evt_1, stream_id="1000-0")
        session.commit()

        assert res_1.applied is True
        assert res_1.is_duplicate is False
        assert res_1.is_out_of_order is False
        assert res_1.tenant_revision == 1

    # Verify DB state after v1
    with factory() as session:
        # Check projection
        proj = session.get(OrderProjection, (tenant_id, order_id))
        assert proj is not None
        assert proj.last_applied_version == 1
        assert proj.last_event_id == event_1_id

        # Check deduplication record
        proc_evt = session.get(ProcessedEvent, event_1_id)
        assert proc_evt is not None
        assert proc_evt.order_version == 1

        # Check summary aggregate (placed contributes 0 sales!)
        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
                HourlySummaryAggregate.bucket_start == now_utc,
            )
        )
        assert summary is not None
        assert summary.order_count == 1
        assert summary.sales_amount == Decimal("0.00")

        # Check status aggregate
        placed_status = session.scalar(
            select(HourlyStatusAggregate).where(
                HourlyStatusAggregate.tenant_id == tenant_id,
                HourlyStatusAggregate.currency == "USD",
                HourlyStatusAggregate.status == "placed",
                HourlyStatusAggregate.bucket_start == now_utc,
            )
        )
        assert placed_status is not None
        assert placed_status.order_count == 1
        assert placed_status.sales_amount == Decimal("0.00")

    # Step 2: Process v2 'shipped' ($100.00)
    with factory() as session:
        processor = OrderEventProcessor(session)
        evt_2 = OrderEvent(
            event_id=str(event_2_id),
            tenant_id=str(tenant_id),
            order_id=str(order_id),
            order_version=2,
            event_type="order.updated",
            occurred_at=now_utc,
            snapshot=OrderSnapshot(
                product_id=str(product_id),
                amount_cents=10000,
                currency="USD",
                status=OrderStatus.SHIPPED,
                created_at=now_utc,
                updated_at=now_utc,
                order_id=str(order_id),
                tenant_id=str(tenant_id),
            ),
        )
        res_2 = processor.process_event(evt_2, stream_id="1001-0")
        session.commit()

        assert res_2.applied is True
        assert res_2.tenant_revision == 2

    # Verify DB state after v2 (shipped contributes $100.00 sales!)
    with factory() as session:
        proj = session.get(OrderProjection, (tenant_id, order_id))
        assert proj.last_applied_version == 2

        summary = session.scalar(
            select(HourlySummaryAggregate).where(
                HourlySummaryAggregate.tenant_id == tenant_id,
                HourlySummaryAggregate.currency == "USD",
                HourlySummaryAggregate.bucket_start == now_utc,
            )
        )
        assert summary.order_count == 1
        assert summary.sales_amount == Decimal("100.00")

        placed_status = session.scalar(
            select(HourlyStatusAggregate).where(
                HourlyStatusAggregate.tenant_id == tenant_id,
                HourlyStatusAggregate.currency == "USD",
                HourlyStatusAggregate.status == "placed",
                HourlyStatusAggregate.bucket_start == now_utc,
            )
        )
        assert placed_status.order_count == 0
        assert placed_status.sales_amount == Decimal("0.00")

        shipped_status = session.scalar(
            select(HourlyStatusAggregate).where(
                HourlyStatusAggregate.tenant_id == tenant_id,
                HourlyStatusAggregate.currency == "USD",
                HourlyStatusAggregate.status == "shipped",
                HourlyStatusAggregate.bucket_start == now_utc,
            )
        )
        assert shipped_status.order_count == 1
        assert shipped_status.sales_amount == Decimal("100.00")

        product_agg = session.scalar(
            select(HourlyProductAggregate).where(
                HourlyProductAggregate.tenant_id == tenant_id,
                HourlyProductAggregate.currency == "USD",
                HourlyProductAggregate.product_id == product_id,
                HourlyProductAggregate.bucket_start == now_utc,
            )
        )
        assert product_agg.order_count == 1
        assert product_agg.sales_amount == Decimal("100.00")

    # Step 3: Duplicate delivery of event 2 produces no change
    with factory() as session:
        processor = OrderEventProcessor(session)
        res_dup = processor.process_event(evt_2, stream_id="1001-0")
        session.commit()

        assert res_dup.applied is False
        assert res_dup.is_duplicate is True

    # Step 4: Stale / out-of-order event 1 produces no change
    with factory() as session:
        processor = OrderEventProcessor(session)
        res_stale = processor.process_event(evt_1, stream_id="1000-0")
        session.commit()

        assert res_stale.applied is False
        assert res_stale.is_out_of_order is False  # Already in processed_events -> duplicate check fires first
