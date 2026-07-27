"""Langfuse 可观测性集成（含 CodeAct 审计扩展）"""

import logging
import warnings
from functools import lru_cache
from typing import Any, cast

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from src.core.settings import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def _init_langfuse_client() -> Langfuse | None:
    """初始化 Langfuse 客户端（进程内单例）。"""
    settings = get_settings()

    if not settings.langfuse_enabled:
        return None

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    try:
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            timeout=settings.langfuse_timeout,
        )

        if not client.auth_check():
            warnings.warn(
                "Langfuse 凭证或服务地址无效，已自动禁用 Langfuse 回调。"
                "请检查 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST。",
                stacklevel=2,
            )
            return None

        return client
    except Exception as e:
        warnings.warn(f"Langfuse 初始化失败，已自动禁用: {e}", stacklevel=2)
        return None


def get_langfuse_handler() -> BaseCallbackHandler | None:
    """获取 Langfuse 回调处理器，未配置或未启用则返回 None"""
    settings = get_settings()

    if not settings.langfuse_enabled:
        return None

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    client = _init_langfuse_client()
    if client is None:
        return None
    return CallbackHandler(public_key=settings.langfuse_public_key)


# ── 审计 Trace 扩展 ──────────────────────────────────────────────────────────


def trace_rag_evidences(
    trace_id: str,
    evidences: list[dict[str, Any]],
    rewrite_history: list[str] | None = None,
) -> None:
    """记录 RAG 检索证据及评分到 Langfuse trace。"""
    client = _init_langfuse_client()
    if client is None:
        return

    try:
        trace = cast(Any, client).trace(id=trace_id)
        trace.event(
            name="rag_evidences",
            metadata={
                "evidences": evidences,
                "rewrite_history": rewrite_history or [],
                "evidence_count": len(evidences),
                "avg_score": (
                    sum(e.get("score", 0) for e in evidences) / len(evidences)
                    if evidences
                    else 0
                ),
            },
        )
    except Exception as e:
        logger.debug("记录 RAG 证据到 Langfuse 失败: %s", e)


def trace_sql_repair(
    trace_id: str,
    original_sql: str,
    repair_history: list[dict[str, Any]],
) -> None:
    """记录 SQL 修复历史到 Langfuse trace。"""
    client = _init_langfuse_client()
    if client is None:
        return

    try:
        trace = cast(Any, client).trace(id=trace_id)
        trace.event(
            name="sql_repair_history",
            metadata={
                "original_sql": original_sql,
                "repair_rounds": len(repair_history),
                "repairs": repair_history,
            },
        )
    except Exception as e:
        logger.debug("记录 SQL 修复历史到 Langfuse 失败: %s", e)


def trace_code_execution(
    trace_id: str,
    code: str,
    result: dict[str, Any],
    elapsed_ms: float,
) -> None:
    """记录代码执行日志到 Langfuse trace。"""
    client = _init_langfuse_client()
    if client is None:
        return

    try:
        trace = cast(Any, client).trace(id=trace_id)
        trace.event(
            name="code_execution",
            metadata={
                "code": code[:2000],
                "success": result.get("success", False),
                "elapsed_ms": elapsed_ms,
                "error": result.get("error"),
                "result_preview": str(result.get("result", ""))[:500],
            },
        )
    except Exception as e:
        logger.debug("记录代码执行日志到 Langfuse 失败: %s", e)


def trace_routing_decision(
    trace_id: str,
    question: str,
    route: str,
    reason: str = "",
) -> None:
    """记录 Supervisor 路由决策到 Langfuse trace。"""
    client = _init_langfuse_client()
    if client is None:
        return

    try:
        trace = cast(Any, client).trace(id=trace_id)
        trace.event(
            name="routing_decision",
            metadata={
                "question": question,
                "route": route,
                "reason": reason,
            },
        )
    except Exception as e:
        logger.debug("记录路由决策到 Langfuse 失败: %s", e)
