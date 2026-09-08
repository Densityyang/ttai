"""Slice 2 contracts: generated authority only, no enterprise access."""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.nl2sql.contracts import BoundFilter, FetchMetricStep, QueryPlan, TimeRange
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.candidates import rowset_sha256
from src.nl2sql.orchestration.execution import PlanStepError
from src.nl2sql.orchestration.metric_query import (
    GatewayMetricStepRunner,
    OrganizationDimensionBinding,
    RelationBinding,
    metric_plan_executor,
)
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    load_metric_catalog,
    metric_catalog_ir,
)
from tests.metric_fixtures import MetricAuthority, ratio_contract, seed_contract


def org_filter(dimension: str = "area", value: Any = "area-a", operator: str = "eq") -> BoundFilter:
    return BoundFilter.model_validate(dict(
        field_ref=dimension, operator=operator, value=value, source="entity_alias",
    ))


def test_empty_organization_in_filter_rejected_by_contract() -> None:
    with pytest.raises(ValidationError):
        org_filter("area", [], "in")


def test_ratio_seed_and_authoring_preserve_target_and_pending_governance() -> None:
    source = Path(__file__).resolve().parents[2] / "config/metrics/complaint.yaml"
    catalog = load_metric_catalog(source.read_text(encoding="utf-8"))
    assert [metric.metric_key for metric in catalog.metrics] == [
        "complaint_in_transit_count", "complaint_first_response_rate",
    ]
    metric = catalog.metrics[1]
    assert metric.release_status == "pending_source" and not metric.benchmark_eligible
    assert metric.owner is None and metric.approver is None and metric.freshness_sla_seconds is None
    assert metric.daily_report_enabled and metric.daily_report_order == 14 and metric.assistant_enabled
    assert metric.business_time_column == "acceptance_time"
    assert metric.ratio is not None and metric.ratio.value_scale == "0_100"
    assert [(p.field, p.operator) for p in metric.ratio.denominator_predicates] == [
        ("has_valid_bandwidth", "is_true"), ("completion_time", "is_not_null"),
    ]
    assert [(p.field, p.operator) for p in metric.ratio.numerator_predicates] == [
        ("is_first_response_on_time", "is_true"),
    ]
    active = ratio_contract()
    ir = metric_catalog_ir(MetricCatalog(metrics=(active,)), relations={"complaint_orders": "ai_views.complaint_orders"})
    assert "is_first_response_on_time" in ir.metrics[0].source_columns
    assert ir.metrics[0].execution_contract == active.model_dump(mode="json")
    other = ratio_contract(metric_key="complaint_other_ratio", formula_version="complaint_other_ratio.v1")
    assert len(load_metric_catalog(MetricCatalog(metrics=(active, other)).model_dump_json()).metrics) == 2


@pytest.mark.parametrize("changes", [
    {"ratio": None}, {"operation": "count"}, {"ratio": {}}, {"formula": "100*n/d"},
    {"supported_dimensions": []}, {"supported_dimensions": ["area", "area"]},
])
def test_strict_operation_contract(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ratio_contract(**changes)


@pytest.mark.parametrize(("field", "value"), [
    ("unit", "count"), ("value_scale", "0_1"), ("decimal_places", 3),
    ("zero_denominator_policy", "zero"), ("denominator_predicates", []),
    ("numerator_predicates", []), ("sql", "SELECT 1"),
    ("numerator_predicates", [{"field": "x", "operator": "eq"}]),
])
def test_ratio_fields_cannot_be_omitted_or_generalized(field: str, value: Any) -> None:
    metric = ratio_contract()
    assert metric.ratio is not None
    payload = metric.ratio.model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        ratio_contract(ratio=payload)
    payload = metric.ratio.model_dump(mode="json")
    payload.pop("unit")
    with pytest.raises(ValidationError):
        ratio_contract(ratio=payload)
    with pytest.raises(ValidationError):
        seed_contract(ratio=metric.ratio)


@pytest.mark.parametrize("payload", [
    {"dimension": "unknown"}, {"dimension": "area"},
    {"dimension": "team", "field": "team_id"},
    {"dimension": "area", "field": "x;SELECT", "value_type": "text"},
    {"dimension": "city_company", "field": "area_id", "value_type": "text"},
])
def test_invalid_deployment_dimensions(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OrganizationDimensionBinding.model_validate(payload)


def test_duplicate_deployment_dimension_rejected() -> None:
    binding = MetricAuthority().binding
    payload = binding.model_dump(mode="json")
    payload["organization_dimensions"] *= 2
    with pytest.raises(ValidationError, match="duplicate"):
        RelationBinding.model_validate(payload)


@pytest.mark.parametrize("limit", [0, 101, True, "5", 1.5, float("inf")])
def test_ranking_limit_is_strict_and_bounded(limit: Any) -> None:
    with pytest.raises(ValidationError):
        MetricAuthority().plan(intent="ranking", result_limit=limit)


@pytest.mark.parametrize("intent", ["metric", "trend", "comparison", "detail"])
def test_other_intents_cannot_silently_apply_limit(intent: str) -> None:
    with pytest.raises(ValidationError, match="only supported for ranking"):
        MetricAuthority().plan(intent=intent, result_limit=2)


def test_ranking_limit_roundtrip_and_checksum_binding() -> None:
    authority = MetricAuthority()
    old_shape = authority.plan().model_dump(mode="json")
    old_shape.pop("result_limit")
    assert QueryPlan.model_validate(old_shape).result_limit is None
    plan = authority.plan(intent="ranking", dimensions=("area",), result_limit=2)
    assert QueryPlan.model_validate_json(plan.model_dump_json()).checksum == plan.checksum
    assert authority.plan(intent="ranking", dimensions=("area",), result_limit=3).checksum != plan.checksum


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamptz", [True, False])
async def test_ratio_sql_target_and_partial_month_boundaries(timestamptz: bool) -> None:
    authority = MetricAuthority(ratio_contract(), timestamptz=timestamptz)
    query = await authority.compiler().compile(authority.plan(), authority.context)
    assert '"is_valid_for_metrics" IS TRUE' in query.sql
    assert '"completion_time" IS NOT NULL' in query.sql
    assert '"is_first_response_on_time" IS TRUE' in query.sql
    assert "CAST(numerator AS numeric) / NULLIF(denominator, 0)" in query.sql
    assert "area_id" not in query.sql and "team_id" not in query.sql
    assert "first_arrival_time" not in query.sql and "archive_time" not in query.sql
    trend = await authority.compiler().compile(authority.plan(
        intent="trend", grain="month", time_range=TimeRange(start=date(2024, 2, 29), end=date(2024, 3, 1)),
    ), authority.context)
    assert trend.params["start_at"].date() == date(2024, 2, 29)
    assert trend.params["end_at"].date() == date(2024, 3, 2)
    assert "DATE_TRUNC('month'" in trend.sql and trend.sql.endswith("ORDER BY period")
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    assert set(gateway.prepare(query.sql).parameter_names) == {"start_at", "end_at"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("dimension", "value", "column"), [
    ("area", "x'; DROP TABLE t; --", "area_id"), ("team", 2, "team_id"),
])
@pytest.mark.parametrize("operator", ["eq", "in"])
async def test_org_ids_bound_and_grouping_order(dimension: str, value: Any, column: str, operator: str) -> None:
    authority = MetricAuthority(ratio_contract())
    value = [value] if operator == "in" else value
    plan = authority.plan(intent="comparison", dimensions=(dimension,), filters=(org_filter(dimension, value, operator),))
    query = await authority.compiler().compile(plan, authority.context)
    assert f'"{column}" IS NOT NULL' in query.sql
    assert "dimension_id" in query.sql and "period" not in query.sql
    assert "DROP TABLE" not in query.sql and "成都" not in query.sql
    assert query.params["filter_0_0" if operator == "in" else "filter_0"] == (value[0] if operator == "in" else value)
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    assert set(gateway.prepare(query.sql).parameter_names) == set(query.params)
    ranking = await authority.compiler().compile(authority.plan(
        intent="ranking", dimensions=(dimension,), result_limit=1,
    ), authority.context)
    assert "ORDER BY value DESC NULLS LAST, dimension_id" in ranking.sql
    assert ranking.sql.endswith("LIMIT 1") and gateway.prepare(ranking.sql).sql.endswith("LIMIT 1")
    default = await authority.compiler().compile(authority.plan(intent="ranking", dimensions=(dimension,)), authority.context)
    assert default.sql.endswith("LIMIT 10")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"dimensions": ("unknown",)}, {"dimensions": ("area", "team"), "intent": "comparison"},
    {"dimensions": ("city_company", "area"), "intent": "comparison"},
    {"dimensions": ("city_company",), "intent": "ranking"}, {"intent": "comparison"},
    {"dimensions": ("area",)}, {"dimensions": ("team",), "intent": "trend"},
    {"filters": (org_filter("area"), org_filter("team", 1))},
    {"dimensions": ("city_company",), "filters": (org_filter(),)},
    {"dimensions": ("team",), "intent": "ranking", "filters": (org_filter(),)},
    {"filters": (org_filter("area_id"),)}, {"filters": (org_filter("city_company"),)},
    {"filters": (org_filter(), org_filter())}, {"filters": (org_filter(operator="ne"),)},
    {"filters": (org_filter("area", ["a", 1], "in"),)},
    {"filters": (org_filter("area", ["a"] * 101, "in"),)},
    {"filters": (org_filter("area", ["a", "a"], "in"),)},
    {"filters": (org_filter("area", [{"bad": 1}], "in"),)},
    {"filters": (org_filter("area", ""),)}, {"filters": (org_filter("area", "x" * 257),)},
    {"filters": (org_filter("team", True),)}, {"filters": (org_filter("team", 2 ** 31),)},
    {"filters": (org_filter("team", "1"),)}, {"filters": (org_filter("area", "a\x00b"),)},
    {"filters": (org_filter().model_copy(update={"source": "user"}),)},
    {"filters": (org_filter().model_copy(update={"source": "semantic_default"}),)},
])
async def test_unknown_mixed_and_invalid_scope_or_filter_fails(changes: dict[str, Any]) -> None:
    authority = MetricAuthority(ratio_contract())
    with pytest.raises(PlanStepError):
        await authority.compiler().compile(authority.plan(**changes), authority.context)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_binding", "allowed", "missing_column", "sensitive", "type", "filter_type", "physical_filter"])
async def test_dimension_and_filter_snapshot_policy(case: str) -> None:
    metric = ratio_contract(filters=[{"field": "priority", "value_type": "text"}]) if case == "filter_type" else ratio_contract()
    if case == "physical_filter":
        metric = ratio_contract(filters=[{"field": "area_id", "value_type": "text"}])
    authority = MetricAuthority(metric)
    assert authority.snapshot is not None
    if case == "missing_binding":
        authority.binding = authority.binding.model_copy(update={"organization_dimensions": ()})
    elif case == "allowed":
        authority.binding = authority.binding.model_copy(update={
            "allowed_columns": tuple(c for c in authority.binding.allowed_columns if c != "area_id"),
        })
    elif case == "sensitive":
        authority.change_snapshot_relation(sensitive_columns=("area_id",))
    elif case in {"type", "missing_column"}:
        authority.change_snapshot_relation(columns=tuple(
            replace(c, data_type="boolean") if c.name == "area_id" else c
            for c in authority.snapshot.candidate.relations[0].columns
            if case != "missing_column" or c.name != "area_id"
        ))
    assert authority.release is not None
    authority.release = replace(authority.release, schema_snapshot_checksum=authority.snapshot.checksum)
    expected_error = {
        "missing_binding": "grain_or_dimension", "allowed": "column_unapproved",
        "missing_column": "column_unapproved", "sensitive": "column_unapproved",
        "type": "column_type_mismatch", "filter_type": "column_type_mismatch",
        "physical_filter": "filter_unsupported",
    }[case]
    with pytest.raises(PlanStepError, match=expected_error):
        await authority.compiler().compile(authority.plan(intent="comparison", dimensions=("area",)), authority.context)


@pytest.mark.asyncio
@pytest.mark.parametrize(("column_type", "bits"), [("smallint", 16), ("integer", 32), ("bigint", 64)])
async def test_organization_integer_bounds_follow_snapshot(column_type: str, bits: int) -> None:
    authority = MetricAuthority(ratio_contract())
    assert authority.snapshot is not None and authority.release is not None
    authority.change_snapshot_relation(columns=tuple(
        replace(column, data_type=column_type) if column.name == "team_id" else column
        for column in authority.snapshot.candidate.relations[0].columns
    ))
    authority.release = replace(authority.release, schema_snapshot_checksum=authority.snapshot.checksum)
    for value in (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1):
        query = await authority.compiler().compile(
            authority.plan(filters=(org_filter("team", value),)), authority.context,
        )
        assert query.params["filter_0"] == value
    for value in (-(2 ** (bits - 1)) - 1, 2 ** (bits - 1)):
        with pytest.raises(PlanStepError, match="filter_type_mismatch"):
            await authority.compiler().compile(
                authority.plan(filters=(org_filter("team", value),)), authority.context,
            )


async def run_rows(rows: list[dict[str, Any]], *, max_rows: int = 200, **changes: Any) -> Any:
    authority = MetricAuthority(ratio_contract())
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views", max_rows=max_rows)
    plan = authority.plan(**changes)
    runner = GatewayMetricStepRunner(authority.compiler(), gateway)
    prepared = await runner.prepare(step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
                                    query_plan=plan, context=authority.context)
    gateway.execute = AsyncMock(return_value=QueryReceipt(
        accepted=True, sql="", sql_fingerprint=prepared.sql_fingerprint,
        rows=rows, row_count=len(rows), max_rows=max_rows, policy_outcome="allow",
    ))
    return await runner.execute(prepared, timeout_ms=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize(("numerator", "denominator", "value", "status"), [
    (2, 3, Decimal("66.67"), "success"), (0, 5, Decimal("0.00"), "success"),
    (0, 0, None, "no_data"), (1, 32, Decimal("3.13"), "success"),
])
async def test_decimal_result_hash_null_and_freshness(numerator: int, denominator: int, value: Decimal | None, status: str) -> None:
    rows = [dict(numerator=numerator, denominator=denominator, value=value, status=status)]
    with localcontext() as context:
        context.prec = 2
        result = await run_rows(rows)
    assert result.value == {"rows": [{**rows[0], "value": None if value is None else format(value, ".2f")}],
                            "no_data": status == "no_data"}
    assert result.receipt.rowset_sha256 == rowset_sha256(rows)
    assert result.receipt.freshness_status == "unknown" and result.receipt.data_as_of is None
    if value is not None:
        assert rowset_sha256(rows) != rowset_sha256([{**rows[0], "value": str(value)}])


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"value": 50.0}, {"value": "50.00"}, {"value": Decimal("NaN")}, {"value": Decimal("Infinity")},
    {"value": Decimal("50.001")}, {"value": None}, {"status": "no_data"},
    {"value": Decimal("50")}, {"value": Decimal("50.000")},
    {"numerator": True}, {"denominator": None}, {"numerator": -1}, {"numerator": 3},
    {"denominator": 2 ** 63}, {"denominator": 0}, {"extra": "detail"},
])
async def test_ratio_result_shape_rejects_invalid_values(updates: dict[str, Any]) -> None:
    row = {"numerator": 1, "denominator": 2, "value": Decimal("50.00"), "status": "success", **updates}
    with pytest.raises(PlanStepError, match="shape_invalid"):
        await run_rows([row])


@pytest.mark.asyncio
async def test_group_order_ties_limits_and_no_data_shape() -> None:
    row = dict(numerator=1, denominator=2, value=Decimal("50.00"), status="success")
    a, b = dict(row, dimension_id="a"), dict(row, dimension_id="b")
    result = await run_rows([a, b], intent="ranking", dimensions=("area",), result_limit=2)
    assert result.value["rows"][0]["dimension_id"] == "a"
    for rows, changes in [
        ([b, a], {"intent": "comparison"}), ([b, a], {"intent": "ranking"}),
        ([a, a], {"intent": "ranking"}), ([a, b], {"intent": "ranking", "result_limit": 1}),
        ([dict(a, dimension_id=None)], {"intent": "comparison"}),
        ([dict(a, dimension_id=True)], {"intent": "comparison"}),
    ]:
        with pytest.raises(PlanStepError, match="shape_invalid"):
            await run_rows(rows, dimensions=("area",), **changes)
    assert (await run_rows([a], intent="ranking", dimensions=("area",),
                           result_limit=1, max_rows=1)).value["rows"]
    with pytest.raises(PlanStepError, match="truncated"):
        await run_rows([a], intent="ranking", dimensions=("area",), result_limit=2, max_rows=1)
    with pytest.raises(PlanStepError, match="truncated"):
        await run_rows([a], intent="comparison", dimensions=("area",), max_rows=1)
    assert (await run_rows([], intent="comparison", dimensions=("area",))).value["no_data"]
    with pytest.raises(PlanStepError, match="shape_invalid"):
        await run_rows([])
    with pytest.raises(PlanStepError, match="shape_invalid"):
        await run_rows([dict(row, period=datetime(2024, 2, 28))], intent="trend")
    assert (await run_rows([dict(row, period=datetime(2024, 2, 1))], intent="trend", grain="month")).value["rows"]


@pytest.mark.asyncio
async def test_ranking_precision_nulls_and_integer_ids() -> None:
    # Low ambient precision must not turn distinct values into a tie.
    rows = [
        dict(dimension_id=2, numerator=6667, denominator=10000, value=Decimal("66.67"), status="success"),
        dict(dimension_id=1, numerator=6666, denominator=10000, value=Decimal("66.66"), status="success"),
        dict(dimension_id=3, numerator=0, denominator=0, value=None, status="no_data"),
    ]
    with localcontext() as context:
        context.prec = 2
        assert not (await run_rows(rows, intent="ranking", dimensions=("team",))).value["no_data"]
    with pytest.raises(PlanStepError, match="shape_invalid"):
        await run_rows([rows[2], *rows[:2]], intent="ranking", dimensions=("team",))
    assert (await run_rows([rows[2]], intent="ranking", dimensions=("team",))).value["no_data"]
    with pytest.raises(PlanStepError, match="shape_invalid"):
        await run_rows([rows[0], dict(rows[1], dimension_id="1")], intent="ranking", dimensions=("team",))


@pytest.mark.asyncio
async def test_ratio_checkpoint_isolation_and_authority_reread() -> None:
    authority = MetricAuthority(ratio_contract())
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    plan = authority.plan(intent="comparison", dimensions=("area",), filters=(org_filter(value="stable-fixture-id"),))
    query = await authority.compiler().compile(plan, authority.context)
    gateway.execute = AsyncMock(return_value=QueryReceipt(
        accepted=True, sql=query.sql, sql_fingerprint=gateway.prepare(query.sql).fingerprint,
        rows=[dict(dimension_id="stable-fixture-id", numerator=7654321, denominator=7654321,
                   value=Decimal("100.00"), status="success")], row_count=1, max_rows=200, policy_outcome="allow",
    ))
    validation = PlanValidator().validate_query_plan(plan=plan, context=authority.context, identity=authority.identity)
    execution = PlanCompiler().compile(plan=plan, context=authority.context, validation=validation)
    result = await metric_plan_executor(authority.compiler(), gateway).execute(
        query_plan=plan, context=authority.context, execution_plan=execution,
        budget=RouteBudgetLedger(route="standard"), deadline_ms=10000,
    )
    assert result.record.status == "succeeded"
    checkpoint = result.record.model_dump_json()
    assert all(token not in checkpoint for token in ("SELECT", "start_at", "filter_0", "stable-fixture-id", "7654321", "100.00"))
    runner = GatewayMetricStepRunner(authority.compiler(), gateway)
    prepared = await runner.prepare(step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys), query_plan=plan, context=authority.context)
    gateway.execute.reset_mock()
    authority.release = None
    with pytest.raises(PlanStepError, match="active_release"):
        await runner.execute(prepared, timeout_ms=1000)
    gateway.execute.assert_not_called()
