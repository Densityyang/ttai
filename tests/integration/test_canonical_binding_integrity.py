"""Execution-level H19b fail-before-SQL counterfactuals.

The successful canonical chain remains exercised by the adjacent PG17 integration
test.  These negative cases use real compiler/executor/QueryGateway components
and prove the gateway is never entered, so no database connection is needed.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.nl2sql.contracts import FetchMetricStep
from src.nl2sql.infra.governance.query_gateway import PreparedQuery, QueryGateway
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.execution import PlanExecutionResult
from src.nl2sql.orchestration.metric_query import (
    CompiledMetricQuery,
    GatewayMetricStepRunner,
    metric_plan_executor,
)
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator

# Reuse the real synthetic release/catalog authority. The accepted W2 PG17
# fixture module remains unchanged; its successful-chain test is run separately.
from tests.integration.test_canonical_calculation_chain import (
    CANONICAL_METRIC,
    DENOMINATOR_METRIC,
    NUMERATOR_METRIC,
    _ChainAuthority,
)


def _replace_binding_input(
    binding: ApprovedCalculationBinding,
    index: int,
    **changes: str,
) -> ApprovedCalculationBinding:
    inputs = list(binding.inputs)
    inputs[index] = inputs[index].model_copy(update=changes)
    return binding.model_copy(update={"inputs": tuple(inputs)})


def _mutate_release_metric(
    authority: _ChainAuthority,
    metric_key: str,
    *,
    remove: bool = False,
    metadata_changes: dict[str, str] | None = None,
) -> None:
    documents = []
    for document in authority.release.documents:
        if document.document_id != metric_key:
            documents.append(document)
            continue
        if remove:
            continue
        documents.append(
            replace(
                document,
                metadata={**document.metadata, **(metadata_changes or {})},
            )
        )
    authority.release = replace(authority.release, documents=tuple(documents))


def _validated_execution(authority: _ChainAuthority, binding: ApprovedCalculationBinding):
    catalog = ApprovedCalculationCatalog([binding])
    validator = PlanValidator(calculation_catalog=catalog)
    plan = authority.plan()
    query_validation = validator.validate_query_plan(
        plan=plan,
        context=authority.context,
        identity=authority.identity,
    )
    assert query_validation.outcome == "allow", query_validation.issues
    execution_plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan,
        context=authority.context,
        validation=query_validation,
    )
    execution_validation = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=authority.context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    assert execution_validation.outcome == "allow", execution_validation.issues
    return catalog, validator, plan, execution_plan, execution_validation


@pytest.mark.asyncio
async def test_valid_release_and_all_input_hashes_prepare_canonical_dependency() -> None:
    authority = _ChainAuthority()
    catalog = ApprovedCalculationCatalog([authority.binding])
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    execute_spy = AsyncMock(wraps=gateway.execute)
    gateway.execute = execute_spy
    runner = GatewayMetricStepRunner(
        authority.compiler,
        gateway,
        calculation_catalog=catalog,
    )

    prepared = await runner.prepare(
        step=FetchMetricStep(
            step_id="fetch_numerator",
            metric_keys=(NUMERATOR_METRIC,),
            calculation_input_role="numerator",
            calculation_binding_checksum=authority.binding.checksum,
            calculation_output_metric_key=CANONICAL_METRIC,
        ),
        query_plan=authority.plan(),
        context=authority.context,
    )

    assert prepared.dependency_fetch is True
    assert isinstance(prepared.payload, CompiledMetricQuery)
    assert prepared.payload.calculation_binding == authority.binding
    assert execute_spy.await_count == 0


async def _execute(
    authority: _ChainAuthority,
    binding: ApprovedCalculationBinding,
    *,
    rotate_release_after_prepare: bool = False,
) -> tuple[PlanExecutionResult, RouteBudgetLedger, AsyncMock]:
    catalog, validator, plan, execution_plan, execution_validation = (
        _validated_execution(authority, binding)
    )
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    actual_execute = gateway.execute
    execute_spy = AsyncMock(wraps=actual_execute)
    gateway.execute = execute_spy
    if rotate_release_after_prepare:
        actual_prepare = gateway.prepare
        rotated = False

        def prepare_then_rotate(sql: str) -> PreparedQuery:
            nonlocal rotated
            prepared = actual_prepare(sql)
            if not rotated:
                authority.release = replace(
                    authority.release, checksum="f" * 64
                )
                rotated = True
            return prepared

        gateway.prepare = prepare_then_rotate

    budget = RouteBudgetLedger(route="standard")
    result = await metric_plan_executor(
        authority.compiler,
        gateway,
        calculation_catalog=catalog,
    ).execute(
        query_plan=plan,
        context=authority.context,
        execution_plan=execution_plan,
        validation=execution_validation,
        expected_policy_version=validator.policy_version,
        expected_policy_checksum=validator.policy_checksum,
        budget=budget,
        deadline_ms=10_000,
    )
    return result, budget, execute_spy


@pytest.mark.asyncio
@pytest.mark.parametrize("input_index", [0, 1], ids=["wrong-first-hash", "wrong-second-hash"])
async def test_any_bound_input_hash_mismatch_fails_before_first_sql(
    input_index: int,
) -> None:
    authority = _ChainAuthority()
    binding = _replace_binding_input(
        authority.binding,
        input_index,
        metric_contract_sha256="f" * 64,
    )

    result, budget, execute_spy = await _execute(authority, binding)

    assert result.record.status == "failed"
    assert result.record.stop_reason == "metric_contract_checksum_mismatch"
    assert budget.sql_executions == 0
    assert execute_spy.await_count == 0


@pytest.mark.asyncio
async def test_wrong_active_release_checksum_fails_before_first_sql(
) -> None:
    authority = _ChainAuthority()
    binding = authority.binding.model_copy(
        update={"semantic_release_checksum": "f" * 64}
    )

    result, budget, execute_spy = await _execute(authority, binding)

    assert result.record.status == "failed"
    assert result.record.stop_reason == "metric_semantic_release_checksum_mismatch"
    assert budget.sql_executions == 0
    assert execute_spy.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "error_code"),
    [
        ("missing", "metric_contract_missing"),
        ("inactive", "metric_contract_inactive"),
        ("invalid", "metric_contract_invalid"),
    ],
)
async def test_every_bound_input_contract_is_resolved_before_first_sql(
    case: str,
    error_code: str,
) -> None:
    authority = _ChainAuthority()
    if case == "missing":
        binding = _replace_binding_input(
            authority.binding, 1, metric_key="metric.chain_missing_input"
        )
        authority.context = authority.context.model_copy(
            update={
                "asset_ids": (*authority.context.asset_ids, "metric.chain_missing_input")
            }
        )
    elif case == "inactive":
        binding = authority.binding
        _mutate_release_metric(
            authority,
            DENOMINATOR_METRIC,
            metadata_changes={"status": "inactive"},
        )
    else:
        binding = authority.binding
        _mutate_release_metric(
            authority,
            DENOMINATOR_METRIC,
            metadata_changes={"execution_contract": "not-json"},
        )

    result, budget, execute_spy = await _execute(authority, binding)

    assert result.record.status == "failed"
    assert result.record.stop_reason == error_code
    assert budget.sql_executions == 0
    assert execute_spy.await_count == 0


@pytest.mark.asyncio
async def test_release_change_after_prepare_blocks_gateway_execution(
) -> None:
    authority = _ChainAuthority()

    result, budget, execute_spy = await _execute(
        authority, authority.binding, rotate_release_after_prepare=True
    )

    assert result.record.status == "failed"
    assert result.record.stop_reason == "metric_semantic_release_checksum_mismatch"
    # The executor charges its SQL attempt before runner.execute.  This is
    # distinct from a QueryGateway.execute call, which remains at zero.
    assert budget.sql_executions == 1
    assert execute_spy.await_count == 0


@pytest.mark.asyncio
async def test_missing_required_metric_context_is_rejected_before_sql(
) -> None:
    authority = _ChainAuthority()
    authority.context = authority.context.model_copy(
        update={
            "asset_ids": tuple(
                key for key in authority.context.asset_ids if key != DENOMINATOR_METRIC
            )
        }
    )

    # The execution-plan validator rejects the unresolved second dependency;
    # PlanExecutor must stop on that deny before preparing or executing SQL.
    catalog = ApprovedCalculationCatalog([authority.binding])
    validator = PlanValidator(calculation_catalog=catalog)
    plan = authority.plan()
    query_validation = validator.validate_query_plan(
        plan=plan,
        context=authority.context,
        identity=authority.identity,
    )
    assert query_validation.outcome == "allow", query_validation.issues
    execution_plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan,
        context=authority.context,
        validation=query_validation,
    )
    execution_validation = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=authority.context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    assert execution_validation.outcome == "deny"
    assert any(
        issue.code == "trusted_calculation_dependency_context_missing"
        for issue in execution_validation.issues
    )

    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    execute_spy = AsyncMock(wraps=gateway.execute)
    gateway.execute = execute_spy
    budget = RouteBudgetLedger(route="standard")
    result = await metric_plan_executor(
        authority.compiler,
        gateway,
        calculation_catalog=catalog,
    ).execute(
        query_plan=plan,
        context=authority.context,
        execution_plan=execution_plan,
        validation=execution_validation,
        expected_policy_version=validator.policy_version,
        expected_policy_checksum=validator.policy_checksum,
        budget=budget,
        deadline_ms=10_000,
    )

    assert result.record.status == "failed"
    assert result.record.stop_reason == "execution_plan_validation_denied"
    assert budget.sql_executions == 0
    assert execute_spy.await_count == 0
