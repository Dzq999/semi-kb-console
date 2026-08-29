"""Baseline current schema and add durable LangGraph control columns."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.db import Base
from app import models  # noqa: F401


revision = "0001_postgres_langgraph"
down_revision = None
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_missing(table: str, columns: list[sa.Column]) -> None:
    existing = _columns(table)
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())
    _add_missing("runs", [
        # Rows predating the graph migration must never be resumed as if they had checkpoints.
        sa.Column("orchestrator_engine", sa.String(24), nullable=False, server_default="legacy"),
        sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stop_after_round", sa.Integer(), nullable=True),
        sa.Column("checkpoint_thread_id", sa.String(160), nullable=True),
        sa.Column("worker_id", sa.String(120), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    ])
    _add_missing("run_rounds", [
        sa.Column("checkpoint_id", sa.String(160), nullable=True),
        sa.Column("resumed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("node_attempts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("quarantined_files_json", sa.Text(), nullable=False, server_default="[]"),
    ])
    _add_missing("agent_iterations", [
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkpoint_id", sa.String(160), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
    ])


def downgrade() -> None:
    for table, names in {
        "agent_iterations": ["input_hash", "checkpoint_id", "attempt_count"],
        "run_rounds": ["quarantined_files_json", "node_attempts_json", "resumed_count", "checkpoint_id"],
        "runs": ["recovery_count", "heartbeat_at", "worker_id", "checkpoint_thread_id", "stop_after_round", "cancel_requested", "pause_requested", "orchestrator_engine"],
    }.items():
        existing = _columns(table)
        for name in names:
            if name in existing:
                op.drop_column(table, name)
