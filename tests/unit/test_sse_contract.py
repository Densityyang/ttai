from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from src.nl2sql.api import stream_blocks


class _GraphState(TypedDict):
    messages: list[Any]


class _StreamingEngine:
    async def astream_events(self, messages, config, version):
        del messages, config, version
        yield {
            "event": "on_chain_end",
            "name": "nl2sql_v2_explicit",
            "parent_ids": [],
            "data": {"output": {"messages": [AIMessage(content="ok")]}},
        }


class _RootAwareStreamingEngine:
    async def astream_events(self, messages, config, version):
        del messages, config, version
        yield {
            "event": "on_chain_end",
            "name": None,
            "parent_ids": [],
            "data": {"output": {"messages": [AIMessage(content="unnamed-child")]}} ,
        }
        yield {
            "event": "on_chain_end",
            "name": "typed_runtime",
            "parent_ids": ["root-run"],
            "data": {"output": {"messages": [AIMessage(content="child")]}},
        }
        yield {
            "event": "on_chain_end",
            "name": "nl2sql_v2_explicit",
            "parent_ids": ["not-root"],
            "data": {"output": {"messages": [AIMessage(content="fake-root")]}},
        }
        yield {
            "event": "on_chain_end",
            "name": "nl2sql_v2_explicit",
            "parent_ids": [],
            "data": {
                "output": {
                    "response_blocks": [{"type": "text", "text": "root"}]
                }
            },
        }


class _ScriptedStreamingEngine:
    def __init__(self, *events):
        self.events = events

    async def astream_events(self, messages, config, version):
        del messages, config, version
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


def _root_event(output):
    return {
        "event": "on_chain_end",
        "name": "nl2sql_v2_explicit",
        "parent_ids": [],
        "data": {"output": output},
    }


def _parsed_events(chunks):
    parsed = []
    for chunk in chunks:
        lines = chunk.splitlines()
        event_name = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data_line = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        payload = data_line if data_line == "[DONE]" else json.loads(data_line)
        parsed.append((event_name, payload))
    return parsed


def _terminal_events(parsed):
    return [item for item in parsed if item[0] in {"done", "error"}]


def _real_state_graph(
    node: Callable[[_GraphState], dict[str, Any]],
) -> Any:
    graph = StateGraph(_GraphState)
    graph.add_node("work", node)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph.compile()


async def test_sse_events_have_resumable_ids_and_explicit_event_types() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _StreamingEngine(),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-1",
        )
    ]

    first_lines = chunks[0].splitlines()
    assert first_lines[0].startswith("id: query-")
    assert first_lines[0].endswith(":1")
    assert first_lines[1] == "event: block"
    payload = json.loads(first_lines[2].removeprefix("data: "))
    assert payload["id"] == first_lines[0].removeprefix("id: ")
    assert payload["thread_id"] == "thread-1"

    done_lines = chunks[-1].splitlines()
    assert done_lines[0].endswith(":done")
    assert done_lines[1] == "event: done"
    assert done_lines[2] == "data: [DONE]"


async def test_sse_ignores_child_chain_end_and_emits_server_metadata_first() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _RootAwareStreamingEngine(),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-1",
            {
                "metadata": {
                    "thread_id": "thread-1",
                    "run_id": "run-1",
                    "requested_mode": "BUILD",
                    "effective_mode": "BUILD",
                    "switched_from_run_id": None,
                    "authority_provenance": "local_real_demo",
                }
            },
        )
    ]

    assert chunks[0].splitlines()[1] == "event: metadata"
    metadata = json.loads(chunks[0].splitlines()[2].removeprefix("data: "))
    assert metadata["run_id"] == metadata["metadata"]["run_id"] == "run-1"
    assert metadata["effective_mode"] == "BUILD"
    assert len([chunk for chunk in chunks if "event: block" in chunk]) == 1
    assert '"text": "root"' in chunks[1]
    assert '"No response was produced."' not in "".join(chunks)
    assert chunks[-1].splitlines()[1] == "event: done"


async def test_sse_without_a_proven_root_emits_no_public_block() -> None:
    class _NoRoot:
        async def astream_events(self, messages, config, version):
            del messages, config, version
            yield {
                "event": "on_chain_end",
                "name": "typed_runtime",
                "parent_ids": ["root"],
                "data": {"output": {"messages": [AIMessage(content="child")]}},
            }

    chunks = [
        chunk
        async for chunk in stream_blocks(
            _NoRoot(), [{"role": "user", "content": "hello"}], {}, "thread-1"
        )
    ]
    assert all("event: block" not in chunk for chunk in chunks)
    assert chunks[-1].splitlines()[1] == "event: error"
    assert json.loads(chunks[-1].splitlines()[2].removeprefix("data: "))["code"] == (
        "stream_root_result_missing"
    )


async def test_sse_empty_root_cannot_promote_compatibility_fallback_to_success() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(_root_event({"messages": []})),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-empty-root",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["error"]
    assert parsed[0][1]["code"] == "stream_root_result_missing"
    assert "No response was produced." not in "".join(chunks)
    assert len(_terminal_events(parsed)) == 1


async def test_sse_root_chain_error_emits_one_safe_terminal() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(
                {
                    "event": "on_chain_error",
                    "name": "nl2sql_v2_explicit",
                    "parent_ids": [],
                    "data": {"error": "private"},
                },
            ),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-2",
            {
                "metadata": {
                    "thread_id": "thread-2",
                    "run_id": "run-2",
                    "requested_mode": "QUERY",
                    "effective_mode": "QUERY",
                    "switched_from_run_id": None,
                    "authority_provenance": "local_real_demo",
                }
            },
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["metadata", "error"]
    assert len(_terminal_events(parsed)) == 1
    name, payload = parsed[-1]
    assert name == "error"
    assert payload["code"] == "stream_engine_error"
    assert payload["thread_id"] == "thread-2"
    assert payload["id"].endswith(":error")
    assert "private" not in json.dumps(payload)


async def test_sse_duplicate_root_errors_emit_only_one_terminal() -> None:
    root_error = {
        "event": "on_chain_error",
        "name": "nl2sql_v2_explicit",
        "parent_ids": [],
        "data": {"error": "private"},
    }
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(root_error, root_error),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-duplicate-error",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["error"]
    assert parsed[0][1]["code"] == "stream_engine_error"
    assert len(_terminal_events(parsed)) == 1


async def test_sse_root_error_stops_a_misleading_root_success() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(
                {
                    "event": "on_chain_error",
                    "name": "nl2sql_v2_explicit",
                    "parent_ids": [],
                    "data": {"error": "private"},
                },
                _root_event(
                    {"response_blocks": [{"type": "text", "text": "misleading"}]}
                ),
            ),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-no-false-success",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["error"]
    assert parsed[0][1]["code"] == "stream_engine_error"
    assert "misleading" not in "".join(chunks)
    assert len(_terminal_events(parsed)) == 1


async def test_sse_child_chain_error_does_not_preempt_a_valid_root_result() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(
                {
                    "event": "on_chain_error",
                    "name": "typed_runtime",
                    "parent_ids": ["root-run"],
                    "data": {"error": "handled child failure"},
                },
                _root_event(
                    {"response_blocks": [{"type": "text", "text": "root recovered"}]}
                ),
            ),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-child-error",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["block", "done"]
    assert parsed[0][1]["block"]["text"] == "root recovered"
    assert parsed[1][1] == "[DONE]"
    assert len(_terminal_events(parsed)) == 1


async def test_sse_exception_before_root_emits_safe_error_without_exception_text() -> None:
    secret_detail = "database password must never leak"
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(RuntimeError(secret_detail)),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-3",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["error"]
    assert parsed[0][1]["code"] == "stream_engine_error"
    assert secret_detail not in "".join(chunks)
    assert len(_terminal_events(parsed)) == 1


async def test_sse_exception_after_partial_blocks_preserves_blocks_and_errors() -> None:
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(
                _root_event(
                    {"response_blocks": [{"type": "text", "text": "partial result"}]}
                ),
                RuntimeError("late internal failure"),
            ),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-4",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["block", "error"]
    assert parsed[0][1]["block"]["text"] == "partial result"
    assert parsed[1][1]["code"] == "stream_engine_error"
    assert len(_terminal_events(parsed)) == 1


async def test_sse_duplicate_root_completion_emits_blocks_once_and_one_done() -> None:
    root = _root_event({"response_blocks": [{"type": "text", "text": "once"}]})
    chunks = [
        chunk
        async for chunk in stream_blocks(
            _ScriptedStreamingEngine(root, root),
            [{"role": "user", "content": "hello"}],
            {},
            "thread-5",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["block", "done"]
    assert parsed[0][1]["block"]["text"] == "once"
    assert parsed[1][1] == "[DONE]"
    assert len(_terminal_events(parsed)) == 1


async def test_sse_cancelled_stream_propagates_without_terminal_event() -> None:
    stream = stream_blocks(
        _ScriptedStreamingEngine(asyncio.CancelledError()),
        [{"role": "user", "content": "hello"}],
        {},
        "thread-6",
    )

    with pytest.raises(asyncio.CancelledError):
        [chunk async for chunk in stream]


async def test_sse_real_langgraph_root_completion_keeps_success_protocol() -> None:
    def answer_node(_state: _GraphState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="actual LangGraph answer")]}

    chunks = [
        chunk
        async for chunk in stream_blocks(
            _real_state_graph(answer_node),
            [{"role": "user", "content": "hello"}],
            {"run_name": "nl2sql_v2_explicit"},
            "thread-real-graph",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["block", "done"]
    assert parsed[0][1]["block"]["text"] == "actual LangGraph answer"
    assert parsed[1][1] == "[DONE]"


async def test_sse_real_langgraph_exception_becomes_safe_error_terminal() -> None:
    secret_detail = "private provider failure detail"

    def failing_node(_state: _GraphState) -> dict[str, Any]:
        raise RuntimeError(secret_detail)

    chunks = [
        chunk
        async for chunk in stream_blocks(
            _real_state_graph(failing_node),
            [{"role": "user", "content": "hello"}],
            {"run_name": "nl2sql_v2_explicit"},
            "thread-real-error",
        )
    ]

    parsed = _parsed_events(chunks)
    assert [name for name, _ in parsed] == ["error"]
    assert parsed[0][1]["code"] == "stream_engine_error"
    assert secret_detail not in "".join(chunks)
    assert len(_terminal_events(parsed)) == 1
