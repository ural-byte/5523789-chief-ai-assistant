from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def session_factory():
    engine = create_engine(settings().database_url, pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)
