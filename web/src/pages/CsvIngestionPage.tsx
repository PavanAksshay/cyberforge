import React, { useState } from 'react';
import {
  UploadCloud,
  FileText,
  Download,
  CheckCircle,
  AlertTriangle,
  ArrowRight,
  Database,
  Radio,
  Cpu,
  Layers,
  Sparkles,
  RefreshCw,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { ingestCsv, fetchSummary } from '../lib/api';
import { IngestionResult } from '../types';
import { formatCurrency, formatNumber, downloadCsv } from '../lib/utils';

export const CsvIngestionPage: React.FC = () => {
  const { activeTenant, currency, startDate, endDate, token, triggerRefresh } = useApp();

  const [csvContent, setCsvContent] = useState<string>('');
  const [dragActive, setDragActive] = useState<boolean>(false);
  const [dryRun, setDryRun] = useState<boolean>(false);
  const [triggerRelay, setTriggerRelay] = useState<boolean>(true);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [result, setResult] = useState<IngestionResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Delta tracking
  const [beforeSales, setBeforeSales] = useState<string | null>(null);
  const [beforeOrders, setBeforeOrders] = useState<number | null>(null);
  const [afterSales, setAfterSales] = useState<string | null>(null);
  const [afterOrders, setAfterOrders] = useState<number | null>(null);

  const sampleCsv = `product_id,amount,currency,status,created_at
PROD-PREMIUM-LAPTOP,1299.99,USD,shipped,2026-10-20T10:00:00Z
PROD-4K-MONITOR,450.00,USD,shipped,2026-10-20T11:00:00Z
PROD-WIRELESS-MOUSE,49.50,USD,placed,2026-10-20T12:00:00Z
PROD-MECH-KEYBOARD,120.00,USD,shipped,2026-10-21T09:00:00Z
PROD-USB-C-DOCK,189.00,USD,shipped,2026-10-21T14:30:00Z
PROD-HEADPHONES,249.99,USD,placed,2026-10-22T08:15:00Z
PROD-DESK-PAD,29.99,USD,shipped,2026-10-22T16:45:00Z
PROD-WEBCAM-HD,89.00,USD,cancelled,2026-10-23T11:20:00Z
PROD-STAND-UP-DESK,599.00,USD,shipped,2026-10-23T15:00:00Z
PROD-ERGONOMIC-CHAIR,349.50,USD,shipped,2026-10-24T10:30:00Z`;

  const handleDownloadTemplate = () => {
    downloadCsv('astraroll_orders_template.csv', sampleCsv);
  };

  const handleLoadSample = () => {
    setCsvContent(sampleCsv);
    setResult(null);
    setError(null);
  };

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const file = e.dataTransfer.files[0];
      const reader = new FileReader();
      reader.onload = (event) => {
        if (event.target?.result) {
          setCsvContent(event.target.result as string);
        }
      };
      reader.readAsText(file);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      const file = e.target.files[0];
      const reader = new FileReader();
      reader.onload = (event) => {
        if (event.target?.result) {
          setCsvContent(event.target.result as string);
        }
      };
      reader.readAsText(file);
    }
  };

  const handleIngest = async () => {
    if (!activeTenant) return;
    if (!csvContent.trim()) {
      setError('Please provide CSV data to ingest.');
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      // 1. Fetch baseline summary before ingestion
      if (!dryRun) {
        const preSum = await fetchSummary(activeTenant.id, currency, startDate, endDate, token).catch(
          () => null
        );
        if (preSum) {
          setBeforeSales(preSum.sales_amount);
          setBeforeOrders(preSum.total_order_count);
        }
      }

      // 2. Submit ingestion
      const res = await ingestCsv(activeTenant.id, csvContent, dryRun, triggerRelay, token);
      setResult(res);

      // 3. If live commit, refresh dashboard metrics and capture post-ingestion state
      if (!dryRun && res.accepted > 0) {
        triggerRefresh();
        setTimeout(async () => {
          const postSum = await fetchSummary(activeTenant.id, currency, startDate, endDate, token).catch(
            () => null
          );
          if (postSum) {
            setAfterSales(postSum.sales_amount);
            setAfterOrders(postSum.total_order_count);
          }
        }, 800);
      }
    } catch (err: any) {
      setError(err.message || 'Ingestion failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-base font-extrabold text-slate-900 dark:text-white">
            Transactional CSV Ingestion Pipeline
          </h2>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
            Atomically insert orders into tenant database with transactional outbox event emission and live rollup propagation.
          </p>
        </div>

        <div className="flex items-center space-x-2">
          <button
            onClick={handleDownloadTemplate}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 text-xs font-semibold transition"
          >
            <Download className="w-3.5 h-3.5" />
            <span>Download Template</span>
          </button>
          <button
            onClick={handleLoadSample}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-emerald-50 dark:bg-emerald-950/60 hover:bg-emerald-100 dark:hover:bg-emerald-900 text-emerald-600 dark:text-emerald-400 border border-emerald-200/80 dark:border-emerald-800 text-xs font-semibold transition"
          >
            <Sparkles className="w-3.5 h-3.5" />
            <span>Load Demo Orders</span>
          </button>
        </div>
      </div>

      {/* Architecture Flow Visualizer */}
      <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm">
        <div className="text-xs font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500 mb-4">
          End-to-End Transactional Pipeline Stages
        </div>

        <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
          <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700 text-center space-y-1">
            <div className="w-7 h-7 mx-auto rounded-lg bg-emerald-100 dark:bg-emerald-950/80 text-emerald-600 dark:text-emerald-400 flex items-center justify-center font-bold text-xs">
              1
            </div>
            <div className="font-bold text-xs text-slate-800 dark:text-slate-200">
              CSV Validation
            </div>
            <div className="text-[10px] text-slate-400 leading-tight">
              Client & server parsing, type & boundary checks
            </div>
          </div>

          <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700 text-center space-y-1">
            <div className="w-7 h-7 mx-auto rounded-lg bg-emerald-100 dark:bg-emerald-950/80 text-emerald-600 dark:text-emerald-400 flex items-center justify-center font-bold text-xs">
              2
            </div>
            <div className="font-bold text-xs text-slate-800 dark:text-slate-200">
              Tenant PostgreSQL
            </div>
            <div className="text-[10px] text-slate-400 leading-tight">
              Atomic write: <code>orders</code> + <code>outbox_events</code>
            </div>
          </div>

          <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700 text-center space-y-1">
            <div className="w-7 h-7 mx-auto rounded-lg bg-emerald-100 dark:bg-emerald-950/80 text-emerald-600 dark:text-emerald-400 flex items-center justify-center font-bold text-xs">
              3
            </div>
            <div className="font-bold text-xs text-slate-800 dark:text-slate-200">
              MultiTenantRelay
            </div>
            <div className="text-[10px] text-slate-400 leading-tight">
              Streams unpublished events to Redis Stream
            </div>
          </div>

          <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700 text-center space-y-1">
            <div className="w-7 h-7 mx-auto rounded-lg bg-emerald-100 dark:bg-emerald-950/80 text-emerald-600 dark:text-emerald-400 flex items-center justify-center font-bold text-xs">
              4
            </div>
            <div className="font-bold text-xs text-slate-800 dark:text-slate-200">
              Aggregation Worker
            </div>
            <div className="text-[10px] text-slate-400 leading-tight">
              Consumes stream, applies rollups, XACKs
            </div>
          </div>

          <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700 text-center space-y-1">
            <div className="w-7 h-7 mx-auto rounded-lg bg-emerald-100 dark:bg-emerald-950/80 text-emerald-600 dark:text-emerald-400 flex items-center justify-center font-bold text-xs">
              5
            </div>
            <div className="font-bold text-xs text-slate-800 dark:text-slate-200">
              Dashboard Live
            </div>
            <div className="text-[10px] text-slate-400 leading-tight">
              Analytics API reflects incremental delta
            </div>
          </div>
        </div>
      </div>

      {/* Upload Zone & Editor */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Dropzone */}
        <div
          onDragEnter={handleDrag}
          onDragLeave={handleDrag}
          onDragOver={handleDrag}
          onDrop={handleDrop}
          className={`flex flex-col items-center justify-center p-8 rounded-2xl border-2 border-dashed transition duration-200 ${
            dragActive
              ? 'border-emerald-500 bg-emerald-50/50 dark:bg-emerald-950/30'
              : 'border-slate-300 dark:border-slate-700 bg-white dark:bg-[#0B140F]'
          }`}
        >
          <div className="p-4 rounded-2xl bg-slate-100 dark:bg-slate-800 text-emerald-600 dark:text-emerald-400 mb-3">
            <UploadCloud className="w-8 h-8" />
          </div>
          <p className="font-bold text-sm text-slate-800 dark:text-slate-200">
            Drag and drop orders CSV file here
          </p>
          <p className="text-xs text-slate-400 mt-1 mb-4">
            Supports UTF-8 CSV with product_id, amount, currency, status, created_at
          </p>
          <label className="cursor-pointer px-4 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-xs font-semibold text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700 transition">
            Browse File
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={handleFileChange}
              className="hidden"
            />
          </label>
        </div>

        {/* Raw CSV Text Area */}
        <div className="flex flex-col p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-3">
          <div className="flex items-center justify-between">
            <label className="text-xs font-bold text-slate-700 dark:text-slate-300 flex items-center space-x-1.5">
              <FileText className="w-3.5 h-3.5 text-emerald-500" />
              <span>CSV Editor / Direct Input</span>
            </label>
            <span className="text-[11px] text-slate-400 font-mono">
              {csvContent.split('\n').filter((l) => l.trim()).length} lines
            </span>
          </div>

          <textarea
            rows={8}
            value={csvContent}
            onChange={(e) => setCsvContent(e.target.value)}
            placeholder="product_id,amount,currency,status,created_at&#10;PROD-1,100.00,USD,shipped,2026-10-20T12:00:00Z"
            className="w-full flex-1 p-3 text-xs font-mono rounded-xl bg-slate-50 dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700 text-slate-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"
          />

          {/* Controls & Submit */}
          <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
            <div className="flex items-center space-x-4 text-xs">
              <label className="flex items-center space-x-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(e) => setDryRun(e.target.checked)}
                  className="rounded border-slate-300 text-emerald-600 focus:ring-emerald-500"
                />
                <span className="font-semibold text-slate-700 dark:text-slate-300">
                  Dry Run Preview Only
                </span>
              </label>

              <label className="flex items-center space-x-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={triggerRelay}
                  onChange={(e) => setTriggerRelay(e.target.checked)}
                  className="rounded border-slate-300 text-emerald-600 focus:ring-emerald-500"
                />
                <span className="font-semibold text-slate-700 dark:text-slate-300">
                  Trigger Instant Relay & Consumer
                </span>
              </label>
            </div>

            <button
              onClick={handleIngest}
              disabled={isSubmitting || !csvContent.trim()}
              className="flex items-center space-x-2 px-5 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white font-bold text-xs shadow-md shadow-emerald-600/20 transition"
            >
              {isSubmitting ? (
                <>
                  <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                  <span>Processing Batch...</span>
                </>
              ) : (
                <>
                  <UploadCloud className="w-3.5 h-3.5" />
                  <span>{dryRun ? 'Validate & Preview' : 'Ingest Orders Atomically'}</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="p-4 rounded-xl bg-rose-50 dark:bg-rose-950/40 border border-rose-200 dark:border-rose-900 text-rose-700 dark:text-rose-400 text-xs">
          <strong>Ingestion Error:</strong> {error}
        </div>
      )}

      {/* Live Delta Tracker Banner */}
      {!dryRun && (beforeSales !== null || afterSales !== null) && (
        <div className="p-5 rounded-2xl bg-gradient-to-r from-emerald-50 via-teal-50 to-white dark:from-emerald-950/30 dark:via-teal-950/20 dark:to-[#0B140F] border border-emerald-200 dark:border-emerald-800 shadow-sm space-y-3 animate-in fade-in">
          <div className="flex items-center space-x-2 text-emerald-800 dark:text-emerald-300 font-bold text-xs uppercase tracking-wider">
            <CheckCircle className="w-4 h-4 text-emerald-600" />
            <span>Verified End-to-End Freshness Delta</span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 font-mono text-xs">
            <div className="p-3 rounded-xl bg-white/80 dark:bg-slate-900/80 border border-emerald-100 dark:border-emerald-900/50">
              <div className="text-[10px] text-slate-500 uppercase font-sans">
                Shipped Sales Before
              </div>
              <div className="text-base font-bold text-slate-700 dark:text-slate-300 mt-1">
                {formatCurrency(beforeSales || '0.00', currency)}
              </div>
            </div>

            <div className="p-3 rounded-xl bg-white/80 dark:bg-slate-900/80 border border-emerald-100 dark:border-emerald-900/50">
              <div className="text-[10px] text-slate-500 uppercase font-sans">
                Shipped Sales After Ingestion
              </div>
              <div className="text-base font-bold text-emerald-600 dark:text-emerald-400 mt-1">
                {formatCurrency(afterSales || '0.00', currency)}
              </div>
            </div>

            <div className="p-3 rounded-xl bg-white/80 dark:bg-slate-900/80 border border-emerald-100 dark:border-emerald-900/50">
              <div className="text-[10px] text-slate-500 uppercase font-sans">
                Orders Volume Delta
              </div>
              <div className="text-base font-bold text-emerald-600 dark:text-emerald-400 mt-1">
                {beforeOrders !== null && afterOrders !== null
                  ? `+${afterOrders - beforeOrders} orders`
                  : 'Updated'}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Ingestion Results Summary & Preview Table */}
      {result && (
        <div className="p-5 rounded-2xl bg-white dark:bg-[#0B140F] border border-slate-200/80 dark:border-slate-800 shadow-sm space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <CheckCircle className="w-4 h-4 text-emerald-500" />
              <h3 className="font-bold text-sm text-slate-900 dark:text-white">
                Ingestion Summary {result.dry_run && '(Dry Run)'}
              </h3>
            </div>
            <span className="text-xs text-slate-400">
              Total Processed: {result.total_rows} rows
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
            <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700">
              <span className="text-[10px] text-slate-400 font-sans block">Accepted Orders</span>
              <strong className="text-emerald-600 dark:text-emerald-400 text-sm">
                {result.accepted}
              </strong>
            </div>

            <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700">
              <span className="text-[10px] text-slate-400 font-sans block">Rejected / Errors</span>
              <strong className="text-rose-600 dark:text-rose-400 text-sm">
                {result.rejected}
              </strong>
            </div>

            <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700">
              <span className="text-[10px] text-slate-400 font-sans block">Duplicate IDs</span>
              <strong className="text-amber-600 dark:text-amber-400 text-sm">
                {result.duplicates}
              </strong>
            </div>

            <div className="p-3 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700">
              <span className="text-[10px] text-slate-400 font-sans block">Shipped Sales Added</span>
              <strong className="text-emerald-600 dark:text-emerald-400 text-sm">
                {formatCurrency(result.total_sales, currency)}
              </strong>
            </div>
          </div>

          {/* Row Errors if any */}
          {result.errors.length > 0 && (
            <div className="p-4 rounded-xl bg-rose-50/50 dark:bg-rose-950/20 border border-rose-200 dark:border-rose-900/60 space-y-1.5 text-xs text-rose-700 dark:text-rose-400">
              <div className="font-bold">Validation Errors Detected:</div>
              {result.errors.map((e, idx) => (
                <div key={idx} className="font-mono text-[11px]">
                  Line {e.line_number} [{e.column}]: {e.message}
                </div>
              ))}
            </div>
          )}

          {/* Preview Table */}
          {result.preview_rows.length > 0 && (
            <div>
              <div className="text-xs font-bold text-slate-700 dark:text-slate-300 mb-2">
                Sample Parsed Records ({result.preview_rows.length} shown)
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left font-mono">
                  <thead className="bg-slate-50 dark:bg-slate-800/60 text-slate-500 uppercase text-[10px]">
                    <tr>
                      <th className="px-3 py-2 rounded-l-lg">Line</th>
                      <th className="px-3 py-2">Order ID</th>
                      <th className="px-3 py-2">Product ID</th>
                      <th className="px-3 py-2">Amount</th>
                      <th className="px-3 py-2">Currency</th>
                      <th className="px-3 py-2 rounded-r-lg">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-slate-800/80">
                    {result.preview_rows.map((r, i) => (
                      <tr key={i} className="hover:bg-slate-50/50 dark:hover:bg-slate-800/40">
                        <td className="px-3 py-2 font-sans text-slate-400">{r.line_number}</td>
                        <td className="px-3 py-2 text-slate-600 dark:text-slate-400 truncate max-w-[120px]">
                          {r.order_id}
                        </td>
                        <td className="px-3 py-2 text-slate-900 dark:text-white font-sans font-medium">
                          {r.raw_product_id}
                        </td>
                        <td className="px-3 py-2 font-bold">{formatCurrency(r.amount, currency)}</td>
                        <td className="px-3 py-2">{r.currency}</td>
                        <td className="px-3 py-2">
                          <span
                            className={`px-2 py-0.5 rounded-full text-[10px] font-bold uppercase font-sans ${
                              r.status === 'shipped'
                                ? 'bg-teal-100 text-teal-700 dark:bg-teal-950/60 dark:text-teal-400'
                                : r.status === 'placed'
                                ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-400'
                                : 'bg-rose-100 text-rose-700 dark:bg-rose-950/60 dark:text-rose-400'
                            }`}
                          >
                            {r.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

