export type Currency = "USD" | "EUR" | "GBP" | "JPY" | "CAD" | "AUD";
export type Interval = "hour" | "day";

export interface Freshness {
  checkpoint?: string | null;
  last_processed_at?: string | null;
  tenant_revision?: number | null;
}

export interface SummaryResponse {
  tenant_id: string;
  start: string;
  end: string;
  currency: Currency;
  metric_semantics: string;
  freshness?: Freshness | null;
  total_order_count: number;
  sales_amount: string;
}

export interface TrendBucket {
  bucket_start: string;
  total_order_count: number;
  sales_amount: string;
}

export interface TrendsResponse {
  tenant_id: string;
  start: string;
  end: string;
  currency: Currency;
  interval: Interval;
  metric_semantics: string;
  freshness?: Freshness | null;
  buckets: TrendBucket[];
}

export interface ProductMetric {
  product_name?: string;
  sku?: string;
  product_id: string;
  total_order_count: number;
  sales_amount: string;
}

export interface TopProductsResponse {
  tenant_id: string;
  start: string;
  end: string;
  currency: Currency;
  limit: number;
  metric_semantics: string;
  freshness?: Freshness | null;
  products: ProductMetric[];
}

export interface StatusMetric {
  status: "placed" | "shipped" | "cancelled";
  total_order_count: number;
  sales_amount: string;
}

export interface StatusBreakdownResponse {
  tenant_id: string;
  start: string;
  end: string;
  currency: Currency;
  metric_semantics: string;
  freshness?: Freshness | null;
  statuses: StatusMetric[];
}

export interface Tenant {
  id: string;
  name: string;
  created_at: string;
}

export interface TenantStatus {
  tenant_id: string;
  name: string;
  status: string;
  database_name: string;
  database_port: number;
  schema_version: string;
  order_count: number;
  total_outbox: number;
  unpublished_outbox: number;
  is_healthy: boolean;
  last_verified_at?: string | null;
  projection_count: number;
  aggregate_revision: number | null;
  last_processed_at: string | null;
  last_source_commit_at: string | null;
  last_source_event_at: string | null;
  covered_pending_history: number;
  pending_live_events: number;
  coverage: {
    currency: string;
    status: string;
    start: string;
    end: string;
    verified_at: string | null;
  }[];
  recent_logs: any[];
  write_protected: boolean;
  worker_health: string;
  checkpoint: string | null;
}

export interface RowError {
  line_number: number;
  column: string;
  message: string;
  raw_value?: string;
}

export interface IngestionPreviewRow {
  line_number: number;
  order_id: string;
  product_id: string;
  raw_product_id: string;
  amount: string;
  currency: string;
  status: string;
  created_at: string;
}

export interface IngestionResult {
  total_rows: number;
  accepted: number;
  rejected: number;
  duplicates: number;
  errors: RowError[];
  preview_rows: IngestionPreviewRow[];
  imported_order_ids: string[];
  total_sales: string | null;
  total_amount: string | null;
  dry_run: boolean;
  totals_by_currency: Record<
    string,
    { order_count: number; sales_amount: string; total_amount: string }
  >;
  progress?: any;
  processing_error?: { stage: string; message: string } | null;
}

export interface AssistantResponse {
  reply: string;
  intent: string;
  provider: "groq" | "grounded_rule_engine";
  model: string;
  endpoints_called: string[];
  data: Record<string, any>;
  freshness?: Freshness | null;
  provider_error?: string | null;
}

export interface OpsSummary {
  status: string;
  deployment_mode: string;
  active_tenants: number;
  analytics_backend: string;
  benchmark_milestone: {
    tenants: number;
    orders_per_tenant: number;
    total_orders: number;
    target_p95_ms: number;
    summary_p95_ms: number;
    trends_p95_ms: number;
    top_products_p95_ms: number;
    status_breakdown_p95_ms: number;
    architecture: string;
  };
}
