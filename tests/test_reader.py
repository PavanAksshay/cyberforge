"""Integration tests for PostgresAnalyticsReader against PostgreSQL."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from astraroll.aggregation.models import (
    HourlyProductAggregate,
    HourlyStatusAggregate,
    HourlySummaryAggregate,
    TenantProcessingState,
)
from astraroll.aggregation.reader import (
    AnalyticsQuery,
    AnalyticsUnavailable,
    PostgresAnalyticsReader,
    TopProductsQuery,
    TrendsQuery,
)
from astraroll.models import Tenant

pytestmark = pytest.mark.integration


def test_reader_summary_and_aov(factory):
    tenant_id = uuid4()
    b1 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    b2 = datetime(2026, 10, 31, 11, 0, 0, tzinfo=UTC)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        # Summary aggregates: 2 orders in b1 ($100), 1 order in b2 ($50)
        session.add(
            HourlySummaryAggregate(
                tenant_id=tenant_id,
                currency="USD",
                bucket_start=b1,
                order_count=2,
                sales_amount=Decimal("100.00"),
            )
        )
        session.add(
            HourlySummaryAggregate(
                tenant_id=tenant_id,
                currency="USD",
                bucket_start=b2,
                order_count=1,
                sales_amount=Decimal("50.00"),
            )
        )

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        # Query window covering both buckets
        summary = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
            to_ts=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC),
        )

        assert summary.total_orders == 3
        assert summary.total_sales == Decimal("150.00")
        # AOV = total sales ($150.00) / total orders (3) = $50.00
        assert summary.average_order_value == Decimal("50.00")


def test_reader_aov_with_zero_orders(factory):
    tenant_id = uuid4()
    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        summary = reader.get_summary(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=UTC),
        )

        assert summary.total_orders == 0
        assert summary.total_sales == Decimal("0.00")
        assert summary.average_order_value == Decimal("0.00")


def test_reader_top_products_ranking_and_zero_sales_inclusion(factory):
    tenant_id = uuid4()
    b = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    prod_high = uuid4()
    prod_med = uuid4()
    prod_zero = uuid4()  # Placed/cancelled only product with 0 sales

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        session.add(
            HourlyProductAggregate(
                tenant_id=tenant_id,
                currency="USD",
                product_id=prod_high,
                bucket_start=b,
                order_count=2,
                sales_amount=Decimal("200.00"),
            )
        )
        session.add(
            HourlyProductAggregate(
                tenant_id=tenant_id,
                currency="USD",
                product_id=prod_med,
                bucket_start=b,
                order_count=5,
                sales_amount=Decimal("50.00"),
            )
        )
        session.add(
            HourlyProductAggregate(
                tenant_id=tenant_id,
                currency="USD",
                product_id=prod_zero,
                bucket_start=b,
                order_count=3,
                sales_amount=Decimal("0.00"),
            )
        )

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        top_prods = reader.get_top_products(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=UTC),
            limit=10,
        )

        # prod_zero is INCLUDED because order_count >= 1 even if sales_amount == 0.00
        assert len(top_prods) == 3
        assert top_prods[0].product_id == prod_high
        assert top_prods[0].rank == 1
        assert top_prods[0].sales_amount == Decimal("200.00")

        assert top_prods[1].product_id == prod_med
        assert top_prods[1].rank == 2
        assert top_prods[1].sales_amount == Decimal("50.00")

        assert top_prods[2].product_id == prod_zero
        assert top_prods[2].rank == 3
        assert top_prods[2].sales_amount == Decimal("0.00")
        assert top_prods[2].order_count == 3


def test_reader_status_breakdown_all_three_statuses(factory):
    tenant_id = uuid4()
    b = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_id, name="Test Tenant"))
        # Only 'shipped' is present in DB
        session.add(
            HourlyStatusAggregate(
                tenant_id=tenant_id,
                currency="USD",
                status="shipped",
                bucket_start=b,
                order_count=4,
                sales_amount=Decimal("120.00"),
            )
        )

    with factory() as session:
        reader = PostgresAnalyticsReader(session)
        breakdown = reader.get_status_breakdown(
            tenant_id=tenant_id,
            currency="USD",
            from_ts=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
            to_ts=datetime(2026, 10, 31, 11, 0, 0, tzinfo=UTC),
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
    now_utc = datetime(2026, 10, 31, 15, 30, 0, tzinfo=UTC)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_uninit, name="Uninitialized Tenant"))
        session.add(Tenant(id=tenant_init, name="Initialized Tenant"))
        session.add(
            TenantProcessingState(
                tenant_id=tenant_init,
                revision=42,
                last_successful_apply_at=now_utc,
                last_successful_stream_id="5000-0",
            )
        )

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


def test_reader_protocol_readiness_and_empty_windows(factory):
    tenant_uninit = uuid4()
    tenant_init = uuid4()
    now_utc = datetime(2026, 10, 31, 15, 30, 0, tzinfo=UTC)
    start_ts = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    end_ts = datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC)

    with factory.begin() as session:
        session.add(Tenant(id=tenant_uninit, name="Uninit"))
        session.add(Tenant(id=tenant_init, name="Init"))
        session.add(
            TenantProcessingState(
                tenant_id=tenant_init,
                revision=10,
                last_successful_apply_at=now_utc,
                last_successful_stream_id="10-0",
            )
        )

    with factory() as session:
        reader = PostgresAnalyticsReader(session)

        # 1. Uninitialized tenant raises AnalyticsUnavailable
        q_uninit = AnalyticsQuery(
            tenant_id=tenant_uninit,
            start=start_ts,
            end=end_ts,
            currency="USD",
        )
        with pytest.raises(AnalyticsUnavailable, match="uninitialized or no trustworthy coverage"):
            reader.summary(q_uninit)

        # 2. Initialized tenant with empty window returns covered zeros with Freshness
        q_init = AnalyticsQuery(
            tenant_id=tenant_init,
            start=start_ts,
            end=end_ts,
            currency="USD",
        )
        res = reader.summary(q_init)
        assert res.value.total_order_count == 0
        assert res.value.sales_amount == Decimal("0.00")
        assert res.freshness is not None
        assert res.freshness.checkpoint == "10"
        assert res.freshness.last_processed_at == now_utc

        # 3. Trends protocol
        t_res = reader.trends(
            TrendsQuery(
                tenant_id=tenant_init,
                start=start_ts,
                end=end_ts,
                currency="USD",
                interval="hour",
            )
        )
        assert t_res.value == []
        assert t_res.freshness.checkpoint == "10"

        # 4. Top products protocol
        tp_res = reader.top_products(
            TopProductsQuery(
                tenant_id=tenant_init,
                start=start_ts,
                end=end_ts,
                currency="USD",
                limit=5,
            )
        )
        assert tp_res.value == []
        assert tp_res.freshness.checkpoint == "10"

        # 5. Status breakdown protocol
        sb_res = reader.status_breakdown(q_init)
        assert len(sb_res.value) == 3
        for status_item in sb_res.value:
            assert status_item.total_order_count == 0
            assert status_item.sales_amount == Decimal("0.00")
