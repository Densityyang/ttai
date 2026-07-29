"""FastAPI lifecycle and response helpers shared by the v2 API boundary."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
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
        SupervisorResponse,
        TextBlock,
        serialize_response_blocks,
    )

    structured: SupervisorResponse | None = result.get("structured_response")
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


async def stream_blocks(
    supervisor: Any,
    input_messages: list[dict[str, str]],
    config: RunnableConfig,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    """Emit a compact SSE stream while preserving proxy-safe framing."""

    stream_id = f"query-{uuid4().hex}"
    created = int(time.time())
    sent_blocks = False
    async for event in supervisor.astream_events(
        {"messages": input_messages}, config, version="v2"
    ):
        if event.get("event") != "on_chain_end" or sent_blocks:
            continue
        output = event.get("data", {}).get("output")
        if not isinstance(output, dict):
            continue
        for sequence, block in enumerate(extract_blocks(output), start=1):
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
    yield f"id: {stream_id}:done\nevent: done\ndata: [DONE]\n\n"
