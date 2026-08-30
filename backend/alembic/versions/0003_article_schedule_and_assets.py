"""Add scheduled batch article and image-generation settings."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_article_schedule_and_assets"
down_revision = "0002_knowledge_articles_exports"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    article_columns = {
        "generation_date": sa.Column("generation_date", sa.String(length=10), nullable=True),
        "sequence_no": sa.Column("sequence_no", sa.Integer(), nullable=True),
        "deleted_at": sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        "article_model_id": sa.Column("article_model_id", sa.String(length=160), nullable=True),
        "image_model_id": sa.Column("image_model_id", sa.String(length=160), nullable=True),
        "cover_prompt": sa.Column("cover_prompt", sa.Text(), nullable=True),
    }
    for name, column in article_columns.items():
        if name not in _columns("articles"):
            op.add_column("articles", column)
    settings_columns = {
        "daily_article_count": sa.Column("daily_article_count", sa.Integer(), nullable=False, server_default="1"),
        "article_model_id": sa.Column("article_model_id", sa.String(length=160), nullable=True),
        "image_model_id": sa.Column("image_model_id", sa.String(length=160), nullable=True),
        "image_count": sa.Column("image_count", sa.Integer(), nullable=False, server_default="1"),
    }
    for name, column in settings_columns.items():
        if name not in _columns("article_settings"):
            op.add_column("article_settings", column)
    inspector = sa.inspect(op.get_bind())
    if "ix_articles_generation_date" not in {index["name"] for index in inspector.get_indexes("articles")}:
        op.create_index("ix_articles_generation_date", "articles", ["generation_date"])


def downgrade() -> None:
    for name in ("image_count", "image_model_id", "article_model_id", "daily_article_count"):
        if name in _columns("article_settings"):
            op.drop_column("article_settings", name)
    if "ix_articles_generation_date" in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("articles")}:
        op.drop_index("ix_articles_generation_date", table_name="articles")
    for name in ("cover_prompt", "image_model_id", "article_model_id", "deleted_at", "sequence_no", "generation_date"):
        if name in _columns("articles"):
            op.drop_column("articles", name)
