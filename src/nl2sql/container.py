"""Application-scoped runtime dependencies; no request path relies on module globals."""

from __future__ import annotations

import asyncio
from typing import Any

from src.nl2sql.infra.llm.gateway import ModelGateway, build_model_gateway
from src.nl2sql.infra.memory.checkpointer import CheckpointerManager
from src.nl2sql.observability.control_audit import ControlAuditStore


class AppContainer:
    """Own the resources for one FastAPI application instance and its lifespan."""

    def __init__(self) -> None:
        self._checkpointer_manager = CheckpointerManager()
        self._engine: Any | None = None
        self._engine_lock = asyncio.Lock()
        self._model_gateway: ModelGateway | None = None
        self._audit_store: ControlAuditStore | None = None

    async def start(self) -> None:
        """Initialize only state persistence; migrations and index builds are external jobs."""

        await self._checkpointer_manager.init(setup=False)
        from src.core.settings import get_settings

        settings = get_settings()
        if settings.control_database_url:
            self._audit_store = await ControlAuditStore.open(settings.control_database_url)

    @property
    def audit_available(self) -> bool:
        return self._audit_store is not None

    async def get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        async with self._engine_lock:
            if self._engine is None:
                from src.nl2sql.orchestration.engine import create_v2_engine

                self._model_gateway = build_model_gateway()
                self._engine = create_v2_engine(
                    checkpointer=self._checkpointer_manager.checkpointer,
                    model_gateway=self._model_gateway,
                    trace_sink=self._audit_store,
                )
        return self._engine

    async def close(self) -> None:
        self._engine = None
        self._model_gateway = None
        if self._audit_store is not None:
            await self._audit_store.close()
            self._audit_store = None
        await self._checkpointer_manager.close()
