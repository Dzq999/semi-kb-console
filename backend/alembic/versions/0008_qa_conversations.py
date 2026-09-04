"""Q&A conversations/messages + per-user LLM endpoint overrides."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_qa_conversations"
down_revision = "0007_report_schedule_trigger"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    tables = _tables()
    if "qa_conversations" not in tables:
        op.create_table(
            "qa_conversations",
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("title", sa.String(length=240), nullable=False, server_default="新会话"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "qa_messages" not in tables:
        op.create_table(
            "qa_messages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("conversation_id", sa.String(length=40), sa.ForeignKey("qa_conversations.id"), nullable=False, index=True),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("citations_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("model_id", sa.String(length=160), nullable=True),
            sa.Column("grounded", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, index=True),
        )
    pref_columns = _columns("user_preferences")
    for name in ("llm_base_url", "model_catalog_url"):
        if name not in pref_columns:
            op.add_column("user_preferences", sa.Column(name, sa.String(length=300), nullable=True))
    if "llm_api_style" not in pref_columns:
        op.add_column("user_preferences", sa.Column("llm_api_style", sa.String(length=16), nullable=True))


def downgrade() -> None:
    tables = _tables()
    if "qa_messages" in tables:
        op.drop_table("qa_messages")
    if "qa_conversations" in tables:
        op.drop_table("qa_conversations")
    pref_columns = _columns("user_preferences")
    for name in ("llm_api_style", "model_catalog_url", "llm_base_url"):
        if name in pref_columns:
            op.drop_column("user_preferences", name)
