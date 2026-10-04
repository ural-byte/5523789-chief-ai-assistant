import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import domain_actions, models  # noqa: F401
from app.db import Base


@pytest.fixture
def sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL must point to a disposable PostgreSQL database")
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("Refusing to clear a database without _test suffix")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()
