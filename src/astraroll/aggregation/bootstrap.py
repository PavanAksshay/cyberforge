"""Resumable rebuild. Archive, exact oracle validation and activation are tenant-fenced.

Source orders/outbox are never deleted. UUID enumeration is not a watermark:
fenced catchup reconciles inserts/updates behind the cursor before activation.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from astraroll.aggregation.models import TenantProcessingState
from astraroll.aggregation.release_models import AggregateCoverage, BootstrapJob
from astraroll.analytics.contracts import METRIC_SEMANTICS, AnalyticsQuery
from astraroll.locking import tenant_lock
from astraroll.models import Tenant

TABLES = tuple(
    f"{g}_{d}_aggregates" for g in ("hourly", "daily") for d in ("summary", "status", "product")
)


def canonical_sql(alias="o"):
    return f"""jsonb_build_object(
        'product_id',{alias}.product_id::text,'amount_cents',({alias}.amount*100)::bigint,
        'currency',{alias}.currency,'status',{alias}.status,
        'created_at',{alias}.created_at,'updated_at',{alias}.updated_at,
        'order_id',{alias}.id::text,'tenant_id',{alias}.tenant_id::text)"""


def start_job(factory, tenant_id: UUID, currencies: list[str], start: datetime, end: datetime):
    for currency in currencies:
        AnalyticsQuery(tenant_id=tenant_id, currency=currency, start=start, end=end)
    if not currencies or start.hour or end.hour:
        raise ValueError("Supply currencies and full UTC day boundaries")
    with factory.begin() as session:
        tenant_lock(session, tenant_id)
        if session.get(Tenant, tenant_id) is None:
            raise ValueError("Tenant does not exist")
        active = session.scalar(
            select(BootstrapJob.id).where(
                BootstrapJob.tenant_id == tenant_id,
                BootstrapJob.status.in_(("building", "catchup")),
            )
        )
        if active:
            raise ValueError(f"Resume existing bootstrap job {active}")
        job = BootstrapJob(
            id=uuid4(),
            tenant_id=tenant_id,
            currencies=sorted(set(currencies)),
            start=start,
            end=end,
            status="building",
            metric_semantics=METRIC_SEMANTICS,
        )
        session.add(job)
        session.flush()
        session.execute(
            update(AggregateCoverage)
            .where(AggregateCoverage.tenant_id == tenant_id)
            .values(status="building")
        )
        for currency in job.currencies:
            stmt = insert(AggregateCoverage).values(
                tenant_id=tenant_id,
                currency=currency,
                job_id=job.id,
                status="building",
                metric_semantics=METRIC_SEMANTICS,
                start=start,
                end=end,
                source_order_count=0,
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "currency"],
                    set_={
                        "job_id": job.id,
                        "status": "building",
                        "metric_semantics": METRIC_SEMANTICS,
                        "start": start,
                        "end": end,
                        "verified_at": None,
                    },
                )
            )
        return job.id


def stage_batch(factory, job_id: UUID, batch_size=1000):
    if not 1 <= batch_size <= 100000:
        raise ValueError("Batch size must be 1..100000")
    with factory.begin() as session:
        job = session.get(BootstrapJob, job_id)
        if job is None:
            raise ValueError("Unknown bootstrap job")
        tenant_lock(session, job.tenant_id)
        session.refresh(job)
        if job.status != "building":
            return 0
        rows = (
            session.execute(
                text(f"""
            INSERT INTO bootstrap_orders(job_id,order_id,version,snapshot)
            SELECT :job,o.id,o.version,{canonical_sql()} FROM orders o
            WHERE o.tenant_id=:tenant AND (CAST(:cursor AS uuid) IS NULL
                OR o.id>CAST(:cursor AS uuid))
            ORDER BY o.id LIMIT :batch
            ON CONFLICT(job_id,order_id) DO UPDATE SET
                version=EXCLUDED.version,snapshot=EXCLUDED.snapshot
            RETURNING order_id
        """),
                {"job": job.id, "tenant": job.tenant_id, "cursor": job.cursor, "batch": batch_size},
            )
            .scalars()
            .all()
        )
        if rows:
            job.cursor = max(rows)
            job.copied += len(rows)
        else:
            job.status = "catchup"
        job.updated_at = datetime.now(UTC)
        return len(rows)


def oracle_sql(table, source="orders"):
    grain, dimension, _ = table.split("_")
    grain = "hour" if grain == "hourly" else "day"
    dim = {"summary": "", "status": ",status", "product": ",product_id"}[dimension]
    return f"""SELECT currency,date_trunc('{grain}',created_at) AS bucket_start{dim},
        count(*)::bigint AS order_count,
        coalesce(sum(CASE WHEN status='shipped' THEN amount ELSE 0 END),0)::numeric AS sales_amount
        FROM {source} WHERE tenant_id=:tenant GROUP BY currency,bucket_start{dim}"""


def validate_rollups(session, tenant_id):
    """Exact comparison while caller holds tenant source/consumer fence."""
    for table in TABLES:
        dimension = table.split("_")[1]
        dim = {"summary": "", "status": ",status", "product": ",product_id"}[dimension]
        actual = (
            f"SELECT currency,bucket_start{dim},order_count,sales_amount FROM {table} "
            "WHERE tenant_id=:tenant AND (order_count<>0 OR sales_amount<>0)"
        )
        expected = oracle_sql(table)
        mismatch = session.scalar(
            text(
                f"SELECT EXISTS(({expected} EXCEPT ALL {actual}) "
                f"UNION ALL ({actual} EXCEPT ALL {expected}))"
            ),
            {"tenant": tenant_id},
        )
        if mismatch:
            raise ValueError(f"Oracle mismatch in {table}; activation rolled back")


def activate_job(factory, job_id: UUID, batch_size=1000, max_catchup=100000):
    if not 1 <= batch_size <= 100000 or max_catchup < 0:
        raise ValueError("Invalid bounded activation settings")
    with factory.begin() as session:
        job = session.get(BootstrapJob, job_id)
        if job is None:
            raise ValueError("Unknown bootstrap job")
        tenant_lock(session, job.tenant_id)
        session.refresh(job)
        if job.status == "ready":
            return
        if job.status != "catchup":
            raise ValueError("Finish source enumeration before activation")
        params = {"job": job.id, "tenant": job.tenant_id, "batch": batch_size}
        caught = 0
        while True:
            changed = session.execute(
                text(f"""
                INSERT INTO bootstrap_orders(job_id,order_id,version,snapshot)
                SELECT :job,o.id,o.version,{canonical_sql()} FROM orders o
                LEFT JOIN bootstrap_orders s ON s.job_id=:job AND s.order_id=o.id
                WHERE o.tenant_id=:tenant AND (s.order_id IS NULL OR s.version<>o.version
                    OR s.snapshot IS DISTINCT FROM {canonical_sql()})
                ORDER BY o.id LIMIT :batch
                ON CONFLICT(job_id,order_id) DO UPDATE SET
                version=EXCLUDED.version,snapshot=EXCLUDED.snapshot
            """),
                params,
            ).rowcount
            caught += changed
            if caught > max_catchup:
                raise ValueError("Catchup bound exceeded; resume with a reviewed larger bound")
            if not changed:
                break
        if session.scalar(
            text("""SELECT EXISTS(SELECT 1 FROM bootstrap_orders s
            LEFT JOIN orders o ON o.id=s.order_id AND o.tenant_id=:tenant
            WHERE s.job_id=:job AND o.id IS NULL)"""),
            params,
        ):
            raise ValueError("Source deletion detected; manual review required")
        # Retain old derived rows for audit. Source and outbox remain untouched.
        for table in (*TABLES, "order_projections"):
            session.execute(
                text(
                    f"INSERT INTO rebuild_archive(job_id,table_name,row_data) "
                    f"SELECT :job,:table,to_jsonb(t) FROM {table} t WHERE tenant_id=:tenant"
                ),
                {**params, "table": table},
            )
            session.execute(text(f"DELETE FROM {table} WHERE tenant_id=:tenant"), params)
        session.execute(
            text("""INSERT INTO order_projections
            (tenant_id,order_id,last_applied_version,canonical_snapshot,last_event_id,updated_at)
            SELECT :tenant,order_id,version,snapshot,NULL,now()
            FROM bootstrap_orders WHERE job_id=:job
        """),
            params,
        )
        source = """(SELECT CAST(:tenant AS uuid) AS tenant_id,
            snapshot->>'currency' AS currency,(snapshot->>'product_id')::uuid AS product_id,
            snapshot->>'status' AS status,(snapshot->>'created_at')::timestamptz AS created_at,
            (snapshot->>'amount_cents')::numeric/100 AS amount
            FROM bootstrap_orders WHERE job_id=:job) AS staged"""
        for table in TABLES:
            dimension = table.split("_")[1]
            dim = {"summary": "", "status": ",status", "product": ",product_id"}[dimension]
            grouped = oracle_sql(table, source)
            session.execute(
                text(
                    f"INSERT INTO {table}(tenant_id,currency,bucket_start{dim},"
                    f"order_count,sales_amount,updated_at) "
                    f"SELECT :tenant,q.*,now() FROM ({grouped}) q"
                ),
                params,
            )
        validate_rollups(session, job.tenant_id)
        now = datetime.now(UTC)
        job.status, job.activated_at, job.updated_at = "ready", now, now
        state = insert(TenantProcessingState).values(
            tenant_id=job.tenant_id, revision=1, last_successful_apply_at=None
        )
        session.execute(
            state.on_conflict_do_update(
                index_elements=["tenant_id"], set_={"revision": TenantProcessingState.revision + 1}
            )
        )
        currencies = set(job.currencies) | set(
            session.scalars(
                text(
                    "SELECT DISTINCT snapshot->>'currency' FROM bootstrap_orders WHERE job_id=:job"
                ),
                params,
            )
        )
        for currency in currencies:
            count = session.scalar(
                text(
                    "SELECT count(*) FROM bootstrap_orders WHERE job_id=:job "
                    "AND snapshot->>'currency'=:currency"
                ),
                {**params, "currency": currency},
            )
            stmt = insert(AggregateCoverage).values(
                tenant_id=job.tenant_id,
                currency=currency,
                job_id=job.id,
                status="ready",
                metric_semantics=METRIC_SEMANTICS,
                start=job.start,
                end=job.end,
                verified_at=now,
                source_order_count=count,
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "currency"],
                    set_={
                        key: getattr(stmt.excluded, key)
                        for key in (
                            "job_id",
                            "status",
                            "metric_semantics",
                            "start",
                            "end",
                            "verified_at",
                            "source_order_count",
                        )
                    },
                )
            )


def bootstrap(factory, tenant_id, currencies, start, end, batch_size=1000, job_id=None):
    job_id = job_id or start_job(factory, tenant_id, currencies, start, end)
    with factory() as session:
        job = session.get(BootstrapJob, job_id)
        if job is None or (job.tenant_id, job.currencies, job.start, job.end) != (
            tenant_id,
            sorted(set(currencies)),
            start,
            end,
        ):
            raise ValueError("Resume arguments do not match the durable job")
    try:
        while stage_batch(factory, job_id, batch_size):
            pass
        activate_job(factory, job_id, batch_size)
    except Exception as exc:
        with factory.begin() as session:
            job = session.get(BootstrapJob, job_id)
            job.last_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            job.updated_at = datetime.now(UTC)
        raise
    return job_id
