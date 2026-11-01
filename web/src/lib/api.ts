import {
  Tenant,
  SummaryResponse,
  TrendsResponse,
  TopProductsResponse,
  StatusBreakdownResponse,
  AssistantResponse,
  IngestionResult,
  TenantStatus,
  Interval,
} from "../types";

const API_BASE = "";

const DEFAULT_AUTH_TOKEN = "";

export function extractErrorMessage(errPayload: any, fallback: string): string {
  if (!errPayload) return fallback;
  if (typeof errPayload === "string") return errPayload;
  if (typeof errPayload.detail === "string") return errPayload.detail;
  if (errPayload.detail && typeof errPayload.detail.message === "string")
    return errPayload.detail.message;
  if (Array.isArray(errPayload.detail) && errPayload.detail.length > 0) {
    return errPayload.detail
      .map((e: any) => e.msg || e.message || JSON.stringify(e))
      .join(", ");
  }
  if (typeof errPayload.message === "string") return errPayload.message;
  try {
    return JSON.stringify(errPayload);
  } catch {
    return fallback;
  }
}

function getHeaders(token?: string): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const active =
    token ||
    (typeof window !== "undefined"
      ? sessionStorage.getItem("astraroll_token")
      : null) ||
    DEFAULT_AUTH_TOKEN;
  if (active) {
    headers["Authorization"] = `Bearer ${active}`;
  }
  return headers;
}

export async function fetchTenants(
  token?: string,
  signal?: AbortSignal,
): Promise<Tenant[]> {
  const res = await fetch(`${API_BASE}/tenants`, {
    headers: getHeaders(token),
    signal,
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Tenant registry ${res.status}: ${res.statusText}`);
  }
  return await res.json();
}

export async function fetchTenantStatus(
  tenantId: string,
  token?: string,
  signal?: AbortSignal,
): Promise<TenantStatus> {
  const res = await fetch(`${API_BASE}/tenants/${tenantId}/status`, {
    headers: getHeaders(token),
    signal,
    cache: "no-store",
  });
  if (res.ok) {
    return await res.json();
  }
  throw new Error(`Tenant telemetry request failed (${res.status})`);
}

export async function fetchSummary(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  token?: string,
  signal?: AbortSignal,
): Promise<SummaryResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
  });
  const res = await fetch(
    `${API_BASE}/analytics/summary?${params.toString()}`,
    {
      headers: getHeaders(token),
      signal,
      cache: "no-store",
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(err, `Summary request failed (${res.status})`),
    );
  }
  return await res.json();
}

export async function fetchTrends(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  interval: Interval = "day",
  token?: string,
  signal?: AbortSignal,
): Promise<TrendsResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
    interval,
  });
  const res = await fetch(`${API_BASE}/analytics/trends?${params.toString()}`, {
    headers: getHeaders(token),
    signal,
    cache: "no-store",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(err, `Trends request failed (${res.status})`),
    );
  }
  return await res.json();
}

export async function fetchTopProducts(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  limit: number = 10,
  token?: string,
  signal?: AbortSignal,
): Promise<TopProductsResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
    limit: String(limit),
  });
  const res = await fetch(
    `${API_BASE}/analytics/top-products?${params.toString()}`,
    {
      headers: getHeaders(token),
      signal,
      cache: "no-store",
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(err, `Top products request failed (${res.status})`),
    );
  }
  const data: TopProductsResponse = await res.json();
  if (data.products.length) {
    try {
      const names = await apiJson(
        `/tenants/${tenantId}/products?product_ids=${data.products.map((p) => p.product_id).join(",")}`,
        token,
        signal,
      );
      data.products = data.products.map((p) => ({
        ...p,
        product_name: names[p.product_id]?.product_name,
        sku: names[p.product_id]?.sku,
      }));
    } catch (e) {
      if (signal?.aborted) throw e;
    }
  }
  return data;
}

export async function fetchStatusBreakdown(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  token?: string,
  signal?: AbortSignal,
): Promise<StatusBreakdownResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
  });
  const res = await fetch(
    `${API_BASE}/analytics/status-breakdown?${params.toString()}`,
    {
      headers: getHeaders(token),
      signal,
      cache: "no-store",
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(
          err,
          `Status breakdown request failed (${res.status})`,
        ),
    );
  }
  return await res.json();
}

export async function ingestCsv(
  tenantId: string,
  csvContent: string,
  dryRun: boolean = false,
  triggerRelay: boolean = true,
  token?: string,
  signal?: AbortSignal,
): Promise<IngestionResult> {
  const params = new URLSearchParams({
    dry_run: String(dryRun),
    trigger_relay: String(triggerRelay),
  });
  const headers = getHeaders(token);
  headers["Content-Type"] = "text/csv";
  const res = await fetch(
    `${API_BASE}/tenants/${tenantId}/ingest/csv?${params.toString()}`,
    {
      method: "POST",
      headers,
      body: csvContent,
      signal,
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(err, `Ingestion failed (${res.status})`),
    );
  }
  return await res.json();
}

export async function askAssistant(
  tenantId: string,
  query: string,
  currency: string = "USD",
  start?: string,
  end?: string,
  token?: string,
  signal?: AbortSignal,
): Promise<AssistantResponse> {
  const headers = getHeaders(token);
  headers["Content-Type"] = "application/json";
  const body: Record<string, any> = {
    query,
    currency,
  };
  if (start) body.start = start;
  if (end) body.end = end;
  const res = await fetch(`${API_BASE}/tenants/${tenantId}/assistant`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(err, `Assistant request failed (${res.status})`),
    );
  }
  return await res.json();
}

export async function apiJson(
  path: string,
  token?: string,
  signal?: AbortSignal,
  body?: unknown,
) {
  const res = await fetch(path, {
    headers: {
      ...getHeaders(token),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    signal,
    cache: "no-store",
    method: body ? "POST" : "GET",
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok)
    throw new Error(
      `${res.status}: ` +
        extractErrorMessage(await res.json().catch(() => null), res.statusText),
    );
  return res.json();
}
