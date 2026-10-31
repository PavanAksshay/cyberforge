"""AstraRoll Contribution Engine.

Pure functions that calculate an order's positive, negative, and net incremental
contributions to hourly and daily summary, status, and product aggregates.

Business Semantics:
- Total order count includes every extant order.
- Cancelled orders remain included in order counts.
- Cancelled orders contribute zero to sales.
- Placed and shipped orders contribute their amount_cents to sales.
- Every order contributes to exactly one current-status bucket.
- Product and status changes must be reversible.
- All updates are attributed to the original immutable created_at timestamp.
- Financial arithmetic uses exact integer cents (minor currency units).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from app.aggregation.contracts import (
    AggregateDelta,
    AggregateType,
    AggregationKey,
    AggregationWindow,
    OrderSnapshot,
    OrderStatus,
)
from app.aggregation.exceptions import ImmutableFieldViolationError, InvalidSnapshotError


def normalize_to_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware and normalized to UTC."""
    if not isinstance(dt, datetime):
        raise InvalidSnapshotError(f"Expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_hourly_bucket_start(dt: datetime) -> datetime:
    """Floor UTC datetime to the start of its hour (minute=0, second=0, microsecond=0)."""
    utc_dt = normalize_to_utc(dt)
    return utc_dt.replace(minute=0, second=0, microsecond=0)


def get_daily_bucket_start(dt: datetime) -> datetime:
    """Floor UTC datetime to the start of its day (hour=0, minute=0, second=0, microsecond=0)."""
    utc_dt = normalize_to_utc(dt)
    return utc_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def calculate_sales_contribution(snapshot: OrderSnapshot) -> int:
    """Calculate sales cents contributed by the snapshot based on status semantics.

    - Placed and Shipped orders contribute their amount_cents.
    - Cancelled orders contribute 0 cents.
    """
    if snapshot.status in (OrderStatus.PLACED, OrderStatus.SHIPPED):
        return snapshot.amount_cents
    if snapshot.status == OrderStatus.CANCELLED:
        return 0
    return 0


def calculate_hourly_summary_contribution(
    snapshot: OrderSnapshot, tenant_id: str
) -> Tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the hourly summary aggregate.

    Attributed to the immutable created_at hour bucket.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
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
) -> Tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the daily summary aggregate.

    Attributed to the immutable created_at day bucket.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
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
) -> Tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the hourly status aggregate.

    Attributed to the immutable created_at hour bucket and snapshot.status.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
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
) -> Tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the daily status aggregate.

    Attributed to the immutable created_at day bucket and snapshot.status.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
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
) -> Tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the hourly product aggregate.

    Attributed to the immutable created_at hour bucket and snapshot.product_id.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
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
) -> Tuple[AggregationKey, AggregateDelta]:
    """Calculate an order's positive contribution to the daily product aggregate.

    Attributed to the immutable created_at day bucket and snapshot.product_id.
    """
    key = AggregationKey(
        tenant_id=tenant_id,
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
) -> Dict[AggregationKey, AggregateDelta]:
    """Calculate all positive contributions across the 6 aggregate dimensions for an order snapshot.

    1. Hourly summary
    2. Daily summary
    3. Hourly status
    4. Daily status
    5. Hourly product
    6. Daily product
    """
    if not tenant_id or not tenant_id.strip():
        raise InvalidSnapshotError("tenant_id must be a non-empty string")

    h_summary_k, h_summary_v = calculate_hourly_summary_contribution(snapshot, tenant_id)
    d_summary_k, d_summary_v = calculate_daily_summary_contribution(snapshot, tenant_id)
    h_status_k, h_status_v = calculate_hourly_status_contribution(snapshot, tenant_id)
    d_status_k, d_status_v = calculate_daily_status_contribution(snapshot, tenant_id)
    h_prod_k, h_prod_v = calculate_hourly_product_contribution(snapshot, tenant_id)
    d_prod_k, d_prod_v = calculate_daily_product_contribution(snapshot, tenant_id)

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
) -> Dict[AggregationKey, AggregateDelta]:
    """Calculate exact negative contribution (negation) of an order snapshot.

    Reverses order counts and sales amounts previously contributed by this snapshot.
    """
    positive = calculate_order_contribution(snapshot, tenant_id)
    return {k: -v for k, v in positive.items()}


def compute_contribution_delta(
    old_snapshot: Optional[OrderSnapshot],
    new_snapshot: Optional[OrderSnapshot],
    tenant_id: str,
) -> Dict[AggregationKey, AggregateDelta]:
    """Compute the net incremental delta between an old order state and new order state.

    - First-time order (old_snapshot is None): returns positive contribution of new_snapshot.
    - Order deletion/removal (new_snapshot is None): returns negative contribution of old_snapshot.
    - Order update: calculates exact negative contribution of old_snapshot and positive
      contribution of new_snapshot, returning the net delta per aggregation key.
    - Preserves immutable created_at constraint: all updates are attributed to the original
      created_at timestamp.
    """
    if not tenant_id or not tenant_id.strip():
        raise InvalidSnapshotError("tenant_id must be a non-empty string")

    if old_snapshot is None and new_snapshot is None:
        return {}

    if old_snapshot is None and new_snapshot is not None:
        return calculate_order_contribution(new_snapshot, tenant_id)

    if old_snapshot is not None and new_snapshot is None:
        return calculate_negative_contribution(old_snapshot, tenant_id)

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

    neg_contrib = calculate_negative_contribution(old_snapshot, tenant_id)
    pos_contrib = calculate_order_contribution(new_snapshot, tenant_id)

    merged: Dict[AggregationKey, AggregateDelta] = dict(neg_contrib)
    for key, pos_delta in pos_contrib.items():
        if key in merged:
            merged[key] = merged[key] + pos_delta
        else:
            merged[key] = pos_delta

    return merged

