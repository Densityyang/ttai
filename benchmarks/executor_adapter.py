"""S3 executor adapter: structured typed run -> TypedAnswerReceipt, zero parsing.

Why this module exists
----------------------
Before S3 the only reachable real execution path was the legacy text bridge in
`benchmarks/agent_bridge.py`, which pulled numbers out of natural-language
answers with a regex.  That is exactly the "wiring exists but the path is not
reachable" trap P9A must not repeat.

This adapter consumes STRUCTURED run products only: the checkpoint-safe engine
state keys (`execution_plan`, `execution_record`, `grounded_answer_artifact`,
`pending_decision`, `model_receipt`, ...) and the typed contracts behind them.
It never reads `grounded_answer_text`, `messages` or `response_blocks`,
because those carry rendered prose.  There is no regular expression anywhere in
this module.

Honesty rules encoded here
--------------------------
* A run whose typed runtime was unavailable, or whose only structured outcome is
  an internal stop reason, yields NO receipt.  The runner turns a missing
  receipt into PROVENANCE_FAILURE; the adapter must never manufacture one.
* A present-but-malformed structured product raises TypedRunProductError, and
  the adapter fails closed (no receipt) rather than guessing.
* No confidence/candidate score is invented: there is no deterministic producer
  for one, so it stays absent.

Adaptation seam
---------------
`product_from_engine_state` is the ONLY place that knows the src contract
shapes.  If `src/nl2sql/orchestration/typed_runtime.py` or the contract field
names move, this one function is what changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, cast

from benchmarks.adapters import BenchmarkCase, derive_mode
from benchmarks.registry import EvalCase, canonical_json, sha256_hex
from benchmarks.typed_receipts import ProviderCallReceipt, TypedAnswerReceipt

# Selection hands the runner EvalCase objects; the legacy consumers hand it
# BenchmarkCase.  The invoker accepts both and never re-derives a mode that the
# registry already fixed.
RunCase = BenchmarkCase | EvalCase

AnswerType = Literal["answer", "clarification", "rejected", "hitl"]
PolicyOutcome = Literal["allow", "deny", "approval"]

EVIDENCE_HARNESS: Final[str] = "harness"
EVIDENCE_UNAVAILABLE: Final[str] = "typed_runtime_unavailable"
# A benchmark process holds no trusted Backend AuthorizationContext, and the
# production factory fails closed on exactly that first gate.
PRODUCTION_UNAVAILABLE_REASON: Final[str] = "authorization_context_missing"

# The engine state keys this adapter is ALLOWED to read.  Prose keys are absent
# by construction; the test suite asserts they are never touched.
STRUCTURED_STATE_KEYS: Final[tuple[str, ...]] = (
    "run_envelope",
    "execution_plan",
    "execution_plan_validation",
    "execution_record",
    "grounded_answer_artifact",
    "model_receipt",
    "pending_decision",
    "needs_hitl",
    "hitl_status",
    "mode_capability_outcome",
    "typed_runtime_unavailable_reason",
)
# Rendered-text keys the adapter must never read.  Listed so a tripwire test can
# prove the adapter is indifferent to them.
TEXT_STATE_KEYS: Final[tuple[str, ...]] = (
    "messages",
    "grounded_answer_text",
    "response_blocks",
)

# The repository's DecisionKind vocabulary (decision_contract.DecisionKind).
# Only a human confirmation suspends as HITL; a bounded clarification does not.
_HITL_DECISION_KINDS: Final[frozenset[str]] = frozenset(
    {"business_confirmation", "risk_policy_decision"}
)
_HITL_STATUSES: Final[frozenset[str]] = frozenset({"pending", "awaiting_confirmation"})


class TypedRunProductError(RuntimeError):
    """A structured run product was present but could not be trusted."""


@dataclass(frozen=True)
class TypedRunProduct:
    """The structured facts one typed run proved, before receipt shaping."""

    trace_id: str
    answer_type: AnswerType
    policy_outcome: PolicyOutcome
    execution_accepted: bool
    execution_row_count: int
    answer_hash: str
    rowset_sha256: str | None
    candidate_score: float | None
    sql_fingerprint: str | None
    confirmed_plan_checksum: str | None
    observed_plan_checksum: str | None
    expected_row_count: int | None
    result_value: Any
    result_value_sha256: str | None
    model_calls: tuple[ProviderCallReceipt, ...] = ()
    evidence_fields: tuple[str, ...] = ()


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return None


def _validate(model: Any, payload: Mapping[str, Any], label: str) -> Any:
    try:
        return model.model_validate(dict(payload))
    except Exception as exc:  # noqa: BLE001 - fail closed with a stable error
        raise TypedRunProductError(f"invalid structured product: {label}") from exc


def _usage(receipt: Any, *names: str) -> int:
    usage = getattr(receipt, "usage", None)
    if not isinstance(usage, Mapping):
        return 0
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _rowset_sha256(record: Any) -> str | None:
    digests = sorted(
        {
            receipt.rowset_sha256
            for receipt in record.step_receipts
            if receipt.status == "succeeded" and receipt.rowset_sha256
        }
    )
    if not digests:
        return None
    if len(digests) == 1:
        return digests[0]
    return sha256_hex(canonical_json(digests))


def _grounded_values(artifact: Any) -> list[Any]:
    return [
        fact.value
        for fact in artifact.facts
        if fact.status == "grounded" and fact.value is not None
    ]


def product_from_engine_state(state: Mapping[str, Any]) -> TypedRunProduct | None:
    """Project a checkpoint-safe engine state into a typed run product.

    Returns None when the run proved no structured answer terminal (runtime
    unavailable, internal stop reason, no grounded artifact).  Raises
    TypedRunProductError when a structured product is present but malformed.
    This is the isolated src-contract adaptation point.
    """
    # Lazy imports: the adapter stays importable without src, and the coupling
    # surface is exactly these contract models.
    from src.nl2sql.contracts import (
        AnswerArtifact,
        ExecutionPlan,
        ModelReceipt,
        PlanExecutionRecord,
        PlanValidationRecord,
    )

    envelope = _as_mapping(state.get("run_envelope"))
    trace_id = str(envelope.get("run_id") or "") if envelope is not None else ""
    if not trace_id:
        raise TypedRunProductError("engine state carries no run envelope run_id")
    if state.get("typed_runtime_unavailable_reason"):
        # The typed runtime never composed: there is nothing to receipt.
        return None

    plan_payload = _as_mapping(state.get("execution_plan"))
    validation_payload = _as_mapping(state.get("execution_plan_validation"))
    record_payload = _as_mapping(state.get("execution_record"))
    artifact_payload = _as_mapping(state.get("grounded_answer_artifact"))
    decision = _as_mapping(state.get("pending_decision"))
    capability = _as_mapping(state.get("mode_capability_outcome"))
    model_payload = _as_mapping(state.get("model_receipt"))

    plan = (
        _validate(ExecutionPlan, plan_payload, "execution_plan")
        if plan_payload is not None
        else None
    )
    validation = (
        _validate(PlanValidationRecord, validation_payload, "execution_plan_validation")
        if validation_payload is not None
        else None
    )
    record = (
        _validate(PlanExecutionRecord, record_payload, "execution_record")
        if record_payload is not None
        else None
    )
    artifact = (
        _validate(AnswerArtifact, artifact_payload, "grounded_answer_artifact")
        if artifact_payload is not None
        else None
    )
    model_receipt = (
        _validate(ModelReceipt, model_payload, "model_receipt")
        if model_payload is not None
        else None
    )

    grounded = bool(artifact is not None and _grounded_values(artifact))
    awaiting = state.get("needs_hitl") is True or str(
        state.get("hitl_status") or ""
    ) in _HITL_STATUSES

    # Terminal derivation is purely structural, in precedence order.
    if validation is not None and validation.outcome == "deny":
        answer_type: AnswerType = "rejected"
    elif decision is not None:
        kind = str(decision.get("decision_kind") or "")
        answer_type = "hitl" if kind in _HITL_DECISION_KINDS else "clarification"
    elif awaiting:
        answer_type = "hitl"
    elif artifact is not None and record is not None and record.status == "succeeded" and grounded:
        answer_type = "answer"
    elif capability is not None:
        answer_type = "clarification"
    else:
        # An internal stop reason (plan proposal failed, execution failed) is
        # not an answer terminal; no receipt may be invented for it.
        return None

    policy_outcome: PolicyOutcome = (
        "deny" if answer_type == "rejected"
        else "approval" if answer_type == "hitl"
        else "allow"
    )
    execution_accepted = (
        answer_type == "answer" and record is not None and record.status == "succeeded"
    )

    if answer_type == "answer" and artifact is not None:
        values = _grounded_values(artifact)
        result_value: Any = values[0] if len(values) == 1 else values
    else:
        result_value = None
    result_value_sha256 = (
        sha256_hex(canonical_json(result_value)) if result_value is not None else None
    )

    facts_payload = (
        [fact.model_dump(mode="json") for fact in artifact.facts]
        if artifact is not None
        else []
    )
    answer_hash = sha256_hex(
        canonical_json(
            {
                "answer_type": answer_type,
                "facts": facts_payload,
                "blocks": list(artifact.blocks) if artifact is not None else [],
                "capability_outcome": dict(capability) if capability is not None else None,
                "decision_kind": (
                    str(decision.get("decision_kind") or "")
                    if decision is not None
                    else None
                ),
            }
        )
    )

    model_calls: tuple[ProviderCallReceipt, ...] = ()
    if model_receipt is not None:
        model_calls = (
            ProviderCallReceipt(
                alias=model_receipt.alias,
                stage=cast("Any", model_receipt.stage),
                resolved_model=model_receipt.resolved_model,
                input_tokens=_usage(model_receipt, "input_tokens", "prompt_tokens"),
                output_tokens=_usage(model_receipt, "output_tokens", "completion_tokens"),
                estimated_cost=float(model_receipt.estimated_cost),
                latency_ms=float(model_receipt.latency_ms),
                fallback_used=bool(model_receipt.fallback_used),
            ),
        )

    evidence_fields = tuple(
        key for key in STRUCTURED_STATE_KEYS if state.get(key) is not None
    )
    return TypedRunProduct(
        trace_id=trace_id,
        answer_type=answer_type,
        policy_outcome=policy_outcome,
        execution_accepted=execution_accepted,
        execution_row_count=0,
        answer_hash=answer_hash,
        rowset_sha256=_rowset_sha256(record) if record is not None else None,
        candidate_score=None,
        sql_fingerprint=None,
        confirmed_plan_checksum=plan.checksum if plan is not None else None,
        observed_plan_checksum=(
            record.execution_plan_checksum
            if record is not None
            else (plan.checksum if plan is not None else None)
        ),
        expected_row_count=None,
        result_value=result_value,
        result_value_sha256=result_value_sha256,
        model_calls=model_calls,
        evidence_fields=evidence_fields,
    )


def build_receipt(product: TypedRunProduct) -> TypedAnswerReceipt:
    """Shape a typed run product into the receipt the evaluator adjudicates."""
    return TypedAnswerReceipt(
        trace_id=product.trace_id,
        answer_type=product.answer_type,
        answer_hash=product.answer_hash,
        rowset_sha256=product.rowset_sha256,
        candidate_score=product.candidate_score,
        policy_outcome=product.policy_outcome,
        execution_accepted=product.execution_accepted,
        execution_row_count=product.execution_row_count,
        model_calls=product.model_calls,
        sql_fingerprint=product.sql_fingerprint,
        confirmed_plan_checksum=product.confirmed_plan_checksum,
        observed_plan_checksum=product.observed_plan_checksum,
        expected_row_count=product.expected_row_count,
        result_value=product.result_value,
        result_value_sha256=product.result_value_sha256,
    )


class TypedRunInvoker(Protocol):
    """One typed run -> its structured engine state, or None when it did not run."""

    evidence_kind: str

    async def __call__(self, case: RunCase) -> Mapping[str, Any] | None: ...


@dataclass
class ExecutorAdapter:
    """TypedExecutor built from a structured-run invoker.  Obeys the S3 contract."""

    invoker: TypedRunInvoker
    evidence_kind: str = EVIDENCE_HARNESS
    unavailable_reason: str = ""

    async def __call__(self, case: RunCase) -> TypedAnswerReceipt | None:
        state = await self.invoker(case)
        if state is None:
            return None
        try:
            product = product_from_engine_state(state)
        except TypedRunProductError:
            return None
        if product is None:
            return None
        return build_receipt(product)


@dataclass
class UnavailableTypedExecutor:
    """Fail-closed executor: no structured run exists, so no receipt is produced.

    Every case therefore reaches the evaluator as receipt_required=True with no
    receipt, which is PROVENANCE_FAILURE -- never a silent pass.
    """

    reason: str = PRODUCTION_UNAVAILABLE_REASON
    evidence_kind: str = EVIDENCE_UNAVAILABLE

    async def __call__(self, case: RunCase) -> TypedAnswerReceipt | None:
        del case
        return None


# ── the reachable typed driver: the real engine over the demo fixture runtime ──


class _NoModelGateway:
    """A benchmark gateway that REFUSES to fabricate a model response.

    QUERY/BUILD deterministic runs never call it.  An ANALYZE case that needs a
    model therefore fails closed instead of receiving invented content.
    """

    def __init__(self) -> None:
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.calls += 1
        raise RuntimeError("benchmark_model_gateway_disabled")

    async def invoke_structured(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.calls += 1
        raise RuntimeError("benchmark_model_gateway_disabled")


@dataclass
class DemoEngineInvoker:
    """Drive the REAL engine graph over the demo fixture typed runtime.

    This is a harness/fake-provider path: it proves the typed execution and
    grounding pipeline is reachable and that its structured products flow into
    the evaluator.  It establishes NO real accuracy and is labelled accordingly.
    """

    user_id: str = "demo-analyst"
    evidence_kind: str = EVIDENCE_HARNESS
    _engine: Any = field(default=None, init=False, repr=False)
    _identity: Any = field(default=None, init=False, repr=False)
    _authorization: Any = field(default=None, init=False, repr=False)

    async def _ensure(self) -> tuple[Any, Any, Any]:
        if self._engine is not None:
            return self._engine, self._identity, self._authorization

        from uuid import UUID

        from langgraph.checkpoint.memory import MemorySaver

        from src.core.auth.demo_provider import DemoBackendAuthorizationProvider
        from src.core.auth.types import AuthUser
        from src.nl2sql.contracts import RequestIdentity
        from src.nl2sql.demo.runtime import build_demo_runtime
        from src.nl2sql.orchestration.engine import create_v2_engine
        from src.nl2sql.orchestration.typed_runtime import TypedRuntimeUnavailable

        authorization = await DemoBackendAuthorizationProvider().load(
            AuthUser(user_id=self.user_id, telephone=None, roles=[], permissions=[])
        )
        if authorization is None:
            raise RuntimeError("benchmark demo identity is not a fixture")
        identity = RequestIdentity(request_id=UUID(int=0), user_id=self.user_id)

        async def factory(
            *,
            identity: Any,
            authorization: Any,
            expected_revision: Any,
            capabilities: Any = frozenset(),
        ) -> Any:
            del capabilities
            if authorization is None:
                return TypedRuntimeUnavailable("authorization_context_missing")
            runtime = build_demo_runtime(
                identity=identity,
                authorization=authorization,
                expected_revision=expected_revision,
            )
            if runtime is None:
                return TypedRuntimeUnavailable("demo_identity_not_fixture")
            return runtime

        self._engine = create_v2_engine(
            checkpointer=MemorySaver(),
            # The benchmark stub deliberately refuses every model call; it is
            # structurally a ModelGateway but is not the production class.
            model_gateway=cast("Any", _NoModelGateway()),
            typed_runtime_factory=factory,
        )
        self._identity = identity
        self._authorization = authorization
        return self._engine, identity, authorization

    async def __call__(self, case: RunCase) -> Mapping[str, Any] | None:
        from uuid import uuid4

        from src.nl2sql.contracts import RequestContext
        from src.nl2sql.orchestration.mode_contract import (
            RunEnvelope,
            resolve_requested_mode,
        )
        from src.nl2sql.ownership import runtime_config

        engine, identity, authorization = await self._ensure()
        # An EvalCase already carries its reviewed mode; only a legacy
        # BenchmarkCase needs it derived.
        mode = case.mode if isinstance(case, EvalCase) else derive_mode(case)
        envelope = RunEnvelope(
            run_id=uuid4().hex,
            requested_mode=cast("Any", mode),
            effective_mode=resolve_requested_mode(cast("Any", mode)),
        )
        context = RequestContext(
            identity=identity,
            thread_id=uuid4(),
            trace_id=f"bench-{case.case_id}",
            authorization=authorization,
        )
        config: dict[str, Any] = dict(runtime_config(context))
        config["recursion_limit"] = 50
        graph_input: dict[str, Any] = {
            "messages": [{"role": "user", "content": case.question}],
            "run_envelope": envelope.model_dump(mode="json"),
        }
        try:
            state = await engine.ainvoke(graph_input, config)
        except Exception:  # noqa: BLE001 - no structured run => no receipt
            return None
        return cast("Mapping[str, Any]", state) if isinstance(state, Mapping) else None


def build_demo_engine_executor(*, user_id: str = "demo-analyst") -> ExecutorAdapter:
    """The reachable harness executor over the real engine + demo runtime."""
    invoker = DemoEngineInvoker(user_id=user_id)
    return ExecutorAdapter(invoker=invoker, evidence_kind=EVIDENCE_HARNESS)


async def _never_active_release() -> Any:
    return None


async def _never_snapshot(snapshot_id: str) -> Any:
    del snapshot_id
    return None


def production_gate_reason() -> str:
    """Read-only probe of the production factory's FIRST gate.

    A benchmark process holds no trusted Backend AuthorizationContext, so this
    calls the REAL entry point with authorization=None and reports the reason it
    actually returned.  The factory returns before touching any deployment
    input, so no database, gateway or semantic release is constructed.  This is
    an isolated adaptation point: if the signature moves, the probe degrades to
    the documented reason instead of breaking the run.
    """
    import asyncio
    from uuid import UUID

    try:
        from src.nl2sql.contracts import RequestIdentity
        from src.nl2sql.orchestration.typed_runtime import (
            TypedRuntimeUnavailable,
            build_request_typed_runtime,
        )
    except Exception:  # noqa: BLE001 - a moved module must not break the CLI
        return PRODUCTION_UNAVAILABLE_REASON

    async def _probe() -> Any:
        return await build_request_typed_runtime(
            views=cast("Any", None),
            read_active=_never_active_release,
            read_snapshot=_never_snapshot,
            gateway=cast("Any", None),
            identity=RequestIdentity(request_id=UUID(int=0), user_id="benchmark"),
            authorization=None,
        )

    try:
        result = asyncio.run(_probe())
    except RuntimeError:
        # Called from inside a running event loop; the documented reason stands.
        return PRODUCTION_UNAVAILABLE_REASON
    except Exception:  # noqa: BLE001
        return PRODUCTION_UNAVAILABLE_REASON
    if isinstance(result, TypedRuntimeUnavailable):
        return result.reason
    return PRODUCTION_UNAVAILABLE_REASON


def build_unavailable_typed_executor(
    reason: str = PRODUCTION_UNAVAILABLE_REASON,
) -> UnavailableTypedExecutor:
    """The production-default executor: no trusted Backend authority, no run."""
    return UnavailableTypedExecutor(reason=reason)


__all__ = [
    "EVIDENCE_HARNESS",
    "RunCase",
    "EVIDENCE_UNAVAILABLE",
    "PRODUCTION_UNAVAILABLE_REASON",
    "STRUCTURED_STATE_KEYS",
    "TEXT_STATE_KEYS",
    "DemoEngineInvoker",
    "ExecutorAdapter",
    "TypedRunInvoker",
    "TypedRunProduct",
    "TypedRunProductError",
    "UnavailableTypedExecutor",
    "build_demo_engine_executor",
    "build_receipt",
    "build_unavailable_typed_executor",
    "product_from_engine_state",
    "production_gate_reason",
]
