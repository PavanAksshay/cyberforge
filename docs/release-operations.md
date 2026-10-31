# Release operations and guarantees

## Bootstrap, interruption and repair

Run `python -m astraroll.admin bootstrap --tenant UUID --currency USD --currency EUR
--start 2026-10-01T00:00:00Z --end 2026-10-08T00:00:00Z --batch-size 5000` with
trusted local database access. Inspect `python -m astraroll.admin coverage --tenant UUID`.
Resume with identical arguments plus `--resume JOB_UUID`. A second building job for
the same tenant is rejected. An empty tenant must also be explicitly bootstrapped.

`bootstrap_jobs` persists enumeration cursor, copied count, status, semantics,
window and last error. `bootstrap_orders` holds bounded-batch source snapshots.
All existing tenant coverage becomes building/unavailable before enumeration.
UUID enumeration is **not** treated as a source watermark. Final activation takes
the same transaction-scoped tenant advisory lock used by order writes, SQL workload
seeding and consumers. It repairs inserts/changes behind the cursor, checks for
deleted source orders, then rebuilds projections and all six rollups. Catchup is
bounded (100,000 changed rows by default); exceeding it rolls activation back.

Old derived tenant rows are archived in `rebuild_archive` in the same transaction
before replacement. Raw orders, outbox events and processed-event IDs are never
deleted by bootstrap. Six exact PostgreSQL source-vs-rollup comparisons run while
the source/consumer fence is held. Only successful validation atomically marks the
job/coverage ready and advances a real revision. A bootstrap does not invent a
last-applied timestamp. Failure rolls back activation and leaves the durable job
resumable; completed staging remains. No automatic data reset exists.

The fence is an application protocol. Direct SQL writers must acquire it or be
stopped during bootstrap/reconciliation. Existing processes running older code do
not participate: upgrade API, seeders and consumers together before rebuilding.
Staging uses bounded memory, but final SQL grouping, oracle validation and archived
replacement scale with tenant size and block that tenant's writers. Inspect lock
waits and schedule larger cutovers deliberately. Other tenants can proceed.

Coverage includes currency, UTC retained window, semantics, job/generation,
verification time and explicit status. Both hour/day and all three dimensions are
advertised after validation. First-event processing never establishes coverage.
Missing/building/invalid/stale coverage returns 503. Ready out-of-window requests
return 422. Valid empty windows return zeros. The default verification TTL is 24h.
Coverage is a validated baseline plus transactional maintenance, **not** a guarantee
that every newly committed source event has already been processed.

## Reconciliation

`python -m astraroll.admin reconcile --tenant UUID` holds the tenant fence and
checks source/projection versions first. If lagging, it reports `compared:false`
and does not compare an ahead-of-source oracle with lagging rollups. At a matched
boundary, all six source/group comparisons must match. A successful comparison
refreshes already-ready, compatible coverage. Drift invalidates coverage and increments
a durable counter; it never silently repairs data. Explicit archived bootstrap is
the repair path, including for corrupted projection snapshots. Review unexplained
drift before rebuilding. Source deletions are outside the producer contract.

## Monitoring

Authenticated admins can request `GET /ops/metrics?tenant_id=UUID`:

- Tenant unpublished outbox count and oldest unpublished event age.
- Tenant source versions not yet matched by projections and oldest such source
  update age. This is a source-state lag diagnostic, not an applied stream watermark.
- Real revision, last successful apply time and last observed applied stream ID.
- Processed-event records in the last minute (includes stale-version dedup records;
  it is not the rate of applied aggregate mutations).
- Durable recovery/replay/worker-loop-error/reconciliation counters and quarantine count.
- Redis group lag/PEL information, stream length and memory when Redis is available.
- Coverage status/window/semantics/verified time.
- Process-local API p50/p95/p99 over at most 2,048 recent samples per route template.

The stream ID is diagnostic only; parallel processing prevents treating it as a
contiguous completed checkpoint. Live benchmark commit-to-drain latency is measured
separately. No lag timestamp is invented for empty tenants. Operational source
joins are explicit admin requests, never normal analytics reads. Queries have a
5-second statement timeout; use periodic sampling rather than high-frequency polling.
Redis unavailability is reported independently. API latency resets on restart and
is not aggregated across processes. Persistent counters measure completed actions;
a counter write that cannot reach PostgreSQL cannot count that outage reliably.

## Recovery and backpressure

Consumer commit includes rollups, canonical projection, dedup record and revision.
XACK follows commit or durable quarantine. Duplicate/stale versions preserve totals.
XAUTOCLAIM advances its cursor and recovers abandoned PEL work. An unknown/corrupt
existing canonical snapshot aborts processing; it is never treated as a new order.
Replay uses bounded keyset pages ordered by (occurred_at, UUID), retaining at-least-once
delivery. Interrupted replay can safely restart; no source/outbox history is removed.

Consumer batches are validated 1..10,000; default 50. Relay batches are bounded by
configuration and wait for database/Redis operations, naturally limiting admission.
These controls do not constitute fair scheduling or adaptive backpressure. A shared
stream does not guarantee tenant fairness. No fairness or cache improvement is
claimed without an actual before/after experiment.

## Security and release boundaries

All producer/analytics routes require real expiring bearer verification and trusted
membership. Tenant creation and operations require a trusted admin record. Bootstrap,
reconciliation, seeding, replay and key issuance are local operator commands requiring
OS/database/Redis access. Health and OpenAPI are public; health/ready checks database
connectivity, not analytics readiness. Invalid/missing identity fails before tenant SQL.
Registry revocation/rotation takes effect after process reload. There is no PostgreSQL
RLS, request-rate limiter, external identity lifecycle or TLS terminator in this V1.

The forward 0004 migration widens aggregate NUMERIC to (38,2), registers coverage/
bootstrap/archive/counters and a processed-time index. It preserves old totals but
does not label them compatible; bootstrap must rebuild them. Downgrade refuses
narrowing/data deletion. Earlier migrations and both branch histories remain intact.
No main/integration merge, force push, volume removal or dataset deletion is part of
the release procedure.
