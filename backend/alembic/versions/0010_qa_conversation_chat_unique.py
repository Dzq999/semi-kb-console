"""QaConversation 同群唯一约束 + 合并历史重复会话（企微群并发消息去重）。

背景：wecom 群消息由 SDK 事件并发派发，旧逻辑建会话只 flush 不 commit，commit 在 ~40s 后
answer_question 才发生，导致同群近乎同时到达的消息各自建了重复会话。此处先把已有重复合并
（保留最早一条，其余会话的消息改挂到它、按时间仍然有序，再删空壳），再加唯一约束根治。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_qa_conversation_chat_unique"
down_revision = "0009_qa_conversation_source"
branch_labels = None
depends_on = None

_UQ_NAME = "uq_qa_conversations_user_source_chat"
_UQ_COLS = ("user_id", "source", "external_chat_id")


def _has_target_uc(bind) -> bool:
    """检测目标唯一约束是否已存在（测试库由 create_all 直接按 models.py 建表，
    约束已内建；真实 Postgres 走增量迁移，此前 0009 尚未加，故不存在）。"""
    target = set(_UQ_COLS)
    for uc in sa.inspect(bind).get_unique_constraints("qa_conversations"):
        if set(uc.get("column_names") or []) == target:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    if "qa_conversations" not in sa.inspect(bind).get_table_names():
        return
    # 1) 合并已有重复：同 (user_id, source, external_chat_id) 且 external_chat_id 非空的组，
    #    保留 created_at 最早(并列取 id 最小)那条作为 keeper。
    dupes = bind.execute(sa.text(
        """
        SELECT user_id, source, external_chat_id
        FROM qa_conversations
        WHERE external_chat_id IS NOT NULL
        GROUP BY user_id, source, external_chat_id
        HAVING COUNT(*) > 1
        """
    )).fetchall()
    for user_id, source, chat_id in dupes:
        rows = bind.execute(sa.text(
            """
            SELECT id FROM qa_conversations
            WHERE user_id = :u AND source = :s AND external_chat_id = :c
            ORDER BY created_at ASC, id ASC
            """
        ), {"u": user_id, "s": source, "c": chat_id}).fetchall()
        keeper = rows[0][0]
        losers = [r[0] for r in rows[1:]]
        for loser in losers:
            bind.execute(sa.text(
                "UPDATE qa_messages SET conversation_id = :k WHERE conversation_id = :l"
            ), {"k": keeper, "l": loser})
            bind.execute(sa.text(
                "DELETE FROM qa_conversations WHERE id = :l"
            ), {"l": loser})
    # 2) 加唯一约束（NULL 互不冲突，故 web 会话不受影响）。
    #    幂等：约束已在（测试库 create_all 已内建）则跳过。
    if _has_target_uc(bind):
        return
    if bind.dialect.name == "sqlite":
        # SQLite 不支持 ALTER TABLE ADD CONSTRAINT，走 batch 复制迁移。
        with op.batch_alter_table("qa_conversations") as batch:
            batch.create_unique_constraint(_UQ_NAME, list(_UQ_COLS))
    else:
        op.create_unique_constraint(_UQ_NAME, "qa_conversations", list(_UQ_COLS))


def downgrade() -> None:
    bind = op.get_bind()
    if "qa_conversations" not in sa.inspect(bind).get_table_names():
        return
    if not _has_target_uc(bind):
        return
    if bind.dialect.name == "sqlite":
        # SQLite 反射回来的批量建约束 name 为 None，无法按名 drop；
        # 改为反射整表、剔除目标唯一约束后按新定义重建（copy-and-move）。
        reflected = sa.Table("qa_conversations", sa.MetaData(), autoload_with=bind)
        target = set(_UQ_COLS)
        for c in list(reflected.constraints):
            if isinstance(c, sa.UniqueConstraint) and {col.name for col in c.columns} == target:
                reflected.constraints.discard(c)
        with op.batch_alter_table("qa_conversations", copy_from=reflected, recreate="always"):
            pass
    else:
        op.drop_constraint(_UQ_NAME, "qa_conversations", type_="unique")
