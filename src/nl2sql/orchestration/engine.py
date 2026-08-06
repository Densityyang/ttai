"""The explicit LangGraph v2 request graph.

This graph deliberately has no tool-calling loop.  SQL execution is introduced
only by later typed candidate nodes, after the route and ModelGateway policy
have been recorded.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.contracts import ModelRequest
from src.nl2sql.infra.llm.gateway import ModelGateway, ModelGatewayError
from src.nl2sql.observability.trace import TraceEnvelope, TraceEvent, fingerprint
from src.nl2sql.orchestration.budget import BudgetExceeded, CallBudget
from src.nl2sql.orchestration.routing import RiskSignals, choose_route


class V2EngineState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    route_record: NotRequired[dict[str, object]]
    model_receipt: NotRequired[dict[str, object]]
    degradation_flags: NotRequired[list[str]]
    pending_answer: NotRequired[str]
    needs_hitl: NotRequired[bool]
    hitl_version: NotRequired[int]
    hitl_status: NotRequired[str]
    applied_actions: NotRequired[dict[str, dict[str, object]]]
    trace_events: NotRequired[list[dict[str, object]]]


class TraceSink(Protocol):
    async def append(self, event: TraceEvent) -> None: ...


def create_v2_engine(
    *,
    checkpointer: BaseCheckpointSaver,
    model_gateway: ModelGateway,
    trace_sink: TraceSink | None = None,
) -> Any:
    graph = StateGraph(V2EngineState)

    async def route_node(state: V2EngineState) -> dict[str, object]:
        question = _question(state)
        signals = _signals(question)
        confidence = 0.9 if signals.score <= 20 else 0.7 if signals.score <= 60 else 0.45
        decision = choose_route(signals=signals, confidence=confidence)
        trace = _trace(state)
        trace.record(
            "query",
            "received",
            question_fingerprint=fingerprint(question),
            question_length=len(question),
        )
        trace.record("policy", "route_selected", route=decision.route, risk=signals.score, confidence=confidence)
        await _persist_new_events(trace_sink, trace.events[-2:])
        return {"route_record": decision.replay_record(), "trace_events": _events(trace)}

    async def model_node(state: V2EngineState) -> dict[str, object]:
        config = get_agent_config()
        runtime = var_child_runnable_config.get()
        configurable = runtime.get("configurable", {}) if isinstance(runtime, dict) else {}
        raw_context = configurable.get("request_context", {}) if isinstance(configurable, dict) else {}
        request_deadline_ms = raw_context.get("deadline_ms") if isinstance(raw_context, dict) else None
        deadline_ms = min(
            config.v2_request_deadline_ms,
            request_deadline_ms if isinstance(request_deadline_ms, int) else config.v2_request_deadline_ms,
        )
        route = str(state.get("route_record", {}).get("route", "standard"))
        is_shadow = bool(configurable.get("shadow_mode")) if isinstance(configurable, dict) else False
        alias = "plan.standard" if is_shadow or route == "deep" else "fast.default"
        stage = "plan" if alias == "plan.standard" else "answer"
        budget = CallBudget(
            deadline_ms=deadline_ms,
            token_budget=config.v2_token_budget,
            cost_budget=config.v2_cost_budget,
            max_attempts=config.v2_max_model_attempts,
        )
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
            return {
                "messages": [AIMessage(content="Model service is unavailable for this request.")],
                "degradation_flags": [type(exc).__name__],
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
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        result: dict[str, object] = {
            "model_receipt": receipt.model_dump(mode="json"),
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

    def after_model(state: V2EngineState) -> Literal["hitl", "__end__"]:
        return "hitl" if state.get("needs_hitl") else "__end__"

    graph.add_node("route", route_node)
    graph.add_node("model", model_node)
    graph.add_node("hitl", hitl_node)
    graph.add_edge(START, "route")
    graph.add_edge("route", "model")
    graph.add_conditional_edges("model", after_model, {"hitl": "hitl", END: END})
    return graph.compile(checkpointer=checkpointer, name="nl2sql_v2_explicit")


def _question(state: V2EngineState) -> str:
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", None) in {"human", "user"}:
            return str(getattr(message, "content", ""))
    return ""


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
    for event in events:
        await trace_sink.append(event)


def _signals(question: str) -> RiskSignals:
    lower = question.lower()
    return RiskSignals(
        restricted_data=any(token in lower for token in ("敏感", "restricted", "pii")),
        table_count=3 if any(token in lower for token in ("join", "关联", "多表")) else 1,
        ambiguous_metric_or_filter=any(token in lower for token in ("可能", "大概", "全部")),
        dynamic_calculation=any(token in lower for token in ("同比", "环比", "排名", "自定义计算")),
        unknown_explain_cost=True,
    )
