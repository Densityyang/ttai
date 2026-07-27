"""Checkpointer 工厂 - 根据配置创建对应的会话持久化后端"""

import logging
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from src.core.settings import get_settings

logger = logging.getLogger(__name__)


class CheckpointerManager:
    """Checkpointer 管理器，处理不同后端的初始化"""

    def __init__(self) -> None:
        self._checkpointer: BaseCheckpointSaver | None = None
        self._postgres_context: Any = None

    @property
    def checkpointer(self) -> BaseCheckpointSaver:
        if self._checkpointer is None:
            raise RuntimeError("Checkpointer 未初始化，请先调用 init()")
        return self._checkpointer

    async def init(self) -> None:
        """异步初始化 checkpointer"""
        if self._checkpointer is not None:
            return

        settings = get_settings()
        logger.info(f"初始化 checkpointer, backend={settings.memory_backend}")

        try:
            match settings.memory_backend:
                case "memory":
                    self._checkpointer = MemorySaver()
                    logger.info("MemorySaver checkpointer 初始化成功")
                case "postgresql":
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                    if not settings.memory_backend_url:
                        raise ValueError(
                            "memory_backend=postgresql 时必须配置 MEMORY_BACKEND_URL"
                        )
                    # from_conn_string 返回异步上下文管理器
                    self._postgres_context = AsyncPostgresSaver.from_conn_string(
                        settings.memory_backend_url
                    )
                    # 进入上下文
                    checkpointer = await self._postgres_context.__aenter__()
                    # 初始化表（setup 是 AsyncPostgresSaver 特有方法）
                    await checkpointer.setup()  # type: ignore[attr-defined]
                    self._checkpointer = checkpointer
                    logger.info("PostgreSQL checkpointer 初始化成功")
                case _:
                    raise ValueError(f"不支持的 memory_backend: {settings.memory_backend}")
        except Exception as e:
            logger.error(f"Checkpointer 初始化失败: {e}", exc_info=True)
            raise

    async def close(self) -> None:
        """关闭 checkpointer，释放资源"""
        if self._postgres_context is not None:
            await self._postgres_context.__aexit__(None, None, None)
            self._postgres_context = None
        self._checkpointer = None


# 全局 checkpointer 管理器
_checkpointer_manager: CheckpointerManager | None = None


def get_checkpointer_manager() -> CheckpointerManager:
    """获取全局 checkpointer 管理器"""
    global _checkpointer_manager
    if _checkpointer_manager is None:
        _checkpointer_manager = CheckpointerManager()
    return _checkpointer_manager