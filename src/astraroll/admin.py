"""Trusted local database operator commands. No API-key material is stored here."""

import argparse
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from astraroll.aggregation.bootstrap import bootstrap
from astraroll.aggregation.release_models import AggregateCoverage, BootstrapJob
from astraroll.db import get_session_factory
from astraroll.models import Tenant
from astraroll.operations import reconcile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("tenant-create")
    create.add_argument("--name", required=True)
    for name in ("bootstrap", "coverage", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--tenant", type=UUID, required=True)
        if name == "bootstrap":
            command.add_argument("--currency", action="append", required=True)
            command.add_argument("--start", type=datetime.fromisoformat, required=True)
            command.add_argument("--end", type=datetime.fromisoformat, required=True)
            command.add_argument("--batch-size", type=int, default=1000)
            command.add_argument("--resume", type=UUID)
    args = parser.parse_args()
    factory = get_session_factory()
    if args.command == "tenant-create":
        with factory.begin() as session:
            tenant = Tenant(name=args.name)
            session.add(tenant)
            session.flush()
            result = {"tenant_id": tenant.id}
    elif args.command == "bootstrap":
        try:
            result = {
                "job_id": bootstrap(
                    factory,
                    args.tenant,
                    args.currency,
                    args.start,
                    args.end,
                    args.batch_size,
                    args.resume,
                )
            }
        except Exception as exc:
            # No credentials or driver SQL in public output; operators inspect logs separately.
            parser.exit(
                1,
                f"Bootstrap failed ({type(exc).__name__}); inspect coverage/jobs, resume safely\n",
            )
    elif args.command == "reconcile":
        result = reconcile(factory, args.tenant)
    else:
        with factory() as session:
            result = {
                "coverage": [
                    dict(row)
                    for row in session.execute(
                        select(AggregateCoverage.__table__).where(
                            AggregateCoverage.tenant_id == args.tenant
                        )
                    ).mappings()
                ],
                "jobs": [
                    dict(row)
                    for row in session.execute(
                        select(BootstrapJob.__table__).where(BootstrapJob.tenant_id == args.tenant)
                    ).mappings()
                ],
            }
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
