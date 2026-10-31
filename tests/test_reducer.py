"""Tests for AstraRoll Aggregation Reducer.

Verifies:
- First-time order creation contributes exactly once.
- Updating amount reverses old amount.
- Changing product updates both product buckets.
- Changing status updates both status buckets.
- Cancellation reverses sales without removing total order count.
- Idempotency: applying the same event twice produces no additional contribution.
- Out-of-order protection: older order versions cannot overwrite newer versions.
- Missing previous projection state handling.
- Snapshot immutability: never mutates input snapshots.
- Tenant isolation and segregation.
- Error handling for invalid/empty inputs and immutable field violations.
- End-to-end multi-step order lifecycle.
"""

from datetime import datetime, timezone, timedelta
import pytest

from astraroll.aggregation.contracts import (
    AggregateDelta,
    AggregateType,
    AggregationKey,
    AggregationWindow,
    OrderEvent,
    OrderProjectionState,
    OrderSnapshot,
    OrderStatus,
)
from astraroll.aggregation.exceptions import (
    ImmutableFieldViolationError,
    InvalidEventError,
    InvalidSnapshotError,
    OutOfOrderEventError,
    TenantMismatchError,
)
from astraroll.aggregation.reducer import (
    AggregationReducer,
    ReductionResult,
    reduce_event,
)

TENANT_ID = "tenant-prod-1"
OTHER_TENANT_ID = "tenant-prod-2"
ORDER_ID = "ord-999"
CREATED_AT = datetime(2026, 10, 31, 10, 15, 0, tzinfo=timezone.utc)


def create_test_event(
    event_id: str,
    version: int,
    product_id: str = "PROD-100",
    amount_cents: int = 10000,
    status: OrderStatus = OrderStatus.PLACED,
    tenant_id: str = TENANT_ID,
    order_id: str = ORDER_ID,
    created_at: datetime = CREATED_AT,
    occurred_at: datetime = CREATED_AT,
) -> OrderEvent:
    """Helper to construct deterministic OrderEvent instances."""
    return OrderEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        order_id=order_id,
        order_version=version,
        event_type="order_updated" if version > 1 else "order_created",
        occurred_at=occurred_at,
        snapshot=OrderSnapshot(
            product_id=product_id,
            amount_cents=amount_cents,
            status=status,
            created_at=created_at,
            updated_at=occurred_at,
        ),
    )


class TestPureReduceEvent:
    """Tests for the pure reduce_event function."""

    def test_new_order_contributes_exactly_once(self):
        event = create_test_event("evt-1", 1, amount_cents=10000)
        result = reduce_event(event, current_state=None)

        assert result.applied is True
        assert result.is_duplicate is False
        assert result.is_out_of_order is False
        assert result.previous_state is None
        assert result.new_state is not None
        assert result.new_state.current_version == 1
        assert result.new_state.last_event_id == "evt-1"

        # 6 aggregate dimensions
        assert len(result.deltas) == 6
        for delta in result.deltas.values():
            assert delta.order_count == 1
            assert delta.sales_cents == 10000

    def test_updating_amount_reverses_old_amount(self):
        event_v1 = create_test_event("evt-1", 1, amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        # Version 2 with amount 15000 (+$50)
        event_v2 = create_test_event("evt-2", 2, amount_cents=15000)
        res_v2 = reduce_event(event_v2, current_state=res_v1.new_state)

        assert res_v2.applied is True
        assert res_v2.new_state.current_version == 2

        # Net order_count delta is 0 for all buckets; sales delta is +5000
        for delta in res_v2.deltas.values():
            assert delta.order_count == 0
            assert delta.sales_cents == 5000

    def test_changing_product_updates_both_product_buckets(self):
        event_v1 = create_test_event("evt-1", 1, product_id="PROD-A", amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        # Version 2: product changed to PROD-B
        event_v2 = create_test_event("evt-2", 2, product_id="PROD-B", amount_cents=10000)
        res_v2 = reduce_event(event_v2, current_state=res_v1.new_state)

        assert res_v2.applied is True

        h_prod_a = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="PROD-A",
        )
        h_prod_b = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="PROD-B",
        )

        assert res_v2.deltas[h_prod_a].order_count == -1
        assert res_v2.deltas[h_prod_a].sales_cents == -10000
        assert res_v2.deltas[h_prod_b].order_count == 1
        assert res_v2.deltas[h_prod_b].sales_cents == 10000

    def test_changing_status_updates_both_status_buckets(self):
        event_v1 = create_test_event("evt-1", 1, status=OrderStatus.PLACED, amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        event_v2 = create_test_event("evt-2", 2, status=OrderStatus.SHIPPED, amount_cents=10000)
        res_v2 = reduce_event(event_v2, current_state=res_v1.new_state)

        assert res_v2.applied is True

        h_placed = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="placed",
        )
        h_shipped = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="shipped",
        )

        assert res_v2.deltas[h_placed].order_count == -1
        assert res_v2.deltas[h_placed].sales_cents == -10000
        assert res_v2.deltas[h_shipped].order_count == 1
        assert res_v2.deltas[h_shipped].sales_cents == 10000

    def test_cancellation_reverses_sales_without_removing_total_order_count(self):
        event_v1 = create_test_event("evt-1", 1, status=OrderStatus.PLACED, amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        event_v2 = create_test_event("evt-2", 2, status=OrderStatus.CANCELLED, amount_cents=10000)
        res_v2 = reduce_event(event_v2, current_state=res_v1.new_state)

        assert res_v2.applied is True

        h_summary = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
        )
        # Summary order_count delta must be 0 (order is not removed), sales delta must be -10000
        assert res_v2.deltas[h_summary].order_count == 0
        assert res_v2.deltas[h_summary].sales_cents == -10000

    def test_duplicate_event_by_event_id_produces_no_contribution(self):
        event_v1 = create_test_event("evt-1", 1, amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        # Re-send same event
        res_dup = reduce_event(event_v1, current_state=res_v1.new_state)

        assert res_dup.applied is False
        assert res_dup.is_duplicate is True
        assert res_dup.deltas == {}
        assert res_dup.new_state == res_v1.new_state

    def test_duplicate_event_by_version_produces_no_contribution(self):
        event_v1 = create_test_event("evt-1", 1, amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        # Another event with same order_version=1 but different event_id
        event_v1_dup = create_test_event("evt-1-retry", 1, amount_cents=10000)
        res_dup = reduce_event(event_v1_dup, current_state=res_v1.new_state)

        assert res_dup.applied is False
        assert res_dup.is_duplicate is True
        assert res_dup.deltas == {}

    def test_older_order_version_cannot_overwrite_newer_version(self):
        event_v1 = create_test_event("evt-1", 1, amount_cents=10000)
        res_v1 = reduce_event(event_v1, current_state=None)

        event_v3 = create_test_event("evt-3", 3, amount_cents=12000)
        res_v3 = reduce_event(event_v3, current_state=res_v1.new_state)

        # Stale event arriving late with version 2
        event_v2_late = create_test_event("evt-2", 2, amount_cents=11000)
        res_stale = reduce_event(event_v2_late, current_state=res_v3.new_state)

        assert res_stale.applied is False
        assert res_stale.is_out_of_order is True
        assert res_stale.deltas == {}
        assert res_stale.new_state.current_version == 3

    def test_strict_ordering_raises_on_out_of_order(self):
        event_v2 = create_test_event("evt-2", 2, amount_cents=10000)
        state_v2 = OrderProjectionState(
            tenant_id=TENANT_ID,
            order_id=ORDER_ID,
            current_version=2,
            last_event_id="evt-2",
            snapshot=event_v2.snapshot,
            updated_at=CREATED_AT,
        )

        event_v1_late = create_test_event("evt-1", 1, amount_cents=10000)
        with pytest.raises(OutOfOrderEventError, match="Out of order event"):
            reduce_event(event_v1_late, current_state=state_v2, strict_ordering=True)

    def test_missing_previous_projection_state_handles_cleanly(self):
        """When an event arrives without existing projection state, it is projected as a new state."""
        event_v2 = create_test_event("evt-2", 2, amount_cents=8000)
        result = reduce_event(event_v2, current_state=None)

        assert result.applied is True
        assert result.new_state.current_version == 2
        assert len(result.deltas) == 6

    def test_tenant_mismatch_raises_error(self):
        event = create_test_event("evt-1", 1, tenant_id="tenant-A")
        existing_state = OrderProjectionState(
            tenant_id="tenant-B",
            order_id=ORDER_ID,
            current_version=1,
            last_event_id="evt-0",
            snapshot=event.snapshot,
            updated_at=CREATED_AT,
        )

        with pytest.raises(TenantMismatchError, match="Tenant mismatch"):
            reduce_event(event, current_state=existing_state)

    def test_order_id_mismatch_raises_error(self):
        event = create_test_event("evt-1", 1, order_id="ord-NEW")
        existing_state = OrderProjectionState(
            tenant_id=TENANT_ID,
            order_id="ord-OLD",
            current_version=1,
            last_event_id="evt-0",
            snapshot=event.snapshot,
            updated_at=CREATED_AT,
        )

        with pytest.raises(ImmutableFieldViolationError, match="Order ID mismatch"):
            reduce_event(event, current_state=existing_state)

    def test_never_mutates_original_input_snapshot(self):
        event = create_test_event("evt-1", 1, amount_cents=10000)
        orig_amount = event.snapshot.amount_cents
        orig_status = event.snapshot.status

        reduce_event(event, current_state=None)

        assert event.snapshot.amount_cents == orig_amount
        assert event.snapshot.status == orig_status

        # Frozen pydantic model prevents direct mutation
        with pytest.raises(Exception):
            event.snapshot.amount_cents = 20000  # type: ignore[misc]


class TestAggregationReducerEngine:
    """Tests for the stateful AggregationReducer."""

    def test_full_lifecycle_and_cumulative_aggregates(self):
        reducer = AggregationReducer()

        # Step 1: Create Order 1 (v1: placed, $100, PROD-A)
        evt_1 = create_test_event("e1", 1, product_id="PROD-A", amount_cents=10000, status=OrderStatus.PLACED)
        res_1 = reducer.process_event(evt_1)
        assert res_1.applied is True

        h_summary_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
        )
        h_placed_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="placed",
        )
        h_prod_a_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="PROD-A",
        )

        assert reducer.get_aggregate(h_summary_k) == AggregateDelta(order_count=1, sales_cents=10000)
        assert reducer.get_aggregate(h_placed_k) == AggregateDelta(order_count=1, sales_cents=10000)
        assert reducer.get_aggregate(h_prod_a_k) == AggregateDelta(order_count=1, sales_cents=10000)

        # Step 2: Update Amount to $150 (v2: placed, $150, PROD-A)
        evt_2 = create_test_event("e2", 2, product_id="PROD-A", amount_cents=15000, status=OrderStatus.PLACED)
        res_2 = reducer.process_event(evt_2)
        assert res_2.applied is True
        assert reducer.get_aggregate(h_summary_k) == AggregateDelta(order_count=1, sales_cents=15000)
        assert reducer.get_aggregate(h_placed_k) == AggregateDelta(order_count=1, sales_cents=15000)
        assert reducer.get_aggregate(h_prod_a_k) == AggregateDelta(order_count=1, sales_cents=15000)

        # Step 3: Change Product to PROD-B (v3: placed, $150, PROD-B)
        evt_3 = create_test_event("e3", 3, product_id="PROD-B", amount_cents=15000, status=OrderStatus.PLACED)
        res_3 = reducer.process_event(evt_3)
        assert res_3.applied is True

        h_prod_b_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.PRODUCT,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="PROD-B",
        )
        # PROD-A is now 0 count, 0 sales
        assert reducer.get_aggregate(h_prod_a_k) == AggregateDelta(order_count=0, sales_cents=0)
        # PROD-B has 1 count, 15000 sales
        assert reducer.get_aggregate(h_prod_b_k) == AggregateDelta(order_count=1, sales_cents=15000)
        # Summary is unchanged
        assert reducer.get_aggregate(h_summary_k) == AggregateDelta(order_count=1, sales_cents=15000)

        # Step 4: Status change to SHIPPED (v4: shipped, $150, PROD-B)
        evt_4 = create_test_event("e4", 4, product_id="PROD-B", amount_cents=15000, status=OrderStatus.SHIPPED)
        res_4 = reducer.process_event(evt_4)
        assert res_4.applied is True

        h_shipped_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="shipped",
        )
        # 'placed' is now 0 count, 0 sales
        assert reducer.get_aggregate(h_placed_k) == AggregateDelta(order_count=0, sales_cents=0)
        # 'shipped' has 1 count, 15000 sales
        assert reducer.get_aggregate(h_shipped_k) == AggregateDelta(order_count=1, sales_cents=15000)

        # Step 5: Duplicate event delivery (e4 delivered again)
        res_4_dup = reducer.process_event(evt_4)
        assert res_4_dup.applied is False
        assert res_4_dup.is_duplicate is True
        # Cumulative aggregates MUST NOT change
        assert reducer.get_aggregate(h_summary_k) == AggregateDelta(order_count=1, sales_cents=15000)

        # Step 6: Out-of-order event delivery (e2 arriving late)
        res_stale = reducer.process_event(evt_2)
        assert res_stale.applied is False
        # Cumulative aggregates MUST NOT change
        assert reducer.get_aggregate(h_summary_k) == AggregateDelta(order_count=1, sales_cents=15000)

        # Step 7: Cancellation (v5: cancelled, $150, PROD-B)
        evt_5 = create_test_event("e5", 5, product_id="PROD-B", amount_cents=15000, status=OrderStatus.CANCELLED)
        res_5 = reducer.process_event(evt_5)
        assert res_5.applied is True

        h_cancelled_k = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.STATUS,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
            dimension_value="cancelled",
        )
        # Summary: order_count MUST REMAIN 1, sales MUST BE 0!
        assert reducer.get_aggregate(h_summary_k) == AggregateDelta(order_count=1, sales_cents=0)
        # Shipped is now 0 count, 0 sales
        assert reducer.get_aggregate(h_shipped_k) == AggregateDelta(order_count=0, sales_cents=0)
        # Cancelled is now 1 count, 0 sales
        assert reducer.get_aggregate(h_cancelled_k) == AggregateDelta(order_count=1, sales_cents=0)
        # Product B is 1 count, 0 sales
        assert reducer.get_aggregate(h_prod_b_k) == AggregateDelta(order_count=1, sales_cents=0)

    def test_multi_tenant_segregation(self):
        """Events for different tenants must maintain strictly isolated aggregates."""
        reducer = AggregationReducer()

        # Tenant 1 order
        evt_t1 = create_test_event("evt-t1", 1, tenant_id="tenant-1", amount_cents=5000)
        reducer.process_event(evt_t1)

        # Tenant 2 order
        evt_t2 = create_test_event("evt-t2", 1, tenant_id="tenant-2", amount_cents=8000)
        reducer.process_event(evt_t2)

        h_summary_t1 = AggregationKey(
            tenant_id="tenant-1",
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
        )
        h_summary_t2 = AggregationKey(
            tenant_id="tenant-2",
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
        )

        assert reducer.get_aggregate(h_summary_t1) == AggregateDelta(order_count=1, sales_cents=5000)
        assert reducer.get_aggregate(h_summary_t2) == AggregateDelta(order_count=1, sales_cents=8000)

        # Projections are segregated
        assert reducer.get_projection("tenant-1", ORDER_ID) is not None
        assert reducer.get_projection("tenant-2", ORDER_ID) is not None
        assert reducer.get_projection("tenant-3", ORDER_ID) is None

    def test_invalid_event_input(self):
        reducer = AggregationReducer()
        with pytest.raises(InvalidEventError):
            reducer.process_event(None)  # type: ignore[arg-type]

    def test_order_event_deserialization_with_order_alias(self):
        """Verify Redis Streams payload formatted with 'order' instead of 'snapshot' deserializes properly."""
        payload = {
            "event_id": "evt-json-1",
            "tenant_id": "tenant-xyz",
            "order_id": "ord-json-100",
            "order_version": 1,
            "event_type": "order_created",
            "occurred_at": "2026-10-31T10:15:00Z",
            "order": {
                "product_id": "PROD-WIDGET",
                "amount_cents": 2500,
                "status": "placed",
                "created_at": "2026-10-31T10:15:00Z",
                "updated_at": "2026-10-31T10:15:00Z",
            },
        }
        event = OrderEvent.model_validate(payload)
        assert event.event_id == "evt-json-1"
        assert event.snapshot.product_id == "PROD-WIDGET"
        assert event.snapshot.amount_cents == 2500
        assert event.snapshot.status == OrderStatus.PLACED

        reducer = AggregationReducer()
        res = reducer.process_event(event)
        assert res.applied is True

    def test_free_promotional_order_zero_amount(self):
        """Free order with 0 cents contributes 1 to order_count and 0 to sales."""
        reducer = AggregationReducer()
        evt = create_test_event("evt-free-1", 1, amount_cents=0, status=OrderStatus.PLACED)
        res = reducer.process_event(evt)
        assert res.applied is True

        h_summary = AggregationKey(
            tenant_id=TENANT_ID,
            window=AggregationWindow.HOURLY,
            aggregate_type=AggregateType.SUMMARY,
            bucket_start=datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert reducer.get_aggregate(h_summary) == AggregateDelta(order_count=1, sales_cents=0)
