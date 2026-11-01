# V2 frontend integration contract

Scope: freeze the implemented dashboard/order API. CSV import, import tracking and
an analytics-assistant backend are **not implemented** and must not be presented as
working backend features. The exported [OpenAPI](v2-openapi.json) is authoritative
for available route and DTO shapes. No password/OAuth login or public key-issuance
endpoint exists: opaque expiring bearer keys are issued by trusted server tooling.

## Identity and routing

Send Authorization: Bearer <issued-key>. Server configuration stores only the key's
SHA-256 digest, subject, tenant memberships, expiry and optional admin scope. A UUID
is context, not a credential. Tenant selector options must come from trusted setup;
there is no /me or tenant-membership-discovery API. Never mint memberships in the
browser or expose control/tenant database URLs. All requests use V2 control routing,
not the archival V1 database. There is no cross-origin CORS middleware: use a
same-origin reverse proxy or frontend development proxy for /analytics, /tenants,
/ops and /health. Do not assume direct cross-origin browser fetch is supported.

## Dashboard reads

All four GET routes require tenant_id UUID, currency (three uppercase letters),
start and end (explicit ISO UTC, whole hours). Window is [start,end), max 366 days.
Non-UTC offsets, partial hours, reversed ranges and unsupported coverage fail closed.
Daily trends require midnight boundaries. Dates refer to immutable order creation
cohorts, not the date on which a status update arrived.

| Route | Extra query | Response fields in addition to common context |
| --- | --- | --- |
| /analytics/summary | none | total_order_count, sales_amount |
| /analytics/trends | interval=day or hour; default day | interval, buckets[] |
| /analytics/top-products | limit 1..10; default 10 | limit, products[] |
| /analytics/status-breakdown | none | statuses[] (placed, shipped, cancelled) |

Common context: tenant_id/start/end/currency/metric_semantics and optional freshness.
metric_semantics is created_at_shipped_sales_v2. Counts include cancelled orders
once. Only shipped orders contribute recognized sales; placed/cancelled sales are
zero. Currency always partitions counts and amounts: a USD count is not the tenant's
USD+EUR count. Do not add monetary values across currencies. Monetary JSON values
are Decimal strings; preserve precision when rendering/calculating. API timestamps
are UTC and IDs are UUID strings. Trend rows contain bucket_start/count/sales; empty
buckets are filled with exact zero. Products contain product_id/count/sales and are
positive-sales only, sorted sales DESC, count DESC, UUID ASC before limit. No product
name/catalog endpoint exists. Status rows contain status/count/sales for all three
statuses. Empty initialized windows are zeros/empty products; uninitialized or stale
coverage returns 503, never a fabricated zero result.

freshness.tenant_revision and freshness.last_processed_at come from durable state;
display the latter as last aggregate application, not an invented global stream
checkpoint. Optional checkpoint may be absent. Certified coverage can age out
(default 24h); surface unavailable state and request verified maintenance rather
than extending freshness in the browser. A new update may change sales in an older
creation cohort; refetch all affected dashboard cards after visibility is confirmed.

## Order operations and pagination

| Method/path | Input/result |
| --- | --- |
| GET /tenants/{tenant_id} | authorized TenantRead |
| GET /tenants/{tenant_id}/orders | limit default 100, 1..500; offset >=0; OrderRead[] |
| POST /tenants/{tenant_id}/orders | product_id, amount, currency; 201 OrderRead |
| GET /tenants/{tenant_id}/orders/{order_id} | OrderRead |
| PATCH /tenants/{tenant_id}/orders/{order_id} | expected_version and non-null amount, product_id and/or status |
| POST /tenants/{tenant_id}/orders/{order_id}/cancel | expected_version; OrderRead |
| POST /tenants | admin-only TenantCreate(name); TenantRead |

List order is created_at,id. Offset pagination is implemented; cursor pagination,
status/search/date filters are not. Every OrderRead includes id,tenant_id,product_id,
amount,currency,status,version,created_at,updated_at. Create starts placed with a
server UTC timestamp/version 1. Currency is immutable. Status mutation accepts shipped
or cancelled; terminal orders cannot be reopened. Send expected_version from the
latest response and handle 409 by reloading, never blindly retrying a stale mutation.
No delete route exists. Producer data and its outbox event commit atomically; read
analytics visibility is asynchronous. OrderCreate does not accept historical
created_at: do not implement CSV historical import by looping this endpoint.

## Errors and operations

401: absent/invalid/expired identity. 403: wrong membership/admin scope. 404: missing
authorized resource. 409: version/transition conflict. 422: invalid request/date/grain/
limit. 503: unavailable registry/tenant DB, incompatible/uninitialized/stale coverage
or verifier. Preserve context and offer retry on transient 503; do not show old cached
numbers as a fresh successful response. Error bodies may be a detail string or
structured detail object; validate per OpenAPI/route contract. Driver details and
credentials are not exposed. /health/live and /health/ready are liveness/readiness;
readiness samples a tenant, so it is not a guarantee that every tenant is available.
/ops/metrics is administrator-only and is not a dashboard/user endpoint or a
replacement for data oracles. In V2 its SQL diagnostics route to the selected tenant;
Redis diagnostics are global operational metadata, not tenant financial metrics.

## Unavailable frontend flows and future work

CSV upload, import creation/status/listing, rejected-row download, idempotent import
retry, and a read-only analytics-assistant endpoint have no routes or DTOs today.
Do not call invented /imports or /assistant URLs or fabricate completion percentages.
Disable these controls with a clear unavailable state. Before implementation, agree
on tenant-scoped import IDs, bounded validation/chunks, exact Decimal/currency/UTC
mapping, duplicate policy, durable job progress, partial-failure/retry semantics and
authorized status/error retrieval. Any assistant must use authorized query contracts,
preserve currency/cohort semantics and never accept arbitrary SQL or perform writes.
These are requirements to resolve, not published endpoints or a shipped AI feature.

Current backend is eligible for authenticated read-only dashboard and order UI
integration when release gates pass. It is not a completed backend for every CSV/
import/assistant flow. Historical transport backlog and narrow latency margins are
separately documented in the release report; do not advertise a global real-time SLA.
