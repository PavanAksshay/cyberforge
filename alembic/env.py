from alembic import context
from astraroll import models  # noqa: F401
from astraroll.aggregation import models as aggregation_models  # noqa: F401
from astraroll.aggregation import release_models  # noqa: F401
from astraroll.config import get_settings
from astraroll.db import Base, get_engine

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name.startswith("control_"):
        return False
    return True


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()
else:

    def run(connection):
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()

    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        run(supplied_connection)
    else:
        with get_engine().connect() as connection:
            run(connection)
