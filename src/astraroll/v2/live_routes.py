"""Product APIs: evidence-based ingestion, read-only SQL parity, and bounded telemetry."""

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from astraroll.analytics.auth import get_authorized_path_tenant
from astraroll.analytics.contracts import AnalyticsQuery
from astraroll.analytics.routes import Reader, Tenant, get_summary_query, read
from astraroll.config import get_settings
from astraroll.v2.live_ingestion import advance_import, import_progress

router = APIRouter()


def manager():
    if get_settings().deployment_mode != "database_per_tenant":
        raise HTTPException(503, "Live product features require database-per-tenant mode")
    from astraroll.v2.runtime import get_tenant_engine_manager

    return get_tenant_engine_manager()


def protect_benchmark(tenant_id: UUID):
    from astraroll.v2.runtime import get_control_registry

    if get_settings().deployment_mode == "database_per_tenant":
        record = get_control_registry().get_tenant(tenant_id)
        if record and record.database_name.startswith("v2bench_"):
            raise HTTPException(
                409,
                "Benchmark tenant is read-only. Select an isolated demo tenant.",
            )


class ProgressRequest(BaseModel):
    order_ids: list[UUID] = Field(min_length=1, max_length=10000)
    advance: bool = False


@router.post(
    "/tenants/{tenant_id}/ingest/progress", dependencies=[Depends(get_authorized_path_tenant)]
)
def progress(tenant_id: UUID, payload: ProgressRequest):
    mgr = manager()
    error = None
    if payload.advance:
        protect_benchmark(tenant_id)
        error = advance_import(tenant_id, payload.order_ids, mgr)
    return {**import_progress(tenant_id, payload.order_ids, mgr), "processing_error": error}


def commit_import(tenant_id, content, dry_run, trigger_relay):
    from astraroll.v2.ingestion import ingest_csv_data, validate_and_parse_csv

    if dry_run:
        return asdict(ingest_csv_data(None, tenant_id, content, dry_run=True))
    mgr = manager()
    protect_benchmark(tenant_id)
    with mgr.session(tenant_id) as session, session.begin():
        result = ingest_csv_data(session, tenant_id, content)
    ids = [UUID(i) for i in result.imported_order_ids]
    catalog_error = None
    try:
        from astraroll.v2.products import register_product

        rows, _ = validate_and_parse_csv(content, id_namespace=tenant_id)
        for row in rows:
            if row.product_name and row.order_id in set(ids):
                register_product(tenant_id, row.product_id, row.product_name, row.raw_product_id)
    except SQLAlchemyError:
        catalog_error = "Orders committed; optional product catalog update failed"
    processing_error = advance_import(tenant_id, ids, mgr) if ids and trigger_relay else None
    state = import_progress(tenant_id, ids, mgr) if ids else {"stage": "NO_NEW_ORDERS"}
    return {
        **asdict(result),
        "progress": state,
        "processing_error": processing_error,
        "catalog_error": catalog_error,
    }


@router.post("/tenants/{tenant_id}/ingest/csv", dependencies=[Depends(get_authorized_path_tenant)])
async def ingest(
    tenant_id: UUID,
    request: Request,
    dry_run: bool = Query(False),
    trigger_relay: bool = Query(True),
    file: UploadFile | None = File(None),
):
    raw = await file.read(5 * 1024 * 1024 + 1) if file else await request.body()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "CSV payload exceeds 5MB limit")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "CSV must be UTF-8") from None
    if not content.strip():
        raise HTTPException(400, "Empty CSV content provided")
    return await run_in_threadpool(commit_import, tenant_id, content, dry_run, trigger_relay)


class CatalogUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_name: str = Field(min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=100)


@router.put(
    "/tenants/{tenant_id}/products/{product_id}", dependencies=[Depends(get_authorized_path_tenant)]
)
def update_product(tenant_id: UUID, product_id: UUID, payload: CatalogUpdate):
    from astraroll.v2.products import register_product

    protect_benchmark(tenant_id)
    # Also resolves/validates an active tenant before touching catalog metadata.
    manager().get_engine(tenant_id)
    return register_product(tenant_id, product_id, payload.product_name, payload.sku)


@router.get("/tenants/{tenant_id}/products", dependencies=[Depends(get_authorized_path_tenant)])
def product_names(tenant_id: UUID, product_ids: str = Query(..., max_length=4000)):
    from astraroll.v2.products import resolve_products

    try:
        ids = [UUID(p) for p in product_ids.split(",")]
        if len(ids) > 100:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "Supply 1..100 product UUIDs") from None
    manager().get_engine(tenant_id)
    return resolve_products(tenant_id, ids)


@router.get("/tenants/{tenant_id}/status", dependencies=[Depends(get_authorized_path_tenant)])
def tenant_status(tenant_id: UUID):
    from astraroll.v2.runtime import get_control_registry

    mgr = manager()
    record = get_control_registry().get_tenant(tenant_id)
    if record is None:
        raise HTTPException(404, "Tenant not registered")
    try:
        with mgr.session(tenant_id) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout='3000ms'"))
            params = {"tid": tenant_id}
            counts = (
                session.execute(
                    text("""SELECT
              (SELECT count(*) FROM orders WHERE tenant_id=:tid) AS order_count,
              (SELECT count(*) FROM order_projections WHERE tenant_id=:tid) AS projection_count,
              (SELECT count(*) FROM outbox_events WHERE tenant_id=:tid) AS total_outbox,
              (SELECT count(*) FROM outbox_events WHERE tenant_id=:tid
                  AND published_at IS NULL) AS unpublished_outbox,
              (SELECT count(*) FROM outbox_events e JOIN order_projections p ON
                 p.tenant_id=e.tenant_id AND p.order_id=e.order_id
                 WHERE e.tenant_id=:tid AND e.published_at IS NULL
                   AND p.last_applied_version >= e.order_version) AS covered_pending_history,
              (SELECT count(*) FROM outbox_events e LEFT JOIN order_projections p ON
                 p.tenant_id=e.tenant_id AND p.order_id=e.order_id
                 WHERE e.tenant_id=:tid AND (p.last_applied_version IS NULL
                   OR p.last_applied_version < e.order_version)) AS pending_unapplied_events,
              (SELECT max(occurred_at) FROM outbox_events
                 WHERE tenant_id=:tid) AS last_source_event_at
            """),
                    params,
                )
                .mappings()
                .one()
            )
            state = (
                session.execute(
                    text("""SELECT revision, last_successful_apply_at,
              last_successful_stream_id FROM tenant_processing_state WHERE tenant_id=:tid"""),
                    params,
                )
                .mappings()
                .first()
            )
            coverage = (
                session.execute(
                    text("""SELECT currency,status,start,"end",verified_at,
              source_order_count FROM aggregate_coverage WHERE tenant_id=:tid ORDER BY currency"""),
                    params,
                )
                .mappings()
                .all()
            )
            logs = (
                session.execute(
                    text("""SELECT e.id AS correlation_id, e.order_id, e.event_type,
              e.occurred_at AS timestamp, e.published_at, e.stream_id, p.processed_at
              FROM outbox_events e LEFT JOIN processed_events p ON p.event_id=e.id
              WHERE e.tenant_id=:tid ORDER BY e.occurred_at DESC,e.id DESC LIMIT 20"""),
                    params,
                )
                .mappings()
                .all()
            )
            schema = session.scalar(text("SELECT version_num FROM alembic_version"))
            port = session.scalar(text("SELECT inet_server_port()"))
            return {
                "tenant_id": tenant_id,
                "name": record.name,
                "status": record.status,
                "database_name": record.database_name,
                "database_port": port,
                "schema_version": schema,
                "is_healthy": True,
                **dict(counts),
                "pending_live_events": counts.pending_unapplied_events,
                "aggregate_revision": state.revision if state else None,
                "last_processed_at": state.last_successful_apply_at if state else None,
                "last_source_commit_at": None,
                "checkpoint": state.last_successful_stream_id if state else None,
                "coverage": [dict(c) for c in coverage],
                "recent_logs": [
                    {
                        **dict(e),
                        "tenant_id": tenant_id,
                        "component": "outbox/processor",
                        "severity": "INFO",
                        "evidence": "persisted event receipt",
                    }
                    for e in logs
                ],
                "worker_health": "unknown (no worker heartbeat configured)",
                "write_protected": record.database_name.startswith("v2bench_"),
                "checked_at": datetime.now(UTC),
            }
    except SQLAlchemyError:
        raise HTTPException(
            503, "Tenant telemetry unavailable; no zero counters substituted"
        ) from None


@router.get("/analytics/verify")
def verify(
    query: Annotated[AnalyticsQuery, Depends(get_summary_query)], tenant: Tenant, reader: Reader
):
    """Independent raw orders and rollup SQL, not arbitrary client SQL; manual only."""
    api = read(query, tenant, reader, lambda: reader.summary(query))
    mgr = manager()
    start = perf_counter()
    try:
        with mgr.session(query.tenant_id) as s, s.begin():
            s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            s.execute(text("SET LOCAL statement_timeout='3000ms'"))
            args = dict(tid=query.tenant_id, curr=query.currency, start=query.start, end=query.end)
            raw = s.execute(
                text("""SELECT count(*),coalesce(sum(
                  CASE WHEN status='shipped' THEN amount ELSE 0 END),0)
               FROM orders WHERE tenant_id=:tid AND currency=:curr
                  AND created_at>=:start AND created_at<:end"""),
                args,
            ).one()
            rollup = s.execute(
                text("""SELECT coalesce(sum(order_count),0),coalesce(sum(sales_amount),0)
               FROM hourly_summary_aggregates WHERE tenant_id=:tid AND currency=:curr
                  AND bucket_start>=:start AND bucket_start<:end"""),
                args,
            ).one()
            revision = s.scalar(
                text("SELECT revision FROM tenant_processing_state WHERE tenant_id=:tid"), args
            )
            database = s.scalar(text("SELECT current_database()"))
    except SQLAlchemyError:
        raise HTTPException(503, "Bounded read-only verification failed or timed out") from None

    def value(count, amount):
        return {"total_orders": int(count), "total_shipped_sales": f"{Decimal(amount):.2f}"}

    api_value = value(api.value.total_order_count, api.value.sales_amount)
    raw_value, rollup_value = value(*raw), value(*rollup)
    concurrent = api.freshness is None or api.freshness.tenant_revision != revision
    status = (
        "INCONCLUSIVE_CONCURRENT_CHANGE"
        if concurrent
        else ("MATCH" if api_value == raw_value == rollup_value else "MISMATCH")
    )
    return {
        "tenant_id": query.tenant_id,
        "currency": query.currency,
        "start": query.start,
        "end": query.end,
        "database_name": database,
        "verified_at": datetime.now(UTC),
        "duration_ms": round((perf_counter() - start) * 1000, 3),
        "status": status,
        "api": api_value,
        "rollup": rollup_value,
        "raw_orders": raw_value,
        "data_source": "authorized tenant database, raw orders + hourly rollups; API reader",
        "explanation": "Independent SQL comparison at a stable aggregate revision"
        if not concurrent
        else "Retry: aggregate revision changed between API and SQL snapshots",
    }
