"""FastAPI lifecycle and response helpers shared by the v2 API boundary."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from langchain_core.runnables import RunnableConfig


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own runtime dependencies through the FastAPI application lifecycle.

    Database migrations and RAG/index rebuilds are explicitly external jobs; this
    lifespan only prepares the state persistence required for serving requests.
    """

    from src.nl2sql.container import AppContainer

    container = AppContainer()
    app.state.container = container
    await container.start()
    try:
        yield
    finally:
        await container.close()


def extract_messages(state_values: dict[str, Any]) -> list[dict[str, str]]:
    """Serialize LangChain messages without exposing implementation objects."""

    messages = state_values.get("messages", [])
    if not isinstance(messages, list):
        return []
    result: list[dict[str, str]] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            role = getattr(message, "type", None) or type(message).__name__.lower()
            result.append({"role": str(role), "content": content})
    return result


def extract_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract public response blocks from a supervisor result."""

    from src.nl2sql.supervisor.schemas import (
        ClarificationBlock,
        ModeSuggestionBlock,
        SupervisorResponse,
        TextBlock,
        serialize_response_blocks,
    )

    response_blocks = result.get("response_blocks")
    if isinstance(response_blocks, list) and response_blocks:
        return serialize_response_blocks(response_blocks)

    # A typed mode-capability outcome takes precedence: it is a first-class
    # product block that reports a deterministic QUERY capability limitation
    # and offers an explicit switch.  It is produced WITHOUT any model call.
    capability = result.get("mode_capability_outcome")
    if isinstance(capability, dict) and capability.get("suggested_mode"):
        envelope = result.get("run_envelope")
        run_id = envelope.get("run_id") if isinstance(envelope, dict) else None
        block = ModeSuggestionBlock.model_validate(
            {
                "current_mode": capability.get("effective_mode"),
                "suggested_mode": capability.get("suggested_mode"),
                "outcome": capability.get("outcome"),
                "reason": "Deterministic QUERY capability cannot resolve this request.",
                "run_id": run_id,
            }
        )
        return serialize_response_blocks([block])
    pending = result.get("pending_decision")
    if isinstance(pending, dict):
        from src.nl2sql.orchestration.decision_contract import revalidate_request

        try:
            request = revalidate_request(pending)
        except Exception:
            request = None
        if request is not None:
            return serialize_response_blocks(
                [
                    ClarificationBlock(
                        request_id=request.request_id,
                        decision_kind=request.decision_kind,
                        version=request.version,
                        allowed_actions=request.allowed_actions,
                        unresolved_slots=request.unresolved_slots,
                        issue_codes=request.issue_codes,
                        safe_summary=request.safe_summary,
                    )
                ]
            )
    structured: SupervisorResponse | None = result.get("structured_response")
    if isinstance(structured, dict):
        structured = SupervisorResponse.model_validate(structured)
    if structured is not None and structured.blocks:
        return serialize_response_blocks(structured.blocks)
    messages = result.get("messages", [])
    if isinstance(messages, list):
        for message in reversed(messages):
            if getattr(message, "type", None) == "ai" and isinstance(
                content := getattr(message, "content", None), str
            ):
                return serialize_response_blocks([TextBlock(text=content)])
    return serialize_response_blocks([TextBlock(text="No response was produced.")])


def _has_streamable_root_result(result: dict[str, Any]) -> bool:
    """Return whether a root output contains a real public response source.

    ``extract_blocks`` keeps its compatibility fallback for non-streaming
    callers.  The SSE terminal protocol must not treat that fallback as a
    successful graph result.
    """

    response_blocks = result.get("response_blocks")
    if isinstance(response_blocks, list) and response_blocks:
        return True

    capability = result.get("mode_capability_outcome")
    if isinstance(capability, Mapping) and capability.get("suggested_mode"):
        return True

    pending = result.get("pending_decision")
    if isinstance(pending, Mapping):
        from src.nl2sql.orchestration.decision_contract import revalidate_request

        try:
            revalidate_request(pending)
        except Exception:
            pass
        else:
            return True

    structured = result.get("structured_response")
    structured_blocks = (
        structured.get("blocks")
        if isinstance(structured, Mapping)
        else getattr(structured, "blocks", None)
    )
    if isinstance(structured_blocks, (list, tuple)) and structured_blocks:
        return True

    messages = result.get("messages")
    if isinstance(messages, list):
        return any(
            getattr(message, "type", None) == "ai"
            and isinstance(content := getattr(message, "content", None), str)
            and bool(content.strip())
            for message in messages
        )
    return False


def _sse_error_frame(
    stream_id: str, created: int, thread_id: str, code: str
) -> str:
    """Build a safe, stable terminal error without internal exception data."""

    event_id = f"{stream_id}:error"
    payload = {
        "id": event_id,
        "created": created,
        "thread_id": thread_id,
        "code": code,
    }
    return (
        f"id: {event_id}\n"
        "event: error\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


async def stream_blocks(
    supervisor: Any,
    input_messages: list[dict[str, str]],
    config: RunnableConfig,
    thread_id: str,
    extra_input: Mapping[str, object] | None = None,
) -> AsyncGenerator[str, None]:
    """Emit a compact SSE stream while preserving proxy-safe framing."""

    stream_id = f"query-{uuid4().hex}"
    created = int(time.time())
    sent_blocks = False
    metadata = None
    # Extra graph INPUT kept separate from the conversation messages, so the
    # server-owned run envelope reaches the engine on the streaming path too.
    graph_input: dict[str, object] = {"messages": input_messages}
    if extra_input:
        for key, value in extra_input.items():
            if key in {"metadata", "stream_metadata"}:
                if isinstance(value, Mapping):
                    metadata = dict(value)
                continue
            graph_input[key] = value
    if metadata is not None:
        # Metadata is a first-class product event, not hidden in the terminal
        # marker.  The caller owns the server-derived values; this helper only
        # frames them and preserves the exact required vocabulary.
        required = (
            "thread_id",
            "run_id",
            "requested_mode",
            "effective_mode",
            "switched_from_run_id",
            "authority_provenance",
        )
        event_id = f"{stream_id}:metadata"
        metadata_payload = {name: metadata.get(name) for name in required}
        payload = {
            "id": event_id,
            "created": created,
            "thread_id": thread_id,
            "metadata": metadata_payload,
            **metadata_payload,
        }
        yield (
            f"id: {event_id}\n"
            "event: metadata\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        )
    try:
        event_stream = supervisor.astream_events(
            graph_input, config, version="v2"
        )
        async with aclosing(event_stream):
            async for event in event_stream:
                if not isinstance(event, Mapping):
                    continue
                parent_ids = event.get("parent_ids")
                is_proven_root = (
                    event.get("name") == "nl2sql_v2_explicit"
                    and isinstance(parent_ids, (list, tuple))
                    and not parent_ids
                )
                # Only a proven root failure invalidates the whole request. A child
                # chain may report a handled error and still produce a valid root
                # result, so child errors must not preempt that completion.
                if event.get("event") == "on_chain_error" and is_proven_root:
                    yield _sse_error_frame(
                        stream_id, created, thread_id, "stream_engine_error"
                    )
                    return
                if event.get("event") != "on_chain_end" or sent_blocks:
                    continue
                # LangGraph v2 root completion is the only event allowed to project
                # final product blocks. Missing metadata cannot prove root output.
                if not is_proven_root:
                    continue
                event_data = event.get("data")
                output = event_data.get("output") if isinstance(event_data, Mapping) else None
                if not isinstance(output, dict) or not _has_streamable_root_result(output):
                    continue
                try:
                    blocks = extract_blocks(output)
                except Exception:
                    yield _sse_error_frame(
                        stream_id, created, thread_id, "stream_output_invalid"
                    )
                    return
                if not blocks:
                    yield _sse_error_frame(
                        stream_id, created, thread_id, "stream_output_invalid"
                    )
                    return
                for sequence, block in enumerate(blocks, start=1):
                    event_id = f"{stream_id}:{sequence}"
                    payload = {
                        "id": event_id,
                        "created": created,
                        "thread_id": thread_id,
                        "block": block,
                    }
                    yield (
                        f"id: {event_id}\n"
                        "event: block\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                sent_blocks = True
    except asyncio.CancelledError:
        raise
    except Exception:
        # Keep exception strings and implementation details out of the public
        # stream. Previously emitted blocks remain usable but are not success.
        yield _sse_error_frame(stream_id, created, thread_id, "stream_engine_error")
        return

    if sent_blocks:
        yield f"id: {stream_id}:done\nevent: done\ndata: [DONE]\n\n"
    else:
        yield _sse_error_frame(
            stream_id, created, thread_id, "stream_root_result_missing"
        )
