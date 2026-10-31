"""Real HTTP/PG/Redis demo in retained isolated namespaces; no secret output."""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from alembic.config import Config
from redis import Redis
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from astraroll.aggregation.bootstrap import bootstrap, validate_rollups
from astraroll.aggregation.consumer import AggregationConsumer
from astraroll.aggregation.replay import replay_outbox_events
from astraroll.config import get_settings
from astraroll.db import get_engine
from astraroll.locking import tenant_lock
from astraroll.models import OutboxEvent, Tenant
from astraroll.relay import relay_batch


def main():
    run_id = uuid4().hex
    schema = f"demo_{run_id}"
    output = Path("artifacts") / schema
    output.mkdir(parents=True, exist_ok=False)
    base = get_engine()
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        base.url, connect_args={"options": f"-csearch_path={schema} -c timezone=UTC"}
    )
    with engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    factory = sessionmaker(engine, expire_on_commit=False)
    a, b = uuid4(), uuid4()
    with factory.begin() as session:
        session.add_all([Tenant(id=a, name="Demo A"), Tenant(id=b, name="Demo B")])
    tokens = [secrets.token_urlsafe(32) for _ in range(3)]
    records = [
        {
            "subject": f"demo-{i}",
            "tenant_ids": [] if i == 2 else [str((a, b)[i])],
            "is_admin": i == 2,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }
        for i, token in enumerate(tokens)
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = base.url.update_query_dict({"options": f"-csearch_path={schema} -c timezone=UTC"})
    env = {
        **os.environ,
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "ANALYTICS_BACKEND": "postgres",
        "ANALYTICS_COVERAGE_PROVIDER": "astraroll.aggregation.coverage:read_coverage",
        "ANALYTICS_API_KEYS_JSON": json.dumps(records),
        "ORDER_STREAM": f"demo:orders:{run_id}",
    }
    log = (output / "api.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "astraroll.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    report = {
        "schema": schema,
        "dataset": "9 orders, two tenants, USD/EUR; functional demo, not a scale benchmark",
    }
    try:
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client,
            Redis.from_url(get_settings().redis_url, socket_timeout=30) as redis,
        ):
            for _ in range(100):
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Demo API did not become ready; inspect api.log")
            stream = env["ORDER_STREAM"]
            group = f"demo:group:{run_id}"
            worker = AggregationConsumer(
                factory, redis, stream_name=stream, group_name=group, batch_size=1000, block_ms=1
            )
            worker.init_group()

            def headers(tenant):
                return {"Authorization": f"Bearer {tokens[0 if tenant == a else 1]}"}

            def drain():
                while relay_batch(factory, redis, stream, batch_size=1000):
                    pass
                while worker.process_batch():
                    pass
                assert redis.xpending(stream, group)["pending"] == 0

            def create(tenant, product, amount, currency="USD"):
                response = client.post(
                    f"/tenants/{tenant}/orders",
                    headers=headers(tenant),
                    json={
                        "product_id": str(UUID(int=product)),
                        "amount": amount,
                        "currency": currency,
                    },
                )
                response.raise_for_status()
                drain()
                return response.json()

            def patch(tenant, order, **changes):
                response = client.patch(
                    f"/tenants/{tenant}/orders/{order['id']}",
                    headers=headers(tenant),
                    json={"expected_version": order["version"], **changes},
                )
                response.raise_for_status()
                drain()
                return response.json()

            start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            params = {
                "tenant_id": str(a),
                "currency": "USD",
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            assert (
                client.get("/analytics/summary", params=params, headers=headers(a)).status_code
                == 503
            )
            first = create(a, 9, "10.00")
            first = patch(a, first, amount="25.00", product_id=str(UUID(int=2)))
            patch(a, first, status="shipped")
            for product, amount in ((2, "25.00"), (1, "50.00"), (3, "25.00"), (3, "25.00")):
                patch(a, create(a, product, amount), status="shipped")
            cancelled = create(a, 8, "100.00")
            response = client.post(
                f"/tenants/{a}/orders/{cancelled['id']}/cancel",
                headers=headers(a),
                json={"expected_version": 1},
            )
            response.raise_for_status()
            drain()
            create(a, 7, "5.00")
            patch(a, create(a, 5, "99.00", "EUR"), status="shipped")
            patch(b, create(b, 6, "1000.00"), status="shipped")
            for tenant, currencies in ((a, ["USD", "EUR"]), (b, ["USD"])):
                bootstrap(factory, tenant, currencies, start, end)
                with factory.begin() as session:
                    tenant_lock(session, tenant)
                    validate_rollups(session, tenant)
            report["endpoints"] = {}
            for endpoint in ("summary", "trends", "top-products", "status-breakdown"):
                response = client.get(f"/analytics/{endpoint}", params=params, headers=headers(a))
                response.raise_for_status()
                report["endpoints"][endpoint] = response.json()
            summary = report["endpoints"]["summary"]
            assert summary["total_order_count"] == 7 and summary["sales_amount"] == "150.00"
            denied = client.get(f"/tenants/{b}/orders", headers=headers(a))
            assert denied.status_code == 403
            assert (
                client.post(
                    f"/tenants/{b}/orders",
                    headers=headers(a),
                    json={"product_id": str(uuid4()), "amount": "1.00", "currency": "USD"},
                ).status_code
                == 403
            )
            assert (
                client.get(
                    "/analytics/summary", params=params, headers={"Authorization": "Bearer invalid"}
                ).status_code
                == 401
            )
            report["cross_tenant_read_write"] = "403; invalid key 401"
            replay_outbox_events(factory, redis, stream)
            with factory() as session:
                old = dict(
                    session.scalar(
                        select(OutboxEvent.payload).where(
                            OutboxEvent.order_id == UUID(first["id"]),
                            OutboxEvent.order_version == 1,
                        )
                    )
                )
            old["event_id"] = str(uuid4())
            redis.xadd(stream, {"event": json.dumps(old)})
            redis.xadd(stream, {"event": '{"schema_version":1}'})
            drain()
            for endpoint, body in report["endpoints"].items():
                assert (
                    client.get(f"/analytics/{endpoint}", params=params, headers=headers(a)).json()
                    == body
                )
            report["replay_stale_and_quarantine"] = "Durable ACK with unchanged results"
            response = client.get(
                "/ops/metrics",
                params={"tenant_id": str(a)},
                headers={"Authorization": f"Bearer {tokens[2]}"},
            )
            response.raise_for_status()
            report["operations"] = response.json()
            report["oracle"] = "All six rollups match source for both tenants at fenced boundaries"
            report["status"] = "passed"
            report["completed_at"] = datetime.now(UTC).isoformat()
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()
        (output / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"Demo artifact: {output / 'report.json'}")


if __name__ == "__main__":
    main()
