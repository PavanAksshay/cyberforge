# Analytics integration contract

## Owner decisions and compatibility

The project owner explicitly approved the following during this integration phase:

- Recognized sales are **shipped-only**. Placed and cancelled orders contribute zero.
- All orders still count once, including cancellations.
- Top products include **positive sales only**, ordered by sales DESC, order count
  DESC, then product UUID ASC. Apply the order and limit at persistence level.
- Analytics authentication uses server-issued expiring opaque API keys, with only
  SHA-256 key digests and trusted identity/membership records in server configuration.

The metric semantics identifier is now **`created_at_shipped_sales_v2`**. This
replaces `created_at_current_state_v1`, which included placed-order sales. The
producer event contract is unchanged: schema_version 2, Redis stream
`astraroll:orders:v1`, field `event`.

Developer B's `origin/dev-b@36a0aeb1fbaec3bf6509f6e47e3db4a64efca27f` was reviewed.
Its six currency-partitioned hourly/daily rollup tables, Decimal reader results,
schema-2 ingress adapter, and shipped-only contributions now match these metric
choices. No B files, models, migrations, reducers or shared migration registration
are copied into dev-a. Optional imports allow dev-a to start without B installed.
A's `PostgresAnalyticsAdapter` wraps B's reviewed `get_*` methods instead of
recreating the aggregate schema or B's independently named protocol.

A final fetch also reviewed **`790907514a72db109cf93ccb46c4bce926712330`**. That
commit fixes fractional-cent quarantine, invalid projection handling and claim
cursor progression; all three A-owned regression checks now pass against it.
However, it restores placed + shipped sales, zero-sales product inclusion and
sales/UUID ranking, advertising `created_at_current_state_v1`. These differ from
the owner's explicit decisions above. The adapter rejects that advertised
mismatch with 503 before SQL, even if credentials and coverage are otherwise
configured. No successful new-semantics response is relabeled from old data.
B must align contribution logic, SQL top-N selection and its advertised semantics
to `created_at_shipped_sales_v2` before that latest backend can be enabled.

## Four query APIs

| Route | Additional parameters | Metrics |
| --- | --- | --- |
| GET `/analytics/summary` | none | total_order_count, sales_amount |
| GET `/analytics/trends` | interval=day or hour (default day) | ordered, dense buckets with counts/sales |
| GET `/analytics/top-products` | limit=1..10 (default 10) | ranked product UUID, count, sales |
| GET `/analytics/status-breakdown` | none | placed, shipped, cancelled counts/sales |

Every query requires tenant_id (UUID), currency (uppercase three-letter code),
and start/end (UTC-aware ISO 8601 timestamps). The window is **[start, end)**,
start < end, at most 366 days. Currency filters both counts and amounts: no FX,
implicit currency or mixing. Timestamps must use UTC and complete hour boundaries;
naive/date-only timestamps, epoch numbers, non-UTC offsets and partial hours are
422. Daily trends require midnight boundaries. Unsupported intervals are 422.

The adapter chooses hourly aggregates when covered, or daily aggregates for full
UTC days when only those are covered. Trends require the exact requested stored
grain to be certified. A daily proof cannot authorize partial-day reads. Windows
outside known retention are 422; unavailable/uninitialized coverage is 503.
No rounding of boundaries, raw-order scans, event replay or Redis reads occur on
normal analytics queries.

## Metric definition

These are **current-state metrics for an order creation cohort**, not historical
as-of status or event-flow totals. Filter and bucket by immutable created_at;
read the latest projected status, amount and product at the reader snapshot.

- Count each order once in its creation bucket, including cancelled orders.
- Only shipped orders contribute their latest Decimal amount to recognized sales.
- Placed amount/product changes replace the previous projected contribution and
  retain creation time. Product changes move counts to the current product.
- Shipping moves a count from placed to shipped and recognizes its sales once.
- Cancellation moves placed to cancelled, retaining total count with zero sales.
  Producer terminal-state rules remain unchanged; shipped orders cannot be cancelled.
- Top products omit groups with zero recognized sales. At equal sales, higher
  order count wins; UUID ascending resolves equal count. Orders are not units sold.
- Status rows are always placed, shipped, cancelled, in that order. Placed and
  cancelled rows must have zero recognized sales.

Decimal amounts serialize as JSON strings; floats are rejected. Aggregate amounts
are not capped to a single order's precision at the API boundary. B currently uses
NUMERIC(18,2) for persisted totals, so aggregate overflow remains a persistence
capacity limit and fails as unavailable rather than being rounded or clamped by A.

Ready, genuinely empty scopes return zero summary, zero-filled trend buckets,
empty products, and three zero status rows. Empty success is never inferred from
missing data or the first processing revision. Duplicate/invalid groups and
out-of-window trend rows fail instead of being silently merged.

## Real reader configuration and the remaining coverage blocker

Defaults remain fail-closed:

```dotenv
ANALYTICS_BACKEND=unavailable
ANALYTICS_API_KEYS_JSON=[]
```

In an approved combined deployment with B's package and migration 0003 present:

```dotenv
ANALYTICS_BACKEND=postgres
ANALYTICS_COVERAGE_PROVIDER=approved_package.coverage:read_coverage
```

**Do not configure a fake or always-ready provider.** The provider name above is
an illustrative entry point, not an implementation included in this repository.
B has not yet published durable bootstrap/retention/coverage state. Until that
provider is implemented and reviewed, valid authenticated requests still return
503. A first tenant_processing_state revision, MAX(bucket_start), current clock,
Redis stream tail, or row existence is insufficient proof.
The reader also requires B's processing-state row for honest revision metadata;
a certified empty baseline can use revision 0 with a null processing timestamp.

The synchronous provider receives the adapter's **same read-only Session** plus
validated AnalyticsQuery and returns `CoverageProof` from
`astraroll.analytics.coverage`:

| Field | Requirement |
| --- | --- |
| tenant_id / currency | exactly match the authorized query scope |
| start / end | trustworthy half-open retained creation-time coverage |
| metric_semantics | exactly created_at_shipped_sales_v2 |
| ready | true only after trustworthy baseline/bootstrap completion |
| intervals | certified stored grains: hour and/or day |
| dimensions | initialized summary, product and/or status dimensions |

This is a query-side DTO, not a new aggregate table. B owns the durable source and
must read it through the supplied Session so proof, metrics and metadata share a
snapshot. Provider exceptions may be AnalyticsUnavailable (503) or
UnsupportedAnalyticsWindow (422), with safe public diagnostics. Unknown or invalid
providers fail closed. Operators must rebuild/verify older materialized totals if
their contribution semantics differ; changing a label does not repair old data.

Each read uses a PostgreSQL **REPEATABLE READ, READ ONLY** transaction. Coverage,
B's freshness lookup and all result queries occur inside that transaction.
Expected database errors become a generic 503 without SQL text/credentials.
Unexpected programming errors remain failures, never fabricated zeros.

## Freshness

Responses echo tenant/currency/start/end and metric_semantics. Optional freshness
contains:

- checkpoint: null unless an actual checkpoint is supplied; A invents none.
- last_processed_at: B's actual last_successful_apply_at, normalized to UTC.
- tenant_revision: B's durable tenant_processing_state.revision.

The revision is tenant-wide, including other currencies. It is not an order
version, ingestion completeness watermark, Redis position, per-currency lag or
historical as-of timestamp. A last processed timestamp cannot prove that no older
messages remain pending. Separate HTTP requests can observe different snapshots;
metrics and metadata within one response share a consistent snapshot.

Example covered empty summary (a contract example, not a live result):

```json
{
  "tenant_id": "5aa5848f-cd4f-46b4-87cd-eb241341e152",
  "currency": "USD",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-02T00:00:00Z",
  "metric_semantics": "created_at_shipped_sales_v2",
  "freshness": {"checkpoint": null, "last_processed_at": null, "tenant_revision": 0},
  "total_order_count": 0,
  "sales_amount": "0.00"
}
```

## API-key authentication

Generate a 256-bit opaque key locally, for one or more authorized tenants:

```powershell
./.venv/Scripts/python -m astraroll.analytics.issue_key --subject demo --tenant YOUR-TENANT-UUID --hours 24
```

The command prints the secret once, followed by a registry record. Keep the secret
in the client; never put it in git, screenshots or logs. Put only the record in a
server-side `ANALYTICS_API_KEYS_JSON` JSON array. Each record requires key_sha256
(64 lowercase hex characters), subject, a nonempty tenant_ids UUID list, and an
aware expires_at timestamp. The issuer accepts expiry 1..720 hours. No real keys
or registry records are generated into tracked files by this implementation.

Send `Authorization: Bearer <issued-key>`. The verifier hashes the presented key,
uses constant-time digest comparisons, rejects unknown/expired keys, and derives
subject/membership only from trusted server records. Caller headers/UUIDs cannot
grant tenant access. Empty, invalid or duplicate-digest registries return 503;
missing credentials or invalid/expired keys return 401; another tenant returns
403 before reader/SQL access. Removing a record revokes that key after API workers
restart or trusted settings reload: Settings is cached, so editing a file does
not hot-reload credentials automatically. Expiry is checked on every request.

This is real credential verification for analytics, with tests for valid, expired,
unknown and revoked keys and cross-tenant denial. It is a small hackathon identity
mechanism, not a complete application security system: producer routes remain
unauthenticated, and there is no user lifecycle, RLS or full identity service.
Use trusted server configuration and appropriate transport protection before
exposure. There is no allow-all fallback or development principal in deployment.

## Verification and isolated combined checks

```powershell
# A-only tests (combined-only modules skip when B is absent):
$env:RUN_INTEGRATION = '1'
./.venv/Scripts/python -m pytest -q
./.venv/Scripts/ruff check .
./.venv/Scripts/ruff format --check .
docker compose config --quiet

# Export an immutable B snapshot and overlay only A-owned integration files:
./.venv/Scripts/python scripts/prepare_combined_check.py --b-ref origin/dev-b
# In the printed temporary directory, with DATABASE_URL / REDIS_URL set:
# $env:PYTHONPATH = '<printed-directory>/src'
# D:/cyberforge/.venv/Scripts/python -m pytest -q -rx
```

The helper never merges/switches branches or edits migration registration. It
retains B's models, migration 0003, consumer and tests unchanged in a temporary
checkout, and writes a manifest of the B commit and overlaid file hashes.
Temporary exports and test data are never committed or auto-deleted.

Combined tests use PostgreSQL rollups and Redis 7.4.3. They exercise real API-key
verification, order API/outbox/relay/consumer processing, all four analytics
queries, multi-tenant/currency isolation, corrections in original buckets,
zero-sales exclusion, top-product LIMIT ties, replay/old versions/quarantine,
readiness vs empty, and repeatable-read snapshot consistency. A test-harness
coverage proof is enabled only after its known inputs are processed; it is not a
production provider or an assertion that B's durable coverage blocker is solved.
Normal analytics SQL is audited to exclude raw orders/outbox/projections.

Verification ledger (Python 3.12.10, PostgreSQL 16.6, Redis 7.4.3):

| Environment | Results | Meaning |
| --- | --- | --- |
| dev-a only | 169 passed, 2 skipped | combined-only modules require B; all producer tests enabled |
| combined with reviewed 36a0aeb | 242 passed, 3 expected failures | full approved-semantics pipeline works; three legacy B defects remain visible |
| combined with latest 7909075 | 245 passed, 7 skipped | three defect checks pass; seven successful-real-read scenarios blocked by advertised metric mismatch |

The three strict expected failures apply only to the immutable legacy 36a0aeb
snapshot: fractional-cent rounding, discarded claim cursors and invalid projection
double counting. They are normal passing regression tests on 7909075, not hidden
xfails. The older snapshot is a compatibility verification artifact, not a
recommended deployment. Its missing durable coverage provider and defects still
prevent declaring it production-ready.

The latest snapshot's skipped scenarios are explicit contract blocks, not claims
that all four endpoints are live against the latest reducer. Separate rejection
checks prove the adapter fails closed before SQL. A real Redis 7 test verifies
commit-before-ACK crash recovery, abandoned-message claim/ACK and duplicate safety.
B's latest recovery tests also run in the combined suite.

A lint/format/dependency checks and both Compose configuration checks pass. The
new latest B snapshot is checked separately; A never reformats B's files. Docker
engine probes time out on this host, so no container execution is claimed.

Remaining B work: honor the owner's approved v2 metric/ranking decisions, publish
trusted durable coverage state/provider, and review any old materialized totals
before enabling a new contribution definition. Do not treat first-revision
readiness or last-processed timestamps as complete coverage/watermarks.
