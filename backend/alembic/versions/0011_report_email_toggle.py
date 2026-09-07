"""Add an explicit on/off switch for daily-report email reminders."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_report_email_toggle"
down_revision = "0010_qa_conversation_chat_unique"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    if "email_reminder_enabled" not in _columns("report_settings"):
        # server_default true keeps reminders firing for existing users (current behaviour).
        op.add_column(
            "report_settings",
            sa.Column("email_reminder_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    if "email_reminder_enabled" in _columns("report_settings"):
        op.drop_column("report_settings", "email_reminder_enabled")
