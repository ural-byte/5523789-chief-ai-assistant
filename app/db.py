from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def session_factory():
    return make_sessions(settings().database_url)


def make_sessions(database_url):
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 5,
            "options": (
                "-c lock_timeout=2000 -c statement_timeout=5000 "
                "-c idle_in_transaction_session_timeout=10000"
            ),
        },
    )
    return sessionmaker(engine, expire_on_commit=False)
