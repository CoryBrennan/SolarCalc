"""Database engine + session wiring. SQLite for now — swapping to Postgres
later is a connection-string + engine-args change, not an app rewrite, since
everything above this file talks to SQLModel sessions, not SQLite directly.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

# DATABASE_URL env var lets deployment mount the SQLite file on a persistent
# volume (e.g. sqlite:////data/solar_calc.db on Fly.io) without touching code.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./solar_calc.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
