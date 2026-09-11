"""Add a per-user switch for streaming vs non-streaming research-agent calls."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_llm_stream_mode"
down_revision = "0014_report_template"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    if "llm_stream_mode" not in _columns("user_preferences"):
        # server_default false preserves existing non-streaming behaviour for every user.
        op.add_column(
            "user_preferences",
            sa.Column("llm_stream_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if "llm_stream_mode" in _columns("user_preferences"):
        op.drop_column("user_preferences", "llm_stream_mode")
