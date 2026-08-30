"""Add article generation progress fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_article_generation_progress"
down_revision = "0003_article_schedule_and_assets"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    columns = {
        "generation_stage": sa.Column("generation_stage", sa.String(length=80), nullable=False, server_default="queued"),
        "generation_progress": sa.Column("generation_progress", sa.Integer(), nullable=False, server_default="0"),
        "generation_error": sa.Column("generation_error", sa.Text(), nullable=True),
    }
    for name, column in columns.items():
        if name not in _columns("articles"):
            op.add_column("articles", column)


def downgrade() -> None:
    for name in ("generation_error", "generation_progress", "generation_stage"):
        if name in _columns("articles"):
            op.drop_column("articles", name)
