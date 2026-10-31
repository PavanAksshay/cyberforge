"""Controlled FIFO vs bounded round-robin experiment, using real Redis/PG commits."""

import argparse
import json
import statistics
import time
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from redis import Redis
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from astraroll.aggregation.bootstrap import validate_rollups
from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.models import OrderProjection
from astraroll.aggregation.replay import replay_outbox_events
from astraroll.config import get_settings
from astraroll.db import get_engine
from astraroll.locking import tenant_lock
from astraroll.models import OutboxEvent
from astraroll.workload import seed_tenant


def experiment(factory, redis, hot_orders=120, cold_orders=4, repeats=3):
    runs = []
    for repeat in range(repeats):
        # Alternate order to reduce cache/order bias; distinct source/projections each run.
        for fair in (False, True) if repeat % 2 == 0 else (True, False):
            namespace = uuid4()
            hot = seed_tenant(factory, namespace, 0, hot_orders)
            cold = seed_tenant(factory, namespace, 1, cold_orders)
            stream, group = f"bench:fair:{namespace}", f"bench:fair-group:{namespace}"
            replay_outbox_events(factory, redis, stream, tenant_id=hot)
            replay_outbox_events(factory, redis, stream, tenant_id=cold)
            worker = AggregationConsumer(
                factory,
                redis,
                stream_name=stream,
                group_name=group,
                batch_size=1000,
                block_ms=1,
                fair_dispatch=fair,
            )
            worker.init_group()
            original = worker.process_message
            first_cold = []
            start = time.perf_counter()

            def observe(stream_id, fields):
                result = original(stream_id, fields)
                tenant = json.loads(fields[b"event"])["tenant_id"]
                if tenant == str(cold) and not first_cold:
                    first_cold.append((time.perf_counter() - start) * 1000)
                return result

            worker.process_message = observe
            while worker.process_batch():
                pass
            total = (time.perf_counter() - start) * 1000
            assert redis.xpending(stream, group)["pending"] == 0
            with factory() as session:
                for tenant, count in ((hot, hot_orders), (cold, cold_orders)):
                    projections = session.scalars(
                        select(OrderProjection).where(OrderProjection.tenant_id == tenant)
                    ).all()
                    assert len(projections) == count
                    assert (
                        session.scalar(
                            select(text("count(*)"))
                            .select_from(OutboxEvent)
                            .where(OutboxEvent.tenant_id == tenant)
                        )
                        > 0
                    )
            with factory.begin() as session:
                for tenant in (hot, cold):
                    tenant_lock(session, tenant)
                    validate_rollups(session, tenant)
            runs.append(
                {
                    "repeat": repeat,
                    "fair": fair,
                    "first_cold_ack_ms": first_cold[0],
                    "total_ms": total,
                    "hot_orders": hot_orders,
                    "cold_orders": cold_orders,
                }
            )
    medians = {
        str(fair): statistics.median(r["first_cold_ack_ms"] for r in runs if r["fair"] == fair)
        for fair in (False, True)
    }
    return {
        "runs": runs,
        "median_first_cold_ack_ms": medians,
        "improvement_factor": medians["False"] / medians["True"],
        "scope": "Within one bounded read reply; no global backlog/starvation guarantee",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/fairness.json")
    args = parser.parse_args()
    base = get_engine()
    schema = f"fair_{uuid4().hex}"
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        base.url, connect_args={"options": f"-csearch_path={schema} -c timezone=UTC"}
    )
    with engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    with Redis.from_url(get_settings().redis_url, socket_timeout=30) as redis:
        result = experiment(sessionmaker(engine, expire_on_commit=False), redis)
    result["schema"] = schema
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
