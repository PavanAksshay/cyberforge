# README and frontend snapshot verification

Date: 1 November 2026. Branch: `codex/astraroll-v2-final-product`.

## Published documentation checklist

- [x] Problem statement mapped to the supplied three-page aggregation-layer PRD.
- [x] Approach, implementation benefits and end-to-end architecture explained.
- [x] All seven mandatory functional requirements mapped to concrete routes/behavior.
- [x] Selected bonuses B-1, B-2 and B-5 identified, with resource/metric design choices stated separately.
- [x] Expanded 10M inventory, SQL correctness and endpoint p50/p95/p99 linked to recorded evidence.
- [x] Earlier 1M profile kept distinct by tenant shape, window and pool budget.
- [x] Five real frontend screenshots: Command Center, Advanced Analytics, CSV Ingestion, Data Health and Assistant.
- [x] Screenshot contents inspected; private key dialogs and secret values excluded.
- [x] Fresh V2 setup, local credentials, provisioning, real migration, bootstrap and profile loading documented.
- [x] API, relay and consumer launch commands provided separately.
- [x] Preserved 100-tenant startup and guarded cold-boot instructions separated from fresh setup.
- [x] Host/container ports, Vite proxy and production frontend build explained.
- [x] Four API examples, order/shipment flow, SQL verification and duplicate-import expectations provided.
- [x] Authentication, key expiry, coverage expiry and troubleshooting documented.
- [x] Local Markdown image/document targets and PowerShell/Python code-block syntax checked.
- [x] No protected source reset, deletion, migration or heavy benchmark executed for this documentation change.

## Executed quick-start validation

The exact embedded README Python bootstrap was extracted and executed with the documented environment override against a new control database on isolated PostgreSQL **55434**. It used the existing hardened provisioner, actual Alembic migrations and bootstrap implementation. A private temporary profile was retained under ignored `.runtime/`.

The generated profile was then used with the real FastAPI application, PostgreSQL and Redis **56381**:

| Check                                      | Observed result                                                    |
| ------------------------------------------ | ------------------------------------------------------------------ |
| Tenant provisioning / migration / coverage | Succeeded                                                          |
| Four empty analytics shapes                | HTTP 200, legitimate empty/zero semantics                          |
| CSV dry run                                | HTTP 200                                                           |
| First 12-order CSV commit                  | Accepted 12                                                        |
| Relay and consumer                         | Actual `astraroll.v2.workers` subprocesses, scoped relay, `--once` |
| First resulting summary                    | 12 orders, USD 506.97 sales                                        |
| Independent verification                   | MATCH                                                              |
| Exact CSV re-upload                        | Zero new orders                                                    |
| Ordinary order creation + shipment         | One extra order, USD 12.34 extra sales                             |
| Final resulting summary                    | 13 orders, USD 519.31 sales                                        |
| Final independent verification             | MATCH                                                              |
| Cross-tenant request                       | HTTP 403                                                           |

Detailed local proof: `.runtime/readme-startup-proof.json`. No raw key/profile or database files are committed.

The V2 Compose configuration passed `config --quiet`. Docker Desktop's Linux engine was stopped during this review; the application flow above used the isolated native services. The README explicitly requires a running Docker engine before its container-start commands.

Five screenshots were captured through the browser from the real port-8000 product. They show synthetic tenant data and actual SQL/worker/assistant state. Their original JPEG bytes are preserved with matching `.jpg` filenames; they are not generated mockups. CSV sample loading for a screenshot did not submit an import into the protected tenant.

Full regression results for this review are recorded in the local `.runtime/readme-regression.log`; the source implementation remains the previously reviewed code. All tests run on isolated services, not port 55433. Cold-boot commands are syntax-checked runbook instructions and were not executed against the running protected services.

## Regression checks

- Backend: **386 passed in 173.34 seconds**, with one existing Starlette/httpx deprecation warning.
- Frontend: **12 tests passed** across four test files; ESLint and TypeScript checks passed.
- Python: Ruff check passed; Ruff format check passed for all **120 tracked Python files**.
- Documentation: local link/image checks, embedded Python compilation, PowerShell parser checks and Markdown formatting checked.
- Compose: V2 configuration validated without starting or restarting protected services.
