"""Small durable counters shared by workers and administrative diagnostics."""

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from astraroll.aggregation.release_models import OperationalCounter


def increment(session, name, value=1):
    stmt = insert(OperationalCounter).values(name=name, value=value)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={"value": OperationalCounter.value + value, "updated_at": datetime.now(UTC)},
        )
    )
