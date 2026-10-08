"""Error attribution for the local-real probes.

A probe failure must be diagnosable - driver class, SQLSTATE and a short reason
are what separate a statement timeout from a lock timeout, a connection failure
or a privilege error - and it must never carry the statement text, the bound
parameters, a DSN credential or business row data into the log.  The fail-closed
return values must not change.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asyncpg.exceptions import QueryCanceledError
from sqlalchemy.exc import DBAPIError

from src.nl2sql.local_real import live_probe

_SQL_TEXT = "SELECT id FROM ai_views.complaint_orders WHERE probe_marker = :probe_marker"
_BUSINESS_ROW_MARKER = "business-row-marker"
_DSN_PASSWORD = "Sup3rS3cret-Probe-Pw"  # gitleaks:allow (synthetic probe value)
_LOGGER = "src.nl2sql.local_real.live_probe"


def _factory(_session: object) -> Any:
    return lambda: _session


def _failing(failure: BaseException) -> Any:
    """A bounded read-only session whose probe statement raises failure."""

    class _Session:
        async def execute(self, *_args: object, **_kwargs: object) -> Any:
            raise failure

    @asynccontextmanager
    async def _bounded(_session_factory: object) -> Any:
        yield _Session()

    return _bounded


def _statement_timeout() -> DBAPIError:
    """The exact shape SQLAlchemy renders for a cancelled statement."""

    return DBAPIError.instance(
        statement=_SQL_TEXT,
        params={"probe_marker": _BUSINESS_ROW_MARKER},
        orig=QueryCanceledError("canceling statement due to statement timeout"),
        dbapi_base_err=Exception,
    )


def _failure_log(caplog: pytest.LogCaptureFixture) -> str:
    records = [
        record.getMessage()
        for record in caplog.records
        if "probe failed" in record.getMessage()
    ]
    assert len(records) == 1, records
    return records[0]


@pytest.mark.asyncio
async def test_statement_timeout_is_attributed_without_leaking_sql_or_rows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        live_probe, "bounded_read_only_session", _failing(_statement_timeout())
    )
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        result = await live_probe.resolve_latest_authoritative_date(_factory(object()))
    # Fail-closed behaviour is unchanged.
    assert result == live_probe.LatestDateResult(
        latest_date=None, denominator_rows=0, resolved=False
    )
    message = _failure_log(caplog)
    assert "DBAPIError" in message
    assert "QueryCanceledError" in message
    assert "sqlstate=57014" in message
    assert "canceling statement due to statement timeout" in message
    # SQLAlchemy appends "[SQL: ...]" and "[parameters: ...]"; neither survives.
    assert _SQL_TEXT not in message
    assert _BUSINESS_ROW_MARKER not in message


@pytest.mark.asyncio
async def test_dsn_credentials_never_reach_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = RuntimeError(
        "could not connect to "
        f"postgresql://probe_user:{_DSN_PASSWORD}@10.0.0.9:5432/business"
    )
    monkeypatch.setattr(live_probe, "bounded_read_only_session", _failing(failure))
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        result = await live_probe.resolve_source_watermark(_factory(object()))
    assert result == live_probe.SourceWatermarkResult(data_as_of=None, resolved=False)
    message = _failure_log(caplog)
    assert _DSN_PASSWORD not in message
    assert "probe_user:" not in message


@pytest.mark.asyncio
async def test_a_hostile_exception_cannot_mask_the_fail_closed_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Diagnostics must never replace the failure they describe."""

    class _Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no string form")

    monkeypatch.setattr(live_probe, "bounded_read_only_session", _failing(_Hostile()))
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        result = await live_probe.resolve_recent_authoritative_dates(_factory(object()))
    assert result == live_probe.RecentDateResult(dates=(), resolved=False)
    assert "_Hostile" in _failure_log(caplog)
