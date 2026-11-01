"""Read-only protected data fingerprint. No writes, migrations, or relay."""

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

url = "postgresql+psycopg://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a"
e = create_engine(url)
result = {}
with e.connect() as c:
    rows = c.execute(
        text(
            "SELECT tenant_id,database_name FROM control_tenant_registry "
            "WHERE status='active' ORDER BY tenant_id"
        )
    ).all()
for tid, db in rows:
    te = create_engine(make_url(url).set(database=db))
    with te.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        c.execute(text("SET LOCAL statement_timeout='10000ms'"))
        result[db] = dict(
            c.execute(
                text("""SELECT count(*) AS orders, sum(version)::text AS versions,
          sum(amount)::text AS amount, md5(string_agg(
          id::text||version::text||status||amount::text||created_at::text||updated_at::text,
          ',' ORDER BY id)) AS fingerprint,
          (SELECT count(*) FROM outbox_events) AS outbox,
          (SELECT count(*) FROM order_projections) AS projections,
          (SELECT revision FROM tenant_processing_state LIMIT 1) AS revision
          FROM orders""")
            )
            .mappings()
            .one()
        )
    te.dispose()
with create_engine(make_url(url).set(database="postgres")).connect() as c:
    result["source"] = dict(
        c.execute(
            text("""SELECT (SELECT count(*)
          FROM bench_33aa799a3434455581c3d69793b34df1.orders) AS orders,
       (SELECT count(*) FROM bench_33aa799a3434455581c3d69793b34df1.outbox_events) AS outbox,
       pg_postmaster_start_time()::text AS postmaster_started""")
        )
        .mappings()
        .one()
    )
Path(sys.argv[1]).write_text(json.dumps(result, indent=2))
print(
    json.dumps(
        {
            "databases": len(rows),
            "v2_orders": sum(v["orders"] for k, v in result.items() if k != "source"),
            "source": result["source"],
        }
    )
)
