import React, { useState } from "react";
import { TrendBucket, ProductMetric, StatusMetric, Currency } from "../types";
import {
  formatCurrency,
  formatNumber,
  formatDate,
  truncateId,
} from "../lib/utils";

// --- TREND LINE & AREA CHART ---
interface TrendLineChartProps {
  data: TrendBucket[];
  currency: Currency;
  interval: "hour" | "day";
}

export const TrendLineChart: React.FC<TrendLineChartProps> = ({
  data,
  currency,
}) => {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  if (!data || data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-slate-400 dark:text-slate-500 text-xs">
        No time-series data available for this range.
      </div>
    );
  }

  const values = data.map((d) => parseFloat(d.sales_amount) || 0);
  const maxVal = Math.max(...values, 100);
  const minVal = 0;

  const width = 640;
  const height = 240;
  const padLeft = 45;
  const padBottom = 30;
  const padTop = 20;
  const padRight = 20;

  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  const points = data.map((d, i) => {
    const x = padLeft + (i / Math.max(data.length - 1, 1)) * chartW;
    const yVal = parseFloat(d.sales_amount) || 0;
    const y = padTop + chartH - ((yVal - minVal) / (maxVal - minVal)) * chartH;
    return { x, y, bucket: d };
  });

  const pathD = points.reduce((acc, p, i) => {
    if (i === 0) return `M ${p.x} ${p.y}`;
    const prev = points[i - 1];
    const cx = (prev.x + p.x) / 2;
    return `${acc} C ${cx} ${prev.y}, ${cx} ${p.y}, ${p.x} ${p.y}`;
  }, "");

  const areaD = `${pathD} L ${points[points.length - 1].x} ${padTop + chartH} L ${points[0].x} ${padTop + chartH} Z`;

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-auto overflow-visible select-none"
      >
        <defs>
          <linearGradient id="salesGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#10B981" stopOpacity="0.28" />
            <stop offset="100%" stopColor="#10B981" stopOpacity="0.0" />
          </linearGradient>
        </defs>

        {/* Grid lines */}
        {[0, 0.25, 0.5, 0.75, 1].map((pct, i) => {
          const y = padTop + chartH * (1 - pct);
          const val = minVal + (maxVal - minVal) * pct;
          return (
            <g key={i}>
              <line
                x1={padLeft}
                y1={y}
                x2={width - padRight}
                y2={y}
                stroke="currentColor"
                className="text-slate-200 dark:text-slate-800"
                strokeDasharray="4 4"
                strokeWidth="1"
              />
              <text
                x={padLeft - 8}
                y={y + 3}
                textAnchor="end"
                className="fill-slate-400 dark:fill-slate-500 text-[10px] font-mono"
              >
                {formatCurrency(val, currency).slice(0, -3)}
              </text>
            </g>
          );
        })}

        {/* Shaded Area */}
        <path d={areaD} fill="url(#salesGrad)" />

        {/* Smooth Line */}
        <path
          d={pathD}
          fill="none"
          stroke="#10B981"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />

        {/* Interactive Data Points */}
        {points.map((p, i) => (
          <g key={i}>
            <circle
              cx={p.x}
              cy={p.y}
              r={hoveredIdx === i ? 5.5 : 3}
              className={`transition-all duration-150 ${
                hoveredIdx === i
                  ? "fill-emerald-600 stroke-white stroke-2 shadow-md"
                  : "fill-emerald-500 hover:fill-emerald-600"
              }`}
              onMouseEnter={() => setHoveredIdx(i)}
              onMouseLeave={() => setHoveredIdx(null)}
            />
            {/* Show sample x labels */}
            {(i === 0 ||
              i === Math.floor(points.length / 2) ||
              i === points.length - 1) && (
              <text
                x={p.x}
                y={height - 8}
                textAnchor="middle"
                className="fill-slate-400 dark:fill-slate-500 text-[10px] font-medium"
              >
                {formatDate(p.bucket.bucket_start)}
              </text>
            )}
          </g>
        ))}
      </svg>

      {/* Floating Hover Tooltip */}
      {hoveredIdx !== null && points[hoveredIdx] && (
        <div
          className="absolute z-20 pointer-events-none p-2.5 rounded-xl bg-slate-900 text-white shadow-xl text-xs space-y-1 animate-in fade-in zoom-in-95"
          style={{
            left: `${(points[hoveredIdx].x / width) * 100}%`,
            top: `${(points[hoveredIdx].y / height) * 100 - 30}%`,
            transform: "translate(-50%, -100%)",
          }}
        >
          <div className="text-[10px] text-slate-400 font-medium">
            {new Date(points[hoveredIdx].bucket.bucket_start).toLocaleString(
              "en-US",
              {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                timeZone: "UTC",
              },
            )}{" "}
            UTC
          </div>
          <div className="font-bold text-sm text-emerald-400">
            {formatCurrency(points[hoveredIdx].bucket.sales_amount, currency)}
          </div>
          <div className="text-[11px] text-slate-300">
            Orders:{" "}
            <strong className="text-white">
              {points[hoveredIdx].bucket.total_order_count}
            </strong>
          </div>
        </div>
      )}
    </div>
  );
};

// --- TOP PRODUCTS BAR CHART (DISTINCT RANKED METER GRAPH FOR OVERVIEW) ---
interface TopProductsBarChartProps {
  products: ProductMetric[];
  currency: Currency;
}

export const TopProductsBarChart: React.FC<TopProductsBarChartProps> = ({
  products,
  currency,
}) => {
  const [hoveredProduct, setHoveredProduct] = useState<string | null>(null);

  if (!products || products.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-56 text-slate-400 text-sm">
        No shipped products recorded in this window.
      </div>
    );
  }

  const maxSales = Math.max(
    ...products.map((p) => parseFloat(p.sales_amount) || 0),
    1,
  );
  const totalSales = products.reduce(
    (acc, p) => acc + (parseFloat(p.sales_amount) || 0),
    0,
  );

  return (
    <div className="space-y-4">
      {products.slice(0, 5).map((prod, idx) => {
        const sales = parseFloat(prod.sales_amount) || 0;
        const pct = Math.min(100, Math.max(8, (sales / maxSales) * 100));
        const sharePct =
          totalSales > 0 ? ((sales / totalSales) * 100).toFixed(1) : "0.0";
        const isHovered = hoveredProduct === prod.product_id;

        const rankBadge =
          idx === 0
            ? "bg-amber-400/20 text-amber-300 border-amber-400/60 shadow-amber-950/40"
            : idx === 1
              ? "bg-slate-300/20 text-slate-200 border-slate-300/50 shadow-slate-950/40"
              : idx === 2
                ? "bg-amber-700/20 text-amber-500 border-amber-600/50 shadow-amber-950/40"
                : "bg-emerald-950/40 text-emerald-300 border-emerald-800/40";

        return (
          <div
            key={prod.product_id}
            onMouseEnter={() => setHoveredProduct(prod.product_id)}
            onMouseLeave={() => setHoveredProduct(null)}
            className={`p-3.5 rounded-2xl border transition-all duration-200 ${
              isHovered
                ? "bg-[#082e22] border-emerald-500 shadow-md shadow-emerald-950/40 scale-[1.01]"
                : "bg-[#062118]/80 border-[#0d3d2f] hover:border-emerald-700/60"
            }`}
          >
            {/* Top Row: Rank, ID, Orders, Revenue */}
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center space-x-2.5">
                <span
                  className={`w-6 h-6 flex items-center justify-center rounded-lg font-black text-xs border font-mono shadow-sm ${rankBadge}`}
                >
                  #{idx + 1}
                </span>
                <span className="font-mono text-sm font-bold text-white tracking-wide">
                  {prod.product_name || "Uncataloged product"}
                </span>
                <span className="px-2.5 py-0.5 rounded-full text-[11px] font-mono font-semibold bg-emerald-950/80 text-emerald-300 border border-emerald-800/60">
                  {formatNumber(prod.total_order_count)} orders
                </span>
              </div>

              <div className="flex items-center space-x-3">
                <span className="text-xs font-mono font-medium text-slate-400">
                  {sharePct}% of displayed products
                </span>
                <span className="font-mono font-black text-sm sm:text-base text-emerald-300">
                  {formatCurrency(prod.sales_amount, currency)}
                </span>
              </div>
            </div>

            {/* Bottom Row: High-Impact Meter Bar */}
            <div className="w-full h-3 rounded-full bg-[#03140e] p-0.5 border border-[#0d3d2f] overflow-hidden">
              <div
                className="h-full rounded-full bg-gradient-to-r from-emerald-500 via-teal-400 to-emerald-300 transition-all duration-500 ease-out shadow-[0_0_8px_rgba(52,211,153,0.5)]"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
};

// --- STATUS DONUT CHART (ENLARGED & HIGH CONTRAST FOR OVERVIEW) ---
interface StatusDonutChartProps {
  statuses: StatusMetric[];
  currency: Currency;
}

export const StatusDonutChart: React.FC<StatusDonutChartProps> = ({
  statuses,
  currency,
}) => {
  const totalOrders = statuses.reduce(
    (acc, s) => acc + (s.total_order_count || 0),
    0,
  );

  const colors: Record<
    string,
    { stroke: string; bg: string; text: string; label: string }
  > = {
    shipped: {
      stroke: "#10B981",
      bg: "bg-emerald-950/80 border-emerald-700/60",
      text: "text-emerald-300",
      label: "Shipped (Accrued Sales)",
    },
    placed: {
      stroke: "#06B6D4",
      bg: "bg-cyan-950/80 border-cyan-700/60",
      text: "text-cyan-300",
      label: "Placed (Pending)",
    },
    cancelled: {
      stroke: "#F43F5E",
      bg: "bg-rose-950/80 border-rose-700/60",
      text: "text-rose-300",
      label: "Cancelled (Voided)",
    },
  };

  // Enlarge radius and stroke for prominent display
  const radius = 80;
  const strokeWidth = 22;
  const circumference = 2 * Math.PI * radius;

  let currentAngle = 0;
  const slices = statuses.map((s) => {
    const count = s.total_order_count || 0;
    const ratio = totalOrders > 0 ? count / totalOrders : 0;
    const strokeDasharray = `${ratio * circumference} ${circumference}`;
    const strokeDashoffset = -currentAngle * circumference;
    currentAngle += ratio;

    return {
      status: s.status,
      count,
      ratio,
      sales: s.sales_amount,
      strokeDasharray,
      strokeDashoffset,
      color: colors[s.status] || colors.placed,
    };
  });

  const shippedSlice = slices.find((s) => s.status === "shipped");
  const fulfillmentPct = shippedSlice
    ? Math.round(shippedSlice.ratio * 100)
    : 0;

  return (
    <div className="flex flex-col items-center justify-center gap-6 p-2">
      {/* Enlarged SVG Donut */}
      <div className="relative w-56 h-56 sm:w-64 sm:h-64 shrink-0">
        <svg viewBox="0 0 220 220" className="w-full h-full -rotate-90">
          <circle
            cx="110"
            cy="110"
            r={radius}
            fill="none"
            stroke="#041a13"
            strokeWidth={strokeWidth}
          />
          {totalOrders > 0 &&
            slices.map((slice) => (
              <circle
                key={slice.status}
                cx="110"
                cy="110"
                r={radius}
                fill="none"
                stroke={slice.color.stroke}
                strokeWidth={strokeWidth}
                strokeDasharray={slice.strokeDasharray}
                strokeDashoffset={slice.strokeDashoffset}
                strokeLinecap="round"
                className="transition-all duration-700"
              />
            ))}
        </svg>

        {/* Center summary */}
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none text-center px-4">
          <span className="text-2xl sm:text-3xl font-black text-white font-mono tracking-tight">
            {formatNumber(totalOrders)}
          </span>
          <span className="text-xs font-bold uppercase tracking-wider text-emerald-400 mt-0.5">
            Total Orders
          </span>
          <span className="text-xs font-mono text-slate-300 font-semibold">
            {fulfillmentPct}% Fulfilled
          </span>
        </div>
      </div>

      {/* Spacious Legend & Breakdown Cards */}
      <div className="w-full space-y-3">
        {slices.map((s) => {
          const pct = Math.round(s.ratio * 100);
          return (
            <div
              key={s.status}
              className={`p-3.5 rounded-2xl border ${s.color.bg} transition duration-200 hover:scale-[1.01]`}
            >
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center space-x-2.5">
                  <span
                    className="w-3 h-3 rounded-full shrink-0 shadow-sm"
                    style={{ backgroundColor: s.color.stroke }}
                  />
                  <span
                    className={`text-sm font-extrabold capitalize ${s.color.text}`}
                  >
                    {s.status}
                  </span>
                </div>
                <span className="text-xs font-mono font-bold text-slate-300">
                  {pct}% of volume
                </span>
              </div>

              <div className="flex items-center justify-between text-xs font-mono pt-1 border-t border-white/10">
                <span className="text-slate-300 font-semibold">
                  {formatNumber(s.count)} orders
                </span>
                {s.status === "shipped" ? (
                  <span className="font-black text-sm text-emerald-300">
                    {formatCurrency(s.sales, currency)}
                  </span>
                ) : (
                  <span className="text-slate-400 text-[11px] italic font-sans">
                    Excluded from sales
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

// --- ADVANCED LIFECYCLE BREAKDOWN CHART (DEDICATED VISUAL GRAPH FOR ADVANCED ANALYTICS) ---
export interface AdvancedLifecycleBreakdownChartProps {
  statuses: StatusMetric[];
  currency: Currency;
}

export const AdvancedLifecycleBreakdownChart: React.FC<
  AdvancedLifecycleBreakdownChartProps
> = ({ statuses, currency }) => {
  const totalOrders = statuses.reduce(
    (acc, s) => acc + (s.total_order_count || 0),
    0,
  );

  const colors: Record<
    string,
    { stroke: string; bg: string; border: string; text: string; glow: string }
  > = {
    shipped: {
      stroke: "#10B981",
      bg: "bg-emerald-950/60",
      border: "border-emerald-700/60",
      text: "text-emerald-300",
      glow: "shadow-emerald-950/50",
    },
    placed: {
      stroke: "#06B6D4",
      bg: "bg-cyan-950/60",
      border: "border-cyan-700/60",
      text: "text-cyan-300",
      glow: "shadow-cyan-950/50",
    },
    cancelled: {
      stroke: "#F43F5E",
      bg: "bg-rose-950/60",
      border: "border-rose-700/60",
      text: "text-rose-300",
      glow: "shadow-rose-950/50",
    },
  };

  const radius = 80;
  const strokeWidth = 24;
  const circumference = 2 * Math.PI * radius;

  let currentAngle = 0;
  const slices = statuses.map((s) => {
    const count = s.total_order_count || 0;
    const ratio = totalOrders > 0 ? count / totalOrders : 0;
    const strokeDasharray = `${ratio * circumference} ${circumference}`;
    const strokeDashoffset = -currentAngle * circumference;
    currentAngle += ratio;

    return {
      status: s.status,
      count,
      ratio,
      sales: s.sales_amount,
      strokeDasharray,
      strokeDashoffset,
      color: colors[s.status] || colors.placed,
    };
  });

  const shippedSlice = slices.find((s) => s.status === "shipped");
  const placedSlice = slices.find((s) => s.status === "placed");
  const cancelledSlice = slices.find((s) => s.status === "cancelled");

  const fulfillmentRate = shippedSlice
    ? (shippedSlice.ratio * 100).toFixed(1)
    : "0.0";
  const pendingRate = placedSlice
    ? (placedSlice.ratio * 100).toFixed(1)
    : "0.0";
  const cancellationRate = cancelledSlice
    ? (cancelledSlice.ratio * 100).toFixed(1)
    : "0.0";

  return (
    <div className="space-y-6">
      {/* KPI Highlight Strip */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c] space-y-1 shadow-sm">
          <div className="text-xs font-bold uppercase tracking-wider text-emerald-400">
            Fulfillment Rate
          </div>
          <div className="text-2xl font-black text-white font-mono">
            {fulfillmentRate}%
          </div>
          <div className="text-xs text-slate-300 font-medium">
            Successfully shipped orders
          </div>
        </div>

        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c] space-y-1 shadow-sm">
          <div className="text-xs font-bold uppercase tracking-wider text-emerald-400">
            Recognized Sales
          </div>
          <div className="text-2xl font-black text-emerald-300 font-mono">
            {formatCurrency(shippedSlice?.sales || "0.00", currency)}
          </div>
          <div className="text-xs text-slate-300 font-medium">
            Accrued under frozen semantics
          </div>
        </div>

        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c] space-y-1 shadow-sm">
          <div className="text-xs font-bold uppercase tracking-wider text-cyan-400">
            In-Flight / Placed
          </div>
          <div className="text-2xl font-black text-cyan-300 font-mono">
            {formatNumber(placedSlice?.count || 0)}
          </div>
          <div className="text-xs text-slate-300 font-medium">
            {pendingRate}% awaiting fulfillment
          </div>
        </div>

        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c] space-y-1 shadow-sm">
          <div className="text-xs font-bold uppercase tracking-wider text-rose-400">
            Cancelled / Voided
          </div>
          <div className="text-2xl font-black text-rose-300 font-mono">
            {formatNumber(cancelledSlice?.count || 0)}
          </div>
          <div className="text-xs text-slate-300 font-medium">
            {cancellationRate}% dropped orders
          </div>
        </div>
      </div>

      {/* Side-by-Side Visuals: Large Donut + Conversion Funnel Flow */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 p-6 rounded-3xl bg-[#062118] border border-[#0d3d2f] shadow-lg">
        {/* Polar Donut Breakdown */}
        <div className="lg:col-span-5 flex flex-col items-center justify-center p-4 border-b lg:border-b-0 lg:border-r border-[#0d3d2f]">
          <div className="text-sm font-extrabold uppercase tracking-wider text-slate-300 mb-3 text-center">
            Order Volume Distribution
          </div>
          <div className="relative w-64 h-64">
            <svg viewBox="0 0 220 220" className="w-full h-full -rotate-90">
              <circle
                cx="110"
                cy="110"
                r={radius}
                fill="none"
                stroke="#041a13"
                strokeWidth={strokeWidth}
              />
              {totalOrders > 0 &&
                slices.map((slice) => (
                  <circle
                    key={slice.status}
                    cx="110"
                    cy="110"
                    r={radius}
                    fill="none"
                    stroke={slice.color.stroke}
                    strokeWidth={strokeWidth}
                    strokeDasharray={slice.strokeDasharray}
                    strokeDashoffset={slice.strokeDashoffset}
                    strokeLinecap="round"
                    className="transition-all duration-700"
                  />
                ))}
            </svg>

            {/* Center summary */}
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none text-center px-4">
              <span className="text-3xl font-black text-white font-mono tracking-tight">
                {formatNumber(totalOrders)}
              </span>
              <span className="text-xs font-bold uppercase tracking-wider text-emerald-400 mt-1">
                Orders Analyzed
              </span>
              <span className="text-xs font-mono text-slate-300 mt-0.5">
                100% Ingested
              </span>
            </div>
          </div>
        </div>

        {/* Lifecycle Conversion Flow Cards */}
        <div className="lg:col-span-7 space-y-4 flex flex-col justify-center">
          <div className="text-sm font-extrabold uppercase tracking-wider text-slate-300 mb-1">
            Transactional Lifecycle & Revenue Accrual Flow
          </div>

          {/* Step 1: Placed */}
          <div className="p-4 rounded-2xl bg-cyan-950/40 border border-cyan-800/60 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2.5">
                <span className="px-2.5 py-0.5 rounded-full text-xs font-black uppercase bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 font-mono">
                  Stage 1: Placed
                </span>
                <span className="text-sm font-extrabold text-white">
                  Order Created & Committed
                </span>
              </div>
              <span className="font-mono font-black text-sm text-cyan-300">
                {formatNumber(placedSlice?.count || 0)} orders ({pendingRate}%)
              </span>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              Orders start here upon transactional commit in the tenant
              database. At this stage, amounts are not yet recognized as
              realized revenue.
            </p>
          </div>

          {/* Step 2: Shipped (Accrued) */}
          <div className="p-4 rounded-2xl bg-emerald-950/40 border border-emerald-700/60 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2.5">
                <span className="px-2.5 py-0.5 rounded-full text-xs font-black uppercase bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-mono">
                  Stage 2A: Shipped
                </span>
                <span className="text-sm font-extrabold text-white">
                  Recognized Financial Revenue
                </span>
              </div>
              <span className="font-mono font-black text-sm text-emerald-300">
                {formatNumber(shippedSlice?.count || 0)} orders (
                {fulfillmentRate}%)
              </span>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              Orders reaching shipped status accrue into{" "}
              <code className="font-mono text-emerald-400">
                HourlySummaryAggregate
              </code>{" "}
              and{" "}
              <code className="font-mono text-emerald-400">
                ProductAggregate
              </code>
              . Total accrued revenue:{" "}
              <strong className="text-white font-mono font-bold">
                {formatCurrency(shippedSlice?.sales || "0.00", currency)}
              </strong>
              .
            </p>
          </div>

          {/* Step 3: Cancelled (Voided) */}
          <div className="p-4 rounded-2xl bg-rose-950/40 border border-rose-800/60 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2.5">
                <span className="px-2.5 py-0.5 rounded-full text-xs font-black uppercase bg-rose-500/20 text-rose-300 border border-rose-500/40 font-mono">
                  Stage 2B: Cancelled
                </span>
                <span className="text-sm font-extrabold text-white">
                  Voided & Excluded
                </span>
              </div>
              <span className="font-mono font-black text-sm text-rose-300">
                {formatNumber(cancelledSlice?.count || 0)} orders (
                {cancellationRate}%)
              </span>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              Cancelled orders are strictly excluded from financial sales
              metrics while being accurately tracked in lifecycle volume
              analytics.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};

// --- ADVANCED DUAL-AXIS COMPOSITE CHART (SALES AREA + ORDER VOLUME BARS) ---
export interface DualAxisAreaBarChartProps {
  data: TrendBucket[];
  currency: Currency;
  interval: "hour" | "day";
}

export const DualAxisAreaBarChart: React.FC<DualAxisAreaBarChartProps> = ({
  data,
  currency,
  interval,
}) => {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const [displayMode, setDisplayMode] = useState<"both" | "sales" | "volume">(
    "both",
  );

  if (!data || data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-72 text-slate-400 dark:text-slate-500 text-xs">
        No time-series analytics available for this range.
      </div>
    );
  }

  const salesValues = data.map((d) => parseFloat(d.sales_amount) || 0);
  const volumeValues = data.map((d) => d.total_order_count || 0);

  const maxSales = Math.max(...salesValues, 100);
  const maxVolume = Math.max(...volumeValues, 10);
  const totalSales = salesValues.reduce((a, b) => a + b, 0);
  const totalVolume = volumeValues.reduce((a, b) => a + b, 0);
  const avgTicket = totalVolume > 0 ? totalSales / totalVolume : 0;

  const width = 800;
  const height = 300;
  const padLeft = 60;
  const padRight = 60;
  const padTop = 30;
  const padBottom = 40;

  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;
  const barWidth = Math.max(Math.min(chartW / data.length - 8, 36), 6);

  const points = data.map((d, i) => {
    const x = padLeft + (i / Math.max(data.length - 1, 1)) * chartW;
    const yVal = parseFloat(d.sales_amount) || 0;
    const y = padTop + chartH - (yVal / maxSales) * chartH;
    const barH = (d.total_order_count / maxVolume) * (chartH * 0.75); // max 75% height
    return { x, y, barH, bucket: d };
  });

  const pathD = points.reduce((acc, p, i) => {
    if (i === 0) return `M ${p.x} ${p.y}`;
    const prev = points[i - 1];
    const cx = (prev.x + p.x) / 2;
    return `${acc} C ${cx} ${prev.y}, ${cx} ${p.y}, ${p.x} ${p.y}`;
  }, "");

  const areaD = `${pathD} L ${points[points.length - 1].x} ${padTop + chartH} L ${points[0].x} ${padTop + chartH} Z`;

  return (
    <div className="space-y-4">
      {/* Top KPI Summary Ribbon */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
        <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-800">
          <div className="text-[10px] uppercase font-bold text-slate-400">
            Total Period Sales
          </div>
          <div className="text-base font-extrabold text-emerald-500 font-mono mt-0.5">
            {formatCurrency(totalSales, currency)}
          </div>
        </div>
        <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-800">
          <div className="text-[10px] uppercase font-bold text-slate-400">
            Total Volume
          </div>
          <div className="text-base font-extrabold text-teal-400 font-mono mt-0.5">
            {formatNumber(totalVolume)} orders
          </div>
        </div>
        <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-800">
          <div className="text-[10px] uppercase font-bold text-slate-400">
            Average Ticket (AOV)
          </div>
          <div className="text-base font-extrabold text-slate-900 dark:text-white font-mono mt-0.5">
            {formatCurrency(avgTicket, currency)}
          </div>
        </div>
        <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-800 flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase font-bold text-slate-400">
              Active View
            </div>
            <div className="flex items-center space-x-1 mt-1">
              <button
                onClick={() => setDisplayMode("both")}
                className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                  displayMode === "both"
                    ? "bg-emerald-600 text-white"
                    : "bg-slate-800 text-slate-400"
                }`}
              >
                Dual
              </button>
              <button
                onClick={() => setDisplayMode("sales")}
                className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                  displayMode === "sales"
                    ? "bg-emerald-600 text-white"
                    : "bg-slate-800 text-slate-400"
                }`}
              >
                Sales
              </button>
              <button
                onClick={() => setDisplayMode("volume")}
                className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                  displayMode === "volume"
                    ? "bg-teal-600 text-white"
                    : "bg-slate-800 text-slate-400"
                }`}
              >
                Orders
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* SVG Canvas */}
      <div className="relative w-full overflow-hidden">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full h-auto select-none overflow-visible"
        >
          <defs>
            <linearGradient id="advAreaGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10B981" stopOpacity="0.45" />
              <stop offset="60%" stopColor="#10B981" stopOpacity="0.12" />
              <stop offset="100%" stopColor="#10B981" stopOpacity="0.0" />
            </linearGradient>
            <linearGradient id="advBarGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#0D9488" stopOpacity="0.8" />
              <stop offset="100%" stopColor="#042F2E" stopOpacity="0.4" />
            </linearGradient>
          </defs>

          {/* Grid lines */}
          {[0, 0.25, 0.5, 0.75, 1].map((pct, idx) => {
            const y = padTop + chartH * (1 - pct);
            const salesLabel = formatCurrency(maxSales * pct, currency).slice(
              0,
              -3,
            );
            const volLabel = Math.round(maxVolume * pct);
            return (
              <g key={idx}>
                <line
                  x1={padLeft}
                  y1={y}
                  x2={width - padRight}
                  y2={y}
                  stroke="currentColor"
                  className="text-slate-100 dark:text-slate-800/80"
                  strokeDasharray="4 4"
                />
                {(displayMode === "both" || displayMode === "sales") && (
                  <text
                    x={padLeft - 8}
                    y={y + 4}
                    textAnchor="end"
                    className="text-[10px] fill-slate-400 font-mono font-medium"
                  >
                    {salesLabel}
                  </text>
                )}
                {(displayMode === "both" || displayMode === "volume") && (
                  <text
                    x={width - padRight + 8}
                    y={y + 4}
                    textAnchor="start"
                    className="text-[10px] fill-teal-400 font-mono font-medium"
                  >
                    {volLabel}
                  </text>
                )}
              </g>
            );
          })}

          {/* Background Volume Bars */}
          {(displayMode === "both" || displayMode === "volume") &&
            points.map((p, i) => {
              const bx = p.x - barWidth / 2;
              const by = padTop + chartH - p.barH;
              const isHover = hoveredIdx === i;
              return (
                <rect
                  key={i}
                  x={bx}
                  y={by}
                  width={barWidth}
                  height={p.barH}
                  rx={4}
                  fill="url(#advBarGrad)"
                  className={`transition-all duration-150 cursor-pointer ${
                    isHover
                      ? "opacity-100 filter drop-shadow(0 0 6px rgba(13,148,136,0.6))"
                      : "opacity-70"
                  }`}
                  onMouseEnter={() => setHoveredIdx(i)}
                  onMouseLeave={() => setHoveredIdx(null)}
                />
              );
            })}

          {/* Foreground Sales Area & Line */}
          {(displayMode === "both" || displayMode === "sales") && (
            <>
              <path d={areaD} fill="url(#advAreaGrad)" />
              <path
                d={pathD}
                fill="none"
                stroke="#10B981"
                strokeWidth={2.5}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              {points.map((p, i) => (
                <circle
                  key={i}
                  cx={p.x}
                  cy={p.y}
                  r={hoveredIdx === i ? 6 : 3.5}
                  fill="#051b14"
                  stroke="#10B981"
                  strokeWidth={hoveredIdx === i ? 3 : 2}
                  className="transition-all duration-150 cursor-pointer"
                  onMouseEnter={() => setHoveredIdx(i)}
                  onMouseLeave={() => setHoveredIdx(null)}
                />
              ))}
            </>
          )}

          {/* X-Axis Labels */}
          {points.map((p, i) => {
            if (data.length > 10 && i % 2 !== 0 && i !== data.length - 1)
              return null;
            return (
              <text
                key={i}
                x={p.x}
                y={padTop + chartH + 20}
                textAnchor="middle"
                className="text-[10px] fill-slate-400 font-mono"
              >
                {formatDate(p.bucket.bucket_start)}
              </text>
            );
          })}
        </svg>

        {/* Hover Tooltip Card */}
        {hoveredIdx !== null && points[hoveredIdx] && (
          <div
            className="absolute z-20 pointer-events-none p-3 rounded-xl bg-slate-900/95 text-white border border-emerald-500/40 shadow-xl backdrop-blur-md text-xs space-y-1 transform -translate-x-1/2 -translate-y-full font-mono"
            style={{
              left: `${(points[hoveredIdx].x / width) * 100}%`,
              top: `${(points[hoveredIdx].y / height) * 100 - 10}%`,
            }}
          >
            <div className="text-[10px] text-slate-400 font-sans pb-1 border-b border-slate-800">
              {new Date(points[hoveredIdx].bucket.bucket_start).toLocaleString(
                "en-US",
                {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                  timeZone: "UTC",
                },
              )}{" "}
              UTC
            </div>
            <div className="flex items-center justify-between gap-4 pt-0.5">
              <span className="text-emerald-400 font-bold">Shipped Sales:</span>
              <span>
                {formatCurrency(
                  points[hoveredIdx].bucket.sales_amount,
                  currency,
                )}
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="text-teal-400 font-bold">Orders Volume:</span>
              <span>
                {formatNumber(points[hoveredIdx].bucket.total_order_count)}{" "}
                orders
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

// --- ADVANCED PRODUCT PERFORMANCE MATRIX CHART (FOR ADVANCED ANALYTICS) ---
export interface AdvancedProductMatrixChartProps {
  products: ProductMetric[];
  currency: Currency;
}

export const AdvancedProductMatrixChart: React.FC<
  AdvancedProductMatrixChartProps
> = ({ products, currency }) => {
  const [selectedProduct, setSelectedProduct] = useState<ProductMetric | null>(
    null,
  );

  if (!products || products.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-slate-400 text-sm">
        No product analytics recorded in this window.
      </div>
    );
  }

  const totalTopSales = products.reduce(
    (sum, p) => sum + (parseFloat(p.sales_amount) || 0),
    0,
  );
  const totalTopOrders = products.reduce(
    (sum, p) => sum + (p.total_order_count || 0),
    0,
  );
  const maxSales = Math.max(
    ...products.map((p) => parseFloat(p.sales_amount) || 0),
    1,
  );
  const maxVolume = Math.max(
    ...products.map((p) => p.total_order_count || 0),
    1,
  );

  return (
    <div className="space-y-6">
      {/* KPI Header Bar */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c]">
          <div className="text-xs uppercase font-bold text-emerald-400">
            Top 10 Aggregate Revenue
          </div>
          <div className="text-xl font-extrabold text-white font-mono mt-1">
            {formatCurrency(totalTopSales, currency)}
          </div>
          <div className="text-xs text-slate-300 mt-1">
            Cumulative sales across top 10 catalog items
          </div>
        </div>
        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c]">
          <div className="text-xs uppercase font-bold text-teal-400">
            Top 10 Shipped Units
          </div>
          <div className="text-xl font-extrabold text-white font-mono mt-1">
            {formatNumber(totalTopOrders)} units
          </div>
          <div className="text-xs text-slate-300 mt-1">
            Shipped order items recorded
          </div>
        </div>
        <div className="p-4 rounded-2xl bg-[#082920] border border-[#114f3c]">
          <div className="text-xs uppercase font-bold text-emerald-300">
            Catalog Leader Contribution
          </div>
          <div className="text-xl font-extrabold text-emerald-400 font-mono mt-1">
            {totalTopSales > 0
              ? `${Math.round(((parseFloat(products[0]?.sales_amount) || 0) / totalTopSales) * 100)}%`
              : "0%"}
          </div>
          <div className="text-xs text-slate-300 mt-1">
            Share held by #1 ranked SKU
          </div>
        </div>
      </div>

      {/* Visual Product Comparative Lollipop / Bar SVG Chart */}
      <div className="p-5 rounded-2xl bg-[#07261d] border border-[#114f3c] space-y-3">
        <div className="flex items-center justify-between text-xs pb-2 border-b border-[#0f4838]">
          <span className="font-bold text-sm text-white">
            SKU Volume vs Revenue Comparison
          </span>
          <span className="text-xs text-emerald-400">
            Bar = Revenue ($) | Circle Radius = Volume
          </span>
        </div>

        <div className="h-44 w-full">
          <svg
            viewBox="0 0 700 140"
            className="w-full h-full select-none overflow-visible"
          >
            {products.slice(0, 10).map((prod, idx) => {
              const sales = parseFloat(prod.sales_amount) || 0;
              const x = 35 + idx * 70;
              const barH = (sales / maxSales) * 90;
              const y = 110 - barH;
              const r = Math.max(
                5,
                Math.min(14, (prod.total_order_count / maxVolume) * 14),
              );
              const isHover = selectedProduct?.product_id === prod.product_id;

              return (
                <g
                  key={prod.product_id}
                  className="cursor-pointer group"
                  onClick={() => setSelectedProduct(prod)}
                >
                  {/* Stem line */}
                  <line
                    x1={x}
                    y1={115}
                    x2={x}
                    y2={y}
                    stroke={isHover ? "#34D399" : "#059669"}
                    strokeWidth={isHover ? 3.5 : 2.5}
                    className="transition-all"
                  />
                  {/* Head circle */}
                  <circle
                    cx={x}
                    cy={y}
                    r={r}
                    fill={idx === 0 ? "#F59E0B" : "#10B981"}
                    stroke="#041712"
                    strokeWidth={2}
                    className="transition-all group-hover:scale-125"
                  />
                  {/* Product Rank Label */}
                  <text
                    x={x}
                    y={130}
                    textAnchor="middle"
                    className="text-[11px] font-mono font-bold fill-slate-300"
                  >
                    #{idx + 1}
                  </text>
                  {/* Value on top */}
                  <text
                    x={x}
                    y={y - 8}
                    textAnchor="middle"
                    className="text-[10px] font-mono font-semibold fill-emerald-300"
                  >
                    {formatCurrency(sales, currency).slice(0, -3)}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      </div>

      {/* Ranked Product Matrix Table */}
      <div className="overflow-x-auto rounded-2xl border border-[#114f3c]">
        <table className="w-full text-sm text-left">
          <thead className="bg-[#051c15] text-slate-300 uppercase tracking-wider text-xs font-bold">
            <tr>
              <th className="px-5 py-3.5">Rank</th>
              <th className="px-5 py-3.5">Product Identifier</th>
              <th className="px-5 py-3.5">Shipped Revenue</th>
              <th className="px-5 py-3.5">Revenue Share</th>
              <th className="px-5 py-3.5">Volume Sold</th>
              <th className="px-5 py-3.5">Avg Unit Price (ASP)</th>
              <th className="px-5 py-3.5">Category Tier</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[#0f4838] bg-[#07261d] text-slate-200 font-mono text-xs">
            {products.map((prod, idx) => {
              const sales = parseFloat(prod.sales_amount) || 0;
              const sharePct =
                totalTopSales > 0
                  ? ((sales / totalTopSales) * 100).toFixed(1)
                  : "0";
              const asp =
                prod.total_order_count > 0 ? sales / prod.total_order_count : 0;
              const isFirst = idx === 0;

              return (
                <tr
                  key={prod.product_id}
                  className={`hover:bg-[#0c382b] transition cursor-pointer ${
                    selectedProduct?.product_id === prod.product_id
                      ? "bg-[#0e4233]"
                      : ""
                  }`}
                  onClick={() => setSelectedProduct(prod)}
                >
                  <td className="px-5 py-3.5">
                    <span
                      className={`inline-flex items-center justify-center w-6 h-6 rounded-lg text-xs font-extrabold ${
                        isFirst
                          ? "bg-amber-400 text-slate-950 font-black"
                          : idx === 1
                            ? "bg-slate-300 text-slate-900 font-bold"
                            : "bg-[#092e23] text-emerald-300 border border-[#114f3c]"
                      }`}
                    >
                      {idx + 1}
                    </span>
                  </td>
                  <td className="px-5 py-3.5 font-sans font-medium text-white">
                    <div className="font-mono text-xs">
                      {prod.product_name || "Uncataloged product"}
                    </div>
                    <div className="text-[11px] text-slate-400 font-sans">
                      {prod.sku || prod.product_id}
                    </div>
                  </td>
                  <td className="px-5 py-3.5 font-bold text-emerald-400 text-sm">
                    {formatCurrency(prod.sales_amount, currency)}
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex items-center space-x-2">
                      <div className="w-16 h-2 rounded-full bg-[#051c15] overflow-hidden">
                        <div
                          className="h-full bg-emerald-500 rounded-full"
                          style={{
                            width: `${Math.min(100, Math.max(5, parseFloat(sharePct)))}%`,
                          }}
                        />
                      </div>
                      <span className="text-xs text-slate-300 font-bold">
                        {sharePct}%
                      </span>
                    </div>
                  </td>
                  <td className="px-5 py-3.5 text-teal-300 font-semibold">
                    {formatNumber(prod.total_order_count)} units
                  </td>
                  <td className="px-5 py-3.5 font-medium text-slate-300">
                    {formatCurrency(asp, currency)}
                  </td>
                  <td className="px-5 py-3.5 font-sans">
                    <span
                      className={`px-2.5 py-1 rounded-full text-[10px] font-bold uppercase ${
                        isFirst
                          ? "bg-amber-950 text-amber-300 border border-amber-800"
                          : idx < 3
                            ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                            : "bg-teal-950 text-teal-300 border border-teal-800"
                      }`}
                    >
                      {isFirst
                        ? "★ #1 Revenue Driver"
                        : idx < 3
                          ? "High Volume"
                          : "Catalog Core"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};
