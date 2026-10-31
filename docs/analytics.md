# Analytics contract

Metric identifier: **created_at_shipped_sales_v2**. Event schema 2 is unchanged.
Recognized sales sum shipped amounts; placed/cancelled sales are zero. Total count
includes every current order once, including cancellations. Current state is
attributed to immutable created_at. Product reassignment reverses the old product
count and applies the new; placed repricing recognizes no sales until shipment.

| GET path | Additional parameters | Response |
| --- | --- | --- |
| /analytics/summary | none | total_order_count, sales_amount |
| /analytics/trends | interval=day or hour | exact UTC buckets, filled known zeros |
| /analytics/top-products | limit=1..10, default 10 | positive sales, sales/count descending then UUID ascending |
| /analytics/status-breakdown | none | placed, shipped, cancelled, always all three |

All require verified bearer authorization, tenant_id UUID, currency (uppercase
three-letter), start/end UTC ISO timestamps. Windows are half-open, at most 366 days,
and align to full hours; daily trends align to midnight. Unsupported windows or
intervals return 422. No currency conversion or mixing is performed. Decimal money
is serialized as strings. Empty covered summary/status metrics are zero; trends
contain explicit zero buckets; products are empty. Summary intentionally exposes
count/sales only; the internal reader's AOV uses shipped count and returns zero
when no shipped orders exist.

Shared modules: analytics/contracts.py (Pydantic), analytics/reader.py (protocol),
analytics/coverage.py (CoverageProof), analytics/postgres.py (adapter). B's
aggregation/reader.py imports the same protocol/DTOs and exposes capabilities,
summary, trends, top_products, status_breakdown plus its SQL get_* helpers.
Only the adapter is wired into FastAPI. SQL sorts/filter products before LIMIT.

Default settings use the real PostgreSQL adapter and durable
`astraroll.aggregation.coverage:read_coverage`. Missing/partial/invalid/stale metadata
returns 503, never fake success. Initialization is explicit, including empty
tenants. A revision is not completeness. Queries outside a verified window return
422. Coverage advertises both hourly/daily and all three dimensions only after all
six rollups pass exact oracle validation. The coverage TTL defaults to 86400 seconds;
explicit successful reconciliation refreshes it without certifying uninitialized data.

Reads use read-only REPEATABLE READ: coverage, processing revision and metrics share
one snapshot. No orders/outbox/projection scan and no Redis fallback occurs on normal
analytics calls. Optional freshness includes a genuine tenant revision and last
successful applied time. The checkpoint stays null: last applied Redis ID is not a
contiguous watermark. Coverage proves a source baseline plus transactional live
maintenance, not zero ingestion lag. Admin diagnostics report unapplied source
versions separately. A bootstrap can initialize revision without inventing an apply
timestamp.

All tenant/order routes now share the same expiring opaque bearer authentication.
Trusted configuration stores SHA-256 digests, subjects, tenant memberships, expiry
and an optional strict admin flag. Invalid credentials 401; membership/admin denial
403; malformed/empty server registry 503. Remove a record and restart to revoke.
No RLS or external identity provider is claimed. See README for key issuance.

Live verification: RUN_INTEGRATION=1 python -m pytest tests/test_analytics_integration.py
uses actual source/outbox, Redis relay/consumer, durable bootstrap, real PostgreSQL
reads and verified keys. It audits executed analytics SQL for raw table access,
checks multiple currencies/tenants, ranking LIMIT ties, exact buckets, revision
snapshot consistency, replay, stale versions and quarantine. Release tests additionally
cover interrupted initialization, catchup, archived repair, migration preservation,
coverage expiry and producer authorization. Test fakes remain only for isolated
contract-level unit tests.
