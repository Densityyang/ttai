from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from src.nl2sql.api import stream_blocks


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
    assert chunks[-1].splitlines()[1] == "event: done"
