from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import checkpoints as ckpt
from app.services.checkpoints import CheckpointRuntime


class _FailingContext:
    """Mimics AsyncPostgresSaver.from_conn_string(...) whose connect fails."""

    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self):
        raise ConnectionError("checkpoint db unreachable")

    async def __aexit__(self, *exc):
        self.exited = True
        return False


def test_startup_degrades_to_memory_when_db_unreachable(monkeypatch):
    context = _FailingContext()
    # settings is a frozen dataclass with a property; swap the whole reference.
    monkeypatch.setattr(ckpt, "settings", SimpleNamespace(checkpoint_database_url="postgresql://x/y"))
    monkeypatch.setattr(ckpt.AsyncPostgresSaver, "from_conn_string", lambda url: context)

    runtime = CheckpointRuntime()
    # Must not raise — a broken checkpoint DB cannot block backend startup.
    asyncio.run(runtime.startup())

    assert runtime.backend == "memory"
    assert runtime.last_error is not None and "ConnectionError" in runtime.last_error
    assert context.exited is True  # failed context was torn down, no leak
    # A resumable-run config still works against the in-memory saver.
    assert runtime.config("run-x", 1)["configurable"]["thread_id"] == "run-x:round:1"


def test_startup_noops_without_checkpoint_url(monkeypatch):
    monkeypatch.setattr(ckpt, "settings", SimpleNamespace(checkpoint_database_url=None))
    runtime = CheckpointRuntime()
    asyncio.run(runtime.startup())
    assert runtime.backend == "memory"
    assert runtime.last_error is None
