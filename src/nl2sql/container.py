"""Application-scoped runtime dependencies; no request path relies on module globals."""

from __future__ import annotations

import asyncio
from typing import Any

from src.nl2sql.infra.memory.checkpointer import CheckpointerManager


class AppContainer:
    """Own the resources for one FastAPI application instance and its lifespan."""

    def __init__(self) -> None:
        self._checkpointer_manager = CheckpointerManager()
        self._supervisor: Any | None = None
        self._supervisor_lock = asyncio.Lock()

    async def start(self) -> None:
        """Initialize only state persistence; migrations and index builds are external jobs."""

        await self._checkpointer_manager.init(setup=False)

    async def get_supervisor(self) -> Any:
        if self._supervisor is not None:
            return self._supervisor
        async with self._supervisor_lock:
            if self._supervisor is None:
                from src.nl2sql.supervisor.agent import create_supervisor

                self._supervisor = await create_supervisor(self._checkpointer_manager.checkpointer)
        return self._supervisor

    async def close(self) -> None:
        self._supervisor = None
        await self._checkpointer_manager.close()
