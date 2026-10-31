"""Explicit V2 worker entrypoints; never fall back to the archival shared DB."""

import argparse
import logging
import signal
from threading import Event

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
    args = parser.parse_args()
    settings = get_settings()
    require_v2(settings)  # Before opening Redis or any database connection.
    logging.basicConfig(level=logging.INFO)
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    manager = get_tenant_engine_manager()
    try:
        with Redis.from_url(
            settings.redis_url, socket_timeout=5, socket_connect_timeout=5
        ) as redis:
            if args.worker == "relay":
                operation = get_multi_tenant_relay(redis).relay_batch
            else:
                consumer = get_multi_tenant_consumer(redis)
                consumer.init_group()
                operation = consumer.process_batch
            while not stop.is_set():
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
