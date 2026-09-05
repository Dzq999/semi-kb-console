"""企微群 @机器人 → 智能问答 桥接层。

把一条群消息接到既有严格接地问答链路(answer_question)，答案回群 + 落进机器人 owner 的
问答历史(QaConversation/QaMessage)，前端因此可见。

安全边界（核心）：
- 只调用 answer_question——其 build_grounding_context 只喂经营模型/仿真/vFab 来源族
  (restricted 仅标题)/本体术语，从不读代码、配置、.env。群机器人自动继承这套边界与 NDA
  防护，不新开任何会读文件的路径。
- 无 API Key / 无默认模型 / 作答异常 → 回群一句友好提示，绝不泄漏内部错误细节/堆栈。
- 每条消息自开 SessionLocal（收消息在后台任务里，请求级会话不可用），仿 qa 流式作答的写库方式。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import SessionLocal
from ..models import EncryptedCredential, QaConversation, User, UserPreference
from ..security import decrypt_secret
from .llm import ExternalServiceError
from .qa import answer_question
from .wecom_aibot import IncomingMessage

logger = logging.getLogger("wecom_qa_bridge")

_BOT_ID_KIND = "wecom_aibot_id"
_BOT_SECRET_KIND = "wecom_aibot_secret"

# 企微群路径收紧的生成上限：单次成型、生成期间不回中间内容，长答案叠加首调超时重试会让
# 企微单发路径的生成上限。真正卡「只见三个点」的根因是看门狗超龄换连掐了作答连接（已在
# wecom_aibot._inflight 修复），这两项如今只负责「控最坏单发等待时长」，不再承担正确性职责。
# 取值依据实测：覆盖类回答约 2000~2600 output token（一度顶满旧上限 3000），单发耗时 ~110~166s。
# 故 max_tokens 放到 4000 留足表格余量；timeout 放到 180s——让一次正常生成能在单次尝试内跑完，
# 重试只兜真·网络抖动，不再因 60s 过短而重复跑整段慢生成（那反而把等待放大）。仍是单发、非流式。
_WECOM_MAX_TOKENS = 4000
_WECOM_TIMEOUT_SECONDS = 180

# 归属用户缺配置时回群的友好提示（不含任何内部细节）。
_MSG_NO_API_KEY = "机器人暂未配置可用的模型访问凭据，请联系管理员在控制台完成配置后再试。"
_MSG_NO_MODEL = "机器人暂未设置默认问答模型，请联系管理员在控制台选择后再试。"
_MSG_INTERNAL = "抱歉，刚才处理这个问题时出了点状况，请稍后再试或换个问法。"
_MSG_EMPTY = "没太理解这个问题，能再具体描述一下吗？"


def bot_credentials(db, user_id: int) -> tuple[str, str] | None:
    """读机器人 owner 的 Bot ID + Secret（加密存储）。缺任一返回 None。绝不日志明文。"""
    rows = {
        row.kind: row.ciphertext
        for row in db.scalars(
            select(EncryptedCredential).where(
                EncryptedCredential.user_id == user_id,
                EncryptedCredential.kind.in_((_BOT_ID_KIND, _BOT_SECRET_KIND)),
            )
        ).all()
    }
    if _BOT_ID_KIND not in rows or _BOT_SECRET_KIND not in rows:
        return None
    try:
        return decrypt_secret(rows[_BOT_ID_KIND]), decrypt_secret(rows[_BOT_SECRET_KIND])
    except Exception:  # noqa: BLE001 解密失败按未配置处理
        logger.warning("WeCom AI bot credential decrypt failed for user %s", user_id)
        return None


def find_bot_owner_id(db) -> int | None:
    """定位机器人 owner：已配置 wecom_aibot 凭据的用户。多用户配置时取 id 最小者（主用户）。"""
    row = db.scalar(
        select(EncryptedCredential.user_id)
        .where(EncryptedCredential.kind == _BOT_SECRET_KIND)
        .order_by(EncryptedCredential.user_id.asc())
        .limit(1)
    )
    return int(row) if row is not None else None


def _conversation_for_chat(db, user_id: int, chat_id: str) -> QaConversation:
    """按群 chatid 幂等定位/新建会话（归 owner 名下，source=wecom）。

    并发防重：新建后**立即 commit**，让同群并发消息(SDK 事件并发派发，各自开 SessionLocal)
    能马上看到这条会话，而不是等到 ~40s 后 answer_question 才提交——那正是之前产生重复会话
    的窗口。若仍撞上毫秒级并发(唯一约束 user_id+source+external_chat_id 拦下)，回滚后重查复用。
    """
    stmt = select(QaConversation).where(
        QaConversation.user_id == user_id,
        QaConversation.source == "wecom",
        QaConversation.external_chat_id == chat_id,
    )
    conversation = db.scalar(stmt)
    if conversation is not None:
        return conversation
    conversation = QaConversation(
        id=uuid.uuid4().hex[:16],
        user_id=user_id,
        title="企微群问答",
        source="wecom",
        external_chat_id=chat_id,
    )
    db.add(conversation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # 并发对手已建同一会话：复用它，不再重复
        conversation = db.scalar(stmt)
        if conversation is None:  # 极端情况(约束因别的原因触发)：兜底重抛
            raise
    return conversation


async def handle_group_message(owner_user_id: int, message: IncomingMessage) -> str | None:
    """处理一条群 @机器人 消息，返回要回群的文本（None 表示不回复）。

    自开 SessionLocal；作答复用 answer_question（严格接地 + 落库）。任何失败都降级为
    友好提示，绝不外泄内部细节。
    """
    question = (message.text or "").strip()
    if not question:
        return _MSG_EMPTY
    with SessionLocal() as db:
        owner = db.get(User, owner_user_id)
        if owner is None:
            logger.warning("WeCom AI bot owner user %s not found", owner_user_id)
            return _MSG_INTERNAL
        preference = db.get(UserPreference, owner_user_id)
        model_id = preference.default_model_id if preference else None
        if not model_id:
            return _MSG_NO_MODEL
        conversation = _conversation_for_chat(db, owner_user_id, message.chat_id)
        try:
            answer = await answer_question(
                db, owner, conversation, question, model_id,
                max_tokens=_WECOM_MAX_TOKENS, timeout_seconds=_WECOM_TIMEOUT_SECONDS,
            )
        except ExternalServiceError as exc:
            # 无 API Key 是最常见的 ExternalServiceError；区分提示，其余按内部错误。
            detail = str(exc)
            logger.warning("WeCom AI bot answer failed: %s", detail)
            return _MSG_NO_API_KEY if "API Key" in detail else _MSG_INTERNAL
        except Exception as exc:  # noqa: BLE001
            logger.exception("WeCom AI bot answer unexpected error: %s", exc)
            return _MSG_INTERNAL
        return answer.content or _MSG_INTERNAL
