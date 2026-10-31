# AstraRoll V1 - Developer A

Python 3.12+, FastAPI, SQLAlchemy/Alembic, PostgreSQL 16 and Redis 7.
Producer infrastructure, tenant/order API, atomic transactional outbox, relay,
and additive dataset seeder for a two-person hackathon.

## Layout and handoff

```text
src/astraroll/
  config.py, db.py           settings and session factories
  models.py, schemas.py      producer DB and request/event contracts
  orders.py                 transactional producer domain services
  api.py                    FastAPI routes
  relay.py                  PostgreSQL outbox -> Redis Streams
  seed.py                   bounded, repeatable synthetic input
  analytics/                query contracts, authorization boundary, reader protocol, routes
alembic/versions/0001_producer.py  # preserved history
alembic/versions/0002_producer_contract.py
tests/                      validation + PostgreSQL/Redis integration tests
docs/contracts.md           Developer B handoff contract
compose.yaml, Dockerfile, requirements.lock
```

Read [the database/event contract](docs/contracts.md) before implementing B's
consumers, rollups, corrections or caching. Those modules are not included here.
Developer A services have no dependency on B's modules.

## Analytics query APIs

The API exposes `GET /analytics/summary`, `/analytics/trends`,
`/analytics/top-products`, and `/analytics/status-breakdown`. All require explicit
tenant UUID, currency and an exact UTC `[start, end)` range. Trends support day
buckets and hour buckets when the installed reader has hourly coverage. Product
limits are 1..10. Amounts are Decimal strings; counts include cancelled orders,
while sales include placed/shipped orders only, attributed to creation time and
the latest accepted amount/product state.

Read [the analytics API and Developer B handoff](docs/analytics.md) for exact
schemas, date/grain restrictions, empty results, metric semantics and persistence
requirements. No B aggregate models or migrations are duplicated. Physical table
fields, currency-safe grouping and stored coverage remain unresolved, so no real
PostgreSQL aggregate reader is connected and no raw-order scan is used as fallback.
A late fetch found B's pure aggregation contracts at `dev-b@525b79d`: their
contribution semantics match, but currency is absent from the current key and
snapshot, and durable table/readiness/checkpoint contracts are still unpublished.

**Authentication is not integrated and security is not complete.** Analytics
requires a verified principal through a fail-closed dependency: missing bearer
credentials return 401; an unverified bearer returns 503 while the verifier is
absent. A tenant UUID alone never authenticates. After a real verifier is installed,
unauthorized tenant membership returns 403, and an absent/unready aggregate reader
returns 503. The successful test fake is never installed in the application.

```powershell
# Contract/route tests need neither PostgreSQL nor Redis:
./.venv/Scripts/python -m pytest tests/test_analytics.py -q
# Inspect all four request/response contracts in http://localhost:8000/docs
```

This phase passed **143 tests**: 83 analytics contract/route tests with SQL access
trapped, plus all 60 producer tests with real portable PostgreSQL 16.6/Redis
5.0.14.1 integration enabled. Ruff, formatting and Compose config checks passed.
Real aggregate persistence and real identity verification remain untested because
their integrations are not present. Docker containers were not run in this phase.

## Frozen contract and upgrades

New orders require `product_id`, `amount` and `currency`, and start as `placed`.
Only placed orders can be edited or transition to `shipped` / `cancelled`; both
are terminal. New events use **schema_version 2** on **astraroll:orders:v1**, field
**event**. Schema-1 events remain historical and need explicit handling by B.
See [the exact event fixture](docs/order-event-v2.json).

Fresh databases upgrade through `0001_producer` then `0002_producer_contract`.
For existing databases, stop API/relay/consumers together and follow the
[authoritative backfill policy](docs/contracts.md#migration-and-authoritative-backfill-policy)
before starting the new producer. Missing products and unresolved legacy statuses
block migration safely; no default products or shipping decisions are inferred.
Revision 0002 is forward-only, and revision 0001 is unchanged.

```powershell
docker compose stop api relay
docker compose build
docker compose run --rm migrate
# Resume only after migration and Developer B's baseline/version coordination:
docker compose up -d api relay
```

## Run with Docker Compose

Start Docker Desktop (Linux containers). From the repository, in PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs migrate api relay
```

Copy `.env` only on first setup; do not overwrite an existing configuration.
Compose runs migrations before starting API/relay. PostgreSQL and Redis have
health checks and persistent volumes. No host Python is needed for containers.
API: http://localhost:8000/docs (interactive OpenAPI).

```powershell
$tenant = Invoke-RestMethod http://localhost:8000/tenants -Method Post -ContentType 'application/json' -Body '{"name":"Hackathon"}'
$url = "http://localhost:8000/tenants/$($tenant.id)/orders"
$order = Invoke-RestMethod $url -Method Post -ContentType 'application/json' -Body '{"product_id":"47b53ec8-c26c-45d3-8503-76f809b55952","amount":"12.50","currency":"USD"}'
Invoke-RestMethod "$url/$($order.id)" -Method Patch -ContentType 'application/json' -Body '{"expected_version":1,"amount":"19.99"}'
Invoke-RestMethod "$url/$($order.id)/cancel" -Method Post -ContentType 'application/json' -Body '{"expected_version":2}'
# Shipment is an alternative to cancellation: PATCH {"expected_version":1,"status":"shipped"}
# on a different placed order. A shipped order cannot subsequently be cancelled.
docker compose exec redis redis-cli XRANGE astraroll:orders:v1 - + COUNT 5
```

## Tests and verification

```powershell
docker compose --profile test run --build --rm test
```

Tests create a unique `test_<uuid>` PostgreSQL schema, apply Alembic migrations
there, and publish only to unique `test:astraroll:<uuid>` Redis streams. Test
artifacts are retained for inspection; tests never truncate shared tables, flush
Redis, remove volumes, or downgrade a valuable database. Migration tests also verify
that revision 0002 safely refuses downgrade inside a unique test schema. Coordinate
explicit cleanup when needed. The normal relay cannot see test schemas or their outboxes.

Integration checks cover migration/model parity, API lifecycle and tenant
isolation, input and DB constraints, concurrent version conflicts, atomic rollback
including outbox insert failure, relay retries/ambiguous publication duplicates,
locked-row skipping, and seeder repeatability. Without `RUN_INTEGRATION=1`, pytest
explicitly skips integration tests rather than substituting SQLite.

Verified on 2026-10-31 with Python 3.12.10, PostgreSQL 16.6 and portable Redis
5.0.14.1: **60 tests passed**, including fresh/historical migration tests, full
snapshots, terminal transitions, invalid mutations, retry identity and mixed event
versions. A CLI smoke check migrated a fresh database, seeded 20 orders (zero new
orders on rerun), and relayed 32 schema-2 events to the exact frozen stream/field.
Ruff and Compose configuration validation passed. Container startup with the pinned Redis 7 image remains unverified on
this host because Docker Desktop fails during initialization of its inference
socket; the fallback processes used separate localhost ports and test data.

## Local Python development

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.lock
./.venv/Scripts/python -m pip install --no-deps -e .
docker compose up -d postgres redis
./.venv/Scripts/alembic upgrade head
./.venv/Scripts/python -m uvicorn astraroll.api:app --reload
# In another terminal:
./.venv/Scripts/astraroll-relay
# Run the full test suite:
$env:RUN_INTEGRATION = '1'
./.venv/Scripts/python -m pytest -q
./.venv/Scripts/ruff check .
./.venv/Scripts/ruff format --check .
```

On Linux/macOS use `.venv/bin/` instead of `.venv/Scripts/`, `cp` to copy the env
template, and `RUN_INTEGRATION=1 .venv/bin/python -m pytest -q`.
`requirements.lock` pins tested dependencies; Docker installs it before the local
package. `uvloop` is intentionally not required for portability.

## Seed data

```powershell
docker compose run --rm api astraroll-seed --tenants 3 --orders-per-tenant 1000 --seed 42 --batch-size 500
# Or locally:
./.venv/Scripts/astraroll-seed --tenants 3 --orders-per-tenant 1000 --seed 42
```

The seeder writes to PostgreSQL using production domain/outbox services. It
produces placed/shipped/cancelled orders plus amount/product changes for corrections.
Each tenant has five deterministic synthetic product UUIDs. USD and EUR orders
remain separate; real historical product IDs are never generated by migration.
Amounts, order IDs, tenant IDs, and original creation timestamps are stable for
a seed. Event IDs and mutation timestamps are runtime values. Historical creation
dates span 2025; update events occur at seeding time. Re-running skips existing
deterministic order IDs and inserts zero duplicates; larger counts extend the
dataset. It never deletes or overwrites existing orders. Per-tenant advisory locks
serialize seed runs; bounded batches limit memory and transaction size. No
generated bulk files are committed.

## Operations and limits

`docker compose stop` stops services without deleting volumes.
`docker compose run --rm relay astraroll-relay --once` publishes one batch.
The relay retries failures without marking events delivered and supports graceful
SIGINT/SIGTERM shutdown. Do not trim the stream, reset markers, downgrade migrations,
or remove volumes without approval and coordination with Developer B.

This V1 is a localhost hackathon service with no authentication. `.env.example`
contains development placeholders; `.env`, virtual environments and bulk datasets
are ignored. POST creates are not request-idempotent. Multiple relays can publish
out of order, and duplicates are expected: B must implement the idempotency/version
rules in the contract. Redis storage loss requires coordinated outbox replay.
There is no Kafka, Kubernetes or ML component.
