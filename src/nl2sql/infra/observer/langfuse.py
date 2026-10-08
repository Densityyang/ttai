"""Langfuse 可观测性集成（含 CodeAct 审计扩展）"""

import logging
import warnings
from collections.abc import Sequence
from functools import lru_cache
from typing import Any, cast

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langfuse.types import (
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanIdentifier,
    OtelSpanPatch,
)

from src.core.settings import get_settings
from src.nl2sql.observability.content_policy import scrub_value
from src.nl2sql.observability.secret_source import observability_secret_values
from src.nl2sql.observability.sink_policy import (
    LANGFUSE_SINK,
    build_sink_envelope,
    raw_content_observability_enabled,
)

logger = logging.getLogger(__name__)

# R5 SOURCE-FACT (langfuse==4.14.1 langchain/CallbackHandler.py): on_chain_start,
# __on_llm_action and on_llm_end write the raw prompts, raw model outputs and
# caller metadata straight into the Langfuse OTel observation attributes below.
# CallbackHandler.__init__ accepts ONLY public_key/trace_context and exposes NO
# capture control, so raw capture is closed at the supported export-stage
# mask_otel_spans hook: it deletes the raw input/output/status_message
# attributes unless raw observability is explicitly enabled and scrubs
# technical secrets either way.  Operational metadata that survives carries no
# raw caller content, because create_monitored_config envelopes it first.
_RAW_CONTENT_ATTRIBUTE_PREFIXES = (
    "langfuse.trace.input",
    "langfuse.trace.output",
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.observation.status_message",
    # Defense-in-depth for any OpenTelemetry LLM instrumentor that may be added
    # later; none is installed today, so these never fire on the current stack.
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "llm.input_messages",
    "llm.output_messages",
    "ai.prompt",
    "ai.response",
    "traceloop.entity.input",
    "traceloop.entity.output",
    "mlflow.spanInputs",
    "mlflow.spanOutputs",
    "input.value",
    "output.value",
)


def mask_langfuse_spans(
    *,
    params: MaskOtelSpansParams,
    secret_values: Sequence[str] | None = None,
    raw_content_enabled: bool | None = None,
) -> MaskOtelSpansResult | None:
    """Export-stage technical-secret mask for the Langfuse sink (R5).

    Runs on the SUPPORTED langfuse==4.14.1 mask_otel_spans hook, after the SDK
    has serialized an observation, so it inspects the exact span attributes that
    would leave for Langfuse:

    * a technical credential shape or configured system secret value is scrubbed
      from every string attribute, under either raw toggle;
    * raw prompt/answer/error attributes are REMOVED unless the deployment
      explicitly enabled raw content observability.

    The metadata channel is deliberately NOT dropped: it is gated upstream by
    the approved SinkEnvelope (create_monitored_config + the trace helpers), so
    only trusted safe metadata reaches it while raw caller metadata is dropped
    or scrubbed before it ever becomes an attribute.
    """

    secrets = (
        observability_secret_values() if secret_values is None else tuple(secret_values)
    )
    raw_enabled = (
        raw_content_observability_enabled()
        if raw_content_enabled is None
        else raw_content_enabled
    )
    patches: dict[OtelSpanIdentifier, OtelSpanPatch] = {}
    for identifier, span in params.spans.items():
        delete_keys: list[str] = []
        set_attributes: dict[str, Any] = {}
        for key, value in span.attributes.items():
            if not raw_enabled and _is_raw_content_attribute(key):
                delete_keys.append(key)
                continue
            scrubbed = _scrub_attribute(value, secrets)
            if scrubbed is not value:
                set_attributes[key] = scrubbed
        if delete_keys or set_attributes:
            patches[identifier] = OtelSpanPatch(
                delete_attributes=tuple(delete_keys),
                set_attributes=set_attributes,
            )
    if not patches:
        return None
    return MaskOtelSpansResult(span_patches=patches)


def _is_raw_content_attribute(key: str) -> bool:
    return any(
        key == prefix or key.startswith(prefix + ".")
        for prefix in _RAW_CONTENT_ATTRIBUTE_PREFIXES
    )


def _scrub_attribute(value: Any, secret_values: Sequence[str]) -> Any:
    # Recurse through structured attribute values (and mapping keys) so a
    # technical secret cannot hide inside a nested attribute.
    scrubbed = scrub_value(value, secret_values=secret_values)
    return scrubbed if scrubbed != value else value


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
            # R5: supported export-stage mask -- scrub technical secrets from
            # every exported attribute and drop raw prompt/answer attributes
            # unless raw observability is explicitly enabled.
            mask_otel_spans=mask_langfuse_spans,
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
    """返回受治理的 Langfuse 回调处理器；未启用或未配置则返回 None。

    R5: the pinned CallbackHandler captures raw prompts/answers and offers no
    capture control, so the raw-content policy is enforced at the export
    boundary by mask_langfuse_spans (configured on the client in
    _init_langfuse_client): raw input/output/status_message attributes are
    deleted unless raw observability is explicitly enabled, and technical
    secrets are scrubbed either way.  The handler is therefore attached
    whenever Langfuse is enabled and configured, so safe operational telemetry
    (span structure, model, usage, latency, level) is preserved by default.
    """

    settings = get_settings()

    if not settings.langfuse_enabled:
        return None

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    client = _init_langfuse_client()
    if client is None:
        return None
    return _new_callback_handler(settings.langfuse_public_key)


def _new_callback_handler(public_key: str) -> BaseCallbackHandler:
    """Seam so the attachment decision is testable without a live client."""

    return CallbackHandler(public_key=public_key)


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
        envelope = build_sink_envelope(
            LANGFUSE_SINK,
            metadata={
                "evidence_count": len(evidences),
                "avg_score": (
                    sum(e.get("score", 0) for e in evidences) / len(evidences)
                    if evidences
                    else 0
                ),
            },
            content={
                "evidences": evidences,
                "rewrite_history": rewrite_history or [],
            },
        )
        trace.event(name="rag_evidences", metadata=envelope.as_record())
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
        envelope = build_sink_envelope(
            LANGFUSE_SINK,
            metadata={"repair_rounds": len(repair_history)},
            content={"original_sql": original_sql, "repairs": repair_history},
        )
        trace.event(name="sql_repair_history", metadata=envelope.as_record())
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
        envelope = build_sink_envelope(
            LANGFUSE_SINK,
            metadata={
                "success": result.get("success", False),
                "elapsed_ms": elapsed_ms,
            },
            content={
                "code": code[:2000],
                "error": result.get("error"),
                "result_preview": str(result.get("result", ""))[:500],
            },
        )
        trace.event(name="code_execution", metadata=envelope.as_record())
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
        envelope = build_sink_envelope(
            LANGFUSE_SINK,
            metadata={"route": route},
            content={"question": question, "reason": reason},
        )
        trace.event(name="routing_decision", metadata=envelope.as_record())
    except Exception as e:
        logger.debug("记录路由决策到 Langfuse 失败: %s", e)
