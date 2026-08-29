from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from ..config import settings


class CheckpointRuntime:
    """Owns one process-wide checkpointer without leaking database credentials into graph state."""

    def __init__(self) -> None:
        self.saver: Any = MemorySaver()
        self.backend = "memory"
        self._context: AbstractAsyncContextManager | None = None

    async def startup(self) -> None:
        if not settings.checkpoint_database_url:
            return
        self._context = AsyncPostgresSaver.from_conn_string(settings.checkpoint_database_url)
        self.saver = await self._context.__aenter__()
        await self.saver.setup()
        self.backend = "postgres"

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
