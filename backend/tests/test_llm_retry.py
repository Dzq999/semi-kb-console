from __future__ import annotations

import httpx
import pytest

from app.services.llm import ExternalServiceError, LlmService


class _FakeResponse:
    def __init__(self, status_code: int, content: str = "ok"):
        self.status_code = status_code
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Stands in for httpx.AsyncClient; replays a scripted sequence of outcomes per .post()."""

    script: list = []
    calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        outcome = type(self).script[type(self).calls]
        type(self).calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_attempt):
        return None

    monkeypatch.setattr(LlmService, "_backoff", staticmethod(_instant))


def _install(monkeypatch, script):
    _FakeClient.script = script
    _FakeClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


async def _call():
    return await LlmService().complete("key", "model", "sys", "user")


@pytest.mark.asyncio
async def test_transient_disconnect_then_success(monkeypatch):
    _install(monkeypatch, [httpx.RemoteProtocolError("dropped"), _FakeResponse(200, "recovered")])
    assert await _call() == "recovered"
    assert _FakeClient.calls == 2


@pytest.mark.asyncio
async def test_retryable_status_then_success(monkeypatch):
    _install(monkeypatch, [_FakeResponse(503), _FakeResponse(200, "second")])
    assert await _call() == "second"
    assert _FakeClient.calls == 2


@pytest.mark.asyncio
async def test_exhausts_retries_then_raises(monkeypatch):
    from app.services.llm import _HTTP_RETRIES
    # 每次尝试都抛瞬时错误：共 1 初次 + _HTTP_RETRIES 次重试，全部耗尽后上抛。
    script = [httpx.RemoteProtocolError(f"d{i}") for i in range(_HTTP_RETRIES + 1)]
    _install(monkeypatch, script)
    with pytest.raises(ExternalServiceError):
        await _call()
    assert _FakeClient.calls == _HTTP_RETRIES + 1


@pytest.mark.asyncio
async def test_non_retryable_status_not_retried(monkeypatch):
    _install(monkeypatch, [_FakeResponse(400), _FakeResponse(200, "unused")])
    with pytest.raises(ExternalServiceError):
        await _call()
    assert _FakeClient.calls == 1  # 400 surfaces immediately, no retry


class _NativeResponse:
    """原生 Anthropic 响应结构：content 是 text 块数组，而非 OpenAI 的 choices。"""

    def __init__(self, text: str):
        self.status_code = 200
        self._text = text

    def json(self):
        return {"id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": self._text}], "stop_reason": "end_turn"}


class _CapturingClient(_FakeClient):
    last_url: str = ""
    last_payload: dict = {}

    async def post(self, url, *args, **kwargs):
        type(self).last_url = url
        type(self).last_payload = kwargs.get("json") or {}
        return await super().post(url, *args, **kwargs)


def _use_style(monkeypatch, style: str):
    # settings 是 frozen dataclass，不能就地 setattr；用 replace 造副本替换 llm 模块里的 settings 引用。
    import dataclasses

    import app.services.llm as llm_module

    monkeypatch.setattr(llm_module, "settings", dataclasses.replace(llm_module.settings, llm_api_style=style))


@pytest.mark.asyncio
async def test_anthropic_style_builds_native_request_and_parses_content(monkeypatch):
    # 默认 anthropic：打到 /v1/messages，system 顶层、max_tokens 必填、user 单条消息；解析 content[].text。
    _use_style(monkeypatch, "anthropic")
    _CapturingClient.script = [_NativeResponse("native-ok")]
    _CapturingClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    result = await LlmService().complete("key", "claude-x", "sys-prompt", "user-prompt")
    assert result == "native-ok"
    assert _CapturingClient.last_url.endswith("/messages")
    assert _CapturingClient.last_payload["system"] == "sys-prompt"
    assert _CapturingClient.last_payload["messages"] == [{"role": "user", "content": "user-prompt"}]
    assert _CapturingClient.last_payload["max_tokens"] >= 256


@pytest.mark.asyncio
async def test_anthropic_style_falls_back_to_openai_choices_shape(monkeypatch):
    # 中转若把原生端点也归一化成 OpenAI 的 choices 结构，_extract_text 仍能取到文本。
    _use_style(monkeypatch, "anthropic")
    _install(monkeypatch, [_FakeResponse(200, "choices-shape")])
    assert await _call() == "choices-shape"


@pytest.mark.asyncio
async def test_openai_style_builds_chat_completions_request(monkeypatch):
    # 显式回退 openai：打到 /v1/chat/completions，system 作为消息。
    _use_style(monkeypatch, "openai")
    _CapturingClient.script = [_FakeResponse(200, "compat-ok")]
    _CapturingClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    result = await LlmService().complete("key", "gpt-x", "sys-prompt", "user-prompt")
    assert result == "compat-ok"
    assert _CapturingClient.last_url.endswith("/chat/completions")
    assert _CapturingClient.last_payload["messages"][0] == {"role": "system", "content": "sys-prompt"}


@pytest.mark.asyncio
async def test_endpoint_override_beats_global_settings(monkeypatch):
    # 传入的 per-user endpoint 应完全覆盖全局 settings：即便全局是 anthropic，
    # endpoint.api_style=openai 也要打到自定义 base_url 的 /chat/completions。
    from app.services.llm import LlmEndpoint

    _use_style(monkeypatch, "anthropic")
    _CapturingClient.script = [_FakeResponse(200, "custom-ok")]
    _CapturingClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    endpoint = LlmEndpoint(base_url="https://proxy.example.com/v1", catalog_url="https://proxy.example.com/v1/models", api_style="openai")
    result = await LlmService().complete("key", "gpt-x", "sys", "user", endpoint=endpoint)
    assert result == "custom-ok"
    assert _CapturingClient.last_url == "https://proxy.example.com/v1/chat/completions"


# ---- 流式补全 complete_via_stream：截断显式判失败，正常收尾才返回 ----

class _StreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamClient:
    """替身 httpx.AsyncClient：.stream() 回放一段脚本化 SSE 行序列。"""

    script: list = []
    calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        outcome = type(self).script[type(self).calls]
        type(self).calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return _StreamResponse(outcome)


def _install_stream(monkeypatch, script):
    _StreamClient.script = script
    _StreamClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _StreamClient)


async def _stream_call():
    return await LlmService().complete_via_stream("key", "model", "sys", "user")


_ANTH_TEXT = 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"%s"}}'


@pytest.mark.asyncio
async def test_stream_clean_stop_returns_full_text(monkeypatch):
    _install_stream(monkeypatch, [[
        _ANTH_TEXT % "他",
        _ANTH_TEXT % "好",
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        'event: message_stop',
    ]])
    assert await _stream_call() == "他好"
    assert _StreamClient.calls == 1


@pytest.mark.asyncio
async def test_stream_max_tokens_cut_raises_not_partial(monkeypatch):
    # 关键：中转在 max_tokens 处截断，累积文本是残缺 JSON —— 绝不能当成功返回。
    # 每次尝试都截断 → 耗尽重试后上抛，而不是把残缺片段交回下游。
    from app.services.llm import _HTTP_RETRIES
    cut = [
        _ANTH_TEXT % "{\\\"a\\\":1",
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}',
    ]
    _install_stream(monkeypatch, [list(cut) for _ in range(_HTTP_RETRIES + 1)])
    with pytest.raises(ExternalServiceError):
        await _stream_call()


@pytest.mark.asyncio
async def test_stream_silent_eof_without_terminal_raises(monkeypatch):
    # 只发文本、从不发终止事件（EOF 静默断流）→ 判失败，不把残缺文本当完整。
    from app.services.llm import _HTTP_RETRIES
    partial = [_ANTH_TEXT % "半截"]
    _install_stream(monkeypatch, [list(partial) for _ in range(_HTTP_RETRIES + 1)])
    with pytest.raises(ExternalServiceError):
        await _stream_call()


@pytest.mark.asyncio
async def test_stream_openai_length_finish_is_cut(monkeypatch):
    from app.services.llm import LlmEndpoint, _HTTP_RETRIES
    ep = LlmEndpoint(base_url="https://p/v1", catalog_url="https://p/v1/models", api_style="openai")
    cut = ['data: {"choices":[{"delta":{"content":"部分"},"finish_reason":"length"}]}']
    _install_stream(monkeypatch, [list(cut) for _ in range(_HTTP_RETRIES + 1)])
    with pytest.raises(ExternalServiceError):
        await LlmService().complete_via_stream("key", "model", "sys", "user", endpoint=ep)
