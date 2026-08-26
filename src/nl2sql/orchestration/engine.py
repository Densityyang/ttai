"""The explicit LangGraph v2 request graph.

This graph deliberately has no tool-calling loop. SQL execution is introduced
only through a validated typed plan after routing and budget selection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired, Protocol, TypedDict, cast

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.contracts import (
    ContextBundle,
    ExecutionPlan,
    ModelRequest,
    PlanValidationRecord,
    QueryPlan,
    RequestIdentity,
    RouteName,
    RoutePolicy,
    RoutingBudgetPolicy,
)
from src.nl2sql.infra.llm.gateway import ModelGateway, ModelGatewayError, ModelPolicyDenied
from src.nl2sql.observability.trace import TraceEnvelope, TraceEvent, fingerprint
from src.nl2sql.orchestration.budget import (
    BudgetExceeded,
    CallBudget,
    RouteBudgetLedger,
    bootstrap_routing_budget_policy,
    should_stop,
)
from src.nl2sql.orchestration.execution import PlanExecutor
from src.nl2sql.orchestration.planning import (
    ContextResolver,
    PlanCompiler,
    PlanValidator,
    QueryPlanProvider,
)
from src.nl2sql.orchestration.routing import RiskSignals, bootstrap_route_policy, choose_route

_TRACE_SINK_TIMEOUT_SECONDS = 0.25
_INVALID_REQUEST_ELAPSED_MS = 120_000


class V2EngineState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    context_bundle: NotRequired[dict[str, object] | None]
    query_plan: NotRequired[dict[str, object] | None]
    query_plan_validation: NotRequired[dict[str, object] | None]
    route_record: NotRequired[dict[str, object] | None]
    budget_record: NotRequired[dict[str, object] | None]
    execution_plan: NotRequired[dict[str, object] | None]
    execution_plan_validation: NotRequired[dict[str, object] | None]
    execution_record: NotRequired[dict[str, object] | None]
    model_receipt: NotRequired[dict[str, object] | None]
    degradation_flags: NotRequired[list[str]]
    stop_reason: NotRequired[str | None]
    pending_answer: NotRequired[str | None]
    needs_hitl: NotRequired[bool]
    hitl_version: NotRequired[int]
    hitl_status: NotRequired[str | None]
    applied_actions: NotRequired[dict[str, dict[str, object]]]
    trace_events: NotRequired[list[dict[str, object]]]
    request_started_at: NotRequired[str]


class TraceSink(Protocol):
    async def append(self, event: TraceEvent) -> None: ...


def create_v2_engine(
    *,
    checkpointer: BaseCheckpointSaver,
    model_gateway: ModelGateway,
    trace_sink: TraceSink | None = None,
    route_policy: RoutePolicy | None = None,
    budget_policy: RoutingBudgetPolicy | None = None,
    context_resolver: ContextResolver | None = None,
    query_plan_provider: QueryPlanProvider | None = None,
    plan_validator: PlanValidator | None = None,
    plan_compiler: PlanCompiler | None = None,
    plan_executor: PlanExecutor | None = None,
) -> Any:
    resolved_route_policy = route_policy or bootstrap_route_policy()
    resolved_budget_policy = budget_policy or bootstrap_routing_budget_policy()
    typed_pipeline_requested = any(
        component is not None
        for component in (
            context_resolver,
            query_plan_provider,
            plan_validator,
            plan_compiler,
            plan_executor,
        )
    )
    typed_pipeline_enabled = all(
        component is not None
        for component in (context_resolver, query_plan_provider, plan_executor)
    )
    if typed_pipeline_requested and not typed_pipeline_enabled:
        raise ValueError(
            "typed plan pipeline requires context resolver, query plan provider, and executor"
        )
    if typed_pipeline_enabled and not bool(
        getattr(query_plan_provider, "is_deterministic", False)
    ):
        raise ValueError(
            "pre-route query plan provider must be deterministic and zero-model"
        )
    resolved_plan_validator = plan_validator or PlanValidator()
    resolved_plan_compiler = plan_compiler or PlanCompiler()
    graph = StateGraph(V2EngineState)

    async def receive_node(state: V2EngineState) -> dict[str, object]:
        question = _question(state)
        request_started_at = datetime.now(UTC).isoformat()
        trace = TraceEnvelope(trace_id=_trace_id(state))
        trace.record(
            "query",
            "received",
            question_fingerprint=fingerprint(question),
            question_length=len(question),
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        return {
            "context_bundle": None,
            "query_plan": None,
            "query_plan_validation": None,
            "route_record": None,
            "budget_record": None,
            "execution_plan": None,
            "execution_plan_validation": None,
            "execution_record": None,
            "model_receipt": None,
            "degradation_flags": [],
            "stop_reason": None,
            "pending_answer": None,
            "needs_hitl": False,
            "hitl_status": None,
            "trace_events": _events(trace),
            "request_started_at": request_started_at,
        }

    async def context_node(state: V2EngineState) -> dict[str, object]:
        if not typed_pipeline_enabled:
            return {}
        assert context_resolver is not None
        trace = _trace(state)
        try:
            timeout_ms = _pre_route_timeout_ms(state, resolved_budget_policy)
            if timeout_ms < 1:
                raise TimeoutError
            async with asyncio.timeout(timeout_ms / 1000):
                context = await context_resolver.resolve(
                    question=_question(state),
                    identity=_request_identity(),
                    route_hint="standard",
                )
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError)
            trace.record(
                "retrieval",
                "context_deadline_exceeded"
                if timed_out
                else "context_compilation_failed",
                error_type=type(exc).__name__,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [
                    AIMessage(
                        content="Semantic context could not be resolved for this request."
                    )
                ],
                "stop_reason": (
                    "context_deadline_exceeded"
                    if timed_out
                    else "context_compilation_failed"
                ),
                "degradation_flags": _degradation_flags(
                    state,
                    "ContextDeadlineExceeded"
                    if timed_out
                    else "ContextCompilationFailed",
                ),
                "trace_events": _events(trace),
            }
        trace.record(
            "retrieval",
            "context_compiled",
            semantic_release_id=str(context.semantic_release_id),
            schema_snapshot_id=str(context.schema_snapshot_id),
            context_checksum=context.checksum,
            resolution_status=context.resolution_status,
            token_cost=context.token_cost,
            evidence_count=context.evidence_count,
            approved_relations=len(context.approved_relation_ids),
            approved_edges=len(context.approved_edge_ids),
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        return {
            "context_bundle": context.model_dump(mode="json"),
            "degradation_flags": _degradation_flags(
                state,
                *context.degradation_flags,
            ),
            "trace_events": _events(trace),
        }

    async def plan_node(state: V2EngineState) -> dict[str, object]:
        if not typed_pipeline_enabled:
            return {}
        assert query_plan_provider is not None
        trace = _trace(state)
        try:
            context = _context_bundle(state)
            timeout_ms = _pre_route_timeout_ms(state, resolved_budget_policy)
            if timeout_ms < 1:
                raise TimeoutError
            async with asyncio.timeout(timeout_ms / 1000):
                plan = await query_plan_provider.propose(
                    question=_question(state),
                    context=context,
                    identity=_request_identity(),
                )
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError)
            trace.record(
                "candidate",
                "query_plan_deadline_exceeded"
                if timed_out
                else "query_plan_proposal_failed",
                error_type=type(exc).__name__,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="A typed query plan could not be produced.")],
                "stop_reason": (
                    "query_plan_deadline_exceeded"
                    if timed_out
                    else "query_plan_proposal_failed"
                ),
                "degradation_flags": _degradation_flags(
                    state,
                    "QueryPlanDeadlineExceeded"
                    if timed_out
                    else "QueryPlanProposalFailed",
                ),
                "trace_events": _events(trace),
            }
        trace.record(
            "candidate",
            "query_plan_proposed",
            query_plan_sha256=plan.checksum,
            schema_version=plan.schema_version,
            intent=plan.intent,
            domain=plan.domain,
            metric_count=len(plan.metric_keys),
            filter_count=len(plan.filters),
            source_strategy=plan.source_strategy,
            deterministic_provider=True,
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        return {
            "query_plan": plan.model_dump(mode="json"),
            "trace_events": _events(trace),
        }

    async def validate_node(state: V2EngineState) -> dict[str, object]:
        if not typed_pipeline_enabled:
            return {}
        trace = _trace(state)
        try:
            validation = resolved_plan_validator.validate_query_plan(
                plan=_query_plan(state),
                context=_context_bundle(state),
                identity=_request_identity(),
            )
        except Exception as exc:
            trace.record(
                "policy",
                "query_plan_validation_failed",
                error_type=type(exc).__name__,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="The typed query plan could not be validated.")],
                "stop_reason": "query_plan_validation_failed",
                "degradation_flags": _degradation_flags(
                    state,
                    "QueryPlanValidationFailed",
                ),
                "trace_events": _events(trace),
            }
        trace.record(
            "policy",
            "query_plan_validated",
            outcome=validation.outcome,
            policy_version=validation.policy_version,
            policy_checksum=validation.policy_checksum,
            query_plan_sha256=validation.query_plan_sha256,
            context_checksum=validation.context_checksum,
            issue_codes=[issue.code for issue in validation.issues],
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        result: dict[str, object] = {
            "query_plan_validation": validation.model_dump(mode="json"),
            "trace_events": _events(trace),
        }
        if validation.outcome != "allow":
            stop_reason = (
                "query_plan_clarification_required"
                if validation.outcome == "clarify"
                else "query_plan_validation_denied"
            )
            result.update(
                {
                    "messages": [
                        AIMessage(
                            content=(
                                "The request needs clarification before execution."
                                if validation.outcome == "clarify"
                                else "The typed query plan was not permitted."
                            )
                        )
                    ],
                    "stop_reason": stop_reason,
                    "degradation_flags": _degradation_flags(
                        state,
                        "PlanClarificationRequired"
                        if validation.outcome == "clarify"
                        else "PlanValidationDenied",
                    ),
                }
            )
        return result

    async def route_node(state: V2EngineState) -> dict[str, object]:
        question = _question(state)
        context = _optional_context_bundle(state)
        plan = _optional_query_plan(state)
        validation = _optional_plan_validation(state, "query_plan_validation")
        signals = _signals(
            question,
            context=context,
            plan=plan,
            validation=validation,
            fast_budget_available=(
                _remaining_route_deadline_ms(
                    state,
                    route="fast",
                    policy=resolved_budget_policy,
                )
                > resolved_budget_policy.reserve_ms
            ),
        )
        confidence = (
            0.9
            if context is not None and context.resolution_status == "resolved"
            else 0.9
            if signals.score <= 20
            else 0.7
            if signals.score <= 60
            else 0.45
        )
        decision = choose_route(
            signals=signals,
            confidence=confidence,
            policy=resolved_route_policy,
        )
        budget = RouteBudgetLedger(route=decision.route, policy=resolved_budget_policy)
        trace = _trace(state)
        trace.record(
            "policy",
            "route_selected",
            route=decision.route,
            risk=signals.score,
            confidence=confidence,
            reason=decision.reason,
            route_policy_version=decision.policy_version,
            route_policy_checksum=decision.policy_checksum,
            budget_policy_version=resolved_budget_policy.version,
            budget_policy_checksum=resolved_budget_policy.checksum,
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        return {
            "route_record": decision.replay_record(),
            "budget_record": budget.checkpoint_record().model_dump(mode="json"),
            "trace_events": _events(trace),
        }

    async def compile_node(state: V2EngineState) -> dict[str, object]:
        trace = _trace(state)
        route = _route(state)
        route_budget = _route_budget_from_state(
            state,
            route=route,
            policy=resolved_budget_policy,
        )
        try:
            context = _context_bundle(state)
            query_plan = _query_plan(state)
            query_validation = _plan_validation(state, "query_plan_validation")
            execution_plan = resolved_plan_compiler.compile(
                plan=query_plan,
                context=context,
                validation=query_validation,
            )
            execution_validation = resolved_plan_validator.validate_execution_plan(
                execution_plan=execution_plan,
                query_plan=query_plan,
                context=context,
                route_budget=route_budget.limits,
            )
        except Exception as exc:
            stop_reason = route_budget.halt("execution_plan_compilation_failed")
            trace.record(
                "candidate",
                "execution_plan_compilation_failed",
                error_type=type(exc).__name__,
                route=route,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="A governed execution plan could not be compiled.")],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": stop_reason,
                "degradation_flags": _degradation_flags(
                    state,
                    "ExecutionPlanCompilationFailed",
                ),
                "trace_events": _events(trace),
            }
        trace.record(
            "candidate",
            "execution_plan_compiled",
            execution_plan_checksum=execution_plan.checksum,
            query_plan_sha256=execution_plan.query_plan_sha256,
            policy_version=execution_plan.policy_version,
            step_count=len(execution_plan.steps),
            route=route,
        )
        trace.record(
            "policy",
            "execution_plan_validated",
            outcome=execution_validation.outcome,
            policy_version=execution_validation.policy_version,
            policy_checksum=execution_validation.policy_checksum,
            issue_codes=[issue.code for issue in execution_validation.issues],
            route=route,
        )
        await _persist_new_events(trace_sink, trace.events[-2:])
        result: dict[str, object] = {
            "execution_plan": execution_plan.model_dump(mode="json"),
            "execution_plan_validation": execution_validation.model_dump(mode="json"),
            "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
            "trace_events": _events(trace),
        }
        if execution_validation.outcome != "allow":
            stop_reason = route_budget.halt(
                "execution_plan_approval_required"
                if execution_validation.outcome == "approval"
                else "execution_plan_validation_denied"
            )
            result.update(
                {
                    "messages": [
                        AIMessage(
                            content=(
                                "The execution plan requires explicit approval."
                                if execution_validation.outcome == "approval"
                                else "The execution plan was not permitted."
                            )
                        )
                    ],
                    "budget_record": route_budget.checkpoint_record().model_dump(
                        mode="json"
                    ),
                    "stop_reason": stop_reason,
                    "degradation_flags": _degradation_flags(
                        state,
                        "ExecutionPlanApprovalRequired"
                        if execution_validation.outcome == "approval"
                        else "ExecutionPlanValidationDenied",
                    ),
                }
            )
        return result

    async def execute_node(state: V2EngineState) -> dict[str, object]:
        assert plan_executor is not None
        trace = _trace(state)
        route = _route(state)
        route_budget = _route_budget_from_state(
            state,
            route=route,
            policy=resolved_budget_policy,
        )
        query_plan = _query_plan(state)
        context = _context_bundle(state)
        execution_plan = _execution_plan(state)
        execution_validation = _plan_validation(
            state,
            "execution_plan_validation",
        )
        if (
            execution_validation.outcome != "allow"
            or execution_validation.query_plan_sha256 != query_plan.checksum
            or execution_validation.context_checksum != context.checksum
        ):
            stop_reason = route_budget.halt("execution_plan_validation_missing")
            return {
                "messages": [AIMessage(content="Validated execution authorization is unavailable.")],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": stop_reason,
                "degradation_flags": _degradation_flags(
                    state,
                    "ExecutionPlanValidationMissing",
                ),
            }
        result = await plan_executor.execute(
            query_plan=query_plan,
            context=context,
            execution_plan=execution_plan,
            budget=route_budget,
            deadline_ms=_remaining_route_deadline_ms(
                state,
                route=route,
                policy=resolved_budget_policy,
            ),
        )
        record = result.record
        trace.record(
            "sql",
            "typed_plan_executed",
            execution_plan_checksum=record.execution_plan_checksum,
            status=record.status,
            step_count=len(record.step_receipts),
            output_count=len(record.output_step_ids),
            stop_reason=record.stop_reason,
            sql_candidates=route_budget.sql_candidates,
            sql_executions=route_budget.sql_executions,
            join_hops=route_budget.join_hops,
            route=route,
        )
        response: dict[str, object] = {
            "execution_record": record.model_dump(mode="json"),
            "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
        }
        if record.status == "succeeded":
            answer = "Typed execution completed. Grounded answer rendering is pending."
            trace.record(
                "answer",
                "typed_execution_completed",
                answer_hash=fingerprint(answer),
                execution_plan_checksum=record.execution_plan_checksum,
                route=route,
                model_calls=route_budget.model_calls,
            )
            response.update(
                {
                    "messages": [AIMessage(content=answer)],
                    "degradation_flags": _degradation_flags(
                        state,
                        "GroundedAnswerPending",
                    ),
                }
            )
        else:
            response.update(
                {
                    "messages": [
                        AIMessage(
                            content="Typed execution stopped before an answer could be produced."
                        )
                    ],
                    "stop_reason": record.stop_reason or "typed_execution_failed",
                    "degradation_flags": _degradation_flags(
                        state,
                        "TypedExecutionFailed",
                    ),
                }
            )
        new_event_count = 2 if record.status == "succeeded" else 1
        await _persist_new_events(trace_sink, trace.events[-new_event_count:])
        response["trace_events"] = _events(trace)
        return response

    async def model_node(state: V2EngineState) -> dict[str, object]:
        config = get_agent_config()
        runtime = var_child_runnable_config.get()
        configurable = runtime.get("configurable", {}) if isinstance(runtime, dict) else {}
        route = _route(state)
        route_budget = _route_budget_from_state(
            state,
            route=route,
            policy=resolved_budget_policy,
        )
        deadline_ms = _remaining_route_deadline_ms(
            state,
            route=route,
            policy=resolved_budget_policy,
        )
        is_shadow = bool(configurable.get("shadow_mode")) if isinstance(configurable, dict) else False
        if route == "fast":
            stop_reason = route_budget.halt("fast_model_call_blocked")
            trace = _trace(state)
            trace.record(
                "policy",
                "model_call_blocked",
                route=route,
                reason=stop_reason,
                budget_policy_version=resolved_budget_policy.version,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "Deterministic Fast execution is unavailable for this request; "
                            "model invocation was blocked."
                        )
                    )
                ],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": stop_reason,
                "degradation_flags": ["FastModelCallBlocked"],
                "trace_events": _events(trace),
            }
        alias = "plan.standard" if is_shadow or route == "deep" else "fast.default"
        stage = "plan" if alias == "plan.standard" else "answer"
        budget = CallBudget(
            deadline_ms=deadline_ms,
            token_budget=config.v2_token_budget,
            cost_budget=config.v2_cost_budget,
            max_attempts=config.v2_max_model_attempts,
        )
        stop_reason = should_stop(budget=budget, policy=resolved_budget_policy)
        if stop_reason is not None:
            route_budget.halt(stop_reason)
            return {
                "messages": [AIMessage(content="The request budget is unavailable for this route.")],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": stop_reason,
                "degradation_flags": ["BudgetExceeded"],
            }
        try:
            route_budget.begin_model_call()
        except BudgetExceeded as exc:
            stop_reason = route_budget.stop_reason or route_budget.halt(str(exc))
            return {
                "messages": [AIMessage(content="The request budget is unavailable for this route.")],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": stop_reason,
                "degradation_flags": [type(exc).__name__],
            }
        request = ModelRequest(
            stage=stage,
            alias=alias,
            messages=[{"role": "user", "content": _question(state)}],
            deadline_ms=budget.deadline_ms,
            token_budget=budget.token_budget,
            cost_budget=budget.cost_budget,
            data_classification="internal",
            prompt_version="v2-engine-v1",
            plan_reason="deep_or_shadow_route" if stage == "plan" else None,
        )
        try:
            receipt = await model_gateway.invoke(request, budget)
        except (ModelGatewayError, BudgetExceeded) as exc:
            error_code = exc.code if isinstance(exc, ModelGatewayError) else str(exc)
            if isinstance(exc, ModelGatewayError):
                route_budget.record_error(
                    error_code,
                    policy_denied=isinstance(exc, ModelPolicyDenied),
                    retryable_provider=exc.retryable,
                )
            stop_reason = route_budget.stop_reason or route_budget.halt(error_code)
            trace = _trace(state)
            trace.record(
                "policy",
                "model_stopped",
                route=route,
                reason=stop_reason,
                error_code=error_code,
                model_calls=route_budget.model_calls,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="Model service is unavailable for this request.")],
                "degradation_flags": [type(exc).__name__],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": stop_reason,
                "trace_events": _events(trace),
            }
        retry_stop_reason = route_budget.record_provider_retries(receipt.retries)
        if retry_stop_reason is not None:
            trace = _trace(state)
            trace.record(
                "policy",
                "model_stopped",
                route=route,
                reason=retry_stop_reason,
                model_calls=route_budget.model_calls,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="Model service is unavailable for this request.")],
                "degradation_flags": ["ProviderRetryBudgetExceeded"],
                "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
                "stop_reason": retry_stop_reason,
                "trace_events": _events(trace),
            }
        answer = f"{'Shadow plan (no business SQL executed): ' if is_shadow else ''}{receipt.content}"
        trace = _trace(state)
        trace.record(
            "answer",
            "model_completed",
            resolved_model=receipt.resolved_model,
            input_tokens=receipt.usage.get("input_tokens", 0),
            output_tokens=receipt.usage.get("output_tokens", 0),
            estimated_cost=receipt.estimated_cost,
            model_alias=receipt.alias,
            model_profile_version=receipt.profile_version,
            model_profile_checksum=receipt.profile_checksum,
            prompt_version=receipt.prompt_version,
            prompt_hash=receipt.prompt_hash,
            answer_hash=fingerprint(answer),
            route=route,
            model_calls=route_budget.model_calls,
            budget_policy_version=resolved_budget_policy.version,
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        result: dict[str, object] = {
            "model_receipt": receipt.model_dump(mode="json"),
            "budget_record": route_budget.checkpoint_record().model_dump(mode="json"),
            "trace_events": _events(trace),
        }
        if route == "deep" and not is_shadow:
            # Keep the generated plan at the checkpoint; business SQL remains blocked
            # until the owner makes an explicit, versioned decision.
            result.update(
                {
                    "pending_answer": answer,
                    "needs_hitl": True,
                    "hitl_version": 1,
                    "hitl_status": "awaiting_action",
                    "applied_actions": {},
                }
            )
        else:
            result["messages"] = [AIMessage(content=answer)]
        return result

    async def hitl_node(state: V2EngineState) -> dict[str, object]:
        version = int(state.get("hitl_version", 1))
        action_payload = interrupt(
            {
                "kind": "nl2sql_hitl_action",
                "version": version,
                "actions": ["approve", "modify", "reject", "cancel"],
                "summary": state.get("pending_answer", ""),
            }
        )
        if not isinstance(action_payload, dict):
            return _failed_action("invalid_action_payload", version)
        action = action_payload.get("action")
        idempotency_key = action_payload.get("idempotency_key")
        expected_version = action_payload.get("expected_version")
        if action not in {"approve", "modify", "reject", "cancel"}:
            return _failed_action("unsupported_action", version)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return _failed_action("missing_idempotency_key", version)
        if expected_version != version:
            return _failed_action("stale_action_version", version)

        applied_actions = dict(state.get("applied_actions", {}))
        existing = applied_actions.get(idempotency_key)
        if existing is not None:
            return {
                "hitl_status": existing["status"],
                "needs_hitl": False,
                "applied_actions": applied_actions,
            }
        status_by_action: dict[str, str] = {
            "approve": "approved",
            "modify": "modified",
            "reject": "rejected",
            "cancel": "cancelled",
        }
        action_status = status_by_action[action]
        if action == "approve":
            content = str(state.get("pending_answer", ""))
        elif action == "modify":
            feedback = action_payload.get("feedback")
            content = f"Plan modification requested: {feedback}" if feedback else "Plan modification requested."
        else:
            content = f"Request {action_status}. No business SQL was executed."
        applied_actions[idempotency_key] = {"status": action_status, "version": version + 1}
        return {
            "messages": [AIMessage(content=content)],
            "hitl_status": action_status,
            "hitl_version": version + 1,
            "needs_hitl": False,
            "applied_actions": applied_actions,
        }

    def after_context(state: V2EngineState) -> Literal["plan", "__end__"]:
        return "__end__" if state.get("stop_reason") else "plan"

    def after_plan(state: V2EngineState) -> Literal["validate", "__end__"]:
        return "__end__" if state.get("stop_reason") else "validate"

    def after_validation(state: V2EngineState) -> Literal["route", "__end__"]:
        return "__end__" if state.get("stop_reason") else "route"

    def after_route(state: V2EngineState) -> Literal["compile", "model", "__end__"]:
        if state.get("stop_reason"):
            return "__end__"
        if typed_pipeline_enabled and _route(state) == "fast":
            return "compile"
        return "model"

    def after_compile(state: V2EngineState) -> Literal["execute", "__end__"]:
        return "__end__" if state.get("stop_reason") else "execute"

    def after_model(state: V2EngineState) -> Literal["hitl", "__end__"]:
        return "hitl" if state.get("needs_hitl") else "__end__"

    graph.add_node("receive", receive_node)
    graph.add_node("context", context_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_node("route", route_node)
    graph.add_node("compile", compile_node)
    graph.add_node("execute", execute_node)
    graph.add_node("model", model_node)
    graph.add_node("hitl", hitl_node)
    graph.add_edge(START, "receive")
    graph.add_edge("receive", "context")
    graph.add_conditional_edges("context", after_context, {"plan": "plan", END: END})
    graph.add_conditional_edges("plan", after_plan, {"validate": "validate", END: END})
    graph.add_conditional_edges(
        "validate",
        after_validation,
        {"route": "route", END: END},
    )
    graph.add_conditional_edges(
        "route",
        after_route,
        {"compile": "compile", "model": "model", END: END},
    )
    graph.add_conditional_edges(
        "compile",
        after_compile,
        {"execute": "execute", END: END},
    )
    graph.add_edge("execute", END)
    graph.add_conditional_edges("model", after_model, {"hitl": "hitl", END: END})
    return graph.compile(checkpointer=checkpointer, name="nl2sql_v2_explicit")


def _question(state: V2EngineState) -> str:
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", None) in {"human", "user"}:
            return str(getattr(message, "content", ""))
    return ""


def _context_bundle(state: V2EngineState) -> ContextBundle:
    raw = state.get("context_bundle")
    if not isinstance(raw, dict):
        raise ValueError("typed context bundle is unavailable")
    return ContextBundle.model_validate(raw)


def _optional_context_bundle(state: V2EngineState) -> ContextBundle | None:
    raw = state.get("context_bundle")
    return ContextBundle.model_validate(raw) if isinstance(raw, dict) else None


def _query_plan(state: V2EngineState) -> QueryPlan:
    raw = state.get("query_plan")
    if not isinstance(raw, dict):
        raise ValueError("typed query plan is unavailable")
    return QueryPlan.model_validate(raw)


def _optional_query_plan(state: V2EngineState) -> QueryPlan | None:
    raw = state.get("query_plan")
    return QueryPlan.model_validate(raw) if isinstance(raw, dict) else None


def _execution_plan(state: V2EngineState) -> ExecutionPlan:
    raw = state.get("execution_plan")
    if not isinstance(raw, dict):
        raise ValueError("typed execution plan is unavailable")
    return ExecutionPlan.model_validate(raw)


def _plan_validation(
    state: V2EngineState,
    key: Literal["query_plan_validation", "execution_plan_validation"],
) -> PlanValidationRecord:
    raw = state.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"{key} is unavailable")
    return PlanValidationRecord.model_validate(raw)


def _optional_plan_validation(
    state: V2EngineState,
    key: Literal["query_plan_validation", "execution_plan_validation"],
) -> PlanValidationRecord | None:
    raw = state.get(key)
    return PlanValidationRecord.model_validate(raw) if isinstance(raw, dict) else None


def _route(state: V2EngineState) -> RouteName:
    record = state.get("route_record")
    route = record.get("route") if isinstance(record, dict) else None
    if route not in {"fast", "standard", "deep"}:
        raise ValueError("active route decision is unavailable")
    return cast(RouteName, route)


def _request_identity() -> RequestIdentity:
    configurable = _runtime_configurable()
    raw = configurable.get("request_identity")
    if isinstance(raw, RequestIdentity):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("request identity is unavailable")
    return RequestIdentity.model_validate(raw)


def _request_deadline_ms(*, default: int) -> int:
    configurable = _runtime_configurable()
    raw_context = configurable.get("request_context")
    if isinstance(raw_context, dict):
        deadline_ms = raw_context.get("deadline_ms")
        if isinstance(deadline_ms, int) and deadline_ms >= 1:
            return min(default, deadline_ms)
    return default


def _request_elapsed_ms(state: V2EngineState) -> int:
    raw = state.get("request_started_at")
    if not isinstance(raw, str):
        return _INVALID_REQUEST_ELAPSED_MS
    try:
        started_at = datetime.fromisoformat(raw)
    except ValueError:
        return _INVALID_REQUEST_ELAPSED_MS
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    elapsed_seconds = (datetime.now(UTC) - started_at).total_seconds()
    if elapsed_seconds < -1:
        return _INVALID_REQUEST_ELAPSED_MS
    return max(0, int(elapsed_seconds * 1000))


def _remaining_route_deadline_ms(
    state: V2EngineState,
    *,
    route: RouteName,
    policy: RoutingBudgetPolicy,
) -> int:
    elapsed_ms = _request_elapsed_ms(state)
    request_limit_ms = _request_deadline_ms(
        default=get_agent_config().v2_request_deadline_ms,
    )
    return max(
        0,
        min(
            request_limit_ms - elapsed_ms,
            policy.routes[route].deadline_ms - elapsed_ms,
        ),
    )


def _pre_route_timeout_ms(
    state: V2EngineState,
    policy: RoutingBudgetPolicy,
) -> int:
    return max(
        0,
        _remaining_route_deadline_ms(
            state,
            route="standard",
            policy=policy,
        )
        - policy.reserve_ms,
    )


def _runtime_configurable() -> dict[str, object]:
    runtime = var_child_runnable_config.get()
    raw = runtime.get("configurable", {}) if isinstance(runtime, dict) else {}
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


def _degradation_flags(state: V2EngineState, *flags: str) -> list[str]:
    return list(
        dict.fromkeys(
            flag
            for flag in (*state.get("degradation_flags", []), *flags)
            if flag.strip()
        )
    )


def _failed_action(reason: str, version: int) -> dict[str, object]:
    return {
        "messages": [AIMessage(content="The requested approval action could not be applied.")],
        "hitl_status": reason,
        "hitl_version": version,
        "needs_hitl": False,
    }


def _trace(state: V2EngineState) -> TraceEnvelope:
    events = state.get("trace_events", [])
    trace = TraceEnvelope(trace_id=_trace_id(state))
    for event in events:
        trace.events.append(TraceEvent.model_validate(event))
    return trace


def _events(trace: TraceEnvelope) -> list[dict[str, object]]:
    return [event.model_dump(mode="json") for event in trace.events]


def _route_budget_from_state(
    state: V2EngineState,
    *,
    route: RouteName,
    policy: RoutingBudgetPolicy,
) -> RouteBudgetLedger:
    record = state.get("budget_record")
    if isinstance(record, dict):
        ledger = RouteBudgetLedger.from_record(policy=policy, record=record)
        if ledger.route != route:
            raise ValueError("checkpoint route does not match the active route decision")
        return ledger
    return RouteBudgetLedger(route=route, policy=policy)


def _trace_id(state: V2EngineState) -> str:
    runtime = var_child_runnable_config.get()
    configurable = runtime.get("configurable", {}) if isinstance(runtime, dict) else {}
    request_context = configurable.get("request_context", {}) if isinstance(configurable, dict) else {}
    if isinstance(request_context, dict) and isinstance(request_context.get("trace_id"), str):
        return request_context["trace_id"]
    return str(configurable.get("thread_id", "unscoped"))


async def _persist_new_events(trace_sink: TraceSink | None, events: list[TraceEvent]) -> None:
    if trace_sink is None:
        return
    try:
        async with asyncio.timeout(_TRACE_SINK_TIMEOUT_SECONDS):
            for event in events:
                await trace_sink.append(event)
    except Exception:
        # Checkpoint trace remains authoritative; optional telemetry cannot alter policy.
        return


def _signals(
    question: str,
    *,
    context: ContextBundle | None = None,
    plan: QueryPlan | None = None,
    validation: PlanValidationRecord | None = None,
    fast_budget_available: bool = True,
) -> RiskSignals:
    lower = question.lower()
    deterministic_ready = bool(
        context is not None
        and plan is not None
        and validation is not None
        and validation.outcome == "allow"
        and validation.query_plan_sha256 == plan.checksum
        and validation.context_checksum == context.checksum
        and context.resolution_status == "resolved"
        and not context.unresolved_slots
        and not context.conflict_ids
        and not plan.unresolved_slots
        and plan.source_strategy == "aggregate_first"
        and context.token_cost <= 2_000
        and context.evidence_count <= 6
        and len(context.approved_relation_ids) <= 2
    )
    if context is None:
        table_count = (
            3
            if any(token in lower for token in ("join", "关联", "多表"))
            else 1
        )
        ambiguous = any(token in lower for token in ("可能", "大概", "全部"))
        restricted_data = any(
            token in lower for token in ("敏感", "restricted", "pii")
        )
        dynamic_calculation = any(
            token in lower for token in ("同比", "环比", "排名", "自定义计算")
        )
    else:
        table_count = max(1, len(context.approved_edge_ids) + 1)
        ambiguous = context.resolution_status != "resolved" or bool(
            context.unresolved_slots
        )
        # The typed path relies on policy-filtered context and declared plan intent;
        # prompt keywords never stand in for internal authorization or plan state.
        restricted_data = False
        dynamic_calculation = plan is not None and plan.intent in {
            "comparison",
            "ranking",
        }
    return RiskSignals(
        restricted_data=restricted_data,
        table_count=table_count,
        ambiguous_metric_or_filter=ambiguous,
        dynamic_calculation=dynamic_calculation,
        unknown_explain_cost=not deterministic_ready,
        requires_model=not deterministic_ready,
        fast_budget_available=fast_budget_available,
    )
