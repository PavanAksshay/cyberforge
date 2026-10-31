"""Explicit V2 worker entrypoints; never fall back to the archival shared DB."""

import argparse
import logging
import signal
import time
from threading import Event
from uuid import UUID

from redis import Redis

from astraroll.config import get_settings
from astraroll.v2.runtime import (
    get_control_engine,
    get_multi_tenant_consumer,
    get_multi_tenant_relay,
    get_tenant_engine_manager,
)

log = logging.getLogger(__name__)


def require_v2(settings):
    if settings.deployment_mode != "database_per_tenant" or not settings.control_database_url:
        raise ValueError("V2 workers require database_per_tenant and explicit CONTROL_DATABASE_URL")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker", choices=("relay", "consumer"))
    parser.add_argument("--once", action="store_true")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--tenant-id", type=UUID)
    scope.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--run-seconds", type=float, default=0)
    parser.add_argument("--idle-claim-ms", type=int, default=30000)
    parser.add_argument("--group")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 1000 or args.run_seconds < 0 or args.idle_claim_ms < 0:
        parser.error("Use batch size 1..1000 and nonnegative time/claim bounds")
    if args.worker == "relay" and not (args.tenant_id or args.all_tenants):
        parser.error(
            "Relay requires --tenant-id or explicit --all-tenants; no implicit backlog drain"
        )
    if args.worker == "consumer" and (args.tenant_id or args.all_tenants):
        parser.error("Consumer scope is its configured stream, not a tenant filter")
    settings = get_settings()
    require_v2(settings)  # Before opening Redis or any database connection.
    logging.basicConfig(level=logging.INFO)
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    manager = get_tenant_engine_manager()
    deadline = time.monotonic() + args.run_seconds if args.run_seconds else None
    try:
        with Redis.from_url(
            settings.redis_url, socket_timeout=5, socket_connect_timeout=5
        ) as redis:
            if args.worker == "relay":
                relay = get_multi_tenant_relay(redis)
                operation = (
                    (lambda: relay.relay_tenant(args.tenant_id, args.batch_size))
                    if args.tenant_id
                    else lambda: relay.relay_batch(args.batch_size)
                )
            else:
                consumer = get_multi_tenant_consumer(redis)
                consumer.batch_size = args.batch_size
                consumer.min_idle_claim_ms = args.idle_claim_ms
                if args.group:
                    consumer.group_name = args.group
                consumer.init_group()
                operation = consumer.process_batch
            while not stop.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    return
                try:
                    count = operation()
                    if args.once:
                        return
                    if not count:
                        stop.wait(settings.relay_poll_seconds)
                except Exception:
                    log.exception("V2 worker batch failed; durable work retained for retry")
                    if args.once:
                        raise
                    stop.wait(max(1, settings.relay_poll_seconds))
    finally:
        manager.close_all()
        get_control_engine().dispose()


if __name__ == "__main__":
    main()
