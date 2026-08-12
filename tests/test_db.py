from __future__ import annotations

from app.db import _normalize_database_url


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
