"""Typed clarification suspension -> decision -> cumulative slot-bound replan.

Pins the multi-round behavior: each round refines the CURRENT ACTIVE plan (not
the original question), resolved semantics survive later rounds, versions are
monotonic, lineage chains P1->P2->P3, and a revalidated ALLOW stops at
resolved_pending_current_authorization without compiling or executing.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.nl2sql.contracts import (
    ContextBundle,
    ExecutionReceipt,
    FetchMetricStep,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    RouteName,
    TimeRange,
)
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.decision_contract import (
    HITLDecision,
    HITLRequest,
    SlotBinding,
    revalidate_request,
)
from src.nl2sql.orchestration.decision_contract import (
    resume_token as build_resume_token,
)
from src.nl2sql.orchestration.engine import (
    V2EngineState,
    _active_query_plan,
    _typed_decision_from_payload,
    _typed_decision_replays,
    create_v2_engine,
)
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
)
from src.nl2sql.ownership import runtime_config

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")
SQL_FINGERPRINT = "a" * 64
ROUND_TIME = TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))


def _identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        permissions=frozenset({"metrics:read"}),
    )


def _context(
    *,
    resolution_status: Literal["resolved", "ambiguous", "incomplete", "conflict"] = (
        "resolved"
    ),
    unresolved_slots: tuple[str, ...] = (),
    conflict_ids: tuple[str, ...] = (),
) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=("metric.revenue",),
        resolution_status=resolution_status,
        unresolved_slots=unresolved_slots,
        conflict_ids=conflict_ids,
    )


def _query_plan(
    *,
    unresolved_slots: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = ("metrics:read",),
) -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.revenue",),
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=required_permissions,
        unresolved_slots=unresolved_slots,
    )


def _time_plan() -> QueryPlan:
    return _query_plan(unresolved_slots=("time",))


def _time_and_grain_plan() -> QueryPlan:
    return _query_plan(unresolved_slots=("time", "grain"))


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
            content="model path", model="small", usage={}, finish_reason="stop"
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
        },
    )


class _MetricRunner:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.execute_calls = 0

    async def prepare(
        self,
        *,
        step: FetchMetricStep,
        query_plan: QueryPlan,
        context: ContextBundle,
    ) -> PreparedMetricStep:
        del step, query_plan, context
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
        del prepared, timeout_ms
        self.execute_calls += 1
        return MetricStepResult(
            value={"revenue": 4242},
            receipt=ExecutionReceipt(
                datasource="synthetic",
                readonly_role="fixture_reader",
                elapsed_ms=1,
                row_count=1,
                sql_fingerprint=SQL_FINGERPRINT,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


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
        del question, identity, route_hint
        context = self.contexts[min(self.calls, len(self.contexts) - 1)]
        self.calls += 1
        return context


class _CumulativeQueryPlanProvider:
    """Fake deterministic provider with cumulative time/grain replan semantics."""

    is_deterministic = True

    def __init__(
        self, plan: QueryPlan, replan_extra: dict[str, object] | None = None
    ) -> None:
        self.plan = plan
        self.replan_extra = replan_extra or {}
        self.calls = 0
        self.slot_calls = 0
        self.last_bindings: tuple[SlotBinding, ...] = ()
        self.base_checksums: list[str] = []

    async def propose(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> QueryPlan:
        del question, context, identity
        self.calls += 1
        return self.plan

    async def replan_with_slot_bindings(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
        base_plan: QueryPlan,
        slot_bindings: tuple[SlotBinding, ...],
    ) -> QueryPlan:
        del question, context, identity
        self.slot_calls += 1
        self.last_bindings = slot_bindings
        self.base_checksums.append(base_plan.checksum)
        unresolved = list(base_plan.unresolved_slots)
        update: dict[str, object] = {}
        for binding in slot_bindings:
            if binding.slot == "time":
                update["time_range"] = ROUND_TIME
                unresolved = [slot for slot in unresolved if slot != "time"]
            elif binding.slot == "grain":
                update["grain"] = binding.value
                unresolved = [slot for slot in unresolved if slot != "grain"]
        update["unresolved_slots"] = tuple(unresolved)
        update.update(self.replan_extra)
        return base_plan.model_copy(update=update)


def _config(trace_id: str = "trace-typed-decision") -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id=trace_id,
            deadline_ms=4_000,
        )
    )


async def _engine(
    contexts: tuple[ContextBundle, ...],
    *,
    plan: QueryPlan | None = None,
    replan_extra: dict[str, object] | None = None,
) -> tuple[
    Any,
    _SequenceContextResolver,
    _CountingProvider,
    _MetricRunner,
    _CumulativeQueryPlanProvider,
]:
    resolver = _SequenceContextResolver(contexts)
    provider = _CountingProvider()
    runner = _MetricRunner()
    plan_provider = _CumulativeQueryPlanProvider(
        plan if plan is not None else _query_plan(), replan_extra
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(provider),
        context_resolver=resolver,
        query_plan_provider=plan_provider,
        plan_executor=PlanExecutor(metric_runner=runner),
    )
    return engine, resolver, provider, runner, plan_provider


def _resolve_payload(
    slot: str = "time", value: str = "today", key: str = "k-resolve"
) -> dict[str, object]:
    return {
        "action": "resolve",
        "slot_bindings": [{"slot": slot, "value": value}],
        "idempotency_key": key,
    }


def _stored_request(*, unresolved_slots: tuple[str, ...] = ("time",)) -> HITLRequest:
    return HITLRequest(
        request_id="clarify-" + "a" * 32,
        decision_kind="clarification",
        version=1,
        allowed_actions=("resolve", "choose", "reject", "cancel"),
        plan_sha256="b" * 64,
        context_checksum="c" * 64,
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="d" * 64,
        issue_codes=("query_plan_unresolved_slots",),
        unresolved_slots=unresolved_slots,
    )


# --- routing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_validation_still_routes_and_executes() -> None:
    engine, _, provider, runner, _ = await _engine((_context(),))
    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, _config()
    )
    assert result["stop_reason"] is None
    assert result["decision_status"] is None
    assert result["execution_record"]["status"] == "succeeded"
    assert runner.execute_calls == 1
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_deny_validation_still_ends() -> None:
    engine, _, _, runner, _ = await _engine(
        (_context(resolution_status="conflict", conflict_ids=("c1",)),)
    )
    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, _config()
    )
    assert result["stop_reason"] == "query_plan_validation_denied"
    assert result["pending_decision"] is None
    assert runner.execute_calls == 0


# --- typed suspension ---------------------------------------------------------


@pytest.mark.asyncio
async def test_clarify_creates_typed_pending_request_and_suspends() -> None:
    engine, _, _, runner, _ = await _engine((_context(),), plan=_time_plan())
    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, _config()
    )
    assert result["stop_reason"] is None
    assert result["decision_status"] == "awaiting_decision"
    assert result["decision_version"] == 1
    assert "__interrupt__" in result
    request = revalidate_request(result["pending_decision"])
    assert request.decision_kind == "clarification"
    assert request.unresolved_slots == ("time",)
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_stored_request_binds_plan_context_and_policy() -> None:
    engine, _, _, _, _ = await _engine((_context(),), plan=_time_plan())
    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, _config()
    )
    validation = result["query_plan_validation"]
    request = revalidate_request(result["pending_decision"])
    assert request.plan_sha256 == validation["query_plan_sha256"]
    assert request.context_checksum == validation["context_checksum"]
    assert request.policy_version == validation["policy_version"]
    assert request.policy_checksum == validation["policy_checksum"]


# --- cumulative slot-bound replan ---------------------------------------------


@pytest.mark.asyncio
async def test_resolve_replans_and_stops_before_current_authorization() -> None:
    engine, _, _, runner, plan_provider = await _engine((_context(),), plan=_time_plan())
    config = _config()
    first = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(Command(resume=_resolve_payload()), config)

    assert resumed["stop_reason"] is None
    assert resumed["decision_status"] == "resolved_pending_current_authorization"
    assert resumed["pending_decision"] is None
    assert resumed["resolved_plan_validation"]["outcome"] == "allow"
    lineage = resumed["plan_lineage"]
    assert len(lineage) == 1
    assert lineage[0]["old_plan_sha256"] == first["pending_decision"]["plan_sha256"]
    assert (
        lineage[0]["new_plan_sha256"]
        == resumed["resolved_plan_validation"]["query_plan_sha256"]
    )
    assert lineage[0]["new_plan_sha256"] != lineage[0]["old_plan_sha256"]
    assert plan_provider.slot_calls == 1
    assert plan_provider.base_checksums[0] == first["pending_decision"]["plan_sha256"]
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_two_round_clarification_replans_cumulatively() -> None:
    engine, _, _, runner, plan_provider = await _engine(
        (_context(),), plan=_time_and_grain_plan()
    )
    config = _config()
    r1 = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    assert r1["decision_version"] == 1
    assert r1["pending_decision"]["unresolved_slots"] == ["time", "grain"]
    p1_checksum = r1["pending_decision"]["plan_sha256"]

    r2 = await engine.ainvoke(
        Command(resume=_resolve_payload("time", "today", "k1")), config
    )
    assert r2["decision_status"] == "awaiting_decision"
    assert r2["decision_version"] == 2
    assert r2["pending_decision"]["version"] == 2
    assert r2["pending_decision"]["unresolved_slots"] == ["grain"]
    assert "__interrupt__" in r2
    p2_checksum = r2["resolved_plan_validation"]["query_plan_sha256"]
    assert p2_checksum == r2["pending_decision"]["plan_sha256"]
    assert "k1" in r2["decision_ledger"]

    r3 = await engine.ainvoke(
        Command(resume=_resolve_payload("grain", "month", "k2")), config
    )
    assert r3["stop_reason"] is None
    assert r3["decision_status"] == "resolved_pending_current_authorization"
    assert r3["pending_decision"] is None
    assert r3["resolved_plan_validation"]["outcome"] == "allow"
    p3_checksum = r3["resolved_plan_validation"]["query_plan_sha256"]

    lineage = r3["plan_lineage"]
    assert len(lineage) == 2
    assert lineage[0]["old_plan_sha256"] == p1_checksum
    assert lineage[0]["new_plan_sha256"] == p2_checksum
    assert lineage[1]["old_plan_sha256"] == p2_checksum
    assert lineage[1]["new_plan_sha256"] == p3_checksum
    assert len({p1_checksum, p2_checksum, p3_checksum}) == 3

    assert set(r3["decision_ledger"]) == {"k1", "k2"}
    assert runner.execute_calls == 0
    assert r3["resolved_plan"]["time_range"] == r2["resolved_plan"]["time_range"]
    assert plan_provider.base_checksums == [p1_checksum, p2_checksum]


@pytest.mark.asyncio
async def test_replanned_deny_terminates() -> None:
    engine, _, _, runner, _ = await _engine(
        (_context(),),
        plan=_time_plan(),
        replan_extra={"required_permissions": ("metrics:write",)},
    )
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(Command(resume=_resolve_payload()), config)
    assert resumed["stop_reason"] == "query_plan_validation_denied"
    assert resumed["decision_status"] == "revalidation_denied"
    assert resumed["pending_decision"] is None
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_context_unresolved_slots_block_plan_slot_continuation() -> None:
    context = _context(resolution_status="incomplete", unresolved_slots=("metric",))
    engine, _, _, runner, plan_provider = await _engine((context,), plan=_time_plan())
    config = _config()
    r1 = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    assert set(r1["pending_decision"]["unresolved_slots"]) == {"metric", "time"}
    r2 = await engine.ainvoke(
        Command(
            resume={
                "action": "resolve",
                "slot_bindings": [
                    {"slot": "metric", "value": "metric.revenue"},
                    {"slot": "time", "value": "today"},
                ],
                "idempotency_key": "k-context",
            }
        ),
        config,
    )
    assert r2["decision_status"] == "continuation_stopped"
    assert r2["stop_reason"] == "continuation_context_slot_unsupported"
    assert plan_provider.slot_calls == 0
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_unsupported_slot_is_not_eligible_for_continuation() -> None:
    engine, _, _, runner, plan_provider = await _engine(
        (_context(),), plan=_query_plan(unresolved_slots=("metric",))
    )
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(resume=_resolve_payload("metric", "metric.revenue", "k-metric")), config
    )
    assert resumed["decision_status"] == "continuation_stopped"
    assert resumed["stop_reason"] == "typed_continuation_not_eligible"
    assert plan_provider.slot_calls == 0
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_non_user_slot_source_is_not_eligible_for_continuation() -> None:
    engine, _, _, runner, plan_provider = await _engine((_context(),), plan=_time_plan())
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": "resolve",
                "slot_bindings": [
                    {"slot": "time", "value": "today", "source": "semantic_default"}
                ],
                "idempotency_key": "k-default",
            }
        ),
        config,
    )
    assert resumed["decision_status"] == "continuation_stopped"
    assert plan_provider.slot_calls == 0
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_choose_is_recorded_without_continuation() -> None:
    engine, _, _, runner, plan_provider = await _engine((_context(),), plan=_time_plan())
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": "choose",
                "slot_bindings": [{"slot": "time", "value": "today"}],
                "idempotency_key": "k-choose",
            }
        ),
        config,
    )
    assert resumed["decision_status"] == "choose_recorded_no_continuation"
    assert plan_provider.slot_calls == 0
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_reject_is_terminal_and_clears_active_pending() -> None:
    engine, _, _, runner, plan_provider = await _engine((_context(),), plan=_time_plan())
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(resume={"action": "reject", "idempotency_key": "k-reject"}), config
    )
    assert resumed["decision_status"] == "rejected"
    assert resumed["pending_decision"] is None
    assert resumed["resolved_decision"]["slot_bindings"] == []
    assert plan_provider.slot_calls == 0
    assert runner.execute_calls == 0


# --- invalid decisions preserve the suspension --------------------------------


@pytest.mark.asyncio
async def test_forged_authority_field_is_rejected_and_pending_preserved() -> None:
    engine, _, _, runner, _ = await _engine((_context(),), plan=_time_plan())
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(
            resume={
                **_resolve_payload(),
                "authorization_context": {"authorization_revision": "r"},
            }
        ),
        config,
    )
    assert resumed["decision_status"] == "awaiting_decision"
    assert "__interrupt__" in resumed
    assert resumed["resolved_decision"] is None
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_forged_resume_token_cannot_trigger_replan() -> None:
    engine, _, _, runner, plan_provider = await _engine((_context(),), plan=_time_plan())
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(
            resume={**_resolve_payload(), "resume_token": {"request_id": "forged"}}
        ),
        config,
    )
    assert resumed["decision_status"] == "awaiting_decision"
    assert plan_provider.slot_calls == 0
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_stale_request_version_preserves_pending_state() -> None:
    engine, _, _, runner, _ = await _engine((_context(),), plan=_time_plan())
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    resumed = await engine.ainvoke(
        Command(resume={**_resolve_payload(), "request_version": 99}), config
    )
    assert resumed["decision_status"] == "awaiting_decision"
    assert "__interrupt__" in resumed
    assert runner.execute_calls == 0
    retried = await engine.ainvoke(Command(resume=_resolve_payload()), config)
    assert retried["decision_status"] == "resolved_pending_current_authorization"


# --- active-plan restore ------------------------------------------------------


def test_active_query_plan_restore_is_fail_closed() -> None:
    original = _query_plan()
    request = _stored_request().model_copy(update={"plan_sha256": original.checksum})
    original_state: V2EngineState = {
        "messages": [],
        "query_plan": original.model_dump(mode="json"),
    }
    active = _active_query_plan(original_state, request)
    assert active is not None
    assert active.checksum == original.checksum

    replanned = original.model_copy(update={"unresolved_slots": ()})
    state: V2EngineState = {
        "messages": [],
        "query_plan": original.model_dump(mode="json"),
        "resolved_plan": replanned.model_dump(mode="json"),
    }
    req2 = request.model_copy(update={"plan_sha256": replanned.checksum})
    active_replanned = _active_query_plan(state, req2)
    assert active_replanned is not None
    assert active_replanned.checksum == replanned.checksum

    invalid_state: V2EngineState = {
        "messages": [],
        "query_plan": original.model_dump(mode="json"),
        "resolved_plan": {"x": 1},
    }
    assert (
        _active_query_plan(invalid_state, request)
        is None
    )
    assert _active_query_plan(original_state, _stored_request()) is None


# --- payload / ledger helpers -------------------------------------------------


def test_typed_decision_payload_validation() -> None:
    request = _stored_request()
    decision, failure = _typed_decision_from_payload(request, _resolve_payload())
    assert failure is None
    assert decision is not None
    assert decision.action == "resolve"

    _, failure = _typed_decision_from_payload(
        request, {**_resolve_payload(), "authorization": "allow"}
    )
    assert failure == "typed_decision_authority_field_rejected"

    _, failure = _typed_decision_from_payload(
        request, {**_resolve_payload(), "resume_token": {}}
    )
    assert failure == "typed_decision_unknown_field"

    _, failure = _typed_decision_from_payload(
        request, {**_resolve_payload(), "request_version": 99}
    )
    assert failure == "typed_decision_request_version_mismatch"

    _, failure = _typed_decision_from_payload(request, "not-a-mapping")
    assert failure == "typed_decision_payload_invalid"


def test_typed_decision_replay_and_conflict_semantics() -> None:
    request = _stored_request()
    decision = HITLDecision(
        request_id=request.request_id,
        request_version=request.version,
        action="resolve",
        slot_bindings=(SlotBinding(slot="time", value="today"),),
        idempotency_key="k1",
    )
    token = build_resume_token(request=request, decision=decision)
    entry: dict[str, object] = {
        "request_id": request.request_id,
        "request_version": request.version,
        "request_checksum": request.checksum,
        "decision_checksum": decision.checksum,
        "resume_token_checksum": token.checksum,
        "status": "resolved_pending_revalidation",
    }
    assert _typed_decision_replays(entry, request, decision) is True

    different = HITLDecision(
        request_id=request.request_id,
        request_version=request.version,
        action="reject",
        idempotency_key="k1",
    )
    assert _typed_decision_replays(entry, request, different) is False

    stale = {**entry, "request_version": 99}
    assert _typed_decision_replays(stale, request, decision) is False

    tampered = {**entry, "request_checksum": "0" * 64}
    assert _typed_decision_replays(tampered, request, decision) is False
