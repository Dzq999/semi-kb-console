"""企业微信智能机器人长连接(WebSocket)适配层。

治理边界：本层只负责【传输】——建连、收发、心跳、断线重连由官方 SDK 承接；本层只做
「SDK 事件 → 业务 handler → 回帧」的适配。绝不感知问答业务，也绝不读取代码/配置/密钥
以外的任何项目内容。业务逻辑全部由注入的 handler 承接（见 wecom_qa_bridge），handler
内部只走 answer_question 的严格接地链路。

设计要点（把第三方 SDK 隔离在最小面）：
- 官方 SDK `wecom-aibot-python-sdk`（import 名 `aibot`）是事件发射器模型（pyee 的
  AsyncIOEventEmitter），自带 WebSocket 建连、认证、心跳、指数退避重连。回复是「帧关联」的：
  用收到的原始 frame 调 reply_stream，一次性回复即 finish=True。
- SDK 实例的构造被隔离在 _default_sdk_factory 里；未安装 SDK 时抛 WecomAiBotNotWired，
  由 lifespan 优雅跳过，不影响后端其余功能。测试注入假 SDK 客户端即可完整跑通。
- 已知问题：SDK ~5min 断连、重连后偶发静默停收。故构造时设 max_reconnect_attempts=-1
  (无限重连)，并对 error/reconnecting 事件做告警计数(on_error)，不裸信任默认重连次数。
- 看门狗（本层自建，不裸信任 SDK 重连）：一个常驻协程周期巡检连接健康——
  (a) 收到 disconnected 后超过 reconnect_grace 仍未恢复 → 销毁并重建 SDK 客户端；
  (b) 即便 SDK 自认为"还连着"，每 max_session_seconds 也主动重建一次，兜住"静默停收"
      （连接看似健康却停止收帧）这个最难发现的坑，赶在 ~5min 死亡窗口前换新连接。
  重建期间有极短(亚秒级)收帧空窗，对"人打字提问"的问答机器人可接受（重问即可）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("wecom_aibot")

# 企微智能机器人长连接端点（官方，无需公网域名/IP）。真实握手由 SDK 完成。
WECOM_AIBOT_WS_ENDPOINT = "wss://openws.work.weixin.qq.com"


@dataclass
class IncomingMessage:
    """一条群内 @机器人 消息的适配层视图（与业务解耦，只保留桥接必需字段）。

    raw 保留原始 SDK frame——回复必须用它做帧关联(headers.req_id)，故不能丢。
    """

    chat_id: str  # 群标识(chatid)，用于按群幂等定位问答会话
    from_user: str  # 发送者标识（企微 userid），仅作元信息，不参与作答
    text: str  # 问题正文
    msg_id: str = ""
    msg_type: str = "text"
    raw: dict = field(default_factory=dict)


class SdkClient(Protocol):
    """本层依赖的官方 SDK WSClient 子集面。真实实现是 aibot.WSClient；测试注入假实现。"""

    def on(self, event: str, f: Callable | None = None) -> Any: ...

    async def connect(self) -> Any: ...

    def disconnect(self) -> None: ...

    async def reply_stream(self, frame: dict, stream_id: str, content: str, finish: bool = False) -> Any: ...


# handler：收到消息 → 返回要回复的文本(None 表示不回复)。业务与适配的唯一接缝。
MessageHandler = Callable[[IncomingMessage], Awaitable[Optional[str]]]
# SDK 工厂：给定 bot_id + secret 造一个 SdkClient。真实实现见 _default_sdk_factory。
SdkClientFactory = Callable[[], SdkClient]
ErrorHook = Callable[[str], Awaitable[None]]


class WecomAiBotNotWired(RuntimeError):
    """官方 SDK 未安装/不可用时由默认工厂抛出，供 lifespan 优雅跳过长连接启动。"""


def _default_sdk_factory(bot_id: str, secret: str) -> SdkClient:
    """用官方企微 AI 机器人 SDK(`aibot`) 造 WSClient。未安装则抛 WecomAiBotNotWired。

    max_reconnect_attempts=-1：无限重连（规避 ~5min 断连后进程放弃重连的坑）。
    """
    try:
        from aibot import WSClient, WSClientOptions  # 延迟导入：未装 SDK 时不影响后端启动
    except ImportError as exc:  # pragma: no cover - 取决于部署环境是否装了 SDK
        raise WecomAiBotNotWired(
            "企微智能机器人官方 SDK 未安装。请 `pip install wecom-aibot-python-sdk` 后重启后端。"
        ) from exc
    return WSClient(WSClientOptions(bot_id=bot_id, secret=secret, max_reconnect_attempts=-1))


def _frame_to_message(frame: dict) -> IncomingMessage:
    """把 SDK 文本消息 frame 映射为 IncomingMessage。

    frame 结构（企微回调）：
      {cmd, headers:{req_id}, body:{msgid, aibotid, chatid, chattype,
       from:{userid}, msgtype:"text", text:{content}}}
    """
    body = frame.get("body", {}) if isinstance(frame, dict) else {}
    sender = body.get("from", {}) if isinstance(body.get("from"), dict) else {}
    text = body.get("text", {}) if isinstance(body.get("text"), dict) else {}
    return IncomingMessage(
        chat_id=str(body.get("chatid") or ""),
        from_user=str(sender.get("userid") or ""),
        text=str(text.get("content") or ""),
        msg_id=str(body.get("msgid") or ""),
        msg_type=str(body.get("msgtype") or "text"),
        raw=frame if isinstance(frame, dict) else {},
    )


class WecomAiBotClient:
    """长连接运行器：包官方 SDK 的事件发射器，桥接文本消息到 handler，并帧关联回复。

    SDK 自己管建连/认证/心跳/重连；本层只做：注册事件 → 收到文本 → 交 handler → 一次性
    reply_stream 回群。单条处理异常隔离，绝不影响 SDK 事件循环与其他消息。run() 可被取消，
    收到 CancelledError 时 disconnect 后退出。
    """

    def __init__(
        self,
        sdk_factory: SdkClientFactory,
        handler: MessageHandler,
        *,
        on_error: ErrorHook | None = None,
        alert_after_failures: int = 3,
        watchdog_interval: float = 30.0,
        reconnect_grace: float = 90.0,
        max_session_seconds: float = 240.0,
    ) -> None:
        self._sdk_factory = sdk_factory
        self._handler = handler
        self._on_error = on_error
        self._alert_after = alert_after_failures
        # 看门狗节律：巡检周期 / 断线宽限（超过则重建）/ 单条连接最长存活（到点主动换新连接）。
        self._watchdog_interval = watchdog_interval
        self._reconnect_grace = reconnect_grace
        self._max_session_seconds = max_session_seconds
        self._client: SdkClient | None = None
        self._stop_event = asyncio.Event()
        self._consecutive_failures = 0
        # 正在作答的消息数：>0 时禁止「会话超龄」主动换连接——否则会把等待作答的那条连接
        # 掐掉，答案生成完却发不回群（finish 帧落到已断开的旧 client）。慢问答曾因此只见三个点。
        self._inflight = 0
        # 健康状态（单调时钟，避免系统时间回拨干扰）：
        # _disconnected_since=None 表示"已连上/健康"，否则记录进入不健康态的时刻。
        self._disconnected_since: float | None = None
        self._session_started: float = 0.0

    async def run(self) -> None:
        """建连并常驻，直到被 stop() 或取消。SDK 未接入时优雅返回，不抛出。

        首连由 _try_connect 完成；之后交给看门狗巡检——断线自愈 + 定期换新连接，绕开
        SDK 自身重连停摆与"静默停收"。run() 只挂在 stop_event 上，不裸信任 SDK 的存活。
        """
        try:
            self._client = self._sdk_factory()
        except WecomAiBotNotWired as exc:
            logger.warning("WeCom AI bot not started: %s", exc)
            return
        self._register_events(self._client)
        await self._try_connect(initial=True)  # 失败也不抛：看门狗会持续重试
        watchdog = asyncio.create_task(self._watchdog())
        try:
            await self._stop_event.wait()  # 常驻，直到 stop() 或被取消
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            self._safe_disconnect()

    async def _try_connect(self, *, initial: bool) -> bool:
        """建一次连接（连 self._client）。成功记录会话起点、清健康态；失败记为断线态。

        connect() 只保证建连；认证由 SDK 随后以 SUBSCRIBE 帧完成并触发 authenticated 事件。
        故这里把 connect 成功视为"会话已开始"，最终健康与否交给 authenticated/disconnected 事件。
        """
        client = self._client
        if client is None:
            return False
        self._session_started = time.monotonic()  # 会话计时从"发起本次建连"起算
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001 建连异常不应杀死进程/看门狗
            logger.warning("WeCom AI bot connect failed: %s", exc)
            self._disconnected_since = time.monotonic()
            if initial and self._on_error:
                await self._on_error(f"企微机器人启动失败：{type(exc).__name__}")
            return False
        self._disconnected_since = None
        logger.info("WeCom AI bot connected: %s", WECOM_AIBOT_WS_ENDPOINT)
        return True

    def _rebuild_reason(self, now: float) -> str | None:
        """判定是否需要重建连接。返回原因串（日志/测试用），None 表示无需重建。

        - 断线宽限：进入断线态且已超 reconnect_grace 仍未恢复（真断线，死 socket 必须换）。
        - 会话超龄：连接存活超 max_session_seconds（即便看似健康也主动换新连接，兜静默停收）；
          但**有回复在作答中时不换**——那条连接正等着发 finish 帧回群，掐掉就丢答案。超龄换连
          只推迟到当前这批作答结束（单条问答量级、最多几分钟），静默停收的防护仍在。
        """
        if self._disconnected_since is not None and now - self._disconnected_since >= self._reconnect_grace:
            return f"断线超过 {self._reconnect_grace:.0f}s 未恢复"
        if (
            self._inflight == 0
            and self._session_started
            and now - self._session_started >= self._max_session_seconds
        ):
            return f"连接存活超过 {self._max_session_seconds:.0f}s，主动换新连接"
        return None

    async def _watchdog(self) -> None:
        """周期巡检：断线超宽限 → 重建；连接超龄 → 主动换新连接。异常不外泄、不杀循环。"""
        while not self._stop_event.is_set():
            await asyncio.sleep(self._watchdog_interval)
            if self._stop_event.is_set():
                break
            reason = self._rebuild_reason(time.monotonic())
            if reason:
                logger.warning("WeCom AI bot watchdog rebuilding: %s", reason)
                await self._rebuild()

    async def _rebuild(self) -> None:
        """销毁旧 SDK 客户端并重建、重连。任何异常都不外泄，看门狗照常巡检。"""
        self._safe_disconnect()
        try:
            self._client = self._sdk_factory()
        except WecomAiBotNotWired as exc:  # pragma: no cover - 运行期一般不会突然缺 SDK
            logger.warning("WeCom AI bot rebuild skipped (SDK 未接入): %s", exc)
            self._session_started = time.monotonic()  # 避免每 tick 反复触发超龄
            return
        self._register_events(self._client)
        await self._try_connect(initial=False)

    def _register_events(self, client: SdkClient) -> None:
        client.on("message.text", self._on_text)
        client.on("authenticated", self._on_authenticated)
        client.on("error", self._on_sdk_error)
        client.on("disconnected", self._on_disconnected)
        client.on("reconnecting", self._on_reconnecting)

    async def _on_text(self, frame: dict) -> None:
        """收到文本消息：映射 → 先开一帧空占位（触发企微"正在等待回复"三个点）→ handler
        作答 → 用同一 stream_id 补 finish=True 的完整答案回群。异常隔离。

        回复必须走"收到此帧的那个客户端"：handler 可能耗时数秒，其间看门狗若重建连接会
        换掉 self._client，用新客户端回旧帧(req_id 关联失效)会串线。故在入口捕获本地引用。

        占位帧内容留空：企微的流式回复无论按增量追加还是整段覆盖解释，空内容都不会污染最终
        答案。开占位失败（旧 SDK/网络抖动）不致命——吞掉异常，退回到只发一次性完整答案。
        """
        client = self._client
        # 作答期占用计数：从占位到 finish 帧整段都算"在途"。看门狗见 _inflight>0 便不因超龄换连，
        # 保证这条捕获的连接活到答案发回群为止（慢问答曾因超龄换连丢 finish 帧，只剩三个点）。
        self._inflight += 1
        try:
            message = _frame_to_message(frame)
            # 同一 stream_id 复用于占位帧与最终帧：同 req_id 的回复走串行队列，保证先占位后答案。
            stream_id = f"stream_{uuid.uuid4().hex[:12]}"
            placeholder_open = False
            if client is not None:
                try:
                    await client.reply_stream(frame, stream_id, "", False)
                    placeholder_open = True
                except Exception as exc:  # noqa: BLE001 占位失败不影响最终作答
                    logger.warning("WeCom AI bot stream placeholder failed: %s", exc)
            reply = await self._handler(message)
            if client is not None and (reply or placeholder_open):
                # 有答案就发答案；handler 无回复但已开过占位，也得补一帧 finish=True 收尾，
                # 否则那三个点会一直转（悬挂的未完成流）。收尾内容用答案，无答案则空串。
                await client.reply_stream(frame, stream_id, reply or "", True)
        except Exception as exc:  # noqa: BLE001 单条处理异常绝不影响 SDK 事件循环
            logger.exception("WeCom AI bot message handling failed: %s", exc)
            if self._on_error:
                await self._on_error(f"企微机器人处理消息失败：{type(exc).__name__}")
        finally:
            self._inflight -= 1

    def _on_authenticated(self) -> None:
        self._consecutive_failures = 0  # 认证成功即视为连接健康，清零告警计数
        self._disconnected_since = None  # 恢复健康态，解除看门狗的断线宽限计时
        logger.info("WeCom AI bot authenticated")

    async def _on_sdk_error(self, error: object) -> None:
        self._consecutive_failures += 1
        logger.warning("WeCom AI bot SDK error (#%d): %s", self._consecutive_failures, error)
        if self._consecutive_failures >= self._alert_after and self._on_error:
            await self._on_error(f"企微机器人连续 {self._consecutive_failures} 次连接/传输异常")

    def _on_disconnected(self, reason: object = None) -> None:
        # 记录进入断线态的时刻，供看门狗判断是否超宽限需要重建（已在断线态则不覆盖起点）。
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()
        logger.warning("WeCom AI bot disconnected: %s", reason)

    def _on_reconnecting(self, attempt: object = None) -> None:
        logger.info("WeCom AI bot reconnecting: attempt %s", attempt)

    def _safe_disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def stop(self) -> None:
        self._stop_event.set()
        self._safe_disconnect()
