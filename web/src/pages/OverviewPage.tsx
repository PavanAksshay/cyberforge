import React, { useState, useEffect, useRef } from "react";

import {
  DollarSign,
  ShoppingCart,
  CheckCircle2,
  Clock,
  XCircle,
  Zap,
  TrendingUp,
  BarChart3,
  PieChart,
  ShieldCheck,
  AlertCircle,
  RotateCcw,
} from "lucide-react";

import { useApp } from "../context/AppContext";

import {
  fetchTenantStatus,
  fetchSummary,
  fetchTrends,
  fetchTopProducts,
  fetchStatusBreakdown,
} from "../lib/api";

import {
  SummaryResponse,
  TrendsResponse,
  TopProductsResponse,
  StatusBreakdownResponse,
  Interval,
} from "../types";

import { MetricCard } from "../components/MetricCard";

import {
  TrendLineChart,
  TopProductsBarChart,
  StatusDonutChart,
} from "../components/Charts";

import { usePolling } from "../lib/usePolling";
import { measureRetrieval } from "../lib/retrieval";

import { FreshnessIndicator } from "../components/FreshnessIndicator";

import {
  averageMoney,
  formatCurrency,
  formatNumber,
  formatDateTime,
} from "../lib/utils";

export const OverviewPage: React.FC = () => {
  const {
    activeTenant,
    currency,
    startDate,
    endDate,
    token,
    refreshKey,
    autoRefreshInterval,
    triggerRefresh,
  } = useApp();

  const [interval, setInterval] = useState<Interval>("day");

  const queryState = usePolling(
    `${activeTenant?.id}:${currency}:${startDate}:${endDate}:${interval}:${token}`,
    async (signal) => {
      if (!activeTenant) return null;

      const [summary, measuredTrends, topProducts, statuses, telemetry] =
        await Promise.all([
          fetchSummary(
            activeTenant.id,
            currency,
            startDate,
            endDate,
            token,
            signal,
          ),

          measureRetrieval(() =>
            fetchTrends(
              activeTenant.id,
              currency,
              startDate,
              endDate,
              interval,
              token,
              signal,
            ),
          ),

          fetchTopProducts(
            activeTenant.id,
            currency,
            startDate,
            endDate,
            5,
            token,
            signal,
          ),

          fetchStatusBreakdown(
            activeTenant.id,
            currency,
            startDate,
            endDate,
            token,
            signal,
          ),

          fetchTenantStatus(activeTenant.id, token, signal),
        ]);

      return {
        summary,
        trends: measuredTrends.data,
        trendsRetrievalMs: measuredTrends.retrievalMs,
        topProducts,
        statuses,
        telemetry,
      };
    },
  );

  const { loading, error: fetchError, fetchedAt: lastFetchedAt } = queryState;

  const { summary, trends, topProducts, statuses, telemetry } =
    queryState.data || {};

  if (!activeTenant) {
    return (
      <div className="flex flex-col items-center justify-center h-96 text-slate-400">
        <AlertCircle className="w-8 h-8 mb-2 text-emerald-400" />

        <p className="text-sm">
          Please select a tenant database to view analytics.
        </p>
      </div>
    );
  }

  // Calculate breakdown counts

  const shippedCount =
    statuses?.statuses.find((s) => s.status === "shipped")?.total_order_count ||
    0;

  const placedCount =
    statuses?.statuses.find((s) => s.status === "placed")?.total_order_count ||
    0;

  const cancelledCount =
    statuses?.statuses.find((s) => s.status === "cancelled")
      ?.total_order_count || 0;

  const totalSales = summary?.sales_amount || "0.00";

  const totalOrders = summary?.total_order_count || 0;

  const aov = averageMoney(totalSales, shippedCount);

  // Last materialized timestamp from summary freshness (distinct from HTTP fetch)

  const lastMaterializedAt = summary?.freshness?.last_processed_at ?? null;

  return (
    <div className="space-y-6">
      {/* Freshness & Pipeline Banner */}

      <div className="flex flex-col sm:flex-row sm:items-center justify-between p-4 rounded-2xl bg-gradient-to-r from-emerald-50/80 via-white to-slate-50 dark:from-[#07261d] dark:via-[#092c22] dark:to-[#07261d] border border-emerald-100 dark:border-emerald-900/60 shadow-sm gap-3">
        <div className="flex items-center space-x-3">
          <div className="p-2.5 rounded-xl bg-emerald-600 text-white shadow-md shadow-emerald-600/20">
            <Zap className="w-4 h-4" />
          </div>

          <div>
            <div className="flex items-center space-x-2">
              <span className="font-bold text-sm text-slate-900 dark:text-white">
                Verified Materialized Snapshot
              </span>
            </div>

            <p className="text-xs text-slate-500 dark:text-slate-400">
              Aggregated directly from physical database{" "}
              <code className="text-emerald-600 dark:text-emerald-400 font-mono font-medium">
                {telemetry?.database_name || "Resolving tenant database…"}
              </code>
            </p>
          </div>
        </div>

        <div className="flex flex-col sm:flex-row sm:items-center gap-2">
          {/* Real freshness indicator — A: last HTTP fetch, B: last materialization */}

          <FreshnessIndicator
            lastFetchedAt={lastFetchedAt}

            lastMaterializedAt={lastMaterializedAt}

            isLoading={loading}

            fetchError={fetchError}

            autoRefreshInterval={autoRefreshInterval}

            onManualRefresh={triggerRefresh}

            nextAt={queryState.nextAt}

            pending={!!telemetry?.pending_live_events}

            sourceCommittedAt={telemetry?.last_source_event_at}
          />

          <div className="flex items-center space-x-4 text-xs font-mono text-slate-500 dark:text-slate-400">
            <div>
              Revision:{" "}
              <strong className="text-slate-800 dark:text-slate-200">
                {summary?.freshness?.tenant_revision ?? "—"}
              </strong>
            </div>

            <div>
              Synced:{" "}
              <strong className="text-slate-800 dark:text-slate-200">
                {formatDateTime(lastMaterializedAt)}
              </strong>
            </div>
          </div>
        </div>
      </div>

      {/* Error Notice */}

      {fetchError && (
        <div className="p-4 rounded-xl bg-rose-50 dark:bg-rose-950/40 border border-rose-200 dark:border-rose-900 text-rose-700 dark:text-rose-400 text-xs flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <AlertCircle className="w-4 h-4 shrink-0" />

            <span>Refresh failed: {fetchError}</span>
          </div>

          <button
            onClick={triggerRefresh}

            className="flex items-center space-x-1 px-2.5 py-1 rounded bg-rose-100 dark:bg-rose-900/60 hover:bg-rose-200 font-semibold"
          >
            <RotateCcw className="w-3 h-3" />

            <span>Retry</span>
          </button>
        </div>
      )}

      <p className="text-xs text-slate-400">
        All panels: UTC [{startDate}, {endDate}) · {currency}. Historical
        snapshot; refresh checks for real changes.
      </p>

      {loading && !summary && <p>Checking authorized analytics…</p>}

      {fetchError && !summary && (
        <p>Metrics unavailable. No zero values are substituted.</p>
      )}

      {summary && (
        <>
          {/* KPI Grid */}

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <MetricCard
              title="Total Shipped Sales"

              value={formatCurrency(totalSales, currency)}

              subtitle="Strictly shipped orders"

              icon={<DollarSign className="w-5 h-5" />}

              highlight={true}

              tooltip="Under AstraRoll V2 frozen semantics, only orders in status='shipped' contribute to recognized financial sales."
            />

            <MetricCard
              title="Total Order Volume"

              value={formatNumber(totalOrders)}

              subtitle="All statuses combined"

              icon={<ShoppingCart className="w-5 h-5" />}

              tooltip="Includes placed, shipped, and cancelled orders created in this window."
            />

            <MetricCard
              title="Shipped Orders"

              value={formatNumber(shippedCount)}

              subtitle={`${totalOrders > 0 ? Math.round((shippedCount / totalOrders) * 100) : 0}% fulfillment rate`}

              icon={<CheckCircle2 className="w-5 h-5 text-teal-600" />}

              tooltip="Orders that have reached final shipped status."
            />

            <MetricCard
              title="Average Order Value"

              value={formatCurrency(aov, currency)}

              subtitle="Shipped revenue / order"

              icon={<TrendingUp className="w-5 h-5 text-emerald-500" />}

              tooltip="Calculated dynamically: total recognized sales divided by shipped order volume."
            />
          </div>

          {/* Charts Section: Row 1 */}

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Sales & Orders Time Series */}

            <div className="lg:col-span-2 p-5 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <BarChart3 className="w-4 h-4 text-emerald-500" />

                  <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                    Shipped Sales &amp; Volume Trends
                  </h3>
                  {queryState.data && (
                    <span
                      className="inline-flex items-center gap-1 rounded-full border border-emerald-800 bg-emerald-950/60 px-2 py-1 text-[11px] font-mono text-emerald-300"
                      title={`Last successful trends request, including network and response decoding. Retrieved ${lastFetchedAt?.toISOString() ?? ""}. This is not SQL execution time.`}
                    >
                      <Clock className="h-3 w-3" />
                      Last retrieval:{" "}
                      {queryState.data.trendsRetrievalMs.toFixed(1)} ms
                    </span>
                  )}
                </div>

                <div className="flex items-center rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 text-xs">
                  <button
                    onClick={() => setInterval("day")}

                    className={`px-2.5 py-1 rounded-md font-semibold transition ${
                      interval === "day"
                        ? "bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm"
                        : "text-slate-500 hover:text-slate-900 dark:hover:text-white"
                    }`}
                  >
                    Daily
                  </button>

                  <button
                    onClick={() => setInterval("hour")}

                    className={`px-2.5 py-1 rounded-md font-semibold transition ${
                      interval === "hour"
                        ? "bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm"
                        : "text-slate-500 hover:text-slate-900 dark:hover:text-white"
                    }`}
                  >
                    Hourly
                  </button>
                </div>
              </div>

              <TrendLineChart
                data={trends?.buckets || []}

                currency={currency}

                interval={interval}
              />
            </div>

            {/* Order Lifecycle Breakdown */}

            <div className="p-5 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
              <div className="flex items-center space-x-2">
                <PieChart className="w-4 h-4 text-teal-500" />

                <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                  Lifecycle Status Breakdown
                </h3>
              </div>

              <StatusDonutChart
                statuses={statuses?.statuses || []}

                currency={currency}
              />
            </div>
          </div>

          {/* Charts Section: Row 2 */}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Top 5 Products */}

            <div className="p-5 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <TrendingUp className="w-4 h-4 text-amber-500" />

                  <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                    Top Shipped Products
                  </h3>
                </div>

                <span className="text-[11px] font-semibold text-slate-400">
                  Deterministic Rank
                </span>
              </div>

              <TopProductsBarChart
                products={topProducts?.products || []}

                currency={currency}
              />
            </div>

            {/* Status Lifecycle Cards */}

            <div className="p-5 rounded-2xl bg-white dark:bg-[#07261d] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
              <div className="flex items-center space-x-2">
                <ShieldCheck className="w-4 h-4 text-emerald-500" />

                <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                  Pipeline Stage Diagnostics
                </h3>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div className="p-3.5 rounded-xl bg-teal-50/50 dark:bg-teal-950/20 border border-teal-200/60 dark:border-teal-900/40">
                  <div className="flex items-center space-x-1.5 text-teal-700 dark:text-teal-400 font-bold text-xs mb-1">
                    <CheckCircle2 className="w-3.5 h-3.5" />

                    <span>Shipped</span>
                  </div>

                  <div className="text-xl font-extrabold text-slate-900 dark:text-white">
                    {formatNumber(shippedCount)}
                  </div>

                  <div className="text-[10px] text-teal-600 dark:text-teal-400 mt-1 font-medium">
                    Counts towards sales
                  </div>
                </div>

                <div className="p-3.5 rounded-xl bg-emerald-50/50 dark:bg-emerald-950/20 border border-emerald-200/60 dark:border-emerald-900/40">
                  <div className="flex items-center space-x-1.5 text-emerald-700 dark:text-emerald-400 font-bold text-xs mb-1">
                    <Clock className="w-3.5 h-3.5" />

                    <span>Placed</span>
                  </div>

                  <div className="text-xl font-extrabold text-slate-900 dark:text-white">
                    {formatNumber(placedCount)}
                  </div>

                  <div className="text-[10px] text-slate-400 mt-1 font-medium">
                    Pending fulfillment
                  </div>
                </div>

                <div className="p-3.5 rounded-xl bg-rose-50/50 dark:bg-rose-950/20 border border-rose-200/60 dark:border-rose-900/40">
                  <div className="flex items-center space-x-1.5 text-rose-700 dark:text-rose-400 font-bold text-xs mb-1">
                    <XCircle className="w-3.5 h-3.5" />

                    <span>Cancelled</span>
                  </div>

                  <div className="text-xl font-extrabold text-slate-900 dark:text-white">
                    {formatNumber(cancelledCount)}
                  </div>

                  <div className="text-[10px] text-slate-400 mt-1 font-medium">
                    Zero revenue attributed
                  </div>
                </div>
              </div>

              <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-800/40 text-[11px] text-slate-500 dark:text-slate-400 leading-relaxed">
                All data is queried through authorized FastAPI contracts with{" "}
                <code className="text-slate-800 dark:text-slate-200">
                  REPEATABLE READ
                </code>{" "}
                transaction isolation and verified durable rollup coverage.
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
};
