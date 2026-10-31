# AstraRoll V1 frozen producer contract â€” schema version 2

Developer A owns the producer models, schemas, domain services, API, outbox relay,
seeder, infrastructure and migrations `0001_producer` / `0002_producer_contract`.
Developer B owns consumers, rollups, projections, corrections and caches. This
phase adds no B modules. Import `Base` from `astraroll.db` for B tables and use
separate migrations; coordinate migration heads before integration. Never rewrite
committed migration history.

## Database and API invariants

PostgreSQL is authoritative. Identifiers are UUIDs. All timestamps are UTC-aware
`TIMESTAMPTZ`, serialized as ISO 8601 with `Z` or `+00:00`. Connections explicitly
use UTC. Amounts are nonnegative, finite Python `Decimal` / `NUMERIC(18,2)`, with
at most 16 integral digits and two fractional digits. Send decimal strings in
JSON. Currency is a required uppercase three-letter code, immutable after
creation; validation is syntactic. Separate all totals by tenant and currency,
and product totals by tenant, product and currency.

| Table | Frozen columns and constraints |
| --- | --- |
| `tenants` | UUID `id` PK, `name` VARCHAR(120), UTC `created_at` |
| `orders` | UUID `id` PK, UUID `tenant_id` FK, required UUID `product_id`, `amount` NUMERIC(18,2), `currency` VARCHAR(3), `status`, BIGINT `version` >= 1, UTC `created_at` / `updated_at` |
| `outbox_events` | UUID `id` PK = event ID, tenant/order FKs, `order_version`, `event_type`, `schema_version`, JSONB `payload`, UTC `occurred_at`, nullable UTC `published_at`, nullable `stream_id` |

`product_id` identifies a product in the caller's catalog. The caller must supply
it; there is no default, product API or product table/FK in this producer phase.
The seeder uses a documented synthetic catalog, never a historical backfill.
Foreign-key deletes are restricted. `(order_id, order_version)` is unique in the
outbox. Cancellation retains the order; the producer never physically deletes it.

Statuses are exactly **placed, shipped, cancelled**, enforced by PostgreSQL.
New orders start at `placed`, version 1. Only these transitions are supported:

| Starting status | Operation | Result |
| --- | --- | --- |
| placed | amount and/or product edit | placed |
| placed | PATCH status shipped | shipped |
| placed | PATCH status cancelled, or cancellation endpoint | cancelled |
| shipped or cancelled | any further valid mutation | HTTP 409, unchanged |

A PATCH can combine amount/product edits with either terminal transition; all
changes commit together, evaluated against the locked starting state. Currency,
tenant, ID, creation time and version cannot be assigned through PATCH. Explicit
nulls, empty PATCHes, invalid UUIDs and unsupported statuses are HTTP 422. Every
accepted mutation increments version exactly once, including accepted requests
that supply current amount/product values. `expected_version` is required; stale
versions return HTTP 409 before terminal-state validation. Queries and row locks
include tenant ID, and cross-tenant access returns HTTP 404.

Services run inside caller-owned `Session.begin()` transactions and never commit
independently. Each accepted mutation locks its order using `SELECT FOR UPDATE`,
then writes the order and one complete outbox event atomically. Errors leave
neither partial order changes nor an extra event. The API responds after commit.
Redis availability is not required for API writes. POST create is not request
idempotent: each successful POST creates a new UUID order.

## Redis transport and complete event

The frozen default stream is **`astraroll:orders:v1`**; the one Redis field is
**`event`**, containing the JSON document below. `ORDER_STREAM` can override a
stream for isolated environments, but both developers must use the same setting.
Do not change the field to `payload` or the stream to `orders:events`.

New events have **`schema_version: 2`**. This increment was explicitly approved
by the project owner for coordination with Developer B. The application's V1
name and the Redis stream suffix remain unchanged. `OrderEvent` is the strict
Pydantic v2 event parser and requires an explicit schema version. The standalone
fixture [order-event-v2.json](order-event-v2.json) is validated by tests and matches
this representative atomic product/amount edit plus shipment:

```json
{
  "schema_version": 2,
  "event_id": "a8e9fb1e-1379-460d-ab62-d4263f111116",
  "event_type": "order.updated",
  "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
  "order_id": "1bf45b1f-663f-423c-825e-89b0abac464b",
  "order_version": 2,
  "occurred_at": "2026-10-31T08:00:00Z",
  "before": {
    "id": "1bf45b1f-663f-423c-825e-89b0abac464b",
    "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
    "product_id": "47b53ec8-c26c-45d3-8503-76f809b55952",
    "amount": "10.00",
    "currency": "USD",
    "status": "placed",
    "version": 1,
    "created_at": "2026-10-31T07:00:00Z",
    "updated_at": "2026-10-31T07:00:00Z"
  },
  "after": {
    "id": "1bf45b1f-663f-423c-825e-89b0abac464b",
    "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
    "product_id": "1b7cc4a5-89a3-4b8b-9555-de706b025414",
    "amount": "12.50",
    "currency": "USD",
    "status": "shipped",
    "version": 2,
    "created_at": "2026-10-31T07:00:00Z",
    "updated_at": "2026-10-31T08:00:00Z"
  }
}
```

The envelope fields are exactly `schema_version`, `event_id`, `event_type`,
`tenant_id`, `order_id`, `order_version`, `occurred_at`, `before`, `after`.
Every snapshot contains all ten fields shown above, including `product_id`.
Creation uses `event_type: order.created`, `before: null`, and a placed `after`.
Amount/product edits and shipping use `order.updated`; either cancellation route
uses `order.cancelled`. Updates/cancellations include a complete `before` and
`after`. Amounts are decimal strings; parse them with `Decimal`, never binary
floating point. UUIDs are canonical hyphenated strings. `occurred_at` equals the
new `updated_at`; timestamps are informational, while versions order mutations.

Delivery is **at least once**. Relay workers use `FOR UPDATE SKIP LOCKED` and hold
row locks through XADD and PostgreSQL commit. A timeout, crash, or commit failure
after XADD may redeliver the identical event under a different Redis stream ID.
`event_id`, schema version and payload stay stable across retries. Failed batches
roll back publication markers. Redis stream IDs are transport positions, not
order versions or deduplication keys. No automatic trimming or outbox deletion
occurs. Redis uses AOF `appendfsync always`, persistent volumes and no eviction;
storage loss still requires coordinated replay from the retained outbox.

## Migration and authoritative backfill policy

`0001_producer` is preserved byte-for-byte. `0002_producer_contract` depends on
`0001_producer`, takes an exclusive order-table lock, and adds `product_id` as
nullable before checking existing data. Fresh/empty databases upgrade through
both revisions automatically. No product defaults are generated.

For existing data, the migration refuses to proceed if any product ID is missing,
any legacy `completed` status remains, or any other unsupported status is present.
The error reports counts and points here. A failed upgrade rolls back all of its
DDL and data changes, including its newly added column. Operator preparations
committed before the upgrade remain available for retry.

Policy: legacy `created` explicitly means `placed` and is renamed by this revision.
Legacy `cancelled` remains cancelled. **`completed` is never inferred as shipped**:
only an authoritative, per-order business decision can choose placed, shipped or
cancelled. The migration preserves order IDs, tenants, amounts, currency,
timestamps, versions and every existing outbox row/payload. No synthetic bridge
events are emitted. B must bootstrap existing state as described below.

Before upgrading a valuable database, stop producers/relay and consumers together,
back up through your normal procedure, and review the data with the project owner.
Do not delete tables or volumes. For a database blocked by missing products or
completed statuses, prepare these reviewed mapping sources (names are examples):

- `approved_order_product_mapping(order_id UUID, tenant_id UUID, product_id UUID)`:
  one authoritative non-null mapping per affected order; unique tenant/order key.
- `approved_completed_status_mapping(order_id UUID, tenant_id UUID, new_status TEXT)`:
  one explicitly reviewed status per legacy completed order, restricted to the
  three final statuses. No automatic blanket mapping to shipped.

Then adapt this operator-run SQL to the actual reviewed mapping tables. It is a
manual preparation template, **not a migration that runs automatically**:

```sql
BEGIN;
LOCK TABLE orders IN ACCESS EXCLUSIVE MODE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id UUID;
UPDATE orders AS o SET product_id = m.product_id
FROM approved_order_product_mapping AS m
WHERE o.id = m.order_id AND o.tenant_id = m.tenant_id AND o.product_id IS NULL;
-- Permit explicitly reviewed final statuses while the old schema is still active.
ALTER TABLE orders DROP CONSTRAINT ck_order_status;
ALTER TABLE orders ADD CONSTRAINT ck_order_status CHECK
  (status IN ('created', 'completed', 'cancelled', 'placed', 'shipped'));
UPDATE orders AS o SET status = m.new_status
FROM approved_completed_status_mapping AS m
WHERE o.id = m.order_id AND o.tenant_id = m.tenant_id AND o.status = 'completed'
  AND m.new_status IN ('placed', 'shipped', 'cancelled');
SELECT count(*) FILTER (WHERE product_id IS NULL) AS missing_products,
       count(*) FILTER (WHERE status = 'completed') AS unresolved_completed
FROM orders;
COMMIT;
```

Both counts must be zero before retrying `alembic upgrade head`. Validate mappings
before committing; retain the original records and decision audit. Do not invent
UUIDs for real historical orders. Omit the status preparation when no completed
rows need resolution. The migration tolerates a pre-added UUID column, validates
its type and absence of a product default, then enforces NOT NULL only after all rows pass its checks. Only use a
disposable-development reset if the owner explicitly confirms the data is
expendable; this implementation never resets data automatically.

The revision is intentionally **forward-only**: downgrade raises a clear error
before any changes. Dropping product IDs or translating shipped back into the
old completed meaning would lose information. Roll back application rollout by
holding traffic and using a reviewed forward fix, or restore a verified old
backup into a separate database. Do not run the old producer against revision 0002.

## Developer B compatibility and worker handoff

Historical schema-1 events can omit product_id and use created/completed statuses.
They remain immutable in PostgreSQL and may still be pending in the relay or
present in Redis. The relay forwards each stored payload/version unchanged.
Never relabel a schema-1 event as 2 or parse its old statuses as new meanings.

B must check the raw `schema_version` before using the snapshot parser. Version 2
uses this frozen contract. Version 1, missing versions, and unknown future
versions need a durable quarantine/manual-reconciliation path or an explicitly
implemented legacy reader. Do not silently discard/ACK them as if processed.
A legacy reader cannot infer a real product or shipping decision from old payloads.

For valuable pre-existing data, coordinate a paused, consistent PostgreSQL snapshot
of migrated orders and per-order versions plus a stream/outbox checkpoint. B
must initialize its projection from that baseline and resume after the checkpoint
without double counting. Merely skipping schema-1 events leaves historical orders
with no v2 mutation absent from projections. This phase does not implement B's
baseline loader or worker. Fresh empty databases need no historical backfill.

Workers must deduplicate `event_id` atomically with projection changes and ACK
only after commit. Claim abandoned pending deliveries (`XAUTOCLAIM` or equivalent).
Create new groups at `0-0` when replaying the complete retained history. Multiple
relay workers may deliver versions out of order. Either buffer gaps, or reconcile
a stored per-order projection against the highest-version full `after` snapshot:
subtract the previously projected state, add the new state, and ignore older/equal
versions in the same transaction. Blindly applying `after - before` while skipping
older versions can lose changes. The analytics query-facing contribution/bucket
requirements are now described in [analytics.md](analytics.md); B's agreement and
physical persistence contract must be reviewed before connecting a real reader.
All states, product IDs, currencies and creation/update timestamps are supplied.

## API surface

| Method | Path | Request/result |
| --- | --- | --- |
| POST | `/tenants` | `{ "name": "Demo" }` -> 201 tenant |
| GET | `/tenants/{tenant_id}` | tenant or 404 |
| POST | `/tenants/{tenant_id}/orders` | required product UUID, decimal amount string and currency -> 201 placed order |
| GET | `/tenants/{tenant_id}/orders` | list; limit 1..500, offset >= 0; creation time then UUID order |
| GET | `/tenants/{tenant_id}/orders/{id}` | order or 404 |
| PATCH | `/tenants/{tenant_id}/orders/{id}` | expected_version plus amount, product_id, and/or status shipped/cancelled |
| POST | `/tenants/{tenant_id}/orders/{id}/cancel` | expected_version; same terminal/version rules |
| GET | `/health/live`, `/health/ready` | process liveness / DB connectivity |

Tenant and order routes require expiring server-verified bearer keys and trusted
memberships. POST /tenants requires an admin. No PostgreSQL RLS is implemented.
Compose binds ports to localhost. Readiness checks PostgreSQL connectivity only;
writes can continue while Redis is unavailable and the outbox accumulates.

## Integrated analytics contract

The coordinated release uses the approved **created_at_shipped_sales_v2** semantics:
every order counts once, only shipped amounts recognize sales, and positive-sales
products rank by sales DESC, count DESC, UUID ASC before LIMIT. Six B-owned hourly/
daily tables retain currency, creation buckets, and summary/status/product dimensions.
A's shared AnalyticsReader DTOs/protocol are used by B's reader and A's bridge.
The bridge uses a read-only REPEATABLE READ transaction. There is no raw-order read
fallback or application cache.

Revision 0004 adds durable bootstrap/coverage, archived rebuild rows, operational
counters and NUMERIC(38,2) aggregate amounts. It does not certify existing totals.
Explicit bootstrap archives/rebuilds/validates them before enabling coverage. Source
orders, outbox history and processed-event deduplication survive rebuilds. See
[analytics](analytics.md) and [operations](release-operations.md) for exact guarantees.
