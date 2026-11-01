"""Serve preserved benchmark tenant dashboards read-only, using a local expiring key."""

import json
import os
from pathlib import Path

import uvicorn
from sqlalchemy import create_engine, text

root = Path(__file__).resolve().parents[1]
config = json.loads((root / ".runtime/live-demo-config.json").read_text())
url = "postgresql+psycopg://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a"
engine = create_engine(url)
with engine.connect() as c:
    ids = [
        str(i)
        for i in c.scalars(
            text("SELECT tenant_id FROM control_tenant_registry WHERE status='active'")
        )
    ]
engine.dispose()
os.environ.update(
    {
        "DEPLOYMENT_MODE": "database_per_tenant",
        "DATABASE_URL": url,
        "CONTROL_DATABASE_URL": url,
        "ANALYTICS_READ_BLOCK_REASON": (
            "Preserved benchmark rollups do not match the expanded 10M source. "
            "Owner requested no repair. Use the isolated demo at http://127.0.0.1:8001."
        ),
        "REDIS_URL": "redis://127.0.0.1:56380/0",
        "MAX_ACTIVE_TENANT_POOLS": "10",
        "TENANT_POOL_SIZE": "1",
        "TENANT_POOL_MAX_OVERFLOW": "0",
        "ANALYTICS_API_KEYS_JSON": json.dumps(
            [
                {
                    "subject": "local-read-only-benchmark",
                    "tenant_ids": ids,
                    "key_sha256": json.loads(config["env"]["ANALYTICS_API_KEYS_JSON"])[0][
                        "key_sha256"
                    ],
                    "expires_at": json.loads(config["env"]["ANALYTICS_API_KEYS_JSON"])[0][
                        "expires_at"
                    ],
                }
            ]
        ),
    }
)
uvicorn.run("astraroll.api:app", host="127.0.0.1", port=8000)
