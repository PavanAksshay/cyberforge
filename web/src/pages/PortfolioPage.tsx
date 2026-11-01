import React, { useState } from "react";
import {
  Building2,
  DollarSign,
  ShoppingCart,
  Search,
  ArrowRight,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { apiJson } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { TrendLineChart } from "../components/Charts";
import { formatCurrency, formatNumber, formatDateTime } from "../lib/utils";
import { Freshness, TrendBucket } from "../types";

export interface PortfolioData {
  complete: boolean;
  tenant_count: number;
  ready_tenant_count: number;
  retrieval_ms: number;
  read_completed_at: string;
  totals: { total_order_count: number; sales_amount: string } | null;
  buckets: TrendBucket[] | null;
  tenants: {
    tenant_id: string;
    name: string;
    status: "ready" | "unavailable";
    metrics: { total_order_count: number; sales_amount: string } | null;
    freshness: Freshness | null;
    error: string | null;
  }[];
}

export const PortfolioPage: React.FC<{ onDrillDown: () => void }> = ({
  onDrillDown,
}) => {
  const {
    currency,
    startDate,
    endDate,
    token,
    tenants,
    setActiveTenant,
    triggerRefresh,
  } = useApp();
  const [search, setSearch] = useState("");
  const interval = [startDate, endDate].every(
    (value) => new Date(value).getUTCHours() === 0,
  )
    ? "day"
    : "hour";
  const state = usePolling(
    `portfolio:${currency}:${startDate}:${endDate}:${token}`,
    async (signal) => {
      if (!token) return null;
      const params = new URLSearchParams({
        currency,
        start: startDate,
        end: endDate,
        interval,
      });
      return (await apiJson(
        `/analytics/portfolio?${params}`,
        token,
        signal,
      )) as PortfolioData;
    },
  );
  // Errors may retain a previous response in polling state; never present it as current.
  const data = state.error ? null : state.data;
  const rows =
    data?.tenants.filter((row) =>
      `${row.name} ${row.tenant_id}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) || [];
  const panel = "rounded-2xl border border-emerald-900 bg-[#07261d] p-5";
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm font-semibold text-emerald-400">
            PORTFOLIO COMMAND CENTER
          </p>
          <h1 className="text-3xl font-bold mt-1">All Tenants Overview</h1>
          <p className="text-slate-400 mt-2">
            Every authorized active store, together. Uses the header currency
            and UTC dates; tenant selection is for drill-down only.
          </p>
          <p className="text-xs text-slate-400 mt-2">
            UTC [{startDate}, {endDate}) · {currency} · Shipped-only sales
          </p>
        </div>
        <button
          className="rounded-xl bg-emerald-700 px-4 py-2 disabled:opacity-50"
          disabled={state.loading}
          onClick={triggerRefresh}
        >
          {state.loading ? "Reading tenant databases…" : "Refresh all tenants"}
        </button>
      </div>
      {state.error && (
        <div role="alert" className="rounded-xl bg-rose-950 p-4 text-rose-300">
          Overview unavailable: {state.error}
        </div>
      )}
      {!token && <p role="alert">Open API Bearer Token to authenticate.</p>}
      {data && (
        <>
          {!data.complete && (
            <div
              role="alert"
              className="rounded-xl border border-amber-700 bg-amber-950 p-4 text-amber-200"
            >
              {data.tenant_count - data.ready_tenant_count} tenant database(s)
              could not answer this window. Combined totals and trends are
              withheld. Ready tenant results remain visible below.
            </div>
          )}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className={panel}>
              <div className="flex gap-2 text-emerald-300">
                <Building2 size={20} />
                Authorized stores
              </div>
              <p className="text-3xl font-bold mt-3">{data.tenant_count}</p>
              <p className="text-sm text-slate-400 mt-2">
                {data.ready_tenant_count} ready for this window
              </p>
            </div>
            <div className={panel}>
              <div className="flex gap-2 text-emerald-300">
                <DollarSign size={20} />
                Combined shipped sales
              </div>
              <p className="text-2xl font-bold mt-3 break-words">
                {data.totals
                  ? formatCurrency(data.totals.sales_amount, currency)
                  : "Unavailable"}
              </p>
              <p className="text-sm text-slate-400 mt-2">
                One currency · Exact Decimal aggregation
              </p>
            </div>
            <div className={panel}>
              <div className="flex gap-2 text-emerald-300">
                <ShoppingCart size={20} />
                Combined order volume
              </div>
              <p className="text-3xl font-bold mt-3">
                {data.totals
                  ? formatNumber(data.totals.total_order_count)
                  : "Unavailable"}
              </p>
              <p className="text-sm text-slate-400 mt-2">
                Placed + shipped + cancelled
              </p>
            </div>
          </div>
          <div className="text-xs text-slate-400">
            Retrieved across {data.tenant_count} databases in{" "}
            {data.retrieval_ms.toLocaleString()} ms ·{" "}
            {formatDateTime(data.read_completed_at)} · Independent per-tenant
            snapshots; live writes may occur between database reads.
            {state.loading && " Refreshing…"}
          </div>
          {data.buckets && (
            <section className={panel}>
              <h2 className="font-bold mb-4">
                Combined shipped sales trend ·{" "}
                {interval === "day" ? "Daily" : "Hourly"}
              </h2>
              <TrendLineChart
                data={data.buckets}
                currency={currency}
                interval={interval}
              />
            </section>
          )}
          <section className={panel}>
            <div className="flex flex-wrap justify-between gap-3 mb-5">
              <h2 className="text-lg font-bold">
                Store comparison · Sales ranking
              </h2>
              <label className="flex items-center gap-2 rounded-lg bg-[#051c15] px-3">
                <Search size={16} />
                <input
                  aria-label="Search tenants"
                  className="bg-transparent py-2 outline-none"
                  placeholder="Search stores or tenant IDs"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
            </div>
            <div className="overflow-auto max-h-[36rem]">
              <table className="w-full text-sm text-left">
                <thead className="text-emerald-300 sticky top-0 bg-[#07261d]">
                  <tr>
                    {[
                      "Store",
                      "Orders",
                      `Shipped sales (${currency})`,
                      "Last applied",
                      "Status",
                      "",
                    ].map((label, i) => (
                      <th className="p-3 whitespace-nowrap" key={i}>
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr
                      key={row.tenant_id}
                      className="border-t border-emerald-900/70"
                    >
                      <td className="p-3">
                        <p className="font-semibold" title={row.name}>
                          {row.name.replace(
                            /^Synthetic .+ \/ (\d+)$/,
                            "Enterprise Tenant #$1",
                          )}
                        </p>
                        <p className="font-mono text-xs text-slate-500">
                          {row.tenant_id}
                        </p>
                      </td>
                      <td className="p-3">
                        {row.metrics
                          ? formatNumber(row.metrics.total_order_count)
                          : "—"}
                      </td>
                      <td className="p-3 whitespace-nowrap">
                        {row.metrics
                          ? formatCurrency(row.metrics.sales_amount, currency)
                          : "—"}
                      </td>
                      <td className="p-3 text-xs">
                        {row.freshness?.last_processed_at
                          ? formatDateTime(row.freshness.last_processed_at)
                          : "Not supplied"}
                      </td>
                      <td className="p-3">
                        <span
                          className={
                            row.status === "ready"
                              ? "text-emerald-400"
                              : "text-amber-300"
                          }
                        >
                          {row.status}
                        </span>
                        {row.error && (
                          <p className="text-xs text-amber-200">{row.error}</p>
                        )}
                      </td>
                      <td className="p-3">
                        <button
                          aria-label={`Open ${row.name}`}
                          className="flex items-center gap-1 text-emerald-300"
                          onClick={() => {
                            const tenant = tenants.find(
                              (t) => t.id === row.tenant_id,
                            );
                            if (tenant) {
                              setActiveTenant(tenant);
                              onDrillDown();
                            }
                          }}
                        >
                          Open <ArrowRight size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {rows.length === 0 && (
              <p className="p-4 text-slate-400">
                No matching authorized stores.
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
};
