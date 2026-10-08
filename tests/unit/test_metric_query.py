import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.nl2sql.contracts import (
    AUTHORIZATION_DENIED,
    AuthorizationContext,
    AuthorizationDecision,
    BoundFilter,
    FetchMetricStep,
    TimeRange,
    evaluate_authorization,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.candidates import rowset_sha256
from src.nl2sql.orchestration.execution import PlanStepError, PreparedMetricStep
from src.nl2sql.orchestration.metric_query import (
    CompiledMetricQuery,
    EligibilityPolicy,
    GatewayMetricStepRunner,
    metric_plan_executor,
)
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.registry import SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import (
    ColumnSnapshot,
    OrganizationCoverageBinding,
    SchemaSnapshotState,
    validate_organization_coverage,
)
from tests.metric_fixtures import (
    RELEASE_ID,
    SNAPSHOT_ID,
    MetricAuthority,
    ratio_contract,
    seed_contract,
)


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
@pytest.mark.parametrize("operation", ["count", "ratio"])
@pytest.mark.parametrize("case", [
    "missing_release", "retired_release", "other_release", "missing_snapshot", "retired_snapshot",
    "checksum", "missing_contract", "invalid_contract", "inactive_contract", "missing_binding",
    "missing_policy", "permission", "relation_context", "relation_release", "relation_snapshot",
    "column", "sensitive", "timestamp_type", "boolean_type",
])
async def test_trusted_authority_fails_closed(case: str, operation: str) -> None:
    authority = MetricAuthority(ratio_contract() if operation == "ratio" else seed_contract())
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

# ---------------------------------------------------------------------------
# Slice 2B: dormant typed organization-scope + RelationCoverage enforcement
# ---------------------------------------------------------------------------


def _auth(
    level: str,
    ids: tuple[str, ...],
    *,
    revision: str = "rev-1",
    enabled: bool = True,
) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=enabled,
        scope_level=level,
        allowed_scope_ids=ids,
    )


def _org_filter(dimension: str, value: Any, operator: str = "eq") -> BoundFilter:
    return BoundFilter.model_validate(
        dict(field_ref=dimension, operator=operator, value=value, source="entity_alias")
    )


def _with_employee(authority: MetricAuthority) -> None:
    assert authority.snapshot is not None
    relation = authority.snapshot.candidate.relations[0]
    authority.change_snapshot_relation(
        columns=(
            *relation.columns,
            ColumnSnapshot("employee_id", "text", True, len(relation.columns) + 1),
        ),
        organization_coverage=(
            *relation.organization_coverage,
            OrganizationCoverageBinding("employee", "employee_id", "text"),
        ),
    )
    authority.binding = authority.binding.model_copy(
        update={
            "allowed_columns": (*authority.binding.allowed_columns, "employee_id"),
        }
    )


def _area_team_authority() -> MetricAuthority:
    return MetricAuthority(
        seed_contract(supported_dimensions=("city_company", "area", "team"))
    )


@pytest.mark.asyncio
async def test_multi_level_coverage_on_one_relation() -> None:
    # A. One relation exposes city_company AND area AND team AND employee at once.
    authority = MetricAuthority(
        seed_contract(
            supported_dimensions=("city_company", "area", "team", "employee")
        )
    )
    _with_employee(authority)
    assert authority.snapshot is not None
    relation = authority.snapshot.candidate.relations[0]
    assert {binding.scope_level for binding in relation.organization_coverage} == {
        "city_company",
        "area",
        "team",
        "employee",
    }
    assert validate_organization_coverage(relation) == ()
    query = await authority.compiler(
        authorization=_auth("city_company", ("cc-1",))
    ).compile(authority.plan(intent="comparison", dimensions=("employee",)), authority.context)
    assert '"employee_id"' in query.sql


@pytest.mark.asyncio
async def test_city_company_requires_explicit_coverage_without_a_fake_id() -> None:
    # B. city_company is an explicit root scope: no fabricated id predicate, and
    # missing explicit coverage fails closed.
    authority = MetricAuthority()
    query = await authority.compiler(
        authorization=_auth("city_company", ("cc-1",))
    ).compile(authority.plan(), authority.context)
    assert query.authorization_scope_level == "city_company"
    assert query.authorization_revision == "rev-1"
    assert not any(name.startswith("auth_scope_") for name in query.params)
    assert "company_id" not in query.sql and " IN (" not in query.sql

    authority.change_snapshot_relation(
        organization_coverage=(
            OrganizationCoverageBinding("area", "area_id", "text"),
            OrganizationCoverageBinding("team", "team_id", "integer"),
        )
    )
    with pytest.raises(PlanStepError, match="coverage_unapproved"):
        await authority.compiler(authorization=_auth("city_company", ("cc-1",))).compile(
            authority.plan(), authority.context
        )


@pytest.mark.asyncio
async def test_area_authorization_grouping_by_team_is_a_physical_row_fact() -> None:
    # C. area auth + team grouping: area_id IN authorized areas while grouping
    # team_id, with NO derived area->team membership.
    authority = _area_team_authority()
    query = await authority.compiler(
        authorization=_auth("area", ("area-1",))
    ).compile(
        authority.plan(intent="comparison", dimensions=("team",)), authority.context
    )
    assert '"area_id" IN (:auth_scope_0)' in query.sql
    assert '"team_id"' in query.sql and "GROUP BY" in query.sql
    assert query.params["auth_scope_0"] == "area-1"
    assert "JOIN" not in query.sql
    assert query.sql.count("FROM") == 1


@pytest.mark.asyncio
async def test_team_authorization_grouping_by_employee_is_a_physical_row_fact() -> None:
    # D. team auth + employee grouping requires approved team AND employee
    # coverage and derives NO team->employee hierarchy.
    authority = MetricAuthority(
        seed_contract(supported_dimensions=("city_company", "area", "team", "employee"))
    )
    _with_employee(authority)
    query = await authority.compiler(
        authorization=_auth("team", ("7",))
    ).compile(
        authority.plan(intent="comparison", dimensions=("employee",)), authority.context
    )
    assert '"team_id" IN (:auth_scope_0)' in query.sql
    assert '"employee_id"' in query.sql and "GROUP BY" in query.sql
    # Integer-backed coverage converts the opaque string id strictly.
    assert query.params["auth_scope_0"] == 7
    assert "JOIN" not in query.sql


@pytest.mark.asyncio
async def test_narrower_user_filter_keeps_the_system_predicate() -> None:
    # E. area auth + a narrower team filter: BOTH predicates are present, and no
    # child id list is required from Backend.
    authority = _area_team_authority()
    query = await authority.compiler(
        authorization=_auth("area", ("area-1",))
    ).compile(
        authority.plan(filters=(_org_filter("team", 9),)), authority.context
    )
    assert '"area_id" IN (:auth_scope_0)' in query.sql
    assert '"team_id" = :filter_0' in query.sql
    assert query.params["auth_scope_0"] == "area-1" and query.params["filter_0"] == 9


@pytest.mark.asyncio
async def test_same_level_out_of_scope_filter_is_a_canonical_deny() -> None:
    # F. area auth A1 + requested area A2 denies before any SQL is produced.
    authority = _area_team_authority()
    with pytest.raises(PlanStepError, match="authorization_denied"):
        await authority.compiler(
            authorization=_auth("area", ("area-1",))
        ).compile(
            authority.plan(filters=(_org_filter("area", "area-2"),)), authority.context
        )


@pytest.mark.asyncio
async def test_cross_level_id_collision_does_not_authorize() -> None:
    # G. An id authorized as area must never satisfy a team-level authorization;
    # a colliding team id does not rescue an uncovered area scope.
    assert (
        evaluate_authorization(
            _auth("area", ("12",)),
            expected_revision=None,
            requested_scope_level="team",
            requested_scope_id="12",
        ).outcome
        == "deny"
    )
    authority = _area_team_authority()
    authority.change_snapshot_relation(
        organization_coverage=(
            OrganizationCoverageBinding("city_company"),
            OrganizationCoverageBinding("team", "team_id", "integer"),
        )
    )
    with pytest.raises(PlanStepError, match="coverage_unapproved"):
        await authority.compiler(authorization=_auth("area", ("12",))).compile(
            authority.plan(filters=(_org_filter("team", 12),)), authority.context
        )


@pytest.mark.asyncio
async def test_broader_query_than_the_authorization_level_is_denied() -> None:
    # H. team auth may not request an area (or city_company) organizational view.
    authority = _area_team_authority()
    with pytest.raises(PlanStepError, match="scope_denied"):
        await authority.compiler(authorization=_auth("team", ("team-1",))).compile(
            authority.plan(intent="comparison", dimensions=("area",)), authority.context
        )


@pytest.mark.asyncio
async def test_employee_requires_explicit_metric_and_relation_coverage() -> None:
    # I. employee support without approved employee coverage fails closed; team
    # ids never imply employee access.
    authority = MetricAuthority(
        seed_contract(supported_dimensions=("city_company", "area", "team", "employee"))
    )
    with pytest.raises(PlanStepError, match="coverage_unapproved"):
        await authority.compiler(authorization=_auth("team", ("team-1",))).compile(
            authority.plan(intent="comparison", dimensions=("employee",)), authority.context
        )


@pytest.mark.asyncio
async def test_removing_a_user_filter_never_removes_the_system_predicate() -> None:
    # J. The system predicate is independent of the QueryPlan.
    authority = _area_team_authority()
    compiler = authority.compiler(authorization=_auth("area", ("area-1",)))
    without_filter = await compiler.compile(authority.plan(), authority.context)
    with_filter = await compiler.compile(
        authority.plan(filters=(_org_filter("area", "area-1"),)), authority.context
    )
    assert '"area_id" IN (:auth_scope_0)' in without_filter.sql
    assert '"area_id" IN (:auth_scope_0)' in with_filter.sql


@pytest.mark.asyncio
async def test_compiler_admission_denies_disabled_and_empty_scope() -> None:
    authority = MetricAuthority()
    with pytest.raises(PlanStepError, match="authorization_denied"):
        await authority.compiler(
            authorization=_auth("area", ("area-1",), enabled=False)
        ).compile(authority.plan(), authority.context)
    with pytest.raises(PlanStepError, match="authorization_denied"):
        await authority.compiler(authorization=_auth("area", ())).compile(
            authority.plan(), authority.context
        )


@pytest.mark.asyncio
async def test_compiler_without_injected_authorization_is_unchanged() -> None:
    # T. The dormant pre-existing behavior is preserved when no context is injected.
    authority = MetricAuthority(ratio_contract())
    query = await authority.compiler().compile(authority.plan(), authority.context)
    assert query.authorization_revision is None
    assert query.authorization_scope_level is None
    assert not any(name.startswith("auth_scope_") for name in query.params)
    assert " IN (" not in query.sql
    grouping = await authority.compiler().compile(
        authority.plan(intent="comparison", dimensions=("area",)), authority.context
    )
    assert '"area_id"' in grouping.sql and " IN (" not in grouping.sql

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids",
    [("007",), ("+7",), (" 7",), ("7.0",), ("1e1",), ("9223372036854775808",)],
)
async def test_malformed_integer_authorization_ids_fail_closed(ids: tuple[str, ...]) -> None:
    authority = _area_team_authority()
    with pytest.raises(PlanStepError, match="coverage_invalid"):
        await authority.compiler(authorization=_auth("team", ids)).compile(
            authority.plan(), authority.context
        )

@pytest.mark.asyncio
async def test_recompilation_detects_an_authorization_change() -> None:
    authority = _area_team_authority()
    compiler = authority.compiler(authorization=_auth("area", ("area-1",)))
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    runner = GatewayMetricStepRunner(compiler, gateway)
    plan = authority.plan()
    prepared = await runner.prepare(
        step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
        query_plan=plan,
        context=authority.context,
    )
    # A rotated opaque revision must invalidate the prepared authority before the
    # gateway is reached; the revision and scope level bind the replay identity.
    compiler._authorization = _auth("area", ("area-1",), revision="rev-2")
    gateway.execute = AsyncMock()
    with pytest.raises(PlanStepError, match="prepared_query_changed"):
        await runner.execute(prepared, timeout_ms=1000)
    gateway.execute.assert_not_called()

@pytest.mark.asyncio
async def test_no_authorization_city_company_dimension_still_compiles() -> None:
    # B. NO authorization + a city_company dimension compiles exactly as before,
    # even when the relation declares no coverage: city_company remains the
    # default total scope and no coverage is enforced.
    authority = MetricAuthority()
    authority.change_snapshot_relation(organization_coverage=())
    query = await authority.compiler().compile(
        authority.plan(dimensions=("city_company",)), authority.context
    )
    assert query.authorization_revision is None and query.authorization_scope_level is None
    assert "company_id" not in query.sql and " IN (" not in query.sql


@pytest.mark.asyncio
async def test_authorized_city_company_without_declared_coverage_denies() -> None:
    # D. AUTHORIZATION injected + city_company with NO declared city_company
    # coverage is a non-degradable deny: city_company is an explicit scope, never
    # 'authorization disabled'.
    authority = MetricAuthority()
    authority.change_snapshot_relation(
        organization_coverage=(
            OrganizationCoverageBinding("area", "area_id", "text"),
        )
    )
    with pytest.raises(PlanStepError, match="coverage_unapproved"):
        await authority.compiler(authorization=_auth("city_company", ("cc-1",))).compile(
            authority.plan(), authority.context
        )

@pytest.mark.asyncio
async def test_authorized_malformed_coverage_is_non_degradable() -> None:
    # Authorized-path coverage ENFORCEMENT is intact: a coverage declaration that
    # disagrees with the physical column family fails closed and cannot degrade.
    authority = MetricAuthority()
    authority.change_snapshot_relation(
        organization_coverage=(
            # area_id is text, so declaring it integer is malformed.
            OrganizationCoverageBinding("area", "area_id", "integer"),
        )
    )
    with pytest.raises(PlanStepError, match="coverage_invalid"):
        await authority.compiler(authorization=_auth("area", ("area-1",))).compile(
            authority.plan(), authority.context
        )

@pytest.mark.asyncio
async def test_no_authorization_signature_matches_the_pre_2b_payload() -> None:
    # FIX 1: pin the no-auth semantic_signature to the KNOWN pre-2B value.  The
    # hashed payload carried EXACTLY the pre-2B keys; no authorization key may be
    # present on the no-auth path.  This is what makes the constructor comment's
    # byte-identity claim a tested invariant rather than an assertion.
    authority = MetricAuthority()
    assert authority.snapshot is not None and authority.release is not None
    plan = authority.plan()
    query = await authority.compiler().compile(plan, authority.context)
    expected_payload = {
        "plan": plan.model_dump(mode="json", exclude={"source_strategy"}),
        "metric": authority.metric.model_dump(mode="json"),
        "policy": EligibilityPolicy(
            policy_id=authority.metric.eligibility_policy_id
        ).model_dump(mode="json"),
        "release": RELEASE_ID,
        "snapshot": SNAPSHOT_ID,
        "checksum": authority.snapshot.checksum,
        "checkpoint": None,
    }
    assert sorted(expected_payload) == [
        "checkpoint",
        "checksum",
        "metric",
        "plan",
        "policy",
        "release",
        "snapshot",
    ]
    assert "authorization_revision" not in expected_payload
    assert "authorization_scope_level" not in expected_payload
    expected = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert query.semantic_signature == expected
    # Literal pre-2B value pin (the fixture is deterministic).
    assert query.semantic_signature == (
        "e753d3dcfcc0c0a039d1d28d2f87b75d804b94c91ddc43be3f0b91f65b0d1b0b"
    )


@pytest.mark.asyncio
async def test_authorized_compile_receipt_never_takes_the_context_revision() -> None:
    # FIX 2: R12 as a TESTED invariant.  The compiled authority carries the
    # context revision for replay binding, but an ExecutionReceipt only ever gets
    # a revision through bind_execution_receipt_authorization on a real ALLOW --
    # never by copying the context.
    from src.nl2sql.ownership import bind_execution_receipt_authorization

    authority = _area_team_authority()
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    runner = GatewayMetricStepRunner(
        authority.compiler(authorization=_auth("area", ("area-1",))), gateway
    )
    plan = authority.plan()
    prepared = await runner.prepare(
        step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
        query_plan=plan,
        context=authority.context,
    )
    query = prepared.payload
    assert isinstance(query, CompiledMetricQuery)
    assert query.authorization_revision == "rev-1"
    gateway.execute = AsyncMock(return_value=QueryReceipt(
        accepted=True, sql=query.sql, sql_fingerprint=prepared.sql_fingerprint,
        rows=[{"value": 1}], row_count=1, policy_outcome="allow", max_rows=200,
    ))
    result = await runner.execute(prepared, timeout_ms=1000)
    assert result.receipt is not None
    assert result.receipt.authorization_revision is None
    assert "rev-1" not in result.receipt.model_dump_json()
    # No decision => the ORIGINAL receipt object, still unstamped.
    assert bind_execution_receipt_authorization(result.receipt, None) is result.receipt
    # Only an explicit ALLOW decision stamps a revision.
    allowed = AuthorizationDecision(outcome="allow", authorization_revision="rev-1")
    bound = bind_execution_receipt_authorization(result.receipt, allowed)
    assert bound.authorization_revision == "rev-1"
    assert result.receipt.authorization_revision is None
    with pytest.raises(ValueError, match="deny decision"):
        bind_execution_receipt_authorization(result.receipt, AUTHORIZATION_DENIED)
