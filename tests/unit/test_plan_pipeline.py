from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import JsonValue, ValidationError

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    BoundFilter,
    ContextBundle,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    PlanValidationIssue,
    PlanValidationRecord,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    RouteName,
    TimeRange,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
    RegistryTrustedCalculationRunner,
)
from src.nl2sql.orchestration.grounding import (
    build_answer_facts,
    receipt_degradation_flags,
    render_grounded_answer,
)
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.ownership import runtime_config
from src.nl2sql.semantic.context_compiler import (
    ContextBundle as RetrievedContextBundle,
)
from src.nl2sql.semantic.context_compiler import (
    ContextCompiler,
    Evidence,
    PolicyScopedEvidence,
    Route,
    SemanticContextResolver,
)
from src.nl2sql.semantic.registry import SemanticRelease, SemanticReleaseState

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")
SQL_FINGERPRINT = "a" * 64

_SYNTHETIC_PAYLOAD_KEY = "revenue"
_SYNTHETIC_PAYLOAD_VALUE = 4242
# Renderings that json.dumps(..., default=str) writes verbatim for non-JSON objects.
# None can occur inside an ISO timestamp, UUID, hex checksum or plain counter.
_SYNTHETIC_PAYLOAD_MARKERS = (
    '"revenue": 4242',
    '"revenue":4242',
    '"revenue", 4242',
    '"revenue",4242',
    "'revenue': 4242",
    "'revenue':4242",
    "'revenue', 4242",
    "'revenue',4242",
    "revenue=4242",
)


def _assert_business_payload_absent(persisted: object, *, path: str = "$") -> None:
    """Fail when a persisted structure carries the raw synthetic business payload.

    The payload counts as persisted only as a mapping whose "revenue" entry is
    4242. JSON text is decoded and walked structurally; strings that render the
    payload as a Python repr (dataclass/kwargs, tuple/list, single-quoted dict)
    are matched against payload-shaped markers. Opaque metadata (timestamps,
    UUIDs, hashes, counters) satisfies neither and cannot reintroduce the flake.
    """
    if isinstance(persisted, Mapping):
        if persisted.get(_SYNTHETIC_PAYLOAD_KEY) == _SYNTHETIC_PAYLOAD_VALUE:
            raise AssertionError(f"raw business payload persisted at {path}")
        for key, value in persisted.items():
            _assert_business_payload_absent(value, path=f"{path}.{key}")
    elif isinstance(persisted, (list, tuple)):
        for index, value in enumerate(persisted):
            _assert_business_payload_absent(value, path=f"{path}[{index}]")
    elif isinstance(persisted, str):
        if any(marker in persisted for marker in _SYNTHETIC_PAYLOAD_MARKERS):
            raise AssertionError(f"raw business payload persisted at {path}")
        try:
            decoded = json.loads(persisted)
        except json.JSONDecodeError:
            return
        _assert_business_payload_absent(decoded, path=path)


def _identity(*, permissions: frozenset[str] = frozenset({"metrics:read"})) -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        roles=frozenset({"analyst"}),
        permissions=permissions,
    )


def _context(
    *,
    resolution_status: Literal["resolved", "ambiguous", "incomplete", "conflict"] = (
        "resolved"
    ),
    unresolved_slots: tuple[str, ...] = (),
    approved_edge_ids: tuple[str, ...] = (),
) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=("metric.revenue",),
        approved_relation_ids=("relation.revenue_daily",),
        approved_edge_ids=approved_edge_ids,
        resolution_status=resolution_status,
        unresolved_slots=unresolved_slots,
        token_cost=120,
        evidence_count=2,
    )


def _query_plan(
    *,
    required_permissions: tuple[str, ...] = ("metrics:read",),
    unresolved_slots: tuple[str, ...] = (),
) -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.revenue",),
        dimensions=(),
        filters=(
            BoundFilter(
                field_ref="region.code",
                operator="eq",
                value="east",
                source="user",
            ),
        ),
        time_range=TimeRange(
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        ),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=required_permissions,
        unresolved_slots=unresolved_slots,
    )


class _MetricRunner:
    def __init__(self, value: JsonValue | None = None) -> None:
        self.value = value if value is not None else {"revenue": 4242}
        self.prepare_calls = 0
        self.execute_calls = 0

    async def prepare(
        self,
        *,
        step: FetchMetricStep,
        query_plan: QueryPlan,
        context: ContextBundle,
    ) -> PreparedMetricStep:
        assert step.metric_keys == query_plan.metric_keys
        assert context.semantic_release_id == RELEASE_ID
        self.prepare_calls += 1
        return PreparedMetricStep(
            sql_fingerprint=SQL_FINGERPRINT,
            join_hops=0,
            payload={"sql": "SELECT synthetic_revenue"},
        )

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
    ) -> MetricStepResult:
        assert prepared.payload == {"sql": "SELECT synthetic_revenue"}
        assert timeout_ms >= 1
        self.execute_calls += 1
        return MetricStepResult(
            value=self.value,
            receipt=ExecutionReceipt(
                datasource="synthetic",
                readonly_role="fixture_reader",
                elapsed_ms=1,
                row_count=1,
                sql_fingerprint=prepared.sql_fingerprint,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


class _BlockingMetricRunner(_MetricRunner):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
    ) -> MetricStepResult:
        del prepared, timeout_ms
        self.started.set()
        await self.release.wait()
        raise AssertionError("cancelled execution must not resume")


class _StaticContextResolver:
    def __init__(self, context: ContextBundle) -> None:
        self.context = context
        self.calls = 0

    async def resolve(
        self,
        *,
        question: str,
        identity: RequestIdentity,
        route_hint: RouteName,
    ) -> ContextBundle:
        assert question == "show revenue"
        assert identity.user_id == "analyst"
        assert route_hint == "standard"
        self.calls += 1
        return self.context


class _SequenceContextResolver:
    def __init__(self, contexts: tuple[ContextBundle, ...]) -> None:
        self.contexts = contexts
        self.calls = 0

    async def resolve(
        self,
        *,
        question: str,
        identity: RequestIdentity,
        route_hint: RouteName,
    ) -> ContextBundle:
        assert question == "show revenue"
        assert identity.user_id == "analyst"
        assert route_hint == "standard"
        context = self.contexts[self.calls]
        self.calls += 1
        return context


class _StaticQueryPlanProvider:
    is_deterministic = True

    def __init__(self, plan: QueryPlan) -> None:
        self.plan = plan
        self.calls = 0

    async def propose(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> QueryPlan:
        assert question == "show revenue"
        assert context.semantic_release_id == RELEASE_ID
        assert identity.permissions == frozenset({"metrics:read"})
        self.calls += 1
        return self.plan


class _ModelBackedQueryPlanProvider(_StaticQueryPlanProvider):
    is_deterministic = False


class _CountingProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
        response_schema: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        del model, messages, timeout_ms, max_output_tokens, response_schema
        self.calls += 1
        return ProviderResponse(
            content="model path",
            model="small",
            usage={},
            finish_reason="stop",
        )

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


def _model_gateway(provider: _CountingProvider) -> ModelGateway:
    return ModelGateway(
        providers={provider.provider_name: provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget(provider.provider_name, "small", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "test-v1",
                frozenset({"plan"}),
                ModelTarget(provider.provider_name, "small", "small"),
                None,
            ),
        },
    )


def test_typed_filter_and_time_contracts_reject_unbounded_values() -> None:
    with pytest.raises(ValidationError, match="two-item JSON array"):
        BoundFilter(
            field_ref="region.code",
            operator="between",
            value=["east"],
            source="user",
        )

    with pytest.raises(ValidationError, match="end cannot be before start"):
        TimeRange(start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_execution_plan_contract_rejects_unknown_steps_refs_and_cycles() -> None:
    plan = _query_plan()
    context = _context()

    with pytest.raises(ValidationError, match="kind"):
        ExecutionPlan.model_validate(
            {
                "query_plan_sha256": plan.checksum,
                "semantic_release_id": str(context.semantic_release_id),
                "schema_snapshot_id": str(context.schema_snapshot_id),
                "policy_version": "plan-compiler.test.v1",
                "steps": (
                    {
                        "kind": "python",
                        "step_id": "run_python",
                        "code": "result = 1",
                    },
                ),
            }
        )

    with pytest.raises(ValidationError, match="dependency is unknown"):
        ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version="plan-compiler.test.v1",
            steps=(
                VerifyStep(
                    step_id="verify",
                    input_refs=("missing",),
                    invariant_ids=("typed_result_present",),
                    depends_on=("missing",),
                ),
            ),
        )

    with pytest.raises(ValidationError, match="declared as a dependency"):
        ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version="plan-compiler.test.v1",
            steps=(
                FetchMetricStep(
                    step_id="fetch_a",
                    metric_keys=plan.metric_keys,
                ),
                FetchMetricStep(
                    step_id="fetch_b",
                    metric_keys=plan.metric_keys,
                ),
                VerifyStep(
                    step_id="verify",
                    input_refs=("fetch_a",),
                    invariant_ids=("typed_result_present",),
                    depends_on=("fetch_b",),
                ),
            ),
        )

    with pytest.raises(ValidationError, match="contain a cycle"):
        ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version="plan-compiler.test.v1",
            steps=(
                VerifyStep(
                    step_id="verify_a",
                    input_refs=("verify_b",),
                    invariant_ids=("typed_result_present",),
                    depends_on=("verify_b",),
                ),
                VerifyStep(
                    step_id="verify_b",
                    input_refs=("verify_a",),
                    invariant_ids=("typed_result_present",),
                    depends_on=("verify_a",),
                ),
            ),
        )


def test_plan_validator_and_compiler_are_permission_bound_and_replayable() -> None:
    context = _context()
    plan = _query_plan()
    validator = PlanValidator()

    validation = validator.validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    first = PlanCompiler().compile(
        plan=plan,
        context=context,
        validation=validation,
    )
    second = PlanCompiler().compile(
        plan=plan,
        context=context,
        validation=validation,
    )

    assert validation.outcome == "allow"
    assert len(validation.policy_checksum) == 64
    assert first == second
    assert first.checksum == second.checksum
    assert [step.kind for step in first.steps] == ["fetch_metric", "verify"]

    denied = validator.validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(permissions=frozenset()),
    )
    assert denied.outcome == "deny"
    assert [issue.code for issue in denied.issues] == ["query_plan_permission_denied"]


def test_engine_rejects_a_model_backed_provider_before_route_selection() -> None:
    provider = _CountingProvider()

    with pytest.raises(ValueError, match="deterministic and zero-model"):
        create_v2_engine(
            checkpointer=MemorySaver(),
            model_gateway=_model_gateway(provider),
            context_resolver=_StaticContextResolver(_context()),
            query_plan_provider=_ModelBackedQueryPlanProvider(_query_plan()),
            plan_executor=PlanExecutor(metric_runner=_MetricRunner()),
        )

    assert provider.calls == 0


_EXPECTED_POLICY = PlanValidator()


def _execution_validation(
    query_plan: QueryPlan,
    context: ContextBundle,
    execution_plan: ExecutionPlan,
    *,
    validator: PlanValidator | None = None,
) -> PlanValidationRecord:
    return (validator or PlanValidator()).validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=query_plan,
        context=context,
        route_budget=RouteBudgetLedger(route="fast").limits,
    )


def _alt_execution_plan(query_plan: QueryPlan, context: ContextBundle) -> ExecutionPlan:
    return ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(step_id="fetch_alt", metric_keys=query_plan.metric_keys),
            VerifyStep(
                step_id="verify_alt",
                input_refs=("fetch_alt",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch_alt",),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plan_executor_refuses_a_validation_record_for_another_plan() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(plan=plan, context=context, identity=_identity())
    execution_plan = PlanCompiler().compile(plan=plan, context=context, validation=validation)
    other_plan = _alt_execution_plan(plan, context)
    runner = _MetricRunner()
    result = await PlanExecutor(metric_runner=runner).execute(
        query_plan=plan,
        context=context,
        execution_plan=execution_plan,
        validation=_execution_validation(plan, context, other_plan),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="fast"),
        deadline_ms=4_000,
    )
    assert result.record.status == "failed"
    assert result.record.stop_reason == "execution_plan_checksum_mismatch"
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_plan_executor_refuses_a_foreign_policy_identity() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(plan=plan, context=context, identity=_identity())
    execution_plan = PlanCompiler().compile(plan=plan, context=context, validation=validation)
    record = _execution_validation(plan, context, execution_plan)
    for overrides, expected in (
        (
            {"expected_policy_version": "plan-validation.other.v9"},
            "execution_plan_policy_version_mismatch",
        ),
        (
            {"expected_policy_checksum": "f" * 64},
            "execution_plan_policy_checksum_mismatch",
        ),
    ):
        runner = _MetricRunner()
        result = await PlanExecutor(metric_runner=runner).execute(
            query_plan=plan,
            context=context,
            execution_plan=execution_plan,
            validation=record,
            expected_policy_version=overrides.get(
                "expected_policy_version", record.policy_version
            ),
            expected_policy_checksum=overrides.get(
                "expected_policy_checksum", record.policy_checksum
            ),
            budget=RouteBudgetLedger(route="fast"),
            deadline_ms=4_000,
        )
        assert result.record.status == "failed"
        assert result.record.stop_reason == expected
        assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_plan_executor_refuses_a_non_allow_or_mismatched_validation() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(plan=plan, context=context, identity=_identity())
    execution_plan = PlanCompiler().compile(plan=plan, context=context, validation=validation)
    denied = PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        outcome="deny",
        query_plan_sha256=plan.checksum,
        context_checksum=context.checksum,
        execution_plan_sha256=execution_plan.checksum,
        issues=(PlanValidationIssue(code="denied", safe_message="denied"),),
    )
    stale_query = PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        outcome="allow",
        query_plan_sha256="0" * 64,
        context_checksum=context.checksum,
        execution_plan_sha256=execution_plan.checksum,
    )
    stale_context = PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        outcome="allow",
        query_plan_sha256=plan.checksum,
        context_checksum="0" * 64,
        execution_plan_sha256=execution_plan.checksum,
    )
    for record, expected in (
        (denied, "execution_plan_validation_denied"),
        (stale_query, "execution_plan_query_hash_mismatch"),
        (stale_context, "execution_plan_context_hash_mismatch"),
    ):
        runner = _MetricRunner()
        result = await PlanExecutor(metric_runner=runner).execute(
            query_plan=plan,
            context=context,
            execution_plan=execution_plan,
            validation=record,
            expected_policy_version=_EXPECTED_POLICY.policy_version,
            expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
            budget=RouteBudgetLedger(route="fast"),
            deadline_ms=4_000,
        )
        assert result.record.status == "failed"
        assert result.record.stop_reason == expected
        assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_plan_executor_charges_before_execution_and_checkpoints_only_receipts() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    execution_plan = PlanCompiler().compile(
        plan=plan,
        context=context,
        validation=validation,
    )
    runner = _MetricRunner()
    ledger = RouteBudgetLedger(route="fast")

    result = await PlanExecutor(metric_runner=runner).execute(
        query_plan=plan,
        context=context,
        execution_plan=execution_plan,
        validation=_execution_validation(plan, context, execution_plan),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=ledger,
        deadline_ms=4_000,
    )

    assert result.record.status == "succeeded"
    assert result.outputs["fetch_metrics"] == {"revenue": 4242}
    assert result.record.output_step_ids == ("fetch_metrics", "verify_result")
    assert runner.prepare_calls == 1
    assert runner.execute_calls == 1
    assert ledger.sql_candidates == 1
    assert ledger.sql_executions == 1
    assert ledger.model_calls == 0
    checkpoint = result.record.model_dump_json()
    assert "SELECT synthetic_revenue" not in checkpoint
    _assert_business_payload_absent(checkpoint)


@pytest.mark.asyncio
async def test_plan_executor_preserves_the_response_deadline_reserve() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    execution_plan = PlanCompiler().compile(
        plan=plan,
        context=context,
        validation=validation,
    )
    runner = _MetricRunner()
    ledger = RouteBudgetLedger(route="fast")

    result = await PlanExecutor(metric_runner=runner).execute(
        query_plan=plan,
        context=context,
        execution_plan=execution_plan,
        validation=_execution_validation(plan, context, execution_plan),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=ledger,
        deadline_ms=ledger.policy.reserve_ms,
    )

    assert result.record.status == "deadline_exceeded"
    assert result.record.stop_reason == "deadline_reserve"
    assert runner.prepare_calls == 0
    assert runner.execute_calls == 0
    assert ledger.sql_candidates == 0
    assert ledger.sql_executions == 0


@pytest.mark.asyncio
async def test_plan_executor_rejects_non_json_step_output() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    execution_plan = PlanCompiler().compile(
        plan=plan,
        context=context,
        validation=validation,
    )

    result = await PlanExecutor(metric_runner=_MetricRunner(float("nan"))).execute(
        query_plan=plan,
        context=context,
        execution_plan=execution_plan,
        validation=_execution_validation(plan, context, execution_plan),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="fast"),
        deadline_ms=4_000,
    )

    assert result.record.status == "failed"
    assert result.record.stop_reason == "plan_step_output_not_json"
    assert result.record.step_receipts[-1].error_code == "plan_step_output_not_json"


@pytest.mark.asyncio
async def test_trusted_calculation_step_uses_only_the_approved_registry() -> None:
    context = _context()
    plan = _query_plan()
    query_validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    assert query_validation.outcome == "allow"
    fetch = FetchMetricStep(
        step_id="fetch_metrics",
        metric_keys=plan.metric_keys,
    )
    calculate = TrustedCalculationStep(
        step_id="calculate_ratio",
        template_id="ratio",
        input_refs={
            "numerator": "fetch_metrics.numerator",
            "denominator": "fetch_metrics.denominator",
        },
        depends_on=(fetch.step_id,),
    )
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(calculate.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(calculate.step_id,),
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(fetch, calculate, verify),
    )
    unapproved = PlanValidator().validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=RouteBudgetLedger(route="fast").limits,
    )
    assert unapproved.outcome == "approval"
    assert [issue.code for issue in unapproved.issues] == [
        "trusted_calculation_approval_required"
    ]

    validator = PlanValidator(approved_template_ids=frozenset({"ratio"}))
    execution_validation = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=RouteBudgetLedger(route="fast").limits,
    )
    assert execution_validation.outcome == "allow"

    result = await PlanExecutor(
        metric_runner=_MetricRunner({"numerator": 6, "denominator": 3}),
        trusted_calculation_runner=RegistryTrustedCalculationRunner(),
    ).execute(
        query_plan=plan,
        context=context,
        execution_plan=execution_plan,
        validation=execution_validation,
        expected_policy_version=validator.policy_version,
        expected_policy_checksum=validator.policy_checksum,
        budget=RouteBudgetLedger(route="fast"),
        deadline_ms=4_000,
    )

    assert result.record.status == "succeeded"
    assert result.outputs["calculate_ratio"] == {"ratio": 2.0}


def test_canonical_provenance_cannot_be_authorized_by_a_template_allowlist() -> None:
    # Canonical metric authority is the resolved trusted binding only.  A step
    # carrying canonical provenance is DENIED when no catalog is available --
    # never downgraded to "approval" and never authorized by approved_template_ids.
    context = _context()
    plan = _query_plan()
    fetch = FetchMetricStep(step_id="fetch_metrics", metric_keys=plan.metric_keys)
    calculate = TrustedCalculationStep(
        step_id="calculate_metric",
        template_id="ratio",
        template_version="1.0",
        template_checksum="a" * 64,
        binding_checksum="b" * 64,
        output_metric_key="metric.revenue_ratio",
        input_refs={
            "numerator": "fetch_metrics.numerator",
            "denominator": "fetch_metrics.denominator",
        },
        depends_on=(fetch.step_id,),
    )
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(calculate.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(calculate.step_id,),
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(fetch, calculate, verify),
    )
    for validator in (
        PlanValidator(),
        PlanValidator(approved_template_ids=frozenset({"ratio"})),
    ):
        record = validator.validate_execution_plan(
            execution_plan=execution_plan,
            query_plan=plan,
            context=context,
            route_budget=RouteBudgetLedger(route="fast").limits,
        )
        assert record.outcome == "deny"
        assert [issue.code for issue in record.issues] == [
            "trusted_calculation_binding_authority_missing"
        ]


def test_partial_calculation_provenance_is_rejected_at_construction() -> None:
    base: dict[str, object] = {
        "step_id": "calculate_metric",
        "template_id": "ratio",
        "input_refs": {"numerator": "fetch_metrics.numerator"},
        "depends_on": ("fetch_metrics",),
    }
    for partial in (
        {"binding_checksum": "b" * 64},
        {"output_metric_key": "metric.revenue_ratio"},
        {"template_version": "1.0", "binding_checksum": "b" * 64},
    ):
        with pytest.raises(ValidationError):
            TrustedCalculationStep(**{**base, **partial})


@pytest.mark.asyncio
async def test_plan_executor_propagates_request_cancellation() -> None:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    execution_plan = PlanCompiler().compile(
        plan=plan,
        context=context,
        validation=validation,
    )
    runner = _BlockingMetricRunner()
    task = asyncio.create_task(
        PlanExecutor(metric_runner=runner).execute(
            query_plan=plan,
            context=context,
            execution_plan=execution_plan,
            validation=_execution_validation(plan, context, execution_plan),
            expected_policy_version=_EXPECTED_POLICY.policy_version,
            expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
            budget=RouteBudgetLedger(route="fast"),
            deadline_ms=4_000,
        )
    )
    await asyncio.wait_for(runner.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_business_payload_absence_check_is_structural_not_substring() -> None:
    false_positive = {
        "trace_events": [
            {
                "name": "received",
                "timestamp": "2026-09-18T07:08:32.424208+00:00",
                "checkpoint_id": "9f8e7d6c-5b4a-3210-9876-543210fedcba",
                "checksum": "4242" * 16,
                "sequence": 424208,
            }
        ],
        "budget_record": {"sql_candidates": 1, "sql_executions": 1},
    }
    _assert_business_payload_absent(false_positive)
    _assert_business_payload_absent(json.dumps(false_positive, sort_keys=True, default=str))

    # Structural and JSON-encoded persistence of the raw payload.
    structural_leaks = (
        {"revenue": 4242},
        {"outputs": {"revenue": 4242}},
        {"a": {"b": {"c": {"revenue": 4242}}}},
        {"items": [{"revenue": 4242}]},
        [{"revenue": 4242}],
        json.dumps({"outputs": {"revenue": 4242}}),
    )
    # Non-JSON renderings that json.dumps(..., default=str) writes verbatim.
    rendered_leaks = (
        "FetchMetricResult(revenue=4242)",
        str(("revenue", 4242)),
        "{'revenue': 4242}",
    )
    for leak in structural_leaks + rendered_leaks:
        with pytest.raises(AssertionError, match="raw business payload persisted at"):
            _assert_business_payload_absent(leak)
        with pytest.raises(AssertionError, match="raw business payload persisted at"):
            _assert_business_payload_absent(json.dumps(leak, sort_keys=True))


@pytest.mark.asyncio
async def test_engine_uses_zero_model_fast_path_for_a_validated_typed_plan() -> None:
    context = _context()
    plan = _query_plan()
    context_resolver = _StaticContextResolver(context)
    plan_provider = _StaticQueryPlanProvider(plan)
    metric_runner = _MetricRunner()
    provider = _CountingProvider()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(provider),
        context_resolver=context_resolver,
        query_plan_provider=plan_provider,
        plan_executor=PlanExecutor(metric_runner=metric_runner),
    )
    request_context = RequestContext(
        identity=_identity(),
        thread_id=THREAD_ID,
        trace_id="trace-fast-typed",
        deadline_ms=4_000,
    )
    config = runtime_config(request_context)

    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        config,
    )
    snapshot = await engine.aget_state(config)
    checkpoint_values = cast(dict[str, object], snapshot.values)

    assert result["route_record"]["route"] == "fast"
    assert result["route_record"]["reason"] == "low_risk_high_confidence"
    assert result["execution_record"]["status"] == "succeeded"
    assert result["budget_record"]["usage"] == {
        "model_calls": 0,
        "sql_candidates": 1,
        "sql_executions": 1,
        "join_hops": 0,
        "repairs": 0,
        "retryable_provider_errors": 0,
    }
    assert result["model_receipt"] is None
    assert provider.calls == 0
    assert context_resolver.calls == 1
    assert plan_provider.calls == 1
    assert metric_runner.execute_calls == 1
    answer_text = result["messages"][-1].content
    assert answer_text == "metric.revenue: 4242"
    degradation_flags = result["degradation_flags"]
    assert "GroundedAnswerPending" not in degradation_flags
    assert "GroundedAnswerFreshnessUnknown" in degradation_flags
    # S1d explicit guard: the paired-key guard below does not catch a bare number,
    # so assert directly that the persisted message carries neither raw rows nor
    # SQL.  The grounded scalar itself is allowed to appear.
    assert "4242" in answer_text
    assert "rows" not in answer_text
    assert "SELECT" not in answer_text
    assert "{" not in answer_text
    assert "}" not in answer_text
    execution_checkpoint = json.dumps(result["execution_record"], sort_keys=True)
    assert "outputs" not in execution_checkpoint
    assert "revenue" not in execution_checkpoint
    _assert_business_payload_absent(execution_checkpoint)
    serialized_checkpoint = json.dumps(checkpoint_values, default=str, sort_keys=True)
    assert "SELECT synthetic_revenue" not in serialized_checkpoint
    _assert_business_payload_absent(serialized_checkpoint)
    assert [event["name"] for event in result["trace_events"]] == [
        "received",
        "context_compiled",
        "query_plan_proposed",
        "query_plan_validated",
        "route_selected",
        "execution_plan_compiled",
        "execution_plan_validated",
        "typed_plan_executed",
        "typed_execution_completed",
    ]


@pytest.mark.asyncio
async def test_engine_rejects_an_execution_validation_from_a_foreign_policy() -> None:
    from src.nl2sql.agents.dynamic_calc.trusted_templates import (
        trusted_template_registry,
    )
    from src.nl2sql.orchestration.approved_compute import (
        ApprovedCalculationBinding,
        ApprovedCalculationCatalog,
        ApprovedCalculationInput,
    )

    context = _context()
    plan = _query_plan()
    metric_runner = _MetricRunner()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(_CountingProvider()),
        context_resolver=_StaticContextResolver(context),
        query_plan_provider=_StaticQueryPlanProvider(plan),
        plan_executor=PlanExecutor(metric_runner=metric_runner),
    )
    request_context = RequestContext(
        identity=_identity(),
        thread_id=THREAD_ID,
        trace_id="trace-policy-swap",
        deadline_ms=4_000,
    )
    config = runtime_config(request_context)
    first = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    assert first["execution_record"]["status"] == "succeeded"
    assert metric_runner.execute_calls == 1

    snapshot = await engine.aget_state(config)
    stored = cast(dict[str, object], snapshot.values)["execution_plan_validation"]
    assert isinstance(stored, dict)

    meta = trusted_template_registry.metadata("ratio")
    catalog = ApprovedCalculationCatalog([
        ApprovedCalculationBinding(
            canonical_metric_key="metric.revenue",
            template_id="ratio",
            template_version=meta.version,
            template_checksum=meta.checksum,
            inputs=(
                ApprovedCalculationInput(
                    role="numerator",
                    metric_key="metric.stores",
                    metric_contract_sha256="b" * 64,
                ),
            ),
            binding_revision="1",
            semantic_release_id="release-1",
            semantic_release_checksum="c" * 64,
        )
    ])
    catalog_checksum = PlanValidator(calculation_catalog=catalog).policy_checksum
    assert catalog_checksum != PlanValidator().policy_checksum

    for foreign_checksum in ("0" * 64, catalog_checksum):
        tampered = dict(stored)
        tampered["policy_checksum"] = foreign_checksum
        await engine.aupdate_state(
            config, {"execution_plan_validation": tampered}, as_node="compile"
        )
        resumed = await engine.ainvoke(None, config)
        assert resumed["stop_reason"] == "execution_plan_validation_missing"
        assert metric_runner.execute_calls == 1
        assert "ExecutionPlanValidationMissing" in resumed["degradation_flags"]


@pytest.mark.asyncio
async def test_engine_resets_request_state_after_a_terminal_clarification() -> None:
    context_resolver = _SequenceContextResolver(
        (
            _context(
                resolution_status="incomplete",
                unresolved_slots=("metric",),
            ),
            _context(),
        )
    )
    provider = _CountingProvider()
    metric_runner = _MetricRunner()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(provider),
        context_resolver=context_resolver,
        query_plan_provider=_StaticQueryPlanProvider(_query_plan()),
        plan_executor=PlanExecutor(metric_runner=metric_runner),
    )
    config = runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-state-reset",
            deadline_ms=4_000,
        )
    )

    first = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        config,
    )
    # Typed suspension: the clarify run pauses with a server-owned request and
    # does not execute.  It is no longer a terminal stop_reason.
    assert first["stop_reason"] is None
    assert first["decision_status"] == "awaiting_decision"
    assert first["pending_decision"]["unresolved_slots"] == ["metric"]
    assert "__interrupt__" in first
    assert metric_runner.execute_calls == 0

    # A typed decision closes the suspension without executing business work.
    resumed = await engine.ainvoke(
        Command(resume={"action": "reject", "idempotency_key": "reset-1"}),
        config,
    )
    assert resumed["decision_status"] == "rejected"
    assert resumed["stop_reason"] is None
    assert metric_runner.execute_calls == 0

    # A fresh run on the SAME thread starts clean (no stale suspension state).
    second = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        config,
    )
    assert second["stop_reason"] is None
    assert second["decision_status"] is None
    assert second["pending_decision"] is None
    assert second["route_record"]["route"] == "fast"
    assert second["execution_record"]["status"] == "succeeded"
    assert second["trace_events"][0]["name"] == "received"
    assert len(second["trace_events"]) == 9
    assert context_resolver.calls == 2
    assert metric_runner.execute_calls == 1
    assert provider.calls == 0


class _PolicyEvidenceProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def retrieve_permitted(
        self,
        *,
        question: str,
        identity: RequestIdentity,
    ) -> PolicyScopedEvidence:
        assert question == "show revenue"
        assert identity == _identity()
        self.calls += 1
        return PolicyScopedEvidence(
            lexical=(
                Evidence(
                    evidence_id="metric.revenue",
                    content="Synthetic revenue metric",
                    source="lexical",
                    metadata={
                        "domain": "finance",
                        "relation_id": "relation.revenue_daily",
                    },
                ),
            ),
            embedding_available=False,
            resolution_status="resolved",
            degradation_flags=("synthetic_fixture",),
        )


class _StaticContextCompiler(ContextCompiler):
    def __init__(self) -> None:
        self.route: str | None = None
        self._release = SemanticRelease(
            release_id=str(RELEASE_ID),
            version=1,
            checksum="b" * 64,
            state=SemanticReleaseState.ACTIVE,
            documents=(),
            validation_report={"ok": True},
            change_summary="synthetic fixture",
            previous_release_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            schema_snapshot_id=str(SNAPSHOT_ID),
        )

    def compile(
        self,
        *,
        route: Route,
        lexical: Iterable[Evidence],
        vector: Iterable[Evidence] | None,
        graph: Iterable[Evidence],
        embedding_available: bool,
    ) -> RetrievedContextBundle:
        del vector, graph, embedding_available
        self.route = route
        return RetrievedContextBundle(
            semantic_release_id=str(RELEASE_ID),
            evidence=tuple(lexical),
            token_cost=10,
            confidence=0.9,
            degraded=True,
            degradation_reasons=("embedding_unavailable",),
        )

    def release(self, release_id: str) -> SemanticRelease:
        assert release_id == str(RELEASE_ID)
        return self._release


@pytest.mark.asyncio
async def test_semantic_context_adapter_binds_policy_evidence_to_release_snapshot() -> None:
    compiler = _StaticContextCompiler()
    provider = _PolicyEvidenceProvider()
    resolver = SemanticContextResolver(
        compiler=compiler,
        evidence_provider=provider,
    )

    context = await resolver.resolve(
        question="show revenue",
        identity=_identity(),
        route_hint="fast",
    )

    assert provider.calls == 1
    assert compiler.route == "fast"
    assert context.semantic_release_id == RELEASE_ID
    assert context.schema_snapshot_id == SNAPSHOT_ID
    assert len(context.checksum) == 64
    assert context.asset_ids == ("metric.revenue",)
    assert context.approved_relation_ids == ("relation.revenue_daily",)
    assert context.degradation_flags == (
        "synthetic_fixture",
        "embedding_unavailable",
    )

def _succeeded_fetch_record(
    *,
    execution_plan: ExecutionPlan,
    step_id: str,
    **receipt_overrides: object,
) -> PlanExecutionRecord:
    fields: dict[str, object] = {
        "step_id": step_id,
        "kind": "fetch_metric",
        "status": "succeeded",
        "elapsed_ms": 1,
        "output_digest": "b" * 64,
    }
    fields.update(receipt_overrides)
    return PlanExecutionRecord(
        execution_plan_checksum=execution_plan.checksum,
        status="succeeded",
        step_receipts=(PlanStepReceipt(**fields),),
        output_step_ids=(step_id,),
    )


def _compiled_execution_plan() -> ExecutionPlan:
    context = _context()
    plan = _query_plan()
    validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=context,
        identity=_identity(),
    )
    return PlanCompiler().compile(plan=plan, context=context, validation=validation)


def test_answer_fact_defaults_every_descriptive_field_to_absent() -> None:
    fact = AnswerFact(
        fact_id="a" * 64,
        step_id="fetch_metrics",
        metric_key="metric.revenue",
        value="87.30",
    )

    assert fact.status == "grounded"
    assert fact.value == "87.30"
    assert fact.unit is None
    assert fact.time_range is None
    assert fact.dimension is None
    assert fact.quality is None
    assert fact.freshness_explanation is None
    assert fact.confidence_band is None
    assert fact.source_id is None
    assert fact.rowset_sha256 is None
    assert fact.freshness_status == "unknown"


def test_answer_artifact_is_constructible_without_a_confidence_producer() -> None:
    artifact = AnswerArtifact()

    assert artifact.confidence_band is None
    assert artifact.facts == ()
    assert artifact.degradation_flags == ()


def test_answer_fact_is_frozen_and_rejects_an_unavailable_value() -> None:
    with pytest.raises(ValidationError):
        AnswerFact(
            fact_id="a" * 64,
            step_id="fetch_metrics",
            metric_key="metric.revenue",
            status="unavailable",
            value=1,
        )

    fact = AnswerFact(
        fact_id="a" * 64,
        step_id="fetch_metrics",
        metric_key="metric.revenue",
        value=1,
    )
    with pytest.raises(ValidationError):
        fact.value = 2  # type: ignore[misc]


def test_grounding_marks_no_data_unavailable_without_inventing_fields() -> None:
    plan = _query_plan()
    execution_plan = _compiled_execution_plan()
    record = _succeeded_fetch_record(
        execution_plan=execution_plan,
        step_id="fetch_metrics",
        rowset_sha256="c" * 64,
        source_id="warehouse.revenue",
        semantic_signature="d" * 64,
        freshness_status="fresh",
    )
    outputs = {"fetch_metrics": {"rows": [], "no_data": True}}

    facts = build_answer_facts(
        query_plan=plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
    )

    assert len(facts) == 1
    fact = facts[0]
    assert fact.status == "unavailable"
    assert fact.value is None
    assert fact.rowset_sha256 == "c" * 64
    assert fact.output_digest == "b" * 64
    assert fact.source_id == "warehouse.revenue"
    assert fact.semantic_signature == "d" * 64
    assert fact.freshness_status == "fresh"
    assert fact.unit is None
    assert fact.confidence_band is None
    assert receipt_degradation_flags(record) == ()

    rebuilt = build_answer_facts(
        query_plan=plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
    )
    assert rebuilt[0].fact_id == fact.fact_id


def test_grounded_renderer_keeps_raw_rows_out_and_marks_stale_freshness() -> None:
    plan = _query_plan()
    execution_plan = _compiled_execution_plan()
    record = _succeeded_fetch_record(
        execution_plan=execution_plan,
        step_id="fetch_metrics",
        freshness_status="stale",
        source_degradation=("aggregate_stale",),
    )
    outputs = {
        "fetch_metrics": {
            "rows": [{"value": "87.30", "region_code": "east"}],
            "no_data": False,
        }
    }

    facts = build_answer_facts(
        query_plan=plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
    )
    text = render_grounded_answer(query_plan=plan, facts=facts)

    assert len(facts) == 1
    assert facts[0].status == "grounded"
    assert facts[0].value == "87.30"
    assert text == 'metric.revenue: "87.30"'
    assert "region_code" not in text
    assert "rows" not in text
    assert receipt_degradation_flags(record) == (
        "aggregate_stale",
        "GroundedAnswerStale",
    )


def test_grounded_trend_renderer_preserves_business_period_mapping() -> None:
    fact = AnswerFact(
        fact_id="e" * 64,
        step_id="fetch_metrics",
        metric_key="metric.revenue",
        value=75,
        time_range=TimeRange(
            start=date(2026, 9, 12), end=date(2026, 9, 12)
        ),
    )
    rendered = render_grounded_answer(query_plan=_query_plan(), facts=(fact,))
    assert rendered == "metric.revenue [2026-09-12]: 75"
    assert "rows" not in rendered
    assert "SELECT" not in rendered


@pytest.mark.asyncio
async def test_engine_renders_a_grounded_scalar_without_raw_rows_or_sql() -> None:
    context = _context()
    plan = _query_plan()
    runner = _MetricRunner(
        {"rows": [{"value": 4242, "region_code": "east"}], "no_data": False}
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(_CountingProvider()),
        context_resolver=_StaticContextResolver(context),
        query_plan_provider=_StaticQueryPlanProvider(plan),
        plan_executor=PlanExecutor(metric_runner=runner),
    )
    config = runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-grounded-rows",
            deadline_ms=4_000,
        )
    )

    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        config,
    )
    answer_text = result["messages"][-1].content

    assert answer_text == "metric.revenue: 4242"
    assert "region_code" not in answer_text
    assert "rows" not in answer_text
    assert "SELECT" not in answer_text
    assert "GroundedAnswerPending" not in result["degradation_flags"]
    execution_checkpoint = json.dumps(result["execution_record"], sort_keys=True)
    assert "outputs" not in execution_checkpoint
    _assert_business_payload_absent(execution_checkpoint)
    snapshot = await engine.aget_state(config)
    serialized = json.dumps(
        cast(dict[str, object], snapshot.values), default=str, sort_keys=True
    )
    assert "region_code" not in serialized
    assert "SELECT synthetic_revenue" not in serialized
    _assert_business_payload_absent(serialized)


@pytest.mark.asyncio
async def test_engine_renders_structured_unavailable_for_no_data() -> None:
    context = _context()
    plan = _query_plan()
    runner = _MetricRunner({"rows": [], "no_data": True})
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(_CountingProvider()),
        context_resolver=_StaticContextResolver(context),
        query_plan_provider=_StaticQueryPlanProvider(plan),
        plan_executor=PlanExecutor(metric_runner=runner),
    )
    config = runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-grounded-no-data",
            deadline_ms=4_000,
        )
    )

    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        config,
    )

    assert result["messages"][-1].content == "metric.revenue: no data returned"
    assert "GroundedAnswerPending" not in result["degradation_flags"]
    assert result["execution_record"]["status"] == "succeeded"
