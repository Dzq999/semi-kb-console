from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import psycopg
from psycopg import sql
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _create_historical_sqlite(path: Path) -> None:
    """Create a representative pre-LangGraph database with non-trivial IDs."""
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("username", String(80), nullable=False),
        Column("password_hash", String(256), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    preferences = Table(
        "user_preferences",
        metadata,
        Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
        Column("default_model_id", String(160)),
        Column("default_agent_count", Integer, nullable=False),
        Column("timezone", String(80), nullable=False),
    )
    credentials = Table(
        "encrypted_credentials",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
        Column("kind", String(40), nullable=False),
        Column("ciphertext", Text, nullable=False),
        Column("masked_hint", String(120), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    runs = Table(
        "runs",
        metadata,
        Column("id", String(40), primary_key=True),
        Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
        Column("model_id", String(160), nullable=False),
        Column("status", String(32), nullable=False),
        Column("current_stage", String(64), nullable=False),
        Column("progress", Float, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
        Column("error", Text),
        Column("config_json", Text, nullable=False),
        Column("metrics_before_json", Text, nullable=False),
        Column("metrics_after_json", Text, nullable=False),
    )
    agents = Table(
        "agent_runs",
        metadata,
        Column("id", String(48), primary_key=True),
        Column("run_id", String(40), ForeignKey("runs.id"), nullable=False),
        Column("name", String(120), nullable=False),
        Column("role", String(120), nullable=False),
        Column("domain", String(80), nullable=False),
        Column("objective", Text, nullable=False),
        Column("source_mode", String(24), nullable=False),
        Column("model_id", String(160), nullable=False),
        Column("status", String(32), nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
        Column("duration_seconds", Float),
        Column("output_json", Text, nullable=False),
        Column("error", Text),
    )
    rounds = Table(
        "run_rounds",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("run_id", String(40), ForeignKey("runs.id"), nullable=False),
        Column("round_number", Integer, nullable=False),
        Column("status", String(32), nullable=False),
        Column("current_stage", String(64), nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
        Column("duration_seconds", Float),
        Column("metrics_before_json", Text, nullable=False),
        Column("metrics_after_json", Text, nullable=False),
        Column("validation_json", Text, nullable=False),
        Column("artifacts_json", Text, nullable=False),
        Column("error", Text),
    )
    iterations = Table(
        "agent_iterations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("run_id", String(40), ForeignKey("runs.id"), nullable=False),
        Column("agent_id", String(48), ForeignKey("agent_runs.id"), nullable=False),
        Column("round_number", Integer, nullable=False),
        Column("status", String(32), nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
        Column("duration_seconds", Float),
        Column("evidence_json", Text, nullable=False),
        Column("output_json", Text, nullable=False),
        Column("candidate_files_json", Text, nullable=False),
        Column("error", Text),
    )
    events = Table(
        "run_events",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("run_id", String(40), ForeignKey("runs.id"), nullable=False),
        Column("event_type", String(40), nullable=False),
        Column("level", String(16), nullable=False),
        Column("message", Text, nullable=False),
        Column("payload_json", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(users.insert(), {"id": 41, "username": "legacy", "password_hash": "hash", "created_at": now})
        connection.execute(preferences.insert(), {"user_id": 41, "default_model_id": "legacy-model", "default_agent_count": 6, "timezone": "Asia/Shanghai"})
        connection.execute(credentials.insert(), {"id": 42, "user_id": 41, "kind": "llm_api_key", "ciphertext": "encrypted", "masked_hint": "configured", "updated_at": now})
        connection.execute(runs.insert(), {"id": "legacy-run", "user_id": 41, "model_id": "legacy-model", "status": "completed", "current_stage": "completed", "progress": 100.0, "created_at": now, "started_at": now, "completed_at": now, "config_json": "{}", "metrics_before_json": "{}", "metrics_after_json": "{}"})
        connection.execute(agents.insert(), {"id": "legacy-agent", "run_id": "legacy-run", "name": "Legacy Agent", "role": "ontology", "domain": "fab", "objective": "migrate", "source_mode": "model", "model_id": "legacy-model", "status": "completed", "started_at": now, "completed_at": now, "duration_seconds": 1.0, "output_json": "{}"})
        connection.execute(rounds.insert(), {"id": 43, "run_id": "legacy-run", "round_number": 1, "status": "completed", "current_stage": "completed", "started_at": now, "completed_at": now, "duration_seconds": 1.0, "metrics_before_json": "{}", "metrics_after_json": "{}", "validation_json": "{}", "artifacts_json": "{}"})
        connection.execute(iterations.insert(), {"id": 44, "run_id": "legacy-run", "agent_id": "legacy-agent", "round_number": 1, "status": "completed", "started_at": now, "completed_at": now, "duration_seconds": 1.0, "evidence_json": "[]", "output_json": "{}", "candidate_files_json": "[]"})
        connection.execute(events.insert(), {"id": 45, "run_id": "legacy-run", "event_type": "completed", "level": "info", "message": "legacy", "payload_json": "{}", "created_at": now})
    engine.dispose()


def _run_migration(source: Path, database_url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "scripts.migrate_sqlite_to_postgres", "--source", str(source)],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> None:
    password = os.getenv("SEMI_KB_DB_PASSWORD")
    if not password:
        raise SystemExit("SEMI_KB_DB_PASSWORD is required")
    admin_password = os.getenv("SEMI_KB_POSTGRES_ADMIN_PASSWORD", password)
    host = os.getenv("SEMI_KB_DB_HOST", "127.0.0.1")
    port = int(os.getenv("SEMI_KB_DB_PORT", "5432"))
    app_user = os.getenv("SEMI_KB_DB_USER", "semi_kb_console")
    admin_user = os.getenv("SEMI_KB_POSTGRES_ADMIN_USER", "postgres")
    database_name = f"semi_kb_migration_verify_{uuid.uuid4().hex[:10]}"
    database_url = (
        f"postgresql+psycopg://{quote_plus(app_user)}:{quote_plus(password)}"
        f"@{host}:{port}/{database_name}"
    )

    admin_connection = psycopg.connect(
        host=host,
        port=port,
        dbname="postgres",
        user=admin_user,
        password=admin_password,
        autocommit=True,
    )
    try:
        admin_connection.execute(
            sql.SQL("create database {} owner {}").format(
                sql.Identifier(database_name),
                sql.Identifier(app_user),
            )
        )
        with tempfile.TemporaryDirectory(prefix="semi-kb-migration-") as directory:
            source = Path(directory) / "legacy.db"
            _create_historical_sqlite(source)
            result = _run_migration(source, database_url)
            if result.returncode != 0:
                raise RuntimeError(f"migration failed:\n{result.stdout}\n{result.stderr}")
            if "migrated_rows=8" not in result.stdout:
                raise AssertionError(f"unexpected migration summary: {result.stdout.strip()}")

            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    counts = {
                        table: connection.scalar(text(f'select count(*) from "{table}"'))
                        for table in (
                            "users",
                            "user_preferences",
                            "encrypted_credentials",
                            "runs",
                            "agent_runs",
                            "run_rounds",
                            "agent_iterations",
                            "run_events",
                        )
                    }
                    if set(counts.values()) != {1}:
                        raise AssertionError(f"row counts were not preserved: {counts}")
                    run_state = connection.execute(
                        text(
                            "select orchestrator_engine, pause_requested, cancel_requested, recovery_count "
                            "from runs where id='legacy-run'"
                        )
                    ).one()
                    if tuple(run_state) != ("legacy", False, False, 0):
                        raise AssertionError(f"legacy defaults are unsafe: {tuple(run_state)}")
                    next_user_id = connection.scalar(
                        text(
                            "insert into users (username, password_hash, created_at) "
                            "values ('sequence-check', 'hash', :created_at) returning id"
                        ),
                        {"created_at": datetime.now(timezone.utc)},
                    )
                    if next_user_id != 42:
                        raise AssertionError(f"users sequence was not reset: next id={next_user_id}")
            finally:
                engine.dispose()

            repeated = _run_migration(source, database_url)
            if repeated.returncode == 0 or "Target PostgreSQL is not empty" not in repeated.stderr + repeated.stdout:
                raise AssertionError("migration did not refuse a populated target")

        print("historical_rows=8")
        print("legacy_engine=preserved")
        print("sequence_reset=passed")
        print("nonempty_target_guard=passed")
    finally:
        admin_connection.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s and pid <> pg_backend_pid()",
            (database_name,),
        )
        admin_connection.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))
        admin_connection.close()


if __name__ == "__main__":
    main()
