from __future__ import annotations

import asyncio
import os

import run


def test_server_loop_is_psycopg_compatible_on_windows():
    loop = run.loop_factory()
    try:
        if os.name == "nt":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
