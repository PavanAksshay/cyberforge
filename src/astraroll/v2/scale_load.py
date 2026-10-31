"""Independent-process normal-source interference versus authenticated V2 reads."""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text

from astraroll.benchmark import ENDPOINTS, percentiles
from astraroll.config import get_settings
from astraroll.v2.scale_validation import (
    normalized,
    readonly_engine,
    resource_snapshot,
    source_schema,
)
from astraroll.workload import END, START


def wait_file(path, timeout=60):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Probe handshake timed out: {path.name}")
        time.sleep(0.02)


def normal_probe(spec, root):
    engine = readonly_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            sql = text(
                f'SELECT id,version FROM "{source_schema(spec["schema"])}".orders '
                "WHERE tenant_id=:tenant AND id=:id"
            )
            params = {"tenant": UUID(spec["tenant"]), "id": UUID(spec["order"])}
            for _ in range(50):
                conn.execute(sql, params).one()
            results = {}
            for phase in ("idle", "loaded"):
                if phase == "loaded":
                    (root / "ready").touch()
                    wait_file(root / "go")
                start, values = time.perf_counter(), []
                for i in range(int(spec["seconds"] * spec["normal_rate"])):
                    time.sleep(max(0, start + i / spec["normal_rate"] - time.perf_counter()))
                    begin = time.perf_counter()
                    conn.execute(sql, params).one()
                    values.append((time.perf_counter() - begin) * 1000)
                results[phase] = {
                    **percentiles(values),
                    "samples": len(values),
                    "raw_ms": values,
                    "elapsed_seconds": time.perf_counter() - start,
                }
            (root / "normal.json").write_text(json.dumps(results))
    finally:
        engine.dispose()


def run_load(plan, api_url, output, seconds=20, concurrency=4, dashboard_rate=20, normal_rate=100):
    if seconds < 5 or not 1 <= concurrency <= 16 or dashboard_rate <= 0 or normal_rate <= 0:
        raise ValueError("Use >=5 seconds, 1..16 users and positive request rates")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    tenant = plan["tenants"][0]
    spec = {
        "schema": plan["source_schema"],
        "tenant": tenant["tenant_id"],
        "order": tenant["last_order_id"],
        "seconds": seconds,
        "normal_rate": normal_rate,
    }
    (root / "spec.json").write_text(json.dumps(spec))
    settings = get_settings()
    engine = readonly_engine(settings.database_url)
    log = (root / "probe.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", __name__, "--probe", str(root)],
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        with engine.connect() as conn:
            before = resource_snapshot(conn)
        wait_file(root / "ready", timeout=seconds + 60)

        def dashboard(user):
            values = []
            with httpx.Client(
                base_url=api_url,
                timeout=30,
                headers={"Authorization": f"Bearer {os.environ['BENCHMARK_BEARER_TOKEN']}"},
            ) as client:
                start = time.perf_counter()
                for i in range(int(seconds * dashboard_rate / concurrency)):
                    time.sleep(
                        max(0, start + i * concurrency / dashboard_rate - time.perf_counter())
                    )
                    item = plan["tenants"][(i * concurrency + user) % len(plan["tenants"])]
                    begin = time.perf_counter()
                    response = client.get(
                        f"/analytics/{ENDPOINTS[i % 4]}",
                        params={
                            "tenant_id": item["tenant_id"],
                            "currency": "USD",
                            "start": START.isoformat(),
                            "end": END.isoformat(),
                        },
                    )
                    response.raise_for_status()
                    values.append((time.perf_counter() - begin) * 1000)
            return values

        with ThreadPoolExecutor(concurrency) as pool:
            (root / "go").touch()
            values = [v for batch in pool.map(dashboard, range(concurrency)) for v in batch]
        process.wait(timeout=seconds + 60)
        if process.returncode:
            raise RuntimeError("Normal SQL probe failed; inspect probe.log")
        normal = json.loads((root / "normal.json").read_text())
        with engine.connect() as conn:
            after = resource_snapshot(conn)
        result = {
            "normal": normal,
            "dashboard": {**percentiles(values), "samples": len(values)},
            "normal_p95_slowdown_percent": (
                normal["loaded"]["p95_ms"] / normal["idle"]["p95_ms"] - 1
            )
            * 100,
            "resources_before": before,
            "resources_after": after,
            "configuration": {
                "seconds": seconds,
                "users": concurrency,
                "target_dashboard_rps": dashboard_rate,
                "target_normal_rps": normal_rate,
            },
            "limitations": [
                "Warm indexed normal point read, not all application SQL",
                "Whole-cluster counters and endpoint snapshots, not peak resources",
                "Actual wall time/sample counts disclose scheduling overrun",
                "Repeat with identical settings; no automatic speedup claim",
            ],
        }
        (root / "report.json").write_text(json.dumps(result, default=normalized, indent=2))
        return result
    finally:
        # Child has a bounded loop and exits naturally; never terminate PostgreSQL/user processes.
        if process.poll() is None:
            process.wait(timeout=seconds + 65)
        log.close()
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe")
    parser.add_argument("--manifest")
    parser.add_argument("--api-url")
    parser.add_argument("--output")
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    if args.probe:
        root = Path(args.probe)
        normal_probe(json.loads((root / "spec.json").read_text()), root)
    else:
        if not all(
            (args.manifest, args.api_url, args.output, os.environ.get("BENCHMARK_BEARER_TOKEN"))
        ):
            parser.error("Require --manifest, --api-url, --output and BENCHMARK_BEARER_TOKEN")
        run_load(
            json.loads(Path(args.manifest).read_text()), args.api_url, args.output, args.seconds
        )


if __name__ == "__main__":
    main()
