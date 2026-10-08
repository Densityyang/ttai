"""Bounded, read-only live probes for the local-real case (NOT the KPI path).

Two probes live here, both strictly SELECT-only and bounded:

1. relation availability + required columns for the frozen closure;
2. the latest business date that can actually compute the DENOMINATOR.

Neither probe returns business rows and neither is canonical metric authority.
The KPI VALUE itself always comes from the typed metric execution path through
MetricQueryCompiler -> QueryGateway -> PlanExecutor.  In particular there is no
hand-written metric runner and no Python recomputation of the KPI here.

Every probe runs inside an EXPLICIT bounded READ ONLY transaction:

    SET TRANSACTION READ ONLY
    SET LOCAL statement_timeout = ...
    SET LOCAL lock_timeout = ...

and the transaction is closed by the context manager on every path, including
failure.  No probe ever returns an open connection to its caller.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.nl2sql.local_real.deployment import (
    FALLBACK_PHYSICAL_RELATION,
    PREFERRED_PUBLISHED_RELATION,
    RELATION_STATUS_NOT_PROBED,
    RELATION_STATUS_PUBLISHED_VIEW_READY,
    RELATION_STATUS_SOURCE_MISSING,
    RELATION_STATUS_VIEW_MISSING_SOURCE_PRESENT,
    LocalRealRelationProbe,
    missing_required_columns,
)

logger = logging.getLogger(__name__)

# Bounded statement/lock budgets for the probes: a governance probe must never
# hold a session open or scan an unbounded relation.
PROBE_STATEMENT_TIMEOUT_MS = 5_000
PROBE_LOCK_TIMEOUT_MS = 2_000

# Business time column of the frozen case (canonical time_column: report_time).
FROZEN_CASE_BUSINESS_TIME_COLUMN = "report_time"
FROZEN_CASE_COUNT_COLUMN = "id"

# The ONLY relations this frozen one-case lane may probe.  Arbitrary
# caller-provided schema/table text never enters these probes.
ALLOWED_PROBE_RELATIONS: frozenset[str] = frozenset(
    {PREFERRED_PUBLISHED_RELATION, FALLBACK_PHYSICAL_RELATION}
)


class LocalRealProbeError(RuntimeError):
    """A probe refused to run against an unapproved relation."""


@dataclass(frozen=True, slots=True)
class LatestDateResult:
    """The resolved latest computable business date, with its eligibility proof."""

    latest_date: str | None
    denominator_rows: int
    resolved: bool


@dataclass(frozen=True, slots=True)
class RecentDateResult:
    """Bounded available business dates, without returning business values."""

    dates: tuple[str, ...]
    resolved: bool


@dataclass(frozen=True, slots=True)
class SourceWatermarkResult:
    """The observed source timestamp, without returning business rows."""

    data_as_of: datetime | None
    resolved: bool
    observed_at: datetime | None = None


def _require_allowed_relation(relation_id: str) -> str:
    """Fail closed unless the relation is EXACTLY one of the approved ones.

    Exact allowlist equality rather than identifier sanitisation: this lane has
    one frozen case, so no schema/table text from a request, a model or a user
    may ever reach the probe SQL.
    """

    if relation_id not in ALLOWED_PROBE_RELATIONS:
        raise LocalRealProbeError("local-real probe relation is not approved")
    schema_name, _, relation_name = relation_id.partition(".")
    if not schema_name or not relation_name:
        raise LocalRealProbeError("local-real probe relation is malformed")
    return relation_id


@asynccontextmanager
async def bounded_read_only_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session inside an explicit bounded READ ONLY transaction.

    The transaction is begun explicitly, marked READ ONLY, and given bounded
    statement and lock timeouts.  This context manager OWNS the connection and
    always closes the transaction, including on failure, so no caller can leak an
    open connection or inherit a writable or unbounded session.
    """

    session = session_factory()
    try:
        transaction = await session.begin()
        try:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(
                text(f"SET LOCAL statement_timeout = {int(PROBE_STATEMENT_TIMEOUT_MS)}")
            )
            await session.execute(
                text(f"SET LOCAL lock_timeout = {int(PROBE_LOCK_TIMEOUT_MS)}")
            )
            yield session
        finally:
            # Explicit on EVERY path: a probe never leaves a transaction open.
            if transaction.is_active:
                await transaction.rollback()
    finally:
        await session.close()


async def _relation_columns(
    session: AsyncSession, qualified_relation: str
) -> tuple[str, ...] | None:
    """Visible column names of an APPROVED relation, or None when absent."""

    _require_allowed_relation(qualified_relation)
    schema_name, _, relation_name = qualified_relation.partition(".")
    rows = (
        await session.execute(
            text(
                """
                SELECT attribute.attname
                FROM pg_catalog.pg_class relation
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_attribute attribute
                  ON attribute.attrelid = relation.oid
                WHERE namespace.nspname = :schema_name
                  AND relation.relname = :relation_name
                  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND attribute.attnum > 0
                  AND attribute.attisdropped = false
                ORDER BY attribute.attnum
                """
            ),
            {"schema_name": schema_name, "relation_name": relation_name},
        )
    ).scalars().all()
    if not rows:
        return None
    return tuple(str(name) for name in rows)


async def probe_relation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    catalog_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> LocalRealRelationProbe:
    """Determine whether the published view exists and carries required columns.

    The preferred published view is checked FIRST.  If it is absent but the Silver
    source exists, the precise gap PUBLISHED_VIEW_MISSING_SOURCE_PRESENT is
    reported - this adapter NEVER silently falls back to direct-table metric SQL.
    """

    catalog = catalog_session_factory or session_factory
    try:
        async with bounded_read_only_session(catalog) as session:
            view_columns = await _relation_columns(session, PREFERRED_PUBLISHED_RELATION)
            source_columns = (
                None
                if view_columns is not None
                else await _relation_columns(session, FALLBACK_PHYSICAL_RELATION)
            )
    except Exception as exc:
        logger.error("local-real relation probe failed: %s", type(exc).__name__)
        return LocalRealRelationProbe(status=RELATION_STATUS_NOT_PROBED)
    if view_columns is not None:
        missing = missing_required_columns(view_columns)
        if missing:
            return LocalRealRelationProbe(
                status=RELATION_STATUS_SOURCE_MISSING,
                missing_columns=missing,
            )
        return LocalRealRelationProbe(status=RELATION_STATUS_PUBLISHED_VIEW_READY)
    if source_columns is None:
        return LocalRealRelationProbe(status=RELATION_STATUS_SOURCE_MISSING)
    # The source exists, so the gap is specifically the missing PUBLISHED view.
    return LocalRealRelationProbe(
        status=RELATION_STATUS_VIEW_MISSING_SOURCE_PRESENT,
        missing_columns=missing_required_columns(source_columns),
    )


async def resolve_latest_authoritative_date(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    relation_id: str = PREFERRED_PUBLISHED_RELATION,
) -> LatestDateResult:
    """The latest business date whose DENOMINATOR is actually computable.

    The denominator requires is_valid_for_metrics = true AND
    completion_receipt_time IS NOT NULL, at the canonical business time
    (report_time).  A date whose denominator is empty is skipped, so the caller
    never receives a date that would produce an undefined metric, and the
    returned count is the ACTUAL denominator row count for that date - never the
    row count of the grouped subquery.

    This is a DATA-AVAILABILITY probe only: it returns a date and a count, never
    business rows, and it is never treated as metric authority.
    """

    qualified = _require_allowed_relation(relation_id)
    statement = text(
        f"""
        SELECT
          to_char(business_date, 'YYYY-MM-DD') AS latest_date,
          denominator_rows
        FROM (
            SELECT
              date_trunc('day', {FROZEN_CASE_BUSINESS_TIME_COLUMN}) AS business_date,
              count("{FROZEN_CASE_COUNT_COLUMN}") AS denominator_rows
            FROM {qualified}
            WHERE is_valid_for_metrics IS TRUE
              AND completion_receipt_time IS NOT NULL
              AND {FROZEN_CASE_BUSINESS_TIME_COLUMN} IS NOT NULL
            GROUP BY 1
            HAVING count("{FROZEN_CASE_COUNT_COLUMN}") > 0
            ORDER BY 1 DESC
            LIMIT 1
        ) AS candidate
        """
    )
    try:
        async with bounded_read_only_session(session_factory) as session:
            row = (await session.execute(statement)).mappings().one_or_none()
    except Exception as exc:
        logger.error("local-real latest-date probe failed: %s", type(exc).__name__)
        return LatestDateResult(latest_date=None, denominator_rows=0, resolved=False)
    if row is None or row["latest_date"] is None:
        return LatestDateResult(latest_date=None, denominator_rows=0, resolved=False)
    denominator_rows = int(row["denominator_rows"])
    # resolved=True ONLY when the chosen date genuinely bears denominator rows.
    if denominator_rows <= 0:
        return LatestDateResult(latest_date=None, denominator_rows=0, resolved=False)
    return LatestDateResult(
        latest_date=str(row["latest_date"]),
        denominator_rows=denominator_rows,
        resolved=True,
    )


async def resolve_recent_authoritative_dates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    relation_id: str = PREFERRED_PUBLISHED_RELATION,
    limit: int = 7,
) -> RecentDateResult:
    """Return the newest bounded dates with a valid denominator population."""

    if not 1 <= limit <= 31:
        raise ValueError("recent date limit must be between 1 and 31")
    qualified = _require_allowed_relation(relation_id)
    statement = text(
        f"""
        SELECT to_char(business_date, 'YYYY-MM-DD') AS business_date
        FROM (
            SELECT
              date_trunc('day', {FROZEN_CASE_BUSINESS_TIME_COLUMN}) AS business_date,
              count("{FROZEN_CASE_COUNT_COLUMN}") AS denominator_rows
            FROM {qualified}
            WHERE is_valid_for_metrics IS TRUE
              AND completion_receipt_time IS NOT NULL
              AND {FROZEN_CASE_BUSINESS_TIME_COLUMN} IS NOT NULL
            GROUP BY 1
            HAVING count("{FROZEN_CASE_COUNT_COLUMN}") > 0
            ORDER BY 1 DESC
            LIMIT :limit
        ) AS available_dates
        ORDER BY business_date ASC
        """
    )
    try:
        async with bounded_read_only_session(session_factory) as session:
            rows = (await session.execute(statement, {"limit": limit})).mappings().all()
    except Exception as exc:
        logger.error("local-real recent-date probe failed: %s", type(exc).__name__)
        return RecentDateResult(dates=(), resolved=False)
    dates = tuple(str(row["business_date"]) for row in rows if row["business_date"])
    return RecentDateResult(dates=dates, resolved=bool(dates))


async def resolve_source_watermark(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    relation_id: str = PREFERRED_PUBLISHED_RELATION,
) -> SourceWatermarkResult:
    """Return the observed max canonical business timestamp for valid data."""

    qualified = _require_allowed_relation(relation_id)
    statement = text(
        f"""
        SELECT MAX({FROZEN_CASE_BUSINESS_TIME_COLUMN}) AS source_watermark
        FROM {qualified}
        WHERE is_valid_for_metrics IS TRUE
          AND completion_receipt_time IS NOT NULL
          AND {FROZEN_CASE_BUSINESS_TIME_COLUMN} IS NOT NULL
          AND {FROZEN_CASE_COUNT_COLUMN} IS NOT NULL
        """
    )
    try:
        async with bounded_read_only_session(session_factory) as session:
            row = (await session.execute(statement)).mappings().one_or_none()
    except Exception as exc:
        logger.error("local-real source watermark probe failed: %s", type(exc).__name__)
        return SourceWatermarkResult(data_as_of=None, resolved=False)
    watermark = row["source_watermark"] if row is not None else None
    if not isinstance(watermark, datetime):
        return SourceWatermarkResult(data_as_of=None, resolved=False)
    return SourceWatermarkResult(
        data_as_of=watermark,
        resolved=True,
        observed_at=datetime.now(UTC),
    )


__all__ = [
    "ALLOWED_PROBE_RELATIONS",
    "FROZEN_CASE_BUSINESS_TIME_COLUMN",
    "FROZEN_CASE_COUNT_COLUMN",
    "LatestDateResult",
    "RecentDateResult",
    "SourceWatermarkResult",
    "LocalRealProbeError",
    "PROBE_LOCK_TIMEOUT_MS",
    "PROBE_STATEMENT_TIMEOUT_MS",
    "bounded_read_only_session",
    "probe_relation",
    "resolve_latest_authoritative_date",
    "resolve_recent_authoritative_dates",
    "resolve_source_watermark",
]
