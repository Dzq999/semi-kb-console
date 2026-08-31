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
