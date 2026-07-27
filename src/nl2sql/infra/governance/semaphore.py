"""全局并发控制 -- 基于 asyncio.Semaphore 的资源治理。

为不同类型的操作设置独立的并发上限，防止资源争抢。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class ConcurrencyGovernor:
    """全局并发治理器。"""

    def __init__(
        self,
        rag_concurrency: int = 3,
        sql_concurrency: int = 5,
        code_concurrency: int = 2,
        index_concurrency: int = 1,
    ) -> None:
        self._semaphores = {
            "rag": asyncio.Semaphore(rag_concurrency),
            "sql": asyncio.Semaphore(sql_concurrency),
            "code": asyncio.Semaphore(code_concurrency),
            "index": asyncio.Semaphore(index_concurrency),
        }
        self._limits = {
            "rag": rag_concurrency,
            "sql": sql_concurrency,
            "code": code_concurrency,
            "index": index_concurrency,
        }

    @asynccontextmanager
    async def acquire(self, resource_type: str) -> AsyncIterator[None]:
        """获取指定资源类型的并发许可。

        Args:
            resource_type: rag / sql / code / index
        """
        sem = self._semaphores.get(resource_type)
        if sem is None:
            yield
            return

        logger.debug(
            "等待 %s 并发许可 (当前可用: %d/%d)",
            resource_type,
            sem._value,
            self._limits[resource_type],
        )
        async with sem:
            yield

    def get_status(self) -> dict[str, dict[str, int]]:
        """返回各资源类型的并发状态。"""
        return {
            name: {
                "limit": self._limits[name],
                "available": sem._value,
            }
            for name, sem in self._semaphores.items()
        }


@lru_cache
def get_concurrency_governor() -> ConcurrencyGovernor:
    """获取全局并发治理器单例。"""
    return ConcurrencyGovernor()
