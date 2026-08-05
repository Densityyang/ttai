"""进程内并发控制与 QueryGateway 有界容量治理。

为不同类型的操作设置独立的并发上限，防止资源争抢。
"""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import AsyncIterator, Literal

logger = logging.getLogger(__name__)


CapacityReason = Literal["queue_full", "wait_timeout"]


class CapacityExceededError(RuntimeError):
    """Raised when a bounded resource queue cannot admit a caller."""

    def __init__(self, reason: CapacityReason, resource_type: str = "SQL") -> None:
        message = (
            f"{resource_type} capacity waiting queue is full"
            if reason == "queue_full"
            else f"{resource_type} capacity wait timed out"
        )
        super().__init__(message)
        self.reason = reason
        self.resource_type = resource_type


@dataclass(slots=True)
class _CapacityWaiter:
    future: asyncio.Future[None]
    granted: bool = False


class _BoundedCapacity:
    """FIFO resource capacity without an unbounded ``Semaphore`` waiter list."""

    def __init__(
        self,
        *,
        resource_type: str,
        active_limit: int,
        wait_queue_size: int,
        wait_timeout_seconds: float,
    ) -> None:
        if active_limit < 1:
            raise ValueError(f"{resource_type} active concurrency must be positive")
        if wait_queue_size < 0:
            raise ValueError(f"{resource_type} wait queue size cannot be negative")
        if wait_timeout_seconds <= 0:
            raise ValueError(f"{resource_type} capacity wait timeout must be positive")
        self._resource_type = resource_type
        self._active_limit = active_limit
        self._wait_queue_size = wait_queue_size
        self._wait_timeout_seconds = wait_timeout_seconds
        self._active = 0
        self._waiters: deque[_CapacityWaiter] = deque()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        waiter = self._reserve_or_enqueue()
        slot_acquired = waiter is None
        try:
            if waiter is not None:
                try:
                    async with asyncio.timeout(self._wait_timeout_seconds):
                        await waiter.future
                except TimeoutError as exc:
                    self._withdraw(waiter)
                    raise CapacityExceededError("wait_timeout", self._resource_type) from exc
                except asyncio.CancelledError:
                    self._withdraw(waiter)
                    raise
                slot_acquired = True
            yield
        finally:
            if slot_acquired:
                self._release()

    def status(self) -> dict[str, int]:
        return {
            "limit": self._active_limit,
            "available": self._active_limit - self._active,
            "active": self._active,
            "waiting": len(self._waiters),
            "queue_limit": self._wait_queue_size,
        }

    def _reserve_or_enqueue(self) -> _CapacityWaiter | None:
        # These state transitions contain no await and therefore run atomically
        # with respect to other tasks on the API process event loop.
        self._discard_cancelled_waiters()
        if self._active < self._active_limit and not self._waiters:
            self._active += 1
            return None
        if len(self._waiters) >= self._wait_queue_size:
            raise CapacityExceededError("queue_full", self._resource_type)
        waiter = _CapacityWaiter(asyncio.get_running_loop().create_future())
        self._waiters.append(waiter)
        return waiter

    def _withdraw(self, waiter: _CapacityWaiter) -> None:
        if waiter.granted:
            self._release()
            return
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass
        if not waiter.future.done():
            waiter.future.cancel()

    def _release(self) -> None:
        if self._active < 1:
            raise RuntimeError(
                f"{self._resource_type} capacity slot released without an active holder"
            )
        self._active -= 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue
            waiter.granted = True
            self._active += 1
            waiter.future.set_result(None)
            break

    def _discard_cancelled_waiters(self) -> None:
        if any(waiter.future.done() for waiter in self._waiters):
            self._waiters = deque(
                waiter for waiter in self._waiters if not waiter.future.done()
            )


class ConcurrencyGovernor:
    """全局并发治理器。"""

    def __init__(
        self,
        rag_concurrency: int = 3,
        sql_concurrency: int = 4,
        code_concurrency: int = 2,
        index_concurrency: int = 1,
        *,
        sql_wait_queue_size: int = 8,
        sql_wait_timeout_seconds: float = 3.0,
        rag_wait_queue_size: int = 6,
        code_wait_queue_size: int = 4,
        index_wait_queue_size: int = 2,
    ) -> None:
        self._capacities = {
            "rag": _BoundedCapacity(
                resource_type="RAG",
                active_limit=rag_concurrency,
                wait_queue_size=rag_wait_queue_size,
                wait_timeout_seconds=sql_wait_timeout_seconds,
            ),
            "sql": _BoundedCapacity(
                resource_type="SQL",
                active_limit=sql_concurrency,
                wait_queue_size=sql_wait_queue_size,
                wait_timeout_seconds=sql_wait_timeout_seconds,
            ),
            "code": _BoundedCapacity(
                resource_type="code",
                active_limit=code_concurrency,
                wait_queue_size=code_wait_queue_size,
                wait_timeout_seconds=sql_wait_timeout_seconds,
            ),
            "index": _BoundedCapacity(
                resource_type="index",
                active_limit=index_concurrency,
                wait_queue_size=index_wait_queue_size,
                wait_timeout_seconds=sql_wait_timeout_seconds,
            ),
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
        capacity = self._capacities.get(resource_type)
        if capacity is None:
            yield
            return

        logger.debug("等待 %s 并发许可", resource_type)
        async with capacity.acquire():
            yield

    def get_status(self) -> dict[str, dict[str, int]]:
        """返回各资源类型的并发状态。"""
        return {
            name: self._capacities[name].status()
            for name in self._limits
        }


@lru_cache
def get_concurrency_governor() -> ConcurrencyGovernor:
    """获取全局并发治理器单例。"""
    from src.core.settings import get_settings

    settings = get_settings()
    return ConcurrencyGovernor(
        sql_concurrency=settings.query_gateway_sql_active_concurrency,
        sql_wait_queue_size=settings.query_gateway_sql_wait_queue_size,
        sql_wait_timeout_seconds=settings.query_gateway_sql_wait_timeout_seconds,
    )
