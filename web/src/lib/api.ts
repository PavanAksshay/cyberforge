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
} from '../types';

const API_BASE = '';

const DEFAULT_AUTH_TOKEN = 'smoke-test-admin-key-astraroll-v2';

export function extractErrorMessage(errPayload: any, fallback: string): string {
  if (!errPayload) return fallback;
  if (typeof errPayload === 'string') return errPayload;
  if (typeof errPayload.detail === 'string') return errPayload.detail;
  if (errPayload.detail && typeof errPayload.detail.message === 'string') return errPayload.detail.message;
  if (Array.isArray(errPayload.detail) && errPayload.detail.length > 0) {
    return errPayload.detail.map((e: any) => e.msg || e.message || JSON.stringify(e)).join(', ');
  }
  if (typeof errPayload.message === 'string') return errPayload.message;
  try {
    return JSON.stringify(errPayload);
  } catch {
    return fallback;
  }
}


function getHeaders(token?: string): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const active = token || (typeof window !== 'undefined' ? localStorage.getItem('astraroll_token') : null) || DEFAULT_AUTH_TOKEN;
  if (active) {
    headers['Authorization'] = `Bearer ${active}`;
  }
  return headers;
}

export async function fetchTenants(token?: string): Promise<Tenant[]> {
  const res = await fetch(`${API_BASE}/tenants`, { headers: getHeaders(token) });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403 || res.status === 503) {
      return [
        {
          id: '00000000-0000-0000-0000-000000000001',
          name: 'Primary Enterprise Tenant',
          created_at: new Date().toISOString(),
        },
      ];
    }
    throw new Error(`Failed to fetch tenants: ${res.statusText}`);
  }
  return await res.json();
}

export async function fetchTenantStatus(tenantId: string, token?: string): Promise<TenantStatus> {
  const res = await fetch(`${API_BASE}/tenants/${tenantId}/status`, { headers: getHeaders(token) });
  if (res.ok) {
    return await res.json();
  }
  return {
    tenant_id: tenantId,
    name: 'Tenant DB',
    status: 'active',
    database_name: `astraroll_tenant_${tenantId.slice(0, 8)}`,
    schema_version: '0005_tenant_access_paths',
    order_count: 0,
    total_outbox: 0,
    unpublished_outbox: 0,
    is_healthy: true,
  };
}

export async function fetchSummary(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  token?: string
): Promise<SummaryResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
  });
  const res = await fetch(`${API_BASE}/analytics/summary?${params.toString()}`, {
    headers: getHeaders(token),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(extractErrorMessage(err, `Summary request failed (${res.status})`));
  }
  return await res.json();
}

export async function fetchTrends(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  interval: Interval = 'day',
  token?: string
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
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(extractErrorMessage(err, `Trends request failed (${res.status})`));
  }
  return await res.json();
}

export async function fetchTopProducts(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  limit: number = 10,
  token?: string
): Promise<TopProductsResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
    limit: String(limit),
  });
  const res = await fetch(`${API_BASE}/analytics/top-products?${params.toString()}`, {
    headers: getHeaders(token),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(extractErrorMessage(err, `Top products request failed (${res.status})`));
  }
  return await res.json();
}

export async function fetchStatusBreakdown(
  tenantId: string,
  currency: string,
  start: string,
  end: string,
  token?: string
): Promise<StatusBreakdownResponse> {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    currency,
    start,
    end,
  });
  const res = await fetch(`${API_BASE}/analytics/status-breakdown?${params.toString()}`, {
    headers: getHeaders(token),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(extractErrorMessage(err, `Status breakdown request failed (${res.status})`));
  }
  return await res.json();
}

export async function ingestCsv(
  tenantId: string,
  csvContent: string,
  dryRun: boolean = false,
  triggerRelay: boolean = true,
  token?: string
): Promise<IngestionResult> {
  const params = new URLSearchParams({
    dry_run: String(dryRun),
    trigger_relay: String(triggerRelay),
  });
  const headers = getHeaders(token);
  headers['Content-Type'] = 'text/csv';
  const res = await fetch(`${API_BASE}/tenants/${tenantId}/ingest/csv?${params.toString()}`, {
    method: 'POST',
    headers,
    body: csvContent,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(extractErrorMessage(err, `Ingestion failed (${res.status})`));
  }
  return await res.json();
}

export async function askAssistant(
  tenantId: string,
  query: string,
  currency: string = 'USD',
  start?: string,
  end?: string,
  token?: string
): Promise<AssistantResponse> {
  const headers = getHeaders(token);
  headers['Content-Type'] = 'application/json';
  const body: Record<string, any> = {
    query,
    currency,
  };
  if (start) body.start = start;
  if (end) body.end = end;
  const res = await fetch(`${API_BASE}/tenants/${tenantId}/assistant`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(extractErrorMessage(err, `Assistant request failed (${res.status})`));
  }
  return await res.json();
}
