"""NL2SQL API 服务

入口路由到 Supervisor Agent，由 Supervisor 协调 nl2sql、chart 等子 Agent。
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

# 全局状态
_supervisor: Any | None = None
_routes_registered = False


# === 历史查询响应模型 ===


class MessageItem(BaseModel):
    """单条消息"""

    role: str
    content: str


class StateSnapshot(BaseModel):
    """状态快照"""

    thread_id: str
    checkpoint_id: str
    messages: list[MessageItem]
    created_at: str | None = None


class ThreadHistoryResponse(BaseModel):
    """会话历史响应"""

    thread_id: str
    snapshots: list[StateSnapshot]


def _resolve_thread_id(config: dict[str, Any], request: Any) -> str:
    """解析 thread_id，优先级：header > query > body > 生成"""
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
    """注入运行时配置"""
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
    """注册 Supervisor 路由"""
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
    """Supervisor 路由的生命周期管理"""
    global _supervisor, _routes_registered

    core_settings = get_core_settings()
    if core_settings.skip_rag_startup_sync:
        logger.warning(
            "SKIP_RAG_STARTUP_SYNC=true（环境变量），跳过启动阶段 QA/Semantic 索引同步；"
            "评测前请恢复为 false 并配置可用 EMBEDDING_* 或预置 .vector_store"
        )
    else:
        logger.info("开始检查 RAG 索引状态...")
        try:
            result = await asyncio.to_thread(sync_qa_index)
            if result.changed:
                logger.info(
                    f"✓ QA RAG 索引已更新: {result.total_qas} 条 QA, "
                    f"{result.total_chunks} 个分片 | {result.reason}"
                )
            else:
                logger.info(
                    f"✓ QA RAG 索引已是最新: {result.total_qas} 条 QA, "
                    f"{result.total_chunks} 个分片 | {result.reason}"
                )
        except Exception as e:
            logger.error(f"✗ QA RAG 索引同步失败: {e}", exc_info=True)
            if core_settings.rag_startup_sync_strict:
                raise
            logger.warning(
                "RAG_STARTUP_SYNC_STRICT=false，忽略 QA 索引同步错误并继续启动（检索能力可能受限）"
            )

        try:
            sem_result = await asyncio.to_thread(sync_semantic_index)
            if sem_result.changed:
                logger.info(
                    f"✓ Semantic RAG 索引已更新: {sem_result.total_chunks} 个分片 | {sem_result.reason}"
                )
            else:
                logger.info(
                    f"✓ Semantic RAG 索引已是最新: {sem_result.total_chunks} 个分片 | {sem_result.reason}"
                )
        except Exception as e:
            logger.error(f"✗ Semantic RAG 索引同步失败: {e}", exc_info=True)
            if core_settings.rag_startup_sync_strict:
                raise
            logger.warning(
                "RAG_STARTUP_SYNC_STRICT=false，忽略 Semantic 索引同步错误并继续启动（检索能力可能受限）"
            )

    # 启动时初始化数据库连接（使用 AgentConfig 作为 schema 唯一来源）
    from src.nl2sql.infra.store.database import get_db_manager

    agent_config = get_agent_config()
    await get_db_manager(schema=agent_config.nl2sql_db_schema)
    await warmup_runtime()

    # 初始化 checkpointer
    checkpointer_manager = get_checkpointer_manager()
    await checkpointer_manager.init()

    # 创建 Supervisor（单例）
    if _supervisor is None:
        from src.nl2sql.supervisor.agent import create_supervisor

        _supervisor = await create_supervisor(checkpointer_manager.checkpointer)

    # 注册路由
    if not _routes_registered:
        _register_supervisor_routes(app, _supervisor)
        register_history_routes(app)
        register_chat_completions_routes(app)
        register_hitl_routes(app)
        _routes_registered = True

    yield

    # 关闭时清理
    await checkpointer_manager.close()

    from src.nl2sql.infra.store.database import close_db_manager

    await close_db_manager()


def _extract_messages(state_values: dict[str, Any]) -> list[MessageItem]:
    """从状态值中提取消息列表"""
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
    """获取 Supervisor 实例"""
    if _supervisor is None:
        raise RuntimeError("Supervisor 未初始化")
    return _supervisor


def register_history_routes(app: FastAPI) -> None:
    """注册历史查询路由"""

    @app.get(
        "/threads/{thread_id}/history",
        response_model=ThreadHistoryResponse,
        dependencies=[Depends(require_nl2sql_permission)],
        summary="获取会话历史",
        description="获取指定 thread_id 的完整对话历史，包含所有状态快照",
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
        summary="获取会话最新状态",
        description="获取指定 thread_id 的最新状态快照",
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


# === OpenAI 风格 Chat Completions API ===


class ChatCompletionRequest(BaseModel):
    """Chat Completions 请求"""

    messages: list[dict[str, str]]
    stream: bool = False
    thread_id: str | None = None


class ChatMessage(BaseModel):
    """Chat 消息"""

    role: Literal["assistant"] = "assistant"
    content: list[dict[str, Any]]


class ChatChoice(BaseModel):
    """Chat 选择项"""

    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop"] = "stop"


class ChatCompletionResponse(BaseModel):
    """Chat Completions 响应"""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    choices: list[ChatChoice]
    thread_id: str


class ChatCompletionChunk(BaseModel):
    """Chat Completions 流式块"""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    choices: list[dict[str, Any]]
    thread_id: str


def register_chat_completions_routes(app: FastAPI) -> None:
    """注册 OpenAI 风格的 Chat Completions 路由"""

    @app.post(
        "/nl2sql/chat/completions",
        response_model=None,  # 流式和非流式响应类型不同
        dependencies=[Depends(require_nl2sql_permission)],
        summary="Chat Completions API",
        description="OpenAI 风格的 Chat Completions API，支持流式和非流式响应",
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

        # 注入认证信息
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

        # 构建输入消息
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

        # 非流式处理
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
    """从 supervisor 结果中提取 blocks"""
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
        # 纯对话场景：提取最后一条 AI 消息
        for msg in reversed(messages):
            msg_type = getattr(msg, "type", None)
            if msg_type == "ai":
                content = getattr(msg, "content", None)
                if isinstance(content, str):
                    return serialize_response_blocks([TextBlock(text=content)])

    return serialize_response_blocks([TextBlock(text="未获取到响应")])


async def _stream_chat_completion(
    supervisor: Any,
    input_messages: list[dict[str, str]],
    config: RunnableConfig,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    """流式返回 Chat Completions"""
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
        # 发送第一个事件时，立即返回一个空 delta，让前端知道流已开始
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

        # 结构化输出完成时，发送所有 blocks
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
                # 记录最后一次不含 structured_response 的顶层输出，供 fallback 使用
                fallback_output = output

    # 某些路径没有 structured_response，仅在 messages 中返回文本
    if not blocks_sent and fallback_output is not None:
        for block_dict in _extract_blocks_from_result(fallback_output):
            yield _make_block_chunk(block_dict)

    # 发送结束标记
    final_chunk = ChatCompletionChunk(
        id=completion_id,
        created=created,
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        thread_id=thread_id,
    )
    yield f"data: {final_chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


# === HITL 确认/修改 API ===


class HITLConfirmRequest(BaseModel):
    """HITL 确认请求"""

    thread_id: str
    action: Literal["confirm", "modify", "restart"]
    feedback: str | None = None


class HITLConfirmResponse(BaseModel):
    """HITL 确认响应"""

    thread_id: str
    status: str
    plan_summary: str | None = None
    message: str | None = None


def register_hitl_routes(app: FastAPI) -> None:
    """注册 HITL 计划确认相关路由"""

    @app.post(
        "/nl2sql/hitl/action",
        response_model=HITLConfirmResponse,
        dependencies=[Depends(require_nl2sql_permission)],
        summary="HITL 计划确认/修改/重启",
        description=(
            "对 CodeAct Engine 生成的计算计划执行操作。\n"
            "- confirm: 确认当前计划，锁定并进入 CodeAct 执行\n"
            "- modify: 修改计划（需提供 feedback 描述修改内容）\n"
            "- restart: 丢弃当前计划，重新描述"
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
                message="未找到对应会话状态",
            )

        if body.action == "confirm":
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="confirmed",
                message="计划已确认，正在进入 CodeAct 执行...",
            )

        if body.action == "modify":
            if not body.feedback:
                return HITLConfirmResponse(
                    thread_id=body.thread_id,
                    status="error",
                    message="修改操作需要提供 feedback 描述修改内容",
                )
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="modified",
                message=f"已收到修改意见: {body.feedback}",
            )

        if body.action == "restart":
            return HITLConfirmResponse(
                thread_id=body.thread_id,
                status="restarted",
                message="计划已丢弃，请重新描述您的计算需求。",
            )

        return HITLConfirmResponse(
            thread_id=body.thread_id,
            status="error",
            message=f"未知操作: {body.action}",
        )
