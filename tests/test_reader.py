"""Integration tests for PostgresAnalyticsReader against PostgreSQL."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from astraroll.aggregation.models import (
    HourlyProductAggregate,
    HourlyStatusAggregate,
    HourlySummaryAggregate,
    TenantProcessingState,
)
from astraroll.aggregation.reader import PostgresAnalyticsReader
from astraroll.models import Tenant

pytestmark = pytest.mark.integration


def test_reader_summary_and_aov(factory):
    tenant_id = uuid4()
    b1 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
    b2 = datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        # Summary aggregates: 2 orders in b1 ($100), 1 order in b2 ($50)
        session.add(HourlySummaryAggregate(
            tenant_id=tenant_id, currency="USD", bucket_start=b1, order_count=2, sales_amount=Decimal("100.00")
        ))
        session.add(HourlySummaryAggregate(
            tenant_id=tenant_id, currency="USD", bucket_start=b2, order_count=1, sales_amount=Decimal("50.00")
        ))
        # Status aggregates: in b1, 1 shipped ($100), 1 placed ($0); in b2, 1 shipped ($50)
        session.add(HourlyStatusAggregate(
            tenant_id=tenant_id, currency="USD", status="shipped", bucket_start=b1, order_count=1, sales_amount=Decimal("100.00")
        ))
        session.add(HourlyStatusAggregate(
            tenant_id=tenant_id, currency="USD", status="placed", bucket_start=b1, order_count=1, sales_amount=Decimal("0.00")
        ))
        session.add(HourlyStatusAggregate(
            tenant_id=tenant_id, currency="USD", status="shipped", bucket_start=b2, order_count=1, sales_amount=Decimal("50.00")
        ))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        # Query window covering both buckets
        summary = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc),
        )

        assert summary.total_orders == 3
        assert summary.total_sales == Decimal("150.00")
        # AOV = shipped sales ($150.00) / shipped count (2) = $75.00
        assert summary.average_order_value == Decimal("75.00")


def test_reader_aov_with_zero_shipped_orders(factory):
    tenant_id = uuid4()
    b1 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        # Summary aggregate: 2 placed orders with 0 sales
        session.add(HourlySummaryAggregate(
            tenant_id=tenant_id, currency="USD", bucket_start=b1, order_count=2, sales_amount=Decimal("0.00")
        ))
        # Status aggregate: 2 placed orders
        session.add(HourlyStatusAggregate(
            tenant_id=tenant_id, currency="USD", status="placed", bucket_start=b1, order_count=2, sales_amount=Decimal("0.00")
        ))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        summary = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc),
        )

        assert summary.total_orders == 2
        assert summary.total_sales == Decimal("0.00")
        assert summary.average_order_value == Decimal("0.00")


def test_reader_top_products_ranking_and_zero_sales_exclusion(factory):
    tenant_id = uuid4()
    b = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
    prod_high = uuid4()
    prod_med = uuid4()
    prod_zero = uuid4()  # Placed/cancelled only product with 0 sales

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        session.add(HourlyProductAggregate(
            tenant_id=tenant_id, currency="USD", product_id=prod_high, bucket_start=b, order_count=2, sales_amount=Decimal("200.00")
        ))
        session.add(HourlyProductAggregate(
            tenant_id=tenant_id, currency="USD", product_id=prod_med, bucket_start=b, order_count=5, sales_amount=Decimal("50.00")
        ))
        session.add(HourlyProductAggregate(
            tenant_id=tenant_id, currency="USD", product_id=prod_zero, bucket_start=b, order_count=3, sales_amount=Decimal("0.00")
        ))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        top_prods = reader.get_top_products(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc),
            limit=10,
        )

        # prod_zero is excluded because its recognized sales_amount is zero!
        assert len(top_prods) == 2
        assert top_prods[0].product_id == prod_high
        assert top_prods[0].rank == 1
        assert top_prods[0].sales_amount == Decimal("200.00")

        assert top_prods[1].product_id == prod_med
        assert top_prods[1].rank == 2
        assert top_prods[1].sales_amount == Decimal("50.00")


def test_reader_status_breakdown_all_three_statuses(factory):
    tenant_id = uuid4()
    b = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        # Only 'shipped' is present in DB
        session.add(HourlyStatusAggregate(
            tenant_id=tenant_id, currency="USD", status="shipped", bucket_start=b, order_count=4, sales_amount=Decimal("120.00")
        ))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        breakdown = reader.get_status_breakdown(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc),
        )

        # All 3 statuses must be returned!
        assert len(breakdown) == 3
        statuses = {item.status: item for item in breakdown}

        assert "placed" in statuses
        assert statuses["placed"].order_count == 0
        assert statuses["placed"].sales_amount == Decimal("0.00")

        assert "shipped" in statuses
        assert statuses["shipped"].order_count == 4
        assert statuses["shipped"].sales_amount == Decimal("120.00")

        assert "cancelled" in statuses
        assert statuses["cancelled"].order_count == 0
        assert statuses["cancelled"].sales_amount == Decimal("0.00")


def test_reader_freshness_uninitialized_and_initialized(factory):
    tenant_uninit = uuid4()
    tenant_init = uuid4()
    now_utc = datetime(2026, 10, 31, 15, 30, 0, tzinfo=timezone.utc)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_uninit, name="Uninitialized Tenant"))
        session.add(Tenant(id=tenant_init, name="Initialized Tenant"))
        session.add(TenantProcessingState(
            tenant_id=tenant_init, revision=42, last_successful_apply_at=now_utc, last_successful_stream_id="5000-0"
        ))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)

        # Uninitialized tenant
        f_uninit = reader.get_freshness(tenant_uninit)
        assert f_uninit.is_initialized is False
        assert f_uninit.revision == 0
        assert f_uninit.last_applied_at is None

        # Initialized tenant
        f_init = reader.get_freshness(tenant_init)
        assert f_init.is_initialized is True
        assert f_init.revision == 42
        assert f_init.last_applied_at == now_utc


def test_reader_range_half_open_boundaries(factory):
    tenant_id = uuid4()
    b_before = datetime(2026, 10, 31, 9, 0, 0, tzinfo=timezone.utc)
    b_in = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
    b_edge = datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc)  # Boundary 'to_ts'

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        session.add(HourlySummaryAggregate(
            tenant_id=tenant_id, currency="USD", bucket_start=b_before, order_count=1, sales_amount=Decimal("10.00")
        ))
        session.add(HourlySummaryAggregate(
            tenant_id=tenant_id, currency="USD", bucket_start=b_in, order_count=1, sales_amount=Decimal("20.00")
        ))
        session.add(HourlySummaryAggregate(
            tenant_id=tenant_id, currency="USD", bucket_start=b_edge, order_count=1, sales_amount=Decimal("40.00")
        ))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        # Range is [10:00, 11:00) -> should ONLY include b_in
        summary = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc),
        )
        assert summary.total_orders == 1
        assert summary.total_sales == Decimal("20.00")
