"""Shared Calculation Runtime execution for one reusable Custom Definition.

This core binds already-resolved scalar inputs to the existing typed
``CalculationSpec`` and ``CalculationExecutionBinding`` contracts.  It does
not accept expression strings, perform I/O, widen authorization, or confer
canonical metric authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.nl2sql.contracts import PlanStepId, TimeRange
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
    InputProvenance,
)
from src.nl2sql.semantic.calculation_runtime import evaluate_calculation

__all__ = [
    "CalculationInputProvenance",
    "CustomCalculationExecutionError",
    "CustomCalculationExecutionResult",
    "ResolvedCalculationInput",
    "execute_custom_calculation",
]

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"
ResolvedScalar = Decimal | int | str | None


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class CustomCalculationExecutionError(RuntimeError):
    """Stable refusal emitted before or by shared Calculation Runtime."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ResolvedCalculationInput(_StrictFrozenModel):
    """One governed scalar already resolved for a declared calculation role."""

    role: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    metric_key: str = Field(min_length=1, max_length=256)
    status: Literal["resolved", "unavailable"] = "resolved"
    value: ResolvedScalar = None
    unit: str = Field(min_length=1, max_length=64)
    data_as_of: datetime | None = None
    time_range: TimeRange | None = None
    provenance: InputProvenance
    source_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$"
    )
    receipt_step_id: PlanStepId | None = None
    fact_id: str | None = Field(default=None, pattern=_CHECKSUM_PATTERN)

    @field_validator("value")
    @classmethod
    def validate_bounded_value(cls, value: ResolvedScalar) -> ResolvedScalar:
        if isinstance(value, str) and len(value) > 256:
            raise ValueError("resolved calculation input string is too large")
        return value

    @model_validator(mode="after")
    def validate_status_value(self) -> ResolvedCalculationInput:
        if self.status == "unavailable" and self.value is not None:
            raise ValueError("unavailable calculation input cannot carry a value")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class CalculationInputProvenance(_StrictFrozenModel):
    """Value-free provenance copied into the execution result."""

    role: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    metric_key: str = Field(min_length=1, max_length=256)
    status: Literal["resolved", "unavailable"]
    unit: str = Field(min_length=1, max_length=64)
    data_as_of: datetime | None = None
    time_range: TimeRange | None = None
    provenance: InputProvenance
    source_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$"
    )
    receipt_step_id: PlanStepId | None = None
    fact_id: str | None = Field(default=None, pattern=_CHECKSUM_PATTERN)
    input_checksum: str = Field(pattern=_CHECKSUM_PATTERN)


class CustomCalculationExecutionResult(_StrictFrozenModel):
    """A run result for reusable semantics; never a canonical published value."""

    schema_version: Literal["1.0"] = "1.0"
    calculation_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    spec_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    binding_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    status: Literal["succeeded"] = "succeeded"
    value: Decimal
    unit: str = Field(min_length=1, max_length=64)
    input_provenance: tuple[CalculationInputProvenance, ...] = Field(
        min_length=1, max_length=32
    )
    data_as_of: datetime | None = None
    time_range: TimeRange | None = None
    calculation_scope: Literal["reusable_custom_definition"] = (
        "reusable_custom_definition"
    )

    @field_validator("input_provenance")
    @classmethod
    def validate_unique_roles(
        cls, value: tuple[CalculationInputProvenance, ...]
    ) -> tuple[CalculationInputProvenance, ...]:
        roles = tuple(item.role for item in value)
        if len(set(roles)) != len(roles):
            raise ValueError("calculation result input roles must be unique")
        return value


def execute_custom_calculation(
    *,
    spec: CalculationSpec,
    binding: CalculationExecutionBinding,
    inputs: tuple[ResolvedCalculationInput, ...],
) -> CustomCalculationExecutionResult:
    """Validate identities/binding, then delegate arithmetic to shared runtime."""

    binding_failures = binding.binding_failures(spec)
    if binding_failures:
        raise CustomCalculationExecutionError(binding_failures[0])

    roles = tuple(item.role for item in inputs)
    if len(set(roles)) != len(roles):
        raise CustomCalculationExecutionError("calculation_input_role_duplicate")
    declared = {item.role: item for item in spec.inputs}
    supplied = {item.role: item for item in inputs}
    if set(supplied) - set(declared):
        raise CustomCalculationExecutionError("calculation_input_role_unknown")

    runtime_inputs: dict[str, object] = {}
    provenance: list[CalculationInputProvenance] = []
    for role, requirement in declared.items():
        resolved = supplied.get(role)
        if resolved is None:
            if requirement.required:
                raise CustomCalculationExecutionError("calculation_input_missing")
            continue
        if requirement.metric_key is None or resolved.metric_key != requirement.metric_key:
            raise CustomCalculationExecutionError("calculation_input_metric_mismatch")
        if resolved.provenance != requirement.provenance:
            raise CustomCalculationExecutionError("calculation_input_provenance_mismatch")
        if resolved.status == "unavailable":
            if requirement.required:
                raise CustomCalculationExecutionError("calculation_input_unavailable")
            continue
        runtime_inputs[role] = resolved.value
        provenance.append(_project_provenance(resolved))

    try:
        evaluated = evaluate_calculation(spec, inputs=runtime_inputs, binding=binding)
    except RuntimeError as exc:
        code = getattr(exc, "code", "calculation_runtime_failed")
        raise CustomCalculationExecutionError(str(code)) from exc

    provenance_tuple = tuple(provenance)
    return CustomCalculationExecutionResult(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        binding_checksum=binding.checksum,
        value=evaluated.value,
        unit=spec.custom_unit_id if spec.custom_unit_id is not None else spec.unit,
        input_provenance=provenance_tuple,
        data_as_of=_conservative_data_as_of(provenance_tuple),
        time_range=_common_time_range(provenance_tuple),
    )


def _project_provenance(
    resolved: ResolvedCalculationInput,
) -> CalculationInputProvenance:
    return CalculationInputProvenance(
        role=resolved.role,
        metric_key=resolved.metric_key,
        status=resolved.status,
        unit=resolved.unit,
        data_as_of=resolved.data_as_of,
        time_range=resolved.time_range,
        provenance=resolved.provenance,
        source_id=resolved.source_id,
        receipt_step_id=resolved.receipt_step_id,
        fact_id=resolved.fact_id,
        input_checksum=resolved.checksum,
    )


def _conservative_data_as_of(
    inputs: tuple[CalculationInputProvenance, ...],
) -> datetime | None:
    values = tuple(item.data_as_of for item in inputs)
    if any(value is None or value.tzinfo is None for value in values):
        return None
    return min(value.astimezone(UTC) for value in values if value is not None)


def _common_time_range(
    inputs: tuple[CalculationInputProvenance, ...],
) -> TimeRange | None:
    values = tuple(item.time_range for item in inputs)
    if not values or any(value is None for value in values):
        return None
    first = values[0]
    if first is not None and all(value == first for value in values[1:]):
        return first
    return None
