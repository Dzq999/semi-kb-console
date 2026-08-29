import asyncio
import os

import uvicorn

from app.config import settings


def loop_factory() -> asyncio.AbstractEventLoop:
    """Uvicorn 0.52 defaults to Proactor on Windows, which psycopg async rejects."""
    if os.name == "nt":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


if __name__ == "__main__":
    options = {"loop": loop_factory} if os.name == "nt" else {}
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False, **options)
