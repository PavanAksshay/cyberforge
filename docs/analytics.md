# Analytics query API and persistence handoff

This phase supplies four routes, typed query/response models, an authorization
boundary, and `AnalyticsReader`. It supplies no aggregate tables, migrations,
consumers, reducers, caches or dashboards. The test fake is under `tests/` and is
never the application reader. Producer event schema 2, stream
`astraroll:orders:v1`, and Redis field `event` are unchanged.

## Integration status and unresolved shared contracts

At initial preflight there was no dev-b branch. A later fetch published
`origin/dev-b@525b79d`, which was reviewed read-only, without merging or modifying
B's files. Its `src/astraroll/aggregation/contracts.py` and `contribution.py`
publish pure logical contracts and in-memory contributions, not durable aggregate
SQLAlchemy models or migrations. Observed contribution semantics match this API:
all orders count, placed/shipped contribute sales, cancelled contributes zero
sales, and hourly/daily buckets use original creation time.

Concrete remaining gaps at that reviewed commit:

- `AggregationKey` contains tenant, window, aggregate type, bucket and optional
  dimension, but **no currency**. `OrderSnapshot` likewise has no currency. A
  real reader cannot safely isolate monetary analytics using that key alone;
  currency must survive ingestion and be included in durable grouping/filtering.
- The internal snapshot uses integer `amount_cents`; `AggregateDelta` exposes
  `order_count` / `sales_cents`. An adapter may convert exact integer cents to
  Decimal amounts, but must never use floats or guess currency. Keep two-decimal
  producer amount semantics rather than assuming arbitrary ISO currency exponents.
- The internal event accepts `order`/`snapshot`, unlike producer schema-2
  before/after snapshots. B must supply the ingress translation/version handling;
  the analytics API does not change the producer wire contract to accommodate it.
- Physical table names, columns, keys, indexes, per-dimension stored grain,
  retention, readiness/coverage and durable checkpoint formats remain unpublished.

The real reader remains unavailable until B publishes a reviewed persistence
contract resolving these gaps. Published pure hourly/daily functions alone do not
establish stored hourly coverage. No organizer-specific alternative metric
definition was present; verify external organizer requirements before binding
the persistence adapter.

## Requests

| Route | Extra parameters | Response-specific fields |
| --- | --- | --- |
| GET `/analytics/summary` | none | `total_order_count`, `sales_amount` |
| GET `/analytics/trends` | `interval=day` or `hour`; default day | `interval`, `buckets` |
| GET `/analytics/top-products` | `limit=1..10`; default 10 | `limit`, `products` |
| GET `/analytics/status-breakdown` | none | `statuses` |

All routes require these query parameters:

- `tenant_id`: UUID, checked against the verified principal's tenant membership.
- `currency`: uppercase three-letter code. It filters counts as well as monetary
  amounts; there is no implicit default, cross-currency sum or FX conversion.
- `start`, `end`: timezone-aware ISO 8601 timestamps explicitly using UTC, such as
  `2026-10-01T00:00:00Z` or `2026-10-01T00:00:00+00:00` (URL-encode the plus sign).
  The range is **[start, end)**. Start must precede end and span at most 366 days.

Naive/date-only timestamps, epoch numbers, non-UTC offsets, partial hours and
unknown query parameters are 422. Start/end must align to exact UTC hours, with
zero minutes, seconds and microseconds. Daily trends require both endpoints at
UTC midnight. Day-based readers require midnight boundaries for **every** route;
hourly readers can support full-hour windows. Hourly trends require hourly
aggregate coverage. Other intervals are rejected, never approximated. Day buckets
are exactly 24 UTC hours; no local timezone/DST bucketing is performed.

An adapter must additionally reject unsupported retention/dimension windows with
`UnsupportedAnalyticsWindow` (422). Missing/uninitialized/untrustworthy coverage
is `AnalyticsUnavailable` (503), not a successful empty response. The API never
rounds boundaries or scans raw orders to fill coverage gaps.

## Metric semantics: `created_at_current_state_v1`

These are current-state order-cohort metrics, evaluated at the aggregate reader's
durable snapshot, **not historical as-of status or a sum of mutation events**.
The time filter and trend bucket use immutable order `created_at`, not event
`occurred_at`, processing time or `updated_at`.

- `total_order_count` counts each order once in its creation-time cohort,
  including cancelled orders, restricted to the requested tenant and currency.
- `sales_amount` sums the latest amount for orders currently **placed or shipped**.
  Cancelled orders contribute **zero sales** while remaining in order counts.
  This measures non-cancelled order sales, not recognized revenue or cash receipts.
- Shipping moves an order from placed to shipped without adding another order or
  another sale. Cancellation moves its count to cancelled and removes its sales.
- An accepted amount edit replaces the previous contribution. A product edit
  moves that order's count and sales from its previous product to its new product.
  Neither edit creates an extra order or moves its creation-time bucket.
- Status breakdown groups current order states into exactly placed, shipped,
  cancelled, in that order. Each row includes count and sales; cancelled sales
  must be zero.
- Top products group by latest product UUID, including products with cancelled
  orders and zero sales, but omitting groups with zero orders. Rank by sales DESC,
  then canonical UUID ASC. Product names/quantities are not present in the producer
  contract, so this API does not invent them or equate orders to units sold.

At the same snapshot and scope, status and trend counts/sales sum to the summary.
Separate HTTP requests can observe different snapshots while ingestion advances.
B must handle schema versions, deduplication, corrections, bootstrap and recovery
before claiming that persisted aggregates satisfy this definition.

## Successful responses and empty data

Every response echoes `tenant_id`, `currency`, `start`, `end`, and includes
`metric_semantics: "created_at_current_state_v1"` and nullable `freshness`.
Monetary values are `Decimal` and JSON strings; source floats are rejected. Counts
are nonnegative integers. Aggregate amounts may exceed a single order's precision;
they are not silently rounded to NUMERIC(18,2) or binary floats at the API boundary.

The following is a **response contract example**, not a live result. It describes
a ready, covered, genuinely empty scope:

```json
{
  "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
  "currency": "USD",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-03T00:00:00Z",
  "metric_semantics": "created_at_current_state_v1",
  "freshness": null,
  "total_order_count": 0,
  "sales_amount": "0.00"
}
```

Empty summary returns zero count and sales. Empty top products returns `[]`.
Trends are chronological and dense: every requested bucket is present, including
zero-valued buckets. Status breakdown always returns all three statuses, filling
missing known-zero groups. The API only performs zero filling after a successful
reader call has asserted coverage. Duplicate groups and invalid/out-of-window
trend buckets produce 503 rather than silently merged or inaccurate results.

Top products are sorted deterministically and limited at the API boundary. B must
still apply the same global order **before** a persistence-side LIMIT: reordering
an arbitrary candidate subset cannot recover omitted winning products.

`freshness` is null unless supplied by the reader. Its optional fields are:

- `checkpoint`: an opaque, durable projection checkpoint in a format B must define.
- `last_processed_at`: actual processing timestamp supplied by B, normalized to UTC.

Neither field is fabricated from the request clock or the current Redis stream
tail. A last processed event/position does not prove all earlier events were
processed when gaps/recovery are possible. These fields do not promise complete
watermarks, zero lag or historical as-of consistency. Metrics and freshness must
come from the same reader snapshot. If a reliable checkpoint is unavailable,
return null rather than inventing one.

## Authorization boundary and errors

**Authentication is not implemented and security is not complete.** The existing
producer routes remain unauthenticated. Analytics defaults fail closed:

| Condition | Result |
| --- | --- |
| Missing bearer credentials | 401, `WWW-Authenticate: Bearer` |
| Bearer supplied but no verifier integrated | 503, `analytics_authorization_unavailable` |
| Verified identity lacks requested tenant membership | 403 before reader access |
| Invalid parameters, unsupported interval or exact window | 422 |
| Reader missing, unready or incompatible | 503, `analytics_unavailable` |

Neither a tenant query UUID, an `X-Tenant-ID` header nor an arbitrary bearer string
establishes identity. `AuthenticatedPrincipal(subject, tenant_ids)` is a **trusted
integration result**, not an input schema for callers. Install an actual provider
for `get_authenticated_principal` that verifies credentials and derives current
tenant membership from trusted claims/storage. Keep `get_authorized_tenant` in
place to enforce membership and pass the requested tenant to the reader.

Tests override the principal provider with explicit test identities and verify
cross-tenant denial. They do not prove signature/token validation, membership
revocation, RLS or production authentication. No allow-all development provider or
test fake is wired into production routes. Real credential verification and its
cross-tenant tests are a separate integration requirement before exposure.

## Exact logical persistence requirements for Developer B

`src/astraroll/analytics/reader.py` defines the synchronous `AnalyticsReader`
protocol. Methods receive immutable validated queries, always including tenant,
currency, start and exclusive end. No physical SQL schema is prescribed.
Read durable materialized totals, not signed event deltas or an in-memory reducer
dictionary; the API must not replay events or reconstruct state on a read request.

| Method | Required logical result in `ReadResult.value` |
| --- | --- |
| `capabilities()` | `ReaderCapabilities(base_granularity="hour" or "day", metric_semantics="created_at_current_state_v1")`; may raise unavailable |
| `summary(query)` | `SummaryMetrics(total_order_count, sales_amount)` |
| `trends(query)` | unique `TrendMetrics(bucket_start, total_order_count, sales_amount)` rows for the requested interval; omitted rows mean known zero |
| `top_products(query)` | unique `ProductMetrics(product_id, total_order_count >= 1, sales_amount)` rows covering the true global top `limit`, or complete candidates |
| `status_breakdown(query)` | unique `StatusMetrics(status, total_order_count, sales_amount)` rows; omitted statuses mean known zero |

Every metrics method returns `ReadResult(value, freshness=None or Freshness(...))`.
Return Pydantic metrics objects with actual `Decimal` amounts, not float-valued
dictionaries. Empty returned collections explicitly assert ready/covered zero data.
Raise `AnalyticsUnavailable` for uninitialized/bootstrap-incomplete/missing data,
or `UnsupportedAnalyticsWindow` for an exact window the stored grain/dimensions
cannot answer. Exception messages must be safe public diagnostics, not raw SQL
errors or credentials. B owns translating expected persistence failures to these
exceptions. Unexpected programming failures remain failures, never success zeros.

Before connecting a real adapter, B must publish and review:

1. Table/model locations, exact field types and keys supporting tenant + currency
   + creation bucket, with current-state counts and sales by status and product.
2. Hour/day grain and alignment for each dimension, retention coverage, and how
   finer rows are combined into requested daily results without raw-order scans.
3. Agreement with the contribution/update/cancellation semantics above, including
   correction of old buckets/products, version gaps and schema-1 bootstrap policy.
4. Readiness/coverage signaling distinct from a legitimate empty tenant/window.
5. Consistent snapshot/read transaction behavior for metrics and metadata.
6. Optional durable checkpoint format/meaning, including gaps and recovery limits.
7. The exact reader provider and the real identity-verification integration.

Provide a callable returning an implementation of `AnalyticsReader` via FastAPI's
`get_analytics_reader` dependency. Install the reviewed adapter in the composition
root (or a dependency override); never replace this with a fake or raw orders scan.
The default `UnavailableAnalyticsReader` remains installed until that review.

## Commands

All four query contracts are visible in `/docs` and `/openapi.json` without a DB.
This request demonstrates the shape but **does not authorize or enable analytics**:

```powershell
$tenant = '5aa5848f-cd4f-46b4-87cd-eb241341e152'
$query = "tenant_id=$tenant&currency=USD&start=2026-10-01T00:00:00Z&end=2026-10-03T00:00:00Z"
Invoke-RestMethod "http://localhost:8000/analytics/summary?$query"
# Current default: 401. With an unverified bearer token: 503, not successful metrics.
# After the real verifier and reader are integrated, use verified credentials with:
# /analytics/trends?$query&interval=day
# /analytics/top-products?$query&limit=10
# /analytics/status-breakdown?$query
./.venv/Scripts/python -m pytest tests/test_analytics.py -q
```

Route tests use precomputed aggregate views in `tests/analytics_fake.py`, never
raw order rows or a duplicate reducer. They trap every SQLAlchemy connection to
prove these contract routes do not issue SQL. Run the full producer/integration
suite with `RUN_INTEGRATION=1` against the documented PostgreSQL/Redis setup.
