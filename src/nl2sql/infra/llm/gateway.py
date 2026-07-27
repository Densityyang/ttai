"""The only v2 boundary for provider secrets and model network calls."""

from __future__ import annotations

import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, Protocol

import httpx

from src.core.secrets import SecretProvider
from src.core.settings import get_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.contracts import ModelReceipt, ModelRequest
from src.nl2sql.orchestration.budget import BudgetExceeded, CallBudget, should_stop

ModelTier = Literal["small", "pro"]


class ModelGatewayError(RuntimeError):
    pass


class ModelPolicyDenied(ModelGatewayError):
    pass


class ProviderUnavailable(ModelGatewayError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    model: str
    usage: dict[str, int]
    finish_reason: str


class ProviderAdapter(Protocol):
    @property
    def provider_name(self) -> str: ...

    async def complete(
        self, *, model: str, messages: list[dict[str, Any]], timeout_ms: int, max_output_tokens: int
    ) -> ProviderResponse: ...

    async def list_models(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    provider_name: str
    base_url: str
    api_key: str

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
    ) -> ProviderResponse:
        try:
            async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=timeout_ms / 1000) as client:
                response = await client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "max_tokens": max_output_tokens,
                    },
                )
                response.raise_for_status()
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderUnavailable(f"{self.provider_name}_unavailable") from exc
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderUnavailable(f"{self.provider_name}_invalid_response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderUnavailable(f"{self.provider_name}_missing_content")
        raw_usage = payload.get("usage") or {}
        usage = {
            "input_tokens": int(raw_usage.get("prompt_tokens", 0)),
            "output_tokens": int(raw_usage.get("completion_tokens", 0)),
        }
        return ProviderResponse(content, str(payload.get("model") or model), usage, str(choices[0].get("finish_reason") or "stop"))

    async def list_models(self) -> tuple[str, ...]:
        try:
            async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=5) as client:
                response = await client.get("/models", headers={"Authorization": f"Bearer {self.api_key}"})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.provider_name}_preflight_failed") from exc
        data = response.json().get("data", [])
        return tuple(str(item["id"]) for item in data if isinstance(item, dict) and isinstance(item.get("id"), str))


@dataclass
class FakeProvider:
    """Deterministic provider for route, budget, and failover tests."""

    responses: dict[str, ProviderResponse]
    provider_name: str = "fake"

    async def complete(
        self, *, model: str, messages: list[dict[str, Any]], timeout_ms: int, max_output_tokens: int
    ) -> ProviderResponse:
        del messages, timeout_ms, max_output_tokens
        try:
            return self.responses[model]
        except KeyError as exc:
            raise ProviderUnavailable("fake_model_unavailable") from exc

    async def list_models(self) -> tuple[str, ...]:
        return tuple(self.responses)


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    tier: ModelTier
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


@dataclass(frozen=True)
class ModelProfile:
    alias: str
    version: str
    allowed_stages: frozenset[str]
    primary: ModelTarget
    fallback: ModelTarget | None


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None


class ModelGateway:
    """Enforce profile, stage, timeout, budget and single-fallback invariants."""

    def __init__(self, *, providers: dict[str, ProviderAdapter], profiles: dict[str, ModelProfile]) -> None:
        self._providers = providers
        self._profiles = profiles
        self._circuits: dict[str, _Circuit] = {}

    async def preflight(self, alias: str) -> tuple[str, ...]:
        profile = self._profile(alias)
        models = await self._provider(profile.primary).list_models()
        if profile.primary.model not in models:
            raise ProviderUnavailable("primary_model_not_available")
        return models

    async def invoke(self, request: ModelRequest, budget: CallBudget) -> ModelReceipt:
        profile = self._profile(request.alias)
        self._enforce_policy(profile, request)
        stop_reason = should_stop(budget=budget)
        if stop_reason is not None:
            raise BudgetExceeded(stop_reason)
        estimated_input = _estimate_input_tokens(request.messages)
        if not budget.can_charge(tokens=estimated_input, cost=0):
            raise BudgetExceeded("model_call_budget_exceeded")
        target, fallback_used = profile.primary, False
        started_at = monotonic()
        try:
            response = await self._invoke_target(target, request, budget)
        except ProviderUnavailable:
            if profile.fallback is None:
                raise
            target, fallback_used = profile.fallback, True
            response = await self._invoke_target(target, request, budget)

        input_tokens = response.usage.get("input_tokens", 0)
        output_tokens = response.usage.get("output_tokens", 0)
        cost = _cost(target, input_tokens, output_tokens)
        budget.charge(input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)
        return ModelReceipt(
            provider=target.provider,
            resolved_model=response.model,
            latency_ms=int((monotonic() - started_at) * 1000),
            usage=response.usage,
            finish_reason=response.finish_reason,
            retries=int(fallback_used),
            fallback_used=fallback_used,
            profile_version=profile.version,
            content=response.content,
            estimated_cost=cost,
        )

    async def _invoke_target(
        self, target: ModelTarget, request: ModelRequest, budget: CallBudget
    ) -> ProviderResponse:
        circuit = self._circuits.setdefault(target.provider, _Circuit())
        if circuit.opened_at is not None and monotonic() - circuit.opened_at < 30:
            raise ProviderUnavailable("provider_circuit_open")
        timeout_ms = min(request.deadline_ms, budget.remaining_ms() - budget.reserve_ms())
        if timeout_ms <= 0:
            raise BudgetExceeded("deadline_reserve")
        try:
            remaining_tokens = request.token_budget - max(budget.tokens_used, _estimate_input_tokens(request.messages))
            if remaining_tokens <= 0:
                raise BudgetExceeded("model_call_budget_exceeded")
            response = await self._provider(target).complete(
                model=target.model,
                messages=request.messages,
                timeout_ms=timeout_ms,
                max_output_tokens=remaining_tokens,
            )
        except ProviderUnavailable:
            circuit.failures += 1
            if circuit.failures >= 3:
                circuit.opened_at = monotonic()
            raise
        circuit.failures = 0
        circuit.opened_at = None
        return response

    def _profile(self, alias: str) -> ModelProfile:
        try:
            return self._profiles[alias]
        except KeyError as exc:
            raise ModelPolicyDenied("unknown_model_alias") from exc

    def _provider(self, target: ModelTarget) -> ProviderAdapter:
        try:
            return self._providers[target.provider]
        except KeyError as exc:
            raise ProviderUnavailable("provider_not_configured") from exc

    @staticmethod
    def _enforce_policy(profile: ModelProfile, request: ModelRequest) -> None:
        if request.stage not in profile.allowed_stages:
            raise ModelPolicyDenied("model_alias_not_allowed_for_stage")
        if profile.primary.tier == "pro":
            if request.stage != "plan":
                raise ModelPolicyDenied("pro_model_is_plan_only")
            if not request.plan_reason:
                raise ModelPolicyDenied("pro_plan_reason_required")


def build_model_gateway() -> ModelGateway:
    """Build profiles from public configuration and secrets only at the gateway boundary."""
    config = get_agent_config()
    settings = get_settings()
    deepseek_key = _secret("DEEPSEEK_API_KEY") or settings.openai_api_key
    nvidia_key = _secret("NVIDIA_API_KEY")
    providers: dict[str, ProviderAdapter] = {}
    if deepseek_key:
        providers["deepseek"] = OpenAICompatibleProvider("deepseek", config.deepseek_base_url, deepseek_key)
    if nvidia_key:
        providers["nvidia"] = OpenAICompatibleProvider("nvidia", config.nvidia_nim_base_url, nvidia_key)
    nvidia_fallback = (
        ModelTarget("nvidia", config.nvidia_nim_model_fast, "small")
        if nvidia_key and config.nvidia_nim_model_fast
        else None
    )
    small = frozenset({"classify", "retrieve", "generate_sql", "verify", "answer"})
    profiles = {
        "fast.default": ModelProfile(
            "fast.default", config.model_profile_version, small,
            ModelTarget("deepseek", config.deepseek_flash_model, "small"),
            nvidia_fallback,
        ),
        "plan.standard": ModelProfile(
            "plan.standard", config.model_profile_version, frozenset({"plan"}),
            ModelTarget("deepseek", config.deepseek_flash_model, "small"),
            nvidia_fallback,
        ),
        "plan.pro": ModelProfile(
            "plan.pro", config.model_profile_version, frozenset({"plan"}),
            ModelTarget("deepseek", config.deepseek_pro_model, "pro"),
            ModelTarget("deepseek", config.deepseek_flash_model, "small"),
        ),
    }
    return ModelGateway(providers=providers, profiles=profiles)


def model_gateway_available() -> bool:
    """Return whether a provider credential is configured without making a network call."""
    return bool(_secret("DEEPSEEK_API_KEY") or _secret("NVIDIA_API_KEY") or get_settings().openai_api_key)


def get_legacy_model(
    model_name: str | None = None,
    openai_base_url: str | None = None,
    temperature: float = 0,
    streaming: bool = True,
) -> Any:
    """Compatibility bridge for retired graphs that are not reachable from v2."""
    from src.nl2sql.infra.llm.factory import build_legacy_provider_model

    return build_legacy_provider_model(model_name, openai_base_url, temperature, streaming)


def _secret(name: str) -> str:
    if f"{name}_FILE" in os.environ:
        return SecretProvider().get(name) or ""
    return os.environ.get(name) or ""


def _cost(target: ModelTarget, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * target.input_cost_per_million + output_tokens * target.output_cost_per_million) / 1_000_000


def _estimate_input_tokens(messages: list[dict[str, Any]]) -> int:
    return max(1, sum(len(str(message.get("content", ""))) for message in messages) // 4 + 1)
