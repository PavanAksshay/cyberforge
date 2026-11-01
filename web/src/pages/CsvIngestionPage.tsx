import React, { useEffect, useRef, useState } from "react";
import { useApp } from "../context/AppContext";
import { apiJson, ingestCsv, fetchSummary } from "../lib/api";
import { IngestionResult } from "../types";
import { formatCurrency, downloadCsv } from "../lib/utils";

const sample =
  "product_id,product_name,amount,currency,status,created_at\n" +
  Array.from(
    { length: 12 },
    (_, i) =>
      `DEMO-${(i % 6) + 1},Demo Product ${(i % 6) + 1},${i < 5 ? "100.00" : i === 5 ? "6.97" : "20.00"},USD,${i < 6 ? "shipped" : i < 10 ? "placed" : "cancelled"},2026-10-03T10:00:00Z`,
  ).join("\n");
const cents = (s: string) => BigInt(s.replace(".", ""));
export const CsvIngestionPage: React.FC = () => {
  const { activeTenant, currency, startDate, endDate, token, triggerRefresh } =
    useApp();
  const [csv, setCsv] = useState(""),
    [dryRun, setDryRun] = useState(false),
    [process, setProcess] = useState(true),
    [busy, setBusy] = useState(false);
  const [result, setResult] = useState<IngestionResult | null>(null),
    [stage, setStage] = useState("READY"),
    [details, setDetails] = useState(""),
    [error, setError] = useState<string | null>(null);
  const [delta, setDelta] = useState<{
    before: string;
    after: string;
    orders: number;
  } | null>(null);
  const abort = useRef<AbortController>();
  useEffect(() => {
    abort.current?.abort();
    setResult(null);
    setStage("READY");
    setDetails("");
    setDelta(null);
    setError(null);
    setBusy(false);
    return () => abort.current?.abort();
  }, [activeTenant?.id, currency, startDate, endDate, token]);
  async function resume() {
    if (!activeTenant) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    const signal = controller.signal;
    setBusy(true);
    setError(null);
    try {
      const saved =
        result ||
        JSON.parse(
          sessionStorage.getItem(`astraroll_import_${activeTenant.id}`) ||
            "null",
        );
      if (!saved?.imported_order_ids?.length)
        throw new Error("No committed import receipt in this tab");
      setResult(saved);
      const p = await apiJson(
        `/tenants/${activeTenant.id}/ingest/progress`,
        token,
        signal,
        { order_ids: saved.imported_order_ids, advance: true },
      );
      if (signal.aborted) return;
      setStage(p.stage);
      setDetails(
        p.processing_error
          ? `PENDING at ${p.processing_error.stage}: ${p.processing_error.message}`
          : "Recovery checked durable event/projection state. Refresh analytics; no reconstructed before/after delta is claimed.",
      );
      triggerRefresh();
    } catch (e) {
      if (!signal.aborted) setError(String(e));
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  async function submit() {
    if (!activeTenant) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    const signal = controller.signal;
    setBusy(true);
    setError(null);
    setDelta(null);
    setResult(null);
    setDetails("Validating and checking the current authorized snapshot…");
    setStage("VALIDATING");
    try {
      const tenant = activeTenant.id;
      const before = dryRun
        ? null
        : await fetchSummary(
            tenant,
            currency,
            startDate,
            endDate,
            token,
            signal,
          ).catch(() => null);
      const res = await ingestCsv(tenant, csv, dryRun, process, token, signal);
      if (signal.aborted) return;
      setResult(res);
      if (!dryRun && res.accepted)
        sessionStorage.setItem(
          `astraroll_import_${tenant}`,
          JSON.stringify(res),
        );
      if (dryRun) {
        setStage("VALIDATED");
        setDetails("Validation only. No orders or events committed.");
        return;
      }
      if (!res.accepted) {
        setStage(res.duplicates ? "DUPLICATE" : "REJECTED");
        setDetails(
          `No new orders. ${res.duplicates} duplicate IDs; ${res.rejected} rejected rows.`,
        );
        return;
      }
      setStage("COMMITTED");
      let progress = res.progress;
      const deadline = Date.now() + 30000;
      do {
        if (signal.aborted) return;
        if (progress) {
          setStage(progress.stage);
          setDetails(
            `${res.accepted} orders committed · ${progress.published_count}/${progress.event_count} events published · ${progress.processed_count} durable receipts.`,
          );
        }
        if (progress?.stages?.AGGREGATED || !process) break;
        progress = await apiJson(
          `/tenants/${tenant}/ingest/progress`,
          token,
          signal,
          { order_ids: res.imported_order_ids, advance: true },
        );
        if (progress.processing_error) {
          setDetails(
            `PENDING at ${progress.processing_error.stage}: ${progress.processing_error.message}`,
          );
          break;
        }
        if (!progress.stages.AGGREGATED)
          await new Promise<void>((resolve, reject) => {
            const t = setTimeout(resolve, 1000);
            signal.addEventListener(
              "abort",
              () => {
                clearTimeout(t);
                reject(new DOMException("Aborted", "AbortError"));
              },
              { once: true },
            );
          });
      } while (Date.now() < deadline);
      if (signal.aborted) return;
      const orders = progress?.orders || [];
      const windowOrders = orders.filter(
        (o: any) =>
          new Date(o.created_at) >= new Date(startDate) &&
          new Date(o.created_at) < new Date(endDate),
      );
      const matching = windowOrders.filter((o: any) => o.currency === currency);
      if (!windowOrders.length) {
        setStage(progress?.stages?.AGGREGATED ? "AGGREGATED" : "PENDING");
        setDetails(
          `${res.accepted} orders committed, but 0 are included in the current dashboard date range. UTC [${startDate}, ${endDate}).`,
        );
      } else if (!matching.length) {
        setStage(progress?.stages?.AGGREGATED ? "AGGREGATED" : "PENDING");
        setDetails(
          `${res.accepted} orders committed in ${Object.keys(res.totals_by_currency).join(", ")}. The ${currency} dashboard is not expected to increase. No currency conversion is performed.`,
        );
      } else if (progress?.stages?.AGGREGATED && before) {
        let after = await fetchSummary(
          tenant,
          currency,
          startDate,
          endDate,
          token,
          signal,
        );
        const expected = matching
          .filter((o: any) => o.status === "shipped")
          .reduce((n: bigint, o: any) => n + cents(o.amount), 0n);
        let actual = cents(after.sales_amount) - cents(before.sales_amount),
          count = after.total_order_count - before.total_order_count;
        for (
          let attempt = 0;
          attempt < 4 && (actual !== expected || count !== matching.length);
          attempt++
        ) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          if (signal.aborted) return;
          after = await fetchSummary(
            tenant,
            currency,
            startDate,
            endDate,
            token,
            signal,
          );
          actual = cents(after.sales_amount) - cents(before.sales_amount);
          count = after.total_order_count - before.total_order_count;
        }
        setDelta({
          before: before.sales_amount,
          after: after.sales_amount,
          orders: count,
        });
        if (actual === expected && count === matching.length) {
          setStage("VISIBLE");
          setDetails(
            `Verified with identical tenant, ${currency}, UTC window and shipped-only semantics: +${count} orders, +${formatCurrency(`${actual / 100n}.${(actual % 100n).toString().padStart(2, "0")}`, currency)} shipped sales.`,
          );
        } else {
          setStage("PENDING");
          setDetails(
            `Durable aggregation receipts exist, but exact API delta is not verified. Expected ${matching.length} orders; concurrent changes or coverage require investigation.`,
          );
        }
      } else {
        setStage("PENDING");
        setDetails(
          res.processing_error
            ? `Pending at ${res.processing_error.stage}: ${res.processing_error.message}`
            : "Orders committed; waiting for aggregation and a covered API snapshot. No visibility claim is made.",
        );
      }
      triggerRefresh();
    } catch (e) {
      if (signal.aborted) return;
      setError(e instanceof Error ? e.message : String(e));
      setStage("FAILED");
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  return (
    <div className="space-y-6">
      <section className="p-6 rounded-2xl bg-[#07261d] border border-[#114f3c] space-y-4">
        <h2 className="text-2xl font-black">
          Transactional CSV Ingestion Pipeline
        </h2>
        <button
          className="px-3 py-2 bg-emerald-900 rounded-xl"
          disabled={busy}
          onClick={resume}
        >
          Resume Last Committed Import
        </button>
        <p>
          VALIDATED → COMMITTED → PUBLISHED → CONSUMED → AGGREGATED → VISIBLE.
          Each stage requires backend evidence.
        </p>
        <p className="text-sm text-emerald-300">
          Dashboard: {currency} · UTC [{startDate}, {endDate}). Source currency
          is preserved.
        </p>
        <p>
          Benchmark tenants are read-only. Use an isolated live demo tenant for
          imports.
        </p>
        <div className="flex gap-4">
          <button
            onClick={() => setCsv(sample)}
            className="px-4 py-2 rounded-xl bg-emerald-700"
          >
            Load Demo Orders (12)
          </button>
          <button onClick={() => downloadCsv("astraroll-template.csv", sample)}>
            Download Template
          </button>
          <input
            type="file"
            accept=".csv,text/csv"
            aria-label="CSV file"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (f) setCsv(await f.text());
            }}
          />
        </div>
        <textarea
          aria-label="CSV content"
          value={csv}
          onChange={(e) => setCsv(e.target.value)}
          className="w-full h-64 p-4 rounded-xl bg-[#03150f] border border-emerald-900 font-mono text-sm"
          placeholder="product_id,amount,currency,status,created_at (optional product_name, order_id)"
        />
        <div className="flex gap-6">
          <label>
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(e) => setDryRun(e.target.checked)}
            />{" "}
            Dry run
          </label>
          <label>
            <input
              type="checkbox"
              checked={process}
              onChange={(e) => setProcess(e.target.checked)}
            />{" "}
            Attempt bounded relay & consumer
          </label>
        </div>
        <button
          disabled={busy || !csv.trim()}
          onClick={submit}
          className="bg-emerald-600 rounded-xl px-6 py-3 font-bold disabled:opacity-50"
        >
          {busy
            ? "Processing Batch…"
            : dryRun
              ? "Validate & Preview"
              : "Ingest Orders Atomically"}
        </button>
      </section>
      <section
        className="p-5 rounded-2xl bg-[#062118] border border-emerald-900 space-y-3"
        aria-live="polite"
      >
        <h3 className="text-lg font-bold" data-testid="ingestion-stage">
          {stage}
        </h3>
        <p>{details}</p>
        {error && (
          <p role="alert" className="text-rose-400">
            {error}
          </p>
        )}
        {delta && (
          <p>
            Shipped sales before {formatCurrency(delta.before, currency)} ·
            after {formatCurrency(delta.after, currency)} · Orders volume delta
            +{delta.orders}
          </p>
        )}
      </section>
      {result && (
        <section className="p-5 rounded-2xl bg-[#07261d] border border-emerald-900 space-y-4">
          <h3 className="font-bold text-xl">
            Ingestion Summary {result.dry_run ? "(Dry Run)" : ""}
          </h3>
          <p>
            {result.accepted} accepted · {result.rejected} rejected ·{" "}
            {result.duplicates} duplicates
          </p>
          {Object.entries(result.totals_by_currency).map(([code, v]) => (
            <p key={code}>
              {code}: {v.order_count} orders · shipped sales{" "}
              {formatCurrency(v.sales_amount, code)} · face amount{" "}
              {formatCurrency(v.total_amount, code)}
            </p>
          ))}
          {result.errors.map((e, i) => (
            <p key={i} className="text-rose-400">
              Line {e.line_number} [{e.column}]: {e.message}
            </p>
          ))}
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th>Product / SKU</th>
                <th>Amount</th>
                <th>Currency</th>
                <th>Status</th>
                <th>Created UTC</th>
              </tr>
            </thead>
            <tbody>
              {result.preview_rows.map((r) => (
                <tr key={r.line_number}>
                  <td>{r.raw_product_id}</td>
                  <td>{formatCurrency(r.amount, r.currency)}</td>
                  <td>{r.currency}</td>
                  <td>{r.status}</td>
                  <td>{r.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
};
