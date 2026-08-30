"""Add WeChat draft delivery state to articles."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_wechat_draft_delivery"
down_revision = "0004_article_generation_progress"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    columns = {
        "wechat_status": sa.Column("wechat_status", sa.String(length=24), nullable=False, server_default="not_sent"),
        "wechat_draft_media_id": sa.Column("wechat_draft_media_id", sa.String(length=200), nullable=True),
        "wechat_last_error": sa.Column("wechat_last_error", sa.Text(), nullable=True),
        "wechat_sent_at": sa.Column("wechat_sent_at", sa.DateTime(timezone=True), nullable=True),
        "wechat_response_json": sa.Column("wechat_response_json", sa.Text(), nullable=False, server_default="{}"),
    }
    for name, column in columns.items():
        if name not in _columns("articles"):
            op.add_column("articles", column)


def downgrade() -> None:
    for name in ("wechat_response_json", "wechat_sent_at", "wechat_last_error", "wechat_draft_media_id", "wechat_status"):
        if name in _columns("articles"):
            op.drop_column("articles", name)
