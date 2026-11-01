"""Real HTTP/PG/Redis acceptance. Run only against isolated live_demo.py on 8001."""

import json
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from redis import Redis

config = json.loads(Path(".runtime/live-demo-config.json").read_text())
tid = config["tenant_id"]
headers = {"Authorization": "Bearer " + Path(".runtime/local-access-key.txt").read_text().strip()}
query = dict(
    tenant_id=tid, currency="USD", start="2026-10-01T00:00:00Z", end="2026-10-08T00:00:00Z"
)
client = httpx.Client(base_url="http://127.0.0.1:8001", headers=headers, timeout=40)


def get(path, **kw):
    r = client.get(path, **kw)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


before = get("/analytics/summary", params=query)
before_status = get("/analytics/status-breakdown", params=query)["statuses"]
before_ops = get(f"/tenants/{tid}/status")
with Redis.from_url(config["env"]["REDIS_URL"]) as redis:
    before_stream_length = redis.xlen(config["env"]["ORDER_STREAM"])
rows = []
for i in range(12):
    amount = "100.00" if i < 5 else "6.97" if i == 5 else "20.00"
    status_value = "shipped" if i < 6 else "placed" if i < 10 else "cancelled"
    rows.append(
        f"proof-{i % 6},Proof Product {i % 6},{amount},USD,"
        f"{status_value},2026-10-03T10:00:00Z,{uuid4()}"
    )
raw = "product_id,product_name,amount,currency,status,created_at,order_id\n" + "\n".join(rows)
started = perf_counter()
r = client.post(f"/tenants/{tid}/ingest/csv", content=raw, headers={"Content-Type": "text/csv"})
assert r.status_code == 200, r.text
result = r.json()
Path(".runtime/live-import-result.json").write_text(json.dumps(result, indent=2))
assert result["accepted"] == 12 and result["rejected"] == 0 and result["duplicates"] == 0, result
assert result["progress"]["stage"] == "AGGREGATED", result
assert result["progress"]["event_count"] == 20 and result["progress"]["processed_count"] == 20, (
    result
)
assert result["totals_by_currency"]["USD"]["sales_amount"] == "506.97"
after = get("/analytics/summary", params=query)
visibility_ms = round((perf_counter() - started) * 1000, 3)
assert after["total_order_count"] - before["total_order_count"] == 12
assert Decimal(after["sales_amount"]) - Decimal(before["sales_amount"]) == Decimal("506.97")
verify = get("/analytics/verify", params=query)
assert verify["status"] == "MATCH", verify
status = get("/analytics/status-breakdown", params=query)
before_counts = {s["status"]: s["total_order_count"] for s in before_status}
assert {
    s["status"]: s["total_order_count"] - before_counts[s["status"]] for s in status["statuses"]
} == {"placed": 4, "shipped": 6, "cancelled": 2}
for shape in ("trends", "top-products"):
    data = get("/analytics/" + shape, params=query)
    if shape == "trends":
        assert sum(b["total_order_count"] for b in data["buckets"]) == after["total_order_count"]
assert (
    client.post(
        f"/tenants/{tid}/ingest/csv", content=raw, headers={"Content-Type": "text/csv"}
    ).json()["duplicates"]
    == 12
)
assert get("/analytics/summary", params=query) == after
assert (
    client.get("/analytics/summary", params={**query, "tenant_id": str(uuid4())}).status_code == 403
)
assert httpx.get("http://127.0.0.1:8001/analytics/summary", params=query).status_code == 401
ops = get(f"/tenants/{tid}/status")
assert (
    (ops["order_count"] - before_ops["order_count"]) == 12
    and (ops["projection_count"] - before_ops["projection_count"]) == 12
    and len(ops["recent_logs"]) == 20
)
with Redis.from_url(config["env"]["REDIS_URL"]) as redis:
    assert (
        redis.xpending(config["env"]["ORDER_STREAM"], "astraroll-live-imports-v2")["pending"] == 0
    )
    assert redis.xlen(config["env"]["ORDER_STREAM"]) - before_stream_length == 20
names = get(
    f"/tenants/{tid}/products", params={"product_ids": result["preview_rows"][0]["product_id"]}
)
assert names[result["preview_rows"][0]["product_id"]]["product_name"] == "Proof Product 0"
proof = {
    "before": before,
    "after": after,
    "delta_orders": 12,
    "delta_sales": "506.97",
    "request_to_visible_ms": visibility_ms,
    "parity": verify,
    "outbox_events": 20,
    "durable_receipts": 20,
    "pending_ack": 0,
    "statuses": status,
    "ops": ops,
    "duplicate_orders": 12,
    "cross_tenant_status": 403,
    "unauthenticated_status": 401,
}
Path(".runtime/live-acceptance.json").write_text(json.dumps(proof, indent=2))
print(
    "PASS: +12 orders / USD +506.97; 20 publications, receipts and ACKs; "
    "SQL MATCH; 6/4/2 status deltas; duplicates/tenant denial; real catalog/operations."
)
