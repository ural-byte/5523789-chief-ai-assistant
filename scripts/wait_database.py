"""Bounded startup wait; connection exceptions may contain secrets and are not logged."""

import time

from sqlalchemy import create_engine, text

from app.config import settings


def main():
    engine = create_engine(settings().database_url, connect_args={"connect_timeout": 5})
    for _ in range(60):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return 0
        except Exception:
            time.sleep(2)
    print("Database startup timed out")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
