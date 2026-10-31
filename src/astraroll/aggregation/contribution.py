"""AstraRoll Contribution Engine.

Pure functions that calculate an order's positive, negative, and net incremental
contributions to hourly and daily summary, status, and product aggregates.

Frozen Business Semantics:
- Every non-deleted order contributes exactly 1 to overall order count, including cancelled orders.
- Recognized sales:
  - placed: ZERO sales (0 cents).
  - shipped: FULL order amount (amount_cents).
  - cancelled: ZERO sales (0 cents).
- Status aggregates count orders by their CURRENT status, grouped into the bucket
  of their ORIGINAL immutable created_at.
- Product aggregates count orders by their CURRENT product, grouped into the bucket
  of their ORIGINAL immutable created_at.
- When status/product changes:
  - Reverse the previously projected contribution.
  - Apply the new canonical contribution.
  - Overall order count is preserved.
  - Recognized sales change only when sales contribution changes.
- Multi-currency: Every aggregate key is explicitly partitioned by currency and tenant_id.
- Financial arithmetic uses exact integer cents (minor currency units) and exact Decimals.
"""

from __future__ import annotations

from datetime import UTC, datetime

from astraroll.aggregation.contracts import (
    AggregateDelta,
    AggregateType,
    AggregationKey,
    AggregationWindow,
    OrderSnapshot,
    OrderStatus,
)
from astraroll.aggregation.exceptions import ImmutableFieldViolationError, InvalidSnapshotError


def normalize_to_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware and normalized to UTC."""
    if not isinstance(dt, datetime):
        raise InvalidSnapshotError(f"Expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_hourly_bucket_start(dt: datetime) -> datetime:
    """Floor UTC datetime to the start of its hour (minute=0, second=0, microsecond=0)."""
    utc_dt = normalize_to_utc(dt)
    return utc_dt.replace(minute=0, second=0, microsecond=0)


def get_daily_bucket_start(dt: datetime) -> datetime:
    """Floor UTC datetime to the start of its day (hour=0, minute=0, second=0, microsecond=0)."""
    utc_dt = normalize_to_utc(dt)
    return utc_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def calculate_sales_contribution(snapshot: OrderSnapshot) -> int:
    """Calculate sales cents contributed by the snapshot based on shared analytics semantics.

    - shipped orders contribute their full amount_cents.
    - placed and cancelled orders contribute zero sales.
    """
    if snapshot.status == OrderStatus.SHIPPED:
        return snapshot.amount_cents
    return 0


def calculate_hourly_summary_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the hourly summary aggregate.

    Attributed to the immutable created_at hour bucket and partitioned by currency.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
        currency=snapshot.currency,
        window=AggregationWindow.HOURLY,
        aggregate_type=AggregateType.SUMMARY,
        bucket_start=get_hourly_bucket_start(snapshot.created_at),
        dimension_value=None,
    )
    delta = AggregateDelta(
        order_count=1,
        sales_cents=calculate_sales_contribution(snapshot),
    )
    return key, delta


def calculate_daily_summary_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the daily summary aggregate.

    Attributed to the immutable created_at day bucket and partitioned by currency.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
        currency=snapshot.currency,
        window=AggregationWindow.DAILY,
        aggregate_type=AggregateType.SUMMARY,
        bucket_start=get_daily_bucket_start(snapshot.created_at),
        dimension_value=None,
    )
    delta = AggregateDelta(
        order_count=1,
        sales_cents=calculate_sales_contribution(snapshot),
    )
    return key, delta


def calculate_hourly_status_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the hourly status aggregate.

    Attributed to the immutable created_at hour bucket, currency, and snapshot.status.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
        currency=snapshot.currency,
        window=AggregationWindow.HOURLY,
        aggregate_type=AggregateType.STATUS,
        bucket_start=get_hourly_bucket_start(snapshot.created_at),
        dimension_value=snapshot.status.value,
    )
    delta = AggregateDelta(
        order_count=1,
        sales_cents=calculate_sales_contribution(snapshot),
    )
    return key, delta


def calculate_daily_status_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the daily status aggregate.

    Attributed to the immutable created_at day bucket, currency, and snapshot.status.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
        currency=snapshot.currency,
        window=AggregationWindow.DAILY,
        aggregate_type=AggregateType.STATUS,
        bucket_start=get_daily_bucket_start(snapshot.created_at),
        dimension_value=snapshot.status.value,
    )
    delta = AggregateDelta(
        order_count=1,
        sales_cents=calculate_sales_contribution(snapshot),
    )
    return key, delta


def calculate_hourly_product_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the hourly product aggregate.

    Attributed to the immutable created_at hour bucket, currency, and snapshot.product_id.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
        currency=snapshot.currency,
        window=AggregationWindow.HOURLY,
        aggregate_type=AggregateType.PRODUCT,
        bucket_start=get_hourly_bucket_start(snapshot.created_at),
        dimension_value=snapshot.product_id,
    )
    delta = AggregateDelta(
        order_count=1,
        sales_cents=calculate_sales_contribution(snapshot),
    )
    return key, delta


def calculate_daily_product_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the daily product aggregate.

    Attributed to the immutable created_at day bucket, currency, and snapshot.product_id.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
        currency=snapshot.currency,
        window=AggregationWindow.DAILY,
        aggregate_type=AggregateType.PRODUCT,
        bucket_start=get_daily_bucket_start(snapshot.created_at),
        dimension_value=snapshot.product_id,
    )
    delta = AggregateDelta(
        order_count=1,
        sales_cents=calculate_sales_contribution(snapshot),
    )
    return key, delta


def calculate_order_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> dict[AggregationKey, AggregateDelta]:
    """Calculate all positive contributions across the 6 aggregate dimensions for an order snapshot.

    1. Hourly summary
    2. Daily summary
    3. Hourly status
    4. Daily status
    5. Hourly product
    6. Daily product
    """
    if not tenant_id or not str(tenant_id).strip():
        raise InvalidSnapshotError("tenant_id must be a non-empty string or UUID")

    t_id = str(tenant_id).strip()

    h_summary_k, h_summary_v = calculate_hourly_summary_contribution(snapshot, t_id)
    d_summary_k, d_summary_v = calculate_daily_summary_contribution(snapshot, t_id)
    h_status_k, h_status_v = calculate_hourly_status_contribution(snapshot, t_id)
    d_status_k, d_status_v = calculate_daily_status_contribution(snapshot, t_id)
    h_prod_k, h_prod_v = calculate_hourly_product_contribution(snapshot, t_id)
    d_prod_k, d_prod_v = calculate_daily_product_contribution(snapshot, t_id)

    return {
        h_summary_k: h_summary_v,
        d_summary_k: d_summary_v,
        h_status_k: h_status_v,
        d_status_k: d_status_v,
        h_prod_k: h_prod_v,
        d_prod_k: d_prod_v,
    }


def calculate_negative_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> dict[AggregationKey, AggregateDelta]:
    """Calculate exact negative contribution (negation) of an order snapshot.

    Reverses order counts and sales amounts previously contributed by this snapshot.
    """
    positive = calculate_order_contribution(snapshot, tenant_id)
    return {k: -v for k, v in positive.items()}


def compute_contribution_delta(
    old_snapshot: OrderSnapshot | None,
    new_snapshot: OrderSnapshot | None,
    tenant_id: str,
) -> dict[AggregationKey, AggregateDelta]:
    """Compute the net incremental delta between an old order state and new order state.

    - First-time order (old_snapshot is None): returns positive contribution of new_snapshot.
    - Order deletion/removal (new_snapshot is None): returns negative contribution of old_snapshot.
    - Order update: calculates exact negative contribution of old_snapshot and positive
      contribution of new_snapshot, returning the net delta per aggregation key.
    - Preserves immutable created_at constraint: all updates are attributed to the original
      created_at timestamp.
    - Preserves immutable currency constraint: old and new snapshots must match currency.
    """
    if not tenant_id or not str(tenant_id).strip():
        raise InvalidSnapshotError("tenant_id must be a non-empty string or UUID")

    t_id = str(tenant_id).strip()

    if old_snapshot is None and new_snapshot is None:
        return {}

    if old_snapshot is None and new_snapshot is not None:
        return calculate_order_contribution(new_snapshot, t_id)

    if old_snapshot is not None and new_snapshot is None:
        return calculate_negative_contribution(old_snapshot, t_id)

    # Both old_snapshot and new_snapshot are present
    assert old_snapshot is not None and new_snapshot is not None

    # Immutable created_at validation
    old_utc_created = normalize_to_utc(old_snapshot.created_at)
    new_utc_created = normalize_to_utc(new_snapshot.created_at)
    if old_utc_created != new_utc_created:
        raise ImmutableFieldViolationError(
            f"Order created_at is immutable. Old: {old_utc_created.isoformat()}, "
            f"New: {new_utc_created.isoformat()}"
        )

    # Immutable currency validation
    if old_snapshot.currency != new_snapshot.currency:
        raise ImmutableFieldViolationError(
            f"Order currency is immutable. Old: '{old_snapshot.currency}', "
            f"New: '{new_snapshot.currency}'"
        )

    neg_contrib = calculate_negative_contribution(old_snapshot, t_id)
    pos_contrib = calculate_order_contribution(new_snapshot, t_id)

    merged: dict[AggregationKey, AggregateDelta] = dict(neg_contrib)
    for key, pos_delta in pos_contrib.items():
        if key in merged:
            merged[key] = merged[key] + pos_delta
        else:
            merged[key] = pos_delta

    return merged
