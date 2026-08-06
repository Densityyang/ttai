"""The only v2 boundary for provider secrets and model network calls."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol

import httpx

from src.core.secrets import SecretProvider
from src.core.settings import get_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.contracts import ModelFailure, ModelReceipt, ModelRequest, ModelStage
from src.nl2sql.infra.llm.profiles import ModelProfile, ModelTarget
from src.nl2sql.orchestration.budget import BudgetExceeded, CallBudget, should_stop


class ModelGatewayError(RuntimeError):
    """Base exception carrying safe, typed failure metadata."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        provider: str | None = None,
        status_code: int | None = None,
        attempted_providers: tuple[str, ...] = (),
        causes: tuple[str, ...] = (),
    ) -> None:
        self.failure = ModelFailure(
            code=code,
            retryable=retryable,
            provider=provider,
            status_code=status_code,
            attempted_providers=attempted_providers,
            causes=causes,
        )
        super().__init__(code)

    @property
    def code(self) -> str:
        return self.failure.code

    @property
    def retryable(self) -> bool:
        return self.failure.retryable


class ModelPolicyDenied(ModelGatewayError):
    pass


class ProviderUnavailable(ModelGatewayError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = True,
        provider: str | None = None,
        status_code: int | None = None,
        attempted_providers: tuple[str, ...] = (),
        causes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            code,
            retryable=retryable,
            provider=provider,
            status_code=status_code,
            attempted_providers=attempted_providers,
            causes=causes,
        )


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
    api_key: str = field(repr=False, compare=False)
    transport: httpx.AsyncBaseTransport | None = field(
        default=None, repr=False, compare=False
    )

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
    ) -> ProviderResponse:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                timeout=timeout_ms / 1000,
                transport=self.transport,
            ) as client:
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
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderUnavailable(
                "provider_timeout", provider=self.provider_name, retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise _http_status_failure(self.provider_name, exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                "provider_transport_error", provider=self.provider_name, retryable=True
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(
                "provider_invalid_json", provider=self.provider_name, retryable=True
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(
                "provider_invalid_response", provider=self.provider_name, retryable=True
            )
        choices = payload.get("choices") or []
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderUnavailable(
                "provider_invalid_response", provider=self.provider_name, retryable=True
            )
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise ProviderUnavailable(
                "provider_invalid_response", provider=self.provider_name, retryable=True
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderUnavailable(
                "provider_missing_content", provider=self.provider_name, retryable=True
            )
        raw_usage = payload.get("usage") or {}
        if not isinstance(raw_usage, dict):
            raise ProviderUnavailable(
                "provider_invalid_usage", provider=self.provider_name, retryable=True
            )
        try:
            input_tokens = int(raw_usage.get("prompt_tokens", 0))
            output_tokens = int(raw_usage.get("completion_tokens", 0))
        except (TypeError, ValueError) as exc:
            raise ProviderUnavailable(
                "provider_invalid_usage", provider=self.provider_name, retryable=True
            ) from exc
        if input_tokens < 0 or output_tokens < 0:
            raise ProviderUnavailable(
                "provider_invalid_usage", provider=self.provider_name, retryable=True
            )
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        return ProviderResponse(
            content,
            str(payload.get("model") or model),
            usage,
            str(choices[0].get("finish_reason") or "stop"),
        )

    async def list_models(self) -> tuple[str, ...]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"), timeout=5, transport=self.transport
            ) as client:
                response = await client.get("/models", headers={"Authorization": f"Bearer {self.api_key}"})
                response.raise_for_status()
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderUnavailable(
                "provider_timeout", provider=self.provider_name, retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise _http_status_failure(self.provider_name, exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                "provider_transport_error", provider=self.provider_name, retryable=True
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(
                "provider_invalid_json", provider=self.provider_name, retryable=True
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(
                "provider_invalid_response", provider=self.provider_name, retryable=True
            )
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ProviderUnavailable(
                "provider_invalid_response", provider=self.provider_name, retryable=True
            )
        return tuple(str(item["id"]) for item in data if isinstance(item, dict) and isinstance(item.get("id"), str))


@dataclass
class FakeProvider:
    """Deterministic provider for route, budget, and failover tests."""

    responses: dict[str, ProviderResponse | ProviderUnavailable]
    provider_name: str = "fake"

    async def complete(
        self, *, model: str, messages: list[dict[str, Any]], timeout_ms: int, max_output_tokens: int
    ) -> ProviderResponse:
        del messages, timeout_ms, max_output_tokens
        try:
            response = self.responses[model]
        except KeyError as exc:
            raise ProviderUnavailable(
                "fake_model_unavailable", provider=self.provider_name, retryable=True
            ) from exc
        if isinstance(response, ProviderUnavailable):
            raise response
        return response

    async def list_models(self) -> tuple[str, ...]:
        return tuple(self.responses)


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None


class ModelGateway:
    """Enforce profile, stage, timeout, budget and single-fallback invariants."""

    def __init__(
        self, *, providers: dict[str, ProviderAdapter], profiles: dict[str, ModelProfile]
    ) -> None:
        for name, provider in providers.items():
            if name != provider.provider_name:
                raise ValueError("provider registry key must match provider_name")
        for alias, profile in profiles.items():
            if alias != profile.alias:
                raise ValueError("model profile registry key must match profile alias")
        if not profiles:
            raise ValueError("at least one model profile is required")
        self._providers = dict(providers)
        self._profiles = dict(profiles)
        self._circuits: dict[str, _Circuit] = {}

    def profile_checksum(self, alias: str) -> str:
        return self._profile(alias).checksum

    async def preflight(self, alias: str) -> tuple[str, ...]:
        profile = self._profile(alias)
        models = await self._provider(profile.primary).list_models()
        if profile.primary.model not in models:
            raise ProviderUnavailable(
                "primary_model_not_available",
                provider=profile.primary.provider,
                retryable=False,
            )
        return models

    async def invoke(self, request: ModelRequest, budget: CallBudget) -> ModelReceipt:
        profile = self._profile(request.alias)
        self._enforce_policy(profile, request)
        _enforce_budget_envelope(request, budget)
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
        except ProviderUnavailable as primary_error:
            if profile.fallback is None or not primary_error.retryable:
                raise
            target, fallback_used = profile.fallback, True
            self._enforce_target_policy(target, request)
            try:
                response = await self._invoke_target(target, request, budget)
            except ProviderUnavailable as fallback_error:
                attempted = (
                    primary_error.failure.provider or profile.primary.provider,
                    fallback_error.failure.provider or target.provider,
                )
                raise ProviderUnavailable(
                    "all_model_targets_unavailable",
                    retryable=primary_error.retryable or fallback_error.retryable,
                    attempted_providers=attempted,
                    causes=(primary_error.code, fallback_error.code),
                ) from fallback_error

        input_tokens = response.usage.get("input_tokens", 0)
        output_tokens = response.usage.get("output_tokens", 0)
        cost = _cost(target, input_tokens, output_tokens)
        budget.charge(input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)
        return ModelReceipt(
            alias=profile.alias,
            stage=request.stage,
            provider=target.provider,
            resolved_model=response.model,
            latency_ms=int((monotonic() - started_at) * 1000),
            usage=response.usage,
            finish_reason=response.finish_reason,
            retries=int(fallback_used),
            fallback_used=fallback_used,
            profile_version=profile.version,
            profile_checksum=profile.checksum,
            prompt_version=request.prompt_version,
            prompt_hash=_prompt_hash(request),
            content=response.content,
            estimated_cost=cost,
        )

    async def _invoke_target(
        self, target: ModelTarget, request: ModelRequest, budget: CallBudget
    ) -> ProviderResponse:
        circuit = self._circuits.setdefault(target.provider, _Circuit())
        if circuit.opened_at is not None and monotonic() - circuit.opened_at < 30:
            raise ProviderUnavailable(
                "provider_circuit_open", provider=target.provider, retryable=True
            )
        timeout_ms = min(request.deadline_ms, budget.remaining_ms() - budget.reserve_ms())
        if timeout_ms <= 0:
            raise BudgetExceeded("deadline_reserve")
        try:
            remaining_tokens = (
                budget.token_budget
                - budget.tokens_used
                - _estimate_input_tokens(request.messages)
            )
            if remaining_tokens <= 0:
                raise BudgetExceeded("model_call_budget_exceeded")
            budget.begin_attempt()
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
            raise ProviderUnavailable(
                "provider_not_configured", provider=target.provider, retryable=False
            ) from exc

    @classmethod
    def _enforce_policy(cls, profile: ModelProfile, request: ModelRequest) -> None:
        if request.stage not in profile.allowed_stages:
            raise ModelPolicyDenied("model_alias_not_allowed_for_stage")
        cls._enforce_target_policy(profile.primary, request)

    @staticmethod
    def _enforce_target_policy(target: ModelTarget, request: ModelRequest) -> None:
        if request.data_classification not in target.allowed_data_classifications:
            raise ModelPolicyDenied("model_target_disallows_data_classification")
        if target.tier == "pro":
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
    small: frozenset[ModelStage] = frozenset(
        {"classify", "retrieve", "generate_sql", "verify", "answer"}
    )
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
    try:
        return bool(
            _secret("DEEPSEEK_API_KEY")
            or _secret("NVIDIA_API_KEY")
            or get_settings().openai_api_key
        )
    except ValueError:
        return False


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


def _prompt_hash(request: ModelRequest) -> str:
    payload = request.model_dump(
        mode="json",
        include={"messages", "tool_schema"},
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enforce_budget_envelope(request: ModelRequest, budget: CallBudget) -> None:
    if (
        budget.deadline_ms > request.deadline_ms
        or budget.token_budget > request.token_budget
        or budget.cost_budget > request.cost_budget
    ):
        raise ModelPolicyDenied("call_budget_exceeds_request_envelope")


def _http_status_failure(provider: str, status_code: int) -> ProviderUnavailable:
    if status_code == 429:
        return ProviderUnavailable(
            "provider_rate_limited",
            provider=provider,
            retryable=True,
            status_code=status_code,
        )
    if status_code >= 500:
        return ProviderUnavailable(
            "provider_server_error",
            provider=provider,
            retryable=True,
            status_code=status_code,
        )
    return ProviderUnavailable(
        "provider_request_rejected",
        provider=provider,
        retryable=False,
        status_code=status_code,
    )
