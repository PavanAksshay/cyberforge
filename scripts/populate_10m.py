"""Archived synthetic-scale helper, NOT a release-data migration.

Do not run against preserved databases: this bypasses outbox and rollup maintenance.
The live correctness pass never invokes this utility.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

TARGET_ORDERS = 100000


def get_tenants():
    conn = psycopg.connect("postgresql://astraroll@127.0.0.1:55433/v2bench_control_78b1ee4a")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, database_name FROM control_tenant_registry ORDER BY tenant_id;"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def populate_tenant(tenant_id, db_name):
    t0 = time.time()
    tid_str = str(tenant_id)
    with psycopg.connect(
        f"postgresql://astraroll@127.0.0.1:55433/{db_name}", autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders;")
            current_orders = cur.fetchone()[0]
            needed = TARGET_ORDERS - current_orders
            if needed <= 0:
                return (tid_str, current_orders, current_orders, time.time() - t0, "already_done")

            # Batch insert orders
            cur.execute(f"""
                INSERT INTO orders
                (id, tenant_id, amount, currency, status, version,
                 created_at, updated_at, product_id)
                SELECT
                    gen_random_uuid(),
                    '{tid_str}'::uuid,
                    (random() * 800 + 10)::numeric(18,2),
                    CASE WHEN random() > 0.5 THEN 'USD' ELSE 'EUR' END,
                    (ARRAY['shipped', 'shipped', 'placed', 'cancelled'])[floor(random() * 4 + 1)],
                    1,
                    NOW() - (random() * interval '30 days'),
                    NOW(),
                    gen_random_uuid()
                FROM generate_series(1, {needed});
            """)

            # Batch insert projections
            cur.execute(f"""
                INSERT INTO order_projections
                (tenant_id, order_id, last_applied_version, canonical_snapshot, updated_at)
                SELECT
                    tenant_id,
                    id,
                    version,
                    json_build_object('id', id, 'tenant_id', tenant_id, 'amount', amount,
                      'currency', currency, 'status', status, 'product_id', product_id)::jsonb,
                    updated_at
                FROM orders
                WHERE tenant_id = '{tid_str}'::uuid
                ON CONFLICT (tenant_id, order_id) DO NOTHING;
            """)

            cur.execute("SELECT count(*) FROM orders;")
            final_cnt = cur.fetchone()[0]
            return (tid_str, current_orders, final_cnt, time.time() - t0, "populated")


def main():
    tenants = get_tenants()
    total_tenants = len(tenants)
    print(f"Discovered {total_tenants} tenants from control registry.")
    print(
        f"Target per tenant: {TARGET_ORDERS:,} orders. "
        f"Total target: {total_tenants * TARGET_ORDERS:,} orders."
    )

    start_all = time.time()
    completed = 0
    total_new_orders = 0

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(populate_tenant, tid, db): (tid, db) for tid, db in tenants}
        for future in as_completed(futures):
            tid, db = futures[future]
            try:
                tid_str, initial, final, elapsed, status = future.result()
                completed += 1
                diff = final - initial
                total_new_orders += diff
                print(
                    f"[{completed:3d}/{total_tenants}] Tenant {tid_str[:8]}... "
                    f"DB {db[:30]}...: {initial:,} -> {final:,} orders "
                    f"(+{diff:,}) in {elapsed:.2f}s ({status})"
                )
            except Exception as e:
                print(f"ERROR on tenant {tid} ({db}): {e}")

    total_time = time.time() - start_all
    print("\n=======================================================")
    print(
        f"POPULATION COMPLETE: {completed}/{total_tenants} tenants processed in {total_time:.1f}s."
    )
    print(f"Total new orders inserted: {total_new_orders:,}")
    print(
        f"Overall database orders: {total_tenants * TARGET_ORDERS:,} rows "
        f"across {total_tenants} databases."
    )
    print("=======================================================")


if __name__ == "__main__":
    main()
