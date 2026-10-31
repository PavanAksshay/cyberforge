import React, { useState, useEffect } from 'react';
import {
  Download,
  Filter,
  Calendar,
  Table as TableIcon,
  BarChart2,
  RefreshCw,
  Search,
  ExternalLink,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { fetchTrends, fetchTopProducts, fetchStatusBreakdown } from '../lib/api';
import { TrendsResponse, TopProductsResponse, StatusBreakdownResponse, Interval } from '../types';
import { DualAxisAreaBarChart, TopProductsBarChart } from '../components/Charts';
import { formatCurrency, formatNumber, formatDate, downloadCsv } from '../lib/utils';

export const AdvancedAnalyticsPage: React.FC = () => {
  const { activeTenant, currency, startDate, endDate, token, refreshKey } = useApp();

  const [interval, setInterval] = useState<Interval>('day');
  const [activeView, setActiveView] = useState<'trends' | 'products' | 'statuses'>('trends');
  const [searchFilter, setSearchFilter] = useState('');
  const [loading, setLoading] = useState<boolean>(true);

  const [trends, setTrends] = useState<TrendsResponse | null>(null);
  const [topProducts, setTopProducts] = useState<TopProductsResponse | null>(null);
  const [statuses, setStatuses] = useState<StatusBreakdownResponse | null>(null);

  useEffect(() => {
    if (!activeTenant) return;
    setLoading(true);

    Promise.all([
      fetchTrends(activeTenant.id, currency, startDate, endDate, interval, token),
      fetchTopProducts(activeTenant.id, currency, startDate, endDate, 10, token),
      fetchStatusBreakdown(activeTenant.id, currency, startDate, endDate, token),
    ])
      .then(([trendRes, prodRes, statRes]) => {
        setTrends(trendRes);
        setTopProducts(prodRes);
        setStatuses(statRes);
      })
      .catch((err) => console.error('Advanced analytics load error:', err))
      .finally(() => setLoading(false));
  }, [activeTenant, currency, startDate, endDate, interval, token, refreshKey]);

  const handleExportCsv = () => {
    if (!activeTenant) return;
    if (activeView === 'trends' && trends) {
      const csvContent =
        'bucket_start,shipped_sales,order_count\n' +
        trends.buckets
          .map((b) => `${b.bucket_start},${b.sales_amount},${b.total_order_count}`)
          .join('\n');
      downloadCsv(`trends_${activeTenant.name}_${interval}.csv`, csvContent);
    } else if (activeView === 'products' && topProducts) {
      const csvContent =
        'product_id,shipped_sales,order_count\n' +
        topProducts.products
          .map((p) => `${p.product_id},${p.sales_amount},${p.total_order_count}`)
          .join('\n');
      downloadCsv(`top_products_${activeTenant.name}.csv`, csvContent);
    } else if (activeView === 'statuses' && statuses) {
      const csvContent =
        'status,sales_amount,order_count\n' +
        statuses.statuses
          .map((s) => `${s.status},${s.sales_amount},${s.total_order_count}`)
          .join('\n');
      downloadCsv(`status_breakdown_${activeTenant.name}.csv`, csvContent);
    }
  };

  return (
    <div className="space-y-6">
      {/* Top Controls Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between p-4 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm gap-4">
        {/* View Switcher Tabs */}
        <div className="flex items-center rounded-xl bg-slate-100 dark:bg-slate-800 p-1 text-xs">
          <button
            onClick={() => setActiveView('trends')}
            className={`px-3 py-1.5 font-bold rounded-lg transition ${
              activeView === 'trends'
                ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white'
            }`}
          >
            Time-Series Trends
          </button>
          <button
            onClick={() => setActiveView('products')}
            className={`px-3 py-1.5 font-bold rounded-lg transition ${
              activeView === 'products'
                ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white'
            }`}
          >
            Top Products (10)
          </button>
          <button
            onClick={() => setActiveView('statuses')}
            className={`px-3 py-1.5 font-bold rounded-lg transition ${
              activeView === 'statuses'
                ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white'
            }`}
          >
            Lifecycle Breakdown
          </button>
        </div>

        {/* Action Buttons */}
        <div className="flex items-center space-x-3">
          {activeView === 'trends' && (
            <div className="flex items-center rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 text-xs">
              <button
                onClick={() => setInterval('day')}
                className={`px-2.5 py-1 rounded-md font-semibold transition ${
                  interval === 'day'
                    ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                    : 'text-slate-500'
                }`}
              >
                Day
              </button>
              <button
                onClick={() => setInterval('hour')}
                className={`px-2.5 py-1 rounded-md font-semibold transition ${
                  interval === 'hour'
                    ? 'bg-white dark:bg-emerald-600 text-slate-900 dark:text-white shadow-sm'
                    : 'text-slate-500'
                }`}
              >
                Hour
              </button>
            </div>
          )}

          <button
            onClick={handleExportCsv}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 text-xs font-semibold transition"
          >
            <Download className="w-3.5 h-3.5" />
            <span>Export CSV</span>
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      {activeView === 'trends' && (
        <div className="space-y-6">
          <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                  Dual-Axis Composite Analytics ({interval === 'day' ? 'Daily' : 'Hourly'})
                </h3>
                <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
                  Overlaid Shipped Sales Revenue curve and Order Volume distribution.
                </p>
              </div>
              <span className="px-2.5 py-1 rounded-full text-[10px] font-bold uppercase bg-emerald-950/80 text-emerald-400 border border-emerald-800 font-mono">
                Composite Mode
              </span>
            </div>
            <DualAxisAreaBarChart
              data={trends?.buckets || []}
              currency={currency}
              interval={interval}
            />
          </div>

          {/* Table View */}
          <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                Detailed Granular Buckets
              </h3>
              <span className="text-xs text-slate-400">
                {trends?.buckets.length || 0} UTC intervals
              </span>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left">
                <thead className="bg-slate-50 dark:bg-slate-800/60 text-slate-500 dark:text-slate-400 font-bold uppercase tracking-wider text-[10px]">
                  <tr>
                    <th className="px-4 py-3 rounded-l-lg">Interval Start (UTC)</th>
                    <th className="px-4 py-3">Shipped Sales Amount</th>
                    <th className="px-4 py-3 rounded-r-lg">Total Orders</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800/80 font-mono">
                  {trends?.buckets.map((b) => (
                    <tr
                      key={b.bucket_start}
                      className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40 transition"
                    >
                      <td className="px-4 py-3 text-slate-700 dark:text-slate-300 font-sans">
                        {new Date(b.bucket_start).toLocaleString('en-US', {
                          month: 'short',
                          day: 'numeric',
                          year: 'numeric',
                          hour: '2-digit',
                          minute: '2-digit',
                          timeZone: 'UTC',
                        })}{' '}
                        UTC
                      </td>
                      <td className="px-4 py-3 font-bold text-slate-900 dark:text-white">
                        {formatCurrency(b.sales_amount, currency)}
                      </td>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-400">
                        {formatNumber(b.total_order_count)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {activeView === 'products' && (
        <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="font-bold text-sm text-slate-900 dark:text-white">
              Top 10 Global Products (Deterministically Tied)
            </h3>
            <span className="text-xs text-slate-400">
              Ranked by shipped sales desc, count desc, id asc
            </span>
          </div>

          <TopProductsBarChart
            products={topProducts?.products || []}
            currency={currency}
          />
        </div>
      )}

      {activeView === 'statuses' && (
        <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
          <h3 className="font-bold text-sm text-slate-900 dark:text-white mb-2">
            Status Volume & Revenue Recognition Matrix
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead className="bg-slate-50 dark:bg-slate-800/60 text-slate-500 dark:text-slate-400 font-bold uppercase tracking-wider text-[10px]">
                <tr>
                  <th className="px-4 py-3 rounded-l-lg">Lifecycle Status</th>
                  <th className="px-4 py-3">Total Order Count</th>
                  <th className="px-4 py-3">Recognized Sales</th>
                  <th className="px-4 py-3 rounded-r-lg">Platform Semantics</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800/80">
                {statuses?.statuses.map((s) => (
                  <tr
                    key={s.status}
                    className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40 transition"
                  >
                    <td className="px-4 py-3.5 font-bold uppercase text-slate-900 dark:text-white">
                      {s.status}
                    </td>
                    <td className="px-4 py-3.5 font-mono text-slate-700 dark:text-slate-300">
                      {formatNumber(s.total_order_count)}
                    </td>
                    <td className="px-4 py-3.5 font-mono font-bold text-slate-900 dark:text-white">
                      {formatCurrency(s.sales_amount, currency)}
                    </td>
                    <td className="px-4 py-3.5 text-slate-500 italic">
                      {s.status === 'shipped'
                        ? 'Accrued to financial sales amount'
                        : 'Excluded from recognized financial sales'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};

