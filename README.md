# AstraRoll V1 release candidate

Python 3.12+, FastAPI, PostgreSQL 16+, Redis 7+, SQLAlchemy and Alembic.
The release combines Developer A's producer/API/outbox with Developer B's durable
consumer, projections, deduplication, recovery and six aggregate tables.

## Architecture and contracts

Authenticated order API → PostgreSQL orders + atomic outbox → Redis relay →
Redis Stream → consumer → transactional projections/rollups → authenticated analytics.
A resumable source bootstrap establishes durable coverage; normal analytics reads
use only aggregates, coverage and processing metadata in read-only REPEATABLE READ.

- Event schema **2**, stream **astraroll:orders:v1**, field **event**; complete
  before/after snapshots, UUIDs, exact decimal strings, UTC and monotonic versions.
- Metric **created_at_shipped_sales_v2**: count every current order once in its
  immutable creation bucket. Only shipped orders recognize sales. Placed and
  cancelled sales are zero. Repricing/reassignment before shipment is supported;
  shipped/cancelled are terminal in the producer API.
- Currency is mandatory. UTC half-open ranges `[start,end)` require exact hourly
  boundaries; daily trends require midnight. Maximum request window is 366 days.
- Top products: positive sales only, sales DESC, order count DESC, product UUID ASC,
  all before SQL LIMIT (1..10). AOV in the internal reader divides by shipped count.
- Migration chain: 0001_producer → 0002_producer_contract → 0003_aggregation →
  **0004_coverage_precision**. Earlier revisions are preserved. Aggregate money is
  NUMERIC(38,2); source money remains NUMERIC(18,2).

See [event/database contract](docs/contracts.md), [analytics](docs/analytics.md),
[release operations](docs/release-operations.md), and [initial audit](docs/integration-audit.md).

## Setup

Docker Compose is the canonical deployment specification:

```powershell
# Copy only if .env does not already exist.
Copy-Item .env.example .env
docker compose config
docker compose up --build -d
```

Compose binds services to localhost, persists PG/Redis, and runs migrations before
API, relay and consumer. Redis uses AOF/always and noeviction. Never delete volumes
to resolve a migration or semantic mismatch. Docker Desktop is not required for
native validation; use PostgreSQL 16+ and Redis 7+ with these commands:

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.lock
./.venv/Scripts/python -m pip install -e '.[dev,benchmark]'
# Set DATABASE_URL and REDIS_URL for your own running services.
./.venv/Scripts/python -m alembic upgrade head
./.venv/Scripts/python -m uvicorn astraroll.api:app --host 127.0.0.1 --port 8000
# Separate terminals using the same environment:
./.venv/Scripts/python -m astraroll.relay
./.venv/Scripts/python -m astraroll.aggregation.consumer
```

## Provisioning and authentication

All tenant, order, analytics and operational routes require verified expiring
opaque bearer keys. Tenant creation and operational diagnostics require an admin.
Health and Swagger are public. Empty/bad registry fails closed. No secret key is
committed. UUIDs and X-Tenant-ID headers do not authenticate anyone.

```powershell
# Trusted local operator with database credentials provisions the initial tenant.
./.venv/Scripts/python -m astraroll.admin tenant-create --name Demo
./.venv/Scripts/python -m astraroll.analytics.issue_key --subject demo --tenant TENANT_UUID --hours 24
# Or issue a bootstrap administrator:
./.venv/Scripts/python -m astraroll.analytics.issue_key --subject operator --admin --hours 1
```

Save only digest records in the server's `ANALYTICS_API_KEYS_JSON` array. Give the
random bearer secret to its client and reload server configuration after changes.
Remove a digest to revoke; replace both secret/digest to rotate. Pass
`Authorization: Bearer <secret>` in Swagger or HTTP clients. Configuration is loaded
once per process, so registry updates require process restart.

## Initialize analytics and demonstrate

```powershell
./.venv/Scripts/python -m astraroll.seed --tenants 2 --orders-per-tenant 100 --seed 42
# The deterministic seeder prints counts; obtain its synthetic tenant IDs from DB.
./.venv/Scripts/python -m astraroll.admin bootstrap --tenant TENANT_UUID --currency USD --currency EUR --start 2025-01-01T00:00:00Z --end 2026-01-01T00:00:00Z
./.venv/Scripts/python -m astraroll.admin coverage --tenant TENANT_UUID
./.venv/Scripts/python -m astraroll.admin reconcile --tenant TENANT_UUID
```

Use `/docs` for authenticated create, reprice/reassign, shipment, cancellation and
all four GET `/analytics/{summary,trends,top-products,status-breakdown}` routes.
Every analytics call requires tenant_id, currency, start and end. Test the complete
CLI/Swagger demo with the documented live integration test below; it exercises two
tenants/currencies, corrections, all four HTTP 200 results, denial, duplicate replay
and quarantine. Coverage is not inferred from a processing revision. Uninitialized,
partial, invalid or stale coverage returns 503; outside covered retention is 422.
A covered empty window returns legitimate zeros, empty products and zero trend buckets.

## Verification and benchmarks

```powershell
$env:RUN_INTEGRATION='1'
./.venv/Scripts/python -m pytest -ra
./.venv/Scripts/python -m pytest tests/test_analytics_integration.py -v
./.venv/Scripts/python -m ruff check .
./.venv/Scripts/python -m ruff format --check .
./.venv/Scripts/python -m alembic heads
./.venv/Scripts/python -m alembic check
docker compose config
./.venv/Scripts/python -m pip check
# New isolated schema per run; retains all generated data for inspection.
./.venv/Scripts/python -m astraroll.benchmark --orders 1000000 --tenants 10 --samples 100 --concurrency 4
./.venv/Scripts/python -m astraroll.benchmark --orders 10000000 --tenants 100 --samples 100 --concurrency 4
```

Tests create isolated schemas and uniquely named Redis streams, retained for
inspection. Without RUN_INTEGRATION=1, live tests explicitly skip. Generated datasets,
local runtimes and benchmark artifacts are gitignored; compact validated results
belong in documentation. The benchmark starts a real local Uvicorn process, creates
complete source/outbox history in bounded SQL batches, bootstraps, checks all six
rollups and all four API query shapes against SQL, and emits JSON/CSV.
It reports bootstrap and bounded live-update throughput separately. It does not
claim a full bulk event-consumption benchmark or organizer targets without evidence.

## Limits

This is a hackathon release, not production certification. No PostgreSQL RLS, TLS
termination, external identity lifecycle, distributed cache or automated retention
is provided. Database/Redis credentials and OS access remain trusted. Rebuild cutover
serializes a tenant's writers/consumer and can block a hot tenant; staging is bounded,
but final oracle/grouping cost grows with its source. The shared stream and bounded
batches do not guarantee tenant fairness. No Kafka, Kubernetes or ML is used.
