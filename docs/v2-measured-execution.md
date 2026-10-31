# V2 actual execution report

Status: **locked 1M/100-database milestone verified**, including real authenticated
HTTP, direct SQL, measured latency, ordinary live delivery and injected recovery.
The observed p95 target passes with a narrow margin; limitations are recorded below.

## Reviewed baseline and isolation

Pavan safety fix b452d31da56cc7942f6efcaf500f5d8eac7f41e5 was fetched by immutable
SHA and merged into codex/v2-final-backend, retaining 8561e79/972c365 and the verified
manifest. No main/release merge. The original V1 cluster/data/checkpoints remain at
D:/cyberforge/.runtime/release-pgdata, port 55433, without reset/restart/purge.

Automatic provisioning and migration failure drops are removed in that commit.
Explicit deprovision still contains forced-drop support and is not used for execution.
Three safety tests passed against disposable v2bench_tests_39594bd50b: failed parity
retains/quarantines the target, unknown destination provenance is rejected, and
inactive public tenants fail closed. Test database schema initialization was required
before those tests; an initial missing-table setup failure was corrected by applying
Alembic only to that disposable database, never to the retained V1 schema.

## Manifest-scoped copy

The published whole-tenant command is deliberately not used: it would copy 100k
orders per tenant and whole-tenant rollups. New subset_migration consumes the locked
78b1ee4a... manifest, checks source fingerprints in a read-only REPEATABLE READ
transaction per tenant, and copies only its first 10k UUIDs plus matching full history.
All tenants match the original global manifest snapshot. No synthetic history.

Control DB: v2bench_control_78b1ee4a. Dedicated target prefix: v2bench_78b1ee4a_.
Existing destinations need both the matching registry record and a tenant/manifest
marker inside the target; unknown/mismatched destinations fail closed. Order/history
inserts and progress cursor commit atomically in 1,000-order batches. A cluster
advisory lease excludes another participating subset migrator. Failures retain all
data and mark FAILED. Crash during creation before the marker commits deliberately
requires ownership review; it is not blindly retried as an empty owned database.

Targets remain MIGRATING until exact source/target hashes, row counts, timestamps,
versions, six SQL rollup oracles, canonical projections and actual coverage pass.
No source projections, processed-event records, aggregates or coverage are copied
from the 100k cohort. Existing bootstrap builds those from the selected 10k rows.
Bootstrap generation checkpoints are reused on retry.

Original historical payloads/delivery fields are preserved exactly, including NULL
published_at. No fake Redis publication marker is written. Baseline projection
versions make subsequent historical delivery stale/idempotent; the live test drains
one target's real historical outbox to prove that behavior. Other targets retain
their genuine historical relay backlog; bootstrap is not a claim of full-stream
ingestion. Never treat tenant revision as an order-version watermark.

Migration chain: unchanged 0004 -> additive 0005, one head. Operational target copy
checkpoint metadata is excluded from domain Alembic autogeneration, like control
registry metadata. Pilot alembic check reports no new upgrade operations.

Cached-pool routing was hardened: normalize UUID keys, recheck registry state even
for cached engines, and turn registry SQL failures into unavailable errors. Connection
budgets are per process. Final HTTP uses one API process, four users, ten cached pools,
one connection/pool and zero overflow. LRU eviction is not a universal global
connection bound for arbitrary concurrent users/workers.

## One-tenant pilot evidence

Target v2bench_78b1ee4a_0457061cdb4d553e9b3cba32ec9e5f57:
10,000 orders, 17,371 matching outbox events, exact complete-field parity, all six
rollups/projections and coverage verified. Migration took 20.126 seconds. Observed
database size: 42,168,803 bytes. Both currencies, daily/hourly trends, four HTTP
response contracts and expiring scoped-key authorization passed.

Real HTTP pilot, four users / 100 samples per shape:

| Endpoint | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| summary | 28.002 | 38.486 | 88.834 |
| trends | 30.119 | 37.351 | 67.786 |
| top-products | 26.265 | 37.352 | 42.049 |
| status-breakdown | 24.889 | 38.690 | 44.439 |

These are one-database pilot results, not the 100-database milestone. Pilot normal
source p95 ratio was -11.33%; this reflects short-window noise/scheduling, not a
claim that analytics makes PostgreSQL faster. Full-scale measurements follow bulk
completion and regression tests, without another heavy workload in parallel.

## Reproduction

Use the prepared manifest, not a whole-tenant migrate-tenant command. DATABASE_URL
points to retained source postgres; CONTROL_DATABASE_URL points to the isolated
control database above. Use the worktree's src and Python 3.12 virtual environment.
All generated artifacts/logs/keys/database files remain outside Git.

```powershell
python -m astraroll.v2.subset_migration --manifest artifacts/v2-plan.json --output artifacts/v2-pilot.json --limit 1
python -m astraroll.v2.scale_execution --manifest artifacts/v2-plan.json --output artifacts/v2-pilot-http --limit 1 --samples 100
python -m astraroll.v2.subset_migration --manifest artifacts/v2-plan.json --output artifacts/v2-bulk-migration.json --limit 100
# Before live writes: preserve migration parity report. Live writes intentionally
# change two existing selected orders and the delivery metadata of one target.
python -m astraroll.v2.scale_execution --manifest artifacts/v2-plan.json --output artifacts/v2-final-http --limit 100 --samples 200 --live
```

Full measured counts, storage/WAL, regression results, latency, source interference,
ordinary visibility and injected recovery results follow below.

## Completed migration and regression gate

All 100 dedicated target databases have exactly 10,000 orders: **1,000,000 total**,
with **1,733,742 copied historical events**, zero generated historical orders and
4,189,384,776 bytes combined target database size at migration verification.
One RAM guard pause occurred after 38 verified tenants; all checkpoints/targets were
retained. The remaining 62 were completed with 500-order batches instead of 1,000.
No second pause occurred in that continuation. Resume command:

```powershell
python -m astraroll.v2.subset_migration --manifest artifacts/v2-plan.json --output artifacts/v2-bulk-migration-part2.json --start-index 38 --limit 100 --batch-size 500
```

Whole-cluster WAL generation during the two migration windows was 1,562,372,433
and 2,613,854,893 bytes respectively, including background activity. These counters
are not per-target attribution. Full source/target re-verification runs before final
measurements; source is read-only throughout this execution path.

**316 tests passed, zero skipped**, one existing Starlette/httpx deprecation warning
in 142.12 seconds. This is the combined suite including Pavan's 19 V2 tests and the
new interrupted-copy/idempotent-resume, cached-routing and live recovery tests.
Ruff check/format pass (105 files), one Alembic head 0005, pilot alembic check reports
no new operations, and Compose configuration validation passes. Tests ran against
the disposable test database, after bulk migration and before performance sampling.
Implementation milestone **bb702ab** is pushed to origin/codex/v2-final-backend.

## Final 100-database benchmark

Final execution completed successfully with all 100 tenants, both currencies and
all four HTTP response contracts matching exact direct SQL before and after live
updates. Hourly and daily UTC trend buckets, including zero buckets, match. All six
rollups, canonical projections, monotonic versions and durable coverage match.
Authenticated cross-tenant analytics and producer reads are denied; invalid/expired
keys return 401. A warmed cached pool denies a temporarily inactive tenant (503),
then resumes after status restoration. No tenant UUID is treated as authentication.

One real Uvicorn process; four concurrent HTTP users; 200 round-robin samples per
endpoint per pass, across all 100 databases. Ten cached pools, one connection each,
zero overflow. Requests cover the full Oct 1–8 UTC creation window, USD latency
samples; correctness additionally covers EUR. No heavy migration/test workload ran
concurrently. Same-host client/server and synthetic uniform 10k tenants are limits.

| Endpoint | Pre-live p50 | Pre-live p95 | Pre-live p99 | Post-live p50 | Post-live p95 | Post-live p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| summary | 95.103 | 174.757 | 203.714 | 97.943 | 199.957 | 233.749 |
| trends | 86.213 | 156.331 | 199.466 | 86.460 | 189.097 | 282.130 |
| top-products | 91.037 | 165.394 | 195.998 | 92.235 | 162.198 | 208.816 |
| status-breakdown | 87.863 | 166.622 | 204.157 | 87.130 | 180.243 | 198.899 |

All values are milliseconds. **All eight endpoint/pass p95 values are under 200 ms**.
Post-live summary has only **0.043 ms margin**: this is an observed pass, not a robust
production SLA. Several p99 values exceed 200 ms and are explicitly retained. Per-shape
throughput was 38.66–42.86 requests/s before live writes, 35.99–41.04 afterward.
Percentiles use sorted sample index floor(n*p/100), clamped to the last sample.
Raw latency samples and EXPLAIN ANALYZE/BUFFERS plans (raw oracles plus six actual
aggregate-reader SELECTs) are in [benchmark evidence](v2-final-benchmark.json).
The captured aggregate-reader SELECTs do not scan the raw orders table. Coverage
and routing queries are additional work; the plans are representative of one tenant,
not an exhaustive trace of all tenants. No cache was added.

## Source interference, live delivery and recovery

Independent normal-source process: 2,000 warm indexed point reads per phase at
100 requests/s for 20 seconds. Idle p50/p95/p99: 0.921/1.307/1.572 ms; with four
dashboard users at 20 total requests/s: 0.587/1.154/1.456 ms. Observed p95 ratio:
**-11.71%**, with no positive slowdown in this profile. This does not prove analytics
accelerates PostgreSQL or certify all normal application queries. CPU frequency,
scheduling and a single short paired trial limit inference. It is not a directly
comparable speedup against the old V1 colocated-client measurement. Mixed dashboard
latency over 400 requests: p95 192.878 ms, p99 287.941 ms.

The live pilot genuinely relayed/consumed **17,371 historical events** after bootstrap
without duplicate aggregate contributions. Two existing placed USD orders were then
updated through the authenticated producer API. Both new outbox events reached the
private benchmark Redis stream and the V2 consumer, with exact Decimal sales/status
deltas visible in the authenticated API. No additional order was created.

- Ordinary update visibility: **48.242 ms**, including PATCH, relay, consume and GET.
- Commit-before-ACK injection: database mutation committed, ACK failed, message
  remained pending, XAUTOCLAIM redelivered, processed-event dedup prevented a second
  contribution, and ACK completed. Visibility including that sequence: **145.957 ms**.
- Reclaim used min_idle_claim_ms=0 and explicit invocation. These are coordinated
  synchronous probes, not a steady-state background-worker freshness/recovery SLA.
- Post-live SQL/API verification again passed across all 100 databases/currencies.

The expected injected RuntimeError appears in retained logs; it is a tested failure
boundary, not an unhandled execution failure. The raw result's old literal scope
label says one order; the two recorded UUIDs/live_events=2 and the executed checks
establish two updates. The label has been corrected in the source for future runs.

## Final retained counts, storage and remaining limits

Independent post-live inventory confirms **100 ACTIVE benchmark databases, exactly
10,000 orders and 10,000 projections each**, one tenant row each, **1,000,000 orders**
total. Outbox: 1,733,742 migrated historical events + 2 live events = **1,733,744**.
Combined target size after live processing: **4,213,922,988 bytes**. Full per-target
counts/status/storage are in [post-live inventory](v2-post-live-inventory.json);
original migration hashes are in [pre-live evidence](v2-migration-evidence.json).

V1 still has **10,000,000 orders, 17,333,300 events and exactly 100,000 orders per
tenant across 100 tenants**. D: free 135,423,741,952 bytes; C: free 14,582,890,496
bytes at final inventory. WAL directory 3,539,992,576 bytes. fsync, full_page_writes
and synchronous_commit remain ON. No shared-mode rollback after V2 writes is claimed:
V1 is archival and does not contain the two new V2 updates. Do not rerun migration
over live targets or switch back to shared mode as a data-safe rollback.

Ten-second samples observed minimum RAM/commit headroom of 1.241/4.249 GB in the
first migration window, 1.665/5.119 GB in continuation, and 3.147/4.498 GB during
final execution. The RAM guard dip occurred between samples, so sampled minima are
not absolute minima. Maximum sampled host CPU: 53% migration, 44.2% final execution;
not exclusively PostgreSQL CPU. Watcher wait/session counts are scoped to source
postgres, not all tenant databases. All-cluster interference snapshots each observed
17 client sessions; no full-run cluster connection peak is claimed. Server limit
100 with 3 superuser-reserved connections; budgets are per process.

**Remaining operational limits:** 1,716,371 historical events remain honestly
unpublished in the other 99 target outboxes. Their projections/rollups are already
correct from verified bootstrap. Full historical streaming throughput/backlog drain
and default polling-delay freshness across all tenants were not benchmarked. Plan
catchup before claiming a global real-time worker SLA; no fake published markers were
used. Database roles/OS access remain trusted as documented; bearer API routing and
denial tests do not constitute a complete production security certification.

No code/data correctness blocker remains for the measured locked milestone. The
sub-200-ms margin and historical transport backlog remain material limits. Nothing
was merged into main/release. Raw database/order/event datasets and credentials are
not committed; published JSON contains summaries, digests, timings and plans only.

## V2-only runtime handoff

The legacy shared relay/consumer entrypoints do not select V2 automatically. Explicit
`python -m astraroll.v2.workers relay` and `consumer` commands now run the measured
MultiTenantRelay/MultiTenantAggregationConsumer classes with signal-aware loops and
fail before connections unless database_per_tenant and CONTROL_DATABASE_URL are set.
`compose.v2.yaml` overrides the legacy commands/environment; validate with:

```powershell
docker compose -f compose.yaml -f compose.v2.yaml config --quiet
```

For the retained portable runtime, use the existing V2 control database (not the
archival source) for the API and worker environment:

```powershell
$env:DEPLOYMENT_MODE='database_per_tenant'
$env:DATABASE_URL='postgresql+psycopg://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a'
$env:CONTROL_DATABASE_URL=$env:DATABASE_URL
$env:REDIS_URL='redis://127.0.0.1:56380/0'
$env:MAX_ACTIVE_TENANT_POOLS='10'
$env:TENANT_POOL_SIZE='1'
$env:TENANT_POOL_MAX_OVERFLOW='0'
# Issue a local scoped expiring key; configure only its digest record, never commit keys.
python -m astraroll.analytics.issue_key --subject demo --tenant 0457061c-db4d-553e-9b3c-ba32ec9e5f57
uvicorn astraroll.api:app --host 127.0.0.1 --port 8000
# Separate processes, only after deciding historical backlog/catchup scheduling:
python -m astraroll.v2.workers relay
python -m astraroll.v2.workers consumer
```

Never start the archival V1 relay against the V2 stream: its unselected 9M historical
orders are outside the migrated cohort. Do not switch to shared mode as rollback.
The Compose override is configuration-validated, not a claim that containers ran
this portable-Windows benchmark. Background workers were not left draining 1.7M
historical events without a bounded catchup plan. PostgreSQL/Redis and the verified
V2 data remain available; the private benchmark HTTP helper was stopped afterward.

Final runtime-wiring regression gate: **317 passed, zero skipped, one existing
deprecation warning in 89.99 seconds**. Ruff check/format pass (106 Python files),
both base and V2 Compose configuration validation pass, and the actual V2 consumer
CLI completed one empty private-stream batch successfully. This extra smoke check
did not drain the pending historical outboxes or change any tenant order.
