"""The explicit LangGraph v2 request graph.

This graph deliberately has no tool-calling loop. SQL execution is introduced
only through a validated typed plan after routing and budget selection.
"""

from __future__ import annotations

import logging

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, NotRequired, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from src.nl2sql.config.settings import get_agent_config

logger = logging.getLogger(__name__)
from src.nl2sql.contracts import (
    AnswerArtifact,
    AuthorizationContext,
    ContextBundle,
    ExecutionPlan,
    ModelRequest,
    PlanExecutionRecord,
    PlanValidationRecord,
    QueryPlan,
    RequestIdentity,
    RouteName,
    RoutePolicy,
    RoutingBudgetPolicy,
    query_plan_payload,
)
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelInputPolicyDenied,
    ModelOutputInvalid,
    ModelPolicyDenied,
)
from src.nl2sql.observability.content_policy import scrub_text, scrub_value
from src.nl2sql.observability.trace import TraceEnvelope, TraceEvent, fingerprint
from src.nl2sql.orchestration.analysis_evidence import (
    AnalysisEvidenceError,
    AnalysisInterpretation,
    build_analysis_evidence,
    project_analysis_model_input,
    validate_analysis_interpretation,
)
from src.nl2sql.orchestration.budget import (
    BudgetExceeded,
    CallBudget,
    RouteBudgetLedger,
    bootstrap_routing_budget_policy,
    should_stop,
)
from src.nl2sql.orchestration.decision_contract import (
    FORBIDDEN_DECISION_FIELDS,
    HITLDecision,
    HITLRequest,
    ResumeToken,
    clarification_request,
    revalidate_decision,
    revalidate_request,
    revalidate_resume_token,
)
from src.nl2sql.orchestration.decision_contract import (
    resume_token as build_resume_token,
)
from src.nl2sql.orchestration.deterministic_query_plan import (
    normalize_frozen_real_question,
)
from src.nl2sql.orchestration.execution import PlanExecutor
from src.nl2sql.orchestration.grounding import GroundedAnswer, ground_execution_answer
from src.nl2sql.orchestration.mode_contract import ModeCapabilityOutcome, RunEnvelope
from src.nl2sql.orchestration.planning import (
    ContextResolver,
    DeterministicQueryUnsupported,
    PlanCompiler,
    PlanValidator,
    QueryPlanProvider,
)
from src.nl2sql.orchestration.routing import RiskSignals, bootstrap_route_policy, choose_route
from src.nl2sql.orchestration.typed_runtime import (
    TYPED_RUNTIME_UNAVAILABLE,
    TypedRuntimeBundle,
    TypedRuntimeUnavailable,
)
from src.nl2sql.ownership import (
    authorization_context_from_config,
    evaluate_authorization,
)
from src.nl2sql.supervisor.schemas import (
    ProvenanceAuthorityBlock,
    ProvenanceBlock,
    ProvenanceTimeRangeBlock,
    TextBlock,
)

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
    # Checkpoint-safe grounded product projection.  Raw step outputs, SQL,
    # model prompts and AnalysisEvidenceBundle never enter graph state.
    grounded_answer_artifact: NotRequired[dict[str, object] | None]
    grounded_answer_text: NotRequired[str | None]
    model_receipt: NotRequired[dict[str, object] | None]
    response_blocks: NotRequired[list[dict[str, object]]]
    degradation_flags: NotRequired[list[str]]
    stop_reason: NotRequired[str | None]
    pending_answer: NotRequired[str | None]
    needs_hitl: NotRequired[bool]
    hitl_version: NotRequired[int]
    hitl_status: NotRequired[str | None]
    applied_actions: NotRequired[dict[str, dict[str, object]]]
    # Typed clarification/decision control state.  The typed path NEVER grants
    # authority: no authorization field is read from a decision payload, and a
    # resolved decision stops at a safe checkpoint state instead of compiling or
    # executing.
    pending_decision: NotRequired[dict[str, object] | None]
    decision_status: NotRequired[str | None]
    # The version of the CURRENT active typed request (0 = none).  It advances
    # monotonically across repeated typed suspensions and never restarts at 1.
    decision_version: NotRequired[int]
    decision_ledger: NotRequired[dict[str, dict[str, object]]]
    resolved_decision: NotRequired[dict[str, object] | None]
    resume_token: NotRequired[dict[str, object] | None]
    # Slot-bound replan result: the NEW plan/validation after applying resolved
    # slot bindings, plus suspended->replanned lineage records.
    resolved_plan: NotRequired[dict[str, object] | None]
    resolved_plan_validation: NotRequired[dict[str, object] | None]
    plan_lineage: NotRequired[list[dict[str, object]]]
    trace_events: NotRequired[list[dict[str, object]]]
    request_started_at: NotRequired[str]
    # S1c / frozen V1 authorization lifetime: the trusted authorization snapshot
    # resolved ONCE at run start and BOUND TO THIS RUN.  It is restored (not
    # re-fetched) for HITL suspension, resume and continuation of the same run,
    # and a supplied snapshot that differs is a run-binding mismatch.
    authorization_context: NotRequired[dict[str, object] | None]
    authorization_revision: NotRequired[str | None]
    typed_runtime_unavailable_reason: NotRequired[str | None]
    # The server-owned RunEnvelope for THIS run: run identity + the immutable
    # effective ProductMode.  Declared here so it PERSISTS across checkpoints
    # (and therefore across resume/stream paths); the mode axis is what enforces
    # the QUERY zero-model capability, so it must never depend on the caller.
    run_envelope: NotRequired[dict[str, object] | None]
    # Server-derived owner of the persisted run.  This is deliberately outside
    # RunEnvelope: it is authorization binding state, never client authority.
    run_owner_user_id: NotRequired[str | None]
    # Set once a resumed run has revalidated under CURRENT authorization and
    # compiled an ALLOW ExecutionPlan, so after_replan can route to execution.
    # Declared here for the same reason as run_envelope: an undeclared key is
    # silently dropped by LangGraph state, which would make the continuation
    # edge dead code.
    continuation_ready: NotRequired[bool]
    # The typed mode-capability outcome when an effective QUERY run cannot be
    # served deterministically.  Carries the explicit "suggest ANALYZE" signal
    # so the product can offer a switch instead of silently using a model.
    mode_capability_outcome: NotRequired[dict[str, object] | None]
    # Set when a deterministic provider reported it cannot serve a QUERY request,
    # so after_plan routes to the capability node instead of failing the run.
    mode_capability_pending: NotRequired[bool]


class TraceSink(Protocol):
    async def append(self, event: TraceEvent) -> None: ...


class TypedRuntimeFactory(Protocol):
    """Application-scoped CALLABLE; its output is constructed per request.

    The engine stores this callable (deployment-scoped) and nothing it returns.
    """

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
    ) -> "TypedRuntimeBundle | TypedRuntimeUnavailable": ...


class _RequestTypedScope:
    """One request's memo cell for the factory output.

    The engine holds only the factory callable; this cell is created per graph
    invocation and discarded when the invocation ends, so no request identity,
    authorization or compiler survives a request in AppContainer or the graph.
    """

    __slots__ = ("_factory", "_resolved", "runtime")

    def __init__(self, factory: TypedRuntimeFactory) -> None:
        self._factory = factory
        self._resolved = False
        # Protocol-typed: the engine knows ONLY TypedRuntimeBundle, so a demo
        # runtime is accepted exactly like the production one.
        self.runtime: TypedRuntimeBundle | TypedRuntimeUnavailable | None = None

    async def resolve(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
    ) -> TypedRuntimeBundle | TypedRuntimeUnavailable:
        if self._resolved:
            assert self.runtime is not None
            return self.runtime
        self._resolved = True
        self.runtime = await self._factory(
            identity=identity,
            authorization=authorization,
            expected_revision=expected_revision,
        )
        return self.runtime


_REQUEST_TYPED_SCOPE: ContextVar[_RequestTypedScope | None] = ContextVar(
    "nl2sql_request_typed_scope", default=None
)


class _RequestScopedTypedEngine:
    """Set a fresh typed-runtime memo cell around each graph invocation.

    Node tasks inherit this context (langgraph copies the caller context at
    submit time), so the whole invocation shares ONE runtime while the runtime
    still dies with the invocation.  The compiled graph is unchanged for the
    non-factory wiring, which is returned directly.
    """

    def __init__(self, compiled: Any, factory: TypedRuntimeFactory) -> None:
        self._compiled = compiled
        self._factory = factory

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_compiled"), name)

    def _new_scope(self) -> _RequestTypedScope:
        return _RequestTypedScope(self._factory)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        token = _REQUEST_TYPED_SCOPE.set(self._new_scope())
        try:
            return await self._compiled.ainvoke(input, config, **kwargs)
        finally:
            _REQUEST_TYPED_SCOPE.reset(token)

    async def astream(
        self, input: Any, config: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        token = _REQUEST_TYPED_SCOPE.set(self._new_scope())
        try:
            async for item in self._compiled.astream(input, config, **kwargs):
                yield item
        finally:
            _REQUEST_TYPED_SCOPE.reset(token)

    async def astream_events(
        self, input: Any, config: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        token = _REQUEST_TYPED_SCOPE.set(self._new_scope())
        try:
            async for item in self._compiled.astream_events(input, config, **kwargs):
                yield item
        finally:
            _REQUEST_TYPED_SCOPE.reset(token)


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
    typed_runtime_factory: TypedRuntimeFactory | None = None,
) -> Any:
    resolved_route_policy = route_policy or bootstrap_route_policy()
    resolved_budget_policy = budget_policy or bootstrap_routing_budget_policy()
    # S1c: the request-scoped components MUST NOT be engine/graph-lifetime state,
    # so the factory is the only way to reach them.  It participates in BOTH
    # guard tuples below, so a half-wired configuration is impossible.
    static_request_components = (context_resolver, query_plan_provider, plan_executor)
    typed_pipeline_requested = typed_runtime_factory is not None or any(
        component is not None
        for component in (
            context_resolver,
            query_plan_provider,
            plan_validator,
            plan_compiler,
            plan_executor,
        )
    )
    if typed_runtime_factory is not None and any(
        component is not None for component in static_request_components
    ):
        raise ValueError(
            "typed runtime factory cannot be combined with static typed collaborators"
        )
    typed_pipeline_enabled = typed_runtime_factory is not None or all(
        component is not None for component in static_request_components
    )
    if typed_pipeline_requested and not typed_pipeline_enabled:
        raise ValueError(
            "typed plan pipeline requires context resolver, query plan provider, and executor"
        )
    if (
        typed_runtime_factory is None
        and typed_pipeline_enabled
        and not bool(getattr(query_plan_provider, "is_deterministic", False))
    ):
        raise ValueError(
            "pre-route query plan provider must be deterministic and zero-model"
        )
    resolved_plan_validator = plan_validator or PlanValidator()
    resolved_plan_compiler = plan_compiler or PlanCompiler()

    def _request_runtime() -> TypedRuntimeBundle:
        scope = _REQUEST_TYPED_SCOPE.get()
        runtime = scope.runtime if scope is not None else None
        # The runtime-checkable Protocol gate: production RequestTypedRuntime and
        # DemoRequestTypedRuntime are BOTH accepted, and nothing in the engine
        # special-cases either concrete implementation.
        if not isinstance(runtime, TypedRuntimeBundle):
            raise RuntimeError("request-scoped typed runtime is unavailable")
        return runtime

    def _typed_context_resolver() -> ContextResolver:
        if typed_runtime_factory is None:
            assert context_resolver is not None
            return context_resolver
        return _request_runtime().context_resolver

    def _typed_query_plan_provider() -> QueryPlanProvider:
        if typed_runtime_factory is None:
            assert query_plan_provider is not None
            return query_plan_provider
        return _request_runtime().query_plan_provider

    def _typed_plan_executor() -> PlanExecutor:
        if typed_runtime_factory is None:
            assert plan_executor is not None
            return plan_executor
        return _request_runtime().plan_executor

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
        # Frozen V1: the ONE authority read of this engine happens HERE, at run
        # start.  The trusted snapshot is bound to the run; no later node
        # re-reads the configurable for authority.
        authorization_context, authorization_revision = _resolve_run_authorization()
        return {
            "context_bundle": None,
            "query_plan": None,
            "query_plan_validation": None,
            "route_record": None,
            "budget_record": None,
            "execution_plan": None,
            "execution_plan_validation": None,
            "execution_record": None,
            "grounded_answer_artifact": None,
            "grounded_answer_text": None,
            "model_receipt": None,
            "response_blocks": [],
            "degradation_flags": [],
            "stop_reason": None,
            "pending_answer": None,
            "needs_hitl": False,
            "hitl_status": None,
            "pending_decision": None,
            "decision_status": None,
            "decision_version": 0,
            "decision_ledger": {},
            "resolved_decision": None,
            "resume_token": None,
            "resolved_plan": None,
            "resolved_plan_validation": None,
            "plan_lineage": [],
            "authorization_context": authorization_context,
            "authorization_revision": authorization_revision,
            "typed_runtime_unavailable_reason": None,
            # Persist the server-owned run envelope so the mode axis survives
            # into every later node, checkpoint and resume.  Re-validated here
            # so a malformed/missing envelope can never fake a mode.
            "run_envelope": _persisted_run_envelope(state),
            "run_owner_user_id": _run_owner_user_id(),
            "continuation_ready": False,
            "trace_events": _events(trace),
            "request_started_at": request_started_at,
        }

    async def typed_runtime_node(state: V2EngineState) -> dict[str, object]:
        """Resolve ONE request-scoped typed runtime, or fail closed canonically.

        The factory is invoked with the identity read from the EXISTING runtime
        configurable and the AuthorizationContext RESTORED from the run-bound
        snapshot (resolve-once at receive_node), plus the revision previously
        bound to this run (if any).  The output is memoized only inside this
        invocation's scope cell.
        """

        trace = _trace(state)
        scope = _REQUEST_TYPED_SCOPE.get()
        if scope is None:
            # Protocol-typed: production and demo runtimes are both accepted.
            runtime_or_unavailable: TypedRuntimeBundle | TypedRuntimeUnavailable = (
                TypedRuntimeUnavailable(reason="request_scope_missing")
            )
        else:
            try:
                identity = _request_identity()
            except ValueError:
                identity = None
            # Frozen V1: authorization was resolved ONCE at run start and bound to
            # this run.  This node ONLY restores that snapshot; it never re-reads
            # the configurable for authority.
            authorization = _bound_authorization_context(state)
            if identity is None:
                runtime_or_unavailable = TypedRuntimeUnavailable(
                    reason="request_identity_missing"
                )
            else:
                runtime_or_unavailable = await scope.resolve(
                    identity=identity,
                    authorization=authorization,
                    expected_revision=_bound_authorization_revision(state),
                )
        if isinstance(runtime_or_unavailable, TypedRuntimeUnavailable):
            trace.record(
                "policy",
                "typed_runtime_unavailable",
                reason=runtime_or_unavailable.reason,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [
                    AIMessage(
                        content="The typed query runtime is unavailable for this request."
                    )
                ],
                "stop_reason": TYPED_RUNTIME_UNAVAILABLE,
                "typed_runtime_unavailable_reason": runtime_or_unavailable.reason,
                "degradation_flags": _degradation_flags(
                    state,
                    "TypedRuntimeUnavailable",
                ),
                "trace_events": _events(trace),
            }
        trace.record(
            "policy",
            "typed_runtime_resolved",
            authorization_revision=runtime_or_unavailable.authorization_revision,
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        return {
            "authorization_context": runtime_or_unavailable.authorization.model_dump(
                mode="json"
            ),
            "authorization_revision": runtime_or_unavailable.authorization_revision,
            "typed_runtime_unavailable_reason": None,
            "degradation_flags": _degradation_flags(state),
            "trace_events": _events(trace),
        }

    async def context_node(state: V2EngineState) -> dict[str, object]:
        if not typed_pipeline_enabled:
            return {}
        resolver = _typed_context_resolver()
        trace = _trace(state)
        try:
            timeout_ms = _pre_route_timeout_ms(state, resolved_budget_policy)
            if timeout_ms < 1:
                raise TimeoutError
            async with asyncio.timeout(timeout_ms / 1000):
                context = await resolver.resolve(
                    question=normalize_frozen_real_question(_question(state)),
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
        provider = _typed_query_plan_provider()
        trace = _trace(state)
        try:
            context = _context_bundle(state)
            timeout_ms = _pre_route_timeout_ms(state, resolved_budget_policy)
            if timeout_ms < 1:
                raise TimeoutError
            async with asyncio.timeout(timeout_ms / 1000):
                plan = await provider.propose(
                    question=normalize_frozen_real_question(_question(state)),
                    context=context,
                    identity=_request_identity(),
                )
        except DeterministicQueryUnsupported as exc:
            if _effective_mode(state) == "BUILD":
                # Mode3 BUILD is a SEMANTIC AUTHORING run, not a metric query.
                # The Definition workbench owns the calculation; this server run
                # binds the thread/run so the workbench may proceed.
                trace.record("candidate", "build_proposal_ready", code=exc.code)
                await _persist_new_events(trace_sink, trace.events[-1:])
                return {
                    "response_blocks": [
                        {
                            "type": "plan_card",
                            "title": "Definition 构建已就绪",
                            "summary": (
                                "已创建 BUILD 运行。请在 Definition 工作台创建或编辑"
                                "计算定义；本运行已绑定 thread/run，可继续确认、保存并发布。"
                            ),
                            "version": 1,
                            "actions": ["open_definition_workbench"],
                        }
                    ],
                    "trace_events": _events(trace),
                }
            # A deterministic provider cannot serve this request. In QUERY this
            # must become the typed cannot_resolve/mode_suggestion outcome - it is
            # NOT a proposal failure, and it must NEVER fall through to a model.
            if _effective_mode(state) == "QUERY":
                trace.record(
                    "candidate",
                    "query_plan_deterministic_unsupported",
                    code=exc.code,
                )
                await _persist_new_events(trace_sink, trace.events[-1:])
                return {
                    "mode_capability_pending": True,
                    "trace_events": _events(trace),
                }
            # Any other mode keeps the existing failure behavior.
            trace.record(
                "candidate",
                "query_plan_proposal_failed",
                error_type=type(exc).__name__,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="A typed query plan could not be produced.")],
                "stop_reason": "query_plan_proposal_failed",
                "degradation_flags": _degradation_flags(
                    state,
                    "QueryPlanProposalFailed",
                ),
                "trace_events": _events(trace),
            }
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
            "query_plan": query_plan_payload(plan),
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
        if validation.outcome == "clarify":
            # Typed suspension: build the server-owned HITLRequest and route to
            # the typed decision node.  Clarification is NOT terminal by itself,
            # but a clarify record with no bounded slot fails closed.
            try:
                request = clarification_request(
                    validation=validation,
                    context=_context_bundle(state),
                    plan=_query_plan(state),
                    version=int(state.get("decision_version", 0)) + 1,
                )
            except Exception as exc:
                trace.record(
                    "policy",
                    "typed_clarification_projection_failed",
                    error_type=type(exc).__name__,
                )
                await _persist_new_events(trace_sink, trace.events[-1:])
                result.update(
                    {
                        "messages": [
                            AIMessage(
                                content=(
                                    "The request needs clarification, but no bounded "
                                    "clarification could be formed."
                                )
                            )
                        ],
                        "stop_reason": "query_plan_clarification_unbounded",
                        "degradation_flags": _degradation_flags(
                            state, "PlanClarificationUnbounded"
                        ),
                        "trace_events": _events(trace),
                    }
                )
                return result
            result.update(
                {
                    "pending_decision": request.model_dump(mode="json"),
                    "decision_status": "awaiting_decision",
                    "decision_version": request.version,
                    "needs_hitl": False,
                }
            )
        elif validation.outcome != "allow":
            result.update(
                {
                    "messages": [
                        AIMessage(content="The typed query plan was not permitted.")
                    ],
                    "stop_reason": "query_plan_validation_denied",
                    "degradation_flags": _degradation_flags(
                        state, "PlanValidationDenied"
                    ),
                }
            )
        return result

    async def route_node(state: V2EngineState) -> dict[str, object]:
        if _effective_mode(state) == "ANALYZE" and not typed_pipeline_enabled:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "Governed analysis requires a typed query plan and "
                            "execution evidence."
                        )
                    )
                ],
                "stop_reason": "analysis_typed_pipeline_unavailable",
                "degradation_flags": _degradation_flags(
                    state, "AnalysisTypedPipelineUnavailable"
                ),
            }
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

    async def mode_capability_node(state: V2EngineState) -> dict[str, object]:
        """Typed deterministic QUERY capability outcome. NEVER invokes a model.

        A bounded semantic clarification stays a CLARIFICATION and is never
        converted into a mode suggestion; only a genuinely unservable QUERY
        request produces cannot_resolve.
        """

        envelope_raw = state.get("run_envelope")
        envelope = envelope_raw if isinstance(envelope_raw, dict) else {}
        run_id = envelope.get("run_id")
        run_id = run_id if isinstance(run_id, str) and run_id else "unknown-run"
        validation = state.get("query_plan_validation")
        outcome_value = validation.get("outcome") if isinstance(validation, dict) else None
        if outcome_value == "clarify":
            # Bounded clarification remains clarification - not a mode switch.
            return {
                "mode_capability_outcome": None,
                "stop_reason": "query_plan_clarification_required",
            }
        capability = ModeCapabilityOutcome(
            run_id=run_id,
            effective_mode="QUERY",
            outcome="cannot_resolve",
            suggested_mode="ANALYZE",
        )
        return {
            "mode_capability_outcome": capability.model_dump(mode="json"),
            "stop_reason": "mode_cannot_resolve",
            "needs_hitl": False,
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
        # Local name intentionally mirrors the static collaborator so the
        # database-execution allowlist keeps one stable receiver (executor.execute
        # would be a second, unlisted call site).
        plan_executor = _typed_plan_executor()
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
            or execution_validation.execution_plan_sha256 != execution_plan.checksum
            or execution_validation.policy_version
            != resolved_plan_validator.policy_version
            or execution_validation.policy_checksum
            != resolved_plan_validator.policy_checksum
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
            validation=execution_validation,
            expected_policy_version=resolved_plan_validator.policy_version,
            expected_policy_checksum=resolved_plan_validator.policy_checksum,
            budget=route_budget,
            deadline_ms=_remaining_route_deadline_ms(
                state,
                route=route,
                policy=resolved_budget_policy,
            ),
        )
        record = result.record
        # S1d: the executor's request-local outputs map is the ONLY grounding
        # material (records carry only receipts).  It is captured here and used
        # in this node only; it must never enter engine state, a checkpoint or a
        # message.  Only the rendered answer text below may persist.
        outputs = result.outputs
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
            # The raw outputs remain request-local.  Only the bounded grounded
            # artifact and its deterministic text may cross the checkpoint.
            grounded = ground_execution_answer(
                query_plan=query_plan,
                execution_plan=execution_plan,
                record=record,
                outputs=outputs,
            )
            response_blocks: list[dict[str, object]] = [
                TextBlock(text=grounded.answer_text).model_dump(mode="json")
            ]
            if grounded.facts:
                receipt_by_step = {
                    receipt.step_id: receipt for receipt in record.step_receipts
                }
                source_ids = tuple(
                    dict.fromkeys(
                        receipt.source_id
                        for receipt in receipt_by_step.values()
                        if receipt.source_id is not None
                    )
                )
                source_checkpoints = tuple(
                    dict.fromkeys(
                        receipt.source_checkpoint
                        for receipt in receipt_by_step.values()
                        if receipt.source_checkpoint is not None
                    )
                )
                response_blocks.append(
                    ProvenanceBlock(
                        evidence_checksum=fingerprint(
                            grounded.artifact.model_dump_json()
                        ),
                        metric_keys=tuple(
                            dict.fromkeys(
                                fact.metric_key
                                for fact in grounded.facts
                                if fact.metric_key is not None
                            )
                        ),
                        analysis_window=ProvenanceTimeRangeBlock(
                            start=query_plan.time_range.start.isoformat(),
                            end=query_plan.time_range.end.isoformat(),
                            timezone=query_plan.time_range.timezone,
                        ),
                        data_as_of=next(
                            (
                                fact.data_as_of
                                for fact in grounded.facts
                                if fact.data_as_of is not None
                            ),
                            None,
                        ),
                        data_as_of_date=next(
                            (
                                fact.data_as_of.astimezone(
                                    ZoneInfo("Asia/Shanghai")
                                ).date()
                                for fact in grounded.facts
                                if fact.data_as_of is not None
                            ),
                            None,
                        ),
                        source_ids=source_ids,
                        source_checkpoints=source_checkpoints,
                        fact_ids=tuple(fact.fact_id for fact in grounded.facts),
                        authority_provenance=ProvenanceAuthorityBlock(
                            execution_plan_checksum=execution_plan.checksum,
                            receipt_step_ids=tuple(
                                dict.fromkeys(fact.step_id for fact in grounded.facts)
                            ),
                            source_ids=source_ids,
                            source_checkpoints=source_checkpoints,
                            semantic_signatures=tuple(
                                dict.fromkeys(
                                    fact.semantic_signature
                                    for fact in grounded.facts
                                    if fact.semantic_signature is not None
                                )
                            ),
                        ),
                    ).model_dump(mode="json")
                )
            trace.record(
                "answer",
                "typed_execution_completed",
                answer_hash=fingerprint(grounded.answer_text),
                execution_plan_checksum=record.execution_plan_checksum,
                route=route,
                model_calls=route_budget.model_calls,
            )
            response.update(
                {
                    "messages": [AIMessage(content=grounded.answer_text)],
                    "grounded_answer_artifact": grounded.artifact.model_dump(
                        mode="json"
                    ),
                    "grounded_answer_text": grounded.answer_text,
                    "response_blocks": response_blocks,
                    "degradation_flags": _degradation_flags(
                        state,
                        *grounded.artifact.degradation_flags,
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

    async def analysis_node(state: V2EngineState) -> dict[str, object]:
        """Interpret governed facts once; evidence remains request-local."""

        try:
            artifact_raw = state.get("grounded_answer_artifact")
            answer_text = state.get("grounded_answer_text")
            if not isinstance(artifact_raw, dict) or not isinstance(answer_text, str):
                raise AnalysisEvidenceError("analysis_grounded_answer_missing")
            artifact = AnswerArtifact.model_validate(artifact_raw)
            grounded = GroundedAnswer(
                answer_text=answer_text,
                facts=artifact.facts,
                artifact=artifact,
            )
            record = PlanExecutionRecord.model_validate(state.get("execution_record"))
            query_plan = _query_plan(state)
            execution_plan = _execution_plan(state)
            evidence = build_analysis_evidence(
                grounded=grounded,
                record=record,
                execution_plan_checksum=execution_plan.checksum,
                analysis_window=query_plan.time_range,
            )
            projection = project_analysis_model_input(
                evidence,
                analysis_goal=_question(state),
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, AnalysisEvidenceError)
                else "analysis_evidence_invalid"
            )
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "Governed analysis evidence is unavailable; no model "
                            "interpretation was attempted."
                        )
                    )
                ],
                "stop_reason": code,
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "AnalysisEvidenceUnavailable"
                ),
            }

        config = get_agent_config()
        route = _route(state)
        try:
            route_budget = _route_budget_from_state(
                state,
                route=route,
                policy=resolved_budget_policy,
            )
        except Exception:
            return {
                "messages": [AIMessage(content="Analysis budget state is invalid.")],
                "stop_reason": "analysis_budget_record_invalid",
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "AnalysisBudgetRecordInvalid"
                ),
            }
        deadline_ms = _remaining_route_deadline_ms(
            state,
            route=route,
            policy=resolved_budget_policy,
        )
        if deadline_ms < 1:
            return {
                "messages": [AIMessage(content="Analysis deadline was exhausted.")],
                "stop_reason": "analysis_deadline_exceeded",
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "AnalysisDeadlineExceeded"
                ),
                "budget_record": route_budget.checkpoint_record().model_dump(
                    mode="json"
                ),
            }
        budget = CallBudget(
            deadline_ms=deadline_ms,
            token_budget=config.v2_token_budget,
            cost_budget=config.v2_cost_budget,
            max_attempts=config.v2_max_model_attempts,
        )
        request = ModelRequest(
            stage="answer",
            alias="fast.default",
            messages=[message.model_dump(mode="json") for message in projection.messages],
            tool_schema=AnalysisInterpretation.model_json_schema(),
            deadline_ms=budget.deadline_ms,
            token_budget=budget.token_budget,
            cost_budget=budget.cost_budget,
            data_classification="internal",
            prompt_version="v2-governed-analysis-v1",
            authorization_revision=_bound_authorization_revision(state),
        )
        try:
            route_budget.begin_model_call()
        except BudgetExceeded as exc:
            stop_reason = route_budget.stop_reason or route_budget.halt(str(exc))
            return {
                "messages": [AIMessage(content="Analysis model budget is unavailable.")],
                "stop_reason": stop_reason,
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "AnalysisModelBudgetExceeded"
                ),
                "budget_record": route_budget.checkpoint_record().model_dump(
                    mode="json"
                ),
            }
        try:
            structured = await model_gateway.invoke_structured(
                request,
                budget,
                AnalysisInterpretation,
            )
            interpretation = validate_analysis_interpretation(
                structured.output,
                evidence=evidence,
            )
        except ModelOutputInvalid as exc:
            logger.warning(
                "analysis model output invalid: code=%s causes=%s",
                exc.code,
                list(getattr(exc.failure, "causes", ()) or ()),
            )
            route_budget.record_error(exc.code)
            stop_reason = route_budget.stop_reason or route_budget.halt(
                "analysis_interpretation_invalid"
            )
            trace = _trace(state)
            trace.record(
                "policy",
                "analysis_model_stopped",
                reason=stop_reason,
                error_code=exc.code,
                route=route,
                model_calls=route_budget.model_calls,
                sql_executions=route_budget.sql_executions,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [
                    AIMessage(
                        content="The governed model interpretation was invalid."
                    )
                ],
                "model_receipt": scrub_value(exc.receipt.model_dump(mode="json")),
                "stop_reason": stop_reason,
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "AnalysisInterpretationInvalid"
                ),
                "budget_record": route_budget.checkpoint_record().model_dump(
                    mode="json"
                ),
                "trace_events": _events(trace),
            }
        except AnalysisEvidenceError as exc:
            logger.warning("analysis evidence rejected: code=%s", getattr(exc, "code", "?"))
            route_budget.record_error("analysis_interpretation_invalid")
            stop_reason = route_budget.stop_reason or route_budget.halt(
                "analysis_interpretation_invalid"
            )
            trace = _trace(state)
            trace.record(
                "policy",
                "analysis_model_stopped",
                reason=stop_reason,
                error_code="analysis_interpretation_invalid",
                route=route,
                model_calls=route_budget.model_calls,
                sql_executions=route_budget.sql_executions,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [
                    AIMessage(
                        content="The governed model interpretation was invalid."
                    )
                ],
                "model_receipt": scrub_value(
                    structured.receipt.model_dump(mode="json")
                ),
                "stop_reason": stop_reason,
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "AnalysisInterpretationInvalid"
                ),
                "budget_record": route_budget.checkpoint_record().model_dump(
                    mode="json"
                ),
                "trace_events": _events(trace),
            }
        except (ModelGatewayError, BudgetExceeded) as exc:
            code = exc.code if isinstance(exc, ModelGatewayError) else str(exc)
            if isinstance(exc, ModelGatewayError):
                route_budget.record_error(
                    code,
                    policy_denied=isinstance(exc, ModelPolicyDenied),
                    retryable_provider=exc.retryable,
                )
            stop_reason = route_budget.stop_reason or route_budget.halt(code)
            trace = _trace(state)
            trace.record(
                "policy",
                "analysis_model_stopped",
                reason=stop_reason,
                error_code=code,
                route=route,
                model_calls=route_budget.model_calls,
                sql_executions=route_budget.sql_executions,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [
                    AIMessage(content="Model service is unavailable for analysis.")
                ],
                "stop_reason": stop_reason,
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, type(exc).__name__
                ),
                "budget_record": route_budget.checkpoint_record().model_dump(
                    mode="json"
                ),
                "trace_events": _events(trace),
            }

        receipt = structured.receipt
        retry_stop_reason = route_budget.record_provider_retries(receipt.retries)
        if retry_stop_reason is not None:
            trace = _trace(state)
            trace.record(
                "policy",
                "analysis_model_stopped",
                reason=retry_stop_reason,
                route=route,
                model_calls=route_budget.model_calls,
                retryable_provider_errors=route_budget.retryable_provider_errors,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return {
                "messages": [AIMessage(content="Model service is unavailable for analysis.")],
                "model_receipt": scrub_value(receipt.model_dump(mode="json")),
                "stop_reason": retry_stop_reason,
                "response_blocks": [],
                "degradation_flags": _degradation_flags(
                    state, "ProviderRetryBudgetExceeded"
                ),
                "budget_record": route_budget.checkpoint_record().model_dump(
                    mode="json"
                ),
                "trace_events": _events(trace),
            }

        rendered = _render_analysis_interpretation(
            source_facts=grounded.answer_text,
            interpretation=interpretation,
        )
        blocks = [
            TextBlock(text=rendered).model_dump(mode="json"),
            ProvenanceBlock(
                evidence_checksum=evidence.checksum,
                metric_keys=evidence.metric_keys,
                analysis_window=ProvenanceTimeRangeBlock(
                    start=evidence.analysis_window.start.isoformat(),
                    end=evidence.analysis_window.end.isoformat(),
                    timezone=evidence.analysis_window.timezone,
                ),
                data_as_of=evidence.data_as_of,
                data_as_of_date=(
                    evidence.data_as_of.astimezone(
                        ZoneInfo("Asia/Shanghai")
                    ).date()
                    if evidence.data_as_of is not None
                    else None
                ),
                source_ids=evidence.authority_provenance.source_ids,
                source_checkpoints=(
                    evidence.authority_provenance.source_checkpoints
                ),
                fact_ids=tuple(fact.fact_id for fact in evidence.facts),
                authority_provenance=ProvenanceAuthorityBlock(
                    **evidence.authority_provenance.model_dump(mode="json")
                ),
                model_provider=receipt.provider,
                model=receipt.resolved_model,
            ).model_dump(mode="json"),
        ]
        trace = _trace(state)
        trace.record(
            "answer",
            "governed_analysis_completed",
            evidence_checksum=evidence.checksum,
            fact_count=len(evidence.facts),
            model_provider=receipt.provider,
            resolved_model=receipt.resolved_model,
            model_calls=route_budget.model_calls,
            sql_executions=route_budget.sql_executions,
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        return {
            "messages": [AIMessage(content=rendered)],
            "model_receipt": scrub_value(receipt.model_dump(mode="json")),
            "response_blocks": blocks,
            "budget_record": route_budget.checkpoint_record().model_dump(
                mode="json"
            ),
            "trace_events": _events(trace),
        }

    async def model_node(state: V2EngineState) -> dict[str, object]:
        config = get_agent_config()
        runtime = var_child_runnable_config.get()
        configurable = runtime.get("configurable", {}) if isinstance(runtime, dict) else {}
        # S1c / frozen V1: a deep/BUILD suspension RESTORES the authorization
        # snapshot bound to this run at run start.  It must NOT re-read the
        # configurable, so a config drift between run start and suspension cannot
        # rebind the run.  Nothing bound => existing authorization-free behaviour.
        authorization_snapshot = state.get("authorization_context")
        if not isinstance(authorization_snapshot, dict):
            authorization_snapshot = None
        authorization_revision = _bound_authorization_revision(state)
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
            authorization_revision=authorization_revision,
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
            policy_evidence: dict[str, object] = {}
            if isinstance(exc, ModelInputPolicyDenied):
                policy_evidence = {
                    "model_input_policy_version": exc.decision.policy_version,
                    "model_input_policy_checksum": exc.decision.policy_checksum,
                    "matched_categories": list(exc.decision.matched_categories),
                    "target_provider": exc.decision.target_provider,
                    "target_model": exc.decision.target_model,
                }
            trace.record(
                "policy",
                "model_stopped",
                route=route,
                reason=stop_reason,
                error_code=error_code,
                model_calls=route_budget.model_calls,
                **policy_evidence,
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
        answer = scrub_text(
            f"{'Shadow plan (no business SQL executed): ' if is_shadow else ''}{receipt.content}"
        )
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
            model_input_policy_version=receipt.model_input_policy_version,
            model_input_policy_checksum=receipt.model_input_policy_checksum,
            egress_outcome=receipt.egress_outcome,
            content_sha256=receipt.content_sha256,
            matched_categories=list(receipt.matched_categories),
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        # Checkpoint scrubbing: the checkpointed receipt is a persisted payload,
        # so configured secret values and secret shapes are removed before it is
        # projected.  Ordinary business answer content is preserved verbatim.
        result: dict[str, object] = {
            "model_receipt": scrub_value(receipt.model_dump(mode="json")),
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
                    "authorization_context": authorization_snapshot,
                    "authorization_revision": authorization_revision,
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

        # S1c A4 / frozen V1 run binding: BEFORE the action is applied, any
        # EXPLICITLY supplied snapshot is compared against the snapshot BOUND TO
        # THIS RUN and restored from run state.  A mismatch is a run-binding
        # mismatch; the current configurable is never re-fetched as a new
        # Backend revision for the same run.
        supplied_authorization = _authorization_from_payload(
            action_payload.get("authorization_context")
        )
        binding_failure = authorization_run_binding_failure(
            state, supplied_authorization
        )
        if binding_failure is not None:
            failure = _failed_action(binding_failure, version)
            failure["stop_reason"] = "authorization_run_binding_mismatch"
            return failure

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

    async def decision_node(state: V2EngineState) -> dict[str, object]:
        """Typed clarification / decision suspension.

        A valid decision is RECORDED and handed to deterministic slot-bound
        replan/revalidation.  It never compiles or executes, never reads
        authorization from the payload, and never treats a client-supplied
        ResumeToken as authority.  An invalid decision re-prompts while
        preserving the pending typed request so the user can retry.
        """

        stored = _pending_decision(state)
        if stored is None:
            return {
                "messages": [
                    AIMessage(content="The pending decision could not be restored.")
                ],
                "stop_reason": "typed_decision_state_invalid",
                "decision_status": "invalid",
                "degradation_flags": _degradation_flags(
                    state, "TypedDecisionStateInvalid"
                ),
            }
        failure: str | None = None
        while True:
            prompt: dict[str, object] = {
                "kind": "nl2sql_typed_clarification_decision",
                "request_id": stored.request_id,
                "decision_kind": stored.decision_kind,
                "version": stored.version,
                "allowed_actions": list(stored.allowed_actions),
                "unresolved_slots": list(stored.unresolved_slots),
                "issue_codes": list(stored.issue_codes),
                "safe_summary": stored.safe_summary,
            }
            if failure is not None:
                prompt["previous_failure"] = failure
            payload = interrupt(prompt)
            decision, failure = _typed_decision_from_payload(stored, payload)
            if decision is None:
                continue
            entry = _typed_decision_ledger_entry(state, decision.idempotency_key)
            if entry is not None:
                if _typed_decision_replays(entry, stored, decision):
                    return _typed_decision_replay_result(state, entry, decision)
                failure = "typed_decision_idempotency_conflict"
                continue
            return _record_typed_decision(state, stored, decision)

    async def replan_node(state: V2EngineState) -> dict[str, object]:
        """Deterministic slot-bound replan + revalidation (V1: time/grain only).

        Reads EVERY trusted input from checkpoint state; nothing is accepted
        from the client.  A replanned ALLOW stops at
        resolved_pending_current_authorization because CURRENT Backend
        authorization cannot yet be proven at resume time.  A replanned CLARIFY
        suspends again with a NEW typed request.  A replanned DENY terminates.
        """

        stored = _pending_decision(state)
        decision = _resolved_decision(state)
        token = _stored_resume_token(state)
        if stored is None or decision is None or token is None:
            return _continuation_stopped(state, "typed_continuation_state_invalid")
        context = _context_bundle(state)
        if context.unresolved_slots:
            # V1 reinjects PLAN slots only; a context slot cannot be resolved by
            # mutating the QueryPlan, so continuation fails closed here.
            return _continuation_stopped(state, "continuation_context_slot_unsupported")
        active_plan = _active_query_plan(state, stored)
        if active_plan is None:
            return _continuation_stopped(state, "typed_continuation_active_plan_invalid")
        plan = active_plan
        if _continuation_eligibility(
            stored,
            decision,
            token,
            plan,
            context,
            policy_version=resolved_plan_validator.policy_version,
            policy_checksum=resolved_plan_validator.policy_checksum,
        ):
            return _continuation_stopped(state, "typed_continuation_not_eligible")
        provider = _typed_query_plan_provider()
        bound = getattr(provider, "replan_with_slot_bindings", None)
        if not callable(bound):
            return _continuation_stopped(state, "slot_bound_replan_unavailable")
        identity = _request_identity()
        trace = _trace(state)
        try:
            new_plan = await cast(Any, bound)(
                question=_question(state),
                context=context,
                identity=identity,
                base_plan=active_plan,
                slot_bindings=decision.slot_bindings,
            )
        except Exception as exc:
            trace.record(
                "candidate",
                "slot_bound_replan_failed",
                error_type=type(exc).__name__,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return _continuation_stopped(state, "slot_bound_replan_failed")
        try:
            validation = resolved_plan_validator.validate_query_plan(
                plan=new_plan,
                context=context,
                identity=identity,
            )
        except Exception as exc:
            trace.record(
                "policy",
                "replanned_validation_failed",
                error_type=type(exc).__name__,
            )
            await _persist_new_events(trace_sink, trace.events[-1:])
            return _continuation_stopped(state, "replanned_validation_failed")
        trace.record(
            "policy",
            "slot_bound_replanned",
            old_plan_sha256=active_plan.checksum,
            new_plan_sha256=new_plan.checksum,
            outcome=validation.outcome,
        )
        await _persist_new_events(trace_sink, trace.events[-1:])
        lineage = _plan_lineage(stored, decision, new_plan, validation)
        prior = state.get("plan_lineage")
        base: dict[str, object] = {
            "resolved_plan": query_plan_payload(new_plan),
            "resolved_plan_validation": validation.model_dump(mode="json"),
            "plan_lineage": [*(prior if isinstance(prior, list) else []), lineage],
            "trace_events": _events(trace),
        }
        if validation.outcome == "allow":
            # CONTINUATION revalidates the CURRENT environment.  A past
            # authorization snapshot NEVER grants future permission, so the
            # run-bound snapshot is not reused for execution.
            if typed_runtime_factory is None:
                # No typed runtime is configured: continuation cannot be
                # proven, so it fails closed rather than executing.
                base.update(
                    {
                        "messages": [
                            AIMessage(
                                content=(
                                    "The clarification was applied and the plan "
                                    "revalidated, but this deployment has no "
                                    "typed runtime to revalidate continuation "
                                    "against. Nothing was executed."
                                )
                            )
                        ],
                        "pending_decision": None,
                        "decision_status": (
                            "resolved_pending_current_authorization"
                        ),
                        "needs_hitl": False,
                    }
                )
                return base
            current_authorization = _resolve_current_backend_authorization()
            if current_authorization is None:
                # Unavailable current authority: STOP.  HITL is never asked to
                # restore privilege and the old snapshot is never silently used.
                base.update(
                    {
                        "messages": [
                            AIMessage(
                                content=(
                                    "The clarification was applied and the plan "
                                    "revalidated, but current authorization is "
                                    "unavailable. Nothing was executed."
                                )
                            )
                        ],
                        "pending_decision": None,
                        "decision_status": "current_authorization_unavailable",
                        "stop_reason": "current_authorization_unavailable",
                        "needs_hitl": False,
                    }
                )
                return base
            continuation = await _continue_under_current_authorization(
                state=state,
                plan=new_plan,
                validation=validation,
                context=context,
                authorization=current_authorization,
                typed_runtime_factory=typed_runtime_factory,
                plan_compiler=resolved_plan_compiler,
                plan_validator=resolved_plan_validator,
                budget_policy=resolved_budget_policy,
                base=base,
            )
            return continuation
        if validation.outcome == "clarify":
            try:
                new_request = clarification_request(
                    validation=validation,
                    context=context,
                    plan=new_plan,
                    version=int(state.get("decision_version", 0)) + 1,
                )
            except Exception:
                return _continuation_stopped(
                    state, "replanned_clarification_unbounded"
                )
            base.update(
                {
                    "messages": [
                        AIMessage(
                            content=(
                                "The clarification was applied, but another "
                                "bounded clarification is required."
                            )
                        )
                    ],
                    "pending_decision": new_request.model_dump(mode="json"),
                    "decision_status": "awaiting_decision",
                    "decision_version": new_request.version,
                    "needs_hitl": False,
                }
            )
            return base
        base.update(
            {
                "messages": [
                    AIMessage(content="The replanned request was not permitted.")
                ],
                "pending_decision": None,
                "decision_status": "revalidation_denied",
                "stop_reason": "query_plan_validation_denied",
                "degradation_flags": _degradation_flags(
                    state, "ReplannedPlanDenied"
                ),
            }
        )
        return base

    def after_typed_runtime(state: V2EngineState) -> Literal["context", "__end__"]:
        return "__end__" if state.get("stop_reason") else "context"

    def after_context(state: V2EngineState) -> Literal["plan", "__end__"]:
        return "__end__" if state.get("stop_reason") else "plan"

    def after_plan(
        state: V2EngineState,
    ) -> Literal["validate", "mode_capability", "__end__"]:
        if state.get("mode_capability_pending"):
            return "mode_capability"
        if _effective_mode(state) == "BUILD" and state.get("response_blocks"):
            return "__end__"
        return "__end__" if state.get("stop_reason") else "validate"

    def after_validation(
        state: V2EngineState,
    ) -> Literal["route", "decision", "__end__"]:
        if state.get("stop_reason"):
            return "__end__"
        if state.get("decision_status") == "awaiting_decision" and state.get(
            "pending_decision"
        ):
            return "decision"
        return "route"

    def after_decision(state: V2EngineState) -> Literal["replan", "__end__"]:
        if state.get("decision_status") == "resolved_pending_revalidation":
            return "replan"
        return "__end__"

    def after_replan(
        state: V2EngineState,
    ) -> Literal["decision", "execute", "__end__"]:
        if state.get("decision_status") == "awaiting_decision" and state.get(
            "pending_decision"
        ):
            return "decision"
        # A continuation that revalidated under CURRENT authorization and
        # compiled an ALLOW ExecutionPlan continues to execution exactly once.
        if state.get("continuation_ready"):
            return "execute"
        return "__end__"

    def after_route(
        state: V2EngineState,
    ) -> Literal["compile", "model", "mode_capability", "__end__"]:
        if state.get("stop_reason"):
            return "__end__"
        # QUERY / Mode1 is a HARD zero-model capability, enforced by graph
        # routing - never by prompt instructions and never by RouteName. An
        # effective QUERY run may only take the deterministic compile path.
        mode = _effective_mode(state)
        if mode == "QUERY":
            if typed_pipeline_enabled:
                # Product mode is independent from the risk/budget route.  A
                # validated deterministic QUERY plan can execute under any
                # selected route without making a model call.
                return "compile"
            # No typed runtime is available, so QUERY must NOT fall through to
            # the model edge; emit the explicit capability suggestion instead.
            return "mode_capability"
        # ANALYZE is governed-fetch-first regardless of RouteName.  The mode
        # axis authorizes the analysis call only after typed execution.
        if mode == "ANALYZE" and typed_pipeline_enabled:
            return "compile"
        # Compatibility engines without a server run envelope historically use
        # the deterministic typed path. Preserve that zero-model behavior.
        if typed_pipeline_enabled and _route(state) == "fast":
            return "compile"
        if mode not in (None, "ANALYZE", "BUILD"):
            # FAIL CLOSED: an unrecognized effective mode never authorizes a
            # model call.
            return "__end__"
        # An absent envelope is the pre-mode compatibility path (engines built
        # without the run-envelope contract), which keeps its existing behavior.
        # The API layer always supplies an envelope for executable runs.
        return "model"

    def after_compile(state: V2EngineState) -> Literal["execute", "__end__"]:
        return "__end__" if state.get("stop_reason") else "execute"

    def after_execute(state: V2EngineState) -> Literal["analysis", "__end__"]:
        if state.get("stop_reason"):
            return "__end__"
        return "analysis" if _effective_mode(state) == "ANALYZE" else "__end__"

    def after_model(state: V2EngineState) -> Literal["hitl", "__end__"]:
        return "hitl" if state.get("needs_hitl") else "__end__"

    graph.add_node("receive", receive_node)
    if typed_runtime_factory is not None:
        graph.add_node("typed_runtime", typed_runtime_node)
    graph.add_node("context", context_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_node("route", route_node)
    graph.add_node("compile", compile_node)
    graph.add_node("mode_capability", mode_capability_node)
    graph.add_node("execute", execute_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("model", model_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("decision", decision_node)
    graph.add_node("replan", replan_node)
    graph.add_edge(START, "receive")
    if typed_runtime_factory is not None:
        graph.add_edge("receive", "typed_runtime")
        graph.add_conditional_edges(
            "typed_runtime",
            after_typed_runtime,
            {"context": "context", END: END},
        )
    else:
        graph.add_edge("receive", "context")
    graph.add_conditional_edges("context", after_context, {"plan": "plan", END: END})
    graph.add_conditional_edges(
        "plan",
        after_plan,
        {"validate": "validate", "mode_capability": "mode_capability", END: END},
    )
    graph.add_conditional_edges(
        "validate",
        after_validation,
        {"route": "route", "decision": "decision", END: END},
    )
    graph.add_conditional_edges(
        "route",
        after_route,
        {
            "compile": "compile",
            "model": "model",
            "mode_capability": "mode_capability",
            END: END,
        },
    )
    # The typed capability outcome is terminal for this run.
    graph.add_edge("mode_capability", END)
    graph.add_conditional_edges(
        "compile",
        after_compile,
        {"execute": "execute", END: END},
    )
    graph.add_conditional_edges(
        "execute",
        after_execute,
        {"analysis": "analysis", END: END},
    )
    graph.add_edge("analysis", END)
    graph.add_conditional_edges("model", after_model, {"hitl": "hitl", END: END})
    # Typed clarification decision node: suspends on interrupt() and records a
    # valid typed decision.  A resolve decision continues into deterministic
    # slot-bound replan + revalidation; compile/execute remains disabled and the
    # run stops at resolved_pending_current_authorization.
    graph.add_conditional_edges(
        "decision",
        after_decision,
        {"replan": "replan", END: END},
    )
    graph.add_conditional_edges(
        "replan",
        after_replan,
        {"decision": "decision", "execute": "execute", END: END},
    )
    compiled = graph.compile(checkpointer=checkpointer, name="nl2sql_v2_explicit")
    if typed_runtime_factory is not None:
        return _RequestScopedTypedEngine(compiled, typed_runtime_factory)
    return compiled


def _render_analysis_interpretation(
    *,
    source_facts: str,
    interpretation: AnalysisInterpretation,
) -> str:
    """Deterministically separate executed facts from model interpretation."""

    lines = [
        "SOURCE FACTS",
        source_facts,
        "",
        "MODEL INTERPRETATION",
        interpretation.summary.text,
    ]
    if interpretation.observations:
        lines.extend(("", "OBSERVATIONS"))
        lines.extend(f"- {item.text}" for item in interpretation.observations)
    if interpretation.caveats:
        lines.extend(("", "CAVEATS"))
        lines.extend(f"- {item.text}" for item in interpretation.caveats)
    return "\n".join(lines)


def _persisted_run_envelope(state: V2EngineState) -> dict[str, object] | None:
    """Validate and normalize the incoming run envelope for persistence.

    A malformed or absent envelope collapses to None, which the mode guard
    treats as "not QUERY" only because the caller also fails closed; the
    envelope is never inferred from the request.
    """

    raw = state.get("run_envelope")
    if isinstance(raw, RunEnvelope):
        return raw.model_dump(mode="json")
    if not isinstance(raw, dict):
        return None
    try:
        return RunEnvelope.model_validate(dict(raw)).model_dump(mode="json")
    except Exception:
        return None


def _effective_mode(state: V2EngineState) -> str | None:
    """The immutable effective ProductMode bound to THIS run, if any.

    Read from the server-owned RunEnvelope in run state.  This is a SEPARATE
    axis from RouteName: the route is execution/budget shape, the mode is the
    run capability envelope.
    """

    raw = state.get("run_envelope")
    if not isinstance(raw, dict):
        return None
    mode = raw.get("effective_mode")
    return mode if isinstance(mode, str) else None


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


def _run_owner_user_id() -> str | None:
    try:
        return _request_identity().user_id
    except Exception:
        return None


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


def _resolve_run_authorization() -> tuple[dict[str, object] | None, str | None]:
    """The ONE authority read of the engine: resolve ONCE at run start.

    Authorization is read from the EXISTING trusted configurable carrier exactly
    here, at run start, and bound to the run.  No later node re-reads the
    configurable for authority; suspension/resume restore the run-bound snapshot
    from run state.  The revision is stamped ONLY on an explicit trusted ALLOW.
    """

    authorization = authorization_context_from_config(_runtime_configurable())
    if not isinstance(authorization, AuthorizationContext):
        return None, None
    snapshot = authorization.model_dump(mode="json")
    decision = evaluate_authorization(authorization, expected_revision=None)
    revision = decision.authorization_revision if decision.outcome == "allow" else None
    return snapshot, revision


async def _continue_under_current_authorization(
    *,
    state: V2EngineState,
    plan: QueryPlan,
    validation: PlanValidationRecord,
    context: ContextBundle,
    authorization: AuthorizationContext,
    typed_runtime_factory: TypedRuntimeFactory,
    plan_compiler: PlanCompiler,
    plan_validator: PlanValidator,
    budget_policy: RoutingBudgetPolicy,
    base: dict[str, object],
) -> dict[str, object]:
    """Revalidate the replanned request against CURRENT authorization, then run.

    The suspended business state is restored from checkpoint state; the
    ENVIRONMENT is revalidated fresh.  The continuation compiles and validates
    an ExecutionPlan under the CURRENT snapshot and executes exactly once.  A
    changed authorization revision is NOT itself a denial: if the new trusted
    snapshot still legitimately authorizes the request, continuation proceeds
    and the execution receipt carries the CURRENT revision.

    Anything that cannot be proven stops before execution.
    """

    runtime = state.get("typed_runtime")
    if not isinstance(runtime, dict):
        return _continuation_stopped(state, "current_authorization_runtime_missing")
    request_identity = runtime.get("request_identity")
    try:
        identity = RequestIdentity.model_validate(request_identity)
    except Exception:
        return _continuation_stopped(state, "current_authorization_identity_invalid")

    # Revalidate under the CURRENT snapshot through the SAME request-scoped
    # factory the initial request used.  The factory itself fails closed on
    # authorization_context_missing / authorization_denied.
    rebuilt = await typed_runtime_factory(
        identity=identity,
        authorization=authorization,
        expected_revision=None,
    )
    if isinstance(rebuilt, TypedRuntimeUnavailable):
        base.update(
            {
                "messages": [
                    AIMessage(
                        content=(
                            "The clarification was applied and the plan "
                            "revalidated, but current authorization does not "
                            "permit continuation. Nothing was executed."
                        )
                    )
                ],
                "pending_decision": None,
                "decision_status": "current_authorization_denied",
                "stop_reason": rebuilt.reason,
                "needs_hitl": False,
            }
        )
        return base

    # The CURRENT revision is what the execution evidence must carry.
    base["authorization_context"] = authorization.model_dump(mode="json")
    base["authorization_revision"] = authorization.authorization_revision
    base["pending_decision"] = None
    base["decision_status"] = "resolved_continuing_current_authorization"
    base["needs_hitl"] = False
    # Compile + validate the ExecutionPlan under the CURRENT snapshot, exactly
    # as the normal compile path does.  Nothing from the client participates.
    try:
        execution_plan = plan_compiler.compile(
            plan=plan,
            context=context,
            validation=validation,
        )
        execution_validation = plan_validator.validate_execution_plan(
            execution_plan=execution_plan,
            query_plan=plan,
            context=context,
            route_budget=_route_budget_from_state(
                state,
                route=_route(state),
                policy=budget_policy,
            ).limits,
        )
    except Exception:
        return _continuation_stopped(state, "continuation_compilation_failed")
    if execution_validation.outcome != "allow":
        base.update(
            {
                "messages": [
                    AIMessage(
                        content=(
                            "The clarification was applied and the plan "
                            "revalidated, but the resulting execution plan was "
                            "not permitted. Nothing was executed."
                        )
                    )
                ],
                "pending_decision": None,
                "decision_status": "continuation_execution_plan_denied",
                "stop_reason": "continuation_execution_plan_denied",
                "needs_hitl": False,
            }
        )
        return base
    base["execution_plan"] = execution_plan.model_dump(mode="json")
    base["execution_plan_validation"] = execution_validation.model_dump(mode="json")
    base["continuation_ready"] = True
    return base


def _resolve_current_backend_authorization() -> AuthorizationContext | None:
    """Resolve the CURRENT trusted Backend authorization at CONTINUATION time.

    This is deliberately DIFFERENT from ``_bound_authorization_context``: that
    one restores the snapshot bound to the run at start and is the frozen V1
    mid-run model.  A resumed run must instead be revalidated against the
    CURRENT environment, because a past authorization snapshot never grants
    future permission.

    The client never supplies this.  It is read from the SAME trusted
    server-side configurable carrier the engine already uses at run start, which
    in production is populated by the Backend provider - never by a request
    body.  Absence collapses to None, which the caller treats as a stop.
    """

    authorization = authorization_context_from_config(_runtime_configurable())
    return authorization if isinstance(authorization, AuthorizationContext) else None


def _bound_authorization_revision(state: V2EngineState) -> str | None:
    raw = state.get("authorization_revision")
    return raw if isinstance(raw, str) and raw.strip() else None


def _bound_authorization_context(state: V2EngineState) -> AuthorizationContext | None:
    """Restore the authorization snapshot BOUND TO THIS RUN from run state.

    This is the frozen V1 model: authorization is resolved once at run start and
    restored from the existing checkpoint/run state afterwards.  It is NEVER
    re-fetched from a newly supplied configurable during the same run, so a
    permission change mid-run is intentionally not applied.
    """

    raw = state.get("authorization_context")
    if isinstance(raw, AuthorizationContext):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return AuthorizationContext.model_validate_json(json.dumps(dict(raw)))
    except Exception:
        return None


def _authorization_from_payload(raw: object) -> AuthorizationContext | None:
    """Parse an OPTIONALLY supplied run-binding snapshot from a resume payload.

    Absent, wrong-typed or malformed payloads collapse to None exactly like an
    absent carrier, which means "restore the run-bound snapshot".
    """

    if isinstance(raw, AuthorizationContext):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return AuthorizationContext.model_validate_json(json.dumps(dict(raw)))
    except Exception:
        return None


def authorization_run_binding_failure(
    state: V2EngineState,
    supplied: AuthorizationContext | None,
) -> str | None:
    """Return a fail-closed reason for a RUN-BINDING mismatch, else None.

    * No snapshot was bound to this run -> the authorization-free path is
      unchanged.
    * No snapshot is supplied on resume -> RESTORE the run-bound snapshot; the
      current configurable is deliberately NOT consulted, so an in-flight run
      keeps its original authority.
    * A supplied snapshot differs from the run-bound one -> RUN-BINDING
      MISMATCH (run-consistency failure, NOT a live revocation).

    The comparison target is always the run-bound snapshot restored from run
    state, never a newly fetched Backend revision.
    """

    bound = _bound_authorization_context(state)
    bound_revision = _bound_authorization_revision(state)
    if bound is None and bound_revision is None:
        return None
    if bound is None:
        # A revision was bound but its snapshot is gone from run state: fail
        # closed rather than continue without the run's authority.
        return "authorization_run_binding_mismatch"
    if supplied is None:
        # RESTORE the run-bound snapshot; the current configurable is ignored.
        return None
    if (
        supplied.authorization_revision != bound.authorization_revision
        or supplied != bound
    ):
        return "authorization_run_binding_mismatch"
    return None


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


# --- typed clarification decision helpers ------------------------------------

_TYPED_DECISION_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"action", "slot_bindings", "idempotency_key", "request_id", "request_version"}
)

_TYPED_DECISION_STATUS: Final[dict[str, str]] = {
    "resolve": "resolved_pending_revalidation",
    # choose never auto-continues in V1: it is recorded but must not compile.
    "choose": "choose_recorded_no_continuation",
    "reject": "rejected",
    "cancel": "cancelled",
}


def _pending_decision(state: V2EngineState) -> HITLRequest | None:
    """Restore + revalidate the server-stored pending request from run state."""

    raw = state.get("pending_decision")
    if not isinstance(raw, dict):
        return None
    try:
        return revalidate_request(raw)
    except Exception:
        return None


def _typed_decision_from_payload(
    stored: HITLRequest, payload: object
) -> tuple[HITLDecision | None, str | None]:
    """Revalidate a raw resume payload as a decision bound to the stored request.

    Request identity is SERVER-OWNED: the stored request_id/version are injected
    and any client-supplied values must match.  Authority/unknown fields are
    rejected before validation so a forged payload cannot escalate.
    """

    if not isinstance(payload, Mapping):
        return None, "typed_decision_payload_invalid"
    keys = set(payload)
    if keys & FORBIDDEN_DECISION_FIELDS:
        return None, "typed_decision_authority_field_rejected"
    if keys - _TYPED_DECISION_PAYLOAD_KEYS:
        return None, "typed_decision_unknown_field"
    supplied_request_id = payload.get("request_id")
    if supplied_request_id is not None and supplied_request_id != stored.request_id:
        return None, "typed_decision_request_id_mismatch"
    supplied_version = payload.get("request_version")
    if supplied_version is not None and supplied_version != stored.version:
        return None, "typed_decision_request_version_mismatch"
    normalized = {
        "request_id": stored.request_id,
        "request_version": stored.version,
        "action": payload.get("action"),
        "slot_bindings": payload.get("slot_bindings", ()),
        "idempotency_key": payload.get("idempotency_key"),
    }
    try:
        decision = revalidate_decision(normalized)
    except Exception:
        return None, "typed_decision_payload_invalid"
    failures = decision.validate_against(stored)
    if failures:
        return None, "typed_decision_rejected:" + ",".join(failures)
    return decision, None


def _typed_decision_ledger_entry(
    state: V2EngineState, idempotency_key: str
) -> dict[str, object] | None:
    ledger = state.get("decision_ledger")
    if not isinstance(ledger, dict):
        return None
    entry = ledger.get(idempotency_key)
    return entry if isinstance(entry, dict) else None


def _typed_decision_replays(
    entry: Mapping[str, object], stored: HITLRequest, decision: HITLDecision
) -> bool:
    token = build_resume_token(request=stored, decision=decision)
    return (
        entry.get("request_id") == stored.request_id
        and entry.get("request_version") == stored.version
        and entry.get("request_checksum") == stored.checksum
        and entry.get("decision_checksum") == decision.checksum
        and entry.get("resume_token_checksum") == token.checksum
    )


def _typed_decision_replay_result(
    state: V2EngineState, entry: Mapping[str, object], decision: HITLDecision
) -> dict[str, object]:
    # A checkpoint-restored ledger entry is revalidated through the normal
    # Pydantic boundary AND re-derived: the stored decision/token content must
    # match the incoming decision and the recorded checksums.
    try:
        stored_decision = revalidate_decision(entry.get("decision"))
        token = revalidate_resume_token(entry.get("resume_token"))
    except Exception:
        return _typed_decision_ledger_invalid(state)
    if (
        stored_decision.checksum != decision.checksum
        or token.checksum != entry.get("resume_token_checksum")
    ):
        return _typed_decision_ledger_invalid(state)
    # The recorded status is DERIVED from the revalidated decision action, never
    # copied from the untrusted checkpoint entry.
    status = _TYPED_DECISION_STATUS.get(
        stored_decision.action, "recorded_no_continuation"
    )
    ledger = state.get("decision_ledger")
    return {
        "messages": [AIMessage(content="The typed decision was already recorded.")],
        "decision_status": status,
        "resolved_decision": stored_decision.model_dump(mode="json"),
        "resume_token": token.model_dump(mode="json"),
        "decision_ledger": dict(ledger) if isinstance(ledger, dict) else {},
        "needs_hitl": False,
    }


def _typed_decision_ledger_invalid(state: V2EngineState) -> dict[str, object]:
    return {
        "messages": [
            AIMessage(content="The recorded decision could not be revalidated.")
        ],
        "stop_reason": "typed_decision_ledger_invalid",
        "decision_status": "invalid",
        "degradation_flags": _degradation_flags(state, "TypedDecisionLedgerInvalid"),
    }


def _record_typed_decision(
    state: V2EngineState, stored: HITLRequest, decision: HITLDecision
) -> dict[str, object]:
    """Record a valid decision; the caller decides whether continuation runs."""

    token = build_resume_token(request=stored, decision=decision)
    status = _TYPED_DECISION_STATUS.get(decision.action, "recorded_no_continuation")
    ledger = dict(state.get("decision_ledger", {}))
    ledger[decision.idempotency_key] = {
        "request_id": stored.request_id,
        "request_version": stored.version,
        "request_checksum": stored.checksum,
        "decision_checksum": decision.checksum,
        "resume_token_checksum": token.checksum,
        "status": status,
        "request": stored.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "resume_token": token.model_dump(mode="json"),
    }
    result: dict[str, object] = {
        "messages": [
            AIMessage(
                content=(
                    "The typed decision was recorded. No business work was "
                    "executed."
                )
            )
        ],
        "decision_status": status,
        "resolved_decision": decision.model_dump(mode="json"),
        "resume_token": token.model_dump(mode="json"),
        "decision_ledger": ledger,
        "needs_hitl": False,
    }
    if status in ("rejected", "cancelled"):
        # A terminal decision must not leave an apparently active pending request.
        result["pending_decision"] = None
    return result


def _resolved_decision(state: V2EngineState) -> HITLDecision | None:
    raw = state.get("resolved_decision")
    if not isinstance(raw, dict):
        return None
    try:
        return revalidate_decision(raw)
    except Exception:
        return None


def _stored_resume_token(state: V2EngineState) -> ResumeToken | None:
    raw = state.get("resume_token")
    if not isinstance(raw, dict):
        return None
    try:
        return revalidate_resume_token(raw)
    except Exception:
        return None


_V1_CONTINUABLE_SLOTS: Final[frozenset[str]] = frozenset({"time", "grain"})


def _active_query_plan(
    state: V2EngineState, pending_request: HITLRequest
) -> QueryPlan | None:
    """Restore the CURRENT ACTIVE plan for one suspended typed request.

    The active plan is the latest successfully replanned plan when its checksum
    matches the pending request, else the original query_plan.  A malformed or
    tampered resolved_plan fails closed and never silently falls back to an
    unrelated old plan; no client-supplied plan or hash participates.
    """

    resolved_raw = state.get("resolved_plan")
    if isinstance(resolved_raw, dict):
        try:
            resolved = QueryPlan.model_validate(resolved_raw)
        except Exception:
            return None
        if resolved.checksum == pending_request.plan_sha256:
            return resolved
    original_raw = state.get("query_plan")
    if isinstance(original_raw, dict):
        try:
            original = QueryPlan.model_validate(original_raw)
        except Exception:
            return None
        if original.checksum == pending_request.plan_sha256:
            return original
    return None


def _continuation_eligibility(
    stored: HITLRequest,
    decision: HITLDecision,
    token: ResumeToken,
    plan: QueryPlan,
    context: ContextBundle,
    *,
    policy_version: str,
    policy_checksum: str,
) -> tuple[str, ...]:
    """V1 continuation eligibility: resolve-only, user-source, time/grain slots.

    The suspended request is re-verified against the RESTORED plan/context, so a
    tampered or stale checkpoint request cannot drive the replan.
    """

    failures: list[str] = []
    if stored.plan_sha256 != plan.checksum:
        failures.append("continuation_plan_mismatch")
    if stored.context_checksum != context.checksum:
        failures.append("continuation_context_mismatch")
    if (
        stored.policy_version != policy_version
        or stored.policy_checksum != policy_checksum
    ):
        failures.append("continuation_policy_mismatch")
    if decision.action != "resolve":
        failures.append("continuation_action_unsupported")
    if decision.validate_against(stored):
        failures.append("continuation_decision_mismatch")
    if token.validate_against(stored, decision):
        failures.append("continuation_token_mismatch")
    if any(binding.source != "user" for binding in decision.slot_bindings):
        failures.append("continuation_slot_source_not_user")
    if not stored.unresolved_slots or not (
        set(stored.unresolved_slots) <= _V1_CONTINUABLE_SLOTS
    ):
        failures.append("continuation_slot_unsupported")
    if set(stored.unresolved_slots) != set(plan.unresolved_slots):
        failures.append("continuation_slot_scope_mismatch")
    return tuple(dict.fromkeys(failures))


def _plan_lineage(
    stored: HITLRequest,
    decision: HITLDecision,
    new_plan: QueryPlan,
    validation: PlanValidationRecord,
) -> dict[str, object]:
    return {
        "old_plan_sha256": stored.plan_sha256,
        "request_id": stored.request_id,
        "request_version": stored.version,
        "request_checksum": stored.checksum,
        "decision_checksum": decision.checksum,
        "slot_bindings": [
            item.model_dump(mode="json") for item in decision.slot_bindings
        ],
        "new_plan_sha256": new_plan.checksum,
        "new_validation_outcome": validation.outcome,
        "new_validation_query_plan_sha256": validation.query_plan_sha256,
        "new_validation_context_checksum": validation.context_checksum,
    }


def _continuation_stopped(state: V2EngineState, reason: str) -> dict[str, object]:
    return {
        "messages": [
            AIMessage(
                content=(
                    "The typed decision was recorded, but slot-bound "
                    "continuation is not available."
                )
            )
        ],
        "pending_decision": None,
        "decision_status": "continuation_stopped",
        "stop_reason": reason,
        "degradation_flags": _degradation_flags(state, "TypedContinuationStopped"),
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
        # V1 sensitivity: ordinary authorized business data inside the caller's
        # effective organization scope is default-allowed for Agent users, so
        # there is no second business-field sensitivity gate.  This routing
        # signal stays non-authoritative, is NEVER derived from scope_level, and
        # no data access decision may depend on it.
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
