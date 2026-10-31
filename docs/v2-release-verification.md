# AstraRoll V2 integration release verification

## Decision

**READY for authenticated dashboard and scoped existing-order frontend integration.**
All essential implemented-backend correctness, authorization, regression and measured
p95 gates pass on the final runtime candidate. CSV ingestion/import status and an
analytics-assistant backend are not implemented; their frontend controls must remain
disabled. Global historical catchup and sustained 100-tenant freshness remain an
operational limitation, not a completed throughput/SLA claim.

## Integration and provenance

Final integration branch: **codex/astraroll-v2-final-integration**. The requested
integration/astraroll-v2-final name was blocked by Git's existing integration ref;
the owner explicitly approved this alternative and preserving the existing ref.

Verified immutable inputs:

- Developer A: 9078a66d900c17601ebec49e88b39cab30795174.
- Pavan: b452d31da56cc7942f6efcaf500f5d8eac7f41e5.

Their common ancestor is Pavan's input, already contained in A. A fresh worktree was
created there and a no-fast-forward merge of A produced
4921b96ac389cde2d2e94b73e5e5cd69eae77c6b. **No merge conflicts occurred.** Neither
developer's branch was rewritten. Existing integration/main/release/astraroll-v1
were not changed. Runtime fixes and tests were committed separately, not described
as conflict resolutions.

The final integrated code freeze is **9af9abdf554ac3d58a6e37b224891b66566d85cb**,
source-tree hash 15cd2ef950f89344b608e8e7c21b56a118fd8b7d. It was measured directly
with a fresh read-only audit/API/SQL/latency run. The earlier tuned runtime candidate
e829c87b23dd669c2a6cee9c6756c73f7b952cdf also passed before/after real live updates.
The only source differences afterward normalize/assert Redis IDs in fault-injection
helpers; HTTP API, router, domain models, provisioning and production workers are
identical. The delivery commit adds documentation/evidence only. Deployment uses
V2 mode and the V2 control database.

## Actual PostgreSQL inspection

Existing PostgreSQL 16.6 at port 55433 was never reset, stopped, recreated or reseeded
by this integration task. Data directory remains D:/cyberforge/.runtime/release-pgdata;
postmaster start time is 2026-10-31T15:07:52.841385Z. Redis is 7.4.3; Python 3.12.10.

Control DB: **v2bench_control_78b1ee4a**. All 100 intended registry entries are ACTIVE,
map to distinct existing v2bench_78b1ee4a_ tenant databases, and physically contain
the matching tenant UUID. Other pre-existing databases are retained and excluded
from this intended benchmark set. All names/sizes are listed in
[database inventory](v2-release-databases.md).

| Verified final property | Actual result |
| --- | ---: |
| Dedicated V2 tenant DBs | 100 |
| Orders per tenant | 10,000 |
| Total V2 orders | 1,000,000 |
| Total canonical projections | 1,000,000 |
| Newly generated historical orders | 0 |
| V1 archive orders | 10,000,000 |
| V1 tenants / orders each | 100 / 100,000 |
| V1 outbox events | 17,333,300 |
| V1 seed checkpoint total | 10,000,000 |
| V2 target storage | 4,214,226,092 bytes |
| V1 benchmark schema storage | 23,896,989,696 bytes |
| Whole source DB storage, including other retained schemas | 27,831,177,699 bytes |

Frozen source rows were reused; no benchmark dataset was regenerated. Live checks
changed existing placed orders legally without adding orders. The two release runs
added four events; Developer A's preceding two live events remain preserved too.
Financial and checkpoint evidence distinguish original migration from these writes.
V1 is archival; switching deployment_mode to shared would not safely roll back V2
writes. It is not a 10M V2 benchmark.

### Tables, columns, indexes and revisions

All 100 targets have Alembic **0005_tenant_access_paths**, one graph above unchanged
0004_coverage_precision. Control metadata is separately owned by ControlBase and
excluded from domain autogeneration; no control Alembic revision is invented.
Each target has one subset_copy_checkpoint with copied=10000 and phase=verified.
The legacy control_tenant_migration_checkpoints table has zero rows: this cohort's
atomic checkpoints are deliberately tenant-local, not missing legacy checkpoints.

All 100 column/default/index/constraint fingerprints match:
3da85f82fc1c67b0d909d6cbcc3ec4da. This is a schema-comparison fingerprint, not an
authentication signature. Full columns, secondary indexes, representative primary/
foreign/check/unique constraints and all database fingerprints are in the linked JSON.

Actual 20 target tables:

```
alembic_version                 tenants
orders                          outbox_events
order_projections               processed_events
event_quarantine                tenant_processing_state
hourly_summary_aggregates       daily_summary_aggregates
hourly_status_aggregates        daily_status_aggregates
hourly_product_aggregates       daily_product_aggregates
bootstrap_jobs                  aggregate_coverage
bootstrap_orders                rebuild_archive
operational_counters            subset_copy_checkpoint
```

Orders use UUID IDs/tenant/product keys, NUMERIC(18,2) amounts, currency/status,
BIGINT versions and timestamptz creation/update fields. Key indexes include
orders(tenant_id,created_at,id), orders(tenant_id,id), unique outbox(order_id,
order_version), outbox(tenant_id,order_id,order_version) and the unpublished-outbox
partial index. Projections use tenant_id/order_id keys, last_applied_version and
canonical_snapshot JSONB. Processed-event UUID keys provide durable deduplication.
All six rollups use currency/tenant/time buckets and NUMERIC(38,2) sales; status and
product tables add their respective dimension. Coverage references a real ready
bootstrap job and the immutable creation-cohort metric contract.

## Real business samples

Representative DB: v2bench_78b1ee4a_0457061cdb4d553e9b3cba32ec9e5f57.
Window: [2026-10-01T00:00:00Z, 2026-10-08T00:00:00Z). Samples below are from the
final database inspection after the legal updates, not copied example CLI output.

| Currency | Orders | Gross face amount (all statuses) | Shipped recognized sales |
| --- | ---: | ---: | ---: |
| EUR | 4,996 | 2,498,745.10 | 1,328,922.32 |
| USD | 5,004 | 2,506,091.30 | 1,325,416.36 |

Currencies are not added together. Gross face amount is not the sales metric.
USD status counts: cancelled **1,030**, placed **1,306**, shipped **2,668**. Placed
and cancelled recognized sales are exactly zero; shipped sales are **1,325,416.36**.

| UTC daily bucket | USD orders | USD recognized sales |
| --- | ---: | ---: |
| 2026-10-01 | 4,352 | 1,010,851.69 |
| 2026-10-02 | 652 | 314,564.67 |

First three UTC hourly buckets on Oct 1: 00:00 has 178 orders / 2,010.81 sales;
01:00 has 192 / 5,714.22; 02:00 has 161 / 7,408.27. API trends also return the
required zero buckets for the rest of the requested covered window.

| Top USD product UUID | Orders | Sales |
| --- | ---: | ---: |
| 5a67d983-856d-5cb1-6575-9ba0160b7748 | 271 | 95,774.48 |
| 4a5853b2-390b-073a-2515-e9a64ff6e13f | 250 | 91,193.65 |
| c82e6d2b-ccac-8155-8bd0-681a3eb333a8 | 263 | 90,737.43 |

Both coverage rows are ready, tied to job de273dc6-9c1a-4982-b4ff-ca9aa1f6cc02,
verified at 2026-10-31T17:26:38.558825Z, with source counts EUR=4996 and USD=5004.
The current representative revision is 7, last aggregate application is
2026-10-31T19:33:35.261638Z, and its genuine last Redis ID is 1793475215063-0.
This is not a global stream watermark or a fabricated current-time checkpoint.

## SQL/API and end-to-end gates

All 100 tenants have exact current-order/canonical-projection parity and matching
versions/timestamps. All **600 rollup comparisons** (six tables per tenant) match
raw-order EXCEPT ALL oracles. Actual durable coverage is ready for both currencies
and summary/status/product at hourly/daily grains. All four real authenticated HTTP
response shapes match direct SQL before and after live changes across all tenants
and both currencies; exact zero buckets and deterministic product ordering pass.

Passed identity/routing checks include invalid/expired keys (401), cross-tenant
analytics and producer reads (403), warmed cached-pool inactive status (503), real
disposable-DB unavailability, and a cold registry outage (sanitized 503). No password
login/identity-provider integration is claimed. The trusted server-issued bearer
scheme and frontend proxy/configuration are specified in the frontend contract.

Real V2 worker subprocesses, not fake route readers, performed scoped outbox relay,
consumer application, duplicate delivery, commit-before-ACK recovery and timed clean
shutdown. Every worker exited 0. Final ordinary visibility was **4,125.273 ms** and
recovery visibility **5,175.573 ms**, including Python worker startup, PATCH/relay/
consume/GET. Recovery used zero-idle reclaim deliberately. The timed two-second
consumer exited in 4.224 seconds including startup/cleanup. These are controlled
functional probes, not long-lived worker freshness or disaster-recovery SLAs.

The fault-injection caller initially passed bytes to a string-ID method, causing a
bytea-style textual processing ID. Probe callers now decode IDs and assert exact
stored IDs. One compare-and-set metadata correction was made only after proving the
decoded ID matched the actual Redis event, outbox row and processed-event receipt.
Revision, timestamp, financial data and row counts were unchanged. The correction
record is published; no checkpoint was invented and no undelivered event was marked
processed to hide a backlog.

## Performance: regression retained, final gate passes

One Uvicorn process, four users, 200 round-robin samples per endpoint per pass,
100 x 10k tenants, USD timing / both-currency correctness, same Oct 1–8 window.
Ten active pools, one connection each, zero overflow. No test/seed/migration workload
ran concurrently. HTTP clients/server share this Windows host.

First integrated candidate 98cf746 **failed** p95: summary/trends/products/status
223.313/231.432/245.698/233.351 ms before live writes, and
254.777/223.277/258.808/225.978 ms afterward. This regression is retained in evidence,
not silently replaced by A's earlier 199.957-ms summary pass.

Direct diagnostics measured cold engine connections around 40–188 ms versus warm
summary reads mostly 21–40 ms. The final manager retains bounded initialized dialect/
statement metadata (128 dormant engines), while disposing idle connections. Late
connection check-ins for passive engines are invalidated. Active pool budget remains
ten; statuses are rechecked, and dormant engines are reused only for matching resolved
URLs. Registry bindings are frozen for this release; hot database remapping is not
a frontend API. No query results or freshness are cached. Tests cover inactive
rejection, dormant URL mismatch, bounded cache size, empty
dormant pools, late returns and new provisioning with the actual migration revision.

Memory headroom also recovered after the owner closed applications: interference
snapshots show commit headroom 1.62 GB in the failed run versus 17.28 GB in the final
run, RAM 2.64 versus 4.89 GB. **The improvement cannot be attributed solely to code.**
This is a same-profile observational retest with a documented environment change.

Final e829c87 timings (milliseconds):

| Endpoint | Pre p50 | Pre p95 | Pre p99 | Post-live p50 | Post-live p95 | Post-live p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| summary | 87.619 | 181.403 | 195.826 | 81.005 | 102.872 | 186.241 |
| trends | 65.050 | 160.500 | 181.076 | 58.396 | 76.787 | 165.151 |
| top-products | 75.623 | 89.718 | 182.522 | 74.984 | 143.987 | 187.843 |
| status-breakdown | 64.287 | 87.797 | 169.114 | 60.476 | 91.834 | 173.417 |

All eight p95 values meet **under 200 ms**. Throughput was 42.34–57.75 requests/s
before and 47.41–63.93 afterward. Sorted index floor(n*p/100), clamped, is the
percentile method. Real reader/oracle SQL plans and raw latency samples are published.
This does not certify a sustained production SLA or arbitrary workload/concurrency.

### Final integrated-code confirmation (9af9abd)

A final read-only run on the exact frozen code, with the same 100 tenants/four users/
200 samples per endpoint, confirmed:

| Endpoint | p50 ms | p95 ms | p99 ms | Requests/s |
| --- | ---: | ---: | ---: | ---: |
| summary | 82.996 | 183.035 | 214.687 | 44.01 |
| trends | 60.146 | 104.521 | 173.239 | 60.43 |
| top-products | 76.447 | 94.744 | 186.215 | 48.70 |
| status-breakdown | 62.871 | 156.966 | 185.605 | 55.44 |

All four final p95 gates pass; summary p99 exceeds 200 ms and is retained. No further
order updates, backlog drain or dataset regeneration occurred in this confirmation.
All 100 current target audits/API oracles passed again, including the corrected
string checkpoint. Final normal-source p95 was 1.3447 ms idle / 1.1141 ms loaded
(-17.15%), 2,000 samples per phase; the same warm-query/short-profile limitations
apply. See [frozen-code measurements](release-evidence/frozen-9af9abd.json).

Independent-process normal-source probe: 2,000 reads/phase at 100 requests/s,
20 seconds each, with four dashboard clients at 20 total requests/s. Normal p95
was **1.3445 ms idle / 1.1043 ms loaded (-17.87%)**. No positive slowdown was observed
for this warm indexed point lookup; this does not prove analytics accelerates PG or
certify all application SQL. Mixed dashboard p95/p99 were 188.580/256.966 ms over
400 requests. Cluster snapshots observed 22 clients; no complete peak-connection,
CPU or memory certification is claimed. Server limit stays 100, three reserved.

## Historical backlog assessment and retention

Initial 1,716,371 unpublished events were copied historical transport work, not
missing financial contributions. Bootstrap already materialized those orders; every
pending event is projection/version-covered. Consumer dedup and monotonic projection
versions prevent reapplication. Two controlled 100-event samples from one pending
tenant were genuinely published/consumed/ACKed, with unchanged order count and
revision and exact six-table parity. No global drain was started.

**1,716,171 remain pending and projection-covered across 99 targets.** They are not
silently marked published, not database-quarantined, and not resolved. Operationally
they are paused by explicit relay scope: relay requires --tenant-id or deliberate
--all-tenants. Compose needs V2_RELAY_TENANT_ID; blank configuration fails closed.
Do not enable all-tenants continuous relay before reviewing catchup capacity.

Safe plan: retain outbox payloads, processed IDs, projections and bootstrap/copy
checkpoints; take verified backups and test restoration independently before any
retention deletion. Drain one chosen tenant in bounded 100-event batches with the
matching V2 consumer; check ACK/pending receipts, source lag, revision and raw/rollup
oracles between bounded windows. Stop on unavailable DB, memory/disk pressure,
quarantine or parity drift. Never infer per-order completion from a tenant revision,
automatically mark new live events processed, or publish the V1 archive's unselected
9M orders into the V2 stream. Validation streams/IDs are retained in the evidence.
Full historical throughput and global steady-state freshness remain unmeasured.

## Tests, fixes and frozen interfaces

**322 tests passed, zero skipped, one existing Starlette/httpx deprecation warning,
89.74 seconds** on a separately initialized disposable PG instance at 55434 and
Redis at 56381. Destructive fixtures were explicitly authorized there only. Those
services are stopped; data files remain ignored for inspection. A prior run had
321 passes plus a host commit-guard failure; the failing checkpoint test passed on
retry, then the whole suite passed after headroom recovered. No guard/financial/
authorization rule was relaxed. The disposable test cache was 32 MiB; protected PG
remains unchanged. A final focused recovery test also passed after probe-ID decoding.

Additional reproduced fixes: cold control failures/shutdown now fail safely; V2
tenant POST returns a real created_at and 201; fresh provisioning applies/stamps
the actual Alembic head (serialized within a process for Alembic proxy safety);
admin-only diagnostics route SQL to the selected tenant. A normal key still gets
403 for admin diagnostics. Existing data were not reprovisioned to test these fixes.

Ruff/format, both Compose configurations, one-head validation and actual tenant
alembic check pass. The [frontend contract](v2-frontend-contract.md) and
[OpenAPI](v2-openapi.json) freeze implemented schemas, auth, pagination, date/currency
semantics, errors and unsupported flows. Use a same-origin/dev proxy; no unrestricted
CORS or browser-side database access is added. Current benchmark coverage is Oct
1–8 UTC; do not request today's uncovered dates or manufacture zeros/coverage.

## Launch and review

Use DEPLOYMENT_MODE=database_per_tenant, CONTROL_DATABASE_URL pointing at the V2
control DB, DATABASE_URL at that same control DB for serving, and scoped expiring
API-key digest configuration. See .env.example and compose.v2.yaml. Native serving:

```powershell
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
$env:DEPLOYMENT_MODE='database_per_tenant'
$env:DATABASE_URL='postgresql+psycopg://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a'
$env:CONTROL_DATABASE_URL=$env:DATABASE_URL
$env:REDIS_URL='redis://127.0.0.1:56380/0'
$env:MAX_ACTIVE_TENANT_POOLS='10'
$env:TENANT_POOL_SIZE='1'
$env:TENANT_POOL_MAX_OVERFLOW='0'
python -m astraroll.analytics.issue_key --subject dashboard --tenant 0457061c-db4d-553e-9b3c-ba32ec9e5f57
# Configure the emitted trusted digest record locally; never commit the bearer key.
uvicorn astraroll.api:app --host 127.0.0.1 --port 8000
# Separate processes, scoped to the reviewed/drained live pilot:
python -m astraroll.v2.workers relay --tenant-id 0457061c-db4d-553e-9b3c-ba32ec9e5f57
python -m astraroll.v2.workers consumer
```

Do not run these commands in shared mode or point them at the V1 archive. For Compose,
set the reviewed V2_RELAY_TENANT_ID and use both compose.yaml and compose.v2.yaml.
Compose was configuration-validated; containers did not run the portable benchmark.
Benchmark API/worker subprocesses exited; PostgreSQL/Redis and the verified V2 data
remain available. Keys are not retained in published reports.

Full evidence: [PostgreSQL inspection](release-evidence/postgresql-inspection.json),
[all database names/sizes](v2-release-databases.md),
[schema fingerprints/constraints](release-evidence/schema-consistency.json),
[final measurements](release-evidence/measured-e829c87.json),
[frozen-code confirmation](release-evidence/frozen-9af9abd.json),
[failed regression](release-evidence/regression-98cf746.json),
[source preservation](release-evidence/archive-preservation.json),
[checkpoint correction](release-evidence/checkpoint-encoding-correction.json).

Final readiness is scoped: dashboard and pilot live integration can proceed under
the published contracts. CSV/import tracking/assistant endpoints, broad catchup,
identity-provider login and production security/freshness certification remain
unimplemented or unverified. No main merge or V1 release change was performed.
