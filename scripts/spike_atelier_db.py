"""Создание схемы исключительно в отдельной disposable spike БД."""

from sqlalchemy import create_engine, text

from app import domain_actions, domain_memory, models  # noqa: F401
from app.db import Base
from app.spike_atelier.config import SpikeSettings
from app.spike_atelier.receipts import SpikeReceipt  # noqa: F401


def main():
    config = SpikeSettings()
    engine = create_engine(config.database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(connection)
    engine.dispose()


if __name__ == "__main__":
    main()
