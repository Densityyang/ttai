from __future__ import annotations

import pytest

from src.nl2sql.contracts import ModelRequest
from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelPolicyDenied,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.budget import BudgetExceeded, CallBudget, should_stop
from src.nl2sql.orchestration.routing import RiskSignals, choose_route
from src.nl2sql.orchestration.shadow import is_shadow_sample


def _request(*, alias: str, stage: str = "answer") -> ModelRequest:
    return ModelRequest(
        alias=alias,
        stage=stage,  # type: ignore[arg-type]
        messages=[{"role": "user", "content": "show revenue"}],
        deadline_ms=1_000,
        token_budget=100,
        cost_budget=1,
        data_classification="internal",
        plan_reason="required for high-risk plan" if stage == "plan" else None,
    )


def _gateway() -> ModelGateway:
    primary = FakeProvider({})
    fallback = FakeProvider(
        {
            "small-fallback": ProviderResponse(
                content="fallback answer",
                model="small-fallback",
                usage={"input_tokens": 3, "output_tokens": 4},
                finish_reason="stop",
            )
        }
    )
    profiles = {
        "fast.default": ModelProfile(
            "fast.default",
            "test-v1",
            frozenset({"answer"}),
            ModelTarget("primary", "small-primary", "small"),
            ModelTarget("fallback", "small-fallback", "small"),
        ),
        "plan.pro": ModelProfile(
            "plan.pro",
            "test-v1",
            frozenset({"plan"}),
            ModelTarget("primary", "pro-primary", "pro"),
            ModelTarget("fallback", "small-fallback", "small"),
        ),
    }
    return ModelGateway(providers={"primary": primary, "fallback": fallback}, profiles=profiles)


@pytest.mark.asyncio
async def test_gateway_uses_only_configured_small_fallback() -> None:
    receipt = await _gateway().invoke(
        _request(alias="fast.default"),
        CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2),
    )

    assert receipt.fallback_used
    assert receipt.resolved_model == "small-fallback"
    assert receipt.content == "fallback answer"


@pytest.mark.asyncio
async def test_preflight_validates_the_primary_model_id() -> None:
    gateway = ModelGateway(
        providers={
            "fake": FakeProvider(
                {
                    "small": ProviderResponse(
                        content="ok", model="small", usage={}, finish_reason="stop"
                    )
                }
            )
        },
        profiles={
            "fast.default": ModelProfile(
                "fast.default", "test-v1", frozenset({"answer"}), ModelTarget("fake", "small", "small"), None
            )
        },
    )

    assert await gateway.preflight("fast.default") == ("small",)


@pytest.mark.asyncio
async def test_pro_alias_fails_closed_outside_plan_stage() -> None:
    with pytest.raises(ModelPolicyDenied, match="model_alias_not_allowed_for_stage"):
        await _gateway().invoke(
            _request(alias="plan.pro", stage="answer"),
            CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2),
        )


@pytest.mark.asyncio
async def test_budget_is_checked_before_provider_invocation() -> None:
    with pytest.raises(BudgetExceeded, match="model_call_budget_exceeded"):
        await _gateway().invoke(
            _request(alias="fast.default"),
            CallBudget(deadline_ms=1_000, token_budget=1, cost_budget=1, max_attempts=2),
        )


def test_route_decisions_are_replayable_and_risk_weighted() -> None:
    decision = choose_route(
        signals=RiskSignals(restricted_data=True, table_count=3), confidence=0.4
    )

    assert decision.route == "deep"
    assert decision.risk == 55
    assert choose_route(signals=decision.signals, confidence=decision.confidence).replay_record() == decision.replay_record()


def test_early_stop_reserves_deadline_and_attempt_budget() -> None:
    budget = CallBudget(deadline_ms=1_000, token_budget=10, cost_budget=1, max_attempts=1)
    budget.attempts = 1
    assert should_stop(budget=budget) == "attempt_budget_exhausted"


def test_shadow_sampling_is_deterministic_and_capped() -> None:
    from uuid import UUID

    request_id = UUID("22222222-2222-2222-2222-222222222222")
    assert is_shadow_sample(request_id, percentage=5) == is_shadow_sample(request_id, percentage=5)
    assert is_shadow_sample(request_id, percentage=0) is False
    with pytest.raises(ValueError, match="between 0 and 5"):
        is_shadow_sample(request_id, percentage=6)
