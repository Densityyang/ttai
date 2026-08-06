"""Immutable, checksummed model profile contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Literal

from src.nl2sql.contracts import ModelDataClassification, ModelStage

ModelTier = Literal["small", "pro"]

_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_MODEL_STAGES: frozenset[ModelStage] = frozenset(
    {"classify", "retrieve", "plan", "generate_sql", "verify", "answer"}
)
_DATA_CLASSIFICATIONS: frozenset[ModelDataClassification] = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
_DEFAULT_DATA_CLASSIFICATIONS: frozenset[ModelDataClassification] = frozenset(
    {"public", "internal"}
)
_TIER_ORDER: dict[ModelTier, int] = {"small": 0, "pro": 1}


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    tier: ModelTier
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    allowed_data_classifications: frozenset[ModelDataClassification] = (
        _DEFAULT_DATA_CLASSIFICATIONS
    )

    def __post_init__(self) -> None:
        provider = self.provider.strip()
        model = self.model.strip()
        if not provider or len(provider) > 128:
            raise ValueError("model target provider must be between 1 and 128 characters")
        if not model or len(model) > 256:
            raise ValueError("model target ID must be between 1 and 256 characters")
        if self.tier not in _TIER_ORDER:
            raise ValueError("model target tier must be small or pro")
        if any(
            not math.isfinite(value) or value < 0
            for value in (self.input_cost_per_million, self.output_cost_per_million)
        ):
            raise ValueError("model target costs must be finite and non-negative")
        classifications = frozenset(self.allowed_data_classifications)
        if not classifications or not classifications <= _DATA_CLASSIFICATIONS:
            raise ValueError("model target data classifications are invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "allowed_data_classifications", classifications)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier,
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
            "allowed_data_classifications": sorted(self.allowed_data_classifications),
        }


@dataclass(frozen=True)
class ModelProfile:
    alias: str
    version: str
    allowed_stages: frozenset[ModelStage]
    primary: ModelTarget
    fallback: ModelTarget | None

    def __post_init__(self) -> None:
        alias = self.alias.strip()
        version = self.version.strip()
        stages = frozenset(self.allowed_stages)
        if not _ALIAS_PATTERN.fullmatch(alias):
            raise ValueError("model profile alias is invalid")
        if not version or len(version) > 128:
            raise ValueError("model profile version must be between 1 and 128 characters")
        if not stages or not stages <= _MODEL_STAGES:
            raise ValueError("model profile stages are invalid")
        targets = (self.primary,) if self.fallback is None else (self.primary, self.fallback)
        if any(target.tier == "pro" for target in targets) and stages != frozenset({"plan"}):
            raise ValueError("pro model targets are plan-only")
        if self.fallback is not None:
            if _TIER_ORDER[self.fallback.tier] > _TIER_ORDER[self.primary.tier]:
                raise ValueError("model fallback cannot upgrade the primary tier")
            if not (
                self.fallback.allowed_data_classifications
                <= self.primary.allowed_data_classifications
            ):
                raise ValueError("model fallback cannot broaden data classification access")
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "allowed_stages", stages)

    @property
    def checksum(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def canonical_payload(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "version": self.version,
            "allowed_stages": sorted(self.allowed_stages),
            "primary": self.primary.canonical_payload(),
            "fallback": self.fallback.canonical_payload() if self.fallback else None,
        }
