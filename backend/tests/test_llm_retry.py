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
    _install(monkeypatch, [httpx.RemoteProtocolError("d1"), httpx.ReadTimeout("d2"), httpx.ConnectError("d3")])
    with pytest.raises(ExternalServiceError):
        await _call()
    assert _FakeClient.calls == 3  # 1 initial + 2 retries


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
