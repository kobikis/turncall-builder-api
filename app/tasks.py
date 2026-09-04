"""Fire-and-forget background tasks.

`docker compose up -d --build` takes tens of seconds to minutes; running it
inline made Create/Update/Start hang the whole request. These run it after the
response, holding a strong reference so the task can't be GC'd mid-build.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_BG_TASKS: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task
