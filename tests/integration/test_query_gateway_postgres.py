"""Real PostgreSQL contracts for the PR4 QueryGateway.

The module is opt-in because it creates one isolated Docker container.  Run it
with ``TTAI_RUN_POSTGRES_INTEGRATION=1``; cleanup targets only the UUID-named
container created by this fixture.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.nl2sql.contracts import BoundFilter, TimeRange
from src.nl2sql.infra.governance.query_gateway import (
    QueryErrorCode,
    QueryGateway,
    _sqlstate,
)
from src.nl2sql.infra.store.database import DatabaseManager
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.metric_query import metric_plan_executor
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.metric_layer import MetricSemanticLayer
from tests.metric_fixtures import MetricAuthority, ratio_contract, seed_contract

RUN_INTEGRATION = os.environ.get("TTAI_RUN_POSTGRES_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        not RUN_INTEGRATION,
        reason="set TTAI_RUN_POSTGRES_INTEGRATION=1 to run Docker PostgreSQL contracts",
    ),
]


def _docker(
    *arguments: str,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        rendered = " ".join(arguments[:2])
        raise AssertionError(
            f"docker command failed ({rendered}):\n{result.stdout}\n{result.stderr}"
        )
    return result


def _wait_for_postgres(container: str, owner: str, database: str) -> None:
    for _ in range(90):
        state = _docker("inspect", "--format", "{{.State.Status}}", container, check=False)
        if state.stdout.strip() == "exited":
            pytest.fail(f"PostgreSQL container exited during init:\n{_container_logs(container)}")
        logs = _container_logs(container)
        if "PostgreSQL init process complete; ready for start up." in logs:
            ready = _docker(
                "exec",
                container,
                "psql",
                "--no-psqlrc",
                "--username",
                owner,
                "--dbname",
                database,
                "--tuples-only",
                "--command",
                "SELECT 1",
                check=False,
                timeout=10,
            )
            if ready.returncode == 0:
                return
        time.sleep(0.5)
    pytest.fail(f"PostgreSQL did not become ready:\n{_container_logs(container)}")


def _container_logs(container: str) -> str:
    result = _docker("logs", container, check=False)
    return f"{result.stdout}{result.stderr}"


def _published_port(container: str) -> int:
    result = _docker("port", container, "5432/tcp")
    return int(result.stdout.strip().rsplit(":", maxsplit=1)[1])


def _async_url(role: str, password: str, port: int, database: str) -> str:
    return (
        f"postgresql+asyncpg://{quote(role, safe='')}:{quote(password, safe='')}"
        f"@127.0.0.1:{port}/{quote(database, safe='')}"
    )


@dataclass(frozen=True)
class _GatewayPostgres:
    reader_url: str = field(repr=False)
    probe_url: str = field(repr=False)


@pytest.fixture(scope="module")
def gateway_postgres() -> Iterator[_GatewayPostgres]:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail(f"Docker daemon is unavailable: {info.stderr.strip()}")

    suffix = uuid.uuid4().hex[:12]
    container = f"ttai-pr4-gateway-{suffix}"
    database = "ttai_gateway"
    owner = "gateway_owner"
    reader = "business_reader"
    probe = "readwrite_probe"
    owner_password = secrets.token_hex(24)
    reader_password = secrets.token_hex(24)
    probe_password = secrets.token_hex(24)

    try:
        _docker(
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_DB={database}",
            "--env",
            f"POSTGRES_USER={owner}",
            "--env",
            f"POSTGRES_PASSWORD={owner_password}",
            "postgres:17-alpine",
            timeout=120,
        )
        _wait_for_postgres(container, owner, database)
        setup_sql = f"""
        CREATE SCHEMA ai_views;
        CREATE TABLE ai_views.orders (
            id integer PRIMARY KEY,
            customer_name text NOT NULL,
            api_key text NOT NULL
        );
        INSERT INTO ai_views.orders VALUES
            (1, 'alpha', 'alpha-secret'),
            (2, 'beta', 'beta-secret'),
            (3, 'gamma', 'gamma-secret');
        CREATE TABLE public.private_orders (id integer PRIMARY KEY);
        INSERT INTO public.private_orders VALUES (99);
        CREATE TABLE ai_views.v_metric_result (
            metric_code text NOT NULL,
            time_grain text NOT NULL,
            time_value text NOT NULL,
            value numeric NOT NULL,
            value_type text,
            unit text,
            dimension_type text
        );
        INSERT INTO ai_views.v_metric_result VALUES
            ('revenue', 'day', '2026-07-01', 10.5, 'decimal', 'CNY', 'overall'),
            ('revenue', 'day', '2026-07-02', 20.5, 'decimal', 'CNY', 'overall'),
            ('orders', 'day', '2026-07-01', 3, 'integer', 'count', 'overall');
        CREATE TABLE ai_views.complaint_orders (
            acceptance_time timestamptz,
            completion_time timestamptz,
            is_valid_for_metrics boolean,
            has_valid_bandwidth boolean,
            category text,
            priority integer,
            area text,
            team text,
            is_first_response_on_time boolean,
            area_id text,
            team_id integer
        );
        CREATE INDEX complaint_time ON ai_views.complaint_orders (acceptance_time);
        -- Generated synthetic cases: Shanghai leap-day boundaries, NULL flags,
        -- finished rows, and a city placeholder with NULL area/team.
        INSERT INTO ai_views.complaint_orders
            (acceptance_time, completion_time, is_valid_for_metrics,
             has_valid_bandwidth, category, priority, area, team)
        SELECT CASE n
            WHEN 1 THEN '2024-02-28 15:59:59+00'::timestamptz
            WHEN 3 THEN '2024-02-29 15:59:59+00'::timestamptz
            WHEN 4 THEN '2024-02-29 16:00:00+00'::timestamptz
            WHEN 10 THEN NULL
            ELSE '2024-02-28 16:00:00+00'::timestamptz END,
            CASE WHEN n = 7 THEN '2024-03-01 00:00:00+00'::timestamptz ELSE NULL END,
            CASE WHEN n = 8 THEN NULL ELSE n <> 5 END,
            CASE WHEN n = 9 THEN NULL ELSE n <> 6 END,
            'synthetic', 1,
            CASE WHEN n = 11 THEN NULL ELSE 'synthetic-area' END,
            CASE WHEN n = 11 THEN NULL ELSE 'synthetic-team' END
        FROM generate_series(1, 11) AS generated(n);
        -- Slice 2 uses another leap year so Slice 1 expectations stay intact.
        -- Stable synthetic IDs only; n=6 is the city-company placeholder.
        INSERT INTO ai_views.complaint_orders
            (acceptance_time, completion_time, is_valid_for_metrics,
             has_valid_bandwidth, is_first_response_on_time, area_id, team_id)
        SELECT CASE n
            WHEN 1 THEN '2028-02-28 15:59:59+00'::timestamptz
            WHEN 3 THEN '2028-02-29 15:59:59+00'::timestamptz
            WHEN 11 THEN '2028-02-29 16:00:00+00'::timestamptz
            WHEN 12 THEN NULL
            ELSE '2028-02-28 16:00:00+00'::timestamptz END,
            CASE WHEN n = 13 THEN NULL ELSE '2028-03-02 00:00:00+00'::timestamptz END,
            CASE WHEN n = 8 THEN NULL ELSE n <> 7 END,
            CASE WHEN n = 10 THEN NULL ELSE n <> 9 END,
            CASE WHEN n = 14 THEN NULL ELSE n NOT IN (3, 15) END,
            CASE WHEN n = 6 THEN NULL
                 WHEN n IN (1, 2, 3, 11, 12) THEN 'area-a'
                 WHEN n = 4 THEN 'area-b' WHEN n = 5 THEN 'area-c'
                 WHEN n IN (14, 15) THEN 'area-d' ELSE 'area-e' END,
            CASE WHEN n = 6 THEN NULL
                 WHEN n IN (1, 2, 3, 11, 12) THEN 1
                 WHEN n = 4 THEN 2 WHEN n = 5 THEN 3
                 WHEN n IN (14, 15) THEN 4 ELSE 5 END
        FROM generate_series(1, 15) AS generated(n);
        ANALYZE ai_views.complaint_orders;
        CREATE ROLE {reader} LOGIN PASSWORD '{reader_password}';
        CREATE ROLE {probe} LOGIN PASSWORD '{probe_password}';
        GRANT CONNECT ON DATABASE {database} TO {reader}, {probe};
        GRANT USAGE ON SCHEMA ai_views TO {reader}, {probe};
        GRANT SELECT ON ALL TABLES IN SCHEMA ai_views TO {reader};
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ai_views TO {probe};
        ANALYZE ai_views.orders;
        """
        _docker(
            "exec",
            container,
            "psql",
            "--no-psqlrc",
            "--username",
            owner,
            "--dbname",
            database,
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            setup_sql,
        )
        port = _published_port(container)
        yield _GatewayPostgres(
            reader_url=_async_url(reader, reader_password, port, database),
            probe_url=_async_url(probe, probe_password, port, database),
        )
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=30)


def _gateway(
    url: str,
    **limits: Any,
) -> tuple[Any, QueryGateway]:
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, QueryGateway(
        sessions,
        schema="ai_views",
        readonly_role="business_reader",
        **limits,
    )


@pytest.mark.asyncio
async def test_real_postgres_parameter_binding_masking_and_read_only_receipt(
    gateway_postgres: _GatewayPostgres,
) -> None:
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=10)
    try:
        receipt = await gateway.execute(
            "SELECT id, customer_name, api_key FROM orders WHERE id = :id",
            {"id": 2},
        )
        read_only = await gateway.execute(
            "SELECT current_setting('transaction_read_only') AS read_only"
        )
        literal_colon = await gateway.execute(
            "SELECT CAST(:bound AS integer) AS bound, "
            "':not_a_bind' AS literal /* :comment */",
            {"bound": 7},
        )
    finally:
        await engine.dispose()

    assert receipt.accepted
    assert receipt.rows == [
        {"id": 2, "customer_name": "beta", "api_key": "[REDACTED]"}
    ]
    assert receipt.masked_columns == ("api_key",)
    assert receipt.estimated_cost is not None
    assert receipt.sql_fingerprint
    assert read_only.accepted
    assert read_only.rows == [{"read_only": "on"}]
    assert literal_colon.accepted
    assert literal_colon.rows == [{"bound": 7, "literal": ":not_a_bind"}]


async def _slice2_execute(
    gateway: QueryGateway, authority: MetricAuthority, **changes: Any,
) -> Any:
    plan = authority.plan(time_range=TimeRange(start=date(2028, 2, 29), end=date(2028, 2, 29)), **changes)
    validator = PlanValidator()
    validation = validator.validate_query_plan(plan=plan, context=authority.context, identity=authority.identity)
    execution = PlanCompiler().compile(plan=plan, context=authority.context, validation=validation)
    budget = RouteBudgetLedger(route="standard")
    assert validator.validate_execution_plan(
        execution_plan=execution, query_plan=plan, context=authority.context, route_budget=budget.limits,
    ).outcome == "allow"
    result = await metric_plan_executor(authority.compiler(), gateway).execute(
        query_plan=plan, context=authority.context, execution_plan=execution, budget=budget, deadline_ms=10000,
    )
    assert budget.sql_executions == 1
    return result


def _organization_filter(dimension: str, value: Any, operator: str = "eq") -> BoundFilter:
    return BoundFilter.model_validate(dict(
        field_ref=dimension, value=value, operator=operator, source="entity_alias",
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize(("filters", "expected"), [
    ((), {"numerator": 4, "denominator": 7, "value": "57.14", "status": "success"}),
    ((_organization_filter("area", "area-d"),), {"numerator": 0, "denominator": 2, "value": "0.00", "status": "success"}),
    ((_organization_filter("team", 5),), {"numerator": 0, "denominator": 0, "value": None, "status": "no_data"}),
    ((_organization_filter("area", "unknown-id"),), {"numerator": 0, "denominator": 0, "value": None, "status": "no_data"}),
    ((_organization_filter("area", ["area-a", "area-b"], "in"),), {"numerator": 2, "denominator": 3, "value": "66.67", "status": "success"}),
    ((_organization_filter("team", [1, 2], "in"),), {"numerator": 2, "denominator": 3, "value": "66.67", "status": "success"}),
    ((_organization_filter("area", "成都"),), {"numerator": 0, "denominator": 0, "value": None, "status": "no_data"}),
    ((_organization_filter("area", ["x'; DROP TABLE complaint_orders; --"], "in"),), {"numerator": 0, "denominator": 0, "value": None, "status": "no_data"}),
])
async def test_pr07a_ratio_real_gateway_scalar_nulls_and_binds(
    gateway_postgres: _GatewayPostgres, filters: tuple[BoundFilter, ...], expected: dict[str, Any],
) -> None:
    authority = MetricAuthority(ratio_contract())
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=200)
    try:
        result = await _slice2_execute(gateway, authority, filters=filters)
        assert result.record.status == "succeeded", result.record
        assert result.outputs["fetch_metrics"] == {"rows": [expected], "no_data": expected["status"] == "no_data"}
        receipt = result.record.step_receipts[0]
        assert receipt.rowset_sha256 is not None
        assert receipt.data_as_of is None and receipt.freshness_status == "unknown"
        checkpoint = result.record.model_dump_json()
        assert all(token not in checkpoint for token in ("SELECT", "filter_0", "start_at", "area-a", "57.14"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["area", "team"])
@pytest.mark.parametrize("intent", ["comparison", "ranking"])
async def test_pr07a_ratio_real_gateway_organizations_and_ranking_ties(
    gateway_postgres: _GatewayPostgres, dimension: str, intent: str,
) -> None:
    authority = MetricAuthority(ratio_contract())
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=200)
    try:
        result = await _slice2_execute(gateway, authority, intent=intent, dimensions=(dimension,),
                                       **({"result_limit": 2} if intent == "ranking" else {}))
        assert result.record.status == "succeeded", result.record
        rows = result.outputs["fetch_metrics"]["rows"]
        ids = ["area-a", "area-b", "area-c", "area-d", "area-e"] if dimension == "area" else [1, 2, 3, 4, 5]
        if intent == "ranking":
            assert [row["dimension_id"] for row in rows] == ids[1:3]
            assert [row["value"] for row in rows] == ["100.00", "100.00"]
        else:
            assert [row["dimension_id"] for row in rows] == ids
            assert [row["value"] for row in rows] == ["50.00", "100.00", "100.00", "0.00", None]
            assert sum(row["denominator"] for row in rows) == 6  # city total = 7
            assert sum(row["numerator"] for row in rows) == 3  # city total = 4
            assert rows[-1]["status"] == "no_data"
        assert not result.outputs["fetch_metrics"]["no_data"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("grain", "start", "end", "expected"), [
    ("day", date(2028, 2, 28), date(2028, 3, 1), [
        ("2028-02-28T00:00:00", 1, 1, "100.00"),
        ("2028-02-29T00:00:00", 4, 7, "57.14"),
        ("2028-03-01T00:00:00", 1, 1, "100.00"),
    ]),
    ("month", date(2028, 2, 28), date(2028, 3, 1), [
        ("2028-02-01T00:00:00", 5, 8, "62.50"), ("2028-03-01T00:00:00", 1, 1, "100.00"),
    ]),
    ("month", date(2028, 2, 29), date(2028, 2, 29), [("2028-02-01T00:00:00", 4, 7, "57.14")]),
    ("day", date(2028, 1, 1), date(2028, 1, 1), []),
])
async def test_pr07a_ratio_real_gateway_period_attribution_and_no_widening(
    gateway_postgres: _GatewayPostgres, grain: str, start: date, end: date,
    expected: list[tuple[str, int, int, str]],
) -> None:
    authority = MetricAuthority(ratio_contract())
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=200)
    try:
        plan = authority.plan(intent="trend", grain=grain, time_range=TimeRange(start=start, end=end))
        validation = PlanValidator().validate_query_plan(plan=plan, context=authority.context, identity=authority.identity)
        execution = PlanCompiler().compile(plan=plan, context=authority.context, validation=validation)
        result = await metric_plan_executor(authority.compiler(), gateway).execute(
            query_plan=plan, context=authority.context, execution_plan=execution,
            budget=RouteBudgetLedger(route="standard"), deadline_ms=10000,
        )
        assert result.record.status == "succeeded", result.record
        output = result.outputs["fetch_metrics"]
        assert output["rows"] == [dict(period=p, numerator=n, denominator=d, value=v, status="success") for p, n, d, v in expected]
        assert output["no_data"] == (not expected)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["comparison", "ranking"])
async def test_pr07a_count_real_gateway_organization_extension(
    gateway_postgres: _GatewayPostgres, intent: str,
) -> None:
    authority = MetricAuthority(seed_contract(supported_dimensions=("city_company", "area", "team")))
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=200)
    try:
        result = await _slice2_execute(gateway, authority, intent=intent, dimensions=("team",))
        assert result.record.status == "succeeded", result.record
        assert result.outputs["fetch_metrics"] == {"rows": [{"dimension_id": 5, "value": 1}], "no_data": False}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("intent", "limit", "succeeded"), [
    ("comparison", None, False), ("ranking", 2, False), ("ranking", 1, True),
])
async def test_pr07a_real_gateway_row_limit_contact_fails_closed(
    gateway_postgres: _GatewayPostgres, intent: str, limit: int | None, succeeded: bool,
) -> None:
    authority = MetricAuthority(ratio_contract())
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=1)
    try:
        result = await _slice2_execute(gateway, authority, intent=intent, dimensions=("area",),
                                       **({"result_limit": limit} if intent == "ranking" else {}))
        if succeeded:
            assert result.record.status == "succeeded", result.record
            assert result.outputs["fetch_metrics"]["rows"] == [
                dict(dimension_id="area-b", numerator=1, denominator=1,
                     value="100.00", status="success"),
            ]
        else:
            assert result.record.status == "failed"
            assert result.record.stop_reason == "metric_result_may_be_truncated"
            assert result.outputs == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_metric_semantic_layer_parameterized_query_uses_gateway_end_to_end(
    gateway_postgres: _GatewayPostgres,
) -> None:
    manager = DatabaseManager(
        database_url=gateway_postgres.reader_url,
        schema="ai_views",
    )
    await manager.connect()
    try:
        layer = MetricSemanticLayer(manager)
        rows = await layer.query(
            metric_code="revenue",
            time_grain="day",
            start_date="2026-07-01",
            end_date="2026-07-02",
        )
        aggregated = await layer.query(
            metric_code="revenue",
            time_grain="day",
            start_date="2026-07-01",
            end_date="2026-07-02",
            aggregation="sum",
            target_grain="month",
        )
    finally:
        await manager.disconnect()

    assert rows == [
        {
            "period": "2026-07-01",
            "value": 10.5,
            "value_type": "decimal",
            "unit": "CNY",
        },
        {
            "period": "2026-07-02",
            "value": 20.5,
            "value_type": "decimal",
            "unit": "CNY",
        },
    ]
    assert len(aggregated) == 1
    assert aggregated[0]["period"] == "2026-07-01"
    assert float(aggregated[0]["value"]) == 31.0
    assert aggregated[0]["unit"] == "CNY"


@pytest.mark.asyncio
async def test_real_postgres_policy_denies_schema_writes_locks_and_side_effects(
    gateway_postgres: _GatewayPostgres,
) -> None:
    engine, gateway = _gateway(gateway_postgres.reader_url)
    try:
        receipts = [
            await gateway.execute("SELECT * FROM public.private_orders"),
            await gateway.execute("INSERT INTO orders VALUES (4, 'x', 'y')"),
            await gateway.execute("SELECT * FROM orders FOR UPDATE"),
            await gateway.execute("SELECT pg_sleep(0.01)"),
        ]
    finally:
        await engine.dispose()

    assert [receipt.error.code for receipt in receipts if receipt.error] == [
        QueryErrorCode.SCHEMA_DENIED,
        QueryErrorCode.POLICY_DENIED,
        QueryErrorCode.POLICY_DENIED,
        QueryErrorCode.POLICY_DENIED,
    ]


@pytest.mark.asyncio
async def test_real_postgres_explain_and_result_gates_fail_closed(
    gateway_postgres: _GatewayPostgres,
) -> None:
    missing_engine, missing_gateway = _gateway(gateway_postgres.reader_url)
    row_engine, row_gateway = _gateway(
        gateway_postgres.reader_url,
        max_plan_rows=100,
    )
    byte_engine, byte_gateway = _gateway(
        gateway_postgres.reader_url,
        max_result_bytes=100,
    )
    try:
        missing = await missing_gateway.execute("SELECT * FROM missing_relation")
        wide = await row_gateway.execute(
            "SELECT value FROM generate_series(1, 10000) AS value"
        )
        large = await byte_gateway.execute(
            "SELECT repeat('x', 1000) AS payload"
        )
    finally:
        await missing_engine.dispose()
        await row_engine.dispose()
        await byte_engine.dispose()

    assert missing.error is not None
    assert missing.error.code == QueryErrorCode.RELATION_NOT_FOUND
    assert missing.error.message == "requested relation or column was not found"
    assert wide.error is not None
    assert wide.error.code == QueryErrorCode.ROWS_EXCEEDED
    assert large.error is not None
    assert large.error.code == QueryErrorCode.RESULT_TOO_LARGE
    assert large.rows == []


@pytest.mark.asyncio
async def test_real_postgres_transaction_guard_blocks_write_capable_role(
    gateway_postgres: _GatewayPostgres,
) -> None:
    engine = create_async_engine(gateway_postgres.probe_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    gateway = QueryGateway(sessions, schema="ai_views", readonly_role="readwrite_probe")
    try:
        # First prove this role could write when the QueryGateway transaction
        # guard is absent.  Rollback keeps the fixture deterministic.
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO ai_views.orders (id, customer_name, api_key) "
                    "VALUES (900, 'probe', 'probe')"
                )
            )
            await transaction.rollback()

        async with sessions() as session:
            await gateway._begin_read_only(session)
            with pytest.raises(Exception) as exc_info:
                await session.execute(
                    text(
                        "INSERT INTO ai_views.orders (id, customer_name, api_key) "
                        "VALUES (901, 'blocked', 'blocked')"
                    )
                )
            await session.rollback()
    finally:
        await engine.dispose()

    assert _sqlstate(exc_info.value) == "25006"

@pytest.mark.asyncio
@pytest.mark.parametrize(("intent", "grain", "empty", "expected"), [
    ("metric", "day", False, 3),
    ("trend", "day", False, 3),
    ("trend", "month", False, 3),
    ("metric", "day", True, 0),
    ("trend", "day", True, None),
])
async def test_pr07a_metric_executor_real_gateway_contract(
    gateway_postgres: _GatewayPostgres,
    intent: str,
    grain: str,
    empty: bool,
    expected: int | None,
) -> None:
    authority = MetricAuthority()
    engine, gateway = _gateway(gateway_postgres.reader_url, max_rows=200)
    try:
        plan = authority.plan(intent=intent, grain=grain, **(
            {"time_range": TimeRange(start=date(2024, 1, 1), end=date(2024, 1, 1))}
            if empty else {}
        ))
        validator = PlanValidator()
        validation = validator.validate_query_plan(
            plan=plan, context=authority.context, identity=authority.identity,
        )
        execution = PlanCompiler().compile(plan=plan, context=authority.context, validation=validation)
        budget = RouteBudgetLedger(route="standard")
        assert validator.validate_execution_plan(
            execution_plan=execution, query_plan=plan, context=authority.context,
            route_budget=budget.limits,
        ).outcome == "allow"
        result = await metric_plan_executor(authority.compiler(), gateway).execute(
            query_plan=plan, context=authority.context, execution_plan=execution,
            budget=budget, deadline_ms=10000,
        )
        assert result.record.status == "succeeded", result.record
        value = result.outputs["fetch_metrics"]
        assert isinstance(value, dict)
        assert value["no_data"] == (expected is None)
        if expected is None:
            assert value["rows"] == []
        else:
            rows = value["rows"]
            assert isinstance(rows, list) and len(rows) == 1
            row = rows[0]
            assert isinstance(row, dict) and row["value"] == expected
            if intent == "trend":
                assert row["period"] == ("2024-02-29T00:00:00" if grain == "day" else "2024-02-01T00:00:00")
        receipt = result.record.step_receipts[0]
        assert receipt.rowset_sha256 is not None
        assert receipt.freshness_status == "unknown" and receipt.data_as_of is None
        checkpoint = result.record.model_dump_json()
        assert "SELECT" not in checkpoint and "acceptance_time" not in checkpoint
        assert "synthetic-area" not in checkpoint and "start_at" not in checkpoint
        assert budget.sql_executions == 1
    finally:
        await engine.dispose()
