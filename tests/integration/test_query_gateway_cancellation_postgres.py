"""Docker PG17 contract for active QueryGateway cancellation and reuse."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.infra.governance.semaphore import ConcurrencyGovernor

ROOT = Path(__file__).resolve().parents[2]
RUN_INTEGRATION = os.environ.get("TTAI_RUN_POSTGRES_INTEGRATION") == "1"
PG_IMAGE = "postgres:17-alpine"
DATABASE = "ttai_cancel"
OWNER = "cancel_owner"
OWNER_PASSWORD = "cancel_owner_pw"
READER = "cancel_reader"
READER_PASSWORD = "cancel_reader_pw"

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        not RUN_INTEGRATION,
        reason="set TTAI_RUN_POSTGRES_INTEGRATION=1 to run Docker PostgreSQL contracts",
    ),
]


class _AuditCapture:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    async def record_sql_start(
        self, trace_id: str, sql: str, **attributes: Any
    ) -> None:
        self.starts.append(
            {"trace_id": trace_id, "sql": sql, **attributes}
        )

    async def record_sql_result(
        self, trace_id: str, sql: str, **attributes: Any
    ) -> None:
        self.results.append(
            {"trace_id": trace_id, "sql": sql, **attributes}
        )


class _Postgres:
    def __init__(self, container: str, port: int) -> None:
        self.container = container
        self.port = port

    @property
    def owner_url(self) -> str:
        return (
            f"postgresql+asyncpg://{OWNER}:{OWNER_PASSWORD}"
            f"@127.0.0.1:{self.port}/{DATABASE}"
        )

    @property
    def reader_url(self) -> str:
        return (
            f"postgresql+asyncpg://{READER}:{READER_PASSWORD}"
            f"@127.0.0.1:{self.port}/{DATABASE}"
        )


def _docker(*arguments: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker command failed ({' '.join(arguments[:2])}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _wait_ready(container: str) -> None:
    for _ in range(80):
        ready = _docker(
            "exec",
            container,
            "pg_isready",
            "--host",
            "127.0.0.1",
            "--username",
            OWNER,
            "--dbname",
            DATABASE,
            check=False,
            timeout=10,
        )
        if ready.returncode == 0:
            return
        state = _docker(
            "inspect", "--format", "{{.State.Status}}", container, check=False
        )
        if state.stdout.strip() == "exited":
            pytest.fail(f"PostgreSQL container exited during init: {container}")
        time.sleep(0.25)
    pytest.fail(f"PostgreSQL did not become ready: {container}")


def _published_port(container: str) -> int:
    result = _docker("port", container, "5432/tcp")
    return int(result.stdout.strip().rsplit(":", maxsplit=1)[1])


@pytest.fixture(scope="module")
def postgres() -> Iterator[_Postgres]:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail(f"Docker daemon is unavailable: {info.stderr.strip()}")

    container = f"ttai-p8-cancel-{uuid.uuid4().hex[:10]}"
    _docker(
        "run",
        "--detach",
        "--name",
        container,
        "--env",
        f"POSTGRES_DB={DATABASE}",
        "--env",
        f"POSTGRES_USER={OWNER}",
        "--env",
        f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
        "--publish",
        "127.0.0.1::5432",
        PG_IMAGE,
        timeout=120,
    )
    try:
        _wait_ready(container)
        setup_sql = f"""
            CREATE SCHEMA ai_views;
            CREATE FUNCTION ai_views.cancel_delay(value integer)
            RETURNS integer LANGUAGE plpgsql VOLATILE AS $function$
            BEGIN
                PERFORM pg_sleep(0.2);
                RETURN value;
            END
            $function$;
            CREATE VIEW ai_views.cancel_rows AS
            SELECT ai_views.cancel_delay(value) AS id
            FROM generate_series(1, 1000) AS generated(value);
            CREATE TABLE ai_views.fast_rows (id integer PRIMARY KEY);
            INSERT INTO ai_views.fast_rows VALUES (7);
            CREATE ROLE {READER} LOGIN PASSWORD '{READER_PASSWORD}';
            GRANT CONNECT ON DATABASE {DATABASE} TO {READER};
            GRANT USAGE ON SCHEMA ai_views TO {READER};
            GRANT SELECT ON ALL TABLES IN SCHEMA ai_views TO {READER};
            GRANT EXECUTE ON FUNCTION ai_views.cancel_delay(integer) TO {READER};
        """
        _docker(
            "exec",
            container,
            "psql",
            "--no-psqlrc",
            "--username",
            OWNER,
            "--dbname",
            DATABASE,
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            setup_sql,
        )
        yield _Postgres(container, _published_port(container))
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=30)


async def _wait_until_sleeping(
    connection: Any,
    application_name: str,
    task: asyncio.Task[Any],
    audit: _AuditCapture,
) -> tuple[int, datetime]:
    deadline = asyncio.get_running_loop().time() + 5
    observations: set[tuple[str, str, str, str]] = set()
    while asyncio.get_running_loop().time() < deadline:
        await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        result = await connection.execute(
            text(
                "SELECT pid, backend_start, usename, state, wait_event_type, wait_event, query "
                "FROM pg_stat_activity "
                "WHERE application_name = :application_name "
                "ORDER BY query_start DESC LIMIT 1"
            ),
            {"application_name": application_name},
        )
        row = result.mappings().one_or_none()
        if row is not None:
            observations.add(
                (
                    str(row["state"]),
                    str(row["wait_event_type"]),
                    str(row["wait_event"]),
                    str(row["query"]),
                )
            )
        if (
            row is not None
            and row["wait_event_type"] == "Timeout"
            and row["wait_event"] == "PgSleep"
        ):
            assert row["usename"] == READER
            return int(row["pid"]), row["backend_start"]
        if task.done():
            try:
                query_result: object = task.result()
            except BaseException as exc:
                query_result = type(exc).__name__
            raise AssertionError(
                f"gateway query ended before executing the slow PG function; "
                f"result={query_result!r} starts={audit.starts!r} "
                f"results={audit.results!r} pg_activity={row!r} "
                f"observations={observations!r}"
            )
        await asyncio.sleep(0.02)
    activity_result = await connection.execute(
        text(
            "SELECT pid, application_name, state, wait_event_type, wait_event, query "
            "FROM pg_stat_activity WHERE datname = current_database() "
            "ORDER BY query_start DESC"
        )
    )
    raise AssertionError(
        "gateway query did not enter the expected PG sleep; "
        f"starts={audit.starts!r} results={audit.results!r} "
        f"pg_activity={activity_result.mappings().all()!r}"
    )


async def _wait_until_recovered(
    connection: Any, backend_identity: tuple[int, datetime]
) -> None:
    backend_pid, backend_start = backend_identity
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        result = await connection.execute(
            text(
                "SELECT state FROM pg_stat_activity "
                "WHERE pid = :backend_pid AND backend_start = :backend_start"
            ),
            {"backend_pid": backend_pid, "backend_start": backend_start},
        )
        state = result.scalar_one_or_none()
        if state is None or state == "idle":
            return
        await asyncio.sleep(0.02)
    await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
    active = await connection.execute(
        text(
            "SELECT state, wait_event_type, wait_event, query FROM pg_stat_activity "
            "WHERE pid = :backend_pid"
        ),
        {"backend_pid": backend_pid},
    )
    raise AssertionError(
        "cancelled QueryGateway backend was not rolled back or discarded: "
        f"{active.mappings().one_or_none()!r}"
    )


@pytest.mark.asyncio
async def test_cancelled_active_pg_query_discards_connection_and_releases_capacity(
    postgres: _Postgres,
) -> None:
    application_name = f"ttai-p8-cancel-{uuid.uuid4().hex[:8]}"
    audit = _AuditCapture()
    governor = ConcurrencyGovernor(
        sql_concurrency=1,
        sql_wait_queue_size=1,
        sql_wait_timeout_seconds=2,
    )
    engine = create_async_engine(
        postgres.reader_url,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"application_name": application_name}},
    )
    cancellation_engine = create_async_engine(
        postgres.reader_url,
        pool_size=1,
        max_overflow=0,
        connect_args={
            "server_settings": {
                "application_name": f"{application_name}-cancel"
            }
        },
    )
    owner_engine = create_async_engine(postgres.owner_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    gateway = QueryGateway(
        sessions,
        schema="ai_views",
        timeout_seconds=15,
        plan_timeout_ms=10_000,
        lock_timeout_ms=8_000,
        max_plan_cost=1_000_000,
        max_plan_rows=10_000,
        max_rows=20,
        audit_sink=audit,
        concurrency_governor=governor,
        cancellation_engine=cancellation_engine,
    )

    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
        async with owner_engine.connect() as observer:
            task = asyncio.create_task(
                gateway.execute("SELECT id FROM ai_views.cancel_rows", trace_id="cancel-pg17")
            )
            try:
                backend_identity = await _wait_until_sleeping(
                    observer, application_name, task, audit
                )
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                await _wait_until_recovered(observer, backend_identity)
            finally:
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

        assert governor.get_status()["sql"]["active"] == 0
        assert governor.get_status()["sql"]["waiting"] == 0
        assert len(audit.starts) == 1
        assert len(audit.results) == 1
        assert audit.results[0]["accepted"] is False
        assert audit.results[0]["error_code"] == "cancelled"

        # The same one-connection pool must be reusable after cancellation and
        # rollback; no aborted transaction or leaked SQL slot may remain.
        receipt = await gateway.execute("SELECT id FROM ai_views.fast_rows")
        assert receipt.accepted is True
        assert receipt.rows == [{"id": 7}]
        assert governor.get_status()["sql"]["active"] == 0
    finally:
        await engine.dispose()
        await cancellation_engine.dispose()
        await owner_engine.dispose()
