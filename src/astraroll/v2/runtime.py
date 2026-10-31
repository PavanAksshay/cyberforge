"""Runtime Singletons and Service Resolution for AstraRoll V2."""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from astraroll.analytics.coverage import unavailable_coverage
from astraroll.config import get_settings
from astraroll.v2.adapter import MultiTenantPostgresAnalyticsAdapter
from astraroll.v2.consumer import MultiTenantAggregationConsumer
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry
from astraroll.v2.relay import MultiTenantRelay
from astraroll.v2.router import TenantDatabaseResolver, TenantEngineManager

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


@lru_cache
def get_control_engine() -> Engine:
    """Acquire the control plane database engine."""
    settings = get_settings()
    url_str = settings.control_database_url or settings.database_url
    url = make_url(url_str)
    options = url.query.get("options", "")
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": f"{options} -c timezone=UTC"},
    )


@lru_cache
def get_control_registry() -> ControlRegistry:
    """Acquire the central ControlRegistry instance."""
    engine = get_control_engine()
    registry = ControlRegistry(engine)
    registry.init_schema()
    return registry


@lru_cache
def get_tenant_resolver() -> TenantDatabaseResolver:
    """Acquire the TenantDatabaseResolver."""
    settings = get_settings()
    base_url = settings.control_database_url or settings.database_url
    return TenantDatabaseResolver(get_control_registry(), base_url)


@lru_cache
def get_tenant_engine_manager() -> TenantEngineManager:
    """Acquire the TenantEngineManager managing dynamic connection pools."""
    settings = get_settings()
    return TenantEngineManager(
        resolver=get_tenant_resolver(),
        max_active_pools=settings.max_active_tenant_pools,
        pool_size=settings.tenant_pool_size,
        max_overflow=settings.tenant_pool_max_overflow,
    )


@lru_cache
def get_tenant_provisioner() -> TenantProvisioner:
    """Acquire the TenantProvisioner for tenant lifecycle operations."""
    settings = get_settings()
    admin_url = settings.control_database_url or settings.database_url
    return TenantProvisioner(
        registry=get_control_registry(),
        admin_database_url=admin_url,
        tenant_database_prefix=settings.tenant_database_prefix,
    )


def get_v2_analytics_adapter() -> MultiTenantPostgresAnalyticsAdapter:
    """Produce the tenant-routed PostgresAnalyticsAdapter."""
    settings = get_settings()
    provider_path = settings.analytics_coverage_provider
    provider = unavailable_coverage
    if provider_path:
        try:
            module_name, attribute = provider_path.split(":", 1)
            loaded = getattr(importlib.import_module(module_name), attribute)
            if callable(loaded):
                provider = loaded
        except Exception as exc:
            log.warning("Could not load configured coverage provider: %s", exc)

    return MultiTenantPostgresAnalyticsAdapter(
        engine_manager=get_tenant_engine_manager(),
        coverage=provider,
    )


def get_multi_tenant_relay(redis_client: Redis | None = None) -> MultiTenantRelay:
    """Construct a MultiTenantRelay worker."""
    settings = get_settings()
    client = redis_client or Redis.from_url(settings.redis_url)
    return MultiTenantRelay(
        registry=get_control_registry(),
        engine_manager=get_tenant_engine_manager(),
        redis_client=client,
        stream_name=settings.order_stream,
        batch_size=settings.relay_batch_size,
    )


def get_tenant_migrator():
    """Acquire the TenantMigrator utility."""
    from astraroll.db import get_session_factory
    from astraroll.v2.migration import TenantMigrator

    return TenantMigrator(
        shared_session_factory=get_session_factory(),
        control_registry=get_control_registry(),
        provisioner=get_tenant_provisioner(),
        engine_manager=get_tenant_engine_manager(),
    )


def get_multi_tenant_consumer(
    redis_client: Redis | None = None,
    consumer_name: str | None = None,
    fair_dispatch: bool = True,
) -> MultiTenantAggregationConsumer:
    """Construct a MultiTenantAggregationConsumer worker."""
    settings = get_settings()
    client = redis_client or Redis.from_url(settings.redis_url)
    return MultiTenantAggregationConsumer(
        engine_manager=get_tenant_engine_manager(),
        redis_client=client,
        stream_name=settings.order_stream,
        consumer_name=consumer_name,
        fair_dispatch=fair_dispatch,
    )
