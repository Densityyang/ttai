from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from src.nl2sql.api import stream_blocks


class _StreamingEngine:
    async def astream_events(self, messages, config, version):
        del messages, config, version
        yield {
            "event": "on_chain_end",
            "data": {"output": {"messages": [AIMessage(content="ok")]}},
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
