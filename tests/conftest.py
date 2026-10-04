import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import domain_actions, domain_memory, models  # noqa: F401
from app.config import Settings, settings
from app.db import Base


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Tests must never read the developer's live credentials or provider defaults."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    settings.cache_clear()
    yield
    settings.cache_clear()


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
