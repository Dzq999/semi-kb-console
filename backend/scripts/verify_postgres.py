from __future__ import annotations

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from app.db import engine
from app.migrations import upgrade_database
from app.services.checkpoints import checkpoint_runtime


class SmokeState(TypedDict):
    value: int


async def main() -> None:
    if engine.dialect.name != "postgresql":
        raise SystemExit("PostgreSQL environment is not configured")
    upgrade_database()
    with engine.connect() as connection:
        identity = connection.execute(text("select current_user, current_database()")) .one()
    await checkpoint_runtime.startup()
    graph = StateGraph(SmokeState)
    graph.add_node("increment", lambda state: {"value": state["value"] + 1})
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)
    config = checkpoint_runtime.config("postgres-smoke", 1)
    result = await graph.compile(checkpointer=checkpoint_runtime.saver).ainvoke({"value": 1}, config=config)
    checkpoint = await checkpoint_runtime.saver.aget_tuple(config)
    print(f"database={identity[1]}")
    print(f"user={identity[0]}")
    print(f"checkpoint_backend={checkpoint_runtime.backend}")
    print(f"graph_value={result['value']}")
    print(f"checkpoint_saved={bool(checkpoint)}")
    await checkpoint_runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
