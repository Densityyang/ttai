"""全局可观测性工具 - 统一的监控封装（含 CodeAct 审计扩展）"""

import logging
from collections.abc import Generator
from typing import Any

from langchain_core.runnables import Runnable, RunnableConfig

from src.nl2sql.infra.observer.langfuse import get_langfuse_handler
from src.nl2sql.observability.sink_policy import LANGFUSE_SINK, build_sink_envelope

logger = logging.getLogger(__name__)


def create_monitored_config(
    session_id: str | None = None,
    base_config: RunnableConfig | None = None,
    run_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunnableConfig:
    """创建带监控的运行配置

    Args:
        session_id: 会话 ID，如果为 None 则自动生成
        base_config: 基础配置，会与监控配置合并
        run_name: 运行名称
        metadata: 额外的元数据（如 RAG 评分、修复历史等）

    Returns:
        包含监控回调的运行配置
    """
    config: RunnableConfig = base_config or {}
    if run_name and not config.get("run_name"):
        config = {
            **config,
            "run_name": run_name,
        }

    existing_metadata = config.get("metadata")
    caller_metadata = {
        **(existing_metadata if isinstance(existing_metadata, dict) else {}),
        **dict(metadata or {}),
    }
    # Trusted run identity stays on the always-safe channel.  Langfuse links a
    # trace to its session via the "langfuse_session_id" metadata key read at
    # the root chain, so it is emitted here instead of on a handler attribute.
    safe_metadata = {"langfuse_session_id": session_id} if session_id else {}
    if caller_metadata or safe_metadata:
        # R4: ALL caller-controlled metadata that can reach the Langfuse
        # callback is routed through the SAME policy envelope -- INCLUDING
        # metadata supplied through base_config.  Raw content is attached only
        # under the explicit deployment-level raw-content observability policy,
        # and the egress boundary scrubs technical secrets either way.
        envelope = build_sink_envelope(
            LANGFUSE_SINK, metadata=safe_metadata, content=caller_metadata
        )
        config = {**config, "metadata": envelope.as_record()}

    langfuse_handler = get_langfuse_handler()
    if langfuse_handler:
        existing_callbacks = config.get("callbacks")
        if existing_callbacks is None:
            callbacks = [langfuse_handler]
        elif isinstance(existing_callbacks, list):
            callbacks = [*existing_callbacks, langfuse_handler]
        else:
            callbacks = [langfuse_handler]

        config = {
            **config,
            "callbacks": callbacks,
        }

    return config


def stream_with_monitoring(
    runnable: Runnable[Any, Any],
    input_data: Any,
    session_id: str | None = None,
    config: RunnableConfig | None = None,
    stream_mode: str = "values",
) -> Generator[Any, None, None]:
    """使用监控执行流式运行

    Args:
        runnable: 要执行的 Runnable 对象
        input_data: 输入数据
        session_id: 会话 ID，如果为 None 则自动生成
        config: 额外的运行配置
        stream_mode: 流式模式

    Yields:
        运行的每一步结果
    """
    monitored_config = create_monitored_config(session_id, config)

    for step in runnable.stream(input_data, monitored_config, stream_mode=stream_mode):
        yield step


def invoke_with_monitoring(
    runnable: Runnable[Any, Any],
    input_data: Any,
    session_id: str | None = None,
    config: RunnableConfig | None = None,
) -> Any:
    """使用监控执行单次调用

    Args:
        runnable: 要执行的 Runnable 对象
        input_data: 输入数据
        session_id: 会话 ID，如果为 None 则自动生成
        config: 额外的运行配置

    Returns:
        运行结果
    """
    monitored_config = create_monitored_config(session_id, config)
    return runnable.invoke(input_data, monitored_config)
