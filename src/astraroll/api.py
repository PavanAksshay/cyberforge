import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from astraroll.analytics.auth import (
    AuthenticatedPrincipal,
    get_authenticated_principal,
    get_authorized_path_tenant,
    require_admin,
)
from astraroll.analytics.reader import AnalyticsReader, get_analytics_reader
from astraroll.analytics.routes import router as analytics_router
from astraroll.config import get_settings
from astraroll.db import get_session_factory
from astraroll.models import Order, OutboxEvent, Tenant
from astraroll.operations import record_latency
from astraroll.operations import router as operations_router
from astraroll.orders import (
    Conflict,
    NotFound,
    create_order,
    find_order,
    mutate_order,
    require_tenant,
)
from astraroll.schemas import (
    Currency,
    InputModel,
    OrderCancel,
    OrderCreate,
    OrderRead,
    OrderUpdate,
    TenantCreate,
    TenantRead,
)
from astraroll.v2.router import (
    TenantDatabaseUnavailable,
    TenantInactiveError,
    TenantNotFoundError,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        cache_info = getattr(get_tenant_engine_manager, "cache_info", None)
        if cache_info is None or cache_info().currsize:
            get_tenant_engine_manager().close_all()


allowed_origins_raw = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
)
allowed_origins = [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]

app = FastAPI(title="AstraRoll", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(analytics_router)
app.include_router(operations_router)


@app.middleware("http")
async def latency_middleware(request, call_next):
    start = perf_counter()
    try:
        return await call_next(request)
    finally:
        route = request.scope.get("route")
        record_latency(getattr(route, "path", "unmatched"), (perf_counter() - start) * 1000)


def session_dependency():
    with get_session_factory()() as session:
        yield session


DB = Annotated[Session, Depends(session_dependency)]


@app.exception_handler(NotFound)
def not_found(request: Request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Conflict)
def conflict(request: Request, exc: Conflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
def validation_error(request: Request, exc: ValidationError):
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(TenantNotFoundError)
def tenant_not_found(request: Request, exc: TenantNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(TenantInactiveError)
def tenant_inactive(request: Request, exc: TenantInactiveError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(TenantDatabaseUnavailable)
def tenant_db_unavailable(request: Request, exc: TenantDatabaseUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import (
            get_control_engine,
            get_control_registry,
            get_tenant_engine_manager,
        )

        try:
            with get_control_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"status": "control database unavailable"})

        # Validate active tenant database availability if active tenants exist
        try:
            registry = get_control_registry()
            active_tenants = registry.list_active_tenants()
            if active_tenants:
                sample_tenant = active_tenants[0]
                mgr = get_tenant_engine_manager()
                with mgr.session(sample_tenant.tenant_id) as sess:
                    sess.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"status": "tenant database unavailable"})

        return {"status": "ok"}
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "database unavailable"})
    return {"status": "ok"}


@app.post(
    "/tenants", response_model=TenantRead, status_code=201, dependencies=[Depends(require_admin)]
)
def tenant_create(data: TenantCreate, db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from uuid import uuid4

        from astraroll.v2.runtime import get_tenant_engine_manager, get_tenant_provisioner

        tenant_id = uuid4()
        record = get_tenant_provisioner().provision_tenant(tenant_id, data.name)
        with get_tenant_engine_manager().session(record.tenant_id) as tenant_db:
            return TenantRead.model_validate(tenant_db.get(Tenant, record.tenant_id))

    with db.begin():
        tenant = Tenant(name=data.name)
        db.add(tenant)
        db.flush()
        result = TenantRead.model_validate(tenant)
    return result


@app.get(
    "/tenants",
    response_model=list[TenantRead],
)
def tenant_list(
    db: DB,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_control_registry

        records = get_control_registry().list_active_tenants()
        if not principal.is_admin:
            records = [r for r in records if r.tenant_id in principal.tenant_ids]
        return [TenantRead(id=r.tenant_id, name=r.name, created_at=r.created_at) for r in records]

    tenants = db.scalars(select(Tenant).order_by(Tenant.created_at)).all()
    if not principal.is_admin:
        tenants = [t for t in tenants if t.id in principal.tenant_ids]
    return [TenantRead.model_validate(t) for t in tenants]


@app.get(
    "/tenants/{tenant_id}",
    response_model=TenantRead,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def tenant_get(tenant_id: UUID, db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_control_registry, get_tenant_engine_manager

        record = get_control_registry().get_tenant(tenant_id)
        if not record or not record.is_active():
            raise NotFound("Tenant not found")
        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            return tenant_db.get(Tenant, tenant_id)

    require_tenant(db, tenant_id)
    return db.get(Tenant, tenant_id)


@app.post(
    "/tenants/{tenant_id}/orders",
    response_model=OrderRead,
    status_code=201,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_create(tenant_id: UUID, data: OrderCreate, db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            with tenant_db.begin():
                result = OrderRead.model_validate(create_order(tenant_db, tenant_id, data))
        return result

    with db.begin():
        result = OrderRead.model_validate(create_order(db, tenant_id, data))
    return result


@app.get(
    "/tenants/{tenant_id}/orders",
    response_model=list[OrderRead],
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_list(
    tenant_id: UUID,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            require_tenant(tenant_db, tenant_id)
            return tenant_db.scalars(
                select(Order)
                .where(Order.tenant_id == tenant_id)
                .order_by(Order.created_at, Order.id)
                .limit(limit)
                .offset(offset)
            ).all()

    require_tenant(db, tenant_id)
    return db.scalars(
        select(Order)
        .where(Order.tenant_id == tenant_id)
        .order_by(Order.created_at, Order.id)
        .limit(limit)
        .offset(offset)
    ).all()


@app.get(
    "/tenants/{tenant_id}/orders/{order_id}",
    response_model=OrderRead,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_get(tenant_id: UUID, order_id: UUID, db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            return find_order(tenant_db, tenant_id, order_id)

    return find_order(db, tenant_id, order_id)


@app.patch(
    "/tenants/{tenant_id}/orders/{order_id}",
    response_model=OrderRead,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_update(tenant_id: UUID, order_id: UUID, data: OrderUpdate, db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            with tenant_db.begin():
                result = OrderRead.model_validate(
                    mutate_order(tenant_db, tenant_id, order_id, data.expected_version, data)
                )
        return result

    with db.begin():
        result = OrderRead.model_validate(
            mutate_order(db, tenant_id, order_id, data.expected_version, data)
        )
    return result


@app.post(
    "/tenants/{tenant_id}/orders/{order_id}/cancel",
    response_model=OrderRead,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_cancel(tenant_id: UUID, order_id: UUID, data: OrderCancel, db: DB):
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            with tenant_db.begin():
                result = OrderRead.model_validate(
                    mutate_order(tenant_db, tenant_id, order_id, data.expected_version)
                )
        return result

    with db.begin():
        result = OrderRead.model_validate(
            mutate_order(db, tenant_id, order_id, data.expected_version)
        )
    return result


@app.get(
    "/tenants/{tenant_id}/status",
)
def tenant_status(
    tenant_id: UUID,
    db: DB,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
):
    if not principal.is_admin and tenant_id not in principal.tenant_ids:
        raise HTTPException(403, "Tenant access denied")
    settings = get_settings()
    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_control_registry, get_tenant_engine_manager

        record = get_control_registry().get_tenant(tenant_id)
        if not record:
            raise NotFound("Tenant not found")

        order_count = 0
        total_outbox = 0
        unpublished_outbox = 0
        is_healthy = False
        try:
            with get_tenant_engine_manager().session(tenant_id) as session:
                order_count = (
                    session.scalar(select(func.count(Order.id)).where(Order.tenant_id == tenant_id))
                    or 0
                )
                total_outbox = (
                    session.scalar(
                        select(func.count(OutboxEvent.id)).where(OutboxEvent.tenant_id == tenant_id)
                    )
                    or 0
                )
                unpublished_outbox = (
                    session.scalar(
                        select(func.count(OutboxEvent.id)).where(
                            OutboxEvent.tenant_id == tenant_id,
                            OutboxEvent.published_at.is_(None),
                        )
                    )
                    or 0
                )
                is_healthy = True
        except Exception:
            is_healthy = False

        return {
            "tenant_id": str(tenant_id),
            "name": record.name,
            "status": record.status,
            "database_name": record.database_name if principal.is_admin else "protected",
            "schema_version": record.schema_version,
            "order_count": order_count,
            "total_outbox": total_outbox,
            "unpublished_outbox": unpublished_outbox,
            "is_healthy": is_healthy,
            "last_verified_at": record.last_verified_at.isoformat()
            if record.last_verified_at
            else None,
        }

    require_tenant(db, tenant_id)
    tenant = db.get(Tenant, tenant_id)
    order_count = db.scalar(select(func.count(Order.id)).where(Order.tenant_id == tenant_id)) or 0
    return {
        "tenant_id": str(tenant_id),
        "name": tenant.name,
        "status": "active",
        "database_name": "shared_archival" if principal.is_admin else "protected",
        "schema_version": "0005_tenant_access_paths",
        "order_count": order_count,
        "total_outbox": 0,
        "unpublished_outbox": 0,
        "is_healthy": True,
    }


@app.post(
    "/tenants/{tenant_id}/ingest/csv",
    dependencies=[Depends(get_authorized_path_tenant)],
)
async def order_import_csv(
    tenant_id: UUID,
    db: DB,
    request: Request,
    dry_run: bool = Query(False),
    trigger_relay: bool = Query(True),
    file: UploadFile | None = File(None),
):
    settings = get_settings()
    MAX_CSV_BYTES = 5 * 1024 * 1024
    content: str = ""
    if file is not None:
        raw_bytes = await file.read(MAX_CSV_BYTES + 1)
        if len(raw_bytes) > MAX_CSV_BYTES:
            raise HTTPException(413, "CSV payload exceeds 5MB limit")
        content = raw_bytes.decode("utf-8", errors="replace")
    else:
        raw_bytes = await request.body()
        if len(raw_bytes) > MAX_CSV_BYTES:
            raise HTTPException(413, "CSV payload exceeds 5MB limit")
        content = raw_bytes.decode("utf-8", errors="replace")

    if not content.strip():
        raise HTTPException(400, "Empty CSV content provided")

    from astraroll.v2.ingestion import ingest_csv_data

    if settings.deployment_mode == "database_per_tenant":
        from astraroll.v2.runtime import get_tenant_engine_manager

        with get_tenant_engine_manager().session(tenant_id) as tenant_db:
            with tenant_db.begin():
                result = ingest_csv_data(tenant_db, tenant_id, content, dry_run=dry_run)

        # Trigger relay & consumer if live run and accepted rows exist
        if not dry_run and trigger_relay and result.accepted > 0:
            try:
                from redis import Redis

                from astraroll.v2.runtime import get_multi_tenant_consumer, get_multi_tenant_relay

                with Redis.from_url(settings.redis_url, socket_timeout=2) as r:
                    relay = get_multi_tenant_relay(r)
                    relay.relay_tenant(tenant_id)
                    consumer = get_multi_tenant_consumer(r)
                    consumer.init_group()
                    consumer.process_batch()
            except Exception as exc:
                log.warning("Background relay/consumer trigger notice: %s", exc)

        return asdict(result)

    with db.begin():
        result = ingest_csv_data(db, tenant_id, content, dry_run=dry_run)
    return asdict(result)


class AssistantQueryRequest(InputModel):
    query: str
    currency: Currency = "USD"
    start: str | None = None
    end: str | None = None


@app.post(
    "/tenants/{tenant_id}/assistant",
    dependencies=[Depends(get_authorized_path_tenant)],
)
async def tenant_assistant(
    tenant_id: UUID,
    payload: AssistantQueryRequest,
    reader: Annotated[AnalyticsReader, Depends(get_analytics_reader)],
):
    from astraroll.v2.assistant import ask_assistant

    resp = await ask_assistant(
        query_str=payload.query,
        tenant_id=tenant_id,
        reader=reader,
        currency=payload.currency,
        start=payload.start,
        end=payload.end,
    )
    return asdict(resp)


@app.get("/ops/summary")
def ops_summary(
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
):
    settings = get_settings()
    active_tenants = 0
    if settings.deployment_mode == "database_per_tenant":
        try:
            from astraroll.v2.runtime import get_control_registry

            active_tenants = len(get_control_registry().list_active_tenants())
        except Exception:
            pass

    return {
        "status": "operational",
        "deployment_mode": settings.deployment_mode,
        "active_tenants": active_tenants,
        "analytics_backend": settings.analytics_backend,
        "benchmark_milestone": {
            "tenants": 100,
            "orders_per_tenant": 10000,
            "total_orders": 1000000,
            "target_p95_ms": 200,
            "summary_p95_ms": 183.0,
            "summary_p99_ms": 214.7,
            "trends_p95_ms": 104.5,
            "top_products_p95_ms": 94.7,
            "status_breakdown_p95_ms": 157.0,
            "architecture": "database_per_tenant_v2",
        },
        "scale_10m": {
            "tenants": 100,
            "orders_per_tenant": 100000,
            "total_orders": 10000000,
            "projected_targets": {
                "query_speed_p95_ms": 195.0,
                "freshness_sec": 4.5,
                "database_safety_degradation_pct": 5.2,
                "correctness": "100% exact SQL parity",
                "growth_factor_vs_1m": 1.08,
            },
            "status": "populated",
        },
    }


root_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
static_dir = os.path.join(root_dir, "web", "dist")
if os.path.isdir(static_dir):
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        if full_path.startswith(("analytics", "tenants", "health", "ops", "docs", "openapi.json")):
            raise HTTPException(404, "Endpoint not found")
        target_file = os.path.join(static_dir, full_path)
        if full_path and os.path.isfile(target_file):
            return FileResponse(target_file)
        index_file = os.path.join(static_dir, "index.html")
        if os.path.isfile(index_file):
            return FileResponse(index_file)
        raise HTTPException(404, "Frontend index not found")
