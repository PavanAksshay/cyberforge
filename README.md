# AstraRoll

**Fast, verifiable analytics for many customers. One PostgreSQL database per tenant.**

AstraRoll saves order changes transactionally, processes them through Redis Streams, maintains durable hourly/daily rollups, and serves authenticated analytics through a dashboard and REST API.

- **10,000,034 existing orders** in the audited expanded baseline.
- **100 dedicated tenant databases**, with authenticated routing.
- **All Tenants Overview** combines authorized stores into one portfolio dashboard, with ranked comparisons and individual-store drill-down.
- **Four analytics APIs** backed by **six rollup tables**.
- **1,000 API accuracy requests matched direct SQL** in the expanded-data validation.
- **All four 10M-profile p95 latencies below one second**; two below 200 ms.
- **402 backend tests passed** in the complete portfolio regression run; **18 portfolio-specific checks** passed after two additional boundary tests; **15 frontend tests passed**.

[Frontend](#frontend-snapshots) · [PRD checklist](#requirements-checklist) · [Measured results](#measured-results) · [Architecture](#how-it-works) · [Quick start](#quick-start-a-fresh-v2-demo) · [Ports](#ports-and-processes) · [Existing 100 tenants](#start-the-preserved-100-tenant-installation) · [API](#api-guide) · [Tests](#verification-and-tests)

## Problem statement

The hackathon PRD asks for counts, trends, top-10 lists and category breakdowns across many customer databases. Results must be fast, close to live, tenant-separated, correct and practical at ten-million-record scale.

- **Large repeated scans:** grouping millions of raw orders on every dashboard refresh repeats expensive work.
- **Operational interference:** reporting consumes resources needed by normal application reads and writes.
- **Freshness gap:** nightly reporting leaves new orders and status changes invisible during the day.
- **Delivery failures:** duplicate messages, worker crashes and interrupted publication can otherwise corrupt totals.
- **Tenant boundaries:** knowing a customer UUID must not grant access to that customer's data.

## Our approach, what we solved, and why it is stronger

| Problem                         | Our implementation                                               | Benefit                                                         |
| ------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------- |
| Expensive repeated reporting    | Six indexed hourly/daily rollups                                 | Reuse work already performed during updates                     |
| PostgreSQL/Redis dual writes    | Atomic order + outbox transaction                                | A committed event remains available for retry                   |
| Duplicate or stale delivery     | Durable event receipts + monotonic order versions                | Avoid repeated or backward aggregate mutations                  |
| Cross-tenant access             | Expiring bearer keys, membership checks and registry routing     | Tenant UUID possession alone grants no access                   |
| Unknown completeness            | Persisted coverage proofs                                        | Distinguish legitimate empty answers from unavailable analytics |
| Incorrect financial totals      | Decimal/NUMERIC, currency partitions and independent SQL oracles | Exact, independently checkable answers                          |
| Interrupted migration or repair | Bounded batches, persisted checkpoints and derived-data archives | Resume and inspect work while preserving source records         |
| Operational debugging           | Data Health, real heartbeat and persisted freshness              | Inspect actual pipeline state                                   |

The main idea is simple: **do a small amount of analytics work when an order changes, so dashboard reads need less work later.** Normal analytics reads use aggregates, coverage and processing metadata. Explicit SQL verification is a separate diagnostic action.

## Frontend snapshots

Real local screenshots captured on **1 November 2026** with authenticated tenant data. Values belong to the selected tenant or authorized portfolio, currency and UTC window; subsequent live writes can change them. No bearer token or provider key is included.

### All Tenants Overview

A marketplace-style portfolio across all authorized active tenant databases: combined shipped sales and order volume, shared-window trends, searchable sales ranking and individual-store drill-down. USD and EUR remain separate. [API guarantees and measured SQL parity](docs/portfolio-overview.md).

![All 100 authorized stores in one portfolio dashboard](docs/images/portfolio-overview.jpg)

### Command Center

Shipped sales, order volume, shipped-order AOV, hourly/daily trends, lifecycle breakdown, top products, measured retrieval duration and SQL verification in one view.

![Command Center with verified tenant analytics](docs/images/command-center.jpg)

<details>
<summary><strong>Advanced Analytics</strong></summary>

Detailed daily/hourly trends, ranked products, lifecycle views and CSV export, using the app's forest theme.

![Advanced Analytics with trends and exact UTC buckets](docs/images/advanced-analytics.jpg)

</details>

<details>
<summary><strong>Transactional CSV Ingestion</strong></summary>

The snapshot shows the sample loaded in the editor. Validation, commit, publication, consumption, aggregation and visibility have distinct evidence-based states.

![CSV ingestion with the 12-order sample loaded](docs/images/csv-ingestion.jpg)

</details>

<details>
<summary><strong>Data Health</strong></summary>

Selected database identity, schema revision, source/projection counts, actual worker heartbeat, unapplied events, coverage and persisted event lifecycle records.

![Data Health with actual tenant and worker metadata](docs/images/data-health.jpg)

</details>

<details>
<summary><strong>AI Analytics Assistant</strong></summary>

Groq explains data retrieved through approved analytics contracts. Responses identify their provider/model and analytics sources. Greetings and current-date/time questions use local handling; unrelated questions receive analytics guidance.

![Assistant explaining retrieved tenant analytics](docs/images/assistant.jpg)

</details>

## Requirements checklist

Mapped to the supplied three-page **Multi-Tenant Aggregation Layer PRD**.

### Mandatory functional requirements

- [x] **FR-1: Counts and totals** — `/analytics/summary` returns order count and exact shipped sales for a requested range.
- [x] **FR-2: Trends** — `/analytics/trends` returns exact hourly/daily UTC buckets, including covered zero buckets.
- [x] **FR-3: Top-N** — `/analytics/top-products` validates limits 1–10 and uses deterministic ranking.
- [x] **FR-4: Category breakdown** — `/analytics/status-breakdown` includes placed, shipped and cancelled.
- [x] **FR-5: Tenant filtering** — authentication, membership checks and database routing enforce the tenant boundary.
- [x] **FR-6: New data and updates** — creation, edits, shipment and cancellation feed the outbox/stream/consumer pipeline.
- [x] **FR-7: API and documentation** — REST endpoints, Swagger/OpenAPI, startup commands and examples are included.

### Selected bonuses and engineering additions

- [x] **B-1: Freshness information** — persisted revision/materialization metadata; distinct fetch, source and data-change clocks.
- [x] **B-2: Rebuild from source** — resumable bootstrap, SQL parity checks and archived prior derived rows.
- [x] **B-5: Monitoring page** — Data Health and the in-product architecture view expose operational evidence.
- [x] **Resource controls** — bounded per-process tenant pools; the local profile uses one connection per tenant pool, with no overflow. Budgets are process-local, rather than a distributed query quota.
- [x] **Reviewed metric contract** — finance semantics are versioned and fixed. New stored metrics follow contribution/model/migration contracts rather than arbitrary configuration expressions.
- [x] **Recovery behavior** — duplicate, stale-version, commit-before-ACK and pending-message recovery tests.
- [x] **Complete product surface** — six frontend pages extend the PRD's simple-API requirement.

The PRD permits selecting bonus items. The selected bonus deliverables are **B-1, B-2 and B-5**. Connection budgets describe our concurrency mechanism; the fixed metric contract describes our approach to metric evolution.

### Performance and scale evidence

| PRD target                                   | Recorded evidence                                                           | Interpretation                                                                                          |
| -------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Query speed at 10M: Good <1 s; Great <200 ms | Endpoint p95 range 118.06–314.90 ms                                         | All four meet Good; trends/status meet Great in this profile                                            |
| Freshness: Good <5 min; Great <30 s          | Controlled visibility probes: about 4.1 s ordinary / 5.2 s recovery         | Great-range observations for those bounded probes                                                       |
| Protect application reads/writes             | Earlier source point-lookup p95: 1.34 ms idle / 1.10 ms with dashboard load | A recorded warm-lookup interference profile; lower loaded latency is variation, not an asserted speedup |
| Match direct SQL                             | Six-rollup validation plus 1,000 API/SQL comparisons                        | Exact counts, Decimal sales, currencies and buckets matched                                             |
| Work as data grows tenfold                   | Separate measured 1M and expanded 10M profiles                              | Both sizes have recorded correctness/performance evidence; window/pool budgets are stated separately    |
| Main scale: 100 tenants                      | 100 dedicated databases; audited 10,000,034-order baseline                  | Physical inventory and per-tenant counts retained                                                       |

The PRD's small-run shape is 1M orders / 10 tenants. The recorded V2 1M profile used **100 × 10,000 orders**. The expanded baseline contained **98 × 100,000**, one tenant with **100,024**, and one with **100,010**. This distribution is recorded precisely rather than represented as a heavily skewed workload.

## Measured results

### Expanded 10M-order profile

- **Inventory:** audited 10,000,034 existing orders in 100 dedicated databases.
- **Accuracy:** 100 tenants × two currencies × five API requests = 1,000 comparisons, including hourly and daily trends.
- **Timing:** 200 authenticated HTTP samples per endpoint, concurrency four, round-robin across 100 databases, USD, full UTC coverage `[2026-10-01, 2026-11-02)`, daily timed trends.
- **Pool profile:** API four active tenant pools; worker two; one connection per pool, zero overflow.

| Endpoint         |       p50 |       p95 |       p99 | PRD query tier |
| ---------------- | --------: | --------: | --------: | -------------- |
| Summary          |  97.15 ms | 203.19 ms | 262.69 ms | Good           |
| Daily trends     |  78.23 ms | 176.67 ms | 202.48 ms | Great          |
| Top products     | 173.74 ms | 314.90 ms | 393.21 ms | Good           |
| Status breakdown |  90.56 ms | 118.06 ms | 207.25 ms | Great          |

### Earlier 1M-order profile

The earlier 100-database / 1M-order profile used four concurrent users, 200 samples per endpoint, a seven-day UTC window and ten active tenant pools. Its endpoint p95s were **183.0 ms summary, 104.5 ms trends, 94.7 ms top products and 157.0 ms status**. All four met 200 ms in that profile.

These are distinct profiles. Different windows and connection budgets matter when discussing growth; these results are not presented as a controlled, otherwise-identical speedup ratio.

### What the 10M execution proves

- Expanded records were counted and fingerprinted in the tenant databases.
- Projections and all six rollups were rebuilt and checked against existing orders.
- Every order/outbox field fingerprint matched before and after each repair.
- **10,143,992 prior derived rows** were retained in the rebuild archive.
- The 100 databases occupied **33,249,262,764 bytes / 30.966 GiB**, including retained archives, at inspection.
- The original V1 source retained **10,000,000 orders and 17,333,300 outbox events**, with an unchanged PostgreSQL postmaster start time.
- Historical bootstrap establishes the baseline. Live delivery and recovery are demonstrated separately; bootstrap time is not substituted for 10M-event streaming throughput.

The original million-order cohort was migrated from preserved source data. The later expanded baseline was already present when restoration began; restoration generated **zero additional historical orders**. Subsequent live imports can increase counts. The architecture page labels its inventory as a last-audited baseline.

Evidence: [10M report](docs/100-tenant-restoration.md), [10M machine-readable evidence](docs/100-tenant-restoration-evidence.json), [1M execution](docs/v2-measured-execution.md), [frozen 1M profile](docs/release-evidence/frozen-9af9abd.json), [live product/recovery](docs/live-product-verification.md).

## How it works

```mermaid
flowchart TD
    UI[Dashboard / CSV / Order client] --> AUTH[Bearer key and tenant membership]
    AUTH --> REG[Control-plane registry]
    REG --> DB[Tenant PostgreSQL database]
    DB --> TX[Atomic orders and outbox transaction]
    TX --> RELAY[Bounded relay]
    RELAY --> REDIS[Redis Stream]
    REDIS --> WORKER[Consumer and durable deduplication]
    WORKER --> AGG[Projections, six rollups and receipt]
    AGG --> API[Read-only analytics and coverage]
    API --> UI
```

### Three execution paths

1. **Write:** authenticate, resolve tenant, validate input, lock an existing order when mutating it, write order + complete event, commit once. API success follows the PostgreSQL commit.
2. **Process:** publish the event, route it to the tenant, reject duplicate/stale application, reverse the old contribution, apply the new contribution, update projection/receipt, commit, then ACK Redis.
3. **Read:** authenticate, resolve tenant, open a read-only `REPEATABLE READ` transaction, require valid coverage, read rollups/freshness, return JSON.

A crash after database commit but before ACK causes possible redelivery. The durable receipt prevents another aggregate mutation. Transport is **at-least-once**, with **idempotent database processing**.

### What each component does

| Component                 | Job                                                           |
| ------------------------- | ------------------------------------------------------------- |
| Python 3.12 / FastAPI     | Producer APIs, analytics routes and operational orchestration |
| Pydantic                  | Strict data and response validation                           |
| PostgreSQL 16             | Business records, durable aggregates, receipts and coverage   |
| SQLAlchemy                | Sessions, queries, locks and transaction boundaries           |
| Alembic                   | Apply and record actual versioned schema changes              |
| Redis 7 Streams           | Event delivery, consumer groups and pending-message recovery  |
| React / TypeScript / Vite | Dashboard, charts, cancellable polling and static build       |
| Groq                      | Explain approved analytics results through the assistant      |
| Docker Compose            | Repeatable PostgreSQL/Redis/application service definitions   |

Redis transports events in this implementation; it is not presented as an analytics response cache. Separate tenant databases share the server's CPU, RAM and disk. Correct routing, membership checks and connection budgets remain essential.

### Inside a tenant database

| Layer                 | Tables / purpose                                                                     |
| --------------------- | ------------------------------------------------------------------------------------ |
| Business source       | `orders`, `outbox_events`                                                            |
| Applied state         | `order_projections`, `processed_events`, `tenant_processing_state`                   |
| Invalid-event holding | `event_quarantine`                                                                   |
| Hourly rollups        | `hourly_summary_aggregates`, `hourly_product_aggregates`, `hourly_status_aggregates` |
| Daily rollups         | `daily_summary_aggregates`, `daily_product_aggregates`, `daily_status_aggregates`    |
| Completeness/repair   | `aggregate_coverage`, `bootstrap_jobs`, `bootstrap_orders`, `rebuild_archive`        |

The control database stores routing/provisioning metadata. API-key digest records live in server configuration; raw bearer/provider keys do not belong in tenant tables or Git.

### Financial and time rules

- Metric contract: **`created_at_shipped_sales_v2`**.
- Every current order counts once. **Only shipped orders contribute sales**; placed/cancelled contribute zero.
- Buckets follow immutable **order creation time**, not shipment time.
- Money uses Decimal, exact integer cents in contribution calculations, source `NUMERIC(18,2)` and aggregate `NUMERIC(38,2)`.
- Currency is mandatory and remains separate; USD/EUR are not added together.
- Windows are UTC, half-open `[start,end)`. Hourly boundaries must align exactly; daily trends require midnight. Maximum range is 366 days.
- Top products require positive sales; ordering is sales DESC, order count DESC, UUID ASC before limit 1–10. Product order counts include all statuses.
- Command Center shipped AOV divides shipped sales by shipped-order count. Advanced Analytics separately labels its all-order ratio **Sales / Total Orders**.
- Producer orders begin placed, version 1. Placed orders can be repriced/reassigned or shipped/cancelled. Shipped/cancelled are terminal in normal producer APIs.
- Each accepted mutation increments version once. Stale `expected_version` returns 409.

Example: a placed USD 100 order contributes one order / zero sales. Shipment changes it to one order / USD 100 sales. The net delta is zero orders / +USD 100. Updates use `existing − old contribution + new contribution`.

### Coverage and freshness

- Coverage proves the verified tenant/currency/window/dimensions. A revision is not an order-version watermark or completeness proof.
- A covered empty window returns genuine zeros. Missing/stale coverage returns 503; unsupported boundaries return 422.
- Browser retrieval duration includes HTTP and response decoding; it is not SQL execution time.
- Fetch time, source-event time and materialization time are different clocks. Old historical source timestamps alone do not imply backlog.
- Rebuild jobs stage state, verify SQL parity and activate under tenant fences while retaining source orders.
- The preserved live worker combines committed-order hints, retained Redis work receipts and bounded PostgreSQL fallback scans. It does not globally drain covered historical backlog by default.

### Event and migration contracts

- Default stream: **`astraroll:orders:v1`**, field **`event`**, payload **`schema_version: 2`**.
- Complete before/after snapshots contain UUIDs, order versions, exact amounts and UTC timestamps.
- Graph: `0001_producer → 0002_producer_contract → 0003_aggregation → 0004_coverage_precision → 0005_tenant_access_paths`.
- API adapters depend on an `AnalyticsReader` interface; producer, transport, aggregation and presentation are modular.

See [database/event contract](docs/contracts.md), [analytics](docs/analytics.md), [frontend contract](docs/v2-frontend-contract.md), and [operations](docs/release-operations.md).

## Ports and processes

| Purpose                              |              Port | Meaning                                             |
| ------------------------------------ | ----------------: | --------------------------------------------------- |
| Preserved 100-tenant dashboard/API   |          **8000** | FastAPI also serves the built frontend              |
| Swagger/OpenAPI                      |          **8000** | `/docs`, `/openapi.json` on the same application    |
| Earlier isolated local demo          |              8001 | Separate historical fixture profile                 |
| Fresh README demo dashboard/API      |          **8002** | Keeps the existing 8000 application available       |
| Frontend development                 |              5173 | Vite proxies API requests to 8000                   |
| Protected local PostgreSQL           |         **55433** | V1 archive + V2 databases; preserve it              |
| Protected local Redis                |         **56380** | Existing live event transport                       |
| Isolated regression PostgreSQL/Redis |     55434 / 56381 | Disposable-test environment                         |
| Fresh README PostgreSQL/Redis        | **55435 / 56382** | Separate Docker project/volumes below               |
| Standard Compose host defaults       |       5432 / 6379 | Set `POSTGRES_PORT` / `REDIS_PORT` to override      |
| PostgreSQL/Redis inside Compose      |       5432 / 6379 | Internal service ports, distinct from host bindings |

Relay and consumer are background workers, not HTTP servers, and need no web ports. `postgres` and `redis` are Docker-network hostnames; native Python uses `127.0.0.1` and published host ports.

## Quick start: a fresh V2 demo

This creates a small tenant on a separate Docker project. It needs no private 10M database files, cloud account or Groq key. PowerShell commands run from the repository root. Install Git, Python 3.12+ and Docker Compose. Node.js LTS/npm is needed for frontend development.

Start Docker Desktop's Linux engine before the Docker commands. The application/bootstrap path below was exercised against an isolated native PostgreSQL 16 / Redis 7 instance; the Compose service definitions are configuration-validated. The same host URLs and ports connect native Python to the published Docker services.

### 1. Clone and install

```powershell
git clone --branch codex/astraroll-v2-final-product https://github.com/PavanAksshay/cyberforge.git
Set-Location cyberforge
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install -e '.[dev,benchmark]'
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

### 2. Start PostgreSQL and Redis

```powershell
$env:POSTGRES_USER='astraroll'
$env:POSTGRES_PASSWORD='local-dev-only'
$env:POSTGRES_DB='astraroll_demo_control'
$env:POSTGRES_PORT='55435'
$env:REDIS_PORT='56382'
docker compose -p astraroll-readme up -d --wait postgres redis
docker compose -p astraroll-readme ps
```

The password is a public local-development example; use your own credentials outside a local demo. Project-specific volumes persist data. Routine shutdown is `docker compose -p astraroll-readme stop`; retain volumes/databases.

### 3. Provision a tenant, bootstrap coverage and issue its key

Run once. This uses the real provisioner/migrations and bootstrap, then writes an ignored private launch profile. Subsequent launches load that profile.

```powershell
@'
import hashlib, json, os, secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from astraroll.aggregation.bootstrap import bootstrap
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry

folder = Path(os.environ.get("README_DEMO_FOLDER", ".runtime/readme-demo"))
profile = folder / "profile.json"
if profile.exists():
    raise SystemExit("Profile already exists. Load it using step 4.")
url = os.environ.get("README_DEMO_CONTROL_URL",
    "postgresql+psycopg://astraroll:local-dev-only@127.0.0.1:55435/astraroll_demo_control")
parsed = make_url(url)
if parsed.port == 55433 or not parsed.database.startswith("astraroll_demo_"):
    raise SystemExit("Use a dedicated astraroll_demo_ database outside protected port 55433.")
folder.mkdir(parents=True, exist_ok=True)
control = create_engine(url)
registry = ControlRegistry(control)
registry.init_schema()
provisioner = TenantProvisioner(registry, url, tenant_database_prefix="astraroll_demo_t_")
tenant = uuid4()
record = provisioner.provision_tenant(tenant, "README Demo")
engine = create_engine(parsed.set(database=record.database_name),
    connect_args={"options": "-c timezone=UTC"})
start = datetime(2026, 10, 1, tzinfo=UTC)
end = datetime(2026, 11, 2, tzinfo=UTC)
bootstrap(sessionmaker(engine, expire_on_commit=False), tenant, ["USD", "EUR"], start, end)
token = secrets.token_urlsafe(32)
key = {"subject": "readme-demo", "tenant_ids": [str(tenant)],
    "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
    "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat()}
env = {"DEPLOYMENT_MODE": "database_per_tenant", "DATABASE_URL": url,
    "CONTROL_DATABASE_URL": url, "REDIS_URL": "redis://127.0.0.1:56382/0",
    "ORDER_STREAM": "astraroll:readme:" + tenant.hex,
    "ANALYTICS_API_KEYS_JSON": json.dumps([key]), "ANALYTICS_READ_BLOCK_REASON": "",
    "MAX_ACTIVE_TENANT_POOLS": "4", "TENANT_POOL_SIZE": "1",
    "TENANT_POOL_MAX_OVERFLOW": "0", "LIVE_OUTBOX_HINTS_ENABLED": "false"}
(folder / "api-key.txt").write_text(token, encoding="utf-8")
profile.write_text(json.dumps({"tenant_id": str(tenant), "database": record.database_name,
    "env": env}, indent=2), encoding="utf-8")
engine.dispose()
provisioner.dispose()
control.dispose()
print("Tenant:", tenant)
print("Database:", record.database_name)
print("Private profile:", profile)
'@ | python
```

This fixture window contains the included October 2026 CSV data. Choose a reviewed bootstrap window covering your own data for other periods. Coverage has an age limit; the key expires after 24 hours. Renew through bootstrap/key-issuance procedures rather than disabling checks.

### 4. Load the profile in each terminal

Open **three terminals**, activate the same virtual environment, and run this in each:

```powershell
$demo = Get-Content .runtime/readme-demo/profile.json -Raw | ConvertFrom-Json
$demo.env.PSObject.Properties | ForEach-Object {
    [Environment]::SetEnvironmentVariable($_.Name, [string]$_.Value, 'Process')
}
```

Then start one process per terminal:

```powershell
# Terminal A: API and built frontend
python -m uvicorn astraroll.api:app --host 127.0.0.1 --port 8002
```

```powershell
# Terminal B: relay only this explicitly selected tenant
python -m astraroll.v2.workers relay --tenant-id $demo.tenant_id
```

```powershell
# Terminal C: consumer for the profile's isolated stream
python -m astraroll.v2.workers consumer
```

### 5. Open, authenticate and demonstrate

1. Open **http://127.0.0.1:8002/**.
2. Select the key icon, **Local API key file**, and `.runtime/readme-demo/api-key.txt`.
3. Save Token; select **README Demo**, USD and **Covered Window**.
4. A verified empty tenant starts with genuine zero metrics.
5. Open CSV Ingestion, load the 12 demo orders, validate/dry-run if desired, then submit once.
6. Inspect progress, return to Command Center, refresh and run SQL verification.
7. Re-upload the exact file to demonstrate duplicate detection.

`demo/acceptance_12.csv` contains **12 orders: six shipped, four placed, two cancelled; USD 506.97 shipped sales**. A first upload to the empty tenant adds +12 orders / +506.97 sales. Exact re-upload creates zero new mutations. Uncataloged products retain real UUIDs rather than invented names.

### 6. Optional assistant and frontend development

Set server-only `GROQ_API_KEY` and `GROQ_MODEL=openai/gpt-oss-120b` in the API environment or private `.env`, then restart that API. `ASSISTANT_TIMEZONE` controls clock replies; analytics remains UTC. Provider failures are explicitly labeled, and local greetings/clock replies do not claim to be LLM answers.

```powershell
Set-Location web
npm ci
npm test
npm run lint
npm run build
```

FastAPI serves `web/dist`; refresh after building. `npm run dev` starts Vite on **5173**, whose checked-in proxy targets **8000**. Use the existing 8000 backend for that development profile, or deliberately change the proxy to **8002** for this fresh demo. The production build served directly on 8002 requires no proxy change.

### All-container V2 deployment

The repository also supplies a V2 Compose overlay:

```powershell
docker compose -f compose.yaml -f compose.v2.yaml config --quiet
# Separately configured installation with its registry and digest records:
docker compose -f compose.yaml -f compose.v2.yaml up --build -d postgres redis migrate api
```

The overlay selects database-per-tenant mode. Standard Compose publishes the API on 8000. Set a reviewed `V2_RELAY_TENANT_ID` before launching the overlay relay. Provisioning, coverage and authentication are explicit steps; the native quick start walks through them completely. Do not use global historical backlog draining as a startup shortcut.

Optional Groq variables must be explicitly passed into the API container's environment through your deployment override/secrets configuration. A host `.env` is used for Compose substitution; undeclared variables are not automatically forwarded into containers. Keep provider secrets out of the Docker image.

## Start the preserved 100-tenant installation

The current Windows review environment is separate from the fresh-clone demo. PostgreSQL data is at `D:/cyberforge/.runtime/release-pgdata`, port **55433**, with Redis on **56380**. Preserve the running services, databases and private audit checkpoints.

```powershell
Set-Location D:/cyberforge-v2-integration
Test-NetConnection 127.0.0.1 -Port 55433
Test-NetConnection 127.0.0.1 -Port 56380
```

### Cold boot: start retained infrastructure only when stopped

These commands are for this existing Windows installation after a host reboot. Confirm the named binary/data paths. They start a stopped service using retained files; they do not stop/restart a running service or initialize a new data directory.

```powershell
$pgCtl=Join-Path $env:TEMP 'astraroll-verification/pg-runtime/pgsql/bin/pg_ctl.exe'
$pgData='D:/cyberforge/.runtime/release-pgdata'
if (-not (Test-Path $pgCtl) -or -not (Test-Path "$pgData/PG_VERSION")) {
    throw 'Locate the reviewed PostgreSQL 16 binary and retained data directory first.'
}
& $pgCtl -D $pgData status
if ($LASTEXITCODE -eq 3) {
    & $pgCtl -D $pgData -l 'D:/cyberforge/.runtime/manual-pg-start.log' start
} elseif ($LASTEXITCODE -ne 0) {
    throw 'Inspect PostgreSQL status before proceeding.'
}
```

```powershell
$redisExe=Join-Path $env:TEMP 'astraroll-verification/redis7/Redis-7.4.3-Windows-x64-msys2/redis-server.exe'
$redisDir='D:/cyberforge/.runtime/perf-redis'
if (-not (Test-NetConnection 127.0.0.1 -Port 56380 -InformationLevel Quiet)) {
    if (-not (Test-Path $redisExe) -or -not (Test-Path "$redisDir/appendonlydir")) {
        throw 'Locate the reviewed Redis binary and retained AOF directory first.'
    }
    Start-Process -FilePath $redisExe -WindowStyle Hidden -ArgumentList @(
        '--bind','127.0.0.1','--port','56380','--dir',$redisDir,
        '--appendonly','yes','--appendfsync','always','--maxmemory-policy','noeviction'
    ) -RedirectStandardOutput 'D:/cyberforge/.runtime/manual-redis-start.log' `
      -RedirectStandardError 'D:/cyberforge/.runtime/manual-redis-start-error.log'
}
```

Recheck both ports, then launch the application and worker below. If local binaries have moved, resolve their paths to the reviewed compatible versions; preserve the data directories.

### Start the API and worker

Start one process per terminal only if it is not already running:

```powershell
# Terminal A
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
& D:/cyberforge/.venv/Scripts/python.exe scripts/live_benchmark.py
```

```powershell
# Terminal B
Set-Location D:/cyberforge-v2-integration
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
& D:/cyberforge/.venv/Scripts/python.exe scripts/live_benchmark.py --worker
```

The launcher checks all 100 registry/coverage mappings against `.runtime/tenant-analytics-repair.json`, loads scoped digest configuration and enables the reviewed live-write profile. It relies on private `.runtime/live-demo-config.json`: it is an installation launcher, not an empty-clone provisioner.

- Dashboard: **http://127.0.0.1:8000/**.
- Swagger/OpenAPI: **http://127.0.0.1:8000/docs**, `/openapi.json`.
- Readiness: **http://127.0.0.1:8000/health/ready**.
- Browser key: load `.runtime/local-access-key.txt` through the key icon.
- Worker heartbeat: ignored `.runtime/live-worker-health.json`.
- New frontend build: refresh with **Ctrl+F5**.

After a host restart, inspect the existing PostgreSQL service/data directory before starting that same cluster. Do not initialize over it, reset it, delete data files or use `docker compose down -v`. Private source/database files are not in GitHub; the fresh demo reproduces the application from a clone.

## API guide

Tenant routes require `Authorization: Bearer <key>`. The key's SHA-256 digest maps to server-configured subject, tenant memberships and expiry. Invalid/expired keys return **401**; unauthorized tenant access returns **403**. Order existence checks are tenant-scoped.

| Method | Endpoint                                        | Purpose                                  |
| ------ | ----------------------------------------------- | ---------------------------------------- |
| GET    | `/tenants`                                      | Visible tenant registry entries          |
| POST   | `/tenants/{tenant_id}/orders`                   | Create a placed order, version 1         |
| PATCH  | `/tenants/{tenant_id}/orders/{order_id}`        | Edit / apply a terminal transition       |
| POST   | `/tenants/{tenant_id}/orders/{order_id}/cancel` | Cancel with `expected_version`           |
| GET    | `/analytics/summary`                            | Count and shipped sales                  |
| GET    | `/analytics/trends`                             | Hour/day buckets                         |
| GET    | `/analytics/top-products`                       | Limit 1–10                               |
| GET    | `/analytics/status-breakdown`                   | Placed/shipped/cancelled                 |
| GET    | `/analytics/verify`                             | Explicit API/rollup/raw-order comparison |
| POST   | `/tenants/{tenant_id}/ingest/csv`               | CSV import with dry-run support          |
| GET    | `/tenants/{tenant_id}/status`                   | Health and coverage                      |
| POST   | `/tenants/{tenant_id}/assistant`                | Scoped analytics assistant               |
| GET    | `/health/live`, `/health/ready`                 | Process/dependency readiness             |

### Query all four shapes

In a profile-loaded fresh-demo PowerShell terminal:

```powershell
$base='http://127.0.0.1:8002'
$headers=@{Authorization='Bearer ' + (Get-Content .runtime/readme-demo/api-key.txt -Raw).Trim()}
$query='tenant_id=' + $demo.tenant_id + '&currency=USD&start=2026-10-01T00:00:00Z&end=2026-11-02T00:00:00Z'
Invoke-RestMethod "$base/analytics/summary?$query" -Headers $headers
Invoke-RestMethod "$base/analytics/trends?$query&interval=day" -Headers $headers
Invoke-RestMethod "$base/analytics/top-products?$query&limit=10" -Headers $headers
Invoke-RestMethod "$base/analytics/status-breakdown?$query" -Headers $headers
Invoke-RestMethod "$base/analytics/verify?$query" -Headers $headers
```

Money is a decimal string, such as `"506.97"`. Freshness uses persisted revision/materialization metadata; a checkpoint is returned only when actually available. GET analytics do not generate orders or advance processing.

### Ordinary live order

With relay/consumer running and coverage containing the current UTC day:

```powershell
$body=@{product_id=[guid]::NewGuid().ToString();amount='12.34';currency='USD'} | ConvertTo-Json
$order=Invoke-RestMethod "$base/tenants/$($demo.tenant_id)/orders" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$ship=@{expected_version=$order.version;status='shipped'} | ConvertTo-Json
Invoke-RestMethod "$base/tenants/$($demo.tenant_id)/orders/$($order.id)" -Method Patch -Headers $headers -ContentType 'application/json' -Body $ship
```

After processing, this adds one order / USD 12.34 shipped sales. Query a covered window containing its creation time. A separate placed order can demonstrate cancellation without changing sales.

### Independent SQL check

Connect to the selected tenant database and use matching filters:

```sql
SELECT count(*) AS total_orders,
       coalesce(sum(CASE WHEN status='shipped' THEN amount ELSE 0 END), 0) AS shipped_sales
FROM orders
WHERE tenant_id = 'REPLACE_WITH_TENANT_UUID'
  AND currency = 'USD'
  AND created_at >= '2026-10-01T00:00:00Z'
  AND created_at <  '2026-11-02T00:00:00Z';
```

Compare with the API and equivalent sums over `hourly_summary_aggregates`. Raw orders are read here as an explicit diagnostic oracle; normal dashboard analytics stay on rollups.

## Verification and tests

Latest complete backend run: **402 passed**, one existing Starlette/httpx deprecation warning. The final portfolio-specific run passed **18 checks**, including two additional boundary tests. Verified frontend: **15 passed**, TypeScript build and ESLint passed. These are recorded results tied to reviewed evidence, not a dynamically polled badge.

### Isolated backend suite

The local regression profile is PostgreSQL **55434** / Redis **56381**. Some tests create/drop their own disposable databases. Never point those tests at protected port 55433.

```powershell
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
$env:DATABASE_URL='postgresql+psycopg://astraroll@127.0.0.1:55434/postgres'
$env:CONTROL_DATABASE_URL=''
$env:DEPLOYMENT_MODE='shared'
$env:REDIS_URL='redis://127.0.0.1:56381/0'
$env:RUN_INTEGRATION='1'
$env:ANALYTICS_API_KEYS_JSON=''
python -m pytest -ra
$pythonFiles = @(git ls-files '*.py')
ruff check @pythonFiles
ruff format --check @pythonFiles
python -m alembic heads
python -m pip check
docker compose -f compose.yaml -f compose.v2.yaml config --quiet
```

On a new machine, provision dedicated PostgreSQL 16 / Redis 7 test services and replace those URLs. `RUN_INTEGRATION=1` enables live dependency tests; unset it for the dependency-free subset.

```powershell
# Frontend, from web/
npm ci
npm test
npm run typecheck
npm run lint
npm run build
npm audit --audit-level=low
```

### Preserved-data SQL oracle and latency measurement

With the preserved application on 8000:

```powershell
Set-Location D:/cyberforge-v2-integration
$env:PYTHONPATH='D:/cyberforge-v2-integration/src'
& D:/cyberforge/.venv/Scripts/python.exe -u -m astraroll.v2.verify_reconciliation --measure
```

This explicitly scans source orders for verification. Schedule it separately from heavy imports/benchmarks and use a stable cohort for cross-snapshot comparisons. Live writes can change a baseline after its audit. Detailed private artifacts stay in `.runtime/`; compact reviewed evidence belongs in `docs/`.

## Troubleshooting

| Symptom                      | Action                                                                                          |
| ---------------------------- | ----------------------------------------------------------------------------------------------- |
| 401 / empty tenant list      | Load the correct profile's unexpired key; 8000, 8001 and 8002 are separate profiles             |
| 403                          | Use an identity with membership for that tenant                                                 |
| 503 coverage                 | Inspect Data Health/bootstrap; reconcile or renew reviewed coverage rather than inventing zeros |
| 422 query                    | Check currency, UTC alignment, interval, date bounds and top-N limit                            |
| 409 mutation                 | Check expected version and terminal status; refetch before a legitimate retry                   |
| Old UI response/build        | Ctrl+F5; build `web/dist` after source changes                                                  |
| Committed import not visible | Match tenant/currency/window, inspect receipts/worker health and resume the saved import        |
| Zero sales delta             | Placed/cancelled sales are zero; check source currency/date and duplicate handling              |
| Provider error               | Inspect explicit provider status and sanitized code; check key/model/rate limits                |
| Port occupied                | Inspect listener/profile; use fresh demo 8002 instead of a duplicate 8000 server                |
| Memory pressure              | Respect the guard, free commit capacity, resume checkpoints; free disk and memory commit differ |

Key rotation is a trusted operator task: `python -m astraroll.analytics.issue_key --subject demo --tenant TENANT_UUID --hours 24`. Store only digest records on the server, share the secret privately, and restart the API after configuration changes. Keep `.env`, keys, bulk datasets and database files out of Git.

For the fresh demo, update the private profile's `env.ANALYTICS_API_KEYS_JSON` record and matching client key file. The preserved launcher reads its key digest/expiry from `.runtime/live-demo-config.json`; renew that installation record and its client key together. Do not substitute a key from a different profile.

## Repository map

```text
src/astraroll/
  api.py, schemas.py, orders.py, models.py   producer and HTTP boundary
  analytics/                               API, auth and reader contracts
  aggregation/                             contributions, consumers, rollups, bootstrap
  v2/                                      routing, provisioning, workers and live product
alembic/                                   versioned database migrations
web/src/                                   React pages, charts and polling
web/dist/                                  build served by FastAPI
demo/                                      small reproducible CSV fixtures
tests/                                     producer, API, recovery and correctness
docs/                                      contracts, evidence and screenshots
```

## Evidence library

- [README checklist and executed startup validation](docs/readme-verification.md)
- [All Tenants Overview: API, authorization and measured SQL parity](docs/portfolio-overview.md)
- [Event/database contract](docs/contracts.md)
- [Analytics semantics/API](docs/analytics.md)
- [100-tenant restoration](docs/100-tenant-restoration.md)
- [10M machine-readable evidence](docs/100-tenant-restoration-evidence.json)
- [Actual 1M execution](docs/v2-measured-execution.md)
- [Frozen 1M profile](docs/release-evidence/frozen-9af9abd.json)
- [Live product and Redis recovery](docs/live-product-verification.md)
- [Assistant, frontend and clock verification](docs/product-polish-verification.md)
- [Operations](docs/release-operations.md)

**Deployment boundary:** the reviewed setup uses localhost services and expiring server-configured keys. Database/OS administration remains trusted. Production hosting has its own identity, TLS, credential-lifecycle and resource-isolation design. AstraRoll uses PostgreSQL, Redis Streams and bounded Python workers without Kafka, Kubernetes or ML.
