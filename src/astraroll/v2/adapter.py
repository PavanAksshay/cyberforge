"""Tenant-Routed PostgreSQL Analytics Adapter for AstraRoll V2.

Routes analytics queries to each tenant's physically isolated database under
REPEATABLE READ isolation and checks durable coverage proofs within that database.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from astraroll.analytics.contracts import (
    AnalyticsQuery,
    Freshness,
)
from astraroll.analytics.coverage import (
    CoverageProof,
    CoverageProvider,
    Dimension,
    require_coverage,
    unavailable_coverage,
)
from astraroll.analytics.postgres import PostgresAnalyticsAdapter
from astraroll.analytics.reader import AnalyticsUnavailable, ReadResult
from astraroll.v2.router import TenantEngineManager, TenantRouterError

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class MultiTenantPostgresAnalyticsAdapter(PostgresAnalyticsAdapter):
    """Analytics adapter that dynamically dispatches queries to the requester's tenant database."""

    def __init__(
        self,
        engine_manager: TenantEngineManager,
        coverage: CoverageProvider = unavailable_coverage,
    ):
        super().__init__(lambda: None, coverage=coverage)  # type: ignore[arg-type]
        self.engine_manager = engine_manager

    def _read[T](
        self, query: AnalyticsQuery, dimension: Dimension, operation: Callable[[Any, str], T]
    ) -> ReadResult[T]:
        self.capabilities()
        reader_class = self._reader_class()

        try:
            engine = self.engine_manager.get_engine(query.tenant_id)
        except TenantRouterError as exc:
            log.warning("Tenant routing failed for query %s: %s", query.tenant_id, exc)
            raise AnalyticsUnavailable("Tenant aggregate database is not available") from exc

        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                with conn.begin():
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    with Session(bind=conn) as session:
                        proof = CoverageProof.model_validate(self.coverage(session, query))
                        require_coverage(proof, query, dimension)
                        grain = self._granularity(query, proof)
                        backend = reader_class(session)
                        state = backend.get_freshness(query.tenant_id)
                        if not state.is_initialized:
                            raise AnalyticsUnavailable("Tenant aggregates are uninitialized")
                        if state.tenant_id != query.tenant_id:
                            raise AnalyticsUnavailable(
                                "Aggregate processing state has wrong tenant"
                            )
                        value = operation(backend, grain)
                        return ReadResult(
                            value,
                            Freshness(
                                tenant_revision=state.revision,
                                last_processed_at=state.last_applied_at,
                            ),
                        )
        except SQLAlchemyError as exc:
            log.warning(
                "Tenant %s aggregate database read failed (%s)",
                query.tenant_id,
                type(exc).__name__,
            )
            raise AnalyticsUnavailable("Aggregate database is unavailable") from exc
        except ValidationError as exc:
            log.warning("Aggregate result did not satisfy API contract: %s", exc)
            raise AnalyticsUnavailable("Aggregate response is incompatible") from exc
