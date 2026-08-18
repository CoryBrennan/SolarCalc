"""One-shot copy of a local SQLite solar_calc.db into a Postgres database
(Supabase, or any other), so moving off the ephemeral in-container SQLite file
doesn't mean starting from an empty catalog.

    python -m scripts.migrate_sqlite_to_postgres \
        --source sqlite:///./solar_calc.db \
        --target "postgresql://postgres.<ref>:<pw>@aws-1-<region>.pooler.supabase.com:5432/postgres"

`--target` defaults to $DATABASE_URL. Both URLs go through the same
normalization the app uses, so a bare `postgres://` string pasted from a
dashboard works.

Table-agnostic on purpose: it walks SQLModel.metadata in FK-dependency order
rather than naming tables, so it stays correct as models are added. Tables that
already hold rows in the target are skipped unless --force is passed, which
makes a re-run after a partial failure safe.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import Boolean, Table, create_engine, func, insert, inspect, select
from sqlmodel import SQLModel

# Imported for the side effect of registering every table on SQLModel.metadata
# -- without these the metadata is empty and the copy silently does nothing.
from app import catalog_models, db_models  # noqa: F401
from app.db import _engine_kwargs, _normalize_database_url

# Rows per INSERT. Photos and plan-set PDFs are bytea columns in the hundreds
# of KB to hundreds of MB, so a whole-table insert can blow past the wire
# limits; batching keeps each statement bounded.
_BATCH = 200


def _coerce(table: Table, rows: list[dict]) -> list[dict]:
    """SQLite has no boolean type -- it stores 0/1 integers, and psycopg3 binds
    them to Postgres as integers, which a `boolean` column rejects outright.
    Every other column type in these models (str, float, bytes, datetime)
    round-trips through SQLAlchemy's type system unchanged."""
    bool_cols = [c.name for c in table.columns if isinstance(c.type, Boolean)]
    if not bool_cols:
        return rows
    for row in rows:
        for name in bool_cols:
            if row[name] is not None:
                row[name] = bool(row[name])
    return rows


def migrate(source_url: str, target_url: str, *, force: bool = False) -> int:
    source_url = _normalize_database_url(source_url)
    target_url = _normalize_database_url(target_url)
    source = create_engine(source_url, **_engine_kwargs(source_url))
    target = create_engine(target_url, **_engine_kwargs(target_url))

    SQLModel.metadata.create_all(target)

    copied_total = 0
    with source.connect() as src, target.begin() as dst:
        source_tables = set(SQLModel.metadata.tables) & set(inspect(source).get_table_names())
        # sorted_tables is topologically ordered by foreign key, so parents
        # are always inserted before the rows that reference them.
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in source_tables:
                print(f"  {table.name}: not present in source, skipped")
                continue

            existing = dst.execute(select(func.count()).select_from(table)).scalar_one()
            if existing and not force:
                print(f"  {table.name}: target already has {existing} row(s), skipped (--force to add anyway)")
                continue

            # An older SQLite file can lag the models: SQLModel's create_all
            # only ever CREATEs, never ALTERs, so a column added to a model
            # after the table first existed is missing from the file. Select
            # the intersection and let the target's own default fill the rest,
            # rather than aborting the whole copy on one absent column.
            present = {c["name"] for c in inspect(source).get_columns(table.name)}
            cols = [c for c in table.columns if c.name in present]
            missing = [c.name for c in table.columns if c.name not in present]

            rows = [dict(r) for r in src.execute(select(*cols)).mappings()]
            if not rows:
                print(f"  {table.name}: empty")
                continue
            if missing:
                print(f"  {table.name}: source is missing {', '.join(missing)} -- defaulted")

            _coerce(table, rows)
            for start in range(0, len(rows), _BATCH):
                dst.execute(insert(table), rows[start : start + _BATCH])
            copied_total += len(rows)
            print(f"  {table.name}: {len(rows)} row(s)")

    return copied_total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="sqlite:///./solar_calc.db")
    parser.add_argument("--target", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument(
        "--force",
        action="store_true",
        help="copy into tables that already have rows (may violate PK uniqueness)",
    )
    args = parser.parse_args()

    if not args.target:
        parser.error("no --target and no DATABASE_URL in the environment")
    if args.target.startswith("sqlite"):
        parser.error("--target is a SQLite URL; point it at the Postgres database")

    print(f"source: {args.source}")
    # Hosts are safe to print; the password in the URL userinfo is not.
    print(f"target: {args.target.split('@')[-1]}")
    total = migrate(args.source, args.target, force=args.force)
    print(f"done -- {total} row(s) copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
