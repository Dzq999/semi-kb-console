"""QaConversation source + external_chat_id (企微群 @机器人问答归属)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_qa_conversation_source"
down_revision = "0008_qa_conversations"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    columns = _columns("qa_conversations")
    if "qa_conversations" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if "source" not in columns:
        op.add_column("qa_conversations", sa.Column("source", sa.String(length=16), nullable=False, server_default="web"))
        op.create_index("ix_qa_conversations_source", "qa_conversations", ["source"])
    if "external_chat_id" not in columns:
        op.add_column("qa_conversations", sa.Column("external_chat_id", sa.String(length=120), nullable=True))
        op.create_index("ix_qa_conversations_external_chat_id", "qa_conversations", ["external_chat_id"])


def downgrade() -> None:
    columns = _columns("qa_conversations")
    if "external_chat_id" in columns:
        op.drop_index("ix_qa_conversations_external_chat_id", table_name="qa_conversations")
        op.drop_column("qa_conversations", "external_chat_id")
    if "source" in columns:
        op.drop_index("ix_qa_conversations_source", table_name="qa_conversations")
        op.drop_column("qa_conversations", "source")
