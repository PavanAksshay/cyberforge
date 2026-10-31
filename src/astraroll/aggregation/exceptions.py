"""AstraRoll Aggregation Engine Exceptions.

Defines the exception hierarchy for aggregation processing, event validation,
projection state management, and concurrency/ordering safeguards.
"""


class AggregationError(Exception):
    """Base exception for all aggregation engine errors."""

    pass


class InvalidEventError(AggregationError):
    """Raised when an incoming event is malformed or invalid."""

    pass


class InvalidSnapshotError(AggregationError):
    """Raised when an order snapshot contains invalid data."""

    pass


class DuplicateEventError(AggregationError):
    """Raised when an event with an already processed event_id or version is encountered."""

    pass


class OutOfOrderEventError(AggregationError):
    """Raised when an older order version arrives after a newer version was processed."""

    pass


class MissingProjectionStateError(AggregationError):
    """Raised when an update operation cannot proceed because prior projection state is missing."""

    pass


class CorruptedProjectionError(AggregationError):
    """Raised when an existing projection state has corrupted or missing canonical snapshot.

    Never fallback to treating an existing projection as missing, as doing so
    would cause existing orders to be double-counted in aggregates.
    """

    pass


class ImmutableFieldViolationError(AggregationError):
    """Raised when an order update violates an immutable field constraint.

    Immutable fields include: created_at, order_id, tenant_id.
    All incremental aggregations must attribute updates to the original immutable
    created_at timestamp.
    """

    pass


class TenantMismatchError(AggregationError):
    """Raised when tenant_id in event or snapshot does not match the expected tenant."""

    pass
