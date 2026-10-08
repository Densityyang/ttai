"""Coding-C INDEPENDENT acceptance tests (first non-live pass).

Written by the independent acceptance owner, NOT by the implementation
authors.  These tests attack claims the existing suites could satisfy via a
false positive: Mode2 accounting is asserted on the REAL budget_record AND
against a provider that genuinely counts invocations; Mode2 evidence safety
inspects the ACTUAL outbound provider payload; Mode3 server-resolved input is
attacked with adversarial extra fields on the REAL contract shape; the stock
app is used for route/permission gates; conflict output is searched for
winner-like semantics in the SERIALIZED body.

DECLARED MOCK BOUNDARY - read this before trusting a green run.  The
database-execution boundary is MOCKED in this module, so it is NOT a
real-execution acceptance: the engine is composed around the REAL
MetricQueryCompiler and PlanExecutor, but the QueryGateway is built over an
AsyncMock session and its execute is replaced by an AsyncMock returning a fixed
QueryReceipt (see the engine helper below).  Consequently no SQL ever reaches
PostgreSQL here, and no dialect, parameter-binding, row-shape or database
permission defect can be caught by this module.  What it does prove is the
orchestration, accounting and gating behaviour AROUND that boundary.  The REAL
execution path is covered by tests/integration (test_query_gateway_postgres.py,
test_postgres_governance.py) and by the live lane, not here.

No production file is modified by this module.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from main import create_app
from src.core.auth.dependencies import (
    _required_nl2sql_permission,
    require_nl2sql_permission,
)
from src.core.auth.types import AuthUser
from src.core.settings import Settings
from src.nl2sql.artifacts.api_definitions import register_definition_routes
from src.nl2sql.artifacts.custom_definition_execution_service import (
    CustomDefinitionExecutionService,
)
from src.nl2sql.artifacts.definition_revalidation import (
    ActiveReleaseEvidence,
    AuthorizationEvidence,
    DataSnapshotEvidence,
    DefinitionRevalidationGate,
    InputFreshnessDQ,
)
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.container import AppContainer
from src.nl2sql.contracts import (
    RequestContext,
    RouteBudget,
    RoutePolicy,
    RoutingBudgetPolicy,
    TimeRange,
)
from src.nl2sql.infra.llm.gateway import ProviderResponse, ProviderUnavailable
from src.nl2sql.orchestration.custom_calculation_execution import (
    CustomCalculationExecutionError,
    ResolvedCalculationInput,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.governed_calculation_inputs import (
    GovernedMetricInputResolutionError,
    TypedMetricCalculationInputResolver,
)
from src.nl2sql.orchestration.metric_query import SourceFreshnessRecord
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.ownership import runtime_config
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    LiteralOperand,
    ParameterBinding,
    ParameterRefOperand,
    ParameterSpec,
)
from src.nl2sql.v2 import BLOCK_MANIFEST
from tests.metric_fixtures import NOW, RELEASE_ID, SNAPSHOT_ID, MetricAuthority
from tests.unit.test_deterministic_query_path import (
    DISPLAY_NAME,
    _identity,
)
from tests.unit.test_deterministic_query_path import (
    _engine as _build_live_engine,
)

_QUESTION = f"{DISPLAY_NAME} time=2024-02-29"
_METRIC = "repair_service_archive_rate_overall_day"


# ---------------------------------------------------------------------------
# Mode2 harness: a REAL counting provider behind the REAL ModelGateway.
# ---------------------------------------------------------------------------


class _FreshAuthority(MetricAuthority):
    """Controlled freshness evidence; no synthetic business-value lane."""

    async def read_freshness(self, source_id: str) -> SourceFreshnessRecord:
        assert self.snapshot is not None
        return SourceFreshnessRecord(
            source_id=source_id,
            status="fresh",
            data_as_of=NOW,
            checkpoint="c-acceptance.v1",
            release_id=RELEASE_ID,
            snapshot_id=SNAPSHOT_ID,
            snapshot_checksum=self.snapshot.checksum,
        )

    def compiler(self, **overrides: Any):
        options: dict[str, Any] = {
            "read_freshness": self.read_freshness,
            "clock": lambda: NOW,
        }
        options.update(overrides)
        return super().compiler(**options)


class _CountingStructuredProvider:
    """A provider that counts REAL invocations and captures REAL payloads."""

    provider_name = "synthetic"

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls = 0
        self.payloads: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
        response_schema: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        del model, timeout_ms, max_output_tokens
        self.calls += 1
        self.payloads.append(
            {
                "messages": json.loads(json.dumps(messages, default=str)),
                "response_schema": response_schema,
            }
        )
        if isinstance(self.reply, Exception):
            raise self.reply
        content = (
            self.reply(messages) if callable(self.reply) else str(self.reply)
        )
        return ProviderResponse(
            content=content,
            model="acceptance-model",
            usage={"input_tokens": 3, "output_tokens": 3},
            finish_reason="stop",
        )

    async def list_models(self) -> tuple[str, ...]:
        return ("acceptance-model",)


def _structured_from_payload(
    messages: list[dict[str, Any]], *, value_override: object | None = None
) -> str:
    """Build a legal structured reply citing the ACTUAL supplied fact id."""

    user = messages[-1]["content"]
    facts_json = user.split("SOURCE FACTS\n", 1)[1].split(
        "\n\nREQUESTED INTERPRETATION", 1
    )[0]
    fact = json.loads(facts_json)["facts"][0]
    value = fact["value"] if value_override is None else value_override
    return json.dumps(
        {
            "summary": {
                "text": f"Observed value {value}.",
                "fact_ids": [fact["fact_id"]],
            },
            "observations": [],
            "caveats": [],
        }
    )


async def _analyze(
    *,
    route_policy: RoutePolicy | None = None,
    budget_policy: RoutingBudgetPolicy | None = None,
    reply: Any,
) -> tuple[dict[str, Any], _CountingStructuredProvider]:
    """Compose the REAL engine with MY counting provider behind a REAL gateway.

    The provider is wired into the ModelGateway itself (not monkeypatched onto a
    fixture provider), so a provider invocation is a genuine gateway invocation.
    """

    from unittest.mock import AsyncMock

    from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
    from src.nl2sql.infra.llm.gateway import ModelGateway, ModelProfile, ModelTarget
    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryPlanProvider,
    )
    from src.nl2sql.orchestration.metric_query import metric_plan_executor
    from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator

    authority = _FreshAuthority()
    gateway = QueryGateway(AsyncMock(), schema="ai_views")
    compiled = await authority.compiler().compile(authority.plan(), authority.context)
    fingerprint = gateway.prepare(compiled.sql).fingerprint
    gateway.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=QueryReceipt(
            accepted=True,
            sql="",
            sql_fingerprint=fingerprint,
            rows=[{"value": 7}],
            row_count=1,
            policy_outcome="allow",
            max_rows=200,
        )
    )
    provider = _CountingStructuredProvider(reply)
    model_gateway = ModelGateway(
        providers={provider.provider_name: provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "c-acceptance-v1",
                frozenset({"answer"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "c-acceptance-v1",
                frozenset({"plan"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
        },
    )
    from src.nl2sql.semantic.context_compiler import (
        ContextCompiler,
        SemanticContextResolver,
    )
    from src.nl2sql.semantic.policy_evidence import (
        ActiveReleaseRegistry,
        ReleaseScopedPolicyEvidenceProvider,
    )

    registry = ActiveReleaseRegistry()
    resolver = SemanticContextResolver(
        compiler=ContextCompiler(registry),
        evidence_provider=ReleaseScopedPolicyEvidenceProvider(
            authority.read_active,
            {authority.metric.source_ref: authority.binding.relation_asset_id},
            None,
            registry=registry,
        ),
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=model_gateway,
        context_resolver=resolver,
        query_plan_provider=DeterministicQueryPlanProvider(registry, lambda: NOW),
        plan_executor=metric_plan_executor(authority.compiler(), gateway),
        plan_validator=PlanValidator(),
        plan_compiler=PlanCompiler(),
        route_policy=route_policy,
        budget_policy=budget_policy,
    )
    thread_id = uuid4()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex, requested_mode="ANALYZE", effective_mode="ANALYZE"
    )
    result = dict(
        await engine.ainvoke(
            {
                "messages": [{"role": "user", "content": _QUESTION}],
                "run_envelope": envelope.model_dump(mode="json"),
            },
            runtime_config(context),
        )
    )
    return result, provider


async def _query_result() -> tuple[dict[str, Any], int]:
    live = await _build_live_engine(_FreshAuthority(), rows=[{"value": 7}])
    thread_id = uuid4()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex, requested_mode="QUERY", effective_mode="QUERY"
    )
    result = dict(
        await live.engine.ainvoke(
            {
                "messages": [{"role": "user", "content": _QUESTION}],
                "run_envelope": envelope.model_dump(mode="json"),
            },
            runtime_config(context),
        )
    )
    return result, live.provider.calls


_ROUTE_POLICIES: dict[str, RoutePolicy] = {
    "fast": RoutePolicy(
        version="c-acc-fast",
        state="calibrated",
        fast_max_risk=100,
        fast_min_confidence=0,
        fast_max_tables=8,
        standard_max_risk=100,
        standard_min_confidence=0,
    ),
    "standard": RoutePolicy(
        version="c-acc-standard",
        state="calibrated",
        fast_max_risk=0,
        fast_min_confidence=1,
        fast_max_tables=1,
        standard_max_risk=100,
        standard_min_confidence=0,
    ),
    "deep": RoutePolicy(
        version="c-acc-deep",
        state="calibrated",
        fast_max_risk=0,
        fast_min_confidence=1,
        fast_max_tables=1,
        standard_max_risk=0,
        standard_min_confidence=1,
    ),
}


# ==========================================================================
# 1. Mode2 resource accounting: real calls == budget_record, per route.
# ==========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["fast", "standard", "deep"])
async def test_mode2_accounting_matches_real_calls_on_every_route(route: str) -> None:
    result, provider = await _analyze(
        route_policy=_ROUTE_POLICIES[route],
        reply=_structured_from_payload,
    )

    assert result["route_record"]["route"] == route, "route must be the selected one"
    # The REAL provider saw exactly one invocation...
    assert provider.calls == 1
    # ...and the checkpointed budget must agree with reality.
    budget = result["budget_record"]
    assert budget["usage"]["model_calls"] == 1
    assert budget["route"] == route
    assert budget["limits"]["max_model_calls"] >= 1
    # A successful interpretation must be presented as such AND be accounted.
    assert result.get("stop_reason") is None
    assert result.get("model_receipt") is not None
    assert [b["type"] for b in result["response_blocks"]] == ["text", "provenance"]


@pytest.mark.asyncio
async def test_query_records_zero_model_calls_and_provider_never_ran() -> None:
    result, provider_calls = await _query_result()
    assert provider_calls == 0, "QUERY reached a real model provider"
    assert result["run_envelope"]["effective_mode"] == "QUERY"
    assert result["budget_record"]["usage"]["model_calls"] == 0
    assert result["budget_record"]["usage"]["sql_executions"] == 1


# ==========================================================================
# 2. Mode2 failure accounting.
# ==========================================================================


@pytest.mark.asyncio
async def test_invalid_json_accounts_the_attempt_and_offers_no_prose() -> None:
    result, provider = await _analyze(reply="not json at all")
    assert provider.calls == 1
    assert result["budget_record"]["usage"]["model_calls"] == 1
    assert result["stop_reason"] == "analysis_interpretation_invalid"
    assert result.get("response_blocks", []) == []
    joined = json.dumps(result, default=str).lower()
    assert "not json at all" not in joined, "raw model prose leaked as an answer"


@pytest.mark.asyncio
async def test_invented_numeric_claim_fails_closed_and_is_accounted() -> None:
    result, provider = await _analyze(
        reply=lambda m: _structured_from_payload(m, value_override=4242)
    )
    assert provider.calls == 1
    assert result["budget_record"]["usage"]["model_calls"] == 1
    assert result["stop_reason"] == "analysis_interpretation_invalid"
    assert result.get("response_blocks", []) == []
    # The invented number must not reach ANY product-visible surface.  The raw
    # model text survives only inside the operator-facing scrubbed receipt
    # (execution evidence), never as an answer, block or message.
    visible = json.dumps(
        {
            "messages": [
                getattr(m, "content", str(m)) for m in result.get("messages", [])
            ],
            "blocks": result.get("response_blocks", []),
            "grounded_answer_text": result.get("grounded_answer_text"),
        },
        default=str,
    )
    assert "4242" not in visible, visible


@pytest.mark.asyncio
async def test_provider_failure_is_accounted_after_the_attempt() -> None:
    result, provider = await _analyze(
        reply=ProviderUnavailable(
            "provider_request_rejected",
            provider="synthetic",
            retryable=False,
            status_code=401,
        )
    )
    assert provider.calls == 1
    budget = result["budget_record"]
    assert budget["usage"]["model_calls"] == 1
    assert budget["error_counts"].get("provider_request_rejected") == 1
    assert result.get("response_blocks", []) == []


@pytest.mark.asyncio
async def test_model_budget_denial_prevents_any_real_invocation() -> None:
    zero = RoutingBudgetPolicy(
        version="c-acc-zero",
        state="calibrated",
        routes={
            name: RouteBudget(
                deadline_ms=30_000,
                max_model_calls=0,
                max_sql_candidates=2,
                max_sql_executions=2,
                max_join_hops=2,
                max_repairs=1,
            )
            for name in ("fast", "standard", "deep")
        },
        reserve_ms=800,
        max_same_sql=2,
        max_same_error=2,
        max_retryable_provider_errors=1,
    )
    result, provider = await _analyze(
        budget_policy=zero, reply=_structured_from_payload
    )
    assert provider.calls == 0, "budget denial must prevent the real call"
    assert result["budget_record"]["usage"]["model_calls"] == 0
    assert result["stop_reason"] == "model_call_budget_exhausted"
    # A run that never invoked a model must NOT present an interpretation.
    assert result.get("response_blocks", []) == []
    assert result.get("model_receipt") is None


# ==========================================================================
# 3. Mode2 evidence safety / checkpoint safety.
# ==========================================================================


@pytest.mark.asyncio
async def test_model_visible_input_is_bounded_governed_evidence_only() -> None:
    result, provider = await _analyze(reply=_structured_from_payload)
    assert result["budget_record"]["usage"]["model_calls"] == 1
    assert len(provider.payloads) == 1
    blob = json.dumps(provider.payloads[0], ensure_ascii=False).lower()

    for forbidden in (
        "select ",
        "from ai_views",
        "insert ",
        "password",
        "api_key",
        "authorization_context",
        "allowed_scope_ids",
        "authorization_revision",
        "bearer ",
        "postgresql://",
        "-----begin",
        '"rows"',
    ):
        assert forbidden not in blob, forbidden
    # It must CONTAIN bounded governed evidence.
    assert "source facts" in blob
    assert "fact_id" in blob
    assert "evidence_checksum" in blob


def test_checkpoint_state_declares_no_raw_or_bundle_payload() -> None:
    from src.nl2sql.orchestration.engine import V2EngineState

    annotations = set(V2EngineState.__annotations__)
    for forbidden in (
        "analysis_evidence",
        "analysis_model_input",
        "raw_execution_rows",
        "model_input",
        "sql",
        "outputs",
    ):
        assert forbidden not in annotations, forbidden
    assert {"grounded_answer_artifact", "grounded_answer_text"} <= annotations


@pytest.mark.asyncio
async def test_persisted_state_carries_no_sql_or_raw_rows() -> None:
    result, _ = await _analyze(reply=_structured_from_payload)
    persisted = json.dumps(
        {key: value for key, value in result.items() if key != "messages"},
        default=str,
    ).lower()
    for forbidden in ("select ", "ai_views.v_", '"rows"', "postgresql://"):
        assert forbidden not in persisted, forbidden


# ==========================================================================
# 4. Mode2 fact vs interpretation separation.
# ==========================================================================


@pytest.mark.asyncio
async def test_public_output_separates_source_facts_from_interpretation() -> None:
    result, _ = await _analyze(reply=_structured_from_payload)
    block = result["response_blocks"][0]
    assert block["type"] == "text"
    text = block["text"]
    assert "SOURCE FACTS" in text
    assert "MODEL INTERPRETATION" in text
    assert text.index("SOURCE FACTS") < text.index("MODEL INTERPRETATION")
    provenance = result["response_blocks"][1]
    assert provenance["type"] == "provenance"
    assert provenance["fact_ids"]
    assert provenance["model"] == "acceptance-model"


# ==========================================================================
# 5. Mode3 shared runtime + generic resolver contract.
# ==========================================================================


def _actual_to_target_spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="custom.actual_to_target_index",
        expression=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="actual"),
                right=ParameterRefOperand(name="target_percent"),
            ),
            right=LiteralOperand(value=Decimal("100")),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual", provenance="published_gold", metric_key=_METRIC
            ),
        ),
        parameters=(ParameterSpec(name="target_percent", value_type="decimal"),),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


def _binding(spec: CalculationSpec, target: int) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="target_percent", value=target),),
    )


class _RevalidationAuthorization:
    async def current_authorization(self, **_: object) -> AuthorizationEvidence:
        return AuthorizationEvidence(authorization_revision="rev-current")


class _RevalidationRelease:
    async def active_release(self, **_: object) -> ActiveReleaseEvidence:
        return ActiveReleaseEvidence(release_id="rel-current", release_checksum="b" * 64)


class _RevalidationSnapshot:
    async def data_snapshot(self, **_: object) -> DataSnapshotEvidence:
        return DataSnapshotEvidence(
            snapshot_id="snap-current", snapshot_checksum="c" * 64
        )


class _RevalidationBudget:
    async def remaining_budget(self, **_: object) -> int:
        return 5


class _RevalidationFreshness:
    async def assess(self, **kwargs: object) -> tuple[InputFreshnessDQ, ...]:
        resolved = kwargs["resolved_inputs"]
        assert isinstance(resolved, tuple)
        return tuple(
            InputFreshnessDQ(
                role=item.role,
                metric_key=item.metric_key,
                freshness="fresh",
                dq="pass",
                data_as_of=item.data_as_of,
            )
            for item in resolved
        )


def _passing_revalidation() -> DefinitionRevalidationGate:
    """Current-state evidence the controlled acceptance doubles can prove."""

    return DefinitionRevalidationGate(
        authorization_provider=_RevalidationAuthorization(),
        active_release_provider=_RevalidationRelease(),
        data_snapshot_provider=_RevalidationSnapshot(),
        freshness_dq_provider=_RevalidationFreshness(),
        budget_provider=_RevalidationBudget(),
        governed_metric_authority=lambda _key: True,
    )


class _GovernedFetcher:
    """A source-agnostic governed fetcher double with SAFE evidence."""

    def __init__(self, actual: Decimal | None = Decimal("45"), **overrides: Any) -> None:
        self.actual = actual
        self.overrides = overrides
        self.calls: list[dict[str, object]] = []

    async def fetch_metric_input(self, **kwargs: object) -> ResolvedCalculationInput:
        self.calls.append(dict(kwargs))
        payload: dict[str, Any] = {
            "role": str(kwargs["role"]),
            "metric_key": str(kwargs["metric_key"]),
            "value": self.actual,
            "unit": "percent",
            "data_as_of": datetime(2026, 9, 20, tzinfo=UTC),
            "time_range": TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20)),
            "provenance": str(kwargs["required_provenance"]),
            "source_id": "gold.repair.archive",
            "receipt_step_id": "fetch_actual",
            "fact_id": "a" * 64,
        }
        payload.update(self.overrides)
        return ResolvedCalculationInput(**payload)


async def _execution_service(
    fetcher: Any | None,
) -> tuple[CustomDefinitionExecutionService, CustomDefinitionService, str]:
    definitions = CustomDefinitionService(governed_metric_keys={_METRIC})
    spec = _actual_to_target_spec()
    draft = await definitions.create_draft(
        owner_user_id="alice", title="Actual to target", calculation=spec
    )
    await definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    await definitions.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    await definitions.save(owner_user_id="alice", definition_id=draft.definition_id)
    service = CustomDefinitionExecutionService(
        definitions=definitions,
        input_resolver=(
            None if fetcher is None else TypedMetricCalculationInputResolver(fetcher)
        ),
        revalidation=_passing_revalidation(),
    )
    return service, definitions, draft.definition_id


@pytest.mark.asyncio
async def test_controlled_flow_through_the_real_service_and_shared_runtime() -> None:
    fetcher = _GovernedFetcher(Decimal("45"))
    service, definitions, definition_id = await _execution_service(fetcher)
    spec = _actual_to_target_spec()

    first = await service.execute(
        owner_user_id="alice",
        definition_id=definition_id,
        version=1,
        binding=_binding(spec, 90),
    )
    second = await service.execute(
        owner_user_id="alice",
        definition_id=definition_id,
        version=1,
        binding=_binding(spec, 75),
    )

    assert first.result.value == Decimal("50.00")
    assert second.result.value == Decimal("60.00")
    # Changing ONLY a parameter VALUE never creates a new Definition version.
    assert first.version == second.version == 1
    assert first.definition_checksum == second.definition_checksum
    assert len(await definitions.list_owned(owner_user_id="alice")) == 1
    assert first.result.spec_checksum == second.result.spec_checksum
    # The governed evidence is carried in the result provenance.
    provenance = first.result.input_provenance[0]
    assert provenance.metric_key == _METRIC
    assert provenance.source_id == "gold.repair.archive"
    assert provenance.receipt_step_id == "fetch_actual"
    assert provenance.fact_id == "a" * 64
    assert first.result.data_as_of == datetime(2026, 9, 20, tzinfo=UTC)


@pytest.mark.asyncio
async def test_zero_target_is_undefined_division_not_no_data() -> None:
    fetcher = _GovernedFetcher(Decimal("45"))
    service, _, definition_id = await _execution_service(fetcher)
    spec = _actual_to_target_spec()
    with pytest.raises(CustomCalculationExecutionError) as raised:
        await service.execute(
            owner_user_id="alice",
            definition_id=definition_id,
            version=1,
            binding=_binding(spec, 0),
        )
    assert raised.value.code == "calculation_undefined_division_by_zero"
    assert raised.value.code != "NO_DATA"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"metric_key": "other.metric"}, "calculation_input_metric_mismatch"),
        ({"provenance": "ad_hoc_metric"}, "calculation_input_provenance_mismatch"),
        ({"status": "unavailable", "value": None}, "calculation_input_unavailable"),
        ({"data_as_of": None}, "calculation_input_data_as_of_missing"),
        ({"source_id": None}, "calculation_input_provenance_incomplete"),
        ({"receipt_step_id": None}, "calculation_input_provenance_incomplete"),
        ({"fact_id": None}, "calculation_input_provenance_incomplete"),
        ({"role": "wrong_role"}, "calculation_input_role_mismatch"),
    ],
)
async def test_resolver_rejects_every_incomplete_or_mismatched_evidence(
    overrides: dict[str, Any], expected: str
) -> None:
    fetcher = _GovernedFetcher(**overrides)
    service, _, definition_id = await _execution_service(fetcher)
    spec = _actual_to_target_spec()
    with pytest.raises(GovernedMetricInputResolutionError) as raised:
        await service.execute(
            owner_user_id="alice",
            definition_id=definition_id,
            version=1,
            binding=_binding(spec, 90),
        )
    assert raised.value.code == expected


def test_duplicate_role_declaration_is_rejected_before_any_resolution() -> None:
    """Duplicate declared roles fail closed at the CONTRACT boundary itself."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="calculation input roles must be unique"):
        CalculationSpec(
            calculation_id="custom.dup",
            expression=LiteralOperand(value=Decimal("1")),
            inputs=(
                CalculationInputSpec(
                    role="actual", provenance="published_gold", metric_key=_METRIC
                ),
                CalculationInputSpec(
                    role="actual", provenance="published_gold", metric_key=_METRIC
                ),
            ),
            unit="percent",
        )


@pytest.mark.asyncio
async def test_resolver_rejects_an_empty_or_unbound_input_contract() -> None:
    """The resolver re-checks the role contract even if a model bypasses Pydantic."""

    definitions = CustomDefinitionService()
    spec = _actual_to_target_spec()
    draft = await definitions.create_draft(
        owner_user_id="alice", title="dup", calculation=spec
    )
    resolver = TypedMetricCalculationInputResolver(_GovernedFetcher())
    forged = draft.model_copy(update={"calculation": spec.model_copy(update={"inputs": ()})})
    with pytest.raises(GovernedMetricInputResolutionError) as raised:
        await resolver.resolve_inputs(
            owner_user_id="alice",
            definition=forged,
            binding=_binding(spec, 90),
        )
    assert raised.value.code == "calculation_input_role_contract_invalid"


def test_generic_seam_is_source_agnostic() -> None:
    """The generic module must contain no local-real SQL, no synthetic values."""

    from src.nl2sql.orchestration import governed_calculation_inputs as module

    source = inspect.getsource(module)
    for forbidden in (
        "select ",
        "silver_repair_service",
        "v_repair_service",
        "QueryGateway",
        "local_real",
        "evaluate_calculation",
        "psycopg",
        "sqlalchemy",
    ):
        assert forbidden.lower() not in source.lower(), forbidden


# ==========================================================================
# 6. Stock-app gates: OpenAPI, routes, permission manifest.
# ==========================================================================


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth_required_permission_invoke="nl2sql:invoke",
        auth_required_permission_stream="nl2sql:stream",
    )


def _sample(path: str) -> str:
    return (
        path.replace("{thread_id}", "11111111-1111-1111-1111-111111111111")
        .replace("{definition_id}", "def_" + "a" * 32)
        .replace("{version}", "1")
    )


def _stock_api_routes(app: Any) -> list[tuple[str, str]]:
    """Every API (method, path) the stock app actually serves.

    This FastAPI version stores an included router as a wrapper object, so the
    nested APIRoutes are read from the original router it carries.
    """

    from fastapi.routing import APIRoute

    rows: list[tuple[str, str]] = []
    for candidate in app.routes:
        routers = [candidate]
        original = getattr(candidate, "original_router", None)
        if original is not None:
            routers.append(original)
        for router in routers:
            for route in getattr(router, "routes", ()) or ():
                if not isinstance(route, APIRoute):
                    continue
                for method in route.methods or set():
                    if method in {"HEAD", "OPTIONS"}:
                        continue
                    rows.append((method, route.path))
    return rows


def test_stock_app_builds_serves_openapi_without_duplicate_routes() -> None:
    app = create_app()
    spec = app.openapi()
    assert spec["paths"]

    rows = _stock_api_routes(app)
    seen = set(rows)
    assert len(seen) == len(rows), "a method/path pair is registered twice"
    expected = {
        ("POST", "/api/v2/nl2sql/queries"),
        ("POST", "/api/v2/nl2sql/queries/stream"),
        ("GET", "/api/v2/nl2sql/threads/{thread_id}"),
        ("GET", "/api/v2/nl2sql/threads/{thread_id}/history"),
        ("POST", "/api/v2/nl2sql/threads/{thread_id}/actions"),
        ("POST", "/api/v2/nl2sql/feedback"),
        ("GET", "/api/v2/nl2sql/capabilities"),
        ("POST", "/api/v2/nl2sql/definitions"),
        ("GET", "/api/v2/nl2sql/definitions"),
        ("GET", "/api/v2/nl2sql/definitions/{definition_id}/versions/{version}"),
        ("PATCH", "/api/v2/nl2sql/definitions/{definition_id}/draft"),
        ("POST", "/api/v2/nl2sql/definitions/{definition_id}/semantic-close"),
        ("POST", "/api/v2/nl2sql/definitions/{definition_id}/confirm"),
        ("POST", "/api/v2/nl2sql/definitions/{definition_id}/save"),
        ("POST", "/api/v2/nl2sql/definitions/{definition_id}/revisions"),
        (
            "POST",
            "/api/v2/nl2sql/definitions/{definition_id}/versions/{version}/publish",
        ),
        (
            "POST",
            "/api/v2/nl2sql/definitions/{definition_id}/versions/{version}/execute",
        ),
        ("GET", "/api/v2/nl2sql/library"),
        ("GET", "/api/v2/nl2sql/library/catalogue"),
        ("POST", "/api/v2/nl2sql/library/install"),
        ("POST", "/api/v2/nl2sql/library/uninstall"),
        ("POST", "/api/v2/nl2sql/library/star"),
        ("POST", "/api/v2/nl2sql/library/unstar"),
        ("POST", "/api/v2/nl2sql/library/upgrade"),
        ("POST", "/api/v2/nl2sql/library/acknowledge-withdrawal"),
        ("POST", "/api/v2/nl2sql/library/fork"),
        ("POST", "/api/v2/nl2sql/library/certify"),
        ("POST", "/api/v2/nl2sql/library/withdraw"),
        ("GET", "/api/v2/nl2sql/conflicts/personal"),
        ("POST", "/api/v2/nl2sql/conflicts/personal/select"),
    }
    missing = sorted(expected - seen)
    assert not missing, f"stock app is missing product routes: {missing}"


def test_every_registered_v2_route_resolves_to_an_explicit_permission() -> None:
    app = create_app()
    settings = _settings()
    checked = 0
    for method, path in _stock_api_routes(app):
        if not path.startswith("/api/v2/nl2sql"):
            continue
        resolved = _required_nl2sql_permission(method, _sample(path), settings)
        assert resolved in {
            settings.auth_required_permission_invoke,
            settings.auth_required_permission_stream,
        }, (method, path, resolved)
        checked += 1
    assert checked >= 30, f"only {checked} v2 routes were inspected"


def test_unknown_route_resolves_to_the_missing_policy_sentinel() -> None:
    settings = _settings()
    assert (
        _required_nl2sql_permission("GET", "/api/v2/nl2sql/not/a/route", settings)
        is None
    )
    assert (
        _required_nl2sql_permission("DELETE", "/api/v2/nl2sql/queries", settings) is None
    )


async def test_stock_container_produces_one_object_graph_with_injected_fetcher() -> None:
    fetcher = _GovernedFetcher()
    container = AppContainer(governed_metric_input_fetcher=fetcher)
    assert container.custom_definition_service() is container.custom_definition_service()
    assert await container.publication_catalogue() is await container.publication_catalogue()
    assert await container.publication_service() is await container.publication_service()
    assert await container.library_repository() is await container.library_repository()
    assert await container.product_library_service() is await container.product_library_service()
    assert (
        await container.personal_conflict_product_service()
        is await container.personal_conflict_product_service()
    )
    assert (
        container.custom_definition_execution_service()
        is container.custom_definition_execution_service()
    )
    assert container.calculation_input_resolver() is container.calculation_input_resolver()
    assert container.calculation_input_resolver() is not None
    # ONE catalogue, ONE definition service, no second instance.
    assert (
        (await container.product_library_service())._catalogue
        is await container.publication_catalogue()
    )
    assert (
        (await container.personal_conflict_product_service())._catalogue
        is await container.publication_catalogue()
    )


def test_container_without_fetcher_reports_resolver_unavailable() -> None:
    container = AppContainer()
    assert container.calculation_input_resolver() is None
    execution = container.custom_definition_execution_service()
    assert execution._input_resolver is None


# ==========================================================================
# 7. Mode3 HTTP: adversarial server-owned field injection must be rejected.
# ==========================================================================


class _HttpContainer:
    def __init__(self, fetcher: Any) -> None:
        self.definitions = CustomDefinitionService(governed_metric_keys={_METRIC})
        self.execution = CustomDefinitionExecutionService(
            definitions=self.definitions,
            input_resolver=TypedMetricCalculationInputResolver(fetcher),
            revalidation=_passing_revalidation(),
        )

    def custom_definition_service(self) -> CustomDefinitionService:
        return self.definitions

    def custom_definition_execution_service(self) -> CustomDefinitionExecutionService:
        return self.execution


async def _http_client(fetcher: Any) -> tuple[TestClient, str, CalculationSpec]:
    container = _HttpContainer(fetcher)
    app = FastAPI()
    app.state.container = container
    register_definition_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    spec = _actual_to_target_spec()
    draft = await container.definitions.create_draft(
        owner_user_id="alice", title="Actual to target", calculation=spec
    )
    await container.definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    await container.definitions.confirm(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    await container.definitions.save(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    return TestClient(app), draft.definition_id, spec


def _binding_body(spec: CalculationSpec, target: int) -> dict[str, Any]:
    return _binding(spec, target).model_dump(mode="json")


async def test_http_execute_computes_from_server_resolved_input() -> None:
    fetcher = _GovernedFetcher(Decimal("45"))
    client, definition_id, spec = await _http_client(fetcher)
    path = f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute"

    a = client.post(path, json={"binding": _binding_body(spec, 90)})
    b = client.post(path, json={"binding": _binding_body(spec, 75)})
    assert a.status_code == b.status_code == 200, a.text
    assert Decimal(a.json()["value"]) == Decimal("50.00")
    assert Decimal(b.json()["value"]) == Decimal("60.00")
    assert a.json()["version"] == b.json()["version"] == 1
    assert a.json()["definition_checksum"] == b.json()["definition_checksum"]
    assert a.json()["spec_checksum"] == b.json()["spec_checksum"]
    assert a.json()["binding_checksum"] != b.json()["binding_checksum"]
    assert [call["metric_key"] for call in fetcher.calls] == [_METRIC, _METRIC]
    assert [call["role"] for call in fetcher.calls] == ["actual", "actual"]
    assert [call["owner_user_id"] for call in fetcher.calls] == ["alice", "alice"]
    assert [call["required_provenance"] for call in fetcher.calls] == [
        "published_gold",
        "published_gold",
    ]


@pytest.mark.parametrize(
    "field",
    [
        "actual",
        "value",
        "unit",
        "data_as_of",
        "source_id",
        "receipt_step_id",
        "fact_id",
        "provenance",
        "metric_key",
        "role",
        "status",
        "input_checksum",
        "time_range",
        "owner_user_id",
        "definition_id",
        "version",
        "definition_checksum",
    ],
)
async def test_http_execute_rejects_every_server_owned_field(field: str) -> None:
    fetcher = _GovernedFetcher(Decimal("45"))
    client, definition_id, spec = await _http_client(fetcher)
    path = f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute"
    body = {"binding": _binding_body(spec, 90), field: "injected"}
    response = client.post(path, json=body)
    assert response.status_code == 422, (field, response.text)
    assert fetcher.calls == [], f"{field} reached execution"


@pytest.mark.parametrize(
    "field",
    ["value", "data_as_of", "source_id", "receipt_step_id", "fact_id", "unit"],
)
async def test_nested_binding_cannot_smuggle_input_evidence(field: str) -> None:
    fetcher = _GovernedFetcher(Decimal("45"))
    client, definition_id, spec = await _http_client(fetcher)
    path = f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute"
    binding = _binding_body(spec, 90)
    binding[field] = "injected"
    response = client.post(path, json={"binding": binding})
    assert response.status_code == 422, (field, response.text)
    assert fetcher.calls == []


async def test_http_execute_zero_target_is_undefined_not_no_data() -> None:
    fetcher = _GovernedFetcher(Decimal("45"))
    client, definition_id, spec = await _http_client(fetcher)
    response = client.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute",
        json={"binding": _binding_body(spec, 0)},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "calculation_undefined_division_by_zero"
    assert response.json()["detail"] != "NO_DATA"


# ==========================================================================
# 8. Mode3 execution service dependency direction.
# ==========================================================================


def test_execution_service_delegates_arithmetic_to_the_one_runtime() -> None:
    from src.nl2sql.orchestration import custom_calculation_execution as module

    source = inspect.getsource(module)
    assert "evaluate_calculation" in source
    # No second evaluator: it must not re-implement arithmetic or touch SQL.
    for forbidden in ("Decimal(", "select ", "QueryGateway", "asyncpg"):
        assert forbidden not in source, forbidden


def test_shared_runtime_is_one_module_with_one_evaluator() -> None:
    from src.nl2sql.semantic import calculation_runtime as runtime

    source = inspect.getsource(runtime)
    assert source.count("def evaluate_calculation(") == 1
    assert "QueryGateway" not in source
    assert "sqlalchemy" not in source


# ==========================================================================
# 9. Personal conflict: no winner semantics in the SERIALIZED body.
# ==========================================================================

_FORBIDDEN_WINNER_FIELDS = (
    "winner",
    "best",
    "recommended",
    "recommendation",
    "rank",
    "ranking",
    "score",
    "preferred",
    "selected",
    "default_choice",
    "top",
)


def _walk_keys(payload: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(str(key).lower())
            keys |= _walk_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            keys |= _walk_keys(item)
    return keys


def test_conflict_response_models_expose_no_winner_semantics() -> None:
    from src.nl2sql.artifacts.api_conflicts import (
        PersonalConflictResponse,
        PersonalSelectionResponse,
    )
    from src.nl2sql.semantic.personal_conflict_contract import SemanticConflict
    from src.nl2sql.semantic.personal_conflict_service import (
        ConflictComparison,
        ConflictComparisonCandidate,
    )
    from src.nl2sql.supervisor.schemas import ConflictCandidateBlock

    for model in (
        PersonalConflictResponse,
        PersonalSelectionResponse,
        SemanticConflict,
        ConflictComparison,
        ConflictComparisonCandidate,
        ConflictCandidateBlock,
    ):
        fields = {name.lower() for name in model.model_fields}
        overlap = fields & set(_FORBIDDEN_WINNER_FIELDS)
        assert not overlap, (model.__name__, sorted(overlap))


async def _conflict_fixture() -> tuple[Any, str, str]:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.personal_conflict_product_service import (
        PersonalConflictProductService,
    )
    from src.nl2sql.artifacts.publication import PublicationCatalogue
    from src.nl2sql.artifacts.publication_service import PublicationService

    definitions = CustomDefinitionService(governed_metric_keys={_METRIC})
    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    service = PersonalConflictProductService(
        definitions=definitions, catalogue=catalogue, library=library
    )

    async def _saved(multiplier: str, title: str) -> str:
        spec = CalculationSpec(
            calculation_id="custom.acceptance_conflict",
            expression=BinaryOperand(
                op="multiply",
                left=InputRefOperand(role="actual"),
                right=LiteralOperand(value=Decimal(multiplier)),
            ),
            inputs=(
                CalculationInputSpec(
                    role="actual", provenance="published_gold", metric_key=_METRIC
                ),
            ),
            unit="percent",
            precision=2,
        )
        draft = await definitions.create_draft(
            owner_user_id="alice", title=title, calculation=spec
        )
        await definitions.mark_semantic_closed(
            owner_user_id="alice", definition_id=draft.definition_id
        )
        await definitions.confirm(owner_user_id="alice", definition_id=draft.definition_id)
        await definitions.save(owner_user_id="alice", definition_id=draft.definition_id)
        return draft.definition_id

    own = await _saved("1", "Archive performance")
    installed = await _saved("2", "Archive performance")
    await publications.publish(owner_user_id="alice", definition_id=installed, version=1)
    await library.install(user_id="alice", identity_id=installed, version=1)
    await library.star(user_id="alice", identity_id=installed)
    await catalogue.certify_local_demo(installed, 1, certified_by="c")
    return service, own, installed


async def test_serialized_conflict_body_carries_no_winner_semantics() -> None:
    service, own, installed = await _conflict_fixture()
    projection = await service.resolve(
        user_id="alice", own_definition_id=own, installed_identity_id=installed
    )
    assert projection.resolution.outcome == "clarification_required"
    serialized = json.loads(projection.model_dump_json())
    keys = _walk_keys(serialized)
    overlap = keys & set(_FORBIDDEN_WINNER_FIELDS)
    assert not overlap, sorted(overlap)
    # Star and certification are present as DESCRIPTIVE facts only.
    blob = json.dumps(serialized).lower()
    assert "star_count" in blob
    assert "certification_state" in blob


async def test_equivalent_exact_candidate_deduplicates_instead_of_conflicting() -> None:
    service, _, installed = await _conflict_fixture()
    same = await service.resolve(
        user_id="alice", own_definition_id=installed, installed_identity_id=installed
    )
    assert same.resolution.outcome == "resolved"
    assert same.semantic_conflict is None
    assert same.conflict_comparison is None


def test_persisted_selection_requires_an_explicit_user_request() -> None:
    from src.nl2sql.semantic.personal_conflict_contract import PersonalSelection

    with pytest.raises(Exception):
        PersonalSelection(
            conflict_id="c",
            required_slot="metric",
            selected_candidate_id="x",
            selection_scope="persisted",
        )


async def test_run_scoped_selection_is_validated_against_the_resolved_conflict() -> None:
    from src.nl2sql.semantic.personal_conflict_contract import PersonalSelection
    from src.nl2sql.semantic.personal_conflict_service import (
        PersonalConflictServiceError,
        validate_personal_selection,
    )

    service, own, installed = await _conflict_fixture()
    projection = await service.resolve(
        user_id="alice", own_definition_id=own, installed_identity_id=installed
    )
    assert projection.semantic_conflict is not None
    conflict = projection.semantic_conflict
    chosen = conflict.candidates[0].candidate_id
    selection = PersonalSelection(
        conflict_id=conflict.conflict_id,
        required_slot=conflict.required_slot,
        selected_candidate_id=chosen,
    )
    assert validate_personal_selection(selection, projection=projection) is selection
    assert selection.selection_scope == "run_scoped"
    with pytest.raises(PersonalConflictServiceError):
        validate_personal_selection(
            PersonalSelection(
                conflict_id=conflict.conflict_id,
                required_slot=conflict.required_slot,
                selected_candidate_id="not-a-candidate",
            ),
            projection=projection,
        )


# ==========================================================================
# 10. Block manifest parity + unknown-block fallback.
# ==========================================================================


def test_block_manifest_matches_the_public_server_contracts() -> None:
    from src.nl2sql.supervisor.schemas import PUBLIC_BLOCK_MODELS

    assert set(BLOCK_MANIFEST) == set(PUBLIC_BLOCK_MODELS)
    assert set(BLOCK_MANIFEST) == {
        "text",
        "chart",
        "metric_card",
        "table",
        "image",
        "clarification",
        "plan_card",
        "mode_suggestion",
        "conflict_comparison",
        "provenance",
        "definition",
    }


def test_unknown_block_is_preserved_visibly_without_leaking_payload() -> None:
    from src.nl2sql.supervisor.schemas import serialize_response_blocks

    serialized = serialize_response_blocks(
        [{"type": "future_quantum_block", "secret_payload": {"k": "v"}}]
    )
    assert serialized == [
        {
            "type": "future_quantum_block",
            "fallback": {
                "kind": "unsupported_block",
                "message": (
                    "This result block is not supported by this client version."
                ),
            },
        }
    ]
    assert "secret_payload" not in json.dumps(serialized)


# ==========================================================================
# 11. Profile isolation.
# ==========================================================================


@pytest.mark.parametrize(
    ("env", "expected_error"),
    [
        (
            {
                "SERVICE_MODE": "product",
                "TYPED_RUNTIME_ACTIVATION": "local_real_data_demo",
            },
            ValueError,
        ),
        (
            {
                "SERVICE_MODE": "product",
                "TYPED_RUNTIME_ACTIVATION": "demo_synthetic_authorization",
            },
            ValueError,
        ),
    ],
)
def test_product_mode_can_never_activate_a_demo_profile(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected_error: type[Exception]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(expected_error):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_local_real_profile_never_imports_the_synthetic_demo_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("semantics", "deployment", "live_probe"):
        text = (root / "src" / "nl2sql" / "local_real" / f"{name}.py").read_text(
            encoding="utf-8"
        )
        assert "demo.runtime" not in text
        assert "build_demo_runtime" not in text
        assert "DemoQueryPlanProvider" not in text


def test_trusted_backend_profile_has_no_fallback_to_demo_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "SERVICE_MODE": "infra-dev",
        "TYPED_RUNTIME_ACTIVATION": "trusted_backend_authorization",
        "AUTH_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    from src.core.settings import get_settings

    get_settings.cache_clear()
    container = AppContainer()
    # The trusted profile returns None: it must NEVER silently degrade to demo.
    assert container.get_backend_authorization_provider() is None
    assert container.authority_provenance() == "unavailable"
    get_settings.cache_clear()


def test_demo_profiles_are_distinct_and_never_cross_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.settings import get_settings

    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("TYPED_RUNTIME_ACTIVATION", "local_real_data_demo")
    monkeypatch.setenv("LOCAL_REAL_DEMO_USER_ID", "local-real-demo")
    get_settings.cache_clear()
    local = AppContainer()
    provider = local.get_backend_authorization_provider()
    assert provider is not None
    assert type(provider).__name__ == "LocalRealAuthorizationProvider"
    assert local.authority_provenance() == "local_real_demo"

    monkeypatch.setenv("TYPED_RUNTIME_ACTIVATION", "demo_synthetic_authorization")
    monkeypatch.setenv("DEMO_SYNTHETIC_USER_ID", "demo-analyst")
    get_settings.cache_clear()
    demo = AppContainer()
    demo_provider = demo.get_backend_authorization_provider()
    assert demo_provider is not None
    assert type(demo_provider).__name__ == "DemoBackendAuthorizationProvider"
    assert demo.authority_provenance() == "demo"
    # The two demo providers are DIFFERENT types: no cross-profile reuse.
    assert type(provider) is not type(demo_provider)
    get_settings.cache_clear()
