"""Checkpointer ?? - ????????????????"""

import logging
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from src.core.settings import get_settings

logger = logging.getLogger(__name__)


class CheckpointerManager:
    """Checkpointer ??????????????"""

    def __init__(self) -> None:
        self._checkpointer: BaseCheckpointSaver | None = None
        self._postgres_context: Any = None

    @property
    def checkpointer(self) -> BaseCheckpointSaver:
        if self._checkpointer is None:
            raise RuntimeError("Checkpointer ????????? init()")
        return self._checkpointer

    async def init(self, *, setup: bool = True) -> None:
        """????? checkpointer"""
        if self._checkpointer is not None:
            return

        settings = get_settings()
        logger.info(f"??? checkpointer, backend={settings.memory_backend}")

        try:
            match settings.memory_backend:
                case "memory":
                    self._checkpointer = MemorySaver()
                    logger.info("MemorySaver checkpointer ?????")
                case "postgresql":
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                    if not settings.memory_backend_url:
                        raise ValueError(
                            "memory_backend=postgresql ????? MEMORY_BACKEND_URL"
                        )
                    # from_conn_string ??????????
                    self._postgres_context = AsyncPostgresSaver.from_conn_string(
                        settings.memory_backend_url
                    )
                    # ?????
                    checkpointer = await self._postgres_context.__aenter__()
                    if setup:
                        await checkpointer.setup()  # type: ignore[attr-defined]
                    self._checkpointer = checkpointer
                    logger.info("PostgreSQL checkpointer ?????")
                case _:
                    raise ValueError(f"???? memory_backend: {settings.memory_backend}")
        except Exception as e:
            logger.error(f"Checkpointer ?????: {e}", exc_info=True)
            raise

    async def close(self) -> None:
        """?? checkpointer?????"""
        if self._postgres_context is not None:
            await self._postgres_context.__aexit__(None, None, None)
            self._postgres_context = None
        self._checkpointer = None


# ?? checkpointer ???
_checkpointer_manager: CheckpointerManager | None = None


def get_checkpointer_manager() -> CheckpointerManager:
    """???? checkpointer ???"""
    global _checkpointer_manager
    if _checkpointer_manager is None:
        _checkpointer_manager = CheckpointerManager()
    return _checkpointer_manager
