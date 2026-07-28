from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.engine import create_v2_engine


class _TraceSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_explicit_engine_records_route_and_receipt_without_supervisor_loop() -> None:
    provider = FakeProvider(
        {
            "small": ProviderResponse(
                content="Revenue plan", model="small", usage={"input_tokens": 4, "output_tokens": 3}, finish_reason="stop"
            )
        }
    )
    gateway = ModelGateway(
        providers={"fake": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default", "test-v1", frozenset({"answer"}), ModelTarget("fake", "small", "small"), None
            ),
            "plan.standard": ModelProfile(
                "plan.standard", "test-v1", frozenset({"plan"}), ModelTarget("fake", "small", "small"), None
            ),
        },
    )
    engine = create_v2_engine(checkpointer=MemorySaver(), model_gateway=gateway)

    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        {"configurable": {"thread_id": "alice:thread"}},
    )

    assert result["route_record"]["route"] == "fast"
    assert result["model_receipt"]["profile_version"] == "test-v1"
    assert result["messages"][-1].content == "Revenue plan"


@pytest.mark.asyncio
async def test_explicit_engine_records_typed_query_policy_and_answer_trace_events() -> None:
    provider = FakeProvider(
        {"small": ProviderResponse(content="Revenue plan", model="small", usage={}, finish_reason="stop")}
    )
    gateway = ModelGateway(
        providers={"fake": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default", "test-v1", frozenset({"answer"}), ModelTarget("fake", "small", "small"), None
            ),
            "plan.standard": ModelProfile(
                "plan.standard", "test-v1", frozenset({"plan"}), ModelTarget("fake", "small", "small"), None
            ),
        },
    )
    sink = _TraceSink()
    engine = create_v2_engine(checkpointer=MemorySaver(), model_gateway=gateway, trace_sink=sink)

    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        {"configurable": {"thread_id": "alice:thread", "request_context": {"trace_id": "trace-1"}}},
    )

    assert [event.stage for event in sink.events] == ["query", "policy", "answer"]  # type: ignore[attr-defined]
    assert result["trace_events"][-1]["stage"] == "answer"
