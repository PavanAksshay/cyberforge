# 100-tenant live restoration — 2026-11-01

The current dashboard is http://127.0.0.1:8000/. This supersedes the earlier
benchmark-unavailable decision in the isolated live-product report. The owner
subsequently requested full operation of all 100 tenants on port 8000.

The existing baseline has **10,000,034 orders** across **100 dedicated databases**:
98 databases contain 100,000 each, tenant `0a5f419c-450d-56d0-aea5-f6a5b6a11a29`
contains 100,024, and tenant `0cb398ed-3c9a-5524-8dd6-3eff835c3a71` contains 100,010.
No historical orders were generated or removed. The earlier million-order milestone
does not describe this expanded dataset.

Each repair uses the existing bootstrap/reducer and tenant fences. All six rollups
are validated by the bootstrap's bidirectional SQL EXCEPT ALL oracle. Canonical
projection snapshots and versions are compared to orders. Full ordered fingerprints
of every order and outbox column match before and after each tenant repair.
Old projections, rollups and coverage remain in `rebuild_archive` for inspection.
No aggregation models or Alembic migrations were changed.

Coverage is UTC **[2026-10-01, 2026-11-02)** for USD and EUR. All statuses count once;
only shipped orders contribute sales. Updates reverse the previous contribution
and apply the new one. Cancellation preserves the count and reverses shipped sales.
Top products require positive sales and sort by sales descending, order count
descending, then product UUID ascending. Monetary comparisons use Decimal.

The release gate checks all 100 registry mappings and persisted ready coverage.
The dashboard's architecture totals are explicitly the last audited baseline;
live imports can change subsequent order counts. Normal analytics requests read
rollups, not raw orders. Explicit SQL verification is a separate diagnostic action.

## Start and authenticate

From `D:/cyberforge-v2-integration`, with the existing protected PostgreSQL 55433
and Redis 56380 already running:

```powershell
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
& D:/cyberforge/.venv/Scripts/python.exe scripts/live_benchmark.py
# Separate terminal, same working directory and PYTHONPATH:
& D:/cyberforge/.venv/Scripts/python.exe scripts/live_benchmark.py --worker
```

Open the key icon and load `.runtime/local-access-key.txt` using **Local API key file**,
then **Save Token**. It is read locally and sent only to this same-origin API.
The existing expiring bearer key scopes the operator to the 100 registered tenants.
Only its digest is configured server-side. The key and runtime configuration are
ignored by Git. A separate local probe key scopes only one tenant for denial tests.
An absent, invalid or expired key fails closed. Keep localhost binding; this is
not production identity, TLS, RLS or hardened credential lifecycle certification.

Use **All verified coverage** to view the entire historical dataset. Latest-day
and latest-three-day presets end at the actual coverage boundary. Auto-refresh
fetches real state; it does not generate events or advance processing with timers.
Historical materialization is labeled SNAPSHOT. Worker health comes from an actual
heartbeat, not an assumed checkpoint.

## Event-driven writes and recovery

The verified launcher enables authenticated order and CSV writes for these tenants.
Default configuration still denies benchmark writes. Order create, update and
cancel commit order/outbox atomically before emitting best-effort live work hints.
The worker publishes bounded explicit cohorts to `astraroll:orders:v1`, field
`event`, schema version 2, and invokes the existing consumer/reducer. Stream ACK
follows the PostgreSQL commit. Redis work receipts are retained through publication
until projection completion, including process restarts. A bounded round-robin
PostgreSQL outbox check recovers missing hints. Projection-covered historical
backlog is not globally drained or double-applied.

Recovery tests cover lost hints, Redis failure after a successful source commit,
crash after publication, pending-message recovery, duplicate delivery, updates and
cancellation. Write tests run against isolated PostgreSQL 55434 / Redis 56381;
they do not insert demonstration orders into the preserved 100-tenant baseline.

Coverage expires according to configured age and remains a bounded window. After
expiry or writes beyond the covered UTC boundary, perform an explicitly reviewed
coverage/reconciliation operation; never bypass the guard or fabricate freshness.

## Reproduce verification

```powershell
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
& D:/cyberforge/.venv/Scripts/python.exe -u -m astraroll.v2.verify_reconciliation --measure
# Local detailed evidence (not committed bulk/runtime data):
# .runtime/tenant-analytics-repair.json
# .runtime/restored-api-verification.json
# .runtime/restored-performance.json
```

The API oracle checks every tenant and currency against independent raw SQL for
summary, hourly and daily trends, top products and status breakdown. The separate
latency profile uses four concurrent authenticated HTTP clients, 200 samples per
shape, round-robin across all 100 databases, USD, full 32-day coverage, daily trends.
The API has at most four active tenant pools (one connection each); the running
worker has at most two. Query plans are retained with the local measurement file.
No raw-order scan is permitted in captured analytics-reader statements.

Tests must use the separate disposable test instance, never protected port 55433:

```powershell
$env:DATABASE_URL='postgresql+psycopg://astraroll@127.0.0.1:55434/postgres'
$env:CONTROL_DATABASE_URL=''
$env:DEPLOYMENT_MODE='shared'
$env:REDIS_URL='redis://127.0.0.1:56381/0'
$env:RUN_INTEGRATION='1'
$env:ANALYTICS_API_KEYS_JSON=''
& D:/cyberforge/.venv/Scripts/python.exe -m pytest -ra
$pythonFiles = @(git ls-files '*.py')
& D:/cyberforge/.venv/Scripts/ruff.exe check @pythonFiles
& D:/cyberforge/.venv/Scripts/ruff.exe format --check @pythonFiles
& D:/cyberforge/.venv/Scripts/python.exe -m alembic heads
& D:/cyberforge/.venv/Scripts/python.exe -m pip check
# In web/:
npm test
npm run lint
npm run typecheck
npm run build
```

The repair CLI is resumable but is not a routine read path. Its manifest is an audit
checkpoint, not disposable cache. Do not remove it to trigger another rebuild.
Resource guards paused earlier attempts safely when Windows commit headroom fell
below 1 GiB; the owner freed memory and execution resumed from persisted jobs.
Recovered pause messages remain in the audit history. PostgreSQL 55433 was never
restarted, reset or purged. There was no source-data-directory copy or database drop.

Measured results and the final validation outcome are recorded in
[restoration evidence](100-tenant-restoration-evidence.json).

The 800-request latency run returned:

| Shape | p50 ms | p95 ms | p99 ms | p95 < 200 ms |
| --- | ---: | ---: | ---: | :---: |
| Summary | 97.15 | 203.19 | 262.69 | No |
| Daily trends | 78.23 | 176.67 | 202.48 | Yes |
| Top products | 173.74 | 314.90 | 393.21 | No |
| Status breakdown | 90.56 | 118.06 | 207.25 | Yes |

All 1,000 accuracy requests matched SQL (100 tenants × two currencies × five
requests including hourly/daily trends). The performance target is **partially
met**, not an all-endpoint pass. The long-window top-products query and summary
latency remain optimization work. There was no cache or raw-orders fallback.

Final checks: **357 backend tests passed** in 143.72 seconds, with one existing
Starlette/httpx deprecation warning. All **10 frontend tests**, TypeScript, ESLint,
production build, Ruff check/format (119 tracked Python files), Compose validation,
dependency checks and migration checks passed. There is one Alembic head,
`0005_tenant_access_paths`, and no model/schema drift on the isolated test instance.
Real HTTP probes verified all 100 tenant status routes, enabled write policy,
ready coverage, zero unapplied live events, and matching order/projection counts.
Unauthenticated and invalid keys returned 401; a one-tenant key was denied access
to another tenant's four analytics shapes and status route with 403. Invalid limits,
missing currency and unsupported UTC boundaries returned 422.

At the post-repair inspection, the 100 V2 databases occupied **33,249,262,764 bytes
(30.966 GiB)** including retained derived-data archives. **10,143,992 old derived
rows** remain archived. D: had **103,801,688,064 bytes (96.673 GiB)** free. The WAL
directory contained 256 segments / **4 GiB**; cumulative WAL statistics are included
in the evidence and are not presented as a measured repair delta. PostgreSQL had
31 observed client connections against a limit of 100. Windows had approximately
14.2 GiB of commit headroom. These are point-in-time observations, not peak claims.

The final V1 check still found **10,000,000 orders and 17,333,300 outbox events**.
Protected PostgreSQL retained its original postmaster start time,
`2026-10-31T15:07:52.841385Z`. Source-interference and sustained 100-tenant write
throughput were not re-benchmarked in this repair profile. The preserved baseline
was not modified for write demonstrations; full producer/Redis/consumer/API write
and recovery regressions used the separately authorized disposable test instance.
