"""Track automatic article validation repairs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_article_validation_repair"
down_revision = "0005_wechat_draft_delivery"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    if "repair_attempts" not in _columns("articles"):
        op.add_column("articles", sa.Column("repair_attempts", sa.Integer(), nullable=False, server_default="0"))
    if "auto_repair" not in _columns("article_settings"):
        op.add_column("article_settings", sa.Column("auto_repair", sa.Boolean(), nullable=False, server_default=sa.true()))
    if "max_repair_attempts" not in _columns("article_settings"):
        op.add_column("article_settings", sa.Column("max_repair_attempts", sa.Integer(), nullable=False, server_default="3"))


def downgrade() -> None:
    if "max_repair_attempts" in _columns("article_settings"):
        op.drop_column("article_settings", "max_repair_attempts")
    if "auto_repair" in _columns("article_settings"):
        op.drop_column("article_settings", "auto_repair")
    if "repair_attempts" in _columns("articles"):
        op.drop_column("articles", "repair_attempts")
