"""Database engine + session wiring. Talks Postgres in production (Render
Postgres, via DATABASE_URL) and SQLite for local dev/tests -- everything
above this file talks to SQLModel sessions either way, so which one is
actually in use is decided here and nowhere else.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine


def _normalize_database_url(url: str) -> str:
    """Render (like Heroku before it) hands out Postgres connection strings
    with the legacy `postgres://` scheme, which SQLAlchemy 1.4+ no longer
    accepts at all. Rewriting to `postgresql+psycopg://` both fixes that and
    pins the driver explicitly to psycopg3 (the one in requirements.txt) --
    plain `postgresql://` would default to psycopg2, which isn't installed."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


# Unset (local dev, tests) falls back to a SQLite file; Render injects a
# Postgres connection string here in production via render.yaml's
# `fromDatabase` reference on the web service.
DATABASE_URL = _normalize_database_url(os.environ.get("DATABASE_URL", "sqlite:///./solar_calc.db"))

# check_same_thread is a SQLite-only pysqlite option: FastAPI's
# one-session-per-request pattern hands sessions across threads under a
# threaded server, which pysqlite blocks by default. Postgres drivers have
# no such restriction and reject the kwarg outright if it's passed to them.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
