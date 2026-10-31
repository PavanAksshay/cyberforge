"""Tests for the AstraRoll Contribution Engine.

Verifies:
- Positive contribution calculation across all 6 aggregate dimensions.
- Negative contribution (reversibility).
- Net incremental deltas for first-time creation, amount updates, product changes,
  status changes, and cancellations.
- Compliance with the specification example:
  (Old: product=A, status=placed, amount=5000 -> New: product=B, status=cancelled, amount=7000)
- UTC hour and day boundary calculations.
- Strict financial integer arithmetic without float rounding errors.
- Immutable created_at attribution.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from astraroll.aggregation.contracts import (
    AggregateType,
    AggregationKey,
    AggregationWindow,
    OrderSnapshot,
    OrderStatus,
)
from astraroll.aggregation.contribution import (
    calculate_daily_product_contribution,
    calculate_daily_status_contribution,
    calculate_daily_summary_contribution,
    calculate_hourly_product_contribution,
    calculate_hourly_status_contribution,
    calculate_hourly_summary_contribution,
    calculate_negative_contribution,
    calculate_order_contribution,
    calculate_sales_contribution,
    compute_contribution_delta,
    get_daily_bucket_start,
    get_hourly_bucket_start,
    normalize_to_utc,
)
from astraroll.aggregation.exceptions import (
    ImmutableFieldViolationError,
    InvalidSnapshotError,
)

TENANT_ID = "tenant-alpha"
ORDER_ID = "ord-1001"
CREATED_AT = datetime(2026, 10, 31, 14, 25, 30, tzinfo=UTC)
UPDATED_AT = datetime(2026, 10, 31, 14, 25, 30, tzinfo=UTC)


@pytest.fixture
def placed_snapshot_a() -> OrderSnapshot:
    """Fixture: Order in placed status with product PROD-A and 5000 cents ($50.00)."""
    return OrderSnapshot(
        product_id="PROD-A",
        amount_cents=5000,
        status=OrderStatus.PLACED,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


@pytest.fixture
def shipped_snapshot_a() -> OrderSnapshot:
    """Fixture: Order updated to shipped status."""
    return OrderSnapshot(
        product_id="PROD-A",
        amount_cents=5000,
        status=OrderStatus.SHIPPED,
        created_at=CREATED_AT,
        updated_at=CREATED_AT + timedelta(hours=1),
    )


@pytest.fixture
def cancelled_snapshot_b() -> OrderSnapshot:
    """Fixture: Order updated to cancelled status with product PROD-B and 7000 cents."""
    return OrderSnapshot(
        product_id="PROD-B",
        amount_cents=7000,
        status=OrderStatus.CANCELLED,
        created_at=CREATED_AT,
        updated_at=CREATED_AT + timedelta(hours=2),
    )


class TestSalesContribution:
    """Tests for sales contribution rules."""

    def test_placed_order_contributes_amount(self, placed_snapshot_a: OrderSnapshot):
        # Under reconciled contract: placed orders contribute sales
        assert calculate_sales_contribution(placed_snapshot_a) == 5000

    def test_shipped_order_contributes_amount(self, shipped_snapshot_a: OrderSnapshot):
        assert calculate_sales_contribution(shipped_snapshot_a) == 5000

    def test_cancelled_order_contributes_zero_sales(self, cancelled_snapshot_b: OrderSnapshot):
        """Cancelled orders must contribute 0 to sales."""
        assert calculate_sales_contribution(cancelled_snapshot_b) == 0


class TestIndividualBucketContributions:
    """Tests for individual positive aggregate dimension contributions."""

    def test_hourly_summary_contribution(self, placed_snapshot_a: OrderSnapshot):
        key, delta = calculate_hourly_summary_contribution(placed_snapshot_a, TENANT_ID)
        assert key.tenant_id == TENANT_ID
        assert key.window == AggregationWindow.HOURLY
        assert key.aggregate_type == AggregateType.SUMMARY
        assert key.bucket_start == datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC)
        assert key.dimension_value is None
        assert delta.order_count == 1
        assert delta.sales_cents == 5000

    def test_daily_summary_contribution(self, placed_snapshot_a: OrderSnapshot):
        key, delta = calculate_daily_summary_contribution(placed_snapshot_a, TENANT_ID)
        assert key.tenant_id == TENANT_ID
        assert key.window == AggregationWindow.DAILY
        assert key.aggregate_type == AggregateType.SUMMARY
        assert key.bucket_start == datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC)
        assert key.dimension_value is None
        assert delta.order_count == 1
        assert delta.sales_cents == 5000

    def test_hourly_status_contribution_placed(self, placed_snapshot_a: OrderSnapshot):
        key, delta = calculate_hourly_status_contribution(placed_snapshot_a, TENANT_ID)
        assert key.window == AggregationWindow.HOURLY
        assert key.aggregate_type == AggregateType.STATUS
        assert key.bucket_start == datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC)
        assert key.dimension_value == "placed"
        assert delta.order_count == 1
        assert delta.sales_cents == 5000

    def test_daily_status_contribution_shipped(self, shipped_snapshot_a: OrderSnapshot):
        key, delta = calculate_daily_status_contribution(shipped_snapshot_a, TENANT_ID)
        assert key.window == AggregationWindow.DAILY
        assert key.aggregate_type == AggregateType.STATUS
        assert key.bucket_start == datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC)
        assert key.dimension_value == "shipped"
        assert delta.order_count == 1
        assert delta.sales_cents == 5000

    def test_hourly_status_contribution_cancelled(self, cancelled_snapshot_b: OrderSnapshot):
        key, delta = calculate_hourly_status_contribution(cancelled_snapshot_b, TENANT_ID)
        assert key.dimension_value == "cancelled"
        assert delta.order_count == 1
        assert delta.sales_cents == 0

    def test_hourly_product_contribution(self, placed_snapshot_a: OrderSnapshot):
        key, delta = calculate_hourly_product_contribution(placed_snapshot_a, TENANT_ID)
        assert key.window == AggregationWindow.HOURLY
        assert key.aggregate_type == AggregateType.PRODUCT
        assert key.bucket_start == datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC)
        assert key.dimension_value == "PROD-A"
        assert delta.order_count == 1
        assert delta.sales_cents == 5000

    def test_daily_product_contribution_cancelled(self, cancelled_snapshot_b: OrderSnapshot):
        key, delta = calculate_daily_product_contribution(cancelled_snapshot_b, TENANT_ID)
        assert key.window == AggregationWindow.DAILY
        assert key.aggregate_type == AggregateType.PRODUCT
        assert key.bucket_start == datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC)
        assert key.dimension_value == "PROD-B"
        assert delta.order_count == 1
        assert delta.sales_cents == 0


class TestNegativeContribution:
    """Tests for exact negative contribution (negation / reversibility)."""

    def test_negative_contribution_exact_inversion(self, placed_snapshot_a: OrderSnapshot):
        positive = calculate_order_contribution(placed_snapshot_a, TENANT_ID)
        negative = calculate_negative_contribution(placed_snapshot_a, TENANT_ID)

        assert len(positive) == 6
        assert len(negative) == 6

        for key, pos_delta in positive.items():
            assert key in negative
            neg_delta = negative[key]
            assert neg_delta.order_count == -pos_delta.order_count
            assert neg_delta.sales_cents == -pos_delta.sales_cents

            # Summing positive and negative gives exact zero
            net = pos_delta + neg_delta
            assert net.is_zero()
            assert net.order_count == 0
            assert net.sales_cents == 0


class TestComputeContributionDelta:
    """Tests for incremental delta transitions."""

    def test_first_time_order_creation(self, placed_snapshot_a: OrderSnapshot):
        deltas = compute_contribution_delta(None, placed_snapshot_a, TENANT_ID)
        assert len(deltas) == 6

        # Placed orders contribute positive order_count=1 and sales_cents=5000
        for key, delta in deltas.items():
            assert delta.order_count == 1
            assert delta.sales_cents == 5000

    def test_updating_amount_reverses_old_amount(self, placed_snapshot_a: OrderSnapshot):
        """Updating amount while placed updates sales delta by net difference."""
        updated_snapshot = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=8000,
            status=OrderStatus.PLACED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(minutes=15),
        )

        deltas = compute_contribution_delta(placed_snapshot_a, updated_snapshot, TENANT_ID)

        # Net order_count delta must be 0 for all buckets; sales_cents delta must be +3000
        for key, delta in deltas.items():
            assert delta.order_count == 0
            assert delta.sales_cents == 3000

        # Shipped order updates
        shipped_v1 = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=5000,
            status=OrderStatus.SHIPPED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        )
        shipped_v2 = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=8000,
            status=OrderStatus.SHIPPED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(minutes=15),
        )
        deltas_shipped = compute_contribution_delta(shipped_v1, shipped_v2, TENANT_ID)
        for key, delta in deltas_shipped.items():
            assert delta.order_count == 0
            assert delta.sales_cents == 3000

    def test_changing_product_updates_both_product_buckets(self, placed_snapshot_a: OrderSnapshot):
        """Changing product PROD-A -> PROD-B moves order count from A to B."""
        updated_snapshot = OrderSnapshot(
            product_id="PROD-B",
            amount_cents=5000,
            status=OrderStatus.PLACED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(minutes=10),
        )

        deltas = compute_contribution_delta(placed_snapshot_a, updated_snapshot, TENANT_ID)

        # Summary and status aggregates should have net 0 change
        h_summary_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
        )
        assert deltas[h_summary_k].order_count == 0
        assert deltas[h_summary_k].sales_cents == 0

        # Hourly product PROD-A must lose 1 order and 5000 sales
        h_prod_a_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="PROD-A",
        )
        assert deltas[h_prod_a_k].order_count == -1
        assert deltas[h_prod_a_k].sales_cents == -5000

        # Hourly product PROD-B must gain 1 order and 5000 sales
        h_prod_b_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="PROD-B",
        )
        assert deltas[h_prod_b_k].order_count == 1
        assert deltas[h_prod_b_k].sales_cents == 5000

    def test_changing_status_updates_both_status_buckets(self, placed_snapshot_a: OrderSnapshot):
        """Changing status placed -> shipped moves order between status buckets."""
        updated_snapshot = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=5000,
            status=OrderStatus.SHIPPED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(minutes=30),
        )

        deltas = compute_contribution_delta(placed_snapshot_a, updated_snapshot, TENANT_ID)

        # Status 'placed' loses 1 count and 5000 sales
        h_status_placed_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="placed",
        )
        assert deltas[h_status_placed_k].order_count == -1
        assert deltas[h_status_placed_k].sales_cents == -5000

        # Status 'shipped' gains 1 count and 5000 sales
        h_status_shipped_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="shipped",
        )
        assert deltas[h_status_shipped_k].order_count == 1
        assert deltas[h_status_shipped_k].sales_cents == 5000

        # Summary receives 0 net sales (since placed and shipped both contribute full sales)
        h_summary_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
        )
        assert deltas[h_summary_k].order_count == 0
        assert deltas[h_summary_k].sales_cents == 0

    def test_cancellation_reverses_sales_without_removing_total_order_count(
        self, placed_snapshot_a: OrderSnapshot
    ):
        """Cancellation reverses sales to 0 but keeps the order included in extant order count."""
        cancelled_snapshot = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=5000,
            status=OrderStatus.CANCELLED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(hours=1),
        )

        # Placed -> Cancelled transition
        deltas = compute_contribution_delta(placed_snapshot_a, cancelled_snapshot, TENANT_ID)

        # Hourly summary: order_count delta is 0, sales delta is -5000 (placed 5000 -> cancelled 0)
        h_summary_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
        )
        assert deltas[h_summary_k].order_count == 0
        assert deltas[h_summary_k].sales_cents == -5000

        # Status 'placed' loses 1 count and 5000 sales
        h_placed_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="placed",
        )
        assert deltas[h_placed_k].order_count == -1
        assert deltas[h_placed_k].sales_cents == -5000

        # Status 'cancelled' gains 1 count and 0 sales
        h_cancelled_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="cancelled",
        )
        assert deltas[h_cancelled_k].order_count == 1
        assert deltas[h_cancelled_k].sales_cents == 0

        # Shipped -> Cancelled transition
        shipped_snapshot = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=5000,
            status=OrderStatus.SHIPPED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(minutes=30),
        )
        deltas_shipped = compute_contribution_delta(shipped_snapshot, cancelled_snapshot, TENANT_ID)
        assert deltas_shipped[h_summary_k].order_count == 0
        assert deltas_shipped[h_summary_k].sales_cents == -5000

    def test_specification_complex_transition_example(self):
        """Exact test for specification example:

        Old state: product=A, status=placed, amount=5000
        New state: product=B, status=cancelled, amount=7000
        """
        old_snapshot = OrderSnapshot(
            product_id="A",
            amount_cents=5000,
            status=OrderStatus.PLACED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        )
        new_snapshot = OrderSnapshot(
            product_id="B",
            amount_cents=7000,
            status=OrderStatus.CANCELLED,
            created_at=CREATED_AT,
            updated_at=CREATED_AT + timedelta(hours=3),
        )

        deltas = compute_contribution_delta(old_snapshot, new_snapshot, TENANT_ID)

        # 1. Hourly Summary: order_count delta = 0 (-1 + 1), sales delta = -5000 (-5000 + 0)
        h_summary_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
        )
        assert deltas[h_summary_k].order_count == 0
        assert deltas[h_summary_k].sales_cents == -5000

        # 2. Daily Summary: order_count delta = 0, sales delta = -5000
        d_summary_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.DAILY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC),
        )
        assert deltas[d_summary_k].order_count == 0
        assert deltas[d_summary_k].sales_cents == -5000

        # 3. Status 'placed': order_count = -1, sales = -5000
        h_placed_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="placed",
        )
        assert deltas[h_placed_k].order_count == -1
        assert deltas[h_placed_k].sales_cents == -5000

        # 4. Status 'cancelled': order_count = +1, sales = 0
        h_cancelled_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="cancelled",
        )
        assert deltas[h_cancelled_k].order_count == 1
        assert deltas[h_cancelled_k].sales_cents == 0

        # 5. Product 'A': order_count = -1, sales = -5000
        h_prod_a_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="A",
        )
        assert deltas[h_prod_a_k].order_count == -1
        assert deltas[h_prod_a_k].sales_cents == -5000

        # 6. Product 'B': order_count = +1, sales = 0 (cancelled order contributes 0 sales)
        h_prod_b_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC),
            dimension_value="B",
        )
        assert deltas[h_prod_b_k].order_count == 1
        assert deltas[h_prod_b_k].sales_cents == 0


class TestImmutableConstraints:
    """Tests ensuring created_at immutability."""

    def test_mutating_created_at_raises_violation(self, placed_snapshot_a: OrderSnapshot):
        mutated_snapshot = OrderSnapshot(
            product_id="PROD-A",
            amount_cents=5000,
            status=OrderStatus.PLACED,
            created_at=CREATED_AT + timedelta(days=1),  # Different created_at!
            updated_at=UPDATED_AT,
        )

        with pytest.raises(ImmutableFieldViolationError, match="Order created_at is immutable"):
            compute_contribution_delta(placed_snapshot_a, mutated_snapshot, TENANT_ID)


class TestUtcBoundaryCalculations:
    """Tests verifying exact UTC date and hour bucket boundaries."""

    def test_hour_boundary_end_of_hour(self):
        dt = datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC)
        hourly_bucket = get_hourly_bucket_start(dt)
        daily_bucket = get_daily_bucket_start(dt)

        assert hourly_bucket == datetime(2026, 10, 31, 23, 0, 0, tzinfo=UTC)
        assert daily_bucket == datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC)

    def test_hour_boundary_start_of_next_day(self):
        dt = datetime(2026, 11, 1, 0, 0, 0, 0, tzinfo=UTC)
        hourly_bucket = get_hourly_bucket_start(dt)
        daily_bucket = get_daily_bucket_start(dt)

        assert hourly_bucket == datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
        assert daily_bucket == datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)

    def test_timezone_normalization(self):
        # 14:30 in UTC+05:30 is 09:00 UTC
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime(2026, 10, 31, 14, 30, 0, tzinfo=ist)

        normalized = normalize_to_utc(dt_ist)
        assert normalized == datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)

        hourly = get_hourly_bucket_start(dt_ist)
        assert hourly == datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)

    def test_leap_year_boundary(self):
        """Verify Feb 29 on leap year calculates boundaries correctly."""
        dt = datetime(2028, 2, 29, 23, 45, 12, tzinfo=UTC)
        hourly = get_hourly_bucket_start(dt)
        daily = get_daily_bucket_start(dt)

        assert hourly == datetime(2028, 2, 29, 23, 0, 0, tzinfo=UTC)
        assert daily == datetime(2028, 2, 29, 0, 0, 0, tzinfo=UTC)

    def test_year_transition_boundary(self):
        """Verify Dec 31 23:59:59 transitioning into Jan 1 00:00:00."""
        dt_end = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
        assert get_hourly_bucket_start(dt_end) == datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC)
        assert get_daily_bucket_start(dt_end) == datetime(2026, 12, 31, 0, 0, 0, tzinfo=UTC)

        dt_start = datetime(2027, 1, 1, 0, 0, 0, 0, tzinfo=UTC)
        assert get_hourly_bucket_start(dt_start) == datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert get_daily_bucket_start(dt_start) == datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)


class TestIntegerArithmeticAndValidation:
    """Tests verifying exact integer currency and strict validation."""

    def test_floating_point_amount_rejected(self):
        with pytest.raises(InvalidSnapshotError, match="amount_cents cannot be a float"):
            OrderSnapshot(
                product_id="PROD-A",
                amount_cents=49.99,  # type: ignore[arg-type]
                status=OrderStatus.PLACED,
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )

    def test_boolean_amount_rejected(self):
        with pytest.raises(InvalidSnapshotError, match="amount_cents cannot be a boolean"):
            OrderSnapshot(
                product_id="PROD-A",
                amount_cents=True,  # type: ignore[arg-type]
                status=OrderStatus.PLACED,
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )

    def test_negative_amount_rejected(self):
        with pytest.raises(InvalidSnapshotError, match="amount_cents cannot be negative"):
            OrderSnapshot(
                product_id="PROD-A",
                amount_cents=-100,
                status=OrderStatus.PLACED,
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )

    def test_large_integer_precision(self):
        """Very large transactions (e.g. $10,000,000.00 = 1,000,000,000 cents) remain exact."""
        large_amount = 1_000_000_000
        snapshot = OrderSnapshot(
            product_id="PROD-LARGE",
            amount_cents=large_amount,
            status=OrderStatus.PLACED,
            created_at=CREATED_AT,
            updated_at=UPDATED_AT,
        )

        contrib = calculate_order_contribution(snapshot, TENANT_ID)
        neg_contrib = calculate_negative_contribution(snapshot, TENANT_ID)

        for k in contrib:
            net = contrib[k] + neg_contrib[k]
            assert net.sales_cents == 0
            assert net.order_count == 0
