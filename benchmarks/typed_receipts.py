"""Typed, reproducible benchmark inputs and release gates."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

MANIFEST_SCHEMA_VERSION: Literal["1.0"] = "1.0"
LEGACY_MANIFEST_SCHEMA_VERSION: Literal["0.9"] = "0.9"


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

    # ── P9A plan-adherence evidence ────────────────────────────────────────
    # A confirmed plan can be re-hashed identically while the actual execution
    # changed its real denominator (e.g. a filter silently dropped).  Keeping
    # both the confirmed checksum and the row count lets the evaluator detect
    # that instead of trusting a matching hash.
    sql_fingerprint: str | None = None
    confirmed_plan_checksum: str | None = None
    observed_plan_checksum: str | None = None
    expected_row_count: int | None = Field(default=None, ge=0)
    # The typed produced value used for oracle comparison.  This is a business
    # value, never a prompt or model input.
    result_value: Any = None
    result_value_sha256: str | None = None


class BenchmarkManifest(_BenchmarkModel):
    """Run manifest.

    P9A deliberately removes the hard-coded three-provider requirement.  The
    enabled provider set and enabled modes are configuration decisions; a run
    with one provider (or none, for a fake-provider harness proof) is valid.
    A caller that genuinely needs a floor declares it with
    required_provider_count.  Unknown legacy keys are preserved in
    legacy_extras by upcast_legacy_manifest rather than being dropped.
    """

    schema_version: Literal["1.0"] = MANIFEST_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    dataset_checksum: str = Field(min_length=16)
    prompt_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    semantic_version: str = Field(min_length=1)
    model_profile_version: str = Field(min_length=1)
    git_revision: str = Field(min_length=7)
    matrix: tuple[str, ...] = ()
    enabled_modes: tuple[str, ...] = ()
    case_selection_checksum: str = ""
    # P9A selection: the checksum above is only meaningful together with the
    # selector version and the seed that produced it.
    selection_version: str = ""
    registry_revision: str = ""
    oracle_version: str = ""
    assertion_version: str = ""
    data_reference: str = ""
    replay_reference: str = ""
    seed: int = Field(default=0, ge=0)
    tolerance: float = Field(default=0.0, ge=0.0)
    required_provider_count: int = Field(default=0, ge=0)
    legacy_extras: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider_scope(self) -> BenchmarkManifest:
        if self.required_provider_count > len(self.matrix):
            raise ValueError("benchmark matrix has fewer providers than required_provider_count")
        if len(set(self.matrix)) != len(self.matrix):
            raise ValueError("benchmark matrix must not repeat a provider alias")
        return self


class NvidiaModelsClient(Protocol):
    async def get(self, path: str) -> Any: ...


async def verify_nvidia_small_model(client: NvidiaModelsClient, model_id: str) -> str:
    """Verify the configured small-model ID against the provider /models response."""
    response = await client.get("/models")
    if getattr(response, "status_code", 500) != 200:
        raise RuntimeError("NVIDIA /models preflight failed")
    payload = response.json()
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    ids = {entry.get("id") for entry in entries if isinstance(entry, dict)}
    if model_id not in ids:
        raise RuntimeError("configured NVIDIA small model was not returned by /models")
    return model_id


class BudgetExceeded(RuntimeError):
    """Raised BEFORE an executor call when the budget cannot cover it."""


@dataclass
class BudgetGate:
    max_total_cost: float
    max_calls: int
    cost_used: float = 0.0
    calls_used: int = 0
    reserved_cost: float = 0.0
    reserved_calls: int = 0

    def remaining_cost(self) -> float:
        return self.max_total_cost - self.cost_used - self.reserved_cost

    def remaining_calls(self) -> int:
        return self.max_calls - self.calls_used - self.reserved_calls

    def precheck(self, *, estimated_cost: float = 0.0, estimated_calls: int = 0) -> None:
        """Check the budget BEFORE the executor runs; raise BudgetExceeded.

        This is the S6 gate: an over-budget case must be rejected while the
        executor call count is still zero.
        """
        if estimated_cost < 0 or estimated_calls < 0:
            raise ValueError("budget estimates must be non-negative")
        if self.calls_used + self.reserved_calls + estimated_calls > self.max_calls:
            raise BudgetExceeded("benchmark budget exhausted: call budget")
        if self.cost_used + self.reserved_cost + estimated_cost > self.max_total_cost:
            raise BudgetExceeded("benchmark budget exhausted: cost budget")

    def reserve(self, *, estimated_cost: float = 0.0, estimated_calls: int = 0) -> None:
        self.precheck(estimated_cost=estimated_cost, estimated_calls=estimated_calls)
        self.reserved_cost += estimated_cost
        self.reserved_calls += estimated_calls

    def release(self, *, estimated_cost: float = 0.0, estimated_calls: int = 0) -> None:
        self.reserved_cost = max(0.0, self.reserved_cost - estimated_cost)
        self.reserved_calls = max(0, self.reserved_calls - estimated_calls)

    def consume(self, receipt: TypedAnswerReceipt) -> None:
        next_cost = self.cost_used + sum(call.estimated_cost for call in receipt.model_calls)
        next_calls = self.calls_used + len(receipt.model_calls)
        if next_cost > self.max_total_cost or next_calls > self.max_calls:
            raise BudgetExceeded("benchmark budget exhausted")
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


# ── 只读 upcast 迁移 ────────────────────────────────────────────────────────


def upcast_legacy_manifest(raw: str | bytes | Mapping[str, Any]) -> BenchmarkManifest:
    """Read an old manifest and upcast it in memory.

    Read-only by construction: nothing is written and no legacy key is
    discarded.  Unknown keys land in legacy_extras so a migration can never
    silently change history.
    """
    if isinstance(raw, bytes):
        payload: Any = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        payload = json.loads(raw)
    else:
        payload = dict(raw)
    if not isinstance(payload, dict):
        raise ValueError("manifest payload must be a JSON object")

    known = set(BenchmarkManifest.model_fields)
    data: dict[str, Any] = {key: value for key, value in payload.items() if key in known}
    extras = {key: value for key, value in payload.items() if key not in known}
    if extras:
        merged = dict(data.get("legacy_extras") or {})
        merged.update(extras)
        data["legacy_extras"] = merged
    data.setdefault("schema_version", MANIFEST_SCHEMA_VERSION)
    return BenchmarkManifest.model_validate(data)


def upcast_manifest_file(path: Path) -> tuple[BenchmarkManifest, str]:
    """Upcast a manifest file without ever rewriting it.

    Returns the manifest plus the sha256 of the original bytes.  The before and
    after byte snapshots must be identical; a migration that edits history is a
    hard error, not a warning.
    """
    before = path.read_bytes()
    manifest = upcast_legacy_manifest(before)
    after = path.read_bytes()
    if before != after:
        raise RuntimeError("legacy manifest migration must not rewrite history")
    return manifest, hashlib.sha256(before).hexdigest()


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


__all__ = [
    "LEGACY_MANIFEST_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "BenchmarkManifest",
    "BudgetExceeded",
    "BudgetGate",
    "ProviderCallReceipt",
    "TypedAnswerReceipt",
    "mcnemar_exact",
    "paired_bootstrap_interval",
    "redacted_sample_id",
    "upcast_legacy_manifest",
    "upcast_manifest_file",
    "validate_receipt",
    "verify_nvidia_small_model",
]
