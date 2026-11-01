import React, { useState, useEffect } from "react";

import { RefreshCw, CheckCircle2, AlertCircle, Clock } from "lucide-react";

import { relativeTime } from "../lib/utils";

interface FreshnessIndicatorProps {
  lastFetchedAt: Date | null; // A: last successful HTTP fetch

  lastMaterializedAt?: string | null; // B: backend aggregate materialization timestamp

  isLoading: boolean;

  fetchError: string | null;

  autoRefreshInterval: number; // seconds, 0 = off

  onManualRefresh: () => void;

  nextAt?: number | null;

  pending?: boolean;

  sourceCommittedAt?: string | null;
}

export const FreshnessIndicator: React.FC<FreshnessIndicatorProps> = ({
  lastFetchedAt,

  lastMaterializedAt,

  isLoading,

  fetchError,

  autoRefreshInterval,

  onManualRefresh,
  nextAt,
  pending,
  sourceCommittedAt,
}) => {
  const [, setTick] = useState(0);

  // Tick every second for live relative time display — no network requests

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);

    return () => clearInterval(id);
  }, []);

  const fetchedLabel = lastFetchedAt ? relativeTime(lastFetchedAt) : "never";

  const materializedLabel = lastMaterializedAt
    ? relativeTime(new Date(lastMaterializedAt))
    : null;

  let status: "live" | "stale" | "loading" | "error" = "live";

  if (isLoading) status = "loading";
  else if (fetchError) status = "error";
  else if (!lastFetchedAt) status = "stale";

  const statusConfig = {
    live: {
      color: "text-emerald-400",
      bg: "bg-emerald-950/40 border-emerald-800",
      dot: "bg-emerald-400",
      label: pending ? "PENDING" : "SNAPSHOT",
    },

    stale: {
      color: "text-amber-400",
      bg: "bg-amber-950/40 border-amber-800",
      dot: "bg-amber-400",
      label: "STALE",
    },

    loading: {
      color: "text-sky-400",
      bg: "bg-sky-950/40 border-sky-800",
      dot: "bg-sky-400",
      label: "CHECKING",
    },

    error: {
      color: "text-rose-400",
      bg: "bg-rose-950/40 border-rose-800",
      dot: "bg-rose-400",
      label: "ERROR",
    },
  }[status];

  return (
    <div
      className={`flex items-center space-x-3 px-3 py-1.5 rounded-xl border text-xs font-mono ${statusConfig.bg}`}
    >
      {/* Status badge */}

      <div
        className={`flex items-center space-x-1.5 font-black ${statusConfig.color}`}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full ${statusConfig.dot} ${status === "loading" ? "animate-pulse" : ""}`}
        />

        <span>{statusConfig.label}</span>
      </div>

      <span className="text-slate-500">·</span>

      {/* Last fetch */}

      {isLoading ? (
        <span className="text-sky-400 flex items-center space-x-1">
          <RefreshCw className="w-3 h-3 animate-spin" />

          <span>Checking…</span>
        </span>
      ) : fetchError ? (
        <span
          className="text-rose-400 flex items-center space-x-1"
          title={fetchError}
        >
          <AlertCircle className="w-3 h-3" />

          <span>Refresh failed · {fetchedLabel}</span>
        </span>
      ) : (
        <span className="text-slate-400">
          Last checked{" "}
          <span className="text-slate-200 font-semibold">{fetchedLabel}</span>
        </span>
      )}

      {/* Aggregation time — distinct from HTTP fetch */}

      {materializedLabel && (
        <>
          <span className="text-slate-500">·</span>

          <span className="text-slate-400">
            Last data change{" "}
            <span className="text-emerald-400 font-semibold">
              {materializedLabel}
            </span>
          </span>
        </>
      )}

      {/* Auto-refresh indicator */}

      {autoRefreshInterval > 0 && !isLoading && (
        <>
          <span className="text-slate-500">·</span>

          <span className="text-slate-500">
            Next check{" "}
            {nextAt
              ? Math.max(0, Math.ceil((nextAt - Date.now()) / 1000))
              : "—"}
            s
          </span>
        </>
      )}

      {pending && <span>Waiting for new events</span>}

      {sourceCommittedAt && (
        <span title={sourceCommittedAt}>
          Source event {relativeTime(new Date(sourceCommittedAt))}
        </span>
      )}

      {/* Manual refresh */}

      <button
        onClick={onManualRefresh}

        disabled={isLoading}

        className="ml-1 p-1 rounded-lg hover:bg-slate-700/50 text-slate-400 hover:text-white transition disabled:opacity-40"

        title="Manual refresh"
      >
        <RefreshCw className={`w-3 h-3 ${isLoading ? "animate-spin" : ""}`} />
      </button>
    </div>
  );
};
