"""NL2SQL API ??

????? Supervisor Agent?? Supervisor ?? nl2sql?chart ?? Agent?
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from langchain_core.runnables import RunnableConfig
from langserve import add_routes as _add_routes
from pydantic import BaseModel

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.core.observer import create_monitored_config
from src.core.settings import get_settings as get_core_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.memory.checkpointer import get_checkpointer_manager
from src.nl2sql.infra.runtime.registry import warmup_runtime
from src.nl2sql.infra.store.qa_rag import sync_qa_index
from src.nl2sql.infra.store.semantic_rag import sync_semantic_index

logger = logging.getLogger(__name__)

add_routes: Any = _add_routes

# ????
_supervisor: Any | None = None


# === ???????? ===


class MessageItem(BaseModel):
    """????"""

    role: str
    content: str


class StateSnapshot(BaseModel):
    """????"""

    thread_id: str
    checkpoint_id: str
    messages: list[MessageItem]
    created_at: str | None = None


class ThreadHistoryResponse(BaseModel):
    """??????"""

    thread_id: str
    snapshots: list[StateSnapshot]


def _resolve_thread_id(config: dict[str, Any], request: Any) -> str:
    """?? thread_id?????header > query > body > ??"""
    header_thread_id = request.headers.get("x-thread-id")
    if isinstance(header_thread_id, str) and header_thread_id.strip():
        return header_thread_id.strip()

    query_thread_id = request.query_params.get("thread_id")
    if isinstance(query_thread_id, str) and query_thread_id.strip():
        return query_thread_id.strip()

    configurable = config.get("configurable", {})
    if isinstance(configurable, dict):
        body_thread_id = configurable.get("thread_id")
        if isinstance(body_thread_id, str) and body_thread_id.strip():
            return body_thread_id.strip()

    return str(uuid4())


def _inject_request_runtime_config(
    config: dict[str, Any], request: Any
) -> RunnableConfig:
    """???????"""
    agent_config = get_agent_config()
    configurable = config.get("configurable", {})
    if not isinstance(configurable, dict):
        configurable = {}

    auth_user = getattr(request.state, "auth_user", None)
    auth_user_config: dict[str, Any] = {}
    if isinstance(auth_user, AuthUser):
        auth_user_config = {
            "auth_user_id": auth_user.user_id,
            "auth_user_telephone": auth_user.telephone,
            "auth_user_roles": auth_user.roles,
        }

    merged_config = cast(
        RunnableConfig,
        {
            **config,
            "recursion_limit": agent_config.graph_recursion_limit,
            "configurable": {
                **configurable,
                "thread_id": _resolve_thread_id(config, request),
                **auth_user_config,
            },
        },
    )
    thread_id = cast(
        str, (merged_config.get("configurable") or {}).get("thread_id", "")
    )
    return create_monitored_config(
        session_id=thread_id,
        base_config=merged_config,
        run_name="nl2sql",
    )


def _register_supervisor_routes(app: FastAPI, supervisor: Any) -> None:
    """?? Supervisor ??"""
    add_routes(
        app,
        supervisor,
        path="/nl2sql",
        config_keys=["configurable"],
        per_req_config_modifier=_inject_request_runtime_config,
        enabled_endpoints=["invoke", "stream_events", "playground"],
        dependencies=[Depends(require_nl2sql_permission)],
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Supervisor ?????????"""
    global _supervisor

    core_settings = get_core_settings()
    if core_settings.skip_rag_startup_sync:
        logger.warning(
            "SKIP_RAG_STARTUP_SYNC=true????????????? QA/Semantic ?????"
            "??????? false ????? EMBEDDING_* ??? .vector_store"
        )
    else:
        logger.info("???? RAG ????...")
        try:
            result = await asyncio.to_thread(sync_qa_index)
            if result.changed:
                logger.info(
                    f"? QA RAG ?????: {result.total_qas} ? QA, "
                    f"{result.total_chunks} ??? | {result.reason}"
                )
            else:
                logger.info(
                    f"? QA RAG ??????: {result.total_qas} ? QA, "
                    f"{result.total_chunks} ??? | {result.reason}"
                )
        except Exception as e:
            logger.error(f"? QA RAG ??????: {e}", exc_info=True)
            if core_settings.rag_startup_sync_strict:
                raise
            logger.warning(
                "RAG_STARTUP_SYNC_STRICT=false??? QA ?????????????????????"
            )

        try:
            sem_result = await asyncio.to_thread(sync_semantic_index)
            if sem_result.changed:
                logger.info(
                    f"? Semantic RAG ?????: {sem_result.total_chunks} ??? | {sem_result.reason}"
                )
            else:
                logger.info(
                    f"? Semantic RAG ??????: {sem_result.total_chunks} ??? | {sem_result.reason}"
                )
        except Exception as e:
            logger.error(f"? Semantic RAG ??????: {e}", exc_info=True)
            if core_settings.rag_startup_sync_strict:
                raise
            logger.warning(
                "RAG_STARTUP_SYNC_STRICT=false??? Semantic ?????????????????????"
            )

    # ?????????????? AgentConfig ?? schema ?????
    from src.nl2sql.infra.store.database import get_db_manager

    agent_config = get_agent_config()
    await get_db_manager(schema=agent_config.nl2sql_db_schema)
    await warmup_runtime()

    # ??? checkpointer
    checkpointer_manager = get_checkpointer_manager()
    await checkpointer_manager.init()

    # ?? Supervisor????
    if _supervisor is None:
        from src.nl2sql.supervisor.agent import create_supervisor

        _supervisor = await create_supervisor(checkpointer_manager.checkpointer)

    yield

    # ?????
    await checkpointer_manager.close()

    from src.nl2sql.infra.store.database import close_db_manager

    await close_db_manager()


def _extract_messages(state_values: dict[str, Any]) -> list[MessageItem]:
    """???????????"""
    messages_raw = state_values.get("messages", [])
    if not isinstance(messages_raw, list):
        return []

    result: list[MessageItem] = []
    for msg in messages_raw:
        role = getattr(msg, "type", None) or type(msg).__name__.lower().replace(
            "message", ""
        )
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            result.append(MessageItem(role=role, content=content))
    return result


def _get_supervisor() -> Any:
    """?? Supervisor ??"""
    if _supervisor is None:
        raise RuntimeError("Supervisor ????")
    return _supervisor


def register_history_routes(app: FastAPI) -> None:
    """????????"""

    @app.get(
        "/threads/{thread_id}/history",
        response_model=ThreadHistoryResponse,
        dependencies=[Depends(require_nl2sql_permission)],
        summary="??????",
        description="???? thread_id ????????????????",
    )
    async def get_thread_history(thread_id: str) -> ThreadHistoryResponse:
        supervisor = _get_supervisor()
        config = {"configurable": {"thread_id": thread_id}}

        snapshots: list[StateSnapshot] = []
        async for state in supervisor.aget_state_history(config):
            checkpoint_id = state.config.get("configurable", {}).get(
                "checkpoint_id", ""
            )
            created_at = state.metadata.get("created_at") if state.metadata else None

            snapshots.append(
                StateSnapshot(
                    thread_id=thread_id,
                    checkpoint_id=checkpoint_id,
                    messages=_extract_messages(state.values),
                    created_at=str(created_at) if created_at else None,
                )
            )

        return ThreadHistoryResponse(thread_id=thread_id, snapshots=snapshots)

    @app.get(
        "/threads/{thread_id}/state",
        response_model=StateSnapshot,
        dependencies=[Depends(require_nl2sql_permission)],
        summary="????????",
        description="???? thread_id ???????",
    )
    async def get_thread_state(thread_id: str) -> StateSnapshot:
        supervisor = _get_supervisor()
        config = {"configurable": {"thread_id": thread_id}}

        state = await supervisor.aget_state(config)
        checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id", "")
        created_at = state.metadata.get("created_at") if state.metadata else None

        return StateSnapshot(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            messages=_extract_messages(state.values),
            created_at=str(created_at) if created_at else None,
        )


# === OpenAI ?? Chat Completions API ===


class ChatCompletionRequest(BaseModel):
    """Chat Completions ??"""

    messages: list[dict[str, str]]
    stream: bool = False
    thread_id: str | None = None


class ChatMessage(BaseModel):
    """Chat ??"""

    role: Literal["assistant"] = "assistant"
    content: list[dict[str, Any]]


class ChatChoice(BaseModel):
    """Chat ???"""

    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop"] = "stop"


class ChatCompletionResponse(BaseModel):
    """Chat Completions ??"""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    choices: list[ChatChoice]
    thread_id: str


class ChatCompletionChunk(BaseModel):
    """Chat Completions ???"""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    choices: list[dict[str, Any]]
    thread_id: str


def register_chat_completions_routes(app: FastAPI) -> None:
    """?? OpenAI ??? Chat Completions ??"""

    @app.post(
        "/nl2sql/chat/completions",
        response_model=None,  # ????????????
        dependencies=[Depends(require_nl2sql_permission)],
        summary="Chat Completions API",
        description="OpenAI ??? Chat Completions API???????????",
    )
    async def chat_completions(
        request: Request,
        body: ChatCompletionRequest,
    ) -> ChatCompletionResponse | StreamingResponse:
        supervisor = _get_supervisor()
        thread_id = body.thread_id or str(uuid4())
        base_config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": get_agent_config().graph_recursion_limit,
        }

        # ??????
        auth_user = getattr(request.state, "auth_user", None)
        if isinstance(auth_user, AuthUser):
            base_config["configurable"]["auth_user_id"] = auth_user.user_id
            base_config["configurable"]["auth_user_telephone"] = auth_user.telephone
            base_config["configurable"]["auth_user_roles"] = auth_user.roles

        config = create_monitored_config(
            session_id=thread_id,
            base_config=base_config,
            run_name="nl2sql",
        )

        # ??????
        input_messages = body.messages

        if body.stream:
            return StreamingResponse(
                _stream_chat_completion(supervisor, input_messages, config, thread_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # ?????
        result = await supervisor.ainvoke({"messages": input_messages}, config)
        blocks = _extract_blocks_from_result(result)

        completion_id = f"chatcmpl-{uuid4().hex[:8]}"
        return ChatCompletionResponse(
            id=completion_id,
            created=int(time.time()),
            choices=[
                ChatChoice(
                    message=ChatMessage(content=blocks),
                )
            ],
            thread_id=thread_id,
        )


def _extract_blocks_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """? supervisor ????? blocks"""
    from src.nl2sql.supervisor.schemas import (
        SupervisorResponse,
        TextBlock,
        serialize_response_blocks,
    )

    structured: SupervisorResponse | None = result.get("structured_response")
    if structured is not None and structured.blocks:
        return serialize_response_blocks(structured.blocks)

    messages = result.get("messages", [])
    if isinstance(messages, list) and messages:
        # ???????????? AI ??
        for msg in reversed(messages):
            msg_type = getattr(msg, "type", None)
            if msg_type == "ai":
                content = getattr(msg, "content", None)
                if isinstance(content, str):
                    return serialize_response_blocks([TextBlock(text=content)])

    return serialize_response_blocks([TextBlock(text="??????")])


async def _stream_chat_completion(
    supervisor: Any,
    input_messages: list[dict[str, str]],
    config: RunnableConfig,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    """???? Chat Completions"""
    completion_id = f"chatcmpl-{uuid4().hex[:8]}"
    created = int(time.time())

    def _make_block_chunk(block_dict: dict[str, Any]) -> str:
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created,
            choices=[
                {"index": 0, "delta": {"content": [block_dict]}, "finish_reason": None}
            ],
            thread_id=thread_id,
        )
        return f"data: {chunk.model_dump_json()}\n\n"

    blocks_sent = False
    first_event_sent = False
    fallback_output: dict[str, Any] | None = None

    async for event in supervisor.astream_events(
        {"messages": input_messages}, config, version="v2"
    ):
        # ???????????????? delta??????????
        if not first_event_sent:
            first_event_sent = True
            start_chunk = ChatCompletionChunk(
                id=completion_id,
                created=created,
                choices=[{"index": 0, "delta": {}, "finish_reason": None}],
                thread_id=thread_id,
            )
            yield f"data: {start_chunk.model_dump_json()}\n\n"

        kind = event.get("event")

        # ????????????? blocks
        if kind == "on_chain_end" and not blocks_sent:
            output = event.get("data", {}).get("output")
            if not isinstance(output, dict):
                continue
            if "structured_response" in output:
                structured = output["structured_response"]
                if structured and hasattr(structured, "blocks") and structured.blocks:
                    from src.nl2sql.supervisor.schemas import serialize_response_blocks

                    for block_dict in serialize_response_blocks(structured.blocks):
                        yield _make_block_chunk(block_dict)
                    blocks_sent = True
            else:
                # ???????? structured_response ??????? fallback ??
                fallback_output = output

    # ?????? structured_response??? messages ?????
    if not blocks_sent and fallback_output is not None:
        for block_dict in _extract_blocks_from_result(fallback_output):
            yield _make_block_chunk(block_dict)

    # ??????
    final_chunk = ChatCompletionChunk(
        id=completion_id,
        created=created,
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        thread_id=thread_id,
    )
    yield f"data: {final_chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


# === HITL ??/?? API ===


class HITLConfirmRequest(BaseModel):
    """HITL ????"""

    thread_id: str
    action: Literal["confirm", "modify", "restart"]
    feedback: str | None = None


class HITLConfirmResponse(BaseModel):
    """HITL ????"""

    thread_id: str
    status: str
    plan_summary: str | None = None
    message: str | None = None


def register_hitl_routes(app: FastAPI) -> None:
    """?? HITL ????????"""

    @app.post(
        "/nl2sql/hitl/action",
        response_model=HITLConfirmResponse,
        dependencies=[Depends(require_nl2sql_permission)],
        summary="HITL ????/??/??",
        description=(
            "? CodeAct Engine ????????????\n"
            "- confirm: ???????????? CodeAct ??\n"
            "- modify: ???????? feedback ???????\n"
            "- restart: ???????????"
        ),
    )
    async def hitl_action(body: HITLConfirmRequest) -> HITLConfirmResponse:

        supervisor = _get_supervisor()
        config = {"configurable": {"thread_id": body.thread_id}}

        state = await supervisor.aget_state(config)
        if not state or not state.values:
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="error",
                message="?????????",
            )

        if body.action == "confirm":
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="confirmed",
                message="?????????? CodeAct ??...",
            )

        if body.action == "modify":
            if not body.feedback:
                return HITLConfirmResponse(
                    thread_id=body.thread_id,
                    status="error",
                    message="???????? feedback ??????",
                )
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="modified",
                message=f"???????: {body.feedback}",
            )

        if body.action == "restart":
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="restarted",
                message="??????????????????",
            )

        return HITLConfirmResponse(
            thread_id=body.thread_id,
            status="error",
            message=f"????: {body.action}",
        )
