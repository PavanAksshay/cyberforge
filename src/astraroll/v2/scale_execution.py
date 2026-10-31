"""Real HTTP verification runner for already migrated dedicated databases."""

import argparse
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
from uuid import UUID

import httpx
from redis import Redis

from astraroll.config import get_settings
from astraroll.performance_process import stop_owned_process
from astraroll.v2.registry import ControlRegistry, TenantStatus
from astraroll.v2.router import TenantDatabaseResolver, TenantEngineManager
from astraroll.v2.scale_live import live_probe
from astraroll.v2.scale_load import run_load
from astraroll.v2.scale_validation import measure_api, normalized, verify_plan
from astraroll.v2.subset_migration import writable_engine
from astraroll.workload import END, START


def execute(plan, output, samples=200, run_live=False):
    settings = get_settings()
    control_url = settings.control_database_url
    if not control_url:
        raise ValueError("Explicit control database required")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = {"phase": "verifying", "baseline": "b452d31da56cc7942f6efcaf500f5d8eac7f41e5"}
    report["parity"] = verify_plan(settings.database_url, control_url, plan)
    tokens = [secrets.token_urlsafe(32) for _ in range(3)]
    records = [
        {
            "subject": f"benchmark-{i}",
            "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "tenant_ids": [t["tenant_id"] for t in plan["tenants"]]
            if i == 0
            else [plan["tenants"][0]["tenant_id"]],
            "expires_at": (datetime.now(UTC) + timedelta(hours=12 if i != 2 else -1)).isoformat(),
        }
        for i, token in enumerate(tokens)
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "DATABASE_URL": control_url,
        "CONTROL_DATABASE_URL": control_url,
        "DEPLOYMENT_MODE": "database_per_tenant",
        "MAX_ACTIVE_TENANT_POOLS": "10",
        "TENANT_POOL_SIZE": "1",
        "TENANT_POOL_MAX_OVERFLOW": "0",
        "ANALYTICS_API_KEYS_JSON": json.dumps(records),
        "ANALYTICS_BACKEND": "postgres",
        "ANALYTICS_COVERAGE_PROVIDER": "astraroll.aggregation.coverage:read_coverage",
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
    engine = writable_engine(control_url)
    manager = TenantEngineManager(
        TenantDatabaseResolver(ControlRegistry(engine), control_url),
        max_active_pools=10,
        pool_size=1,
        max_overflow=0,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(
            base_url=url, timeout=30, headers={"Authorization": f"Bearer {tokens[0]}"}
        ) as client:
            for _ in range(150):
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Real API did not become ready; inspect api.log")
            first = plan["tenants"][0]
            params = {
                "tenant_id": first["tenant_id"],
                "currency": "USD",
                "start": START.isoformat(),
                "end": END.isoformat(),
            }
            denied = (
                plan["tenants"][1]["tenant_id"] if len(plan["tenants"]) > 1 else str(UUID(int=1))
            )
            for endpoint in ("summary", "trends", "top-products", "status-breakdown"):
                for token, tenant, status in (
                    (tokens[1], denied, 403),
                    (tokens[2], first["tenant_id"], 401),
                    ("invalid", first["tenant_id"], 401),
                ):
                    response = client.get(
                        f"/analytics/{endpoint}",
                        params={**params, "tenant_id": tenant},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if response.status_code != status:
                        raise ValueError(f"Authorization boundary failed: {endpoint}")
            report["security"] = "four endpoints: cross-tenant denied; expired/invalid keys denied"
            for path in (f"/tenants/{denied}", f"/tenants/{denied}/orders"):
                denied_response = client.get(path, headers={"Authorization": f"Bearer {tokens[1]}"})
                if denied_response.status_code != 403:
                    raise ValueError("Cross-tenant producer read was not denied")
            # Readiness warmed the real API's first pool: fail closed even there.
            registry = ControlRegistry(engine)
            first_id = UUID(first["tenant_id"])
            registry.update_status(first_id, TenantStatus.MIGRATING)
            try:
                if client.get("/analytics/summary", params=params).status_code != 503:
                    raise ValueError("Inactive tenant bypassed cached API routing")
            finally:
                registry.update_status(first_id, TenantStatus.ACTIVE)
            report["cached_inactive_routing_denied"] = True
            report["measurement"] = measure_api(control_url, plan, url, tokens[0], samples, 4)
            os.environ["BENCHMARK_BEARER_TOKEN"] = tokens[0]
            try:
                report["interference"] = run_load(plan, url, output / "interference", seconds=20)
            finally:
                os.environ.pop("BENCHMARK_BEARER_TOKEN", None)
            if run_live:
                with Redis.from_url(settings.redis_url, socket_timeout=30) as redis:
                    report["live"] = live_probe(
                        manager, redis, client, UUID(first["tenant_id"]), params
                    )
                report["post_live_measurement"] = measure_api(
                    control_url, plan, url, tokens[0], samples, 4
                )
            report["phase"] = "complete"
    finally:
        (output / "report.json").write_text(json.dumps(report, default=normalized, indent=2))
        manager.close_all()
        engine.dispose()
        stop_owned_process(process)  # Only this runner's HTTP helper, never PostgreSQL/Redis.
        log.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    plan = json.loads(Path(args.manifest).read_text())
    plan["tenants"] = plan["tenants"][: args.limit]
    execute(plan, args.output, args.samples, args.live)
    print("Verified real HTTP execution:", args.output)


if __name__ == "__main__":
    main()
