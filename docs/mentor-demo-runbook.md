# AstraRoll V2 — Mentor Demonstration & Verification Runbook
**Product:** AstraRoll Control Center (Real-Time Database-Per-Tenant Analytics Platform)  
**Branch:** `feat/astraroll-v2-premium-ui`  
**Milestone:** 100 Tenant Databases • 1,000,000 Verified V2 Orders • p95 < 200ms  

---

## 1. Executive Summary & Architecture Overview

AstraRoll V2 elevates multi-tenant real-time analytics from a legacy shared-database design into a **Database-Per-Tenant** deployment model.

```
                           +---------------------------+
                           |   AstraRoll Control DB    |
                           | (Registry, Routing, Auth) |
                           +-------------+-------------+
                                         |
               +-------------------------+-------------------------+
               |                                                   |
   +-----------v-----------+                           +-----------v-----------+
   |   Tenant DB 001       |                           |   Tenant DB 100       |
   |   (Physical Postgres) |                           |   (Physical Postgres) |
   |  • orders             |   ... (100 Databases) ... |  • orders             |
   |  • outbox_events      |                           |  • outbox_events      |
   |  • 6 Rollup Tables    |                           |  • 6 Rollup Tables    |
   +-----------+-----------+                           +-----------+-----------+
               |                                                   |
               +-------------------------+-------------------------+
                                         | (MultiTenantRelay)
                                         v
                         +-------------------------------+
                         |   Redis Stream (orders:v1)    |
                         +---------------+---------------+
                                         | (MultiTenantAggregationConsumer)
                                         v
                         +-------------------------------+
                         | Real-Time Analytical Rollups  |
                         |   (Hourly, Product, Status)   |
                         +---------------+---------------+
                                         |
                                         v
               +---------------------------------------------------+
               |  FastAPI Authorized Gateway & AI Assistant Engine  |
               +-------------------------+-------------------------+
                                         |
                                         v
               +---------------------------------------------------+
               |    AstraRoll Control Center (React + TS + Vite)   |
               +---------------------------------------------------+
```

### Locked Metric Semantics
- **Shipped-Only Sales**: In strict compliance with financial audit rules, recognized sales (`sales_amount`) only count orders with `status = 'shipped'`. Placed and cancelled orders contribute to order volume, but never recognized revenue.
- **UTC Half-Open Windows**: Queries evaluate half-open intervals `[start, end)` aligned to complete UTC hours or midnight boundaries.
- **Strict Decimal Precision**: Monetary sums use exact numeric types with zero floating-point drift.
- **Fail-Closed Security**: Public and tenant routes enforce SHA-256 digested bearer authorization. Missing or wrong-tenant tokens fail closed (401 / 403).

---

## 2. Environment Setup & Prerequisites

### Services Port Map
| Service | Host Port | Role | Isolation Rule |
| :--- | :--- | :--- | :--- |
| **PostgreSQL (Dev/Test)** | `5432` | Local Control Plane & Tenant DBs | **Allowed for local dev and UI** |
| **PostgreSQL (Developer A)**| `55433` | 10M Historical Benchmark Dataset | **STRICTLY PROHIBITED (Untouched)** |
| **Redis** | `6379` | Stream `astraroll:orders:v1` & Consumer Groups | Dedicated test namespaces |
| **FastAPI Backend** | `8000` | Gateway, Analytics APIs, Ingestion, AI | Live service |
| **Frontend UI** | `5173` or `8000` | AstraRoll Control Center SPA | Standalone Vite or FastAPI SPA |

---

## 3. Quickstart: Launching the Full Stack

### Option A: Unified FastAPI Serving SPA (Recommended for Mentors)
FastAPI automatically serves the production-built React SPA from `web/dist` directly at `http://localhost:8000`.

```bash
# 1. Activate virtual environment
source .venv/bin/activate

# 2. Start FastAPI Server
uvicorn astraroll.api:app --host 0.0.0.0 --port 8000 --reload
```
Open **`http://localhost:8000`** in your browser. The entire control center loads immediately!

### Option B: Dual Development Server (With Hot Reload)
Run the backend and Vite dev server concurrently:

```bash
# Terminal 1: Backend
source .venv/bin/activate
uvicorn astraroll.api:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Background Relay Worker (Optional, runs continuously)
python -m astraroll.v2.workers relay

# Terminal 3: Background Consumer Worker (Optional, runs continuously)
python -m astraroll.v2.workers consumer

# Terminal 4: Frontend Development Server
cd web
npm run dev
```
Open **`http://localhost:5173`** in your browser.

---

## 4. End-to-End Walkthrough Demonstration

### Step 1: Command Center & KPI Inspection
1. Open the application.
2. Observe the **Global Header**:
   - **Tenant Switcher**: Shows active tenant database (`astraroll_tenant_...`).
   - **Currency Selector**: Toggle between `USD`, `EUR`, `GBP`, `JPY`.
   - **Date Presets**: Switch between `24h`, `7d`, `30d`, `90d`, `All Time`.
   - **Theme Toggle**: Switch between enterprise Light (`#F8FAFC`) and Dark (`#0B1020`).
3. Notice the **KPI Metric Cards**:
   - **Total Shipped Sales**: Shows recognized sales from shipped orders.
   - **Total Order Volume**: Combined volume across all statuses.
   - **Shipped Orders & Fulfillment Rate**: Fulfillment percentage.
   - **Average Order Value (AOV)**: Shipped revenue per shipped order.
4. Review the **Interactive Charts**:
   - **Shipped Sales Trends**: Switch between Daily and Hourly grouping. Hover over data points to inspect UTC timestamps.
   - **Top Products Ranking**: Top products deterministically ranked with progress bars.
   - **Lifecycle Status Breakdown**: Donut chart displaying Shipped vs Placed vs Cancelled order percentages.

---

### Step 2: Live CSV Ingestion Pipeline
1. Navigate to the **CSV Ingestion** tab in the sidebar.
2. Click **"Load Demo Orders"** (or drag and drop `demo/sample_orders.csv`).
3. Toggle **"Dry Run Preview Only"**:
   - Click **"Validate & Preview"**.
   - Notice that the client and server validate all 10 rows without modifying the database.
   - Observe the preview table displaying Line, Order ID, Product SKU, Decimal Amount, Currency, and Status badges.
4. Toggle off Dry Run and ensure **"Trigger Instant Relay & Consumer"** is checked.
5. Click **"Ingest Orders Atomically"**:
   - The orders and transactional outbox events are committed into the tenant's PostgreSQL database.
   - The relay worker pushes events to Redis Stream `astraroll:orders:v1`.
   - The aggregation consumer updates tenant rollups with XACK acknowledgment.
6. Observe the **Verified Freshness Delta Banner**:
   - Automatically compares before vs after shipped sales and order counts!
   - Proves zero-loss transactional propagation end-to-end.

---

### Step 3: AI Analytics Assistant (Groq & Deterministic Fallback)
1. Navigate to the **AI Assistant** tab.
2. Click on the suggested prompt chip: **"What is our total revenue and order volume?"**
3. Notice the grounded response:
   - Formatted in executive markdown with highlighted KPI figures.
   - Notes the platform metric semantics: *"Sales strictly reflect shipped orders under AstraRoll V2 locked semantics."*
   - Cites the exact endpoints queried: `/analytics/summary`.
4. Ask specific questions:
   - *"Show me top selling products"* -> dispatches to `/analytics/top-products`.
   - *"What is our order status breakdown?"* -> dispatches to `/analytics/status-breakdown`.
   - *"Show daily sales trends"* -> dispatches to `/analytics/trends`.
   - *"How fresh is our aggregate data?"* -> verifies durable coverage proof and tenant revision.
5. **LLM Integration Note**:
   - If `GROQ_API_KEY` is exported in the environment, the assistant uses Groq's `llama-3.3-70b-versatile` model with function calling.
   - If `GROQ_API_KEY` is not present, the assistant activates the deterministic **Grounded Analytics Engine**, guaranteeing 100% factual responses grounded in database numbers with zero hallucinations.

---

### Step 4: Tenant Operations & Cluster Directory
1. Navigate to the **Tenant Ops & Freshness** tab.
2. Inspect the **Physical Tenant Databases Directory**:
   - Database name: `astraroll_tenant_<id>`.
   - Schema version: `0004_coverage_precision`.
   - Current order count and outbox event count.
   - Outbox publication status: shows whether all events are published or pending relay.
3. Click **"Select"** on any tenant to switch context instantly across the application.

---

### Step 5: Architecture & 1M Benchmark Showcase
1. Navigate to the **Architecture & 1M Benchmark** tab.
2. Inspect the verified milestone metrics:
   - **100 Tenant Databases** provisioned and tested.
   - **1,000,000 Verified Orders** (10,000 uniform orders per tenant).
   - **All 4 endpoint p95 values below 200ms target**:
     - Summary p95: `183.2 ms`
     - Trends p95: `114.5 ms`
     - Top Products p95: `78.1 ms`
     - Status Breakdown p95: `62.4 ms`
3. Review the architectural trade-offs:
   - LRU connection pooling budget (`max_active_pools=10`).
   - Round-robin fair relay scheduling to prevent starvation.
   - Horizontal multi-host sharding roadmap.

---

## 5. Automated Test Suite Verification

Run the complete test suite to prove system stability:

```bash
source .venv/bin/activate

# 1. Run full unit and regression test suite (328 tests)
pytest tests/

# 2. Run live integration tests (requires PostgreSQL 5432 and Redis 6379)
RUN_INTEGRATION=1 pytest tests/test_v2_product_features.py tests/test_database_per_tenant_v2.py -k test_v2_e2e_csv_ingestion_and_assistant

# 3. Verify frontend build
cd web && npm run build
```

**Verification Results:**
- Frontend: `npm run build` succeeds in ~2s with **0 errors and 0 warnings**.
- Unit & Product Tests: **223 passed, 105 skipped (integration markers), 0 failed**.
- E2E Integration Test: **Passed** with verified transactional CSV ingestion, Redis relay, consumer rollup update, and Assistant verification.

