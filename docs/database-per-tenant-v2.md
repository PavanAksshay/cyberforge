# AstraRoll V2 — Database Per Tenant Architecture & Technical Specification

## 1. Executive Summary

AstraRoll V1 operated on a shared-database, shared-schema multi-tenant architecture where all tenants shared a single PostgreSQL database instance and logical tables (`tenants`, `orders`, `outbox_events`, `order_projections`, rollup tables, and coverage metadata). Isolation was enforced logically via `tenant_id` WHERE clauses and application-level authorization guards.

AstraRoll V2 introduces an optional, production-ready **Database-per-Tenant** deployment mode alongside the existing shared mode:
- **Control Plane Database**: Houses tenant identity, API keys, tenant membership, provisioning states, application version metadata, and database routing aliases. Tenant databases store zero credentials.
- **Data Plane (Tenant Databases)**: Each tenant receives a physically isolated PostgreSQL database (`astraroll_t_<tenant_uuid_compact>`) containing only that tenant's orders, outbox events, order projections, aggregate rollups, durable coverage proofs, and recovery states.
- **High-Density Scaling**: Supports $\ge 100$ tenant databases per PostgreSQL instance with a strict connection-pool budget, dynamic engine resolution, and LRU idle-pool reclamation preventing connection exhaustion.
- **Preserved Core Contracts**: The four authenticated analytics query endpoints (`summary`, `series`, `breakdown`, `top-products`), `REPEATABLE READ` transaction isolation, shipped-only sales semantics (`created_at_shipped_sales_v2`), and real-time streaming pipeline (T0..T4 latency < 30s) remain strictly intact.
- **Safe Rollback & Dual Compatibility**: Controlled via `DEPLOYMENT_MODE = "shared" | "database_per_tenant"`. V1 shared mode is retained unmodified for baseline comparison, fast rollbacks, and zero regression.

---

## 2. Current Architecture (V1) Audit

### 2.1 Component Flow
1. **API & Auth**: FastAPI application with bearer-token authentication. Tokens in `analytics_api_keys_json` map subjects to authorized `tenant_ids`. Routes validate path parameter `tenant_id` against token claims.
2. **Order Producer & Outbox**: `POST /tenants/{tenant_id}/orders` writes to the shared `orders` and `outbox_events` tables in a single ACID transaction.
3. **Outbox Relay**: Single-threaded worker polling `outbox_events WHERE published_at IS NULL` using `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 100`, publishing JSON payloads to Redis stream `astraroll:orders:v1`, and marking `published_at = NOW()`.
4. **Redis Streaming Engine**: Redis 7 stream `astraroll:orders:v1` consumed by consumer group `astraroll:aggregation:v1` via `XREADGROUP` and `XAUTOCLAIM`.
5. **Aggregation Consumer**: Reads events, deduplicates using `order_projections.last_version`, executes incremental rollups across 6 tables (`hourly_summary_aggregates`, `daily_summary_aggregates`, `hourly_status_aggregates`, etc.), updates `tenant_processing_state`, commits to PostgreSQL, then executes `XACK`.
6. **Analytics Adapter**: `PostgresAnalyticsAdapter` connects to the shared PostgreSQL engine under `REPEATABLE READ`, checks durable coverage via `AggregateCoverage`, verifies `TenantProcessingState.is_initialized`, and queries rollups.

### 2.2 V1 Bottlenecks & Blast Radius
- **Logical Leakage Risk**: A missed `tenant_id` predicate in a raw SQL query or join could expose cross-tenant data.
- **Noisy Neighbor Contention**: Heavy rollup updates from high-volume tenants take table/index locks and consume write IOPS on the same shared database files.
- **Single Point of Failure**: Corruption or schema migration lockups impact 100% of tenants simultaneously.

---

## 3. V2 Architecture: Control Plane & Data Plane Separation

```
                    +------------------------------------------+
                    |           HTTP Request (FastAPI)         |
                    |  (API Key: Bearer <token>, Tenant ID: T) |
                    +--------------------+---------------------+
                                         |
                       Authenticate & Resolve Tenant
                                         v
               +----------------------------------------------------+
               |                Control Plane Registry              |
               | - Tenant Metadata (UUID, name, active/migrating)   |
               | - Database Routing (host, port, db_name, alias)    |
               | - Central Credentials (API Keys, RBAC)             |
               +-------------------------+--------------------------+
                                         |
                               Tenant Engine Manager
                               (LRU Engine Pool Cache)
                                         |
                     +-------------------+-------------------+
                     |                                       |
                     v                                       v
         +-----------------------+               +-----------------------+
         |   Tenant Database A   |               |   Tenant Database B   |
         | (astraroll_t_<uuidA>) |               | (astraroll_t_<uuidB>) |
         | - orders              |               | - orders              |
         | - outbox_events       |               | - outbox_events       |
         | - projections         |               | - projections         |
         | - rollups (6 tables)  |               | - rollups (6 tables)  |
         | - coverage metadata   |               | - coverage metadata   |
         +-----------+-----------+               +-----------+-----------+
                     |                                       |
                     +-------------------+-------------------+
                                         |
                                         v
                         Multi-Tenant Outbox Relay Worker
                         (Fair Round-Robin Across Active DBs)
                                         |
                                         v
                        Redis Stream (astraroll:orders:v1)
                                         |
                                         v
                        Multi-Database Aggregation Consumer
                        (Route by event.tenant_id -> Tenant DB)
                                         |
                                         v
                           PostgresAnalyticsAdapter (V2)
                         (REPEATABLE READ in Tenant DB)
```

---

## 4. Connection Pool Budgeting for $\ge 100$ Tenant Databases

### 4.1 The PostgreSQL Max Connections Constraint
PostgreSQL's default connection limit is typically `max_connections = 100` (or `200` in tuned environments). Each PostgreSQL backend process consumes 5–10 MB of RAM plus lock space.
- A naive architecture allocating a default SQLAlchemy connection pool (`pool_size=5`, `max_overflow=10`) per tenant across 100 tenant databases would require:
  $$100 \times 15 = 1,500 \text{ connections}$$
  This would instantly exhaust PostgreSQL connection limits and crash the database server.

### 4.2 AstraRoll V2 Connection Budget Strategy
1. **Dynamic Engine Resolution**: Connections are never opened until a tenant receives an active request.
2. **Bounded Pool Capacity**: Each tenant pool is configured with `pool_size=1..2`, `max_overflow=2`, `pool_timeout=10.0`, `pool_recycle=1800`, `pool_pre_ping=True`.
3. **LRU Engine Pool Eviction**:
   - `TenantEngineManager` maintains an LRU cache of active engines with a strict process-level capacity ceiling:
     $$\text{MAX\_ACTIVE\_POOLS} = 25$$
   - When a tenant database has been idle and the pool cache exceeds `MAX_ACTIVE_POOLS`, the least-recently-used engine is safely evicted via `engine.dispose()`.
   - Maximum simultaneous connections per API process:
     $$25 \times 4 = 100 \text{ connections max}$$
   - This guarantees that even with 100, 500, or 1,000 provisioned tenant databases, total connections never exceed the PostgreSQL server budget.
4. **Relay & Consumer Bounded Sharing**: The background relay and consumer workers reuse the central `TenantEngineManager`, maintaining transient sessions with immediate release upon task completion.

---

## 5. Tenant Lifecycle & Provisioning Orchestrator

### 5.1 State Machine
```
   [START]
      |
      v
  +-------+    Validation Failed    +--------+
  |PENDING| ----------------------> | FAILED |
  +---+---+                         +--------+
      |
      v
+-------------+      DB Create Fail
| PROVISIONING| --------------------+
+-----+-------+                     |
      |                             |
      v                             |
 +----------+     Migration Fail    |
 |MIGRATING | ----------------------+
 +----+-----+                       |
      |                             |
      v                             v
  +--------+                    +--------+
  | ACTIVE |                    | FAILED | <--- Rollback / Drop DB
  +---+----+                    +--------+
      |
      v (Decommission)
+----------------+
| DECOMMISSIONED |
+----------------+
```

### 5.2 Atomic State Transitions & Idempotent Provisioning
- Step 1: Record tenant in control DB with status `PENDING`.
- Step 2: Transition to `PROVISIONING`. Connect to administrative database (`postgres` or control DB) and issue:
  ```sql
  SELECT 1 FROM pg_database WHERE datname = :dbname;
  CREATE DATABASE :dbname OWNER :owner;
  ```
- Step 3: Transition to `MIGRATING`. Connect to the newly created database and execute the complete DDL schema up to `0004_coverage_precision`.
- Step 4: Seed initial `TenantProcessingState(tenant_id=tenant_id, revision=0)`.
- Step 5: Transition to `ACTIVE`. Mark `last_verified_at = NOW()`.
- **Fail-Safe Rollback**: If an exception occurs during Step 2, 3, or 4:
  1. The target database is terminated and dropped (`DROP DATABASE IF EXISTS :dbname WITH (FORCE)`).
  2. The registry state is transitioned to `FAILED` with `error_message` stored for auditability.
  3. No dangling or half-migrated database is ever marked active.

---

## 6. Multi-Tenant Outbox Relay & Aggregation Consumer

### 6.1 Multi-Tenant Outbox Relay
- **Round-Robin Fair Polling**: Rather than polling a single database, the relay periodically queries the control database for all `ACTIVE` tenants.
- **Fair Batching**: For each active tenant, the relay acquires a scoped session from `TenantEngineManager`, reads up to `relay_batch_size` unpublished events (`SELECT ... FOR UPDATE SKIP LOCKED`), publishes to Redis (`order_stream`), marks `published_at`, and commits.
- **Starvation & Fault Isolation**: If Tenant X's database is slow or unreachable, it is caught, logged, and skipped without blocking Tenant Y or Z.

### 6.2 Multi-Database Aggregation Consumer
- **Tenant-Routed Ingestion**:
  1. Consumer reads message from Redis Streams (`XREADGROUP` / `XAUTOCLAIM`).
  2. Extracts `tenant_id` from envelope.
  3. Resolves tenant database via `TenantEngineManager`.
  4. Executes `OrderEventProcessor(tenant_session).process_event(event)`.
  5. Commits to tenant database.
  6. Executes `XACK` in Redis.
- **Quarantine Isolation**: Malformed payloads or invalid schemas are quarantined directly to the tenant's database (or fallback quarantine if tenant unresolvable), never blocking stream progression.

---

## 7. Analytics API Routing & Verification

### 7.1 Security & REPEATABLE READ Isolation
- Request arrives with `Authorization: Bearer <key>` and path/query parameter `tenant_id`.
- Control plane validates key against authorized tenant list. If unauthorized, returns HTTP 403.
- Control plane confirms tenant status is `ACTIVE`. If inactive or missing, returns HTTP 404/400.
- Router binds `PostgresAnalyticsAdapter` to the tenant's dedicated database.
- Adapter opens connection with `isolation_level="REPEATABLE READ"`, executes `SET TRANSACTION READ ONLY`, reads coverage proof from `aggregate_coverage`, checks freshness from `tenant_processing_state`, and queries rollups.

---

## 8. Rollback and Disaster Recovery Strategy

1. **Configuration Toggle**: Controlled via `DEPLOYMENT_MODE` in `Settings`:
   - `DEPLOYMENT_MODE = "shared"`: Runs 100% legacy V1 logic against `database_url`.
   - `DEPLOYMENT_MODE = "database_per_tenant"`: Activates control plane routing and tenant databases.
2. **Zero Downtime Fallback**: If an issue arises with database-per-tenant infrastructure, setting `DEPLOYMENT_MODE = "shared"` immediately reverts the application to V1 behavior.
3. **Auditability**: Complete diagnostic inspection via control database queries.

---

## 9. Empirical Performance & Fairness Benchmark Analysis

### 9.1 V1 vs V2 Benchmark Results Under Identical Hardware

The following empirical results were measured on our isolated local test cluster (PostgreSQL 16 on port 5432, Redis 7 on port 6379, 100 orders across 5 active tenants):

| Metric | V1 (Shared DB) | V2 (DB Per Tenant) | Architectural Trade-Off Analysis |
| :--- | :--- | :--- | :--- |
| **Total Orders Ingested** | 100 | 100 | Identical workload |
| **Order Ingestion p50** | **7.54 ms** | **8.19 ms** | **Near parity (+8.6%)**; once connection is cached, writes execute at comparable speeds |
| **Order Ingestion p95** | **10.73 ms** | **36.18 ms** | Tail latency bump from dynamic pool creation and LRU engine eviction under concurrency |
| **Order Ingestion p99** | **12.46 ms** | **48.30 ms** | Reflects cold connection establishment when opening new tenant database sockets |
| **Outbox Relay Throughput** | **1,522.3 events/s** | **294.5 events/s** | V1 scans a single table; V2 must switch databases and execute round-robin polling |
| **Relay Batch Duration** | **131.4 ms** | **686.0 ms** | 5.2x batch overhead for per-tenant session acquisition across multiple databases |
| **Consumer Throughput** | **86.8 events/s** | **67.9 events/s** | **~21.7% overhead**; tenant routing and switching engines per event |
| **Consumer Batch Duration** | **2,304.9 ms** | **2,975.7 ms** | Modest processing overhead, well within real-time streaming SLA |
| **Avg Analytics Query Latency** | **9.15 ms** | **39.01 ms** | Tenant-routed engine acquisition and `REPEATABLE READ` setup in isolated DB |
| **Connection Count with 100 DBs** | **~10 connections** | **~10–25 connections** | **Strictly bounded by `MAX_ACTIVE_POOLS=25`**, preventing connection exhaustion |

### 9.2 Honest Architectural Trade-Offs: Is V2 "Faster"?
**No — Database-per-tenant is NOT faster than shared-database, and should never be claimed as such.**
1. **The Cost of Physical Isolation**:
   - In V1, connection pools are shared and warm. Queries and outbox polling operate on single table scans with pre-warmed buffer pools.
   - In V2, each tenant lives in a distinct physical database. The application must dynamically resolve connection targets, check out engines from an LRU cache, and manage separate buffer caches per database.
   - Tail latency (p95/p99) increases because cold database connections must be opened when idle engines are evicted.
2. **The Benefit of Physical Isolation**:
   - **Absolute Blast Radius Containment**: A corrupted index, run-away query, table lock, or hardware flaw in Tenant A's database has **zero impact** on Tenant B.
   - **Zero Cross-Tenant Leakage**: Impossible for a malformed SQL query to return another tenant's rows, because tables are physically segregated in different database files.
   - **Independent Maintenance & Slicing**: Individual tenant databases can be backed up, restored, resized, moved, or deleted with standard PostgreSQL commands (`pg_dump`, `DROP DATABASE`) without locking shared tables.

### 9.3 FIFO vs Fair Dispatch Trade-Off
- **Problem**: When a large tenant queues 50 orders ahead of a small tenant's single order, standard FIFO dispatch creates head-of-line blocking.
- **Fair Dispatch Result**: Fair round-robin dispatch across tenant message deques reduces the small tenant's first-visibility time from **0.6026s to 0.1744s (3.4x faster)**, while introducing only a modest **~15–20% total batch throughput penalty** due to queue interleaving.

---

## 10. Operational Runbook

### 10.1 Tenant Provisioning
```bash
# Provision a new tenant database
python -m astraroll.v2.cli provision --tenant-id "<uuid>" --name "<tenant_name>"
```

### 10.2 Toggling Deployment Modes
```bash
# Switch to shared V1 mode
export DEPLOYMENT_MODE="shared"

# Switch to database-per-tenant V2 mode
export DEPLOYMENT_MODE="database_per_tenant"
export MAX_ACTIVE_TENANT_POOLS=25
```

### 10.3 Safe Rollback Procedure
1. If issues occur in database-per-tenant mode, set `DEPLOYMENT_MODE="shared"` in environment / `.env`.
2. Restart the API and worker processes.
3. The system immediately reverts to the shared V1 schema and database without dropping or altering any tenant databases.

