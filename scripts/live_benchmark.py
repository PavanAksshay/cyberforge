"""Serve the restored 100-tenant product on port 8000; fail closed during rebuild."""

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

import uvicorn
from sqlalchemy import text

from astraroll.config import get_settings
from astraroll.v2.reconcile import utc_engine
from astraroll.v2.restoration import release_readiness

root = Path(__file__).resolve().parents[1]
config = json.loads((root / ".runtime/live-demo-config.json").read_text())
url = "postgresql+psycopg://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a"
engine = utc_engine(url)
with engine.connect() as c:
    ids = [
        str(i)
        for i in c.scalars(
            text(
                "SELECT tenant_id FROM control_tenant_registry "
                "WHERE status='active' ORDER BY tenant_id"
            )
        )
    ]
engine.dispose()
ready, reason = release_readiness(url, root / ".runtime/tenant-analytics-repair.json")
original = json.loads(config["env"]["ANALYTICS_API_KEYS_JSON"])[0]
keys = [
    {
        "subject": "local-100-tenant-operator",
        "tenant_ids": ids,
        "key_sha256": original["key_sha256"],
        "expires_at": original["expires_at"],
    }
]
# A narrowly scoped local probe verifies cross-tenant denial without changing operator access.
probe_path = root / ".runtime/tenant-boundary-key.txt"
if not probe_path.exists():
    probe_path.write_text(secrets.token_urlsafe(32))
keys.append(
    {
        "subject": "tenant-isolation-probe",
        "tenant_ids": ids[:1],
        "key_sha256": hashlib.sha256(probe_path.read_text().strip().encode()).hexdigest(),
        "expires_at": original["expires_at"],
    }
)
os.environ.update(
    DEPLOYMENT_MODE="database_per_tenant",
    DATABASE_URL=url,
    CONTROL_DATABASE_URL=url,
    ASSISTANT_TIMEZONE="Asia/Kolkata",
    ANALYTICS_READ_BLOCK_REASON="" if ready else reason,
    BENCHMARK_WRITES_ENABLED="true" if ready else "false",
    LIVE_OUTBOX_HINTS_ENABLED="true" if ready else "false",
    LIVE_WORKER_HEALTH_PATH=str(root / ".runtime/live-worker-health.json"),
    REDIS_URL="redis://127.0.0.1:56380/0",
    MAX_ACTIVE_TENANT_POOLS="4",
    TENANT_POOL_SIZE="1",
    TENANT_POOL_MAX_OVERFLOW="0",
    ANALYTICS_API_KEYS_JSON=json.dumps(keys),
)
get_settings.cache_clear()
print(reason, flush=True)
if "--worker" in sys.argv:
    sys.argv.remove("--worker")
    if not ready:
        raise RuntimeError(reason)
    os.environ["MAX_ACTIVE_TENANT_POOLS"] = "2"
    get_settings.cache_clear()
    from astraroll.v2.live_worker import main

    main()
else:
    uvicorn.run("astraroll.api:app", host="127.0.0.1", port=8000)
