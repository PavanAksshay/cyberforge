"""Dynamic Multi-Tenant Database Router and Engine Manager for AstraRoll V2."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from astraroll.v2.registry import ControlRegistry, TenantStatus

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class TenantRouterError(Exception):
    """Base exception for tenant routing and connection management."""


class TenantNotFoundError(TenantRouterError):
    """Raised when a tenant is not registered in the control plane."""


class TenantInactiveError(TenantRouterError):
    """Raised when a tenant is registered but not in the ACTIVE state."""


class TenantDatabaseUnavailable(TenantRouterError):
    """Raised when a tenant's database cannot be reached or fails health check."""


class TenantDatabaseResolver:
    """Resolves tenant UUIDs to target database connection strings via the control plane."""

    def __init__(
        self,
        registry: ControlRegistry,
        base_database_url: str,
        cache_ttl_seconds: float = 30.0,
    ):
        self.registry = registry
        self.base_url = make_url(base_database_url)
        self.cache_ttl = cache_ttl_seconds
        self._cache: dict[UUID, tuple[str, str, float]] = {}  # tenant_id -> (db_name, status, ts)
        self._lock = threading.Lock()

    def invalidate(self, tenant_id: UUID | None = None) -> None:
        """Invalidate routing cache for a specific tenant or entirely."""
        with self._lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                self._cache.pop(tenant_id, None)

    def resolve(
        self,
        tenant_id: UUID,
        allow_inactive: bool = False,
    ) -> tuple[str, str]:
        """Resolve tenant_id to (database_name, database_url).

        Fails closed if tenant is missing or not active (unless allow_inactive=True).
        """
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(tenant_id)
            if cached and (now - cached[2] < self.cache_ttl):
                db_name, status, _ = cached
                if not allow_inactive and status != TenantStatus.ACTIVE.value:
                    raise TenantInactiveError(
                        f"Tenant {tenant_id} is in '{status}' state (not active)"
                    )
                target_url = self.base_url.set(database=db_name).render_as_string(
                    hide_password=False
                )
                return db_name, target_url

        # Cache miss or expired: query control plane
        record = self.registry.get_tenant(tenant_id)
        if not record:
            raise TenantNotFoundError(f"Tenant {tenant_id} is not registered in control plane")

        with self._lock:
            self._cache[tenant_id] = (record.database_name, record.status, now)

        if not allow_inactive and record.status != TenantStatus.ACTIVE.value:
            raise TenantInactiveError(
                f"Tenant {tenant_id} is in '{record.status}' state (not active)"
            )

        target_url = self.base_url.set(database=record.database_name).render_as_string(
            hide_password=False
        )
        return record.database_name, target_url


class TenantEngineManager:
    """Thread-safe connection pool manager with LRU pool eviction across >=100 tenant databases.

    Guarantees that active connections never exceed PostgreSQL's max_connections ceiling,
    reclaiming idle database engines when capacity is reached.
    """

    def __init__(
        self,
        resolver: TenantDatabaseResolver,
        max_active_pools: int = 25,
        pool_size: int = 2,
        max_overflow: int = 2,
        pool_timeout: float = 10.0,
    ):
        self.resolver = resolver
        self.max_active_pools = max_active_pools
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.pool_timeout = pool_timeout
        self._engines: OrderedDict[UUID, Engine] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def active_pool_count(self) -> int:
        with self._lock:
            return len(self._engines)

    def get_engine(
        self,
        tenant_id: UUID,
        allow_inactive: bool = False,
        max_retries: int = 3,
        backoff_base: float = 0.05,
    ) -> Engine:
        """Retrieve an Engine for tenant, applying LRU eviction if budget is exceeded.

        Includes connection retry with exponential backoff for resilience under connection spikes.
        """
        tenant_id = UUID(str(tenant_id))
        # Status checks also apply to cached pools; do not serve a failed cutover.
        self.resolver.invalidate(tenant_id)
        try:
            self.resolver.resolve(tenant_id, allow_inactive=allow_inactive)
        except SQLAlchemyError as exc:
            raise TenantDatabaseUnavailable("Tenant registry is unavailable") from exc
        with self._lock:
            if tenant_id in self._engines:
                self._engines.move_to_end(tenant_id)
                return self._engines[tenant_id]

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                # Resolve tenant database URL (outside lock to avoid contention)
                try:
                    _, target_url = self.resolver.resolve(tenant_id, allow_inactive=allow_inactive)
                except TypeError:
                    _, target_url = self.resolver.resolve(tenant_id)

                # Evict least-recently-used engine if pool budget exceeded
                to_dispose: Engine | None = None
                with self._lock:
                    # Recheck after resolving URL
                    if tenant_id in self._engines:
                        self._engines.move_to_end(tenant_id)
                        return self._engines[tenant_id]

                    if len(self._engines) >= self.max_active_pools:
                        evicted_id, to_dispose = self._engines.popitem(last=False)
                        log.debug("LRU evicted engine pool for tenant %s", evicted_id)

                    engine = create_engine(
                        target_url,
                        pool_size=self.pool_size,
                        max_overflow=self.max_overflow,
                        pool_timeout=self.pool_timeout,
                        pool_recycle=1800,
                        pool_pre_ping=True,
                        connect_args={"options": "-c timezone=UTC"},
                    )
                    self._engines[tenant_id] = engine

                if to_dispose is not None:
                    try:
                        to_dispose.dispose()
                    except Exception as exc:
                        log.warning("Error disposing evicted engine: %s", exc)

                return engine
            except (OperationalError, SQLAlchemyError) as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    sleep_time = backoff_base * (2**attempt)
                    log.warning(
                        "Transient error acquiring engine for tenant %s (attempt %d/%d), "
                        "retrying in %.2fs: %s",
                        tenant_id,
                        attempt + 1,
                        max_retries,
                        sleep_time,
                        exc,
                    )
                    time.sleep(sleep_time)
                else:
                    break

        if last_exc:
            raise last_exc
        raise TenantDatabaseUnavailable(f"Unable to obtain database engine for tenant {tenant_id}")

    def get_session_factory(
        self, tenant_id: UUID, allow_inactive: bool = False
    ) -> sessionmaker[Session]:
        """Produce a sessionmaker bound to the tenant's dedicated database engine."""
        engine = self.get_engine(tenant_id, allow_inactive=allow_inactive)
        return sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def session(self, tenant_id: UUID, allow_inactive: bool = False) -> Iterator[Session]:
        """Scoped session context manager with automatic rollback on error."""
        engine = self.get_engine(tenant_id, allow_inactive=allow_inactive)
        try:
            with Session(engine) as sess:
                yield sess
        except (OperationalError, SQLAlchemyError) as exc:
            log.warning("Database error for tenant %s: %s", tenant_id, exc)
            raise TenantDatabaseUnavailable(
                f"Database error for tenant {tenant_id}: {exc}"
            ) from exc

    def dispose_tenant(self, tenant_id: UUID) -> None:
        """Explicitly dispose and remove a tenant's engine from the cache."""
        engine = None
        with self._lock:
            engine = self._engines.pop(tenant_id, None)
        if engine:
            try:
                engine.dispose()
            except Exception as exc:
                log.warning("Error disposing engine for tenant %s: %s", tenant_id, exc)
        self.resolver.invalidate(tenant_id)

    def close_all(self) -> None:
        """Dispose all open engine connection pools."""
        with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for eng in engines:
            try:
                eng.dispose()
            except Exception as exc:
                log.warning("Error disposing engine during close_all: %s", exc)
        self.resolver.invalidate()
