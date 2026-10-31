from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from astraroll.analytics.auth import get_authorized_path_tenant, require_admin
from astraroll.analytics.routes import router as analytics_router
from astraroll.db import get_session_factory
from astraroll.models import Order, Tenant
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
    OrderCancel,
    OrderCreate,
    OrderRead,
    OrderUpdate,
    TenantCreate,
    TenantRead,
)

app = FastAPI(title="AstraRoll V1", version="0.1.0")
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


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: DB):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "database unavailable"})
    return {"status": "ok"}


@app.post(
    "/tenants", response_model=TenantRead, status_code=201, dependencies=[Depends(require_admin)]
)
def tenant_create(data: TenantCreate, db: DB):
    with db.begin():
        tenant = Tenant(name=data.name)
        db.add(tenant)
        db.flush()
        result = TenantRead.model_validate(tenant)
    return result


@app.get(
    "/tenants/{tenant_id}",
    response_model=TenantRead,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def tenant_get(tenant_id: UUID, db: DB):
    require_tenant(db, tenant_id)
    return db.get(Tenant, tenant_id)


@app.post(
    "/tenants/{tenant_id}/orders",
    response_model=OrderRead,
    status_code=201,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_create(tenant_id: UUID, data: OrderCreate, db: DB):
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
    return find_order(db, tenant_id, order_id)


@app.patch(
    "/tenants/{tenant_id}/orders/{order_id}",
    response_model=OrderRead,
    dependencies=[Depends(get_authorized_path_tenant)],
)
def order_update(tenant_id: UUID, order_id: UUID, data: OrderUpdate, db: DB):
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
    with db.begin():
        result = OrderRead.model_validate(
            mutate_order(db, tenant_id, order_id, data.expected_version)
        )
    return result
