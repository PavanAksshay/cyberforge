from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from astraroll.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    url = make_url(get_settings().database_url)
    options = url.query.get("options", "")
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": f"{options} -c timezone=UTC"},
    )


@lru_cache
def get_session_factory():
    return sessionmaker(get_engine(), expire_on_commit=False)
