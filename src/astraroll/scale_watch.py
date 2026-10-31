"""Low-frequency host/PG diagnostics for one owned benchmark process."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
from sqlalchemy import text

from astraroll.db import get_engine
from astraroll.host_resources import system_commit_available


def sample():
    vm = psutil.virtual_memory()
    result = {
        "at": datetime.now(UTC).isoformat(),
        "available_memory_bytes": vm.available,
        "host_cpu_percent": psutil.cpu_percent(),
        "swap_free_bytes": psutil.swap_memory().free,
        "system_commit_available_bytes": system_commit_available(),
        "drives": {p: psutil.disk_usage(p)._asdict() for p in ("C:/", "D:/")},
        "disk_io": psutil.disk_io_counters()._asdict(),
    }
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SET statement_timeout='3s'"))
            result["wal"] = dict(conn.execute(text("SELECT * FROM pg_stat_wal")).mappings().one())
            result["checkpoints"] = dict(
                conn.execute(text("SELECT * FROM pg_stat_bgwriter")).mappings().one()
            )
            result["waits"] = [
                dict(row)
                for row in conn.execute(
                    text("""SELECT state,
                wait_event_type,wait_event,count(*) AS sessions FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                GROUP BY state,wait_event_type,wait_event""")
                ).mappings()
            ]
            result["settings"] = dict(
                conn.execute(
                    text(
                        "SELECT name,setting FROM pg_settings WHERE name IN "
                        "('fsync','full_page_writes','synchronous_commit','track_io_timing')"
                    )
                ).all()
            )
    except Exception as exc:
        result["database_error_type"] = type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    if args.seconds < 2:
        parser.error("Use at least a two-second sampling interval")
    process = psutil.Process(args.pid)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a") as file:
        while process.is_running():
            file.write(json.dumps(sample(), default=str) + "\n")
            file.flush()
            time.sleep(min(60, args.seconds))


if __name__ == "__main__":
    main()
