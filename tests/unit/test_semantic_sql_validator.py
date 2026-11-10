from __future__ import annotations

from datetime import date

import pytest

from src.nl2sql.contracts import ExecutionPlan, FetchMetricStep, TimeRange, VerifyStep
from src.nl2sql.infra.governance.query_gateway import PolicyEngine
from src.nl2sql.infra.governance.semantic_sql_validator import (
    SemanticSQLValidationError,
    SemanticSQLValidator,
)
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.orchestration.metric_query import EligibilityPolicy
from src.nl2sql.semantic.execution_manifest import (
    ExpectedExecutionManifest,
    RelationColumnAuthority,
    build_expected_execution_manifest,
)
from tests.metric_fixtures import MetricAuthority, ratio_contract


def _manifest() -> ExpectedExecutionManifest:
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    return ExpectedExecutionManifest(
        query_plan_sha256="a" * 64,
        execution_plan_sha256="b" * 64,
        semantic_release_id="release",
        schema_snapshot_id="snapshot",
        policy_version="policy-v1",
        metric_contract_sha256=("c" * 64,),
        relation_binding_sha256=("d" * 64,),
        expected_sql_fingerprints=(),
        expected_parameter_binding_fingerprints=(),
        relation_identity=("ai_views.orders",),
        relation_column_authority=(
            RelationColumnAuthority(
                relation_id="ai_views.orders",
                columns=(
                    "ts", "id", "order_id", "has_valid_bandwidth",
                    "completion_time", "is_first_response_on_time",
                    "value", "numerator", "denominator", "approved_numerator",
                    "approved_denominator", "approved_value", "region_id", "area_id",
                ),
            ),
        ),
        metric_operation=("count",),
        time_column=("ts",),
        time_bounds=(":start_at", ":end_at", "Asia/Shanghai"),
        expected_predicates=(
            '{"field":"ts","operator":"gte","parameter":"start_at","value":"2026-01-01T00:00:00+08:00"}',
            '{"field":"ts","operator":"lt","parameter":"end_at","value":"2026-01-02T00:00:00+08:00"}',
        ),
        predicate_authority=(
            '{"field":"ts","operator":"gte","parameter":"start_at","role":"business_time","value":"2026-01-01T00:00:00+08:00"}',
            '{"field":"ts","operator":"lt","parameter":"end_at","role":"business_time","value":"2026-01-02T00:00:00+08:00"}',
        ),
        placeholder_identities=("start_at", "end_at"),
        parameter_binding_fingerprint=canonical_json_digest(
            {"names": ("end_at", "start_at"), "values": params}
        ),
        output_aliases=("value",),
        output_logical_types=("integer",),
        cardinality_expectation="single_row",
    )


def _ratio_manifest() -> ExpectedExecutionManifest:
    return _manifest().model_copy(
        update={
            "metric_operation": ("ratio",),
            "ratio_numerator_roles": ("is_first_response_on_time",),
            "ratio_denominator_roles": ("has_valid_bandwidth", "completion_time"),
            "ratio_denominator_predicates": (
                '{"field":"has_valid_bandwidth","operator":"is_true"}',
                '{"field":"completion_time","operator":"is_not_null"}',
            ),
            "ratio_numerator_predicates": (
                '{"field":"has_valid_bandwidth","operator":"is_true"}',
                '{"field":"completion_time","operator":"is_not_null"}',
                '{"field":"is_first_response_on_time","operator":"is_true"}',
            ),
            "expected_predicates": (
                '{"field":"ts","operator":"gte","parameter":"start_at","value":"2026-01-01T00:00:00+08:00"}',
                '{"field":"ts","operator":"lt","parameter":"end_at","value":"2026-01-02T00:00:00+08:00"}',
            ),
            "predicate_authority": (
                '{"field":"ts","operator":"gte","parameter":"start_at","role":"business_time","value":"2026-01-01T00:00:00+08:00"}',
                '{"field":"ts","operator":"lt","parameter":"end_at","role":"business_time","value":"2026-01-02T00:00:00+08:00"}',
                '{"field":"has_valid_bandwidth","operator":"is_true","role":"ratio_denominator"}',
                '{"field":"completion_time","operator":"is_not_null","role":"ratio_denominator"}',
                '{"field":"is_first_response_on_time","operator":"is_true","role":"ratio_numerator_condition"}',
            ),
            "output_aliases": ("numerator", "denominator", "value", "status"),
            "output_logical_types": ("integer", "integer", "numeric", "text"),
        }
    )


def _ratio_sql(
    count_input: str = "*",
    numerator_filter: str = (
        "has_valid_bandwidth IS TRUE AND completion_time IS NOT NULL "
        "AND is_first_response_on_time IS TRUE"
    ),
    denominator_filter: str = "has_valid_bandwidth IS TRUE AND completion_time IS NOT NULL",
    value_expression: str = (
        "CASE WHEN denominator = 0 THEN NULL ELSE "
        "ROUND(100 * CAST(numerator AS numeric) / denominator, 2) END"
    ),
    status_expression: str = (
        "CASE WHEN denominator = 0 THEN 'calculation_error' ELSE 'success' END"
    ),
) -> str:
    return (
        f"SELECT COUNT({count_input}) FILTER (WHERE {numerator_filter}) AS numerator, "
        f"COUNT({count_input}) FILTER (WHERE {denominator_filter}) AS denominator, "
        f"{value_expression} AS value, {status_expression} AS status "
        "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at"
    )


def test_semantic_validator_binds_relation_and_columns() -> None:
    validator = SemanticSQLValidator(policy=PolicyEngine(allowed_schema="ai_views"))
    result = validator.validate(
        "SELECT id FROM orders",
        expected_relation_ids=("ai_views.orders",),
        required_columns=("id",),
    )
    assert result.relations == ("ai_views.orders",)
    assert result.columns == ("id",)


def test_semantic_validator_rejects_unbound_relation_and_tablesample() -> None:
    validator = SemanticSQLValidator(policy=PolicyEngine(allowed_schema="ai_views"))
    with pytest.raises(SemanticSQLValidationError, match="relation_unbound"):
        validator.validate(
            "SELECT id FROM orders",
            expected_relation_ids=("ai_views.customers",),
        )
    with pytest.raises(SemanticSQLValidationError, match="security:policy_denied"):
        validator.validate(
            "SELECT id FROM orders TABLESAMPLE SYSTEM (10)",
            expected_relation_ids=("ai_views.orders",),
        )


def test_manifest_mode_excludes_cte_alias_and_requires_half_open_time() -> None:
    validator = SemanticSQLValidator()
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    valid = validator.validate(
        "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
        params,
        _manifest(),
    )
    assert valid.outcome == "allow"
    with pytest.raises(SemanticSQLValidationError, match="time_predicate_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts <= :end_at",
            params,
            _manifest(),
        )
    with pytest.raises(SemanticSQLValidationError, match="scope_unsupported"):
        validator.validate(
            "WITH selected AS (SELECT id FROM ai_views.orders) SELECT COUNT(*) AS value FROM selected WHERE ts >= :start_at AND ts < :end_at",
            params,
            _manifest(),
        )


def test_manifest_mode_rejects_extra_predicates_and_parameter_substitution() -> None:
    validator = SemanticSQLValidator()
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    with pytest.raises(SemanticSQLValidationError, match="authorization_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at AND 1 = 1",
            params,
            _manifest(),
        )
    with pytest.raises(SemanticSQLValidationError, match="parameter_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :from_at AND ts < :end_at",
            {"from_at": params["start_at"], "end_at": params["end_at"]},
            _manifest(),
        )


@pytest.mark.parametrize(
    "conjunct",
    (
        "RANDOM() > 0.5",
        "id = 1 OR id = 2",
        "NOT (region_id = 'allowed-looking')",
        "ts BETWEEN :start_at AND :end_at",
        "LOWER(region_id) = 'allowed-looking'",
        "MAX(region_id) IS NOT NULL",
        "TRUE",
        "2 = 2",
    ),
)
def test_unknown_where_conjunct_fails_closed(conjunct: str) -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    with pytest.raises(SemanticSQLValidationError):
        SemanticSQLValidator().validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at AND " + conjunct,
            params,
            _manifest(),
        )
def test_manifest_mode_rejects_literal_count_and_wrong_time_value() -> None:
    validator = SemanticSQLValidator()
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
        validator.validate(
            "SELECT 42 AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
            params,
            _manifest(),
        )
    with pytest.raises(SemanticSQLValidationError, match="parameter_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
            {**params, "end_at": "2026-01-03T00:00:00+08:00"},
            _manifest(),
        )


def test_manifest_mode_rejects_missing_authorization_and_wrong_ratio_denominator() -> None:
    validator = SemanticSQLValidator()
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    auth_manifest = _manifest().model_copy(
        update={
            "authorization_predicate_fingerprint": "e" * 64,
            "authorization_predicate_fields": ("area_id",),
        }
    )
    with pytest.raises(SemanticSQLValidationError, match="authorization_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
            params,
            auth_manifest,
        )
    ratio_manifest = _ratio_manifest()
    with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
        validator.validate(
            "SELECT COUNT(*) FILTER (WHERE wrong_denominator IS TRUE) AS numerator, "
            "COUNT(*) FILTER (WHERE wrong_denominator IS TRUE) AS denominator, "
            "CASE WHEN denominator = 0 THEN NULL ELSE ROUND(100 * numerator / denominator, 2) END AS value, "
            "'success' AS status FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at",
            params,
            ratio_manifest,
        )


def test_ratio_manifest_accepts_exact_contract_predicates_and_zero_semantics() -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    result = SemanticSQLValidator().validate(_ratio_sql(), params, _ratio_manifest())
    assert result.outcome == "allow"


def test_ratio_typed_role_authority_must_match_full_denominator_and_numerator() -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    manifest = _ratio_manifest().model_copy(
        update={
            "predicate_authority": (
                '{"field":"has_valid_bandwidth","operator":"is_true","role":"ratio_denominator"}',
                # Wrong role would place the denominator condition in the root WHERE authority.
                '{"field":"completion_time","operator":"is_not_null","role":"metric_base"}',
                '{"field":"is_first_response_on_time","operator":"is_true","role":"ratio_numerator_condition"}',
            )
        }
    )
    with pytest.raises(SemanticSQLValidationError, match="predicate_authority_mismatch"):
        SemanticSQLValidator().validate(_ratio_sql(), params, manifest)


def test_duplicate_root_role_authority_fails_even_when_expected_set_is_unchanged() -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    manifest = _ratio_manifest()
    duplicate = manifest.predicate_authority[0]
    tampered = manifest.model_copy(
        update={"predicate_authority": (*manifest.predicate_authority, duplicate)}
    )
    with pytest.raises(SemanticSQLValidationError, match="predicate_authority_mismatch"):
        SemanticSQLValidator().validate(_ratio_sql(), params, tampered)


def _ratio_wrapped_sql(
    *,
    inner_where: str = "ts >= :start_at AND ts < :end_at",
    wrapper_alias: str = "metric_counts",
    inner_group_by: str = "",
    outer_value: str = (
        "CASE WHEN denominator = 0 THEN NULL ELSE "
        "ROUND(100 * CAST(numerator AS numeric) / denominator, 2) END"
    ),
) -> str:
    # Exact shape emitted by MetricQueryCompiler for a non-trend ratio.
    return (
        "SELECT numerator, denominator, "
        f"{outer_value} AS value, "
        "CASE WHEN denominator = 0 THEN 'calculation_error' ELSE 'success' END AS status "
        "FROM (SELECT COUNT(*) FILTER (WHERE has_valid_bandwidth IS TRUE AND "
        "completion_time IS NOT NULL AND is_first_response_on_time IS TRUE) AS numerator, "
        "COUNT(*) FILTER (WHERE has_valid_bandwidth IS TRUE AND "
        "completion_time IS NOT NULL) AS denominator FROM ai_views.orders "
        f"WHERE {inner_where}{inner_group_by}) AS {wrapper_alias}"
    )


def test_ratio_compiler_wrapper_binds_predicates_inner_and_value_outer() -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    assert SemanticSQLValidator().validate(
        _ratio_wrapped_sql(), params, _ratio_manifest()
    ).outcome == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(("intent", "grain"), [("metric", "day"), ("trend", "month")])
async def test_actual_metric_compiler_ratio_sql_passes_typed_manifest(
    intent: str, grain: str
) -> None:
    authority = MetricAuthority(metric=ratio_contract())
    plan = authority.plan(intent=intent, grain=grain)
    compiled = await authority.compiler().compile(plan, authority.context)
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.numerator", "fetch.denominator"),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    manifest = build_expected_execution_manifest(
        query_plan=plan,
        execution_plan=execution_plan,
        context=authority.context,
        metric_contracts=(authority.metric,),
        relation_bindings=(authority.binding,),
        eligibility_policies=(
            EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),
        ),
        schema_snapshot=authority.snapshot,
        semantic_release=authority.release,
        actual_source_kind=compiled.source_kind,
        source_step_id="fetch",
    )
    assert "AS metric_counts" in compiled.sql
    result = SemanticSQLValidator().validate(compiled.sql, compiled.params, manifest)
    assert result.outcome == "allow"
    strict_validator = SemanticSQLValidator(require_complete_authority=True)
    assert (
        strict_validator.validate(
            compiled.sql,
            compiled.params,
            manifest,
            expected_step_id="fetch",
        ).outcome
        == "allow"
    )
    with pytest.raises(SemanticSQLValidationError, match="manifest_authority_invalid"):
        strict_validator.validate(
            compiled.sql,
            compiled.params,
            manifest.model_copy(
                update={"schema_snapshot_checksum": "f" * 64}
            ),
            expected_step_id="fetch",
        )
    if intent == "trend":
        for mutated in (
            compiled.sql.replace(
                "SELECT period, numerator, denominator,",
                "SELECT CAST(period AS date) AS period, numerator, denominator,",
            ),
            compiled.sql.replace(
                "SELECT period, numerator, denominator,",
                "SELECT DATE '2026-09-01' AS period, numerator, denominator,",
            ),
            compiled.sql.replace(
                "SELECT period, numerator, denominator,",
                "SELECT numerator AS period, numerator, denominator,",
            ),
        ):
            with pytest.raises(SemanticSQLValidationError):
                SemanticSQLValidator().validate(mutated, compiled.params, manifest)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conjunct",
    (
        "RANDOM() > 0.5",
        "(priority = 1 OR priority = 2)",
        "NOT (priority = 1)",
        "priority BETWEEN 1 AND 2",
        "LOWER(priority::text) = 'forged'",
        "MAX(priority) IS NOT NULL",
        "TRUE",
        "1 = 1",
    ),
)
async def test_actual_compiler_manifest_rejects_unknown_where_conjuncts(
    conjunct: str,
) -> None:
    authority = MetricAuthority()
    plan = authority.plan()
    compiled = await authority.compiler().compile(plan, authority.context)
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.value",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    manifest = build_expected_execution_manifest(
        query_plan=plan,
        execution_plan=execution_plan,
        context=authority.context,
        metric_contracts=(authority.metric,),
        relation_bindings=(authority.binding,),
        eligibility_policies=(
            EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),
        ),
        schema_snapshot=authority.snapshot,
        semantic_release=authority.release,
        actual_source_kind=compiled.source_kind,
        source_step_id="fetch",
    )
    assert SemanticSQLValidator().validate(
        compiled.sql, compiled.params, manifest
    ).outcome == "allow"
    with pytest.raises(SemanticSQLValidationError):
        SemanticSQLValidator().validate(
            compiled.sql + " AND " + conjunct,
            compiled.params,
            manifest,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamptz", [True, False])
async def test_actual_compiler_sparse_date_filter_is_typed_and_exact(
    timestamptz: bool,
) -> None:
    dates = (date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 5))
    authority = MetricAuthority(metric=ratio_contract(), timestamptz=timestamptz)
    plan = authority.plan(
        intent="trend",
        grain="day",
        time_range=TimeRange(start=dates[0], end=dates[-1]),
        available_dates=dates,
    )
    compiled = await authority.compiler().compile(plan, authority.context)
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.numerator", "fetch.denominator"),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    manifest = build_expected_execution_manifest(
        query_plan=plan,
        execution_plan=execution_plan,
        context=authority.context,
        metric_contracts=(authority.metric,),
        relation_bindings=(authority.binding,),
        eligibility_policies=(
            EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),
        ),
        schema_snapshot=authority.snapshot,
        semantic_release=authority.release,
        actual_source_kind=compiled.source_kind,
        source_step_id="fetch",
    )
    assert manifest.sparse_date_authority is not None
    assert manifest.sparse_date_authority.dates == dates
    assert SemanticSQLValidator().validate(
        compiled.sql, compiled.params, manifest
    ).outcome == "allow"
    time_column = authority.metric.business_time_column
    date_expression = (
        f'DATE("{time_column}" AT TIME ZONE \'Asia/Shanghai\')'
        if timestamptz
        else f'DATE("{time_column}")'
    )
    date_filter = (
        f"{date_expression} IN "
        "(:available_date_0, :available_date_1, :available_date_2)"
    )
    assert date_filter in compiled.sql
    mutations = [
        compiled.sql.replace(date_filter, "TRUE"),
        compiled.sql.replace(
            date_filter,
            date_filter + " AND LOWER(priority) = 'forged'",
        ),
    ]
    if timestamptz:
        mutations.extend(
            (
                compiled.sql.replace("'Asia/Shanghai') IN", "'UTC') IN"),
                compiled.sql.replace(date_expression, f'DATE("{time_column}")'),
            )
        )
    else:
        mutations.append(
            compiled.sql.replace(
                date_expression,
                f'DATE("{time_column}" AT TIME ZONE \'Asia/Shanghai\')',
            )
        )
    for mutated in mutations:
        with pytest.raises(SemanticSQLValidationError):
            SemanticSQLValidator().validate(mutated, compiled.params, manifest)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dimension", "physical_field", "is_text"),
    (("area", "area_id", True), ("team", "team_id", False)),
)
async def test_actual_ratio_ranking_binds_dimension_projection_and_outer_order(
    dimension: str,
    physical_field: str,
    is_text: bool,
) -> None:
    authority = MetricAuthority(metric=ratio_contract())
    plan = authority.plan(
        intent="ranking", dimensions=(dimension,), result_limit=2
    )
    compiled = await authority.compiler().compile(plan, authority.context)
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.numerator", "fetch.denominator"),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    manifest = build_expected_execution_manifest(
        query_plan=plan,
        execution_plan=execution_plan,
        context=authority.context,
        metric_contracts=(authority.metric,),
        relation_bindings=(authority.binding,),
        eligibility_policies=(
            EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),
        ),
        schema_snapshot=authority.snapshot,
        semantic_release=authority.release,
        actual_source_kind=compiled.source_kind,
        source_step_id="fetch",
    )
    assert manifest.ranking_limit == 2
    dimension_authority = next(
        item for item in manifest.output_field_authority
        if item.alias == "dimension_id"
    )
    assert dimension_authority.field == physical_field
    assert dimension_authority.logical_type == ("text" if is_text else "integer")
    assert SemanticSQLValidator().validate(
        compiled.sql, compiled.params, manifest
    ).outcome == "allow"
    mismatched_relation_authority = dimension_authority.model_copy(
        update={"relation_id": "ai_views.other_relation"}
    )
    duplicate_output_authority = manifest.model_copy(
        update={
            "output_field_authority": (
                *manifest.output_field_authority,
                dimension_authority,
            )
        }
    )
    wrong_relation = manifest.model_copy(
        update={
            "output_field_authority": tuple(
                mismatched_relation_authority
                if item.alias == "dimension_id"
                else item
                for item in manifest.output_field_authority
            )
        }
    )
    for invalid_manifest in (duplicate_output_authority, wrong_relation):
        with pytest.raises(SemanticSQLValidationError):
            SemanticSQLValidator().validate(
                compiled.sql, compiled.params, invalid_manifest
            )
    projection = (
        f'"{physical_field}" COLLATE "C" AS dimension_id'
        if is_text
        else f'"{physical_field}" AS dimension_id'
    )
    grouping = (
        f'"{physical_field}" COLLATE "C"'
        if is_text
        else f'"{physical_field}"'
    )
    wrong_field = (
        '"team_id" AS dimension_id'
        if is_text
        else '"area_id" COLLATE "C" AS dimension_id'
    )
    wrong_collation = (
        compiled.sql.replace(
            projection,
            f'"{physical_field}" COLLATE "POSIX" AS dimension_id',
        )
        if is_text
        else compiled.sql.replace(
            projection,
            f'"{physical_field}" COLLATE "C" AS dimension_id',
        )
    )
    mutations = (
        compiled.sql.replace(
            "SELECT dimension_id, numerator, denominator,",
            "SELECT dimension_id, denominator, numerator,",
        ),
        compiled.sql.replace(
            "SELECT dimension_id, numerator, denominator,",
            "SELECT other_counts.dimension_id, numerator, denominator,",
        ),
        compiled.sql.replace(
            "SELECT dimension_id, numerator, denominator,",
            "SELECT LOWER(dimension_id) AS dimension_id, numerator, denominator,",
        ),
        compiled.sql.replace(
            "SELECT dimension_id, numerator, denominator,",
            "SELECT 'forged' AS dimension_id, numerator, denominator,",
        ),
        compiled.sql.replace(
            "SELECT dimension_id, numerator, denominator,",
            "SELECT numerator AS dimension_id, numerator, denominator,",
        ),
        compiled.sql.replace(
            "value DESC NULLS LAST, dimension_id",
            "value ASC NULLS LAST, dimension_id",
        ),
        compiled.sql.replace(
            "ORDER BY value DESC NULLS LAST, dimension_id LIMIT 2",
            "LIMIT 2",
        ),
        compiled.sql.replace(
            "value DESC NULLS LAST, dimension_id",
            "dimension_id",
        ),
        compiled.sql.replace(
            "value DESC NULLS LAST, dimension_id",
            "value DESC NULLS FIRST, dimension_id",
        ),
        compiled.sql.replace(
            "value DESC NULLS LAST, dimension_id",
            "value DESC NULLS LAST, dimension_id DESC",
        ),
        compiled.sql.replace("LIMIT 2", "LIMIT 1"),
        compiled.sql.replace("LIMIT 2", "LIMIT 2 OFFSET 0"),
        compiled.sql.replace(
            projection,
            wrong_field,
        ),
        compiled.sql.replace(projection, 'MAX("priority") AS dimension_id'),
        compiled.sql.replace(
            projection,
            "'forged' AS dimension_id" if is_text else "1 AS dimension_id",
        ),
        compiled.sql.replace(projection, '"unauthorized_column" AS dimension_id'),
        compiled.sql.replace(f"GROUP BY {grouping}", 'GROUP BY "priority"'),
        wrong_collation,
        compiled.sql.replace(
            projection,
            f'LOWER("{physical_field}") COLLATE "C" AS dimension_id',
        ),
    )
    for mutated in mutations:
        with pytest.raises(SemanticSQLValidationError):
            SemanticSQLValidator().validate(mutated, compiled.params, manifest)


@pytest.mark.parametrize(
    "sql",
    [
        _ratio_sql(count_input="order_id"),
        _ratio_wrapped_sql(inner_where="ts >= :start_at"),
        _ratio_wrapped_sql(inner_group_by=" GROUP BY area_id"),
        _ratio_wrapped_sql(wrapper_alias="other_counts"),
        _ratio_wrapped_sql(outer_value=(
            "CASE WHEN denominator = 0 THEN NULL ELSE "
            "ROUND(100 * CAST(denominator AS numeric) / numerator, 2) END"
        )),
    ],
)
def test_ratio_compiler_wrapper_mutations_fail_closed(sql: str) -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    with pytest.raises(SemanticSQLValidationError):
        SemanticSQLValidator().validate(sql, params, _ratio_manifest())


@pytest.mark.parametrize(
    "inner_suffix",
    [
        " HAVING COUNT(*) > 0",
        " LIMIT 1",
        " OFFSET 1",
        " ORDER BY numerator",
    ],
)
def test_ratio_wrapper_rejects_unmanifested_inner_clauses(
    inner_suffix: str,
) -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    manifest = _ratio_manifest().model_copy(
        update={"cardinality_expectation": "bounded"}
    )
    with pytest.raises(SemanticSQLValidationError, match="scope_unsupported"):
        SemanticSQLValidator().validate(
            _ratio_wrapped_sql(inner_group_by=inner_suffix), params, manifest
        )


def test_count_source_uses_only_authoritative_count_or_aggregate_value_column() -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    detail = _manifest().model_copy(update={"count_column": "order_id"})
    valid_count = (
        "SELECT COUNT(order_id) AS value FROM ai_views.orders "
        "WHERE ts >= :start_at AND ts < :end_at"
    )
    assert SemanticSQLValidator().validate(valid_count, params, detail).outcome == "allow"
    wrong_count = valid_count.replace("COUNT(order_id)", "COUNT(*)")
    with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
        SemanticSQLValidator().validate(wrong_count, params, detail)

    aggregate = detail.model_copy(
        update={
            "source_kind": ("approved_aggregate",),
            "aggregate_value_column": "approved_value",
        }
    )
    valid_sum = (
        "SELECT CAST(COALESCE(SUM(approved_value), 0) AS bigint) AS value "
        "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at"
    )
    assert SemanticSQLValidator().validate(valid_sum, params, aggregate).outcome == "allow"
    for mutated in (
        valid_sum.replace("SUM(approved_value)", "SUM(wrong_value)"),
        valid_sum.replace("SUM(approved_value)", "SUM(approved_value) * 0"),
        valid_sum.replace("COALESCE(SUM(approved_value), 0)", "COALESCE(SUM(approved_value), 1)"),
    ):
        with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
            SemanticSQLValidator().validate(mutated, params, aggregate)


def test_approved_ratio_aggregate_requires_exact_authoritative_sum_columns() -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    manifest = _ratio_manifest().model_copy(
        update={
            "source_kind": ("approved_aggregate",),
            "ratio_aggregate_numerator_column": "approved_numerator",
            "ratio_aggregate_denominator_column": "approved_denominator",
        }
    )
    sql = (
        "SELECT CAST(COALESCE(SUM(approved_numerator), 0) AS bigint) AS numerator, "
        "CAST(COALESCE(SUM(approved_denominator), 0) AS bigint) AS denominator, "
        "CASE WHEN denominator = 0 THEN NULL ELSE "
        "ROUND(100 * CAST(numerator AS numeric) / denominator, 2) END AS value, "
        "CASE WHEN denominator = 0 THEN 'calculation_error' ELSE 'success' END AS status "
        "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at"
    )
    assert SemanticSQLValidator().validate(sql, params, manifest).outcome == "allow"
    for mutated in (
        sql.replace(
            "SUM(approved_numerator)", "SUM(approved_numerator) * 0"
        ),
        sql.replace("SUM(approved_numerator)", "SUM(other_numerator)"),
        sql.replace("COALESCE(SUM(approved_denominator), 0)", "COALESCE(SUM(approved_denominator), 1)"),
    ):
        with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
            SemanticSQLValidator().validate(mutated, params, manifest)


@pytest.mark.parametrize(
    "sql",
    [
        # Denominator population can neither omit a condition nor change its operator.
        _ratio_sql(denominator_filter="has_valid_bandwidth IS TRUE"),
        _ratio_sql(denominator_filter="has_valid_bandwidth IS FALSE AND completion_time IS NOT NULL"),
        # Numerator must retain the complete denominator population and add the exact numerator condition.
        _ratio_sql(numerator_filter="completion_time IS NOT NULL AND is_first_response_on_time IS TRUE"),
        _ratio_sql(
            numerator_filter=(
                "has_valid_bandwidth IS TRUE AND completion_time IS NOT NULL "
                "AND is_first_response_on_time IS FALSE"
            )
        ),
        # Value must be ROUND(100 * numerator / denominator, 2).
        _ratio_sql(value_expression=(
            "CASE WHEN denominator = 0 THEN NULL ELSE "
            "ROUND(100 * CAST(denominator AS numeric) / numerator, 2) END"
        )),
        _ratio_sql(value_expression=(
            "CASE WHEN denominator = 0 THEN NULL ELSE "
            "ROUND(100 * CAST(numerator AS numeric) / denominator, 1) END"
        )),
        _ratio_sql(value_expression=(
            "CASE WHEN denominator = 0 THEN 0 ELSE "
            "ROUND(100 * CAST(numerator AS numeric) / denominator, 2) END"
        )),
        # Zero denominator is an undefined calculation, not success or no-data.
        _ratio_sql(status_expression=(
            "CASE WHEN denominator = 0 THEN 'success' ELSE 'calculation_error' END"
        )),
        _ratio_sql(status_expression=(
            "CASE WHEN denominator = 0 THEN 'no_data' ELSE 'success' END"
        )),
    ],
)
def test_ratio_semantic_mutations_fail_closed(sql: str) -> None:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
        SemanticSQLValidator().validate(sql, params, _ratio_manifest())
