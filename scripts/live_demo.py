"""Create/resume an isolated live-demo control database; never use port 55433."""

import hashlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from astraroll.aggregation.bootstrap import bootstrap
from astraroll.v2.provisioning import TenantProvisioner
from astraroll.v2.registry import ControlRegistry

root = Path(__file__).resolve().parents[1]
path = root / ".runtime/live-demo-config.json"
if not path.exists():
    base = "postgresql+psycopg://astraroll@127.0.0.1:55434/postgres"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    name = "live_demo_control_" + uuid4().hex[:8]
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    registry = ControlRegistry(create_engine(url))
    registry.init_schema()
    prov = TenantProvisioner(registry, url, tenant_database_prefix="live_demo_t_")
    tenant = uuid4()
    record = prov.provision_tenant(tenant, "Isolated Live Demo (disposable)")
    engine = create_engine(make_url(url).set(database=record.database_name))
    bootstrap(
        sessionmaker(engine, expire_on_commit=False),
        tenant,
        ["USD", "EUR"],
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 8, tzinfo=UTC),
    )
    token = secrets.token_urlsafe(32)
    config = {
        "tenant_id": str(tenant),
        "database_name": record.database_name,
        "env": {
            "DATABASE_URL": url,
            "CONTROL_DATABASE_URL": url,
            "DEPLOYMENT_MODE": "database_per_tenant",
            "REDIS_URL": "redis://127.0.0.1:56381/0",
            "ORDER_STREAM": "astraroll:live-demo:" + tenant.hex,
            "ANALYTICS_API_KEYS_JSON": json.dumps(
                [
                    {
                        "subject": "local-live-demo",
                        "tenant_ids": [str(tenant)],
                        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                    }
                ]
            ),
            "MAX_ACTIVE_TENANT_POOLS": "10",
            "TENANT_POOL_SIZE": "1",
            "TENANT_POOL_MAX_OVERFLOW": "0",
        },
    }
    (root / ".runtime/local-access-key.txt").write_text(token)
    path.write_text(json.dumps(config, indent=2))
    engine.dispose()
    prov.dispose()
    admin.dispose()
config = json.loads(path.read_text())
# One-time upgrade from the earlier local fixture: raw key is a separate local credential.
if "token" in config:
    (root / ".runtime/local-access-key.txt").write_text(config.pop("token"))
    path.write_text(json.dumps(config, indent=2))
print(json.dumps({k: v for k, v in config.items() if k not in ("token", "env")}))
if "--setup-only" not in sys.argv:
    os.environ.update(config["env"])
    from astraroll.config import get_settings

    get_settings.cache_clear()
    import uvicorn

    uvicorn.run("astraroll.api:app", host="127.0.0.1", port=8001)
