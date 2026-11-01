import React from "react";
import { useApp } from "../context/AppContext";
import { usePolling } from "../lib/usePolling";
import { fetchTenantStatus } from "../lib/api";
import { FreshnessIndicator } from "../components/FreshnessIndicator";
export const TenantOpsPage: React.FC = () => {
  const {
    tenants,
    activeTenant,
    setActiveTenant,
    token,
    triggerRefresh,
    autoRefreshInterval,
  } = useApp();
  const state = usePolling(`${activeTenant?.id}:${token}`, (signal) => {
    if (!activeTenant) throw new Error("Select a tenant");
    return fetchTenantStatus(activeTenant.id, token, signal);
  });
  const d = state.data;
  return (
    <div className="space-y-5">
      <h2 className="text-2xl font-black">
        Tenant Operations · {tenants.length} authorized registry entries
      </h2>
      <p>
        Only the selected database is inspected. Registry membership does not
        imply database health.
      </p>
      <button
        className="bg-emerald-700 px-4 py-2 rounded-xl"
        onClick={triggerRefresh}
        disabled={state.loading}
      >
        Synchronize Cluster Status
      </button>
      <FreshnessIndicator
        lastFetchedAt={state.fetchedAt}
        lastMaterializedAt={d?.last_processed_at}
        sourceCommittedAt={d?.last_source_event_at}
        pending={!!d?.pending_live_events}
        isLoading={state.loading}
        fetchError={state.error}
        autoRefreshInterval={autoRefreshInterval}
        nextAt={state.nextAt}
        onManualRefresh={triggerRefresh}
      />
      {state.error && (
        <p role="alert" className="text-rose-400">
          {state.error} · Counters unavailable, not zero.
        </p>
      )}
      <div className="grid grid-cols-3 gap-4">
        <div className="max-h-[700px] overflow-y-auto space-y-1">
          {tenants.map((t) => (
            <button
              key={t.id}
              onClick={() => setActiveTenant(t)}
              className={`w-full text-left p-3 rounded-xl ${t.id === activeTenant?.id ? "bg-emerald-800" : "bg-[#07261d]"}`}
            >
              {t.name}
              <br />
              <small className="font-mono">{t.id}</small>
            </button>
          ))}
        </div>
        <section className="col-span-2 rounded-2xl bg-[#07261d] p-5 space-y-3">
          <h3 className="font-bold text-xl">Tenant Details</h3>
          {d && (
            <>
              <p className="break-all">
                Physical database: <code>{d.database_name}</code>
              </p>
              <p>
                Database health: {d.is_healthy ? "reachable" : "unavailable"} ·
                Schema {d.schema_version}
              </p>
              <p>
                Orders: {d.order_count} · Projections: {d.projection_count} ·
                Outbox: {d.total_outbox}
              </p>
              <p>
                Unpublished: {d.unpublished_outbox} · projection-covered
                history: {d.covered_pending_history} · unapplied events:{" "}
                {d.pending_live_events}
              </p>
              <p>
                Aggregate revision {d.aggregate_revision ?? "unknown"} · Last
                processed {d.last_processed_at ?? "unknown"}
              </p>
              <p>Checkpoint {d.checkpoint ?? "none"}</p>
              <p>
                Worker health: {d.worker_health}. Writes{" "}
                {d.write_protected
                  ? "protected (benchmark)"
                  : "allowed for authorized identities"}
                .
              </p>
              <h4 className="font-bold">Coverage</h4>
              {d.coverage.map((c) => (
                <p key={c.currency}>
                  {c.currency}: {c.status} · UTC [{c.start}, {c.end}) · verified{" "}
                  {c.verified_at ?? "never"}
                </p>
              ))}
              <h4 className="font-bold">
                Recent persisted event lifecycle (20 maximum)
              </h4>
              <p className="text-xs">
                Actual outbox and processing receipts. Process/error history
                without a persisted record is unavailable.
              </p>
              {d.recent_logs.map((e) => (
                <div
                  key={e.correlation_id}
                  className="text-xs border-t border-emerald-900 py-2 break-all"
                >
                  {e.timestamp} · {e.component} · {e.severity} · tenant{" "}
                  {e.tenant_id}
                  <br />
                  {e.event_type} · correlation {e.correlation_id}
                  <br />
                  Published {e.published_at ?? "pending"} · Processed{" "}
                  {e.processed_at ?? "no receipt"} · Stream{" "}
                  {e.stream_id ?? "pending"}
                </div>
              ))}
            </>
          )}
        </section>
      </div>
    </div>
  );
};
