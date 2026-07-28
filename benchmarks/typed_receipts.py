"""Typed, reproducible benchmark inputs and release gates."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderCallReceipt(_BenchmarkModel):
    alias: str
    stage: Literal["classify", "retrieve", "plan", "generate_sql", "verify", "answer"]
    resolved_model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    fallback_used: bool = False
    http_status: int | None = Field(default=None, ge=100, le=599)


class TypedAnswerReceipt(_BenchmarkModel):
    trace_id: str = Field(min_length=1)
    answer_type: Literal["answer", "clarification", "rejected", "hitl"]
    answer_hash: str = Field(min_length=16)
    rowset_sha256: str | None = None
    candidate_score: float | None = Field(default=None, ge=0, le=1)
    policy_outcome: Literal["allow", "deny", "approval"]
    execution_accepted: bool
    execution_row_count: int = Field(default=0, ge=0)
    model_calls: tuple[ProviderCallReceipt, ...] = ()


class BenchmarkManifest(_BenchmarkModel):
    run_id: str = Field(min_length=1)
    dataset_checksum: str = Field(min_length=16)
    prompt_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    semantic_version: str = Field(min_length=1)
    model_profile_version: str = Field(min_length=1)
    git_revision: str = Field(min_length=7)
    matrix: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_matrix(self) -> "BenchmarkManifest":
        required = {"deepseek.flash", "deepseek.pro", "benchmark.nim"}
        if not required.issubset(self.matrix):
            raise ValueError("benchmark matrix must include Flash, plan-only Pro, and verified NVIDIA small")
        return self


class NvidiaModelsClient(Protocol):
    async def get(self, path: str) -> Any: ...


async def verify_nvidia_small_model(client: NvidiaModelsClient, model_id: str) -> str:
    """Verify the configured small-model ID against the provider ``/models`` response."""
    response = await client.get("/models")
    if getattr(response, "status_code", 500) != 200:
        raise RuntimeError("NVIDIA /models preflight failed")
    payload = response.json()
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    ids = {entry.get("id") for entry in entries if isinstance(entry, dict)}
    if model_id not in ids:
        raise RuntimeError("configured NVIDIA small model was not returned by /models")
    return model_id


@dataclass
class BudgetGate:
    max_total_cost: float
    max_calls: int
    cost_used: float = 0.0
    calls_used: int = 0

    def consume(self, receipt: TypedAnswerReceipt) -> None:
        next_cost = self.cost_used + sum(call.estimated_cost for call in receipt.model_calls)
        next_calls = self.calls_used + len(receipt.model_calls)
        if next_cost > self.max_total_cost or next_calls > self.max_calls:
            raise RuntimeError("benchmark budget exhausted")
        self.cost_used = next_cost
        self.calls_used = next_calls


def validate_receipt(receipt: TypedAnswerReceipt) -> None:
    for call in receipt.model_calls:
        if call.alias == "plan.pro" and call.stage != "plan":
            raise ValueError("Pro benchmark calls are allowed only in plan stage")
        if call.alias == "plan.pro" and call.fallback_used:
            raise ValueError("Pro benchmark calls must not be an automatic fallback")


def redacted_sample_id(case_id: str) -> str:
    return hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]


def paired_bootstrap_interval(
    baseline: list[float], experiment: list[float], *, samples: int = 1_000, seed: int = 0
) -> tuple[float, float]:
    n = min(len(baseline), len(experiment))
    if n == 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    differences = []
    for _ in range(samples):
        indexes = [rng.randrange(n) for _ in range(n)]
        differences.append(sum(experiment[i] - baseline[i] for i in indexes) / n)
    differences.sort()
    return differences[int(samples * 0.025)], differences[min(samples - 1, int(samples * 0.975))]


def mcnemar_exact(baseline: list[bool], experiment: list[bool]) -> dict[str, float | int]:
    n = min(len(baseline), len(experiment))
    better = sum(1 for i in range(n) if not baseline[i] and experiment[i])
    worse = sum(1 for i in range(n) if baseline[i] and not experiment[i])
    discordant = better + worse
    if discordant == 0:
        return {"better": better, "worse": worse, "p_value": 1.0}
    tail = sum(math.comb(discordant, k) for k in range(0, min(better, worse) + 1)) / 2**discordant
    return {"better": better, "worse": worse, "p_value": min(1.0, 2 * tail)}
