"""Allow same-day report regeneration while deduplicating one schedule minute."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_report_schedule_trigger"
down_revision = "0006_article_validation_repair"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    if "last_trigger_key" not in _columns("report_settings"):
        op.add_column("report_settings", sa.Column("last_trigger_key", sa.String(length=32), nullable=True))


def downgrade() -> None:
    if "last_trigger_key" in _columns("report_settings"):
        op.drop_column("report_settings", "last_trigger_key")
