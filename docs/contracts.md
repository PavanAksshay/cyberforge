# Shared producer contract (v1)

Developer A owns `config.py`, `db.py`, `models.py`, `schemas.py`, `orders.py`,
`api.py`, `relay.py`, `seed.py`, migration `0001_producer`, and infrastructure.
Developer B owns new consumer, rollup, correction, and cache modules and their
tables/migrations. No B modules are present or modified by this implementation.
Import `Base` from `astraroll.db` for new tables, and import B models in Alembic's
environment when adding migrations. Coordinate migration heads before merging.
Neither side should edit the other's migration history.

## Database

PostgreSQL is authoritative. Identifiers are UUIDs. All timestamps are
`TIMESTAMPTZ`; producers supply aware UTC datetimes and JSON serializes them with
`Z` or `+00:00`. Amounts use `NUMERIC(18,2)` and Python `Decimal` (nonnegative,
up to 16 integral digits); no floating point arithmetic. Currency is a required
uppercase three-letter code, immutable for an order. Separate all aggregates by
tenant **and currency**. Currency codes are syntactically validated.

| Table | Relevant columns / constraints |
| --- | --- |
| `tenants` | `id` UUID PK, `name` VARCHAR(120), `created_at` UTC |
| `orders` | `id` UUID PK, `tenant_id` FK, `amount`, `currency`, `status`, `version` BIGINT >= 1, `created_at`, `updated_at` |
| `outbox_events` | `id` UUID PK = event ID, tenant/order FKs, `order_version`, `event_type`, `schema_version`, `payload` JSONB, `occurred_at`, nullable `published_at`, nullable `stream_id` |

`(order_id, order_version)` is unique in the outbox. Creation starts at version 1.
Every successful update/cancel increments by exactly one, including accepted
updates that supply the current values. Mutations require `expected_version`
and use `SELECT FOR UPDATE`; stale writes return 409 without an event.
`created -> completed` and `created/completed -> cancelled` are supported.
Amounts may be corrected while created/completed; cancelled orders are terminal.
Currency and creation time cannot change. Cancellation is a retained tombstone,
never physical deletion. FK deletes are restricted.

Each mutation flushes both order state and the full event inside one PostgreSQL
transaction. Domain services never commit: callers own `Session.begin()`.
The API returns only after commit. No Redis connection is needed for API writes.
POST create is not request-idempotent: a new successful POST creates a new order.

## Redis event

Default stream: **`astraroll:orders:v1`**, configurable via `ORDER_STREAM`.
Each `XADD` uses an auto-generated Redis stream ID and exactly one field:
**`event`**, whose value is JSON with the following shape. Redis IDs are transport
positions, never order versions or idempotency keys.

```json
{
  "schema_version": 1,
  "event_id": "a8e9fb1e-1379-460d-ab62-d4263f111116",
  "event_type": "order.updated",
  "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
  "order_id": "1bf45b1f-663f-423c-825e-89b0abac464b",
  "order_version": 2,
  "occurred_at": "2026-10-31T08:00:00Z",
  "before": {
    "id": "1bf45b1f-663f-423c-825e-89b0abac464b",
    "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
    "amount": "10.00", "currency": "USD", "status": "created", "version": 1,
    "created_at": "2026-10-31T07:00:00Z", "updated_at": "2026-10-31T07:00:00Z"
  },
  "after": {
    "id": "1bf45b1f-663f-423c-825e-89b0abac464b",
    "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
    "amount": "12.50", "currency": "USD", "status": "completed", "version": 2,
    "created_at": "2026-10-31T07:00:00Z", "updated_at": "2026-10-31T08:00:00Z"
  }
}
```

Types are `order.created`, `order.updated`, `order.cancelled`. Creation has
`before: null`; all events have a full `after`. `OrderEvent` in `schemas.py` is
the machine-readable Pydantic schema. Amount strings may have zero, one or two
decimal places; parse with `Decimal`. `occurred_at` equals the new `updated_at`;
timestamps are informational, while versions order mutations.

Delivery is **at least once**, with eventual retries while PostgreSQL and Redis
remain available. Workers use `FOR UPDATE SKIP LOCKED` to claim a batch and hold
locks through XADD and DB commit. A Redis timeout or crash after XADD may leave
an event pending for redelivery under a different Redis stream ID. Event IDs
and payloads remain unchanged. Failed batches roll back all publication markers.
There is no automatic stream trimming or outbox deletion.

Multiple relay workers may deliver order versions out of sequence. B must make
consumption idempotent and recover pending deliveries (`XAUTOCLAIM` or equivalent).
Create new consumer groups at `0-0` to include events published before startup.
Persist processed event IDs atomically with projection updates, ACK only after
commit, and either buffer version gaps or reconcile a stored per-order projection
with the highest-version full `after` snapshot. Blindly applying `after - before`
while discarding lower versions can lose changes. For a highest-version snapshot
design, subtract the previously **projected** state, add the new state, and ignore
older/equal versions; do this atomically. B chooses aggregate semantics and time
buckets. All required states and creation/update timestamps are present.

Redis uses AOF with `appendfsync always` and persistent volumes; loss of Redis
storage still requires explicit recovery/replay from retained PostgreSQL outbox
rows. Never reset publication markers or remove volumes without coordinating B.

## API surface

| Method | Path | Body / result |
| --- | --- | --- |
| POST | `/tenants` | `{ "name": "Demo" }` -> 201 tenant |
| GET | `/tenants/{tenant_id}` | tenant or 404 |
| POST | `/tenants/{tenant_id}/orders` | `{ "amount": "12.50", "currency": "USD" }` -> 201 order |
| GET | `/tenants/{tenant_id}/orders` | list; `limit` 1..500, `offset` >= 0, ordered by creation then UUID |
| GET | `/tenants/{tenant_id}/orders/{id}` | order or 404 |
| PATCH | `/tenants/{tenant_id}/orders/{id}` | `expected_version` plus non-null `amount` and/or `status: "completed"` |
| POST | `/tenants/{tenant_id}/orders/{id}/cancel` | `{ "expected_version": 2 }` |
| GET | `/health/live`, `/health/ready` | process liveness / DB connectivity |

Unknown tenants/orders and cross-tenant order access return 404; invalid input
returns 422; stale versions or changes to cancelled orders return 409.
API routes enforce tenant scoping, but V1 has **no authentication or RLS**.
Tenant UUIDs are not credentials. Compose binds exposed ports to localhost.
Readiness checks PostgreSQL only; API writes can continue during Redis downtime
while events accumulate in the outbox.

Implementation references: [SQLAlchemy transactions](https://docs.sqlalchemy.org/en/20/orm/session_basics.html),
[row locking](https://docs.sqlalchemy.org/en/20/core/selectable.html#sqlalchemy.sql.expression.Select.with_for_update),
[Redis XADD](https://redis.io/docs/latest/commands/xadd/).
