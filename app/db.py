"""Database engine + session wiring. Talks Postgres in production (Supabase,
via DATABASE_URL) and SQLite for local dev/tests -- everything above this file
talks to SQLModel sessions either way, so which one is actually in use is
decided here and nowhere else.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine

# Supabase serves Postgres under two host families: the shared pooler
# (`aws-<n>-<region>.pooler.supabase.com`) and the project host
# (`db.<project-ref>.supabase.co`), which carries both direct connections and
# -- on paid tiers -- a dedicated pooler.
_SUPABASE_HOST_SUFFIXES = (".supabase.co", ".supabase.com")

# Transaction pooling mode is identified by port, not by host: it's reachable
# both at the shared pooler and at the project host's dedicated pooler. Mode
# matters because it hands a different backend connection to every
# transaction, so anything outliving one transaction -- prepared statements,
# session-level SET, server-side cursors -- breaks. Session mode (5432) keeps
# one backend per client connection and behaves like real Postgres. See
# _engine_kwargs for what each mode forces.
_TRANSACTION_POOLER_PORT = 6543


def _normalize_database_url(url: str) -> str:
    """Strips surrounding whitespace, then fixes the scheme.

    The whitespace matters because this value is always pasted by hand -- into
    a Render env-var field or a local file -- and a trailing newline or space
    survives that trip. psycopg reports the result as an unresolvable host,
    which sends you looking at DNS instead of at the paste.

    Managed Postgres providers hand out connection strings with the legacy
    `postgres://` scheme (Supabase, like Render and Heroku before it, still
    does in places), which SQLAlchemy 1.4+ no longer accepts at all. Rewriting
    to `postgresql+psycopg://` both fixes that and pins the driver explicitly
    to psycopg3 (the one in requirements.txt) -- plain `postgresql://` would
    default to psycopg2, which isn't installed."""
    url = _clean_pasted_url(url)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _engine_kwargs(url: str) -> dict[str, Any]:
    """create_engine() options that depend on which backend the URL points at.

    Split out from the create_engine call so the pooler-mode reasoning below
    is testable without opening a connection."""
    if url.startswith("sqlite"):
        # check_same_thread is a SQLite-only pysqlite option: FastAPI's
        # one-session-per-request pattern hands sessions across threads under
        # a threaded server, which pysqlite blocks by default. Postgres
        # drivers have no such restriction and reject the kwarg outright if
        # it's passed to them.
        return {"connect_args": {"check_same_thread": False}}

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = parse_qs(parts.query)
    connect_args: dict[str, Any] = {}

    # psycopg defaults to sslmode=prefer, which silently falls back to
    # plaintext if the TLS handshake fails -- not what you want for a
    # connection crossing the public internet from Render to Supabase. Only
    # forced for Supabase hosts so a local/self-hosted Postgres without TLS
    # still connects, and never overrides an sslmode already in the URL.
    is_supabase = host.endswith(_SUPABASE_HOST_SUFFIXES)
    if is_supabase and "sslmode" not in query:
        connect_args["sslmode"] = "require"

    kwargs: dict[str, Any] = {
        # Supabase's pooler drops idle client connections, and a free-tier
        # project pauses outright after a week of inactivity. Without
        # pre-ping the first request after either one fails on a dead
        # connection handed out of SQLAlchemy's pool instead of reconnecting.
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    if is_supabase and parts.port == _TRANSACTION_POOLER_PORT:
        # Transaction mode: no prepared statements (psycopg3 creates them
        # automatically after a few executions of the same query, which
        # PgBouncer then can't route), and no client-side connection pool --
        # the pooler is already the pool, so a second one just holds
        # connections open against the project's limit.
        #
        # `None` is the value that disables them, and the distinction matters:
        # in psycopg3, `prepare_threshold=0` means "prepare on the *first*
        # execution" -- the most aggressive setting there is, i.e. the exact
        # opposite of what this branch is for. Only `None` turns the feature
        # off. Guidance written for JDBC says `prepareThreshold=0` to disable
        # (including Supabase's own conn-prepared-statements skill note), which
        # is correct for JDBC and backwards here. Don't "fix" this to 0.
        connect_args["prepare_threshold"] = None
        kwargs["poolclass"] = NullPool
        kwargs.pop("pool_pre_ping")
        kwargs.pop("pool_recycle")

    kwargs["connect_args"] = connect_args
    return kwargs


_SQLITE_FALLBACK = "sqlite:///./solar_calc.db"

_log = logging.getLogger(__name__)


def _clean_pasted_url(raw: str) -> str:
    """Undo the two paste artifacts that turn a correct connection string into
    an unparseable one.

    Both come from copying the right value from the wrong place: the dotenv
    line (`DATABASE_URL=postgres://...`) instead of just its value, and a
    string that arrived wrapped in quotes. SQLAlchemy rejects either with the
    same `Could not parse SQLAlchemy URL` message it gives for a blank value or
    a psql command line, so the error alone never says which mistake it was."""
    url = raw.strip()
    if url.upper().startswith("DATABASE_URL="):
        url = url[len("DATABASE_URL=") :].strip()
    if len(url) >= 2 and url[0] == url[-1] and url[0] in "\"'":
        url = url[1:-1].strip()
    return url


def _resolve_database_url(raw: str | None) -> str:
    """Turn the raw env var into a URL SQLAlchemy will accept, or fail with an
    error that names the actual problem."""
    if raw is None or not raw.strip():
        # Blank is treated as unset -- an env var set to "" should behave no
        # worse than an absent one. Warned about loudly rather than passed over
        # in silence, because in a deployed environment it means the service is
        # about to write real data to a container-local file that the next
        # restart throws away.
        if raw is not None:
            _log.warning(
                "DATABASE_URL is set but empty -- falling back to local SQLite (%s). "
                "Data written here does NOT survive a restart or redeploy.",
                _SQLITE_FALLBACK,
            )
        return _SQLITE_FALLBACK

    url = _normalize_database_url(raw)
    try:
        make_url(url)
    except ArgumentError as exc:
        raise RuntimeError(
            "DATABASE_URL is set but is not a connection URL SQLAlchemy can parse. "
            "It should look like "
            "postgresql://postgres.<project-ref>:<password>@aws-<n>-<region>"
            ".pooler.supabase.com:5432/postgres -- the value only, not the "
            "'DATABASE_URL=' prefix, not a psql command line, and not just the "
            f"password. Got {len(url)} characters starting {url[:12]!r}."
        ) from exc
    return url


# Unset (local dev, tests) falls back to a SQLite file; in production this is
# the Supabase session-pooler connection string, set as an env var on the web
# service (see render.yaml and the deploy notes in README.md).
DATABASE_URL = _resolve_database_url(os.environ.get("DATABASE_URL"))

engine = create_engine(DATABASE_URL, **_engine_kwargs(DATABASE_URL))


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
