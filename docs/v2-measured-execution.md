# V2 actual execution report

Status: migration running; no completed 1M or 100-database latency claim yet.

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
ordinary visibility and injected recovery results will be appended after execution.
