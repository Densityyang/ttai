from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from src.nl2sql.contracts import ModelRequest
from src.nl2sql.infra.llm import gateway as gateway_module
from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelInputPolicyDenied,
    ModelOutputInvalid,
    ModelPolicyDenied,
    ModelProfile,
    ModelTarget,
    OpenAICompatibleProvider,
    ProviderResponse,
    ProviderUnavailable,
    StructuredOutputMode,
)
from src.nl2sql.infra.llm.model_input_policy import calibrated_model_input_policy
from src.nl2sql.orchestration.budget import BudgetExceeded, CallBudget, should_stop
from src.nl2sql.orchestration.routing import RiskSignals, choose_route
from src.nl2sql.orchestration.shadow import is_shadow_sample


class _StructuredAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str
    confidence: float = Field(ge=0, le=1)


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


def _structured_gateway(content: str) -> ModelGateway:
    provider = FakeProvider(
        {
            "small": ProviderResponse(
                content=content,
                model="small-resolved",
                usage={"input_tokens": 4, "output_tokens": 6},
                finish_reason="stop",
            )
        }
    )
    return ModelGateway(
        providers={"fake": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("fake", "small", "small"),
                None,
            )
        },
    )


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
    assert receipt.output_schema_checksum is None
    assert "show revenue" not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_gateway_validates_structured_output_and_keeps_a_replayable_receipt() -> None:
    gateway = _structured_gateway('{"answer":"Revenue is stable","confidence":0.8}')
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)

    result = await gateway.invoke_structured(
        _request(alias="fast.default"),
        budget,
        _StructuredAnswer,
    )

    assert result.output == _StructuredAnswer(answer="Revenue is stable", confidence=0.8)
    assert result.receipt.resolved_model == "small-resolved"
    assert len(result.receipt.output_schema_checksum or "") == 64
    assert _StructuredAnswer.model_validate_json(result.receipt.content) == result.output
    assert result.receipt.usage == {"input_tokens": 4, "output_tokens": 6}
    assert budget.attempts == 1
    assert budget.tokens_used == 10


@pytest.mark.asyncio
async def test_structured_schema_mismatch_is_denied_before_the_model_call() -> None:
    gateway = _structured_gateway('{"answer":"unused","confidence":0.5}')
    request = _request(alias="fast.default").model_copy(
        update={"tool_schema": {"title": "Different", "type": "string"}}
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)

    with pytest.raises(ModelPolicyDenied, match="structured_output_schema_mismatch"):
        await gateway.invoke_structured(request, budget, _StructuredAnswer)

    assert budget.attempts == 0


@pytest.mark.asyncio
async def test_invalid_structured_output_preserves_only_safe_receipt_metadata() -> None:
    gateway = _structured_gateway('{"answer":"unsafe raw value","confidence":2.0}')
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)

    with pytest.raises(ModelOutputInvalid) as raised:
        await gateway.invoke_structured(
            _request(alias="fast.default"),
            budget,
            _StructuredAnswer,
        )

    assert raised.value.failure.model_dump(mode="json") == {
        "code": "model_output_validation_failed",
        "retryable": False,
        "provider": "fake",
        "status_code": None,
        "attempted_providers": ["fake"],
        "causes": ["less_than_equal"],
    }
    assert raised.value.receipt.content == ""
    assert raised.value.receipt.resolved_model == "small-resolved"
    assert raised.value.receipt.usage == {"input_tokens": 4, "output_tokens": 6}
    assert len(raised.value.receipt.output_schema_checksum or "") == 64
    assert "unsafe raw value" not in str(raised.value)
    assert raised.value.__context__ is None
    assert budget.tokens_used == 10


def test_product_mode_denies_legacy_model_bridge_before_loading_the_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_module = "src.nl2sql.infra.llm.factory"
    monkeypatch.delitem(sys.modules, factory_module, raising=False)
    monkeypatch.setattr(
        gateway_module,
        "get_settings",
        lambda: SimpleNamespace(service_mode="product"),
    )

    with pytest.raises(ModelPolicyDenied, match="legacy_model_disabled_in_product") as raised:
        gateway_module.get_legacy_model()

    assert raised.value.code == "legacy_model_disabled_in_product"
    assert factory_module not in sys.modules


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
@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
async def test_openai_compatible_adapter_translates_structured_output_mode(
    mode: StructuredOutputMode,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "small-resolved",
                "choices": [
                    {
                        "message": {"content": '{"answer":"ok","confidence":1}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatibleProvider(
        "primary",
        "https://provider.invalid/v1",
        "test",
        transport=httpx.MockTransport(handler),
        structured_output_mode=mode,
    )
    schema = _StructuredAnswer.model_json_schema()

    await provider.complete(
        model="small",
        messages=[{"role": "user", "content": "hello"}],
        timeout_ms=1_000,
        max_output_tokens=10,
        response_schema=schema,
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    if mode == "json_schema":
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "_StructuredAnswer",
                "strict": True,
                "schema": schema,
            },
        }
    else:
        assert payload["response_format"] == {"type": "json_object"}


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

class _CountingProvider:
    """Provider that records call count so a deny can be proven zero-call."""

    def __init__(
        self, result: ProviderResponse | ProviderUnavailable, *, provider_name: str
    ) -> None:
        self._result = result
        self.provider_name = provider_name
        self.calls = 0

    async def complete(self, **kwargs: object) -> ProviderResponse:
        del kwargs
        self.calls += 1
        if isinstance(self._result, ProviderUnavailable):
            raise self._result
        return self._result

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


def _answer(model: str = "small-resolved") -> ProviderResponse:
    return ProviderResponse(
        content="approved answer",
        model=model,
        usage={"input_tokens": 1, "output_tokens": 1},
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_unapproved_primary_is_denied_before_any_provider_call() -> None:
    provider = _CountingProvider(_answer(), provider_name="counting")
    gateway = ModelGateway(
        providers={"counting": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("counting", "unlisted-model", "small"),
                None,
            )
        },
        model_input_policy=calibrated_model_input_policy(
            (ModelTarget("counting", "approved-model", "small"),)
        ),
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)

    with pytest.raises(ModelInputPolicyDenied, match="model_target_not_approved") as raised:
        await gateway.invoke(_request(alias="fast.default"), budget)

    assert provider.calls == 0
    assert budget.attempts == 0
    assert raised.value.decision.target_provider == "counting"


@pytest.mark.asyncio
async def test_unapproved_fallback_is_denied_independently_after_primary_failure() -> None:
    primary = _CountingProvider(
        ProviderUnavailable("provider_timeout", provider="counting-primary", retryable=True),
        provider_name="counting-primary",
    )
    fallback = _CountingProvider(_answer("small-fallback"), provider_name="counting-fallback")
    gateway = ModelGateway(
        providers={"counting-primary": primary, "counting-fallback": fallback},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("counting-primary", "small-primary", "small"),
                ModelTarget("counting-fallback", "small-fallback", "small"),
            )
        },
        # ONLY the primary is approved; being the fallback grants nothing.
        model_input_policy=calibrated_model_input_policy(
            (ModelTarget("counting-primary", "small-primary", "small"),)
        ),
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2)

    with pytest.raises(ModelInputPolicyDenied, match="model_target_not_approved") as raised:
        await gateway.invoke(_request(alias="fast.default"), budget)

    assert primary.calls == 1
    assert fallback.calls == 0
    assert raised.value.decision.target_provider == "counting-fallback"


@pytest.mark.asyncio
async def test_configured_system_secret_is_denied_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R10-E: an approved target plus a bare bounded NON-model system credential
    # in the prompt must deny BEFORE any provider/budget work.
    monkeypatch.setenv("EMBEDDING_API_KEY", "emb-deploy-A1B2C3D4E5F6G7H8")
    provider = _CountingProvider(_answer(), provider_name="counting")
    gateway = ModelGateway(
        providers={"counting": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("counting", "small", "small"),
                None,
            )
        },
        # No explicit secret values: the deployment source must still apply.
        model_input_policy=calibrated_model_input_policy(
            (ModelTarget("counting", "small", "small"),)
        ),
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)
    request = _request(alias="fast.default").model_copy(
        update={
            "messages": [
                {"role": "user", "content": "key emb-deploy-A1B2C3D4E5F6G7H8"}
            ]
        }
    )

    with pytest.raises(ModelInputPolicyDenied, match="model_input_secret_detected"):
        await gateway.invoke(request, budget)

    assert provider.calls == 0
    assert budget.attempts == 0


@pytest.mark.asyncio
async def test_configured_system_secret_never_reaches_a_fallback_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R10-F: with a configured system secret in the request, neither the primary
    # nor the independently-gated fallback provider is called.
    monkeypatch.setenv("EMBEDDING_API_KEY", "emb-deploy-A1B2C3D4E5F6G7H8")
    primary = _CountingProvider(
        ProviderUnavailable("provider_timeout", provider="counting-primary", retryable=True),
        provider_name="counting-primary",
    )
    fallback = _CountingProvider(_answer("small-fallback"), provider_name="counting-fallback")
    gateway = ModelGateway(
        providers={"counting-primary": primary, "counting-fallback": fallback},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("counting-primary", "small-primary", "small"),
                ModelTarget("counting-fallback", "small-fallback", "small"),
            )
        },
        model_input_policy=calibrated_model_input_policy(
            (
                ModelTarget("counting-primary", "small-primary", "small"),
                ModelTarget("counting-fallback", "small-fallback", "small"),
            )
        ),
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2)
    request = _request(alias="fast.default").model_copy(
        update={
            "messages": [
                {"role": "user", "content": "key emb-deploy-A1B2C3D4E5F6G7H8"}
            ]
        }
    )

    with pytest.raises(ModelInputPolicyDenied, match="model_input_secret_detected"):
        await gateway.invoke(request, budget)

    assert primary.calls == 0
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_technical_secret_is_denied_before_the_provider_call() -> None:
    provider = _CountingProvider(_answer(), provider_name="counting")
    gateway = ModelGateway(
        providers={"counting": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("counting", "small", "small"),
                None,
            )
        },
        model_input_policy=calibrated_model_input_policy(
            (ModelTarget("counting", "small", "small"),)
        ),
    )
    request = _request(alias="fast.default").model_copy(
        update={"messages": [{"role": "user", "content": "password = 'hunter2'"}]}
    )
    budget = CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=1)

    with pytest.raises(ModelInputPolicyDenied, match="model_input_secret_detected") as raised:
        await gateway.invoke(request, budget)

    assert provider.calls == 0
    assert budget.attempts == 0
    assert "credential_assignment" in raised.value.decision.matched_categories


@pytest.mark.asyncio
async def test_successful_receipt_stamps_model_input_policy_evidence() -> None:
    gateway = _gateway()

    receipt = await gateway.invoke(
        _request(alias="fast.default"),
        CallBudget(deadline_ms=1_000, token_budget=100, cost_budget=1, max_attempts=2),
    )

    assert receipt.egress_outcome == "allow"
    assert receipt.model_input_policy_version == "model-input.bootstrap.v1"
    assert receipt.model_input_policy_checksum == gateway.model_input_policy.checksum
    assert receipt.matched_categories == ()
    assert len(receipt.content_sha256) == 64
