"""Process runtime handles (DB pool + TurnCall client).

Handlers used to read these off ``app.state``, which tied every one to the
module-global ``app`` and blocked moving them into router modules. Routers and
helpers now call ``get_pool()`` / ``get_client()`` instead; the app lifespan
sets them on startup and clears them on shutdown. Tests set them directly.
"""

from __future__ import annotations

from typing import Any

_pool: Any = None
_client: Any = None


def set_runtime(pool: Any, client: Any) -> None:
    global _pool, _client
    _pool, _client = pool, client


def clear_runtime() -> None:
    global _pool, _client
    _pool, _client = None, None


def get_pool() -> Any:
    return _pool


def get_client() -> Any:
    return _client
