from __future__ import annotations

import json

import httpx
import pytest

from src.nl2sql.contracts import ModelRequest
from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelPolicyDenied,
    ModelProfile,
    ModelTarget,
    OpenAICompatibleProvider,
    ProviderResponse,
    ProviderUnavailable,
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
        prompt_version="test-prompt-v1",
        plan_reason="required for high-risk plan" if stage == "plan" else None,
    )


def _gateway() -> ModelGateway:
    primary = FakeProvider({}, provider_name="primary")
    fallback = FakeProvider(
        {
            "small-fallback": ProviderResponse(
                content="fallback answer",
                model="small-fallback",
                usage={"input_tokens": 3, "output_tokens": 4},
                finish_reason="stop",
            )
        },
        provider_name="fallback",
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
    assert receipt.alias == "fast.default"
    assert receipt.stage == "answer"
    assert receipt.profile_checksum == _gateway().profile_checksum("fast.default")
    assert receipt.prompt_version == "test-prompt-v1"
    assert len(receipt.prompt_hash) == 64
    assert "show revenue" not in receipt.model_dump_json()


def test_profile_checksum_is_canonical_and_covers_routing_policy() -> None:
    first = ModelProfile(
        "fast.default",
        "test-v1",
        frozenset({"verify", "answer"}),
        ModelTarget("primary", "small-primary", "small"),
        ModelTarget("fallback", "small-fallback", "small"),
    )
    reordered = ModelProfile(
        "fast.default",
        "test-v1",
        frozenset({"answer", "verify"}),
        ModelTarget("primary", "small-primary", "small"),
        ModelTarget("fallback", "small-fallback", "small"),
    )
    changed = ModelProfile(
        "fast.default",
        "test-v1",
        frozenset({"answer", "verify"}),
        ModelTarget("primary", "different-small-model", "small"),
        ModelTarget("fallback", "small-fallback", "small"),
    )

    assert first.checksum == reordered.checksum
    assert len(first.checksum) == 64
    assert first.checksum != changed.checksum


def test_profile_rejects_fallback_tier_or_data_access_upgrade() -> None:
    with pytest.raises(ValueError, match="cannot upgrade the primary tier"):
        ModelProfile(
            "plan.standard",
            "test-v1",
            frozenset({"plan"}),
            ModelTarget("primary", "small-primary", "small"),
            ModelTarget("fallback", "pro-fallback", "pro"),
        )

    with pytest.raises(ValueError, match="cannot broaden data classification access"):
        ModelProfile(
            "fast.default",
            "test-v1",
            frozenset({"answer"}),
            ModelTarget(
                "primary",
                "small-primary",
                "small",
                allowed_data_classifications=frozenset({"public"}),
            ),
            ModelTarget(
                "fallback",
                "small-fallback",
                "small",
                allowed_data_classifications=frozenset({"public", "internal"}),
            ),
        )


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


@pytest.mark.asyncio
async def test_request_budget_cannot_expand_the_call_budget_envelope() -> None:
    with pytest.raises(ModelPolicyDenied, match="call_budget_exceeds_request_envelope"):
        await _gateway().invoke(
            _request(alias="fast.default"),
            CallBudget(deadline_ms=1_001, token_budget=100, cost_budget=1, max_attempts=2),
        )


@pytest.mark.asyncio
async def test_fallback_respects_the_attempt_budget() -> None:
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)

    with pytest.raises(BudgetExceeded, match="attempt_budget_exhausted"):
        await _gateway().invoke(_request(alias="fast.default"), budget)

    assert budget.attempts == 1


@pytest.mark.asyncio
async def test_fallback_is_denied_when_target_disallows_the_data_classification() -> None:
    primary = FakeProvider(
        {
            "small-primary": ProviderUnavailable(
                "provider_timeout", provider="primary", retryable=True
            )
        },
        provider_name="primary",
    )
    fallback = FakeProvider(
        {
            "small-fallback": ProviderResponse(
                content="should not be returned",
                model="small-fallback",
                usage={},
                finish_reason="stop",
            )
        },
        provider_name="fallback",
    )
    gateway = ModelGateway(
        providers={"primary": primary, "fallback": fallback},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("primary", "small-primary", "small"),
                ModelTarget(
                    "fallback",
                    "small-fallback",
                    "small",
                    allowed_data_classifications=frozenset({"public"}),
                ),
            )
        },
    )

    with pytest.raises(
        ModelPolicyDenied, match="model_target_disallows_data_classification"
    ):
        await gateway.invoke(
            _request(alias="fast.default"),
            CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2),
        )


@pytest.mark.asyncio
async def test_all_targets_unavailable_returns_structured_failure() -> None:
    gateway = ModelGateway(
        providers={
            "primary": FakeProvider(
                {
                    "small-primary": ProviderUnavailable(
                        "provider_rate_limited",
                        provider="primary",
                        retryable=True,
                        status_code=429,
                    )
                },
                provider_name="primary",
            ),
            "fallback": FakeProvider(
                {
                    "small-fallback": ProviderUnavailable(
                        "provider_server_error",
                        provider="fallback",
                        retryable=True,
                        status_code=503,
                    )
                },
                provider_name="fallback",
            ),
        },
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("primary", "small-primary", "small"),
                ModelTarget("fallback", "small-fallback", "small"),
            )
        },
    )

    with pytest.raises(ProviderUnavailable) as raised:
        await gateway.invoke(
            _request(alias="fast.default"),
            CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2),
        )

    assert raised.value.failure.model_dump(mode="json") == {
        "code": "all_model_targets_unavailable",
        "retryable": True,
        "provider": None,
        "status_code": None,
        "attempted_providers": ["primary", "fallback"],
        "causes": ["provider_rate_limited", "provider_server_error"],
    }


@pytest.mark.asyncio
async def test_non_retryable_provider_rejection_does_not_fallback() -> None:
    primary = FakeProvider(
        {
            "small-primary": ProviderUnavailable(
                "provider_request_rejected",
                provider="primary",
                retryable=False,
                status_code=401,
            )
        },
        provider_name="primary",
    )
    fallback = FakeProvider(
        {
            "small-fallback": ProviderResponse(
                content="must not run", model="small-fallback", usage={}, finish_reason="stop"
            )
        },
        provider_name="fallback",
    )
    gateway = ModelGateway(
        providers={"primary": primary, "fallback": fallback},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("primary", "small-primary", "small"),
                ModelTarget("fallback", "small-fallback", "small"),
            )
        },
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2)

    with pytest.raises(ProviderUnavailable, match="provider_request_rejected"):
        await gateway.invoke(_request(alias="fast.default"), budget)

    assert budget.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [(429, "provider_rate_limited", True), (503, "provider_server_error", True), (401, "provider_request_rejected", False)],
)
async def test_openai_compatible_adapter_classifies_http_failures(
    status_code: int, expected_code: str, retryable: bool
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, request=request, json={"error": "safe"})
    )
    provider = OpenAICompatibleProvider("primary", "https://provider.invalid/v1", "test", transport)

    with pytest.raises(ProviderUnavailable) as raised:
        await provider.complete(
            model="small",
            messages=[{"role": "user", "content": "hello"}],
            timeout_ms=1_000,
            max_output_tokens=10,
        )

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable
    assert raised.value.failure.status_code == status_code


@pytest.mark.asyncio
async def test_openai_compatible_adapter_rejects_invalid_json() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, content=b"not-json")
    )
    provider = OpenAICompatibleProvider("primary", "https://provider.invalid/v1", "test", transport)

    with pytest.raises(ProviderUnavailable, match="provider_invalid_json") as raised:
        await provider.complete(
            model="small",
            messages=[{"role": "user", "content": "hello"}],
            timeout_ms=1_000,
            max_output_tokens=10,
        )

    assert raised.value.failure.provider == "primary"
    assert json.loads(raised.value.failure.model_dump_json())["retryable"] is True


@pytest.mark.asyncio
async def test_openai_compatible_adapter_classifies_timeout() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = OpenAICompatibleProvider(
        "primary",
        "https://provider.invalid/v1",
        "test",
        httpx.MockTransport(timeout),
    )

    with pytest.raises(ProviderUnavailable, match="provider_timeout") as raised:
        await provider.complete(
            model="small",
            messages=[{"role": "user", "content": "hello"}],
            timeout_ms=1_000,
            max_output_tokens=10,
        )

    assert raised.value.retryable is True
    assert raised.value.failure.provider == "primary"


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
