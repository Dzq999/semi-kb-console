from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from app.config import PROJECT_ROOT, settings
from app.db import Base
from app import models  # noqa: F401
from app.migrations import upgrade_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate an existing SEMI-KB Console SQLite database to configured PostgreSQL.")
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "data" / "console.db")
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"SQLite source does not exist: {args.source}")
    if not settings.database_url.startswith("postgresql"):
        raise SystemExit("Configure PostgreSQL before migration")
    source = create_engine(f"sqlite:///{args.source.as_posix()}")
    target = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        upgrade_database()
        with target.connect() as connection:
            populated = [
                table.name
                for table in Base.metadata.sorted_tables
                if connection.scalar(select(1).select_from(table).limit(1)) is not None
            ]
            if populated:
                names = ", ".join(populated)
                raise SystemExit(f"Target PostgreSQL is not empty ({names}); migration aborted")

        source_tables = set(inspect(source).get_table_names())
        migrated = 0
        migrated_tables: set[str] = set()
        source_metadata = MetaData()
        with source.connect() as source_connection, target.begin() as target_connection:
            for table in Base.metadata.sorted_tables:
                if table.name not in source_tables:
                    continue
                source_table = Table(table.name, source_metadata, autoload_with=source)
                target_columns = {column.name for column in table.columns}
                rows = [
                    {key: value for key, value in dict(row).items() if key in target_columns}
                    for row in source_connection.execute(select(source_table)).mappings()
                ]
                # Rows created before LangGraph existed must never be resumed as graph runs.
                if table.name == "runs" and "orchestrator_engine" not in source_table.c:
                    for row in rows:
                        row["orchestrator_engine"] = "legacy"
                if rows:
                    target_connection.execute(table.insert(), rows)
                    migrated += len(rows)
                    migrated_tables.add(table.name)

            if target.dialect.name == "postgresql":
                for table in Base.metadata.sorted_tables:
                    if table.name not in migrated_tables:
                        continue
                    integer_pk = next(
                        (column for column in table.primary_key.columns if str(column.type).startswith("INTEGER")),
                        None,
                    )
                    if integer_pk is None:
                        continue
                    sequence_name = target_connection.scalar(
                        text("select pg_get_serial_sequence(:table, :column)"),
                        {"table": table.name, "column": integer_pk.name},
                    )
                    if not sequence_name:
                        continue
                    maximum = target_connection.scalar(select(integer_pk).order_by(integer_pk.desc()).limit(1))
                    target_connection.execute(
                        text("select setval(cast(:sequence as regclass), :value, :is_called)"),
                        {
                            "sequence": sequence_name,
                            "value": max(maximum or 1, 1),
                            "is_called": maximum is not None,
                        },
                    )
        print(f"migrated_rows={migrated}")
    finally:
        source.dispose()
        target.dispose()


if __name__ == "__main__":
    main()
