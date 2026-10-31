"""CLI and Automation Tools for AstraRoll V2 Database-per-Tenant Operations."""

from __future__ import annotations

import argparse
from uuid import UUID, uuid4

from astraroll.v2.runtime import get_control_registry, get_tenant_provisioner


def main():
    parser = argparse.ArgumentParser(description="AstraRoll V2 Multi-Tenant Administration CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Provision command
    prov_parser = subparsers.add_parser(
        "provision", help="Provision a new dedicated tenant database"
    )
    prov_parser.add_argument("--name", required=True, help="Tenant organization name")
    prov_parser.add_argument(
        "--tenant-id", type=UUID, default=None, help="Optional UUID for tenant"
    )

    # List command
    subparsers.add_parser("list", help="List all registered tenants and their status")

    # Deprovision command
    deprov_parser = subparsers.add_parser("deprovision", help="Decommission a tenant")
    deprov_parser.add_argument("--tenant-id", type=UUID, required=True, help="Tenant UUID")
    deprov_parser.add_argument(
        "--drop-db", action="store_true", help="Drop physical PostgreSQL database"
    )

    # Scale seed command
    scale_parser = subparsers.add_parser(
        "scale-seed", help="Bulk provision N tenant databases concurrently"
    )
    scale_parser.add_argument(
        "--count", type=int, default=10, help="Number of tenants to provision"
    )
    scale_parser.add_argument("--workers", type=int, default=5, help="Concurrent worker count")

    args = parser.parse_args()

    if args.command == "provision":
        t_id = args.tenant_id or uuid4()
        provisioner = get_tenant_provisioner()
        record = provisioner.provision_tenant(t_id, args.name)
        print(
            f"Provisioned tenant: id={record.tenant_id}, name={record.name}, "
            f"db={record.database_name}, status={record.status}"
        )

    elif args.command == "list":
        registry = get_control_registry()
        tenants = registry.list_all_tenants()
        print(f"{'Tenant ID':<38} | {'Name':<25} | {'Database Name':<30} | {'Status':<12}")
        print("-" * 115)
        for t in tenants:
            tid_s = str(t.tenant_id)
            print(f"{tid_s:<38} | {t.name:<25} | {t.database_name:<30} | {t.status:<12}")

    elif args.command == "deprovision":
        provisioner = get_tenant_provisioner()
        provisioner.deprovision_tenant(args.tenant_id, drop_database=args.drop_db)
        print(f"Deprovisioned tenant {args.tenant_id} (drop_db={args.drop_db})")

    elif args.command == "scale-seed":
        provisioner = get_tenant_provisioner()
        tenants = [(uuid4(), f"Scale Customer {i + 1}") for i in range(args.count)]
        print(f"Bulk provisioning {args.count} tenants with {args.workers} workers...")
        records = provisioner.provision_tenants_concurrently(tenants, max_workers=args.workers)
        print(f"Successfully provisioned {len(records)} tenant databases.")


if __name__ == "__main__":
    main()
