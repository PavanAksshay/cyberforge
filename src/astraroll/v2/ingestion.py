"""CSV Order Ingestion Service for AstraRoll V2.

Provides validated, transactional ingestion of orders into a tenant's dedicated
PostgreSQL database and transactional outbox.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from astraroll.locking import tenant_lock
from astraroll.models import Order
from astraroll.orders import record_event, require_tenant
from astraroll.schemas import OrderRead

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD"}
ALLOWED_STATUSES = {"placed", "shipped", "cancelled"}


@dataclass
class RowError:
    line_number: int
    column: str
    message: str
    raw_value: str = ""


@dataclass
class ParsedOrderRow:
    line_number: int
    order_id: UUID
    product_id: UUID
    raw_product_id: str
    amount: Decimal
    currency: str
    status: str
    created_at: datetime


def resolve_product_uuid(raw_id: str) -> UUID:
    """Deterministically convert raw product SKU/identifier into UUID."""
    try:
        return UUID(raw_id)
    except ValueError:
        return uuid5(NAMESPACE_DNS, f"product:{raw_id}")


@dataclass
class IngestionResult:
    total_rows: int
    accepted: int
    rejected: int
    duplicates: int
    errors: list[dict]
    preview_rows: list[dict]
    imported_order_ids: list[str]
    total_sales: str
    total_amount: str
    dry_run: bool


def parse_iso_datetime(dt_str: str) -> datetime | None:
    """Safely parse ISO 8601 UTC timestamp."""
    try:
        # Handle 'Z' suffix
        cleaned = dt_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt
    except Exception:
        return None


def validate_and_parse_csv(
    content: str,
    max_rows: int = 10000,
) -> tuple[list[ParsedOrderRow], list[RowError]]:
    """Validate CSV header and parse rows into typed structures."""
    errors: list[RowError] = []
    parsed_rows: list[ParsedOrderRow] = []

    f = io.StringIO(content.strip())
    reader = csv.reader(f)

    try:
        header = next(reader, None)
    except Exception as exc:
        errors.append(RowError(line_number=1, column="header", message=f"CSV parse error: {exc}"))
        return [], errors

    if not header:
        errors.append(RowError(line_number=1, column="header", message="Empty CSV content"))
        return [], errors

    # Clean headers (strip whitespace and lower)
    header_clean = [col.strip().lower() for col in header]
    col_map: dict[str, int] = {name: idx for idx, name in enumerate(header_clean)}

    # Verify required columns
    required_cols = ["product_id", "amount", "currency"]
    missing = [rc for rc in required_cols if rc not in col_map]
    if missing:
        errors.append(
            RowError(
                line_number=1,
                column="header",
                message=(
                    f"Missing required columns: {', '.join(missing)}. "
                    f"Found: {', '.join(header_clean)}"
                ),
            )
        )
        return [], errors

    line_num = 1
    for row in reader:
        line_num += 1
        if not row or all(c.strip() == "" for c in row):
            continue  # skip blank lines

        if len(parsed_rows) >= max_rows:
            errors.append(
                RowError(
                    line_number=line_num,
                    column="file",
                    message=f"Row limit exceeded. Maximum allowable rows is {max_rows}.",
                )
            )
            break

        row_errors: list[RowError] = []

        # Product ID
        prod_idx = col_map["product_id"]
        product_id = row[prod_idx].strip() if prod_idx < len(row) else ""
        if not product_id or len(product_id) > 100:
            row_errors.append(
                RowError(
                    line_number=line_num,
                    column="product_id",
                    message="product_id must be between 1 and 100 characters",
                    raw_value=product_id,
                )
            )

        # Amount
        amt_idx = col_map["amount"]
        amt_raw = row[amt_idx].strip() if amt_idx < len(row) else ""
        try:
            amount = Decimal(amt_raw)
            if amount <= 0:
                row_errors.append(
                    RowError(
                        line_number=line_num,
                        column="amount",
                        message="amount must be strictly positive",
                        raw_value=amt_raw,
                    )
                )
        except (InvalidOperation, ValueError):
            row_errors.append(
                RowError(
                    line_number=line_num,
                    column="amount",
                    message=f"Invalid decimal amount: '{amt_raw}'",
                    raw_value=amt_raw,
                )
            )
            amount = Decimal(0)

        # Currency
        curr_idx = col_map["currency"]
        currency = row[curr_idx].strip().upper() if curr_idx < len(row) else ""
        if currency not in ALLOWED_CURRENCIES:
            row_errors.append(
                RowError(
                    line_number=line_num,
                    column="currency",
                    message=(
                        f"Unsupported currency '{currency}'. "
                        f"Must be one of: {sorted(ALLOWED_CURRENCIES)}"
                    ),
                    raw_value=currency,
                )
            )

        # Optional Order ID
        order_id: UUID | None = None
        if "order_id" in col_map:
            oid_idx = col_map["order_id"]
            oid_raw = row[oid_idx].strip() if oid_idx < len(row) else ""
            if oid_raw:
                try:
                    order_id = UUID(oid_raw)
                except ValueError:
                    row_errors.append(
                        RowError(
                            line_number=line_num,
                            column="order_id",
                            message=f"Invalid UUID for order_id: '{oid_raw}'",
                            raw_value=oid_raw,
                        )
                    )
        if order_id is None:
            order_id = uuid4()

        # Optional Status
        status = "placed"
        if "status" in col_map:
            st_idx = col_map["status"]
            st_raw = row[st_idx].strip().lower() if st_idx < len(row) else ""
            if st_raw:
                if st_raw not in ALLOWED_STATUSES:
                    row_errors.append(
                        RowError(
                            line_number=line_num,
                            column="status",
                            message=(
                                f"Invalid status '{st_raw}'. Allowed: {sorted(ALLOWED_STATUSES)}"
                            ),
                            raw_value=st_raw,
                        )
                    )
                else:
                    status = st_raw

        # Optional Created At
        created_at = datetime.now(UTC)
        if "created_at" in col_map:
            dt_idx = col_map["created_at"]
            dt_raw = row[dt_idx].strip() if dt_idx < len(row) else ""
            if dt_raw:
                parsed_dt = parse_iso_datetime(dt_raw)
                if parsed_dt is None:
                    row_errors.append(
                        RowError(
                            line_number=line_num,
                            column="created_at",
                            message=f"Invalid ISO 8601 UTC timestamp: '{dt_raw}'",
                            raw_value=dt_raw,
                        )
                    )
                else:
                    created_at = parsed_dt

        if row_errors:
            errors.extend(row_errors)
        else:
            parsed_rows.append(
                ParsedOrderRow(
                    line_number=line_num,
                    order_id=order_id,
                    product_id=resolve_product_uuid(product_id),
                    raw_product_id=product_id,
                    amount=amount,
                    currency=currency,
                    status=status,
                    created_at=created_at,
                )
            )

    return parsed_rows, errors


def ingest_csv_data(
    session: Session,
    tenant_id: UUID,
    content: str,
    dry_run: bool = False,
    max_rows: int = 10000,
) -> IngestionResult:
    """Execute validated transactional CSV ingestion for tenant.

    Guarantees:
    - Atomically writes orders and outbox events into the tenant database.
    - Idempotent against duplicate order IDs.
    - Zero writes when dry_run=True.
    - Accurately tracks shipped sales vs total amount.
    """
    parsed_rows, errors = validate_and_parse_csv(content, max_rows=max_rows)

    # Prepare preview of first 10 rows
    preview = [
        {
            "line_number": r.line_number,
            "order_id": str(r.order_id),
            "product_id": str(r.product_id),
            "raw_product_id": r.raw_product_id,
            "amount": str(r.amount),
            "currency": r.currency,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in parsed_rows[:10]
    ]

    total_shipped_sales = Decimal("0.00")
    total_batch_amount = Decimal("0.00")
    for r in parsed_rows:
        total_batch_amount += r.amount
        if r.status == "shipped":
            total_shipped_sales += r.amount

    if dry_run or not parsed_rows:
        return IngestionResult(
            total_rows=len(parsed_rows) + len(errors),
            accepted=len(parsed_rows),
            rejected=len(errors),
            duplicates=0,
            errors=[asdict(e) for e in errors],
            preview_rows=preview,
            imported_order_ids=[str(r.order_id) for r in parsed_rows[:50]],
            total_sales=str(total_shipped_sales),
            total_amount=str(total_batch_amount),
            dry_run=True,
        )

    # Live commit path
    tenant_lock(session, tenant_id)
    require_tenant(session, tenant_id)

    # Check for existing duplicate order IDs
    candidate_ids = [r.order_id for r in parsed_rows]
    existing_ids = set(
        session.scalars(
            select(Order.id).where(Order.tenant_id == tenant_id, Order.id.in_(candidate_ids))
        ).all()
    )

    imported_ids: list[str] = []
    duplicates_count = 0
    accepted_count = 0
    actual_shipped_sales = Decimal("0.00")
    actual_batch_amount = Decimal("0.00")

    for r in parsed_rows:
        if r.order_id in existing_ids:
            duplicates_count += 1
            continue

        # Create Order
        version = 1
        initial_status = "placed"
        order = Order(
            id=r.order_id,
            tenant_id=tenant_id,
            product_id=r.product_id,
            amount=r.amount,
            currency=r.currency,
            status=initial_status,
            version=version,
            created_at=r.created_at,
            updated_at=r.created_at,
        )
        session.add(order)
        record_event(session, order, "order.created", None)

        # If CSV specified status is shipped or cancelled, transition order
        if r.status in ("shipped", "cancelled"):
            before = OrderRead.model_validate(order)
            order.status = r.status
            order.version += 1
            order.updated_at = r.created_at
            event_type = "order.cancelled" if r.status == "cancelled" else "order.updated"
            record_event(session, order, event_type, before)

        imported_ids.append(str(r.order_id))
        accepted_count += 1
        actual_batch_amount += r.amount
        if r.status == "shipped":
            actual_shipped_sales += r.amount

    session.flush()

    return IngestionResult(
        total_rows=len(parsed_rows) + len(errors),
        accepted=accepted_count,
        rejected=len(errors),
        duplicates=duplicates_count,
        errors=[asdict(e) for e in errors],
        preview_rows=preview,
        imported_order_ids=imported_ids[:50],
        total_sales=str(actual_shipped_sales),
        total_amount=str(actual_batch_amount),
        dry_run=False,
    )
