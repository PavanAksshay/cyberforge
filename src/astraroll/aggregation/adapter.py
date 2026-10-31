"""Event Adapter for AstraRoll Aggregation Engine.

Translates incoming Redis Streams messages (stream: astraroll:orders:v1, field: event)
into strongly typed domain OrderEvent instances.

Enforces:
- JSON parsing and envelope validation.
- Rejection/quarantining of schema_version != 2 (e.g. V1 or unknown versions).
- Exact Decimal and integer cents conversion for financial accuracy (no floats).
- UUID, currency (3-letter uppercase ISO), and status validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from astraroll.aggregation.contracts import (
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
)


@dataclass(frozen=True)
class AdaptedMessage:
    """Result of parsing and adapting a Redis Streams message."""

    stream_id: str
    stream_name: str
    raw_payload: str
    event: OrderEvent | None = None
    quarantine_reason: str | None = None
    event_id: UUID | None = None
    schema_version: int | None = None
    tenant_id: UUID | None = None
    order_id: UUID | None = None

    @property
    def should_quarantine(self) -> bool:
        return self.quarantine_reason is not None


def _parse_uuid(val: Any) -> UUID | None:
    if val is None:
        return None
    try:
        return UUID(str(val))
    except (ValueError, TypeError):
        return None


def _parse_datetime(val: Any) -> datetime:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=UTC)
        return val.astimezone(UTC)
    if isinstance(val, str):
        # Fromisoformat handles trailing Z in Python 3.11+
        s = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    raise ValueError(f"Cannot parse datetime from {type(val).__name__}: {val}")


def parse_order_snapshot(data: dict[str, Any]) -> OrderSnapshot:
    """Convert raw dictionary snapshot into domain OrderSnapshot."""
    product_id_str = str(data.get("product_id") or "").strip()
    if not product_id_str:
        raise ValueError("product_id is missing or empty")
    # Verify product_id is a valid UUID
    UUID(product_id_str)

    raw_amount = data.get("amount")
    if raw_amount is None:
        raise ValueError("amount is missing")
    if isinstance(raw_amount, float):
        raise ValueError("amount must not be a float")

    try:
        dec_amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid decimal amount: {raw_amount}") from exc

    if not dec_amount.is_finite() or dec_amount.is_nan():
        raise ValueError("amount must be a finite decimal number")

    if dec_amount < 0:
        raise ValueError(f"amount cannot be negative: {dec_amount}")

    # Reject amounts with more than 2 decimal places (e.g. "12.345")
    if dec_amount.as_tuple().exponent < -2:
        raise ValueError(f"amount exceeds 2 decimal places: {raw_amount}")

    if len(dec_amount.as_tuple().digits) > 18:
        raise ValueError("amount exceeds maximum precision of 18 digits")

    cents_val = dec_amount * Decimal(100)
    if cents_val != cents_val.to_integral_value():
        raise ValueError(f"amount has fractional cents: {raw_amount}")

    amount_cents = int(cents_val)

    currency_str = str(data.get("currency") or "").strip().upper()
    if len(currency_str) != 3 or not currency_str.isalpha():
        raise ValueError(f"Invalid currency code: {currency_str}")

    raw_status = str(data.get("status") or "").strip().lower()
    try:
        status = OrderStatus(raw_status)
    except ValueError as exc:
        raise ValueError(f"Invalid order status: {raw_status}") from exc

    created_at = _parse_datetime(data.get("created_at"))
    updated_at = _parse_datetime(data.get("updated_at"))

    order_id = str(data.get("id")) if data.get("id") else None
    tenant_id = str(data.get("tenant_id")) if data.get("tenant_id") else None

    return OrderSnapshot(
        product_id=product_id_str,
        amount_cents=amount_cents,
        currency=currency_str,
        status=status,
        created_at=created_at,
        updated_at=updated_at,
        order_id=order_id,
        tenant_id=tenant_id,
    )


def adapt_stream_message(
    stream_id: str,
    payload_raw: str | bytes,
    stream_name: str = "astraroll:orders:v1",
) -> AdaptedMessage:
    """Parse and validate a Redis Streams event message.

    Returns AdaptedMessage with event if valid, or with quarantine_reason if invalid/unsupported.
    """
    if isinstance(payload_raw, bytes):
        try:
            payload_str = payload_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return AdaptedMessage(
                stream_id=stream_id,
                stream_name=stream_name,
                raw_payload=repr(payload_raw),
                quarantine_reason=f"Unicode decode error: {exc}",
            )
    else:
        payload_str = payload_raw

    try:
        data = json.loads(payload_str)
    except Exception as exc:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason=f"Malformed JSON payload: {exc}",
        )

    if not isinstance(data, dict):
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason="Payload is not a JSON object",
        )

    # Extract metadata fields for quarantine indexing
    event_id = _parse_uuid(data.get("event_id"))
    schema_version = (
        data.get("schema_version") if isinstance(data.get("schema_version"), int) else None
    )
    tenant_id = _parse_uuid(data.get("tenant_id"))
    order_id = _parse_uuid(data.get("order_id"))

    # Envelope validation: must be schema_version 2
    if data.get("schema_version") != 2:
        reason = f"Unsupported schema_version: {data.get('schema_version')} (expected 2)"
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason=reason,
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )

    # Validate essential fields
    if not event_id:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason="Missing or invalid event_id (must be UUID)",
            event_id=None,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )

    if not tenant_id:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason="Missing or invalid tenant_id (must be UUID)",
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=None,
            order_id=order_id,
        )

    if not order_id:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason="Missing or invalid order_id (must be UUID)",
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=None,
        )

    try:
        order_version = int(data.get("order_version", 0))
        if order_version < 1:
            raise ValueError(f"order_version must be >= 1, got {order_version}")
    except (ValueError, TypeError) as exc:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason=f"Invalid order_version: {exc}",
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )

    try:
        occurred_at = _parse_datetime(data.get("occurred_at"))
    except Exception as exc:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason=f"Invalid occurred_at timestamp: {exc}",
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )

    event_type = str(data.get("event_type") or "order.updated")

    after_data = data.get("after")
    if not isinstance(after_data, dict):
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason="Missing or invalid 'after' snapshot object",
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )

    try:
        after_snapshot = parse_order_snapshot(after_data)
    except Exception as exc:
        return AdaptedMessage(
            stream_id=stream_id,
            stream_name=stream_name,
            raw_payload=payload_str,
            quarantine_reason=f"Invalid 'after' snapshot: {exc}",
            event_id=event_id,
            schema_version=schema_version,
            tenant_id=tenant_id,
            order_id=order_id,
        )

    before_snapshot: OrderSnapshot | None = None
    before_data = data.get("before")
    if before_data is not None:
        if not isinstance(before_data, dict):
            return AdaptedMessage(
                stream_id=stream_id,
                stream_name=stream_name,
                raw_payload=payload_str,
                quarantine_reason="Invalid 'before' snapshot: must be an object or null",
                event_id=event_id,
                schema_version=schema_version,
                tenant_id=tenant_id,
                order_id=order_id,
            )
        try:
            before_snapshot = parse_order_snapshot(before_data)
        except Exception as exc:
            return AdaptedMessage(
                stream_id=stream_id,
                stream_name=stream_name,
                raw_payload=payload_str,
                quarantine_reason=f"Invalid 'before' snapshot: {exc}",
                event_id=event_id,
                schema_version=schema_version,
                tenant_id=tenant_id,
                order_id=order_id,
            )

    domain_event = OrderEvent(
        event_id=str(event_id),
        tenant_id=str(tenant_id),
        order_id=str(order_id),
        order_version=order_version,
        event_type=event_type,
        occurred_at=occurred_at,
        snapshot=after_snapshot,
        before_snapshot=before_snapshot,
    )

    return AdaptedMessage(
        stream_id=stream_id,
        stream_name=stream_name,
        raw_payload=payload_str,
        event=domain_event,
        quarantine_reason=None,
        event_id=event_id,
        schema_version=schema_version,
        tenant_id=tenant_id,
        order_id=order_id,
    )
