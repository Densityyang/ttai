import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.nl2sql.contracts import BoundFilter, FetchMetricStep, TimeRange
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.candidates import rowset_sha256
from src.nl2sql.orchestration.execution import PlanStepError, PreparedMetricStep
from src.nl2sql.orchestration.metric_query import GatewayMetricStepRunner, metric_plan_executor
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.registry import SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import SchemaSnapshotState
from tests.metric_fixtures import MetricAuthority, seed_contract


def test_catalog_addition_requires_no_python_and_preserves_channels() -> None:
    seed = seed_contract()
    another = seed_contract(metric_key="complaint_other_approved_count", display_name="Synthetic other")
    catalog = load_metric_catalog(MetricCatalog(metrics=(seed, another)).model_dump_json())
    ir = metric_catalog_ir(catalog, relations={"complaint_orders": "ai_views.complaint_orders"})
    assert len(ir.metrics) == 2
    assert ir.metrics[1].execution_contract is not None
    assert ir.metrics[1].execution_contract["benchmark_eligible"] is False
    assert seed.daily_report_order is None
    with pytest.raises(ValueError, match="binding missing"):
        metric_catalog_ir(catalog, relations={})
    pending = seed_contract(release_status="pending_source", owner=None, approver=None)
    assert metric_catalog_ir(MetricCatalog(metrics=(pending,)), relations={}).metrics[0].status == "retired"


@pytest.mark.parametrize("changes", [
    {"operation": "ratio"}, {"owner": None}, {"approver": " "},
    {"supported_grains": []}, {"supported_grains": ["day", "day"]},
    {"required_permissions": [""]}, {"daily_report_order": 1},
    {"filters": [{"field": "x", "value_type": "text"}] * 2},
    {"business_time_column": "x;DROP TABLE t"}, {"benchmark_eligible": "pending"},
])
def test_invalid_contract_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        seed_contract(**changes)


def test_duplicate_catalog_and_deployment_definitions_fail() -> None:
    with pytest.raises(ValidationError, match="duplicate metric"):
        MetricCatalog(metrics=(seed_contract(), seed_contract()))
    authority = MetricAuthority()
    with pytest.raises(ValueError, match="duplicate deployment"):
        authority.compiler(bindings=(authority.binding, authority.binding))


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamptz", [True, False])
async def test_eligibility_half_open_leap_day_and_month(timestamptz: bool) -> None:
    authority = MetricAuthority(timestamptz=timestamptz)
    query = await authority.compiler().compile(authority.plan(), authority.context)
    assert '"is_valid_for_metrics" IS TRUE' in query.sql
    assert '"has_valid_bandwidth" IS TRUE' in query.sql
    assert '"completion_time" IS NULL' in query.sql
    assert "area" not in query.sql and "team" not in query.sql
    assert query.params["start_at"].date() == date(2024, 2, 29)
    assert query.params["end_at"].date() == date(2024, 3, 1)
    assert (query.params["start_at"].utcoffset() == timedelta(hours=8)) == timestamptz
    trend = await authority.compiler().compile(authority.plan(intent="trend", grain="month"), authority.context)
    assert "DATE_TRUNC('month'" in trend.sql
    assert ("AT TIME ZONE" in trend.sql) == timestamptz


@pytest.mark.asyncio
async def test_filter_value_is_bound_not_interpolated() -> None:
    authority = MetricAuthority(seed_contract(filters=[{"field": "category", "value_type": "text"}]))
    malicious = "x'; DROP TABLE complaint_orders; --"
    plan = authority.plan(filters=(BoundFilter(field_ref="category", operator="eq", value=malicious, source="user"),))
    query = await authority.compiler().compile(plan, authority.context)
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    prepared = gateway.prepare(query.sql)
    assert set(prepared.parameter_names) == {"start_at", "end_at", "filter_0"}
    assert malicious not in query.sql
    assert query.params["filter_0"] == malicious


@pytest.mark.asyncio
@pytest.mark.parametrize(("changes", "code"), [
    ({"intent": "ranking"}, "operation_unsupported"),
    ({"grain": "week"}, "grain_or_dimension"),
    ({"dimensions": ("team",)}, "grain_or_dimension"),
    ({"metric_keys": ("metric.missing",)}, "plan_denied"),
    ({"unresolved_slots": ("time",)}, "plan_denied"),
    ({"time_range": TimeRange(start=date(2024, 1, 1), end=date(2024, 1, 1), timezone="UTC")}, "timezone"),
    ({"time_range": TimeRange(start=date(2020, 1, 1), end=date(2024, 1, 1))}, "too_large"),
    ({"time_range": TimeRange(start=date.max, end=date.max)}, "overflow"),
    ({"filters": (BoundFilter(field_ref="is_valid_for_metrics", operator="eq", value=False, source="user"),)}, "filter_unsupported"),
])
async def test_unknown_or_out_of_bounds_plans_fail(changes: dict[str, Any], code: str) -> None:
    authority = MetricAuthority()
    with pytest.raises(PlanStepError, match=code):
        await authority.compiler().compile(authority.plan(**changes), authority.context)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "missing_release", "retired_release", "other_release", "missing_snapshot", "retired_snapshot",
    "checksum", "missing_contract", "invalid_contract", "inactive_contract", "missing_binding",
    "missing_policy", "permission", "relation_context", "relation_release", "relation_snapshot",
    "column", "sensitive", "timestamp_type", "boolean_type",
])
async def test_trusted_authority_fails_closed(case: str) -> None:
    authority = MetricAuthority()
    assert authority.release is not None and authority.snapshot is not None
    options: dict[str, Any] = {}
    if case == "missing_release":
        authority.release = None
    elif case == "retired_release":
        authority.release = replace(authority.release, state=SemanticReleaseState.RETIRED)
    elif case == "other_release":
        authority.release = replace(authority.release, release_id="other")
    elif case == "missing_snapshot":
        authority.snapshot = None
    elif case == "retired_snapshot":
        authority.snapshot = replace(authority.snapshot, state=SchemaSnapshotState.RETIRED)
    elif case == "checksum":
        authority.release = replace(authority.release, schema_snapshot_checksum="c" * 64)
    elif case in {"missing_contract", "invalid_contract", "inactive_contract", "relation_release"}:
        docs = list(authority.release.documents)
        if case == "missing_contract":
            docs = [doc for doc in docs if doc.document_id != authority.metric.asset_id]
        elif case == "relation_release":
            docs = [doc for doc in docs if doc.metadata["asset_type"] != "relation"]
        else:
            docs = [replace(doc, metadata={**doc.metadata, **(
                {"execution_contract": "{}"} if case == "invalid_contract" else {"status": "retired"}
            )}) if doc.document_id == authority.metric.asset_id else doc for doc in docs]
        authority.release = replace(authority.release, documents=tuple(docs))
    elif case == "missing_binding":
        options["bindings"] = ()
    elif case == "missing_policy":
        options["eligibility_policies"] = ()
    elif case == "permission":
        options["identity"] = authority.identity.model_copy(update={"permissions": frozenset()})
    elif case == "relation_context":
        authority.context = authority.context.model_copy(update={"approved_relation_ids": ()})
    elif case == "relation_snapshot":
        authority.change_snapshot_relation(relation_id="ai_views.other")
    elif case == "column":
        options["bindings"] = (authority.binding.model_copy(update={"allowed_columns": ("acceptance_time",)}),)
    elif case == "sensitive":
        authority.change_snapshot_relation(sensitive_columns=("acceptance_time",))
    else:
        target = "acceptance_time" if case == "timestamp_type" else "has_valid_bandwidth"
        authority.change_snapshot_relation(columns=tuple(
            replace(column, data_type="text") if column.name == target else column
            for column in authority.snapshot.candidate.relations[0].columns
        ))
    with pytest.raises(PlanStepError):
        await authority.compiler(**options).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "operator"), [("wrong", "eq"), (True, "eq"), (1, "gt")])
async def test_typed_integer_filters(value: Any, operator: str) -> None:
    authority = MetricAuthority(seed_contract(filters=[{"field": "priority", "value_type": "integer"}]))
    query_filter = BoundFilter.model_validate(dict(field_ref="priority", operator=operator, value=value, source="user"))
    with pytest.raises(PlanStepError):
        await authority.compiler().compile(authority.plan(filters=(query_filter,)), authority.context)


def test_canonical_rows_order_duplicates_null_precision_and_timestamps() -> None:
    instant = datetime(2024, 3, 1, tzinfo=UTC)
    shanghai = instant.astimezone(timezone(timedelta(hours=8)))
    a = {"n": Decimal("123456789012345678901234567890.1200"), "t": instant, "x": None}
    b = {"x": None, "t": shanghai, "n": Decimal("123456789012345678901234567890.12")}
    c = {"x": "x", "t": shanghai, "n": Decimal("0")}
    assert rowset_sha256([a, c, a]) == rowset_sha256([b, b, c])
    assert rowset_sha256([a, a]) != rowset_sha256([a])
    assert rowset_sha256([{"v": 0}]) != rowset_sha256([])
    assert rowset_sha256([{"v": None}]) != rowset_sha256([{"v": "null"}])
    assert rowset_sha256([{"v": Decimal("1")}]) != rowset_sha256([{"v": {"$decimal": "1"}}])
    assert rowset_sha256([{"v": instant}]) != rowset_sha256([{"v": instant.replace(tzinfo=None)}])
    assert rowset_sha256([{"v": [date(2024, 2, 29), 1.5, False]}])


@pytest.mark.parametrize("rows", [
    [{"v": object()}], [{"v": float("nan")}], [{"v": Decimal("Infinity")}],
    [{1: "bad"}], [{"a": 1}, {"b": 1}], [{"v": {1: "bad"}}],
])
def test_noncanonical_rowsets_are_rejected(rows: Any) -> None:
    with pytest.raises(ValueError):
        rowset_sha256(rows)


@pytest.mark.asyncio
async def test_executor_factory_preserves_receipt_and_hides_ephemeral_values() -> None:
    authority = MetricAuthority()
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    query = await authority.compiler().compile(authority.plan(), authority.context)
    fingerprint = gateway.prepare(query.sql).fingerprint
    gateway.execute = AsyncMock(return_value=QueryReceipt(
        accepted=True, sql=query.sql, sql_fingerprint=fingerprint,
        rows=[{"value": 87654321}], row_count=1, policy_outcome="allow", max_rows=200,
    ))
    plan = authority.plan()
    validator = PlanValidator()
    validated = validator.validate_query_plan(plan=plan, context=authority.context, identity=authority.identity)
    execution = PlanCompiler().compile(plan=plan, context=authority.context, validation=validated)
    budget = RouteBudgetLedger(route="standard")
    assert validator.validate_execution_plan(execution_plan=execution, query_plan=plan,
                                             context=authority.context, route_budget=budget.limits).outcome == "allow"
    result = await metric_plan_executor(authority.compiler(), gateway).execute(
        query_plan=plan, context=authority.context, execution_plan=execution, budget=budget, deadline_ms=10000,
    )
    assert result.record.status == "succeeded"
    receipt = result.record.step_receipts[0]
    assert receipt.rowset_sha256 == rowset_sha256([{"value": 87654321}])
    assert receipt.data_as_of is None and receipt.freshness_status == "unknown"
    checkpoint = result.record.model_dump_json()
    assert "87654321" not in checkpoint and "SELECT" not in checkpoint and "start_at" not in checkpoint
    assert budget.sql_executions == 1


@pytest.mark.asyncio
async def test_runner_rechecks_authority_before_execution() -> None:
    authority = MetricAuthority()
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    gateway.execute = AsyncMock()
    runner = GatewayMetricStepRunner(authority.compiler(), gateway)
    step = FetchMetricStep(step_id="fetch", metric_keys=authority.plan().metric_keys)
    prepared = await runner.prepare(step=step, query_plan=authority.plan(), context=authority.context)
    authority.release = None
    with pytest.raises(PlanStepError, match="active_release"):
        await runner.execute(prepared, timeout_ms=1000)
    gateway.execute.assert_not_called()
    with pytest.raises(PlanStepError, match="prepared_query_invalid"):
        await runner.execute(PreparedMetricStep("a" * 64, 0, object()), timeout_ms=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["denied", "truncated", "shape", "tampered", "step", "cancelled", "timeout"])
async def test_runner_failure_and_cancellation_boundaries(case: str) -> None:
    authority = MetricAuthority()
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    runner = GatewayMetricStepRunner(authority.compiler(), gateway)
    step = FetchMetricStep(step_id="fetch", metric_keys=authority.plan().metric_keys)
    if case == "step":
        with pytest.raises(PlanStepError, match="step_mismatch"):
            await runner.prepare(step=step.model_copy(update={"metric_keys": ("other",)}),
                                 query_plan=authority.plan(), context=authority.context)
        return
    prepared = await runner.prepare(step=step, query_plan=authority.plan(), context=authority.context)
    gateway.execute = AsyncMock(return_value=QueryReceipt(
        accepted=case != "denied", sql="", sql_fingerprint=prepared.sql_fingerprint,
        rows=[{"value": "bad" if case == "shape" else 0}], row_count=1,
        max_rows=1 if case == "truncated" else 200,
    ))
    if case == "tampered":
        prepared = PreparedMetricStep("f" * 64, 0, prepared.payload)
    if case == "cancelled":
        gateway.execute.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await runner.execute(prepared, timeout_ms=1000)
    elif case == "timeout":
        async def blocked(*args: Any, **kwargs: Any) -> QueryReceipt:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        gateway.execute.side_effect = blocked
        with pytest.raises(TimeoutError):
            await runner.execute(prepared, timeout_ms=1)
    else:
        with pytest.raises(PlanStepError):
            await runner.execute(prepared, timeout_ms=1000)


@pytest.mark.asyncio
async def test_integer_filter_and_deployment_extra_eligibility() -> None:
    from src.nl2sql.orchestration.metric_query import EligibilityPolicy
    from src.nl2sql.semantic.metric_contract import Predicate

    authority = MetricAuthority(seed_contract(filters=[{"field": "priority", "value_type": "integer"}]))
    policy = EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id,
                               predicates=(Predicate(field="completion_time", operator="is_null"),))
    query = await authority.compiler(eligibility_policies=(policy,)).compile(
        authority.plan(filters=(BoundFilter(field_ref="priority", operator="eq", value=2, source="user"),)),
        authority.context,
    )
    assert query.params["filter_0"] == 2
    assert '"is_valid_for_metrics" IS TRUE' in query.sql
    assert '"completion_time" IS NULL' in query.sql
