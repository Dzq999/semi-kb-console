"""企微智能机器人：桥接层 + 适配层长连接客户端测试。

严格离线——不连真企微 wss、不调真 LLM。桥接测试 monkeypatch llm_service.complete
返回罐装 JSON；适配测试用内存 FakeTransport 驱动收发与重连逻辑。断言：
- 群 @机器人 → answer_question 全链路 → 回复文本；
- 同群 chatid 幂等复用同一会话；
- 无 API Key / 无默认模型 / 内部异常 一律降级为友好提示，绝不外泄细节；
- 长连接断线自动重连、连续失败触发告警、SDK 未接入优雅退出、单条处理异常隔离。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import SessionLocal
from app.models import EncryptedCredential, QaConversation, QaMessage, User, UserPreference
from app.security import encrypt_secret, hash_password
from app.services import wecom_qa_bridge
from app.services.llm import ExternalServiceError, llm_service
from app.services.wecom_aibot import (
    IncomingMessage,
    WecomAiBotClient,
    WecomAiBotNotWired,
)


def _seed_owner(*, with_api_key: bool = True, with_model: bool = True, with_bot: bool = True) -> int:
    """建机器人 owner：用户 + 偏好(默认模型) + 凭据(API Key / bot 凭据)。返回 user_id。"""
    with SessionLocal() as db:
        user = User(username="owner", password_hash=hash_password("strong-password"))
        db.add(user)
        db.flush()
        pref = UserPreference(user_id=user.id, default_model_id="gpt-test" if with_model else None)
        db.add(pref)
        if with_api_key:
            db.add(EncryptedCredential(user_id=user.id, kind="llm_api_key", ciphertext=encrypt_secret("sk-test"), masked_hint="sk••••st"))
        if with_bot:
            db.add(EncryptedCredential(user_id=user.id, kind="wecom_aibot_id", ciphertext=encrypt_secret("botid-123"), masked_hint="bo••••23"))
            db.add(EncryptedCredential(user_id=user.id, kind="wecom_aibot_secret", ciphertext=encrypt_secret("secret-xyz"), masked_hint="se••••yz"))
        db.commit()
        return user.id


def _msg(text: str, chat_id: str = "chat-1") -> IncomingMessage:
    return IncomingMessage(chat_id=chat_id, from_user="u-1", text=text, msg_id="m-1", msg_type="text", raw={})


# ----------------------------- 桥接层：凭据与 owner 定位 -----------------------------

def test_find_bot_owner_and_credentials_roundtrip():
    owner_id = _seed_owner()
    with SessionLocal() as db:
        assert wecom_qa_bridge.find_bot_owner_id(db) == owner_id
        creds = wecom_qa_bridge.bot_credentials(db, owner_id)
    assert creds == ("botid-123", "secret-xyz")


def test_find_bot_owner_none_when_unconfigured():
    _seed_owner(with_bot=False)
    with SessionLocal() as db:
        assert wecom_qa_bridge.find_bot_owner_id(db) is None


def test_bot_credentials_none_when_incomplete():
    """只配了 secret、缺 id → 视为未配置。"""
    with SessionLocal() as db:
        user = User(username="owner", password_hash=hash_password("strong-password"))
        db.add(user)
        db.flush()
        db.add(EncryptedCredential(user_id=user.id, kind="wecom_aibot_secret", ciphertext=encrypt_secret("secret-xyz"), masked_hint="se••••yz"))
        db.commit()
        uid = user.id
        assert wecom_qa_bridge.bot_credentials(db, uid) is None


# ----------------------------- 桥接层：群消息 → 作答全链路 -----------------------------

def _canned_complete(answer: str = "根据经营模型，单位成本约 300 CNY/unit。"):
    async def fake_complete(api_key, model_id, system, user, *args, **kwargs):
        return json.dumps({"answer": answer, "citations": [], "grounded": True}, ensure_ascii=False)
    return fake_complete


def test_handle_group_message_full_chain_persists(monkeypatch):
    owner_id = _seed_owner()
    monkeypatch.setattr(llm_service, "complete", _canned_complete())

    reply = asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("成本预测怎么算？")))
    assert reply is not None and "单位成本" in reply

    # 落库：归 owner、source=wecom、绑定 chatid，且含 user+assistant 两条消息。
    with SessionLocal() as db:
        conv = db.scalar(select_conv())
        assert conv is not None
        assert conv.user_id == owner_id and conv.source == "wecom" and conv.external_chat_id == "chat-1"
        roles = [m.role for m in db.scalars(msgs_for(conv.id)).all()]
        assert roles == ["user", "assistant"]


def test_handle_group_message_bounds_generation_for_wecom(monkeypatch):
    """企微群路径必须收紧 max_tokens/timeout_seconds（单次成型、无中间反馈，防群里空转两三分钟）。"""
    owner_id = _seed_owner()
    captured = {}

    async def capturing_complete(api_key, model_id, system, user, *args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"answer": "ok", "citations": [], "grounded": True}, ensure_ascii=False)

    monkeypatch.setattr(llm_service, "complete", capturing_complete)
    asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("成本？")))
    assert captured.get("max_tokens") == wecom_qa_bridge._WECOM_MAX_TOKENS
    assert captured.get("timeout_seconds") == wecom_qa_bridge._WECOM_TIMEOUT_SECONDS
    # max_tokens 须显式收紧于全局默认（16384），否则群里超长答案会失控。timeout 只要求显式传值
    # （非 None），不再钉上限——真根因是看门狗掐连接（已修），timeout 现仅控单发最坏等待，可放宽。
    assert wecom_qa_bridge._WECOM_MAX_TOKENS < 16384
    assert isinstance(wecom_qa_bridge._WECOM_TIMEOUT_SECONDS, int)
    assert wecom_qa_bridge._WECOM_TIMEOUT_SECONDS > 0


def test_handle_group_message_same_chat_reuses_conversation(monkeypatch):
    owner_id = _seed_owner()
    monkeypatch.setattr(llm_service, "complete", _canned_complete())

    asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("第一问", chat_id="chat-A")))
    asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("第二问", chat_id="chat-A")))

    with SessionLocal() as db:
        convs = db.scalars(all_convs()).all()
        assert len(convs) == 1  # 幂等：同群复用
        msg_count = db.scalar(count_msgs(convs[0].id))
        assert msg_count == 4  # 两轮各 user+assistant


def test_handle_group_message_distinct_chats_split_conversations(monkeypatch):
    owner_id = _seed_owner()
    monkeypatch.setattr(llm_service, "complete", _canned_complete())

    asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("q", chat_id="chat-A")))
    asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("q", chat_id="chat-B")))

    with SessionLocal() as db:
        assert len(db.scalars(all_convs()).all()) == 2


def test_handle_group_message_empty_question_returns_prompt():
    owner_id = _seed_owner()
    reply = asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("   ")))
    assert reply == wecom_qa_bridge._MSG_EMPTY
    # 空问不落库。
    with SessionLocal() as db:
        assert db.scalars(all_convs()).all() == []


def test_handle_group_message_no_api_key_degrades(monkeypatch):
    """作答层抛「未配置 API Key」类 ExternalServiceError → 桥接映射为无凭据友好提示。

    离线断言：monkeypatch complete 抛该错，绝不发真实网络请求。
    """
    owner_id = _seed_owner()

    async def no_key(*args, **kwargs):
        raise ExternalServiceError("未配置模型 API Key，无法回答")

    monkeypatch.setattr(llm_service, "complete", no_key)
    reply = asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("成本？")))
    assert reply == wecom_qa_bridge._MSG_NO_API_KEY


def test_handle_group_message_no_model_degrades():
    owner_id = _seed_owner(with_model=False)
    reply = asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("成本？")))
    assert reply == wecom_qa_bridge._MSG_NO_MODEL


def test_handle_group_message_internal_error_not_leaked(monkeypatch):
    owner_id = _seed_owner()

    async def boom(*args, **kwargs):
        raise RuntimeError("数据库连接串 postgres://secret@host 泄露风险")

    monkeypatch.setattr(llm_service, "complete", boom)
    reply = asyncio.run(wecom_qa_bridge.handle_group_message(owner_id, _msg("成本？")))
    # 只回友好提示，绝不外泄异常细节。
    assert reply == wecom_qa_bridge._MSG_INTERNAL
    assert "postgres" not in reply and "secret" not in reply


def test_handle_group_message_missing_owner_degrades():
    reply = asyncio.run(wecom_qa_bridge.handle_group_message(999999, _msg("成本？")))
    assert reply == wecom_qa_bridge._MSG_INTERNAL


# ----------------------------- 并发去重：同群唯一会话 -----------------------------

def test_conversation_for_chat_commits_immediately_and_reuses():
    """新建会话立即提交（不再等 answer_question），第二次调用复用同一条，绝不产生重复。"""
    owner_id = _seed_owner()
    with SessionLocal() as db1:
        c1 = wecom_qa_bridge._conversation_for_chat(db1, owner_id, "chat-Z")
    # 独立会话应能立刻看到已提交的会话（模拟并发消息各自开 SessionLocal）。
    with SessionLocal() as db2:
        seen = db2.scalar(all_convs())
        assert seen is not None and seen.id == c1.id
        c2 = wecom_qa_bridge._conversation_for_chat(db2, owner_id, "chat-Z")
        assert c2.id == c1.id
    with SessionLocal() as db3:
        assert len(db3.scalars(all_convs()).all()) == 1  # 全程仅一条


def test_qa_conversation_unique_constraint_blocks_duplicate_wecom_chat():
    """DB 唯一约束(user_id+source+external_chat_id)真的拦下同群重复；web(NULL)不受限。"""
    from sqlalchemy.exc import IntegrityError

    owner_id = _seed_owner()
    with SessionLocal() as db:
        db.add(QaConversation(id="c-dup-1", user_id=owner_id, source="wecom", external_chat_id="chat-DUP"))
        db.commit()
    # 同 (owner, wecom, chat-DUP) 第二条应被约束拒绝。
    with SessionLocal() as db:
        db.add(QaConversation(id="c-dup-2", user_id=owner_id, source="wecom", external_chat_id="chat-DUP"))
        with pytest.raises(IntegrityError):
            db.commit()
    # 两条 web 会话(external_chat_id=NULL)互不冲突。
    with SessionLocal() as db:
        db.add(QaConversation(id="c-web-1", user_id=owner_id, source="web", external_chat_id=None))
        db.add(QaConversation(id="c-web-2", user_id=owner_id, source="web", external_chat_id=None))
        db.commit()
        webs = db.scalars(
            __import__("sqlalchemy").select(QaConversation).where(QaConversation.source == "web")
        ).all()
        assert len(webs) == 2


# ----------------------------- SQL 小工具（避免测试里重复 import） -----------------------------

def select_conv():
    from sqlalchemy import select
    return select(QaConversation).where(QaConversation.source == "wecom").limit(1)


def all_convs():
    from sqlalchemy import select
    return select(QaConversation).where(QaConversation.source == "wecom")


def msgs_for(conv_id: str):
    from sqlalchemy import select
    return select(QaMessage).where(QaMessage.conversation_id == conv_id).order_by(QaMessage.id.asc())


def count_msgs(conv_id: str):
    from sqlalchemy import func, select
    return select(func.count()).select_from(QaMessage).where(QaMessage.conversation_id == conv_id)


# ----------------------------- 适配层：长连接运行器（包官方 SDK 事件模型） -----------------------------

from app.services.wecom_aibot import _default_sdk_factory, _frame_to_message


class FakeSdkClient:
    """模拟官方 aibot.WSClient 的事件发射器面：on 注册、emit 触发（支持 async handler）、
    connect/disconnect/reply_stream 记账。不连真 wss。"""

    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.connected = False
        self.disconnected = False
        self.replies: list[dict] = []

    def on(self, event: str, f=None):
        self.handlers.setdefault(event, []).append(f)
        return f

    async def connect(self):
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.disconnected = True

    async def reply_stream(self, frame: dict, stream_id: str, content: str, finish: bool = False):
        self.replies.append({
            "chatid": frame.get("body", {}).get("chatid"),
            "req_id": frame.get("headers", {}).get("req_id"),
            "stream_id": stream_id,
            "content": content,
            "finish": finish,
        })

    async def emit(self, event: str, *args):
        for h in list(self.handlers.get(event, [])):
            res = h(*args)
            if asyncio.iscoroutine(res):
                await res


def _text_frame(content: str, *, chat_id: str = "chat-1", user: str = "u-1", msg_id: str = "m-1", req_id: str = "r-1") -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": req_id},
        "body": {
            "msgid": msg_id,
            "aibotid": "bot-1",
            "chatid": chat_id,
            "chattype": "group",
            "from": {"userid": user},
            "msgtype": "text",
            "text": {"content": content},
        },
    }


async def _start_client(client: WecomAiBotClient, fake: FakeSdkClient):
    """拉起 run()，等到事件注册+建连完成，返回 task。"""
    task = asyncio.create_task(client.run())
    for _ in range(200):
        if fake.connected and "message.text" in fake.handlers:
            break
        await asyncio.sleep(0.005)
    return task


async def _stop_client(client: WecomAiBotClient, task):
    await client.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_frame_to_message_maps_all_fields():
    msg = _frame_to_message(_text_frame("成本？", chat_id="g-9", user="alice", msg_id="mm"))
    assert msg.chat_id == "g-9"
    assert msg.from_user == "alice"
    assert msg.text == "成本？"
    assert msg.msg_id == "mm"
    assert msg.msg_type == "text"
    assert msg.raw["headers"]["req_id"] == "r-1"  # 原始 frame 保留，回复要靠它做帧关联


def test_client_opens_placeholder_then_delivers_final_answer():
    """先开一帧空占位(finish=False，触发企微"正在等待回复")，handler 返回后同一
    stream_id 补 finish=True 的完整答案。"""
    fake = FakeSdkClient()
    seen: list[str] = []

    async def handler(message: IncomingMessage):
        seen.append(message.text)
        return f"收到：{message.text}"

    client = WecomAiBotClient(lambda: fake, handler)

    async def scenario():
        task = await _start_client(client, fake)
        await fake.emit("message.text", _text_frame("你好"))
        await _stop_client(client, task)

    asyncio.run(scenario())
    assert seen == ["你好"]
    assert len(fake.replies) == 2
    opener, final = fake.replies
    # 占位帧：空内容、finish=False；不污染最终答案（增量/整段两种流式语义下都干净）。
    assert opener["content"] == "" and opener["finish"] is False
    # 最终帧：同一 stream_id，完整答案、finish=True。
    assert final["stream_id"] == opener["stream_id"]
    assert final["chatid"] == "chat-1" and final["content"] == "收到：你好"
    assert final["finish"] is True


def test_client_closes_placeholder_when_handler_returns_none():
    """handler 不回复：占位已开过就得补一帧 finish=True 收尾，否则那三个点一直转。
    收尾内容为空，不会在群里留下有意义的气泡。"""
    fake = FakeSdkClient()

    async def handler(message: IncomingMessage):
        return None  # 不回复

    client = WecomAiBotClient(lambda: fake, handler)

    async def scenario():
        task = await _start_client(client, fake)
        await fake.emit("message.text", _text_frame("忽略我"))
        await _stop_client(client, task)

    asyncio.run(scenario())
    assert len(fake.replies) == 2
    opener, closer = fake.replies
    assert opener["content"] == "" and opener["finish"] is False
    assert closer["stream_id"] == opener["stream_id"]
    assert closer["content"] == "" and closer["finish"] is True  # 空内容收尾，停掉等待动画


def test_client_exits_cleanly_when_sdk_not_wired():
    """SDK 未安装：工厂抛 WecomAiBotNotWired → run() 优雅返回，不抛、不告警。"""
    alerts: list[str] = []

    def not_wired_factory():
        raise WecomAiBotNotWired("SDK 未安装")

    async def handler(message):  # pragma: no cover
        return None

    async def on_error(detail: str):
        alerts.append(detail)

    client = WecomAiBotClient(not_wired_factory, handler, on_error=on_error)
    asyncio.run(asyncio.wait_for(client.run(), timeout=2.0))  # 应迅速返回
    assert alerts == []


def test_client_isolates_handler_exception():
    """单条处理抛异常 → 触发 on_error，但事件循环不崩、后续消息照常处理并回复。"""
    fake = FakeSdkClient()
    handled: list[str] = []
    errors: list[str] = []

    async def handler(message: IncomingMessage):
        if message.text == "坏消息":
            raise ValueError("处理炸了")
        handled.append(message.text)
        return "ok"

    async def on_error(detail: str):
        errors.append(detail)

    client = WecomAiBotClient(lambda: fake, handler, on_error=on_error)

    async def scenario():
        task = await _start_client(client, fake)
        await fake.emit("message.text", _text_frame("坏消息", chat_id="c1"))
        await fake.emit("message.text", _text_frame("好消息", chat_id="c2"))
        await _stop_client(client, task)

    asyncio.run(scenario())
    assert handled == ["好消息"]  # 坏消息异常被隔离
    assert errors and "失败" in errors[0]
    # 坏消息：占位帧已开(c1)，handler 抛异常后不再补最终帧；好消息：占位+最终两帧(c2)。
    good = [r for r in fake.replies if r["finish"]]
    assert len(good) == 1 and good[0]["chatid"] == "c2" and good[0]["content"] == "ok"


def test_client_alerts_after_repeated_sdk_errors_and_resets_on_auth():
    """SDK error 事件连续达阈值 → 告警；authenticated 后计数清零，不重复刷屏。"""
    fake = FakeSdkClient()
    alerts: list[str] = []

    async def handler(message):  # pragma: no cover
        return None

    async def on_error(detail: str):
        alerts.append(detail)

    client = WecomAiBotClient(lambda: fake, handler, on_error=on_error, alert_after_failures=3)

    async def scenario():
        task = await _start_client(client, fake)
        for _ in range(3):
            await fake.emit("error", ConnectionError("boom"))  # 累计到阈值 → 1 次告警
        await fake.emit("authenticated")  # 健康 → 清零
        await fake.emit("error", ConnectionError("boom"))  # 计数回到 1，不告警
        await _stop_client(client, task)

    asyncio.run(scenario())
    assert len(alerts) == 1 and "连续" in alerts[0]


def test_rebuild_reason_flags_stale_disconnect_and_session_age():
    """看门狗判定（纯逻辑，注入合成时刻）：断线超宽限 / 连接超龄 → 需重建；健康 → 不重建。"""
    client = WecomAiBotClient(lambda: FakeSdkClient(), lambda m: None,
                              reconnect_grace=90.0, max_session_seconds=240.0)
    now = 1000.0
    # 初始：未连过(session_started=0)且未断线 → 不重建
    assert client._rebuild_reason(now) is None
    # 连接超龄：存活 250s ≥ 240 → 重建（兜静默停收）
    client._disconnected_since = None
    client._session_started = now - 250
    assert "换新连接" in (client._rebuild_reason(now) or "")
    # 断线超宽限：断线 100s ≥ 90 → 重建（优先于超龄判定）
    client._session_started = now
    client._disconnected_since = now - 100
    assert "断线" in (client._rebuild_reason(now) or "")
    # 健康窗口内：断线仅 10s、存活仅 10s → 不重建
    client._disconnected_since = now - 10
    client._session_started = now - 10
    assert client._rebuild_reason(now) is None


def test_rebuild_reason_defers_session_age_while_reply_inflight():
    """有回复在作答中（_inflight>0）时，超龄不得触发主动换连——否则掐掉正等着发 finish
    帧的那条连接，答案生成完却回不到群（慢问答只见三个点的根因）。但真断线仍必须换。"""
    client = WecomAiBotClient(lambda: FakeSdkClient(), lambda m: None,
                              reconnect_grace=90.0, max_session_seconds=240.0)
    now = 1000.0
    client._disconnected_since = None
    client._session_started = now - 250  # 已超龄
    # 空闲时超龄照常换连
    client._inflight = 0
    assert "换新连接" in (client._rebuild_reason(now) or "")
    # 作答中：超龄换连必须推迟，避免丢 finish 帧
    client._inflight = 1
    assert client._rebuild_reason(now) is None
    # 但作答中若真断线超宽限，仍要换（死 socket 不换答案同样发不出去）
    client._disconnected_since = now - 100
    assert "断线" in (client._rebuild_reason(now) or "")
    # 作答结束回到空闲，超龄换连恢复
    client._inflight = 0
    client._disconnected_since = None
    assert "换新连接" in (client._rebuild_reason(now) or "")


def test_on_text_tracks_inflight_across_delivery():
    """_on_text 全程（占位→作答→finish 帧）维持 _inflight>0，收尾后归零；异常路径也归零。"""
    fake = FakeSdkClient()
    seen_inflight: list[int] = []

    async def slow_handler(_message):
        seen_inflight.append(client._inflight)  # handler 执行期间应 >0
        return "answer"

    client = WecomAiBotClient(lambda: fake, slow_handler)

    async def scenario():
        task = await _start_client(client, fake)
        await fake.emit("message.text", _text_frame("成本？"))
        assert seen_inflight == [1]           # 作答期占用计数已置位
        assert client._inflight == 0          # 发完 finish 帧后归零
        await _stop_client(client, task)

    asyncio.run(scenario())


def test_authenticated_clears_disconnect_state():
    """authenticated 事件应清健康态：解除看门狗的断线宽限计时，避免误重建。"""
    fake = FakeSdkClient()
    client = WecomAiBotClient(lambda: fake, lambda m: None)

    async def scenario():
        task = await _start_client(client, fake)
        await fake.emit("disconnected", "network drop")
        assert client._disconnected_since is not None  # 断线态已记录
        await fake.emit("authenticated")
        assert client._disconnected_since is None       # 认证成功后恢复健康
        await _stop_client(client, task)

    asyncio.run(scenario())


def test_watchdog_rebuilds_after_disconnect_grace():
    """断线超宽限：看门狗销毁旧客户端、重建并重连新客户端（自愈，不裸信任 SDK 重连）。"""
    built: list[FakeSdkClient] = []

    def factory():
        c = FakeSdkClient()
        built.append(c)
        return c

    client = WecomAiBotClient(factory, lambda m: None,
                              watchdog_interval=0.01, reconnect_grace=0.02, max_session_seconds=100.0)

    async def scenario():
        task = asyncio.create_task(client.run())
        for _ in range(200):  # 等首连
            if built and built[0].connected:
                break
            await asyncio.sleep(0.005)
        # 首个客户端断线 → 进入宽限计时
        await built[0].emit("disconnected", "drop")
        for _ in range(400):  # 等看门狗超宽限后重建出第二个客户端并连上
            if len(built) >= 2 and built[1].connected:
                break
            await asyncio.sleep(0.005)
        await _stop_client(client, task)

    asyncio.run(scenario())
    assert len(built) >= 2, "看门狗应重建出新客户端"
    assert built[0].disconnected is True, "旧客户端应被销毁"
    assert built[1].connected is True, "新客户端应已重连"


def test_watchdog_rebuilds_on_session_age_even_if_connected():
    """连接看似健康但超龄：看门狗仍主动换新连接，兜住 SDK『静默停收』坑。"""
    built: list[FakeSdkClient] = []

    def factory():
        c = FakeSdkClient()
        built.append(c)
        return c

    # 从不断线、也从不报错；仅靠 max_session_seconds 触发主动换连。
    client = WecomAiBotClient(factory, lambda m: None,
                              watchdog_interval=0.01, reconnect_grace=100.0, max_session_seconds=0.02)

    async def scenario():
        task = asyncio.create_task(client.run())
        for _ in range(400):
            if len(built) >= 2 and built[1].connected:
                break
            await asyncio.sleep(0.005)
        await _stop_client(client, task)

    asyncio.run(scenario())
    assert len(built) >= 2, "超龄应触发主动换新连接"
    assert built[0].disconnected is True and built[1].connected is True


def test_default_sdk_factory_builds_real_client_when_installed():
    """SDK 已安装：默认工厂应造出带 connect/disconnect/reply_stream 的真实 WSClient（不建连）。"""
    client = _default_sdk_factory("bot-x", "secret-y")
    assert hasattr(client, "connect") and hasattr(client, "disconnect") and hasattr(client, "reply_stream")
