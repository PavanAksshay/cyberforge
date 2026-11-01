"""Read-only direct SQL oracle for every live demo analytics shape and currency."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

cfg = json.loads(Path(".runtime/live-demo-config.json").read_text())
tid = cfg["tenant_id"]
e = create_engine(make_url(cfg["env"]["DATABASE_URL"]).set(database=cfg["database_name"]))
c = httpx.Client(
    base_url="http://127.0.0.1:8001",
    headers={
        "Authorization": "Bearer " + Path(".runtime/local-access-key.txt").read_text().strip()
    },
    timeout=20,
)
start = datetime(2026, 10, 1, tzinfo=UTC)
end = datetime(2026, 10, 8, tzinfo=UTC)
results = []
with e.connect() as db:
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    db.execute(text("SET LOCAL statement_timeout='3000ms'"))
    for currency in ("USD", "EUR"):
        p = {"tid": UUID(tid), "currency": currency, "start": start, "end": end}
        q = {
            "tenant_id": tid,
            "currency": currency,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        where = "tenant_id=:tid AND currency=:currency AND created_at>=:start AND created_at<:end"
        money = "coalesce(sum(CASE WHEN status='shipped' THEN amount ELSE 0 END),0)"

        def request(shape, **extra):
            r = c.get("/analytics/" + shape, params={**q, **extra})
            assert r.status_code == 200, r.text
            return r.json()

        raw = db.execute(text(f"SELECT count(*),{money} FROM orders WHERE {where}"), p).one()
        api = request("summary")
        assert (api["total_order_count"], Decimal(api["sales_amount"])) == tuple(raw)
        for interval in ("hour", "day"):
            raw = db.execute(
                text(
                    f"SELECT date_trunc('{interval}',created_at AT TIME ZONE 'UTC') "
                    f"AT TIME ZONE 'UTC',count(*),{money} FROM orders WHERE {where} GROUP BY 1"
                ),
                p,
            ).all()
            expected = {r[0]: (r[1], r[2]) for r in raw}
            api = request("trends", interval=interval)
            assert all(
                (r["total_order_count"], Decimal(r["sales_amount"]))
                == expected.get(
                    datetime.fromisoformat(r["bucket_start"].replace("Z", "+00:00")),
                    (0, Decimal(0)),
                )
                for r in api["buckets"]
            )
            assert len(api["buckets"]) == int(
                (end - start) / timedelta(hours=1 if interval == "hour" else 24)
            )
        raw = db.execute(
            text(
                f"SELECT product_id,count(*),{money} AS sales FROM orders WHERE {where} "
                f"GROUP BY product_id HAVING {money}>0 "
                "ORDER BY sales DESC,count(*) DESC,product_id ASC LIMIT 10"
            ),
            p,
        ).all()
        api = request("top-products")
        assert [
            (r["product_id"], r["total_order_count"], Decimal(r["sales_amount"]))
            for r in api["products"]
        ] == [(str(r[0]), r[1], r[2]) for r in raw]
        raw = db.execute(
            text(f"SELECT status,count(*),{money} FROM orders WHERE {where} GROUP BY status"), p
        ).all()
        expected = {r[0]: (r[1], r[2]) for r in raw}
        api = request("status-breakdown")
        assert len(api["statuses"]) == 3
        assert all(
            (r["total_order_count"], Decimal(r["sales_amount"]))
            == expected.get(r["status"], (0, Decimal(0)))
            for r in api["statuses"]
        )
        results.append(
            {
                "currency": currency,
                "shapes": [
                    "summary",
                    "trends-hour",
                    "trends-day",
                    "top-products",
                    "status-breakdown",
                ],
                "result": "MATCH",
            }
        )
print(json.dumps(results))
Path(".runtime/live-oracle-evidence.json").write_text(json.dumps(results, indent=2))
