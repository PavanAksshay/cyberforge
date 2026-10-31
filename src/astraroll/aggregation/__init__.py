"""AstraRoll Aggregation Package.

Developer B: Aggregation engine, event processing, incremental corrections.
"""

from astraroll.aggregation.contracts import (
    AggregateDelta,
    AggregateType,
    AggregationKey,
    AggregationWindow,
    OrderContribution,
    OrderEvent,
    OrderProjectionState,
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
    compute_contribution_delta,
    get_daily_bucket_start,
    get_hourly_bucket_start,
    normalize_to_utc,
)
from astraroll.aggregation.exceptions import (
    AggregationError,
    DuplicateEventError,
    ImmutableFieldViolationError,
    InvalidEventError,
    InvalidSnapshotError,
    MissingProjectionStateError,
    OutOfOrderEventError,
    TenantMismatchError,
)
from astraroll.aggregation.reducer import (
    AggregationReducer,
    ReductionResult,
    reduce_event,
)

__all__ = [
    "AggregateDelta",
    "AggregateType",
    "AggregationError",
    "AggregationKey",
    "AggregationReducer",
    "AggregationWindow",
    "DuplicateEventError",
    "ImmutableFieldViolationError",
    "InvalidEventError",
    "InvalidSnapshotError",
    "MissingProjectionStateError",
    "OrderContribution",
    "OrderEvent",
    "OrderProjectionState",
    "OrderSnapshot",
    "OrderStatus",
    "OutOfOrderEventError",
    "ReductionResult",
    "TenantMismatchError",
    "calculate_daily_product_contribution",
    "calculate_daily_status_contribution",
    "calculate_daily_summary_contribution",
    "calculate_hourly_product_contribution",
    "calculate_hourly_status_contribution",
    "calculate_hourly_summary_contribution",
    "calculate_negative_contribution",
    "calculate_order_contribution",
    "compute_contribution_delta",
    "get_daily_bucket_start",
    "get_hourly_bucket_start",
    "normalize_to_utc",
    "reduce_event",
]
