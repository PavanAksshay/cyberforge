"""AstraRoll Aggregation Reducer.

State reduction, event validation, concurrency safeguards, and incremental
aggregate accumulation for AstraRoll V1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from astraroll.aggregation.contracts import (
    AggregateDelta,
    AggregationKey,
    OrderEvent,
    OrderProjectionState,
)
from astraroll.aggregation.contribution import compute_contribution_delta
from astraroll.aggregation.exceptions import (
    ImmutableFieldViolationError,
    InvalidEventError,
    OutOfOrderEventError,
    TenantMismatchError,
)


class ReductionResult(BaseModel):
    """Result of reducing an order event against existing projection state."""

    model_config = ConfigDict(frozen=True)

    applied: bool
    is_duplicate: bool = False
    is_out_of_order: bool = False
    reason: str | None = None
    event_id: str
    tenant_id: str
    order_id: str
    order_version: int
    deltas: dict[AggregationKey, AggregateDelta] = {}
    previous_state: OrderProjectionState | None = None
    new_state: OrderProjectionState | None = None

    def non_zero_deltas(self) -> dict[AggregationKey, AggregateDelta]:
        """Return only the deltas where either order_count != 0 or sales_cents != 0."""
        return {k: v for k, v in self.deltas.items() if not v.is_zero()}


def reduce_event(
    event: OrderEvent,
    current_state: OrderProjectionState | None = None,
    strict_ordering: bool = False,
) -> ReductionResult:
    """Pure reduction function that transitions order state and calculates incremental deltas.

    Safeguards:
    - Verifies event integrity and non-empty identity fields.
    - Enforces tenant isolation.
    - Idempotently ignores duplicate events by event_id or version.
    - Rejects or ignores out-of-order (stale) versions.
    - Handles missing previous projection state (first-time projection).
    - Preserves immutable created_at attribution.
    - Never mutates input snapshot or event.
    """
    if event is None or not isinstance(event, OrderEvent):
        raise InvalidEventError(
            f"Invalid event input: expected OrderEvent, got {type(event).__name__}"
        )

    # Guard against multi-tenant mixing and order_id mismatch
    if current_state is not None:
        if event.tenant_id != current_state.tenant_id:
            raise TenantMismatchError(
                f"Tenant mismatch: event tenant '{event.tenant_id}' does not match "
                f"projection tenant '{current_state.tenant_id}'"
            )
        if event.order_id != current_state.order_id:
            raise ImmutableFieldViolationError(
                f"Order ID mismatch: event order_id '{event.order_id}' does not match "
                f"projection order_id '{current_state.order_id}'"
            )

        # Duplicate event check by event_id
        if event.event_id == current_state.last_event_id:
            return ReductionResult(
                applied=False,
                is_duplicate=True,
                is_out_of_order=False,
                reason=f"Event {event.event_id} has already been applied",
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                order_id=event.order_id,
                order_version=event.order_version,
                deltas={},
                previous_state=current_state,
                new_state=current_state,
            )

        # Duplicate version check
        if event.order_version == current_state.current_version:
            return ReductionResult(
                applied=False,
                is_duplicate=True,
                is_out_of_order=False,
                reason=f"Order version {event.order_version} has already been applied",
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                order_id=event.order_id,
                order_version=event.order_version,
                deltas={},
                previous_state=current_state,
                new_state=current_state,
            )

        # Out-of-order (stale) version check: older version cannot overwrite newer version
        if event.order_version < current_state.current_version:
            if strict_ordering:
                raise OutOfOrderEventError(
                    f"Out of order event: version {event.order_version} is older than "
                    f"current version {current_state.current_version}"
                )
            return ReductionResult(
                applied=False,
                is_duplicate=False,
                is_out_of_order=True,
                reason=(
                    f"Stale event ignored: version {event.order_version} < "
                    f"current version {current_state.current_version}"
                ),
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                order_id=event.order_id,
                order_version=event.order_version,
                deltas={},
                previous_state=current_state,
                new_state=current_state,
            )

    # Compute incremental contribution delta between old state and new state
    old_snapshot = current_state.snapshot if current_state is not None else None
    deltas = compute_contribution_delta(
        old_snapshot=old_snapshot,
        new_snapshot=event.snapshot,
        tenant_id=event.tenant_id,
    )

    new_state = OrderProjectionState(
        tenant_id=event.tenant_id,
        order_id=event.order_id,
        last_applied_version=event.order_version,
        last_event_id=event.event_id,
        canonical_snapshot=event.snapshot,
        updated_at=event.occurred_at,
    )

    return ReductionResult(
        applied=True,
        is_duplicate=False,
        is_out_of_order=False,
        event_id=event.event_id,
        tenant_id=event.tenant_id,
        order_id=event.order_id,
        order_version=event.order_version,
        deltas=deltas,
        previous_state=current_state,
        new_state=new_state,
    )


class AggregationReducer:
    """Stateful aggregation reducer and aggregate accumulator.

    Manages projection states, event deduplication, and incremental aggregate accumulation.
    Ideal for worker event loop and test verification.
    """

    def __init__(self, strict_ordering: bool = False) -> None:
        self.strict_ordering = strict_ordering
        # (tenant_id, order_id) -> OrderProjectionState
        self._projections: dict[tuple[str, str], OrderProjectionState] = {}
        # Seen event IDs for global idempotency per worker
        self._seen_event_ids: set[str] = set()
        # AggregationKey -> AggregateDelta (accumulated values)
        self._aggregates: dict[AggregationKey, AggregateDelta] = {}

    def process_event(self, event: OrderEvent) -> ReductionResult:
        """Process an event idempotently and update accumulated aggregates."""
        if event is None or not isinstance(event, OrderEvent):
            raise InvalidEventError(
                f"Invalid event: expected OrderEvent, got {type(event).__name__}"
            )

        if event.event_id in self._seen_event_ids:
            return ReductionResult(
                applied=False,
                is_duplicate=True,
                is_out_of_order=False,
                reason=f"Event {event.event_id} already seen in deduplication cache",
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                order_id=event.order_id,
                order_version=event.order_version,
                deltas={},
                previous_state=self._projections.get((event.tenant_id, event.order_id)),
                new_state=self._projections.get((event.tenant_id, event.order_id)),
            )

        current_projection = self._projections.get((event.tenant_id, event.order_id))
        result = reduce_event(
            event=event,
            current_state=current_projection,
            strict_ordering=self.strict_ordering,
        )

        if result.applied and result.new_state is not None:
            # Update projection state
            self._projections[(event.tenant_id, event.order_id)] = result.new_state
            self._seen_event_ids.add(event.event_id)

            # Apply deltas to cumulative aggregates
            for key, delta in result.deltas.items():
                current_agg = self._aggregates.get(
                    key, AggregateDelta(order_count=0, sales_cents=0)
                )
                self._aggregates[key] = current_agg + delta

        return result

    def get_projection(self, tenant_id: str, order_id: str) -> OrderProjectionState | None:
        """Retrieve current order projection state for a tenant."""
        return self._projections.get((tenant_id, order_id))

    def get_aggregate(self, key: AggregationKey) -> AggregateDelta:
        """Retrieve current accumulated aggregate for a specific key."""
        return self._aggregates.get(key, AggregateDelta(order_count=0, sales_cents=0))

    def get_aggregates_for_tenant(self, tenant_id: str) -> dict[AggregationKey, AggregateDelta]:
        """Retrieve all non-zero aggregates for a given tenant."""
        return {
            k: v
            for k, v in self._aggregates.items()
            if k.tenant_id == tenant_id and not v.is_zero()
        }

    def clear(self) -> None:
        """Clear all in-memory projections and aggregates."""
        self._projections.clear()
        self._seen_event_ids.clear()
        self._aggregates.clear()
