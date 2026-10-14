"""P2-B: a QUERY that needs analysis produces a typed MODE SUGGESTION, not a model call.

Execution-level evidence through the REAL compiled LangGraph engine:

* a QUERY the deterministic provider cannot serve yields
  mode_capability_outcome == cannot_resolve / suggested_mode == ANALYZE with
  stop_reason == "mode_cannot_resolve";
* that run makes ZERO model calls (a counting gateway proves it) and produces
  NO analysis output (no MODEL INTERPRETATION block);
* a merely slot-missing QUERY stays a TYPED CLARIFICATION (awaiting a bounded
  decision) and is NEVER converted into a mode suggestion;
* a RETAINED clarification re-entering the capability node stays a clarification
  (stop_reason == "query_plan_clarification_required") with no suggestion.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from src.nl2sql.contracts import ContextBundle, QueryPlan, RequestIdentity
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.planning import DeterministicQueryUnsupported
from tests.unit.test_ad_hoc_multi_input_budget import (
    _config,
    _context,
    _CountingAdHocRunner,
    _CountingMetricRunner,
    _graph_input,
    _plan,
    _RuntimeFactory,
)


class _CountingProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        del kwargs
        self.calls += 1
        return ProviderResponse(
            content="x", model="small", usage={}, finish_reason="stop"
        )

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


def _counting_gateway() -> tuple[ModelGateway, _CountingProvider]:
    provider = _CountingProvider()
    gateway = ModelGateway(
        providers={"synthetic": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("synthetic", "small", "small"),
                None,
            ),
        },
    )
    return gateway, provider


class _RaisingPlanProvider:
    """A deterministic provider that cannot serve the request without a model."""

    is_deterministic = True

    async def propose(
        self, *, question: str, context: ContextBundle, identity: RequestIdentity
    ) -> QueryPlan:
        del question, context, identity
        raise DeterministicQueryUnsupported("query_plan_requires_analysis")


class _StaticPlanProvider:
    is_deterministic = True

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan

    async def propose(
        self, *, question: str, context: ContextBundle, identity: RequestIdentity
    ) -> QueryPlan:
        del question, context, identity
        return self._plan


def _engine(
    *, provider: Any, context: ContextBundle
) -> tuple[Any, _CountingProvider]:
    factory = _RuntimeFactory(
        metric_runner=_CountingMetricRunner({}),
        provider=provider,
        context=context,
        ad_hoc_runner=_CountingAdHocRunner(),
    )
    gateway, counting = _counting_gateway()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=gateway,
        typed_runtime_factory=factory,
    )
    return engine, counting


def _analysis_rendered(state: dict[str, Any]) -> bool:
    """True when a Mode-2 interpretation block was projected into the response."""

    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and "MODEL INTERPRETATION" in str(block.get("text", ""))
        for block in (state.get("response_blocks") or [])
    )


@pytest.mark.asyncio
async def test_query_needing_analysis_suggests_analyze_with_zero_model_calls() -> None:
    engine, provider = _engine(
        provider=_RaisingPlanProvider(),
        context=_context(asset_ids=("metric.revenue",)),
    )

    result = await engine.ainvoke(_graph_input(None), _config())

    assert result["stop_reason"] == "mode_cannot_resolve"
    capability = result["mode_capability_outcome"]
    assert isinstance(capability, dict)
    assert capability["outcome"] == "cannot_resolve"
    assert capability["suggested_mode"] == "ANALYZE"
    assert capability["effective_mode"] == "QUERY"
    assert capability["run_id"] == "run-adhoc-budget"
    # ZERO model calls: the counting gateway was never entered.
    assert provider.calls == 0
    # No route, no execution, no analysis output.
    assert result["budget_record"] is None
    assert result["execution_record"] is None
    assert result["model_receipt"] is None
    assert _analysis_rendered(result) is False
    assert "query_plan_deterministic_unsupported" in {
        event["name"] for event in result["trace_events"]
    }


@pytest.mark.asyncio
async def test_slot_missing_query_stays_a_clarification_not_a_mode_suggestion() -> None:
    plan = _plan(metric_keys=("metric.revenue",)).model_copy(
        update={"unresolved_slots": ("time",)}
    )
    engine, provider = _engine(
        provider=_StaticPlanProvider(plan),
        context=_context(asset_ids=("metric.revenue",)),
    )

    result = await engine.ainvoke(_graph_input(None), _config())

    # A bounded clarification is a TYPED SUSPENSION, not a mode switch.
    assert result["stop_reason"] != "mode_cannot_resolve"
    assert result.get("mode_capability_outcome") is None
    assert result["decision_status"] == "awaiting_decision"
    assert result["pending_decision"]["decision_kind"] == "clarification"
    assert result["pending_decision"]["unresolved_slots"] == ["time"]
    assert "__interrupt__" in result
    # Still zero model calls and no analysis output.
    assert provider.calls == 0
    assert result["execution_record"] is None
    assert result["model_receipt"] is None
    assert _analysis_rendered(result) is False


@pytest.mark.asyncio
async def test_retained_clarification_is_never_converted_into_a_mode_switch() -> None:
    """The capability node guards a RETAINED clarification.

    A real clarify run leaves query_plan_validation.outcome == "clarify" in the
    checkpoint.  Re-entering the capability node with a pending capability must
    keep that a clarification and never fabricate a cannot_resolve suggestion.
    """

    plan = _plan(metric_keys=("metric.revenue",)).model_copy(
        update={"unresolved_slots": ("time",)}
    )
    engine, provider = _engine(
        provider=_StaticPlanProvider(plan),
        context=_context(asset_ids=("metric.revenue",)),
    )
    config = _config()

    first = await engine.ainvoke(_graph_input(None), config)
    assert first["query_plan_validation"]["outcome"] == "clarify"
    assert "__interrupt__" in first

    # Re-enter the capability node through the REAL compiled graph while the
    # clarification record is still in the checkpoint.
    await engine.aupdate_state(
        config, {"mode_capability_pending": True}, as_node="plan"
    )
    resumed = await engine.ainvoke(None, config)

    assert resumed["stop_reason"] == "query_plan_clarification_required"
    assert resumed.get("mode_capability_outcome") is None
    assert provider.calls == 0
    assert resumed["execution_record"] is None
    assert _analysis_rendered(resumed) is False
