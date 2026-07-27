"""全局可观测性工具 - 统一的监控封装（含 CodeAct 审计扩展）"""

import logging
from collections.abc import Generator
from typing import Any
from uuid import uuid4

from langchain_core.runnables import Runnable, RunnableConfig

from src.nl2sql.infra.observer.langfuse import get_langfuse_handler

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

    if metadata:
        existing_metadata = config.get("metadata", {})
        config = {
            **config,
            "metadata": {**(existing_metadata or {}), **metadata},
        }


    langfuse_handler = get_langfuse_handler()
    if langfuse_handler:
        if session_id is None:
            session_id = str(uuid4())
        langfuse_handler.session_id = session_id  # type: ignore[attr-defined]

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
