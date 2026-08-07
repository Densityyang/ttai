from __future__ import annotations

from typing import Any

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


class _CountingProvider:
    provider_name = "fake"

    def __init__(self, response: ProviderResponse) -> None:
        self.response = response
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
        response_schema: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        del model, messages, timeout_ms, max_output_tokens, response_schema
        self.calls += 1
        return self.response

    async def list_models(self) -> tuple[str, ...]:
        return (self.response.model,)


@pytest.mark.asyncio
async def test_explicit_engine_promotes_model_required_fast_candidate_to_standard() -> None:
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

    assert result["route_record"]["route"] == "standard"
    assert result["route_record"]["reason"] == "model_required"
    assert result["route_record"]["policy_version"] == "route.bootstrap.v1"
    assert result["model_receipt"]["profile_version"] == "test-v1"
    assert len(result["model_receipt"]["profile_checksum"]) == 64
    assert result["model_receipt"]["prompt_version"] == "v2-engine-v1"
    assert result["budget_record"]["policy_version"] == "routing-budget.bootstrap.v1"
    assert result["budget_record"]["usage"]["model_calls"] == 1
    assert result["budget_record"]["limits"]["max_model_calls"] == 3
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
    answer_attributes = result["trace_events"][-1]["attributes"]
    assert answer_attributes["prompt_version"] == "v2-engine-v1"
    assert len(answer_attributes["prompt_hash"]) == 64
    assert answer_attributes["prompt_hash"] != "[REDACTED]"
    assert answer_attributes["route"] == "standard"
    assert answer_attributes["model_calls"] == 1


@pytest.mark.asyncio
async def test_policy_denial_is_terminal_without_a_provider_call_or_retry() -> None:
    provider = _CountingProvider(
        ProviderResponse(content="unused", model="small", usage={}, finish_reason="stop")
    )
    gateway = ModelGateway(
        providers={"fake": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"plan"}),
                ModelTarget("fake", "small", "small"),
                None,
            )
        },
    )
    engine = create_v2_engine(checkpointer=MemorySaver(), model_gateway=gateway)

    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        {"configurable": {"thread_id": "alice:policy-denied"}},
    )

    assert provider.calls == 0
    assert result["stop_reason"] == "policy_denied"
    assert result["budget_record"]["usage"]["model_calls"] == 1
    assert result["budget_record"]["error_counts"] == {
        "model_alias_not_allowed_for_stage": 1
    }
    assert result["degradation_flags"] == ["ModelPolicyDenied"]
