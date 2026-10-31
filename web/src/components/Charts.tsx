import React, { useState } from 'react';
import { TrendBucket, ProductMetric, StatusMetric, Currency } from '../types';
import { formatCurrency, formatNumber, formatDate, truncateId } from '../lib/utils';

// --- TREND LINE & AREA CHART ---
interface TrendLineChartProps {
  data: TrendBucket[];
  currency: Currency;
  interval: 'hour' | 'day';
}

export const TrendLineChart: React.FC<TrendLineChartProps> = ({ data, currency }) => {
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
  }, '');

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
                  ? 'fill-emerald-600 stroke-white stroke-2 shadow-md'
                  : 'fill-emerald-500 hover:fill-emerald-600'
              }`}
              onMouseEnter={() => setHoveredIdx(i)}
              onMouseLeave={() => setHoveredIdx(null)}
            />
            {/* Show sample x labels */}
            {(i === 0 || i === Math.floor(points.length / 2) || i === points.length - 1) && (
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
            transform: 'translate(-50%, -100%)',
          }}
        >
          <div className="text-[10px] text-slate-400 font-medium">
            {new Date(points[hoveredIdx].bucket.bucket_start).toLocaleString('en-US', {
              month: 'short',
              day: 'numeric',
              hour: '2-digit',
              minute: '2-digit',
              timeZone: 'UTC',
            })}{' '}
            UTC
          </div>
          <div className="font-bold text-sm text-emerald-400">
            {formatCurrency(points[hoveredIdx].bucket.sales_amount, currency)}
          </div>
          <div className="text-[11px] text-slate-300">
            Orders: <strong className="text-white">{points[hoveredIdx].bucket.total_order_count}</strong>
          </div>
        </div>
      )}
    </div>
  );
};

// --- TOP PRODUCTS BAR CHART ---
interface TopProductsBarChartProps {
  products: ProductMetric[];
  currency: Currency;
}

export const TopProductsBarChart: React.FC<TopProductsBarChartProps> = ({ products, currency }) => {
  if (!products || products.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-slate-400 dark:text-slate-500 text-xs">
        No shipped products recorded in this window.
      </div>
    );
  }

  const maxSales = Math.max(...products.map((p) => parseFloat(p.sales_amount) || 0), 1);

  return (
    <div className="space-y-3">
      {products.map((prod, idx) => {
        const sales = parseFloat(prod.sales_amount) || 0;
        const pct = Math.min(100, Math.max(5, (sales / maxSales) * 100));

        return (
          <div key={prod.product_id} className="group">
            <div className="flex items-center justify-between text-xs mb-1.5">
              <div className="flex items-center space-x-2">
                <span
                  className={`w-5 h-5 flex items-center justify-center rounded-md font-extrabold text-[10px] ${
                    idx === 0
                      ? 'bg-amber-100 dark:bg-amber-950/60 text-amber-700 dark:text-amber-400 border border-amber-300 dark:border-amber-800'
                      : idx === 1
                      ? 'bg-slate-200 dark:bg-slate-800 text-slate-700 dark:text-slate-300'
                      : 'bg-slate-100 dark:bg-slate-900 text-slate-500'
                  }`}
                >
                  #{idx + 1}
                </span>
                <span className="font-mono text-slate-800 dark:text-slate-200 font-medium">
                  {truncateId(prod.product_id, 12)}
                </span>
              </div>
              <div className="flex items-center space-x-3">
                <span className="text-[11px] text-slate-400">
                  {formatNumber(prod.total_order_count)} orders
                </span>
                <span className="font-bold text-slate-900 dark:text-white">
                  {formatCurrency(prod.sales_amount, currency)}
                </span>
              </div>
            </div>
            <div className="w-full h-2 rounded-full bg-slate-100 dark:bg-slate-800 overflow-hidden">
              <div
                className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-emerald-600 transition-all duration-500 ease-out"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
};

// --- STATUS DONUT CHART ---
interface StatusDonutChartProps {
  statuses: StatusMetric[];
  currency: Currency;
}

export const StatusDonutChart: React.FC<StatusDonutChartProps> = ({ statuses, currency }) => {
  const totalOrders = statuses.reduce((acc, s) => acc + (s.total_order_count || 0), 0);

  const colors: Record<string, { stroke: string; bg: string; text: string }> = {
    shipped: {
      stroke: '#0D9488',
      bg: 'bg-teal-50 dark:bg-teal-950/40',
      text: 'text-teal-700 dark:text-teal-400',
    },
    placed: {
      stroke: '#10B981',
      bg: 'bg-emerald-50 dark:bg-emerald-950/40',
      text: 'text-emerald-700 dark:text-emerald-400',
    },
    cancelled: {
      stroke: '#F43F5E',
      bg: 'bg-rose-50 dark:bg-rose-950/40',
      text: 'text-rose-700 dark:text-rose-400',
    },
  };

  const radius = 55;
  const strokeWidth = 14;
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

  return (
    <div className="flex flex-col sm:flex-row items-center justify-around gap-6">
      {/* SVG Donut */}
      <div className="relative w-40 h-40 shrink-0">
        <svg viewBox="0 0 140 140" className="w-full h-full -rotate-90">
          <circle
            cx="70"
            cy="70"
            r={radius}
            fill="none"
            stroke="currentColor"
            className="text-slate-100 dark:text-slate-800"
            strokeWidth={strokeWidth}
          />
          {totalOrders > 0 &&
            slices.map((slice) => (
              <circle
                key={slice.status}
                cx="70"
                cy="70"
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
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
          <span className="text-xl font-extrabold text-slate-900 dark:text-white">
            {formatNumber(totalOrders)}
          </span>
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
            Total Orders
          </span>
        </div>
      </div>

      {/* Legend & Breakdown */}
      <div className="w-full space-y-2.5">
        {slices.map((s) => {
          const pct = Math.round(s.ratio * 100);
          return (
            <div
              key={s.status}
              className="flex items-center justify-between p-2.5 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-100 dark:border-slate-800"
            >
              <div className="flex items-center space-x-2">
                <span
                  className="w-2.5 h-2.5 rounded-full shrink-0"
                  style={{ backgroundColor: s.color.stroke }}
                />
                <span className="text-xs font-semibold capitalize text-slate-700 dark:text-slate-300">
                  {s.status}
                </span>
              </div>
              <div className="flex items-center space-x-3 text-xs">
                <span className="text-slate-400 dark:text-slate-500 font-mono">
                  {pct}% ({formatNumber(s.count)})
                </span>
                {s.status === 'shipped' && (
                  <span className="font-bold text-slate-900 dark:text-white">
                    {formatCurrency(s.sales, currency)}
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

