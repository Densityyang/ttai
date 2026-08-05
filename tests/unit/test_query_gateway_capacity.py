from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from pydantic import ValidationError

from src.core.settings import Settings
from src.nl2sql.infra.governance.query_gateway import QueryErrorCode, QueryGateway
from src.nl2sql.infra.governance.semaphore import ConcurrencyGovernor


async def _hold_sql_slot(
    governor: ConcurrencyGovernor,
    entered: asyncio.Event,
    release: asyncio.Event,
) -> None:
    async with governor.acquire("sql"):
        entered.set()
        await release.wait()


async def _wait_for_sql_status(
    governor: ConcurrencyGovernor,
    **expected: int,
) -> None:
    for _ in range(100):
        status = governor.get_status()["sql"]
        if all(status[key] == value for key, value in expected.items()):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"SQL capacity did not reach expected state: {expected}")


def test_api_sql_capacity_defaults_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "QUERY_GATEWAY_SQL_ACTIVE_CONCURRENCY",
        "QUERY_GATEWAY_SQL_WAIT_QUEUE_SIZE",
        "QUERY_GATEWAY_SQL_WAIT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None, auth_enabled=False)  # type: ignore[call-arg]

    assert settings.query_gateway_sql_active_concurrency == 4
    assert settings.query_gateway_sql_wait_queue_size == 8
    assert settings.query_gateway_sql_wait_timeout_seconds == 3.0
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            auth_enabled=False,
            query_gateway_sql_active_concurrency=5,
        )


def test_all_governed_resources_have_explicit_wait_queue_limits() -> None:
    status = ConcurrencyGovernor().get_status()

    assert {
        resource: status[resource]["queue_limit"]
        for resource in ("rag", "sql", "code", "index")
    } == {"rag": 6, "sql": 8, "code": 4, "index": 2}


@pytest.mark.asyncio
async def test_sql_queue_is_bounded_and_queue_full_is_structured() -> None:
    governor = ConcurrencyGovernor(
        sql_concurrency=1,
        sql_wait_queue_size=2,
        sql_wait_timeout_seconds=1,
    )
    release = asyncio.Event()
    holder_entered = asyncio.Event()
    holder = asyncio.create_task(_hold_sql_slot(governor, holder_entered, release))
    await holder_entered.wait()
    queued = [
        asyncio.create_task(_hold_sql_slot(governor, asyncio.Event(), release))
        for _ in range(2)
    ]
    try:
        await _wait_for_sql_status(governor, active=1, waiting=2, queue_limit=2)

        receipt = await QueryGateway(
            cast(Any, None),
            concurrency_governor=governor,
        ).execute("SELECT 1")

        assert receipt.accepted is False
        assert receipt.error is not None
        assert receipt.error.code == QueryErrorCode.CAPACITY_EXCEEDED
        assert receipt.error.retryable is True
        assert governor.get_status()["sql"]["waiting"] == 2
    finally:
        release.set()
        await asyncio.gather(holder, *queued)
    await _wait_for_sql_status(governor, active=0, waiting=0)


@pytest.mark.asyncio
async def test_bootstrap_ninth_sql_request_is_held_in_the_bounded_queue() -> None:
    governor = ConcurrencyGovernor(
        sql_concurrency=4,
        sql_wait_queue_size=8,
        sql_wait_timeout_seconds=1,
    )
    release = asyncio.Event()
    entered = [asyncio.Event() for _ in range(9)]
    holders = [
        asyncio.create_task(_hold_sql_slot(governor, entered[index], release))
        for index in range(9)
    ]
    try:
        await _wait_for_sql_status(governor, active=4, waiting=5)
        assert all(event.is_set() for event in entered[:4])
        assert not any(event.is_set() for event in entered[4:])
    finally:
        release.set()
        await asyncio.gather(*holders)
    await _wait_for_sql_status(governor, active=0, waiting=0)


@pytest.mark.asyncio
async def test_sql_wait_timeout_is_structured_and_releases_queue_slot() -> None:
    governor = ConcurrencyGovernor(
        sql_concurrency=1,
        sql_wait_queue_size=1,
        sql_wait_timeout_seconds=0.01,
    )
    release = asyncio.Event()
    holder_entered = asyncio.Event()
    holder = asyncio.create_task(_hold_sql_slot(governor, holder_entered, release))
    await holder_entered.wait()
    try:
        receipt = await QueryGateway(
            cast(Any, None),
            concurrency_governor=governor,
        ).execute("SELECT 1")

        assert receipt.accepted is False
        assert receipt.error is not None
        assert receipt.error.code == QueryErrorCode.CAPACITY_EXCEEDED
        assert receipt.error.retryable is True
        await _wait_for_sql_status(governor, active=1, waiting=0)
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_cancelled_sql_waiter_propagates_and_does_not_leak_capacity() -> None:
    governor = ConcurrencyGovernor(
        sql_concurrency=1,
        sql_wait_queue_size=1,
        sql_wait_timeout_seconds=1,
    )
    release = asyncio.Event()
    holder_entered = asyncio.Event()
    holder = asyncio.create_task(_hold_sql_slot(governor, holder_entered, release))
    await holder_entered.wait()
    waiting = asyncio.create_task(
        QueryGateway(
            cast(Any, None),
            concurrency_governor=governor,
        ).execute("SELECT 1")
    )
    await _wait_for_sql_status(governor, active=1, waiting=1)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await _wait_for_sql_status(governor, active=1, waiting=0)

    release.set()
    await holder
    async with asyncio.timeout(0.1):
        async with governor.acquire("sql"):
            pass


@pytest.mark.asyncio
async def test_cancelled_active_sql_holder_releases_slot() -> None:
    governor = ConcurrencyGovernor(
        sql_concurrency=1,
        sql_wait_queue_size=1,
        sql_wait_timeout_seconds=1,
    )
    entered = asyncio.Event()
    holder = asyncio.create_task(_hold_sql_slot(governor, entered, asyncio.Event()))
    await entered.wait()

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    await _wait_for_sql_status(governor, active=0, waiting=0)

    async with asyncio.timeout(0.1):
        async with governor.acquire("sql"):
            pass
