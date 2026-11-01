import React, { useState } from "react";
import { useApp } from "../context/AppContext";
import { usePolling } from "../lib/usePolling";
import { apiJson } from "../lib/api";
import {
  Network,
  Database,
  Layers,
  Cpu,
  ShieldCheck,
  Zap,
  BarChart3,
  Server,
  Activity,
  Award,
  CheckCircle2,
  ArrowRight,
  GitBranch,
  Bot,
  Flame,
  Gauge,
  Workflow,
  Sparkles,
  ChevronRight,
  Boxes,
  Lock,
  Clock,
  Split,
  Eye,
} from "lucide-react";

interface StageDetail {
  id: string;
  number: string;
  title: string;
  shortDesc: string;
  icon: React.ReactNode;
  badge: string;
  badgeColor: string;
  whyEfficient: string;
  laptopFootprint: string;
  dataFlow: {
    input: string;
    action: string;
    output: string;
    branching: string[];
  };
  keySpecs: { label: string; value: string }[];
}

export const ArchitecturePage: React.FC = () => {
  const { token } = useApp();
  const audit = usePolling(token, (signal) =>
    apiJson("/ops/restoration", token, signal),
  );
  const snapshot = audit.data;
  const benchmarkStats = [
    {
      label: "Physical Tenant DBs",
      value: snapshot ? String(snapshot.authorized_tenants) : "—",
      subtitle: "Authorized physical databases",
    },
    {
      label: "Orders Per Tenant",
      value: snapshot
        ? `${snapshot.orders_min?.toLocaleString()}–${snapshot.orders_max?.toLocaleString()}`
        : "—",
      subtitle: "Audited per-tenant range",
    },
    {
      label: "Last Audited Source Orders",
      value: snapshot ? snapshot.source_orders.toLocaleString() : "—",
      subtitle: snapshot
        ? `${snapshot.verified_tenants} tenants verified`
        : "Audit unavailable",
    },
    {
      label: "Latest Measured p95",
      value: snapshot?.performance
        ? `${Math.max(...Object.values(snapshot.performance.queries).map((q: any) => q.p95_ms)).toFixed(1)} ms`
        : "Unmeasured",
      subtitle: "Latest profile: worst endpoint p95",
    },
  ];

  const latencyBreakdown = snapshot?.performance
    ? Object.entries(snapshot.performance.queries).map(([shape, value]) => {
        const metric = value as any;
        return {
          endpoint: `/analytics/${shape}`,
          p95: `${metric.p95_ms.toFixed(1)} ms`,
          target: "< 200 ms",
          status: `${metric.p95_under_200ms ? "Passing" : "Above target"} (p99: ${metric.p99_ms.toFixed(1)} ms)`,
        };
      })
    : [];

  const stages: StageDetail[] = [
    {
      id: "ingest",
      number: "01",
      title: "Order Ingestion & Transactional Outbox",
      shortDesc: "Instant order writes without analytics overhead",
      icon: <Workflow className="w-5 h-5 text-emerald-400" />,
      badge: "Atomic tenant-local commit",
      badgeColor: "bg-emerald-950/80 text-emerald-300 border-emerald-800",
      whyEfficient:
        "Orders and outbox event records are committed in the exact same local ACID transaction inside the tenant DB. Tenant fences coordinate source writes, consumers and rebuild activation.",
      laptopFootprint:
        "Uses tenant-local engines for short-lived commits with bounded pools.",
      dataFlow: {
        input:
          "REST API / CSV Ingestion payload (Order details, status, currency)",
        action:
          "Single ACID Transaction: INSERT INTO orders + INSERT INTO outbox_events",
        output: "Durable event UUID and monotonic order version",
        branching: [
          "orders (shipped / placed / cancelled)",
          "outbox_events (event_id, payload, published=false)",
        ],
      },
      keySpecs: [
        { label: "Ingestion Speed", value: "Measured per request" },
        { label: "Write Consistency", value: "Local ACID" },
        { label: "Lock Contention", value: "Tenant-scoped fences" },
      ],
    },
    {
      id: "control",
      number: "02",
      title: "Central Control Plane & Routing",
      shortDesc: "Dynamic catalog & LRU connection pooling",
      icon: <Database className="w-5 h-5 text-teal-400" />,
      badge: "LRU Pool Manager",
      badgeColor: "bg-teal-950/80 text-teal-300 border-teal-800",
      whyEfficient:
        "A tenant registry resolves authorized UUIDs to database connections. An LRU manager bounds active tenant engines instead of opening a persistent pool for every database.",
      laptopFootprint:
        "The local API allows four active tenant pools and the worker two; each pool has one connection and no overflow.",
      dataFlow: {
        input: "Incoming request with Tenant UUID header or URL path",
        action:
          "Control DB looks up connection URL; LRU manager leases or creates cached engine",
        output:
          "Bound SQLAlchemy Session connected directly to target tenant database",
        branching: [
          "control_tenant_registry (routing metadata)",
          "control_tenant_migration_checkpoints (Alembic 0005 head)",
        ],
      },
      keySpecs: [
        { label: "Max Active Pools", value: "API 4 / worker 2" },
        { label: "Idle Tenant Cost", value: "LRU eviction closes pools" },
        { label: "Isolation Level", value: "Per-Tenant Connection" },
      ],
    },
    {
      id: "shards",
      number: "03",
      title: "100 Physically Isolated Tenant DBs",
      shortDesc: "10M rows partitioned into 100k row databases",
      icon: <Server className="w-5 h-5 text-cyan-400" />,
      badge: "100 Physical DBs",
      badgeColor: "bg-cyan-950/80 text-cyan-300 border-cyan-800",
      whyEfficient:
        "Each authorized tenant routes to its own registered physical database. The audited baseline below reports exact counts. PostgreSQL CPU, memory and disk remain shared resources.",
      laptopFootprint:
        "PostgreSQL manages lightweight storage files per DB; buffer cache shared efficiently.",
      dataFlow: {
        input: "Tenant-specific writes, rollups, and analytical reads",
        action: "Parallel execution against isolated physical schemas",
        output:
          "Clean, uncorrupted customer data with independent backup capability",
        branching: [
          "Tenant DB 001..100 (orders, outbox, 6 rollup tables)",
          "Separate databases / independent vacuum cycles",
        ],
      },
      keySpecs: [
        { label: "Database Count", value: "100 Physical" },
        { label: "Rows Per Tenant", value: "See audited range above" },
        { label: "Tenant Boundary", value: "Authenticated scope + routing" },
      ],
    },
    {
      id: "relay",
      number: "04",
      title: "Multi-Tenant Streaming Relay & Redis 7",
      shortDesc: "Round-robin fair publishing to Redis Streams",
      icon: <Zap className="w-5 h-5 text-amber-400" />,
      badge: "Stream: astraroll:orders:v1",
      badgeColor: "bg-amber-950/80 text-amber-300 border-amber-800",
      whyEfficient:
        "Committed-order hints accelerate live updates. Bounded round-robin outbox checks recover lost hints. Durable work receipts support retries after publication, and consumers acknowledge stream events after the PostgreSQL commit.",
      laptopFootprint:
        "Bounded batches and tenant connection pools limit resource use; pressure still depends on workload.",
      dataFlow: {
        input: "Unpublished records from tenant outbox_events tables",
        action:
          "LiveOutboxWorker selects bounded cohorts -> XADD -> existing consumer",
        output: "High-throughput stream published to astraroll:orders:v1",
        branching: [
          "Redis Stream Consumer Group (astraroll-live-imports-v2)",
          "ACK-after-commit tracking with fault recovery",
        ],
      },
      keySpecs: [
        { label: "Relay Scheduling", value: "Round-Robin Fair" },
        { label: "Message Durability", value: "At-Least-Once" },
        { label: "Stream Throughput", value: "Not measured in this profile" },
      ],
    },
    {
      id: "consumer",
      number: "05",
      title: "Continuous Asynchronous Rollup Engine",
      shortDesc: "Pre-computed projection tables in USD & EUR",
      icon: <Layers className="w-5 h-5 text-emerald-400" />,
      badge: "6 Rollup Tables / DB",
      badgeColor: "bg-emerald-950/80 text-emerald-300 border-emerald-800",
      whyEfficient:
        "The rollup consumer incrementally maintains 6 projection tables per tenant database in both USD and EUR using ON CONFLICT DO UPDATE upserts. Dashboard queries never calculate aggregations on raw orders.",
      laptopFootprint:
        "Incremental changes use bounded transactions. Explicit repairs rebuild and verify derived data under tenant fences.",
      dataFlow: {
        input: "Batch of order stream events from Redis consumer group",
        action:
          "Incremental upsert into HourlySummary, Product, and Status aggregate tables",
        output:
          "Verified analytical views with persisted revision and coverage metadata",
        branching: [
          "HourlySummaryAggregate (USD & EUR, interval: hour/day)",
          "ProductAggregate (Top products by revenue & volume)",
          "StatusAggregate (shipped, placed, cancelled counts)",
        ],
      },
      keySpecs: [
        { label: "Aggregate Upsert", value: "O(1) Incremental" },
        { label: "Freshness SLA", value: "Target under 30 seconds" },
        { label: "SQL Parity", value: "Verify against raw SQL" },
      ],
    },
    {
      id: "serving",
      number: "06",
      title: "Measured Rollup Queries & Grounded Assistant",
      shortDesc: "FastAPI analytics engine & function-calling AI",
      icon: <Sparkles className="w-5 h-5 text-emerald-400" />,
      badge: "Tenant-scoped rollup queries",
      badgeColor: "bg-emerald-950/80 text-emerald-300 border-emerald-800",
      whyEfficient:
        "Summary and trends use time-bucket rollups; top products groups product rollups. Actual query plans and latency are measured for the verified UTC window. The assistant uses these same authorized analytics contracts.",
      laptopFootprint:
        "Minimal CPU; index-backed queries return in milliseconds.",
      dataFlow: {
        input: "User requests Summary, Trends, Products, or asks AI Assistant",
        action:
          "FastAPI reads pre-computed aggregates; Groq synthesizes plain executive response",
        output:
          "Instant interactive chart rendering & clean natural language answers",
        branching: [
          "React Control Center (Live graphs, latency metrics)",
          "Groq AI Assistant (openai/gpt-oss-120b tool calls)",
        ],
      },
      keySpecs: [
        { label: "Dashboard p95", value: "Latest measured profile below" },
        { label: "Read Source", value: "Verified hourly / daily rollups" },
        { label: "Scale Comparison", value: "Not measured in this profile" },
      ],
    },
  ];

  const [activeStageId, setActiveStageId] = useState<string>("ingest");
  const activeStage = stages.find((s) => s.id === activeStageId) || stages[0];

  return (
    <div className="space-y-10 max-w-7xl mx-auto pb-12">
      {/* Hero Header */}
      <div className="p-8 rounded-3xl bg-gradient-to-r from-emerald-950 via-emerald-900 to-slate-900 text-white shadow-xl border border-emerald-800/40">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
          <div className="space-y-2">
            <div className="flex items-center space-x-2">
              <span className="px-3 py-1 rounded-full text-[11px] font-extrabold uppercase bg-emerald-500/20 border border-emerald-400/40 text-emerald-300">
                Verified Tenant Baseline
              </span>
              <span className="px-3 py-1 rounded-full text-[11px] font-extrabold uppercase bg-emerald-500/20 border border-emerald-400/40 text-emerald-300 flex items-center">
                <CheckCircle2 className="w-3.5 h-3.5 mr-1" />
                {snapshot ? snapshot.source_orders.toLocaleString() : "—"}{" "}
                audited orders
              </span>
            </div>
            <h1 className="text-3xl sm:text-4xl font-black tracking-tight">
              AstraRoll V2: Database-Per-Tenant Architecture
            </h1>
            <p className="text-sm sm:text-base text-emerald-200/90 max-w-3xl leading-relaxed">
              Physical database-per-tenant routing with asynchronous streaming
              rollups. Audited counts and measured performance appear below.
            </p>
          </div>

          <div className="flex items-center space-x-4 shrink-0 p-4 rounded-2xl bg-black/30 border border-emerald-700/40">
            <div className="text-right">
              <div className="text-[11px] text-emerald-300 uppercase tracking-wider font-semibold">
                Alembic Head
              </div>
              <div className="font-mono font-bold text-sm text-white">
                0005_tenant_access_paths
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 10M Benchmark Showcase Cards */}
      <div className="space-y-4">
        <div className="text-xs font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
          Last Audited Baseline and Measured Profile
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {benchmarkStats.map((st, i) => (
            <div
              key={i}
              className="p-6 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-2 hover:border-emerald-500/40 transition-colors"
            >
              <div className="text-xs font-semibold text-slate-500 dark:text-slate-400">
                {st.label}
              </div>
              <div className="text-3xl font-extrabold text-slate-900 dark:text-white font-mono">
                {st.value}
              </div>
              <div className="text-xs text-emerald-600 dark:text-emerald-400 font-medium">
                {st.subtitle}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ============================================================== */}
      {/* INTERACTIVE ARCHITECTURE TREE & PIPELINE EXPLORER */}
      {/* ============================================================== */}
      <div className="p-8 rounded-3xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-lg space-y-8">
        <div>
          <div className="flex items-center space-x-2.5">
            <Workflow className="w-5 h-5 text-emerald-500" />
            <h2 className="font-extrabold text-lg text-slate-900 dark:text-white">
              Interactive Execution Tree & Architecture Explorer
            </h2>
          </div>
          <p className="text-xs sm:text-sm text-slate-500 dark:text-slate-400 mt-1">
            Click on any pipeline stage below to inspect its data flow,
            isolation mechanics and resource limits.
          </p>
        </div>

        {/* Spacious Interactive Stage Selector Tabs */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          {stages.map((st) => {
            const isActive = st.id === activeStageId;
            return (
              <button
                key={st.id}
                onClick={() => setActiveStageId(st.id)}
                className={`flex flex-col items-start p-4 rounded-2xl border text-left transition-all duration-200 ${
                  isActive
                    ? "bg-emerald-950/60 border-emerald-500 text-white shadow-md shadow-emerald-900/20 ring-1 ring-emerald-500"
                    : "bg-slate-50 dark:bg-slate-900/50 border-slate-200 dark:border-slate-800 text-slate-600 dark:text-slate-400 hover:border-slate-300 dark:hover:border-slate-700"
                }`}
              >
                <div className="flex items-center justify-between w-full mb-2">
                  <div
                    className={`p-2 rounded-xl ${isActive ? "bg-emerald-600 text-white" : "bg-slate-200 dark:bg-slate-800"}`}
                  >
                    {st.icon}
                  </div>
                  <span className="text-xs font-mono font-bold text-slate-400">
                    {st.number}
                  </span>
                </div>
                <div className="font-black text-sm leading-snug text-slate-900 dark:text-white">
                  {st.title}
                </div>
                <div className="text-xs text-slate-300 mt-1 line-clamp-1 font-mono">
                  {st.badge}
                </div>
              </button>
            );
          })}
        </div>

        {/* Stage Deep-Dive Card (Generous Padding & Readable Spacing) */}
        <div className="p-6 sm:p-8 rounded-2xl bg-slate-50 dark:bg-[#070E0A] border border-emerald-800/40 space-y-6">
          {/* Header of Active Stage */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-6 border-b border-slate-200 dark:border-slate-800">
            <div className="flex items-center space-x-3">
              <div className="p-3 rounded-2xl bg-emerald-600 text-white shadow-md">
                {activeStage.icon}
              </div>
              <div>
                <div className="flex items-center space-x-2">
                  <span className="text-sm font-mono font-black text-emerald-400 tracking-wide">
                    STAGE {activeStage.number}
                  </span>
                  <span
                    className={`px-3 py-1 rounded-full text-xs font-extrabold border font-mono ${activeStage.badgeColor}`}
                  >
                    {activeStage.badge}
                  </span>
                </div>
                <h3 className="text-2xl sm:text-3xl font-black text-slate-900 dark:text-white mt-1 tracking-tight">
                  {activeStage.title}
                </h3>
              </div>
            </div>

            <div className="flex items-center space-x-3">
              {activeStage.keySpecs.map((spec, i) => (
                <div
                  key={i}
                  className="px-3 py-1.5 rounded-xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-right"
                >
                  <div className="text-xs uppercase font-extrabold text-slate-300 tracking-wider">
                    {spec.label}
                  </div>
                  <div className="text-sm sm:text-base font-black text-emerald-300 font-mono">
                    {spec.value}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Interactive Tree Node Flow Graphic */}
          <div className="space-y-3">
            <div className="text-sm sm:text-base font-black uppercase tracking-wider text-emerald-400 flex items-center">
              <GitBranch className="w-3.5 h-3.5 mr-1.5 text-emerald-500" />
              Stage Data Flow & Branching Tree
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-5 text-sm">
              {/* Node 1: Input */}
              <div className="p-4 rounded-xl bg-white dark:bg-slate-900/80 border border-slate-200 dark:border-slate-800 space-y-1.5">
                <div className="text-xs font-black uppercase tracking-wider text-slate-300">
                  1. Upstream Input
                </div>
                <div className="font-semibold text-base text-slate-900 dark:text-white leading-relaxed">
                  {activeStage.dataFlow.input}
                </div>
              </div>

              {/* Node 2: Processing Action */}
              <div className="p-4 rounded-xl bg-white dark:bg-slate-900/80 border border-emerald-800/60 space-y-1.5 shadow-sm">
                <div className="text-xs font-black uppercase tracking-wider text-emerald-400">
                  2. Core Processing Engine
                </div>
                <div className="font-semibold text-base text-slate-900 dark:text-white leading-relaxed">
                  {activeStage.dataFlow.action}
                </div>
              </div>

              {/* Node 3: Output & Branches */}
              <div className="p-4 rounded-xl bg-white dark:bg-slate-900/80 border border-slate-200 dark:border-slate-800 space-y-2">
                <div className="text-xs font-black uppercase tracking-wider text-slate-300">
                  3. Downstream Branches
                </div>
                <div className="font-semibold text-base text-slate-900 dark:text-white leading-relaxed">
                  {activeStage.dataFlow.output}
                </div>
                <div className="space-y-1 pt-1 border-t border-slate-200 dark:border-slate-800">
                  {activeStage.dataFlow.branching.map((br, idx) => (
                    <div
                      key={idx}
                      className="flex items-center text-xs sm:text-sm text-emerald-300 font-mono font-medium"
                    >
                      <ChevronRight className="w-3 h-3 mr-1 shrink-0" />
                      <span>{br}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>

          {/* Efficiency & Laptop Resource Explanation */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 text-sm pt-2">
            <div className="p-4 rounded-xl bg-emerald-950/20 border border-emerald-800/50 space-y-1.5">
              <div className="font-black text-base sm:text-lg text-emerald-400 flex items-center">
                <CheckCircle2 className="w-3.5 h-3.5 mr-1.5" />
                How This Stage Works
              </div>
              <p className="text-slate-200 leading-relaxed text-sm sm:text-base font-normal">
                {activeStage.whyEfficient}
              </p>
            </div>

            <div className="p-4 rounded-xl bg-teal-950/20 border border-teal-800/50 space-y-1.5">
              <div className="font-black text-base sm:text-lg text-teal-400 flex items-center">
                <Cpu className="w-3.5 h-3.5 mr-1.5" />
                Standard Laptop Resource Footprint
              </div>
              <p className="text-slate-200 leading-relaxed text-sm sm:text-base font-normal">
                {activeStage.laptopFootprint}
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* ============================================================== */}
      {/* 4 FOUNDATIONAL PILLARS EXPLAINING 10M EFFICIENCY */}
      {/* ============================================================== */}
      <div className="space-y-4">
        <div className="flex items-center space-x-2">
          <Gauge className="w-5 h-5 text-emerald-500" />
          <h3 className="font-black text-xl sm:text-2xl text-slate-900 dark:text-white">
            The 4 Foundational Pillars: How 10M Runs on a Normal Laptop
          </h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 text-sm">
          <div className="p-6 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 space-y-3 shadow-sm">
            <div className="flex items-center justify-between">
              <h4 className="font-black text-slate-900 dark:text-white text-base sm:text-lg">
                1. Physical Sharding (10M Orders → 100x 100k)
              </h4>
              <span className="px-3 py-1 rounded-full text-xs font-black uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                Isolation
              </span>
            </div>
            <p className="text-slate-200 dark:text-slate-200 leading-relaxed text-sm sm:text-base">
              Orders and derived tables live in dedicated tenant databases. Each
              query targets one tenant. The PostgreSQL server still shares CPU,
              RAM and storage, so concurrent workloads can affect latency.
            </p>
          </div>

          <div className="p-6 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 space-y-3 shadow-sm">
            <div className="flex items-center justify-between">
              <h4 className="font-black text-slate-900 dark:text-white text-base sm:text-lg">
                2. Dynamic LRU Connection Pool Recycling
              </h4>
              <span className="px-3 py-1 rounded-full text-xs font-black uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                Memory
              </span>
            </div>
            <p className="text-slate-200 dark:text-slate-200 leading-relaxed text-sm sm:text-base">
              LRU eviction closes unused tenant pools. This local profile uses
              at most four tenant pools in the API and two in the worker, each
              with one connection and no overflow. Control and diagnostic
              connections require additional server capacity.
            </p>
          </div>

          <div className="p-6 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 space-y-3 shadow-sm">
            <div className="flex items-center justify-between">
              <h4 className="font-black text-slate-900 dark:text-white text-base sm:text-lg">
                3. Transactional Outbox vs Distributed 2PC
              </h4>
              <span className="px-3 py-1 rounded-full text-xs font-black uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                Reliability
              </span>
            </div>
            <p className="text-slate-200 dark:text-slate-200 leading-relaxed text-sm sm:text-base">
              Writing to both an order database and an analytical store
              simultaneously risks split-brain failure or distributed two-phase
              commit overhead. With the Transactional Outbox pattern, writes
              commit atomically; the durable outbox supports retries through
              Redis Streams. Visibility latency is measured separately.
            </p>
          </div>

          <div className="p-6 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 space-y-3 shadow-sm">
            <div className="flex items-center justify-between">
              <h4 className="font-black text-slate-900 dark:text-white text-base sm:text-lg">
                4. Pre-Computed Aggregates
              </h4>
              <span className="px-3 py-1 rounded-full text-xs font-black uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                Query Speed
              </span>
            </div>
            <p className="text-slate-200 dark:text-slate-200 leading-relaxed text-sm sm:text-base">
              Instead of recalculating SUM(sales) over 10,000,000 raw rows on
              every page refresh, the dashboard queries pre-aggregated hourly
              and daily buckets. Query cost depends on the selected window and
              grouping. The table below reports the current measured profile; a
              controlled 1M versus 10M comparison is not available.
            </p>
          </div>
        </div>
      </div>

      {/* 10M Scale Competition Benchmark Targets */}
      <div className="p-6 sm:p-8 rounded-3xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div className="flex items-center space-x-2">
            <Award className="w-5 h-5 text-amber-500" />
            <div>
              <h3 className="font-bold text-base text-slate-900 dark:text-white">
                10 Million Records Across 100 Tenants: Official Competition
                Targets
              </h3>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                Performance criteria on a standard laptop: hitting the{" "}
                <strong>Good</strong> column qualifies; hitting the{" "}
                <strong>Great</strong> column stands out.
              </p>
            </div>
          </div>
          <span className="px-3 py-1 rounded-full text-xs font-extrabold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800 shrink-0">
            Targets are separate from measured results
          </span>
        </div>

        <div className="overflow-x-auto pt-2">
          <table className="w-full text-sm sm:text-base text-left">
            <thead className="bg-slate-50 dark:bg-slate-800/60 text-slate-500 uppercase tracking-wider text-[10px]">
              <tr>
                <th className="px-4 py-3 rounded-l-lg">What We Measure</th>
                <th className="px-4 py-3">How</th>
                <th className="px-4 py-3">Good Target</th>
                <th className="px-4 py-3">Great Target</th>
                <th className="px-4 py-3">Current verified profile</th>
                <th className="px-4 py-3 rounded-r-lg">Verdict</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800 font-mono">
              <tr className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40">
                <td className="px-4 py-3 text-slate-900 dark:text-white font-sans font-bold">
                  Query speed
                </td>
                <td className="px-4 py-3 font-sans text-slate-600 dark:text-slate-400">
                  Time for each dashboard query at 10M records
                </td>
                <td className="px-4 py-3 text-slate-500">Under 1 second</td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Under 200 ms
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Use the measured profile above
                </td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                    Unmeasured
                  </span>
                </td>
              </tr>
              <tr className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40">
                <td className="px-4 py-3 text-slate-900 dark:text-white font-sans font-bold">
                  Freshness
                </td>
                <td className="px-4 py-3 font-sans text-slate-600 dark:text-slate-400">
                  Time from adding an order to seeing it in the numbers
                </td>
                <td className="px-4 py-3 text-slate-500">Under 5 minutes</td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Under 30 seconds
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Use measured import lifecycle evidence
                </td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                    Unmeasured
                  </span>
                </td>
              </tr>
              <tr className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40">
                <td className="px-4 py-3 text-slate-900 dark:text-white font-sans font-bold">
                  Database safety
                </td>
                <td className="px-4 py-3 font-sans text-slate-600 dark:text-slate-400">
                  Normal app query time, with and without dashboard load
                </td>
                <td className="px-4 py-3 text-slate-500">
                  Less than 25% slower
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Less than 10% slower
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Use the measured profile above
                </td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                    Unmeasured
                  </span>
                </td>
              </tr>
              <tr className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40">
                <td className="px-4 py-3 text-slate-900 dark:text-white font-sans font-bold">
                  Correctness
                </td>
                <td className="px-4 py-3 font-sans text-slate-600 dark:text-slate-400">
                  Compare your numbers with a direct database query
                </td>
                <td className="px-4 py-3 text-slate-500">
                  Matches on most queries
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Matches on all queries
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Run Verification for the selected tenant
                </td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                    Unmeasured
                  </span>
                </td>
              </tr>
              <tr className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40">
                <td className="px-4 py-3 text-slate-900 dark:text-white font-sans font-bold">
                  Growth
                </td>
                <td className="px-4 py-3 font-sans text-slate-600 dark:text-slate-400">
                  Query speed at 1M vs 10M records
                </td>
                <td className="px-4 py-3 text-slate-500">
                  Less than 5x slower
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  Less than 2x slower
                </td>
                <td className="px-4 py-3 text-emerald-600 dark:text-emerald-400 font-bold">
                  No controlled scale comparison measured
                </td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                    Unmeasured
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* Latency Table */}
      <div className="p-6 rounded-3xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <Activity className="w-4 h-4 text-emerald-500" />
            <h3 className="font-black text-lg sm:text-xl text-slate-900 dark:text-white">
              Measured API Response Times
            </h3>
          </div>
          <span className="text-xs font-semibold text-emerald-600 dark:text-emerald-400">
            {snapshot?.performance
              ? `${snapshot.performance.concurrency} concurrent requests across ${snapshot.performance.tenant_databases} tenant databases`
              : "No measured profile available"}
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm sm:text-base text-left">
            <thead className="bg-slate-50 dark:bg-slate-800/60 text-slate-500 uppercase tracking-wider text-[10px]">
              <tr>
                <th className="px-4 py-3 rounded-l-lg">
                  Authorized Endpoint Shape
                </th>
                <th className="px-4 py-3">Measured p95</th>
                <th className="px-4 py-3">Target SLA</th>
                <th className="px-4 py-3 rounded-r-lg">Verdict</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800 font-mono">
              {latencyBreakdown.map((row, idx) => (
                <tr
                  key={idx}
                  className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40"
                >
                  <td className="px-4 py-3 text-slate-900 dark:text-white font-sans font-medium">
                    <code>{row.endpoint}</code>
                  </td>
                  <td className="px-4 py-3 font-bold text-emerald-600 dark:text-emerald-400">
                    {row.p95}
                  </td>
                  <td className="px-4 py-3 text-slate-500">{row.target}</td>
                  <td className="px-4 py-3">
                    <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-emerald-100 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800">
                      {row.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Honest Architectural Trade-Offs & Scaling Roadmap */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 text-xs leading-relaxed">
        <div className="p-6 rounded-3xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-3">
          <div className="flex items-center space-x-2 font-bold text-slate-900 dark:text-white">
            <ShieldCheck className="w-4 h-4 text-emerald-500" />
            <span className="text-lg sm:text-xl font-black text-white">
              Honest Architectural Analysis
            </span>
          </div>
          <ul className="space-y-2 text-slate-600 dark:text-slate-300">
            <li>
              • <strong>Connection Management:</strong> 100 separate PostgreSQL
              databases require disciplined connection pool budgets. AstraRoll
              V2 bounds active pools to four in the local API and two in the
              worker. Pool eviction trades connection setup cost for lower
              connection use.
            </li>
            <li>
              • <strong>Relay Fairness:</strong> A round-robin cursor across
              active tenants prevents a single heavy tenant from starving outbox
              streaming for others.
            </li>
            <li>
              • <strong>Preserved Semantics:</strong> Retained frozen sales
              definition (
              <code className="font-mono text-[11px]">
                created_at_shipped_sales_v2
              </code>
              ), immutable order creation UTC boundaries, and decimal precision
              throughout.
            </li>
          </ul>
        </div>

        <div className="p-6 rounded-3xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-3">
          <div className="flex items-center space-x-2 font-bold text-slate-900 dark:text-white">
            <Layers className="w-4 h-4 text-emerald-500" />
            <span className="text-lg sm:text-xl font-black text-white">
              Horizontal Next-Scale Roadmap
            </span>
          </div>
          <ul className="space-y-2 text-slate-600 dark:text-slate-300">
            <li>
              • <strong>Distributed Host Clusters:</strong> The control database
              routing table supports separate host and port configurations per
              tenant, enabling multi-cluster distribution across PostgreSQL
              nodes.
            </li>
            <li>
              • <strong>Dynamic Consumer Partitioning:</strong> As tenant count
              scales past 100, the single Redis stream can be partitioned into
              tenant-hashed consumer groups.
            </li>
            <li>
              • <strong>Resumable Migration:</strong> Checkpointed keyset
              batches support retries. Final cutover requires coordinated tenant
              fences; uninterrupted writes are not guaranteed.
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
};
