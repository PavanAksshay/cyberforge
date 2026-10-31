# AstraRoll release integration audit

Audit performed 2026-10-31 before application changes.

## Verified history and scope

- Clean starting checkout: dev-a `09c5ddcaf50c8324b265decc2a1776e4c84760bd`.
- origin/dev-b: `790907514a72db109cf93ccb46c4bce926712330`.
- origin/integration and main: `f428335aa89718b261e97b3aa8d25cad330313cd`.
- Shared A/B ancestor: `f471e4bfbd58091cb553d82f425e3ee1198bf945`.
- Candidate branch created from origin/integration: `release/astraroll-v1`.
- Origin verified as https://github.com/PavanAksshay/cyberforge.
- No applicable repository/parent AGENTS.md was found. No user edits were present.
- The owner now authorizes coordinated changes across both former ownership areas.

Normal merges will preserve both histories. Main, integration, dev-a and dev-b
will not be reset, force-pushed, deleted or merged into without further scope.

## Existing migration graph

A: base -> 0001_producer -> 0002_producer_contract.
B: base -> 0001_producer -> 0002_producer_contract -> 0003_aggregation (one head).
New requirements will use forward revisions after 0003. Existing revisions are
preserved from the merged histories, not rewritten to hide semantic changes.

## Existing feature inventory

A has FastAPI tenant/order endpoints, Decimal/UTC/UUID schemas, atomic outbox,
Redis relay, seeder, four analytics routes, optional PostgreSQL bridge, expiring
digest-based tenant API keys, coverage DTO and isolated combined-test tooling.
B has six durable hourly/daily summary/status/product tables, projections,
deduplication, canonical-order reconciliation, quarantine, Redis consumer/replay,
XAUTOCLAIM cursor recovery and projection-corruption protection.

Overlapping merge-sensitive files include README, Compose, pyproject, Alembic env,
producer tests and shared documentation. A contributes security/query contracts;
B contributes aggregate registration, consumer services/scripts and migration 0003.
Resolve each logically, preserving both contributions and every test.

## Conflicting contracts and P0 defects

1. B currently advertises created_at_current_state_v1 and placed+shipped sales;
   the owner mandates created_at_shipped_sales_v2 and shipped-only sales.
2. B currently includes zero-sales products and sorts sales/UUID; the owner
   mandates positive sales and sales DESC/count DESC/UUID ASC before LIMIT.
3. B duplicates A's query DTO/protocol and exception types. Use one shared contract.
4. No durable bootstrap/retention/coverage certificate exists. First applied
   revision or timestamp is not coverage. Current A production reads fail closed.
5. Producer routes and tenant creation remain unauthenticated. Extend real key
   verification and provide an explicit local admin provisioning path.
6. Historical rollups may contain older semantics. Rebuild in staging, validate
   a consistent source boundary, archive prior derived records, then activate.
7. Aggregate NUMERIC(18,2) can overflow while individual order amounts are valid.
8. Strict decimal parsing, canonical corruption rejection and claim cursor fixes
   exist; preserve and extend their regression coverage.

## Test/runtime baseline

A verified 169 passing tests, two combined-only module skips. Previous combined
latest snapshot verified 245 passing with seven contract-blocked read scenarios.
The release must remove those real-read blocks, not count 503 as a successful read.
Existing B recovery tests require PostgreSQL 16+ and Redis 7+.

Portable PostgreSQL 16.6 and Redis 7.4.3 are available locally. Docker Desktop
engine probes previously timed out; Compose remains deployment specification but
is not a prerequisite for live tests. Approximately 180 GB is free on D:, versus
7.4 GB on C:. Large benchmark data must use a new, isolated D: runtime directory.
No persistent existing cluster or Redis data will be reset/deleted for testing.

## Ordered implementation and verification

P0: merge histories; unify contracts; add forward coverage/precision migration;
implement staged, resumable source bootstrap with source/consumer cutover fences;
secure all tenant routes; connect the durable provider; execute real full-pipeline,
oracle, historical upgrade, interruption/resume and concurrent-ingestion tests.

P1: operational metrics, consistent-boundary reconciliation/repair, bounded batch
workload generation, latency/throughput/backlog measurements and scale limits.
Only claim benchmark scales and targets supported by stored outputs.

P2: reproducible CLI/Swagger demo. Fairness/backpressure/cache/dashboard additions
require stable P0 behavior and measurements that justify their added complexity.

Release risks: bootstrap/live-write races; accidentally exposing old metric data;
source-to-apply lag mistaken for a completeness watermark; unauthorized producer
writes; destructive rebuilds; flaky recovery assumptions; optimistic benchmark
claims; hardware/disk limits at 10M. Address with explicit transactional fences,
archived derived state, fail-closed coverage, real credentials and honest reports.
