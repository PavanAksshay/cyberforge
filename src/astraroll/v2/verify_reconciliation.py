"""Real API versus independent raw SQL across restored tenant databases."""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url

from astraroll.v2.reconcile import utc_engine


def verify_one(client, record, tid, start, end):
    database = record["database"]
    engine = utc_engine(
        make_url("postgresql+psycopg://astraroll@127.0.0.1:55433/postgres").set(database=database)
    )
    observations = []
    with engine.connect() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        for currency in record["before"]["currencies"]:
            params = {"tenant": UUID(tid), "currency": currency, "start": start, "end": end}
            query = {
                "tenant_id": tid,
                "currency": currency,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            where = (
                "tenant_id=:tenant AND currency=:currency "
                "AND created_at>=:start AND created_at<:end"
            )
            sales = "coalesce(sum(CASE WHEN status='shipped' THEN amount ELSE 0 END),0)"

            def request(shape, **extra):
                begun = time.perf_counter()
                response = client.get("/analytics/" + shape, params={**query, **extra})
                observations.append(
                    {
                        "shape": shape,
                        "currency": currency,
                        "milliseconds": round((time.perf_counter() - begun) * 1000, 3),
                        "status": response.status_code,
                    }
                )
                if response.status_code != 200:
                    raise ValueError(
                        f"{tid}/{shape}: {response.status_code}: {response.text[:180]}"
                    )
                return response.json()

            raw = db.execute(
                text(f"SELECT count(*),{sales} FROM orders WHERE {where}"), params
            ).one()
            api = request("summary")
            assert (api["total_order_count"], Decimal(api["sales_amount"])) == tuple(raw), (
                tid,
                "summary",
            )
            for interval in ("hour", "day"):
                raw = db.execute(
                    text(
                        f"SELECT date_trunc('{interval}',created_at),count(*),{sales} "
                        f"FROM orders WHERE {where} GROUP BY 1"
                    ),
                    params,
                ).all()
                expected = {r[0]: (r[1], r[2]) for r in raw}
                api = request("trends", interval=interval)
                assert len(api["buckets"]) == int(
                    (end - start) / timedelta(hours=1 if interval == "hour" else 24)
                ), (tid, "bucket_count")
                for row in api["buckets"]:
                    bucket = datetime.fromisoformat(row["bucket_start"].replace("Z", "+00:00"))
                    assert (row["total_order_count"], Decimal(row["sales_amount"])) == expected.get(
                        bucket, (0, Decimal(0))
                    ), (tid, interval, bucket)
            raw = db.execute(
                text(
                    f"SELECT product_id,count(*),{sales} AS recognized_sales "
                    f"FROM orders WHERE {where} GROUP BY product_id HAVING {sales}>0 "
                    "ORDER BY recognized_sales DESC,count(*) DESC,product_id ASC LIMIT 10"
                ),
                params,
            ).all()
            api = request("top-products", limit=10)
            assert [
                (r["product_id"], r["total_order_count"], Decimal(r["sales_amount"]))
                for r in api["products"]
            ] == [(str(r[0]), r[1], r[2]) for r in raw], (tid, "top_products")
            raw = db.execute(
                text(f"SELECT status,count(*),{sales} FROM orders WHERE {where} GROUP BY status"),
                params,
            ).all()
            expected = {r[0]: (r[1], r[2]) for r in raw}
            api = request("status-breakdown")
            assert {r["status"] for r in api["statuses"]} == {"placed", "shipped", "cancelled"}
            assert all(
                (r["total_order_count"], Decimal(r["sales_amount"]))
                == expected.get(r["status"], (0, Decimal(0)))
                for r in api["statuses"]
            ), (tid, "statuses")
    engine.dispose()
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".runtime/tenant-analytics-repair.json"))
    parser.add_argument("--tenant-id")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--in-process", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/restored-api-verification.json")
    )
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    ids = [tid for tid, r in state["tenants"].items() if r["phase"] == "verified"]
    if args.tenant_id:
        ids = [args.tenant_id] if args.tenant_id in ids else []
    if not ids:
        raise ValueError("No durably verified tenant selected")
    token = Path(".runtime/local-access-key.txt").read_text().strip()
    headers = {"Authorization": "Bearer " + token}
    if args.in_process:
        cfg = json.loads(Path(".runtime/live-demo-config.json").read_text())
        key = json.loads(cfg["env"]["ANALYTICS_API_KEYS_JSON"])[0]
        key["tenant_ids"] = ids
        os.environ.update(
            DEPLOYMENT_MODE="database_per_tenant",
            ANALYTICS_READ_BLOCK_REASON="",
            DATABASE_URL="postgresql+psycopg://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a",
            CONTROL_DATABASE_URL="postgresql+psycopg://astraroll@127.0.0.1:55433/"
            "v2bench_control_78b1ee4a",
            ANALYTICS_API_KEYS_JSON=json.dumps([key]),
            MAX_ACTIVE_TENANT_POOLS="4",
            TENANT_POOL_SIZE="1",
            TENANT_POOL_MAX_OVERFLOW="0",
        )
        from astraroll.config import get_settings

        get_settings.cache_clear()
        from fastapi.testclient import TestClient

        from astraroll.api import app

        client = TestClient(app, headers=headers)
    else:
        client = httpx.Client(base_url="http://127.0.0.1:8000", headers=headers, timeout=30)
    proof = {
        "window": {"start": state["start"], "end": state["end"]},
        "tenants": {},
        "transport": "ASGI real PostgreSQL" if args.in_process else "TCP port 8000",
    }
    with client:
        for i, tid in enumerate(ids, 1):
            observations = verify_one(
                client,
                state["tenants"][tid],
                tid,
                datetime.fromisoformat(state["start"]),
                datetime.fromisoformat(state["end"]),
            )
            proof["tenants"][tid] = {"result": "MATCH", "observations": observations}
            args.output.write_text(json.dumps(proof, indent=2))
            print(
                f"API/SQL MATCH {i}/{len(ids)}: {tid} (all currencies, four shapes, hourly/daily)",
                flush=True,
            )
        if args.measure:
            from astraroll.benchmark import percentiles
            from astraroll.v2.scale_validation import reader_query_plans

            profile = {
                "currency": "USD",
                "concurrency": args.concurrency,
                "samples_per_shape": args.samples,
                "tenant_databases": len(ids),
                "source_orders": sum(state["tenants"][tid]["before"]["orders"] for tid in ids),
                "window": proof["window"],
                "queries": {},
            }
            for shape in ("summary", "trends", "top-products", "status-breakdown"):

                def sample(i):
                    q = {
                        "tenant_id": ids[i % len(ids)],
                        "currency": "USD",
                        "start": state["start"],
                        "end": state["end"],
                    }
                    if shape == "trends":
                        q["interval"] = "day"
                    began = time.perf_counter()
                    r = client.get("/analytics/" + shape, params=q)
                    r.raise_for_status()
                    r.json()
                    return (time.perf_counter() - began) * 1000

                began = time.perf_counter()
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    values = list(pool.map(sample, range(args.samples)))
                profile["queries"][shape] = {
                    **percentiles(values),
                    "samples": len(values),
                    "requests_per_second": len(values) / (time.perf_counter() - began),
                    "p95_under_200ms": percentiles(values)["p95_ms"] < 200,
                    "raw_ms": values,
                }
                print(
                    shape,
                    {k: v for k, v in profile["queries"][shape].items() if k != "raw_ms"},
                    flush=True,
                )
            engine = utc_engine(
                make_url("postgresql+psycopg://astraroll@127.0.0.1:55433/postgres").set(
                    database=state["tenants"][ids[0]]["database"]
                )
            )
            profile["query_plans"] = reader_query_plans(
                engine,
                {
                    "tenant": UUID(ids[0]),
                    "currency": "USD",
                    "start": datetime.fromisoformat(state["start"]),
                    "end": datetime.fromisoformat(state["end"]),
                },
            )
            engine.dispose()
            Path(".runtime/restored-performance.json").write_text(json.dumps(profile, indent=2))


if __name__ == "__main__":
    main()
