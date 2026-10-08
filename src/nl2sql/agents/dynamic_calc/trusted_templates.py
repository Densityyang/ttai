"""Approved, typed dynamic-calculation templates.

No model-produced Python reaches this module. Each template has an explicit
Pydantic input/output contract and is registered in source control before it
can be selected in ``trusted-template`` mode.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class TrustedTemplateError(ValueError):
    """Raised when a caller requests an unknown or invalid approved template."""


class _TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValuesInput(_TemplateModel):
    values: list[float] = Field(min_length=1, max_length=10_000)


class SumValuesOutput(_TemplateModel):
    total: float


class MeanValuesOutput(_TemplateModel):
    mean: float


class RatioInput(_TemplateModel):
    numerator: float
    denominator: float

    @field_validator("denominator")
    @classmethod
    def validate_denominator(cls, value: float) -> float:
        if value == 0:
            raise ValueError("denominator must not be zero")
        return value


class RatioOutput(_TemplateModel):
    ratio: float


TemplateId = Literal["sum_values", "mean_values", "ratio"]
TemplateOutput = SumValuesOutput | MeanValuesOutput | RatioOutput
_TemplateExecutor = Callable[[BaseModel], TemplateOutput]


def _sum_values(payload: BaseModel) -> SumValuesOutput:
    values = ValuesInput.model_validate(payload)
    return SumValuesOutput(total=sum(values.values))


def _mean_values(payload: BaseModel) -> MeanValuesOutput:
    values = ValuesInput.model_validate(payload)
    return MeanValuesOutput(mean=sum(values.values) / len(values.values))


def _ratio(payload: BaseModel) -> RatioOutput:
    values = RatioInput.model_validate(payload)
    return RatioOutput(ratio=values.numerator / values.denominator)


class TemplateMetadata(_TemplateModel):
    """Versioned, deterministic identity of one registered calculation template."""

    template_id: str
    version: str
    input_roles: tuple[str, ...]
    output_role: str

    @property
    def checksum(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TrustedTemplateRegistry:
    """The only calculation executor available in ``trusted-template`` mode.

    Registry membership means "this executor exists and is technically governed".
    It is execution-capability authority only, never business approval authority.
    """

    _templates: dict[str, tuple[type[BaseModel], _TemplateExecutor]] = {
        "sum_values": (ValuesInput, _sum_values),
        "mean_values": (ValuesInput, _mean_values),
        "ratio": (RatioInput, _ratio),
    }

    _metadata: dict[str, TemplateMetadata] = {
        "sum_values": TemplateMetadata(
            template_id="sum_values", version="1.0", input_roles=("values",), output_role="total"
        ),
        "mean_values": TemplateMetadata(
            template_id="mean_values", version="1.0", input_roles=("values",), output_role="mean"
        ),
        "ratio": TemplateMetadata(
            template_id="ratio",
            version="1.0",
            input_roles=("numerator", "denominator"),
            output_role="ratio",
        ),
    }

    @property
    def template_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    @property
    def registry_checksum(self) -> str:
        payload = [self._metadata[key].model_dump(mode="json") for key in sorted(self._metadata)]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def metadata(self, template_id: str) -> TemplateMetadata:
        metadata = self._metadata.get(template_id)
        if metadata is None:
            raise TrustedTemplateError(f"unapproved calculation template: {template_id}")
        return metadata

    def execute(self, template_id: str, inputs: dict[str, Any]) -> TemplateOutput:
        template = self._templates.get(template_id)
        if template is None:
            raise TrustedTemplateError(f"unapproved calculation template: {template_id}")
        input_model, executor = template
        try:
            validated = input_model.model_validate(inputs)
        except ValidationError as exc:
            raise TrustedTemplateError(f"invalid input for {template_id}: {exc}") from exc
        return executor(validated)


trusted_template_registry = TrustedTemplateRegistry()
