# Live product verification — 2026-11-01

Historical report: the subsequent owner-authorized 100-tenant repair and current
port 8000 status are documented in [100-tenant restoration](100-tenant-restoration.md).

The isolated mentor demo is READY at http://127.0.0.1:8001. The preserved benchmark
analytics are NOT READY and intentionally return 503. The owner explicitly chose
not to repair benchmark derived data during this pass. No main/release merge occurred.
Branch: `codex/astraroll-v2-final-product`; the checked-out commit is the
release reference (`git rev-parse HEAD`). Evidence: [machine-readable proof](live-product-evidence.json).

The before/after read-only audit found 100 preserved V2 databases and **10,000,034 orders**,
not the older 1M milestone. All compared V2 order-field fingerprints, counts, sums,
outbox/projection counts and aggregate revisions match. V1 retains 10,000,000 orders
and 17,333,300 outbox events. The protected PostgreSQL postmaster did not restart.
No protected orders, outbox, projections, rollups or coverage were changed.

The first preserved tenant illustrates why we disabled benchmark analytics: for USD
and UTC [2026-10-01, 2026-10-08), raw SQL reports 14,141 orders / 3,146,407.04 shipped
sales, while the old rollups/API report 5,004 / 1,325,416.36. Coverage still describes
the earlier subset. `scripts/populate_10m.py` is an archived synthetic generator
which bypasses outbox/rollup maintenance; it was **not run**. Its formatting and
warning were corrected for the repository lint gate, without executing its SQL.
Its presence is consistent with the mismatch but is not a verified provenance audit.

The zero-delta problem combined these implementation defects:

- The old import trigger published one oldest-event batch. Covered historical backlog
  could consume that batch before any newly imported order was published.
- The frontend advanced publication/consumption on artificial timers, accepted the
  first successful summary response, and used a stale React baseline variable.
- CSV dates outside the selected UTC window and USD uploads under EUR selection
  legitimately produce zero dashboard delta. The old UI still claimed verification.
- IDs omitted from CSV were random, so identical uploads could double-count.
- Global currency labeled source amounts; mixed-currency totals were summed together.
- Refresh requests could overlap, race across filters, or fail while fake healthy/zero
  fallbacks remained visible. Relative-time labels had no independent reliable clock.

The new import path commits orders plus outbox atomically. It publishes only the
explicitly imported cohort to the configured Redis stream, claims real entries into
an import consumer group's PEL, invokes the existing consumer/reducer and ACKs after
its PostgreSQL commit. Normal delivery later is safely deduplicated. It does not
scan/drain unrelated outbox history or introduce a second aggregation implementation.
Each progress stage is read from persisted orders, publication markers, processed-event
receipts and order versions in projections. VISIBLE additionally requires exact API
count and Decimal-sales deltas with identical tenant/currency/[start,end) filters.
Bounded polling times out to PENDING. A saved tab-local receipt allows explicit recovery.
Broker failure never rolls back or disguises a committed import.

CSV IDs without an explicit order_id are deterministic per tenant, full file content
and line. Exact file reuploads are idempotent; semantically changed files should supply
explicit order UUIDs if cross-file idempotency is required. Amount validation rejects
NaN/infinity, fractional cents, nonpositive and NUMERIC overflow values. Five-column
CSVs remain supported; optional product_name is backward-compatible. Currency totals
are grouped; legacy scalar money fields are null for mixed-currency batches. The
browser renders exact money strings, including values beyond JavaScript safe integers. Shipped AOV uses exact integer
cents with half-up rounding; $2534.85 / 30 displays $84.50.

Each mounted analytics/operations view uses the same cancellable, completion-based
polling hook. It groups real authenticated requests, avoids overlapping timers, aborts
on tenant/filter/token/unmount changes, fences late responses, and retains the last
successful timestamp on failure. Authentication failure stops repeated polling.
A separate one-second display clock drives relative-time/countdown labels without
issuing network requests. HTTP fetch time, tenant materialization time/revision and
source event occurrence time are separate. Exact PostgreSQL commit timestamps and
worker heartbeats are unavailable and are not invented. Historical data is labeled
SNAPSHOT rather than LIVE. Operations polls only the selected tenant, not all 100 DBs.

Date controls say **All verified coverage**, not an unqualified All Days. The current
demo coverage is UTC [2026-10-01T00:00:00Z, 2026-10-08T00:00:00Z). Custom UTC windows
are available; unsupported/out-of-coverage queries fail rather than extrapolate.
An outside-window import can be aggregated without being visible in this window.
All four views, exports and SQL verification use identical filters and currency.

Run Verification executes fixed read-only raw-order and hourly-rollup SQL under
REPEATABLE READ with a 3-second statement timeout. It includes the authorized API
result, exact window/currency/database, duration and timestamp. Revision changes
between snapshots yield INCONCLUSIVE_CONCURRENT_CHANGE instead of a false match.
Frontend SQL is never executed. Copy SQL remains available with pgAdmin instructions;
its physical database and port are resolved from authenticated telemetry.

Product names/SKUs are optional metadata in `control_product_catalog`, separate from
all tenant aggregation models/migrations. Authorized PUT updates and CSV-supplied
names persist in the control database. Overview, Explorer, exports and the grounded
assistant resolve the same catalog. Unknown historical UUIDs remain explicitly
Uncataloged product, with a technical identifier; no historical names are invented.
Catalog updates do not change financial orders or aggregates. The first catalog write
creates its separate control metadata table. No tenant schema/migration head changed.

Operations reads real selected-database order/projection/outbox counters, actual
Alembic revision, tenant processing checkpoint, coverage, and 20 bounded persisted
outbox/processing lifecycle records with timestamps, tenant and event correlation ID.
It distinguishes projection-covered unpublished history from unapplied events,
including already-published ones. Errors return 503, never fake zeros. There is no
invented process-log history; unavailable heartbeat/error-log history is labeled unknown.
Archived 1M latency numbers are explicitly historical, not current 10M measurements.

## Measured acceptance

The initial real HTTP/PG/Redis acceptance began from zero: 12 accepted, 0 rejected,
0 duplicates, +12 orders, USD +506.97 sales, status deltas 6 shipped / 4 placed /
2 cancelled, 20 published events, 20 durable receipts and pending ACK count zero.
Raw SQL / rollup / authorized API MATCH. Reupload: 12 duplicates, zero new mutations.
Missing authentication returns 401 and cross-tenant requests return 403.

The repeatable latest acceptance measured 48 → 60 covered orders and 2027.88 →
2534.85 USD sales. Request submission to fresh API visibility was **396.506 ms**,
including synchronous relay/consumer and HTTP overhead. This is one local demo
observation, not a p95 or a 10M performance certification. All four direct SQL
oracles (including hourly/daily trends and exact empty EUR results) pass.

Actual browser interactions verified VISIBLE (+12 / $506.97), EUR-selected/USD-uploaded
explanations with dollar previews, outside-window zero inclusion, real SQL MATCH,
manual refresh, product labels and operational receipts. Five-second polling generated
20 additional real requests per analytics shape between recorded observations. The
one-second display clock and cancellation/race/interval/auth/error behavior also have
automated frontend tests.

A real clean shutdown/save of **isolated Redis 56381** produced COMMITTED/PENDING at
PUBLISHED with TimeoutError in the browser. Restarting that same isolated service and
clicking Resume Last Committed Import recovered exactly +12 / $506.97, with zero
pending events/ACKs. Protected Redis 56380 and PostgreSQL 55433 were not restarted.
The current isolated fixture has 72 orders/projections and 120 outbox events; 12 orders
are intentionally outside coverage, leaving 60 covered USD orders / $2534.85.

## Validation and reproduction

Backend: **350 passed, zero failed/skipped**, one existing Starlette/httpx deprecation
warning. Final focused live-product regressions: **15 passed** after small safety changes.
Frontend: **7 tests passed**, TypeScript, ESLint and production build passed. Ruff and
format checks cover all tracked Python files and new harnesses. Alembic has one head,
0005_tenant_access_paths; actual isolated tenant `alembic check` reports no upgrade
operations. npm audit reports zero vulnerabilities. Git whitespace/credential review
excludes .runtime, .env, credentials, database files and bulk datasets.

Use Python 3.12+, PostgreSQL 16 and Redis 7. This machine's existing isolated services
listen on 55434 and 56381. Do not replace them with the protected 55433/56380 services.
The following commands assume the repository worktree is the current directory and
the shared venv is `D:/cyberforge/.venv`:

```powershell
$env:PYTHONPATH="$PWD/src"
D:/cyberforge/.venv/Scripts/python.exe scripts/live_demo.py --setup-only
D:/cyberforge/.venv/Scripts/python.exe scripts/live_demo.py
# Another terminal, for protected registry inspection only:
D:/cyberforge/.venv/Scripts/python.exe scripts/live_benchmark.py
```

Open http://127.0.0.1:8001. If a new tab needs authentication, use API Bearer Token →
Load local key file → choose `.runtime/local-access-key.txt` → Save Token. The key is
scoped and expires after 24 hours, is kept only in the tab session, and is never bundled
in JavaScript or committed. Trusted server configuration stores its digest/membership/
expiry, not the raw key. The ignored local key file is a development credential, not
production secret management. There is no public hosting/TLS/identity-provider claim.
Coverage expires independently after 24 hours; reconcile stale coverage, do not fake it.
The already-open demo tab is authenticated. Keep both local data services running.

```powershell
# Real acceptance adds only 12 disposable demo orders; exact reruns of its generated
# file are tested for idempotency. This utility deliberately uses fresh order UUIDs.
D:/cyberforge/.venv/Scripts/python.exe scripts/live_acceptance.py
D:/cyberforge/.venv/Scripts/python.exe scripts/verify_live_demo.py
# Read-only protected audit; choose a new output path:
D:/cyberforge/.venv/Scripts/python.exe scripts/protected_fingerprint.py .runtime/audit.json

$env:DATABASE_URL='postgresql+psycopg://astraroll@127.0.0.1:55434/postgres'
$env:CONTROL_DATABASE_URL=''
$env:DEPLOYMENT_MODE='shared'
$env:REDIS_URL='redis://127.0.0.1:56381/0'
$env:ANALYTICS_API_KEYS_JSON=''
$env:RUN_INTEGRATION='1'
D:/cyberforge/.venv/Scripts/python.exe -m pytest -q
D:/cyberforge/.venv/Scripts/python.exe -m alembic heads
$pythonFiles = git ls-files '*.py'
D:/cyberforge/.venv/Scripts/ruff.exe check $pythonFiles
D:/cyberforge/.venv/Scripts/ruff.exe format --check $pythonFiles
cd web
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
npm audit
```

Disposable integration tests create/drop test databases only on the isolated instance,
under the owner's explicit prior authorization. No benchmark migration/provisioning
or purge command is used. Frozen reducer/consumer/aggregate model files and Alembic
migrations are unchanged. Docker includes the built static dashboard and locked multipart dependency. Combined
Compose configuration validates, and pip check passes. An image build
was not used as evidence for this native-runtime demo.

Remaining scope: benchmark analytics need separately authorized reconciliation;
current 10M p95 is unmeasured. No benchmark correctness/performance success is claimed.
Local credential expiry, coverage expiry and unavailable worker heartbeats remain
visible operational limits, not fabricated production guarantees.
