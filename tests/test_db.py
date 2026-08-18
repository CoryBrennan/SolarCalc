from __future__ import annotations

from sqlalchemy.pool import NullPool

from app.db import _engine_kwargs, _normalize_database_url


def test_normalize_leaves_sqlite_url_untouched():
    assert _normalize_database_url("sqlite:///./solar_calc.db") == "sqlite:///./solar_calc.db"


def test_normalize_rewrites_legacy_postgres_scheme():
    url = "postgres://user:pw@host.render.com/dbname"
    assert _normalize_database_url(url) == "postgresql+psycopg://user:pw@host.render.com/dbname"


def test_normalize_pins_psycopg_driver_on_plain_postgresql_scheme():
    url = "postgresql://user:pw@host.render.com/dbname"
    assert _normalize_database_url(url) == "postgresql+psycopg://user:pw@host.render.com/dbname"


def test_normalize_leaves_already_explicit_driver_untouched():
    url = "postgresql+psycopg://user:pw@host.render.com/dbname"
    assert _normalize_database_url(url) == url


# --- engine options per backend -------------------------------------------


def test_sqlite_gets_check_same_thread_and_no_pool_tuning():
    kwargs = _engine_kwargs("sqlite:///./solar_calc.db")
    assert kwargs == {"connect_args": {"check_same_thread": False}}


def test_supabase_session_pooler_keeps_client_pool_with_pre_ping():
    url = _normalize_database_url(
        "postgresql://postgres.abcdef:pw@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
    )
    kwargs = _engine_kwargs(url)
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 300
    assert "poolclass" not in kwargs
    # Session mode is a real Postgres session, so prepared statements are fine.
    assert "prepare_threshold" not in kwargs["connect_args"]


def test_supabase_transaction_pooler_disables_prepared_statements_and_client_pool():
    url = _normalize_database_url(
        "postgresql://postgres.abcdef:pw@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
    )
    kwargs = _engine_kwargs(url)
    # Must be None, not 0. psycopg3 reads 0 as "prepare on the first execution"
    # -- prepared statements at their most aggressive, which is what transaction
    # mode cannot route. Only None disables them. `prepareThreshold=0` is the
    # JDBC spelling of "off" and is backwards here; if this assertion is failing
    # because someone followed that advice, the fix is to put None back.
    assert kwargs["connect_args"]["prepare_threshold"] is None, (
        "prepare_threshold must be None (disabled); 0 means prepare-immediately in psycopg3"
    )
    assert kwargs["poolclass"] is NullPool
    # The pooler is the pool -- a second client-side one just holds
    # connections open against the project's limit.
    assert "pool_pre_ping" not in kwargs
    assert "pool_recycle" not in kwargs


def test_supabase_host_forces_sslmode_require():
    url = _normalize_database_url("postgresql://postgres:pw@db.abcdef.supabase.co:5432/postgres")
    assert _engine_kwargs(url)["connect_args"]["sslmode"] == "require"


def test_sslmode_already_in_url_is_not_overridden():
    url = _normalize_database_url(
        "postgresql://postgres.abcdef:pw@aws-1-us-east-1.pooler.supabase.com:5432/postgres?sslmode=verify-full"
    )
    assert "sslmode" not in _engine_kwargs(url)["connect_args"]


def test_non_supabase_postgres_does_not_get_tls_forced_on_it():
    """A local or self-hosted Postgres with no TLS listener would refuse the
    connection outright if sslmode=require were applied blindly."""
    url = _normalize_database_url("postgresql://user:pw@localhost:5432/solar")
    assert _engine_kwargs(url)["connect_args"] == {}


def test_dedicated_pooler_on_project_host_is_also_transaction_mode():
    """The paid-tier dedicated pooler runs transaction mode on port 6543 of the
    project host (`db.<ref>.supabase.co`), not the shared pooler host — so mode
    has to be read off the port, not the hostname."""
    url = _normalize_database_url("postgresql://postgres:pw@db.abcdef.supabase.co:6543/postgres")
    kwargs = _engine_kwargs(url)
    assert kwargs["connect_args"]["prepare_threshold"] is None
    assert kwargs["poolclass"] is NullPool


def test_legacy_postgres_scheme_from_supabase_pooler_string_still_gets_pooler_options():
    """The dashboard hands out pooler strings as `postgres://`, so scheme
    normalization has to happen before the host/port are inspected."""
    kwargs = _engine_kwargs(
        _normalize_database_url("postgres://postgres.abcdef:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres")
    )
    assert kwargs["connect_args"]["prepare_threshold"] is None
    assert kwargs["connect_args"]["sslmode"] == "require"


def test_normalize_strips_whitespace_from_a_pasted_url():
    """This value is always pasted by hand — into a Render env-var field or a
    local file — so a trailing newline rides along. psycopg reports that as an
    unresolvable host, which points the investigation at DNS, not the paste."""
    url = "  postgres://postgres.abcdef:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres\n"
    assert (
        _normalize_database_url(url)
        == "postgresql+psycopg://postgres.abcdef:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
    )
