"""A-owned bridge to B's reviewed PostgreSQL reader; no copied models or migrations."""

import hashlib
import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from astraroll.analytics.contracts import (
    METRIC_SEMANTICS,
    AnalyticsQuery,
    Freshness,
    ProductMetrics,
    StatusMetrics,
    SummaryMetrics,
    TopProductsQuery,
    TrendMetrics,
    TrendsQuery,
)
from astraroll.analytics.coverage import (
    CoverageProof,
    CoverageProvider,
    Dimension,
    require_coverage,
    unavailable_coverage,
)
from astraroll.analytics.reader import (
    AnalyticsUnavailable,
    ReaderCapabilities,
    ReadResult,
    UnavailableAnalyticsReader,
    UnsupportedAnalyticsWindow,
)
from astraroll.config import get_settings
from astraroll.db import get_engine

log = logging.getLogger(__name__)
# Observed contribution semantics at origin/dev-b 36a0aeb; never relabel this data.
B_METRIC_SEMANTICS = "created_at_shipped_sales_v2"
LEGACY_READER_SHA256 = "189641f9147f32a23819ebadaad8ba2f427a159df10fdbff3513d1110a253bc0"


class PostgresAnalyticsAdapter:
    def __init__(
        self, engine: Callable[[], Engine], coverage: CoverageProvider = unavailable_coverage
    ):
        self.engine = engine
        self.coverage = coverage

    def _reader_class(self):
        try:
            module = importlib.import_module("astraroll.aggregation.reader")
            declared = getattr(module, "METRIC_SEMANTICS", None)
            if declared is None:
                # Only the reviewed, immutable legacy reader may omit its metric ID.
                source = Path(module.__file__).read_bytes().replace(b"\r\n", b"\n")
                if hashlib.sha256(source).hexdigest() != LEGACY_READER_SHA256:
                    raise AnalyticsUnavailable("Unversioned aggregate reader has not been reviewed")
            elif declared != METRIC_SEMANTICS:
                raise AnalyticsUnavailable("B's advertised metric contract is incompatible")
            return module.PostgresAnalyticsReader
        except (ImportError, AttributeError, OSError) as exc:
            raise AnalyticsUnavailable(
                "Developer B's aggregate reader package is unavailable"
            ) from exc

    def capabilities(self):
        if METRIC_SEMANTICS != B_METRIC_SEMANTICS:
            raise AnalyticsUnavailable(
                "Producer/query and aggregation sales semantics are not aligned"
            )
        self._reader_class()
        return ReaderCapabilities(base_granularity="hour", metric_semantics=METRIC_SEMANTICS)

    def _granularity(self, query: AnalyticsQuery, proof: CoverageProof):
        if isinstance(query, TrendsQuery):
            if query.interval not in proof.intervals:
                raise UnsupportedAnalyticsWindow("Requested trend grain is not covered")
            return query.interval
        if "hour" in proof.intervals:
            return "hour"
        if "day" in proof.intervals and not (query.start.hour or query.end.hour):
            return "day"
        raise UnsupportedAnalyticsWindow("Aggregate coverage cannot answer this exact window")

    def _read[T](
        self, query: AnalyticsQuery, dimension: Dimension, operation: Callable[[Any, str], T]
    ) -> ReadResult[T]:
        self.capabilities()
        reader_class = self._reader_class()
        try:
            with (
                self.engine().connect().execution_options(isolation_level="REPEATABLE READ") as conn
            ):
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
            # Public diagnostics never include the SQL text, URL or driver error.
            log.warning("Aggregate database read failed (%s)", type(exc).__name__)
            raise AnalyticsUnavailable("Aggregate database is unavailable") from exc
        except ValidationError as exc:
            log.warning("Aggregate result did not satisfy the API contract")
            raise AnalyticsUnavailable("Aggregate response is incompatible") from exc

    def _arguments(self, query):
        return {
            "tenant_id": query.tenant_id,
            "currency": query.currency,
            "from_ts": query.start,
            "to_ts": query.end,
        }

    def summary(self, query: AnalyticsQuery) -> ReadResult[SummaryMetrics]:
        def operation(backend, grain):
            data = backend.get_summary(**self._arguments(query), granularity=grain)
            if data.tenant_id != query.tenant_id or data.currency != query.currency:
                raise AnalyticsUnavailable("Aggregate result has wrong tenant or currency")
            return SummaryMetrics(
                total_order_count=data.total_orders, sales_amount=data.total_sales
            )

        return self._read(query, "summary", operation)

    def trends(self, query: TrendsQuery) -> ReadResult[list[TrendMetrics]]:
        def operation(backend, grain):
            return [
                TrendMetrics(
                    bucket_start=row.bucket_start,
                    total_order_count=row.order_count,
                    sales_amount=row.sales_amount,
                )
                for row in backend.get_trends(**self._arguments(query), interval=grain)
            ]

        return self._read(query, "summary", operation)

    def top_products(self, query: TopProductsQuery) -> ReadResult[list[ProductMetrics]]:
        def operation(backend, grain):
            return [
                ProductMetrics(
                    product_id=row.product_id,
                    total_order_count=row.order_count,
                    sales_amount=row.sales_amount,
                )
                for row in backend.get_top_products(
                    **self._arguments(query), granularity=grain, limit=query.limit
                )
            ]

        return self._read(query, "product", operation)

    def status_breakdown(self, query: AnalyticsQuery) -> ReadResult[list[StatusMetrics]]:
        def operation(backend, grain):
            return [
                StatusMetrics(
                    status=row.status,
                    total_order_count=row.order_count,
                    sales_amount=row.sales_amount,
                )
                for row in backend.get_status_breakdown(**self._arguments(query), granularity=grain)
            ]

        return self._read(query, "status", operation)


def configured_reader():
    provider_path = get_settings().analytics_coverage_provider
    if not provider_path:
        return PostgresAnalyticsAdapter(get_engine)
    try:
        module_name, attribute = provider_path.split(":", 1)
        provider = getattr(importlib.import_module(module_name), attribute)
        if not callable(provider):
            raise TypeError("Coverage provider is not callable")
        return PostgresAnalyticsAdapter(get_engine, provider)
    except (ImportError, AttributeError, ValueError, TypeError):
        log.warning("Configured aggregate coverage provider could not be loaded")
        return UnavailableAnalyticsReader()
