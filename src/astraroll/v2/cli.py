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

    # Migrate tenant command
    mig_parser = subparsers.add_parser(
        "migrate-tenant", help="Migrate an existing tenant from V1 shared DB to dedicated V2 DB"
    )
    mig_parser.add_argument("--tenant-id", type=UUID, required=True, help="Tenant UUID")
    mig_parser.add_argument(
        "--batch-size", type=int, default=5000, help="Batch size for streaming (default: 5000)"
    )
    mig_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start from beginning rather than resuming from checkpoint",
    )
    mig_parser.add_argument(
        "--purge-source",
        action="store_true",
        help="Purge source records from shared DB after successful parity check",
    )
    mig_parser.add_argument(
        "--confirm-purge",
        type=str,
        default=None,
        help="Confirmation token (must match tenant-id) required if --purge-source is passed",
    )

    # Verify parity command
    verify_parser = subparsers.add_parser(
        "verify-parity",
        help="Run multi-dimensional parity verification between shared DB and tenant DB",
    )
    verify_parser.add_argument("--tenant-id", type=UUID, required=True, help="Tenant UUID")

    # Diagnose tenant command
    diag_parser = subparsers.add_parser(
        "diagnose-tenant", help="Inspect registry, checkpoint, and database status for a tenant"
    )
    diag_parser.add_argument("--tenant-id", type=UUID, required=True, help="Tenant UUID")

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

    elif args.command == "migrate-tenant":
        from astraroll.v2.runtime import get_tenant_migrator

        migrator = get_tenant_migrator()
        migrator.batch_size = args.batch_size
        print(
            f"Starting V1 -> V2 migration for tenant {args.tenant_id} "
            f"(batch_size={args.batch_size})..."
        )
        res = migrator.migrate_tenant(
            args.tenant_id,
            resume=not args.no_resume,
            purge_source_after=args.purge_source,
            confirm_purge=args.purge_source,
            approval_token=args.confirm_purge,
        )
        print(
            f"Successfully migrated tenant {res.tenant_id} to database {res.database_name}!\n"
            f"- Orders Migrated: {res.orders_migrated}\n"
            f"- Outbox Events Migrated: {res.outbox_migrated}\n"
            f"- Rollups Migrated: {res.rollups_migrated}\n"
            f"- Total Shipped Sales: {res.total_sales_amount}\n"
            f"- Duration: {res.duration_seconds:.2f}s\n"
            f"- Parity Verified: {res.verified}\n"
            f"- Resumed from Checkpoint: {res.resumed}\n"
            f"- Recovery Backup: {res.recovery_backup_path or 'None'}"
        )

    elif args.command == "verify-parity":
        from astraroll.v2.runtime import get_tenant_migrator

        migrator = get_tenant_migrator()
        print(f"Executing multi-dimensional parity verification for tenant {args.tenant_id}...")
        report = migrator.verify_parity(args.tenant_id)
        print(
            f"Parity Verification Result: {'PASSED' if report.verified else 'FAILED'}\n"
            f"- Orders: source={report.source_order_count}, target={report.target_order_count}\n"
            f"- Shipped Sales: source={report.source_sales}, target={report.target_sales}\n"
            f"- Outbox: source={report.source_outbox_count}, target={report.target_outbox_count}\n"
            f"- Status Breakdown Parity: {report.status_parity}\n"
            f"- Currency Parity: {report.currency_parity}\n"
            f"- Rollup Tables Parity: {report.rollups_parity}\n"
            f"- Coverage Parity: {report.coverage_parity}"
        )
        if not report.verified:
            print(f"Details: {report.details}")

    elif args.command == "diagnose-tenant":
        from astraroll.v2.runtime import get_tenant_migrator

        migrator = get_tenant_migrator()
        diag = migrator.diagnose_tenant(args.tenant_id)
        import json

        print(json.dumps(diag, indent=2, default=str))


if __name__ == "__main__":
    main()
