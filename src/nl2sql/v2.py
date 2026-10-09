"""The v2 HTTP boundary: strict payload limits and identity-bound state access."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import Any, Final, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import Field, model_validator

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.provider import (
    BackendAuthorizationProvider,
    resolve_authorization_context,
)
from src.core.auth.types import AuthUser
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.contracts import ErrorEnvelope, RequestContext, RequestIdentity, StrictContract
from src.nl2sql.orchestration.ad_hoc_request import AdHocCalculationRequest
from src.nl2sql.orchestration.decision_contract import (
    HITLDecision,
    HITLRequest,
    ResumeToken,
    resume_token,
    revalidate_decision,
    revalidate_request,
    revalidate_resume_token,
)
from src.nl2sql.orchestration.mode_contract import (
    AuthorityProvenance,
    ProductMode,
    RequestedMode,
    RunEnvelope,
    resolve_requested_mode,
)
from src.nl2sql.orchestration.run_lineage import (
    RunLineageInvalid,
    current_owned_run,
)
from src.nl2sql.orchestration.shadow import is_shadow_sample
from src.nl2sql.ownership import runtime_config

MAX_MESSAGE_BYTES = 8 * 1024
MAX_MESSAGES = 20
MAX_REQUEST_BYTES = 32 * 1024


class QueryMessage(StrictContract):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_size(self) -> "QueryMessage":
        if len(self.content.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("each message must be at most 8 KiB")
        return self


class QueryRequest(StrictContract):
    messages: list[QueryMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    thread_id: UUID | None = None
    # The client states INTENT only, and "auto" is not a mode: the server
    # resolves it to ANALYZE.  The client may never supply the effective mode,
    # a run id, or any capability.
    requested_mode: RequestedMode = "auto"
    # Present only when the user ACCEPTS a mode-switch suggestion: a switch
    # starts a NEW run on the SAME thread, carrying the prior run for lineage.
    switched_from_run_id: str | None = Field(default=None, min_length=1, max_length=64)
    # OPTIONAL explicit run-scoped AD_HOC calculation carrier.  It is a STRICT
    # typed request (no authority/lifecycle/canonical field), never a mode: the
    # effective mode still comes from requested_mode, and an AD_HOC carrier is
    # only executable when the run's capability grants run_scoped_derivation.
    # The server validates and resolves it against the run's authorized context
    # and fails closed with a stable code otherwise.
    ad_hoc_calculation: AdHocCalculationRequest | None = None

    @model_validator(mode="after")
    def validate_total_size(self) -> "QueryRequest":
        total = sum(len(message.content.encode("utf-8")) for message in self.messages)
        if total > MAX_REQUEST_BYTES:
            raise ValueError("messages must total at most 32 KiB")
        return self


class QueryResponse(StrictContract):
    thread_id: UUID
    blocks: list[dict[str, Any]]
    # Server-owned run identity and mode resolution (never client-supplied).
    run_id: str | None = None
    requested_mode: RequestedMode | None = None
    effective_mode: ProductMode | None = None
    switched_from_run_id: str | None = None
    # Deployment provenance for the authority behind this run: backend | demo |
    # unavailable.  This is PRESENTATION/deployment provenance, not part of
    # business authorization semantics, and it never appears in ProductMode.
    authority_provenance: AuthorityProvenance | None = None


class MessageItem(StrictContract):
    role: str
    content: str


class StateSnapshot(StrictContract):
    thread_id: UUID
    checkpoint_id: str
    messages: list[MessageItem]
    created_at: str | None = None


class ThreadHistoryResponse(StrictContract):
    thread_id: UUID
    snapshots: list[StateSnapshot]


class ThreadActionRequest(StrictContract):
    target_type: Literal["legacy_hitl", "typed_decision"] = "legacy_hitl"
    action: Literal["approve", "modify", "reject", "cancel"] | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    expected_version: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=4096)
    decision: HITLDecision | None = None

    @model_validator(mode="after")
    def validate_target_payload(self) -> "ThreadActionRequest":
        if self.target_type == "legacy_hitl":
            if self.action is None or self.idempotency_key is None:
                raise ValueError("legacy action and idempotency key are required")
            if self.decision is not None:
                raise ValueError("legacy action cannot carry a typed decision")
            if self.action == "modify" and not self.feedback:
                raise ValueError("modify requires feedback")
        else:
            if self.decision is None:
                raise ValueError("typed decision payload is required")
            if self.action is not None or self.idempotency_key is not None:
                raise ValueError("typed decision cannot carry legacy action fields")
            if self.feedback is not None:
                raise ValueError("typed decision cannot carry legacy feedback")
        return self


class ThreadActionResponse(StrictContract):
    thread_id: UUID
    # Omitted for legacy clients so their exact response shape remains stable.
    target_type: Literal["legacy_hitl", "typed_decision"] | None = None
    status: Literal["approved", "modified", "rejected", "cancelled", "recorded"]
    version: int
    idempotent: bool = False
    decision_status: Literal[
        "resolved_pending_revalidation",
        "choose_recorded_no_continuation",
        "rejected",
        "cancelled",
    ] | None = None
    continuation_status: Literal[
        "resolved_pending_revalidation",
        "resolved_pending_current_authorization",
        "resolved_continuing_current_authorization",
        "current_authorization_unavailable",
        "current_authorization_denied",
        "continuation_execution_plan_denied",
        "continuation_stopped",
        "revalidation_denied",
        "awaiting_decision",
        "choose_recorded_no_continuation",
        "rejected",
        "cancelled",
        "invalid",
    ] | None = None


class FeedbackRequest(StrictContract):
    thread_id: UUID
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=4096)


class CapabilityResponse(StrictContract):
    model: bool
    embedding: bool
    semantic_release: bool
    graph_rag: bool
    hitl: bool
    codeact: bool
    # The CANONICAL block vocabulary.  This server manifest is the single source
    # of truth: the frontend mirrors/generates its types from it instead of
    # keeping a third hand-maintained copy that silently drops unknown blocks.
    supported_block_types: list[str] = Field(default_factory=list)
    # The fallback envelope a client must render VISIBLY when it receives a
    # block type it does not know, so nothing disappears silently.
    unsupported_block_fallback: dict[str, Any] = Field(default_factory=dict)
    # Purely additive, informational: whether THIS deployment activated the
    # trusted typed runtime.  It reflects a deployment-level setting only and
    # grants nothing; it is not a per-request toggle.
    typed_runtime: bool = False
    degradation_reasons: tuple[str, ...] = ()


def _request_identity(request: Request, auth_user: AuthUser) -> RequestIdentity:
    raw_request_id = request.headers.get("x-request-id")
    try:
        request_id = UUID(raw_request_id) if raw_request_id else uuid4()
    except ValueError:
        request_id = uuid4()
    return RequestIdentity(
        request_id=request_id,
        user_id=str(auth_user.user_id),
        roles=frozenset(auth_user.roles),
        permissions=frozenset(auth_user.permissions),
    )


def _request_context_deadline_ms() -> int:
    """The deployment's configured request deadline, honored by EVERY context.

    RequestContext carries a conservative 30s default; without this the engine's
    ``min(config, context)`` would silently clamp a deployment that raised
    V2_REQUEST_DEADLINE_MS for a slower real provider.
    """

    from src.nl2sql.config.settings import get_agent_config

    return get_agent_config().v2_request_deadline_ms


def _request_context(request: Request, auth_user: AuthUser, thread_id: UUID) -> RequestContext:
    identity = _request_identity(request, auth_user)
    return RequestContext(
        identity=identity,
        thread_id=thread_id,
        trace_id=request.headers.get("x-trace-id") or str(identity.request_id),
        deadline_ms=_request_context_deadline_ms(),
    )


def _authorization_provider_from_request(
    request: Request,
) -> BackendAuthorizationProvider | None:
    """Return the app-scoped Backend authorization provider, or None.

    Total/fail-closed: an absent container, an absent or non-callable accessor,
    an accessor returning None, and an accessor that RAISES all collapse to the
    same None, so the trusted boundary has ONE unavailable-source behavior.  A
    provider-like object is returned and resolve_authorization_context decides
    whether load() yields a valid AuthorizationContext.
    """

    container = getattr(request.app.state, "container", None)
    accessor = getattr(container, "get_backend_authorization_provider", None)
    if not callable(accessor):
        return None
    try:
        provider = accessor()
    except Exception:
        return None
    return cast(BackendAuthorizationProvider | None, provider)


async def _new_run_request_context(
    request: Request,
    auth_user: AuthUser,
    thread_id: UUID,
) -> RequestContext:
    """Build a NEW-run RequestContext, resolving trusted authorization ONCE.

    Only the routes that create a new graph run use this.  Existing-run and read
    routes keep the pure base _request_context, so thread/history/action/
    feedback never trigger a fresh Backend authorization fetch; the engine stays
    the sole in-run authority read and restores the bound snapshot.
    """

    identity = _request_identity(request, auth_user)
    authorization = await resolve_authorization_context(
        _authorization_provider_from_request(request), auth_user
    )
    return RequestContext(
        identity=identity,
        thread_id=thread_id,
        trace_id=request.headers.get("x-trace-id") or str(identity.request_id),
        authorization=authorization,
        deadline_ms=_request_context_deadline_ms(),
    )


async def _resume_request_context(
    request: Request,
    auth_user: AuthUser,
    thread_id: UUID,
) -> RequestContext:
    """Build an ACTION/RESUME RequestContext with CURRENT authorization.

    A resume can CONTINUE BUSINESS EXECUTION, so it must re-fetch the CURRENT
    trusted Backend authorization rather than restoring the snapshot bound to
    the run.  A past snapshot never grants future permission.

    This is deliberately distinct from the pure _request_context used by
    read-only thread/history routes: those must NOT trigger a Backend
    authorization refresh.  There is exactly ONE authority channel - the
    trusted server-side provider - and the client can never inject it through
    a body, query or header field.
    """

    identity = _request_identity(request, auth_user)
    authorization = await resolve_authorization_context(
        _authorization_provider_from_request(request), auth_user
    )
    return RequestContext(
        identity=identity,
        thread_id=thread_id,
        trace_id=request.headers.get("x-trace-id") or str(identity.request_id),
        authorization=authorization,
        deadline_ms=_request_context_deadline_ms(),
    )


# The CANONICAL server-side block vocabulary.  Adding a block type starts here;
# Foundation C mirrors it.  An unknown type is still delivered with the
# fallback envelope below so a client renders a visible "unsupported" card.
BLOCK_MANIFEST: Final[tuple[str, ...]] = (
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
)

# Delivered alongside any block the client may not understand.  The client must
# render this rather than dropping the block.
UNSUPPORTED_BLOCK_FALLBACK: Final[dict[str, str]] = {
    "kind": "unsupported_block",
    "message": "This result block is not supported by this client version.",
}


def _run_envelope(body: QueryRequest, context: RequestContext) -> RunEnvelope:
    """Create the SERVER-OWNED run envelope for one executable request.

    The client states intent (``requested_mode``) only.  The server mints the
    run id and resolves ``auto`` to ANALYZE; ``effective_mode`` can never be
    supplied by the client.  A mode switch is a NEW run on the SAME thread and
    carries the prior run for lineage - but NEVER its authorization.
    """

    return RunEnvelope(
        run_id=uuid4().hex,
        requested_mode=body.requested_mode,
        effective_mode=resolve_requested_mode(body.requested_mode),
        switched_from_run_id=body.switched_from_run_id,
    )


def _mode_switch_lineage_invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="mode_switch_lineage_invalid",
    )


async def _validate_mode_switch_lineage(
    *,
    engine: Any,
    context: RequestContext,
    switched_from_run_id: str | None,
    envelope: RunEnvelope,
) -> None:
    """Prove a mode switch names the current run on this user's thread.

    The check is against the persisted, identity-namespaced checkpoint rather
    than the client payload.  A thread has one current/latest checkpoint, so an
    older run id, a foreign owner, a foreign thread, and a missing checkpoint
    all fail with the same stable 409 before a new run or SSE metadata exists.
    """

    try:
        current = await current_owned_run(engine, context=context)
    except RunLineageInvalid as exc:
        raise _mode_switch_lineage_invalid() from exc
    if current is None:
        if switched_from_run_id is not None:
            raise _mode_switch_lineage_invalid()
        return
    if switched_from_run_id is None:
        if current.effective_mode != envelope.effective_mode:
            raise _mode_switch_lineage_invalid()
        return
    if current.effective_mode == envelope.effective_mode:
        raise _mode_switch_lineage_invalid()
    if current.run_id != switched_from_run_id:
        raise _mode_switch_lineage_invalid()


def _authority_provenance(request: Request) -> AuthorityProvenance:
    """Report which authority answered this run: backend | demo | unavailable.

    Presentation provenance only - it grants nothing and is never a mode.
    """

    container = getattr(request.app.state, "container", None)
    accessor = getattr(container, "authority_provenance", None)
    if callable(accessor):
        try:
            value = accessor()
        except Exception:
            return "unavailable"
        if value in ("backend", "demo", "local_real_demo", "unavailable"):
            return cast(AuthorityProvenance, value)
    return "unavailable"


def _runtime_config(context: RequestContext) -> RunnableConfig:
    config = cast(RunnableConfig, runtime_config(context))
    agent_config = get_agent_config()
    config["recursion_limit"] = agent_config.graph_recursion_limit
    configurable = cast(dict[str, object], config.get("configurable", {}))
    configurable["shadow_mode"] = (
        agent_config.engine_mode == "shadow"
        and is_shadow_sample(context.identity.request_id, percentage=agent_config.shadow_traffic_percent)
    )
    return config


def _snapshot(thread_id: UUID, state: Any) -> StateSnapshot:
    from src.nl2sql.api import extract_messages

    values = getattr(state, "values", {})
    config = getattr(state, "config", {})
    metadata = getattr(state, "metadata", {})
    checkpoint_id = config.get("configurable", {}).get("checkpoint_id", "")
    created_at = metadata.get("created_at") if metadata else None
    return StateSnapshot(
        thread_id=thread_id,
        checkpoint_id=str(checkpoint_id),
        messages=[MessageItem.model_validate(item) for item in extract_messages(values)],
        created_at=str(created_at) if created_at else None,
    )


_TYPED_RECORDED_STATUS: Final[dict[str, str]] = {
    "resolve": "resolved_pending_revalidation",
    # Confirm is the typed form of the legacy approve alias and MUST map here
    # too: the engine records it as resolved_pending_revalidation, so omitting it
    # made every typed confirm unrecordable over HTTP (409) the moment something
    # actually produced a confirmation request.  The two maps are the SAME
    # vocabulary and must stay in step.
    "confirm": "resolved_pending_revalidation",
    "choose": "choose_recorded_no_continuation",
    "reject": "rejected",
    "cancel": "cancelled",
}

_TYPED_CONTINUATION_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "resolved_pending_revalidation",
        "resolved_pending_current_authorization",
        "resolved_continuing_current_authorization",
        "current_authorization_unavailable",
        "current_authorization_denied",
        "continuation_execution_plan_denied",
        "continuation_stopped",
        "revalidation_denied",
        "awaiting_decision",
        "choose_recorded_no_continuation",
        "rejected",
        "cancelled",
        "invalid",
        # The typed, fail-closed outcome of a rejected in-place correction: a
        # stable product-visible status instead of a misleading fallback.
        "metric_plan_correction_rejected",
    }
)


def _validated_typed_ledger_entry(
    entry: object,
    *,
    incoming: HITLDecision,
    expected_version: int,
) -> tuple[HITLRequest, HITLDecision, ResumeToken, str]:
    """Revalidate one checkpoint ledger entry and prove exact replay binding."""

    if not isinstance(entry, Mapping):
        raise ValueError("typed decision ledger entry is absent")
    stored_request = revalidate_request(entry.get("request"))
    stored_decision = revalidate_decision(entry.get("decision"))
    stored_token = revalidate_resume_token(entry.get("resume_token"))
    derived_token = resume_token(
        request=stored_request,
        decision=stored_decision,
    )
    if (
        entry.get("request_id") != stored_request.request_id
        or entry.get("request_version") != stored_request.version
        or entry.get("request_checksum") != stored_request.checksum
        or entry.get("decision_checksum") != stored_decision.checksum
        or entry.get("resume_token_checksum") != stored_token.checksum
        or stored_token.checksum != derived_token.checksum
        or stored_token.validate_against(stored_request, stored_decision)
        or expected_version != stored_request.version
        or incoming.checksum != stored_decision.checksum
    ):
        raise ValueError("typed decision idempotency conflict")
    recorded_status = _TYPED_RECORDED_STATUS.get(stored_decision.action)
    if recorded_status is None:
        raise ValueError("typed decision action is not recordable")
    return stored_request, stored_decision, stored_token, recorded_status


def _typed_continuation_status(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and value in _TYPED_CONTINUATION_STATUSES:
        return value
    return fallback


async def _stream_query(
    engine: Any,
    messages: list[dict[str, str]],
    config: RunnableConfig,
    thread_id: UUID,
    extra_input: Mapping[str, object] | None = None,
) -> AsyncGenerator[str, None]:
    from src.nl2sql.api import stream_blocks

    async for chunk in stream_blocks(
        engine, messages, config, str(thread_id), extra_input
    ):
        yield chunk


async def _engine_from_request(request: Request) -> Any:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="runtime unavailable")
    from src.core.settings import get_settings
    from src.nl2sql.infra.llm.gateway import model_gateway_available

    if get_settings().service_mode == "product":
        readiness = (
            container.readiness_report(model_available=model_gateway_available())
            if hasattr(container, "readiness_report")
            else {"status": "not_ready"}
        )
        if readiness.get("status") != "ready":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="runtime dependencies are unavailable",
            )
    get_engine = getattr(container, "get_engine", None)
    if not callable(get_engine):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime engine unavailable",
        )
    from src.nl2sql.container import RuntimeDependencyUnavailable

    try:
        return await cast(Callable[[], Awaitable[Any]], get_engine)()
    except RuntimeDependencyUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        ) from exc


def register_v2_routes(app: FastAPI) -> None:
    """Register the sole executable API contract for NL2SQL."""

    from src.nl2sql.api import extract_blocks
    router = APIRouter(prefix="/api/v2/nl2sql", tags=["nl2sql-v2"])

    @router.post("/queries", response_model=QueryResponse)
    async def query(
        request: Request,
        body: QueryRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> QueryResponse:
        thread_id = body.thread_id or uuid4()
        context = await _new_run_request_context(request, auth_user, thread_id)
        envelope = _run_envelope(body, context)
        config = _runtime_config(context)
        engine = await _engine_from_request(request)
        await _validate_mode_switch_lineage(
            engine=engine,
            context=context,
            switched_from_run_id=body.switched_from_run_id,
            envelope=envelope,
        )
        graph_input: dict[str, object] = {
            "messages": [message.model_dump() for message in body.messages],
            "run_envelope": envelope.model_dump(mode="json"),
        }
        if body.ad_hoc_calculation is not None:
            # Server-validated, authority-free carrier.  The engine resolves it
            # against the run's authorized context and fails closed on any
            # missing/ambiguous/catalog-bound input.
            graph_input["ad_hoc_calculation"] = body.ad_hoc_calculation.model_dump(
                mode="json"
            )
        result = await engine.ainvoke(graph_input, config)
        return QueryResponse(
            thread_id=thread_id,
            blocks=extract_blocks(result),
            run_id=envelope.run_id,
            requested_mode=envelope.requested_mode,
            effective_mode=envelope.effective_mode,
            switched_from_run_id=envelope.switched_from_run_id,
            authority_provenance=_authority_provenance(request),
        )

    @router.post("/queries/stream")
    async def stream_query(
        request: Request,
        body: QueryRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> StreamingResponse:
        thread_id = body.thread_id or uuid4()
        context = await _new_run_request_context(request, auth_user, thread_id)
        envelope = _run_envelope(body, context)
        config = _runtime_config(context)
        engine = await _engine_from_request(request)
        await _validate_mode_switch_lineage(
            engine=engine,
            context=context,
            switched_from_run_id=body.switched_from_run_id,
            envelope=envelope,
        )
        extra_input: dict[str, object] = {
            "run_envelope": envelope.model_dump(mode="json"),
            "stream_metadata": {
                "thread_id": str(thread_id),
                "run_id": envelope.run_id,
                "requested_mode": envelope.requested_mode,
                "effective_mode": envelope.effective_mode,
                "switched_from_run_id": envelope.switched_from_run_id,
                "authority_provenance": _authority_provenance(request),
            },
        }
        if body.ad_hoc_calculation is not None:
            extra_input["ad_hoc_calculation"] = body.ad_hoc_calculation.model_dump(
                mode="json"
            )
        return StreamingResponse(
            _stream_query(
                engine,
                [message.model_dump() for message in body.messages],
                config,
                thread_id,
                extra_input,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/threads/{thread_id}", response_model=StateSnapshot)
    async def get_thread(
        thread_id: UUID,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> StateSnapshot:
        context = _request_context(request, auth_user, thread_id)
        state_value = await (await _engine_from_request(request)).aget_state(runtime_config(context))
        if not state_value or not getattr(state_value, "values", None):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
        return _snapshot(thread_id, state_value)

    @router.get("/threads/{thread_id}/history", response_model=ThreadHistoryResponse)
    async def get_history(
        thread_id: UUID,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ThreadHistoryResponse:
        context = _request_context(request, auth_user, thread_id)
        snapshots = [
            _snapshot(thread_id, state_value)
            async for state_value in (await _engine_from_request(request)).aget_state_history(
                runtime_config(context)
            )
        ]
        if not snapshots:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
        return ThreadHistoryResponse(thread_id=thread_id, snapshots=snapshots)

    @router.post(
        "/threads/{thread_id}/actions",
        response_model=ThreadActionResponse,
        response_model_exclude_none=True,
    )
    async def thread_action(
        thread_id: UUID,
        request: Request,
        body: ThreadActionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ThreadActionResponse:
        # An action/resume can CONTINUE BUSINESS EXECUTION, so it re-fetches the
        # CURRENT trusted Backend authorization (B1-FIX).  Read-only
        # thread/history routes keep the pure _request_context and never trigger
        # a Backend authorization refresh.
        context = await _resume_request_context(request, auth_user, thread_id)
        engine = await _engine_from_request(request)
        config = _runtime_config(context)
        state_value = await engine.aget_state(config)
        if not state_value or not getattr(state_value, "values", None):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
        values = cast(dict[str, object], state_value.values)

        if body.target_type == "typed_decision":
            try:
                incoming = revalidate_decision(body.decision)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="typed decision is invalid",
                ) from exc
            ledger = values.get("decision_ledger")
            existing = (
                ledger.get(incoming.idempotency_key)
                if isinstance(ledger, Mapping)
                else None
            )
            if existing is not None:
                try:
                    stored_request, _, _, recorded_status = (
                        _validated_typed_ledger_entry(
                            existing,
                            incoming=incoming,
                            expected_version=body.expected_version,
                        )
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="typed decision idempotency conflict",
                    ) from exc
                continuation = _typed_continuation_status(
                    values.get("decision_status"), fallback=recorded_status
                )
                return ThreadActionResponse(
                    thread_id=thread_id,
                    target_type="typed_decision",
                    status="recorded",
                    version=stored_request.version,
                    idempotent=True,
                    decision_status=cast(Any, recorded_status),
                    continuation_status=cast(Any, continuation),
                )

            if values.get("decision_status") != "awaiting_decision":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="thread is not awaiting a typed decision",
                )
            try:
                pending = revalidate_request(values.get("pending_decision"))
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="pending typed decision is invalid",
                ) from exc
            decision_version = values.get("decision_version")
            if (
                not isinstance(decision_version, int)
                or body.expected_version != decision_version
                or pending.version != decision_version
                or incoming.request_id != pending.request_id
                or incoming.request_version != pending.version
                or incoming.validate_against(pending)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="typed decision is stale or conflicting",
                )

            result = await engine.ainvoke(
                # The engine's typed-decision boundary is DENY-BY-DEFAULT: it
                # consumes exactly the fields below and rebuilds request identity
                # from the STORED request, so a forged payload cannot escalate.
                # schema_version is part of the wire contract but is NOT one of
                # those fields, so sending it made every production resume fail
                # with typed_decision_unknown_field and never be recorded.
                Command(
                    resume=incoming.model_dump(mode="json", exclude={"schema_version"})
                ),
                config,
            )
            if not isinstance(result, dict):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="typed decision was not recorded",
                )
            result_ledger = result.get("decision_ledger")
            recorded = (
                result_ledger.get(incoming.idempotency_key)
                if isinstance(result_ledger, Mapping)
                else None
            )
            try:
                stored_request, _, _, recorded_status = _validated_typed_ledger_entry(
                    recorded,
                    incoming=incoming,
                    expected_version=body.expected_version,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="typed decision was not recorded",
                ) from exc
            continuation = _typed_continuation_status(
                result.get("decision_status"), fallback=recorded_status
            )
            return ThreadActionResponse(
                thread_id=thread_id,
                target_type="typed_decision",
                status="recorded",
                version=stored_request.version,
                decision_status=cast(Any, recorded_status),
                continuation_status=cast(Any, continuation),
            )

        if body.action is None or body.idempotency_key is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="legacy action payload is invalid",
            )
        applied_actions = values.get("applied_actions", {})
        if isinstance(applied_actions, dict):
            prior = applied_actions.get(body.idempotency_key)
            if isinstance(prior, dict):
                prior_status = prior.get("status")
                prior_version = prior.get("version")
                if isinstance(prior_status, str) and isinstance(prior_version, int):
                    return ThreadActionResponse(
                        thread_id=thread_id,
                        status=cast(Literal["approved", "modified", "rejected", "cancelled"], prior_status),
                        version=prior_version,
                        idempotent=True,
                    )
        if values.get("hitl_status") != "awaiting_action":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="thread is not awaiting an action")
        version = values.get("hitl_version")
        if version != body.expected_version:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="action version is stale")
        legacy_payload = {
            "action": body.action,
            "idempotency_key": body.idempotency_key,
            "expected_version": body.expected_version,
            "feedback": body.feedback,
        }
        result = await engine.ainvoke(Command(resume=legacy_payload), config)
        result_status = result.get("hitl_status") if isinstance(result, dict) else None
        result_version = result.get("hitl_version") if isinstance(result, dict) else None
        # An approval that revalidated CURRENT authorization and continued into
        # governed execution reports "approved_continuing"; the legacy wire
        # status stays "approved" so the response shape is unchanged.
        response_status = (
            "approved"
            if result_status == "approved_continuing"
            else result_status
            if result_status in {"approved", "modified", "rejected", "cancelled"}
            else None
        )
        if response_status is None or not isinstance(result_version, int):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="action was not applied")
        return ThreadActionResponse(
            thread_id=thread_id,
            status=cast(Literal["approved", "modified", "rejected", "cancelled"], response_status),
            version=result_version,
        )

    @router.post("/feedback", status_code=status.HTTP_202_ACCEPTED)
    async def feedback(
        request: Request,
        body: FeedbackRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> dict[str, str]:
        context = _request_context(request, auth_user, body.thread_id)
        state_value = await (await _engine_from_request(request)).aget_state(runtime_config(context))
        if not state_value or not getattr(state_value, "values", None):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
        return {"status": "accepted"}

    @router.get("/capabilities", response_model=CapabilityResponse)
    async def capabilities(
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> CapabilityResponse:
        del auth_user
        config = get_agent_config()
        from src.core.settings import get_settings
        from src.nl2sql.infra.llm.gateway import model_gateway_available

        codeact_available, codeact_reason = config.codeact_capability()
        model_available = model_gateway_available()
        container = getattr(request.app.state, "container", None)
        readiness = (
            container.readiness_report(model_available=model_available)
            if container is not None and hasattr(container, "readiness_report")
            else {"degradation_reasons": ("runtime_container_unavailable",)}
        )
        degradation_reasons = list(readiness.get("degradation_reasons", ()))
        if not model_available:
            degradation_reasons.append("model provider is not configured")
        if codeact_reason:
            degradation_reasons.append(codeact_reason)

        return CapabilityResponse(
            model=model_available,
            embedding=False,
            semantic_release=False,
            graph_rag=config.enable_graph_rag,
            hitl=bool(getattr(container, "checkpoint_available", False)),
            codeact=codeact_available,
            # Populated EXPLICITLY from the canonical manifest - never left to
            # the Pydantic defaults, which would advertise an empty vocabulary.
            supported_block_types=list(BLOCK_MANIFEST),
            unsupported_block_fallback=dict(UNSUPPORTED_BLOCK_FALLBACK),
            typed_runtime=get_settings().typed_runtime_enabled,
            degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
        )

    app.include_router(router)
    # Bounded product routers: the Definition/Publication and Library product
    # surfaces are registered separately so this module does not become a
    # monolith.  Both use the SAME server-side permission dependency.
    from src.nl2sql.artifacts.api_artifacts import register_artifact_routes
    from src.nl2sql.artifacts.api_conflicts import register_conflict_routes
    from src.nl2sql.artifacts.api_definitions import register_definition_routes
    from src.nl2sql.artifacts.api_exploration_confirmations import (
        register_exploration_confirmation_routes,
    )
    from src.nl2sql.artifacts.api_library import register_library_routes

    register_definition_routes(app)
    register_library_routes(app)
    register_conflict_routes(app)
    # The result-artifact surface is its OWN product router, mounted next to the
    # others rather than inside the Definition router: saving a result artifact
    # is an artifact operation (A3) and must never be reachable through the
    # definition lifecycle.
    register_artifact_routes(app)
    # The run-scoped EXPLORATION confirmation surface is likewise its OWN product
    # router: it is mounted here so the §8.16 P7B "an exploration confirmation
    # never stands in for a definition confirmation" invariant is reachable by a
    # real caller instead of being true only inside a unit test.
    register_exploration_confirmation_routes(app)


def register_v1_gone_routes(app: FastAPI) -> None:
    """Make every old executable endpoint an explicit, documented migration error."""

    @app.api_route(
        "/nl2sql/{legacy_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        response_model=ErrorEnvelope,
        status_code=status.HTTP_410_GONE,
        include_in_schema=False,
    )
    async def legacy_v1_gone(request: Request, legacy_path: str) -> JSONResponse:
        del legacy_path
        trace_id = request.headers.get("x-trace-id") or request.headers.get("x-request-id") or "-"
        envelope = ErrorEnvelope(
            code="API_V1_GONE",
            retryable=False,
            stage="api",
            safe_message="This endpoint was removed; migrate to /api/v2/nl2sql.",
            trace_id=trace_id,
        )
        return JSONResponse(status_code=status.HTTP_410_GONE, content=envelope.model_dump())
