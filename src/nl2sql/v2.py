"""The v2 HTTP boundary: strict payload limits and identity-bound state access."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import Field, model_validator

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.contracts import ErrorEnvelope, RequestContext, RequestIdentity, StrictContract
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

    @model_validator(mode="after")
    def validate_total_size(self) -> "QueryRequest":
        total = sum(len(message.content.encode("utf-8")) for message in self.messages)
        if total > MAX_REQUEST_BYTES:
            raise ValueError("messages must total at most 32 KiB")
        return self


class QueryResponse(StrictContract):
    thread_id: UUID
    blocks: list[dict[str, Any]]


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
    action: Literal["approve", "modify", "reject", "cancel"]
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_modify_feedback(self) -> "ThreadActionRequest":
        if self.action == "modify" and not self.feedback:
            raise ValueError("modify requires feedback")
        return self


class ThreadActionResponse(StrictContract):
    thread_id: UUID
    status: Literal["approved", "modified", "rejected", "cancelled"]
    version: int
    idempotent: bool = False


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


def _request_context(request: Request, auth_user: AuthUser, thread_id: UUID) -> RequestContext:
    identity = _request_identity(request, auth_user)
    return RequestContext(
        identity=identity,
        thread_id=thread_id,
        trace_id=request.headers.get("x-trace-id") or str(identity.request_id),
    )


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


async def _stream_query(
    engine: Any,
    messages: list[dict[str, str]],
    config: RunnableConfig,
    thread_id: UUID,
) -> AsyncGenerator[str, None]:
    from src.nl2sql.api import stream_blocks

    async for chunk in stream_blocks(engine, messages, config, str(thread_id)):
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
    if callable(get_engine):
        from src.nl2sql.container import RuntimeDependencyUnavailable

        try:
            return await cast(Callable[[], Awaitable[Any]], get_engine)()
        except RuntimeDependencyUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="runtime dependency unavailable",
            ) from exc
    # Compatibility with test doubles from the v2-contract PR.
    return await container.get_supervisor()


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
        context = _request_context(request, auth_user, thread_id)
        config = _runtime_config(context)
        result = await (await _engine_from_request(request)).ainvoke(
            {"messages": [message.model_dump() for message in body.messages]}, config
        )
        return QueryResponse(thread_id=thread_id, blocks=extract_blocks(result))

    @router.post("/queries/stream")
    async def stream_query(
        request: Request,
        body: QueryRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> StreamingResponse:
        thread_id = body.thread_id or uuid4()
        context = _request_context(request, auth_user, thread_id)
        config = _runtime_config(context)
        return StreamingResponse(
            _stream_query(
                await _engine_from_request(request),
                [message.model_dump() for message in body.messages],
                config,
                thread_id,
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

    @router.post("/threads/{thread_id}/actions", response_model=ThreadActionResponse)
    async def thread_action(
        thread_id: UUID,
        request: Request,
        body: ThreadActionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ThreadActionResponse:
        context = _request_context(request, auth_user, thread_id)
        engine = await _engine_from_request(request)
        config = _runtime_config(context)
        state_value = await engine.aget_state(config)
        if not state_value or not getattr(state_value, "values", None):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
        values = cast(dict[str, object], state_value.values)
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
        result = await engine.ainvoke(Command(resume=body.model_dump()), config)
        result_status = result.get("hitl_status") if isinstance(result, dict) else None
        result_version = result.get("hitl_version") if isinstance(result, dict) else None
        if result_status not in {"approved", "modified", "rejected", "cancelled"} or not isinstance(result_version, int):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="action was not applied")
        return ThreadActionResponse(
            thread_id=thread_id,
            status=cast(Literal["approved", "modified", "rejected", "cancelled"], result_status),
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
            degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
        )

    app.include_router(router)


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
