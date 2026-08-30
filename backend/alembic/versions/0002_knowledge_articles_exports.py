"""Add durable export progress and scenario/article content tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_knowledge_articles_exports"
down_revision = "0001_postgres_langgraph"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    existing = _columns(table)
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add_columns("export_jobs", [
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(length=120), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    ])
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "article_topics" not in tables:
        op.create_table(
            "article_topics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("domain", sa.String(length=80), nullable=False, server_default="semiconductor"),
            sa.Column("customer_role", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("pain_point", sa.Text(), nullable=False, server_default=""),
            sa.Column("business_context", sa.Text(), nullable=False, server_default=""),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("source_ref", sa.Text(), nullable=True),
            sa.Column("priority_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("novelty_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="qualified"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_article_topics_user_id", "article_topics", ["user_id"])
        op.create_index("ix_article_topics_status", "article_topics", ["status"])
    if "articles" not in tables:
        op.create_table(
            "articles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("topic_id", sa.Integer(), sa.ForeignKey("article_topics.id"), nullable=True),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("subtitle", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("content_markdown", sa.Text(), nullable=False, server_default=""),
            sa.Column("content_html", sa.Text(), nullable=False, server_default=""),
            sa.Column("validation_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("metrics_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ai_tone_score", sa.Float(), nullable=True),
            sa.Column("factual_score", sa.Float(), nullable=True),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_articles_user_id", "articles", ["user_id"])
        op.create_index("ix_articles_topic_id", "articles", ["topic_id"])
        op.create_index("ix_articles_status", "articles", ["status"])
    if "article_assets" not in tables:
        op.create_table(
            "article_assets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
            sa.Column("asset_type", sa.String(length=32), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("mime_type", sa.String(length=120), nullable=False, server_default="image/png"),
            sa.Column("caption", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("source_type", sa.String(length=32), nullable=False, server_default="generated"),
            sa.Column("source_ref", sa.Text(), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_article_assets_article_id", "article_assets", ["article_id"])
    if "article_revisions" not in tables:
        op.create_table(
            "article_revisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=False),
            sa.Column("content_markdown", sa.Text(), nullable=False),
            sa.Column("editor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("revision_note", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_article_revisions_article_id", "article_revisions", ["article_id"])
    if "article_settings" not in tables:
        op.create_table(
            "article_settings",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("frequency", sa.String(length=20), nullable=False, server_default="daily"),
            sa.Column("generate_time", sa.String(length=5), nullable=False, server_default="18:00"),
            sa.Column("timezone", sa.String(length=80), nullable=False, server_default="Asia/Shanghai"),
            sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auto_visuals", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_generated_date", sa.String(length=10), nullable=True),
        )


def downgrade() -> None:
    for table in ("article_settings", "article_revisions", "article_assets", "articles", "article_topics"):
        op.drop_table(table)
    for name in ("completed_at", "started_at", "attempt_count", "heartbeat_at", "worker_id", "processed_files", "total_files", "progress"):
        if name in _columns("export_jobs"):
            op.drop_column("export_jobs", name)
