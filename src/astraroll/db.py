from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from astraroll.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


@lru_cache
def get_session_factory():
    return sessionmaker(get_engine(), expire_on_commit=False)
