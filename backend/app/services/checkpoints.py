from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from ..config import settings

logger = logging.getLogger(__name__)


class CheckpointRuntime:
    """Owns one process-wide checkpointer without leaking database credentials into graph state."""

    def __init__(self) -> None:
        self.saver: Any = MemorySaver()
        self.backend = "memory"
        self.last_error: str | None = None
        self._context: AbstractAsyncContextManager | None = None

    async def startup(self) -> None:
        if not settings.checkpoint_database_url:
            return
        context = AsyncPostgresSaver.from_conn_string(settings.checkpoint_database_url)
        try:
            saver = await context.__aenter__()
            await saver.setup()
        except Exception as exc:  # noqa: BLE001 — degrade instead of blocking startup
            # Contain the blast radius: an unreachable/corrupt checkpoint DB must
            # not stop the whole backend from booting.  Fall back to in-memory
            # checkpointing (resumability is lost until the DB is restored).
            try:
                await context.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:  # noqa: BLE001 — teardown best-effort only
                pass
            self.saver = MemorySaver()
            self.backend = "memory"
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Checkpoint database unavailable; falling back to in-memory checkpointer. "
                "Runs will not be resumable until it is restored. Cause: %s",
                self.last_error,
            )
            return
        self._context = context
        self.saver = saver
        self.backend = "postgres"
        self.last_error = None

    async def shutdown(self) -> None:
        if self._context:
            await self._context.__aexit__(None, None, None)
            self._context = None

    def config(self, run_id: str, round_number: int, namespace: str = "") -> dict:
        thread_id = f"{run_id}:round:{round_number}"
        if namespace:
            thread_id = f"{thread_id}:{namespace}"
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            },
            "recursion_limit": 40,
        }


checkpoint_runtime = CheckpointRuntime()
