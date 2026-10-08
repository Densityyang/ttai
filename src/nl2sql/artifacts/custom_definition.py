"""Custom Definition: four INDEPENDENT axes over a shared CalculationSpec.

Reuses CalculationSpec as the ONLY expression language - there is deliberately
no second formula AST.  The four axes are independent fields, NOT a linear
lifecycle enum: DRAFT+SESSION+UNPUBLISHED+UNCERTIFIED is legal, and so is
CONFIRMED+SAVED+PUBLISHED+CERTIFIED.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
    ParameterSpec,
)

ConfirmationAxis = Literal["DRAFT", "CONFIRMED"]
RetentionAxis = Literal["SESSION", "SAVED"]
PublicationAxis = Literal["UNPUBLISHED", "PUBLISHED"]
CertificationAxis = Literal["UNCERTIFIED", "CERTIFIED"]
DefinitionId = Annotated[str, Field(pattern=r"^def_[0-9a-f]{32}$")]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


class ParameterContract(_StrictFrozenModel):
    """The declared parameter SURFACE (not concrete per-run values).

    Changes here are a SEMANTIC change and require a new DefinitionVersion.
    """

    parameters: tuple[ParameterSpec, ...] = Field(default=(), max_length=32)

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class DefinitionAxes(_StrictFrozenModel):
    """Four independent axes with the frozen implication invariants."""

    confirmation: ConfirmationAxis = "DRAFT"
    retention: RetentionAxis = "SESSION"
    publication: PublicationAxis = "UNPUBLISHED"
    certification: CertificationAxis = "UNCERTIFIED"

    @model_validator(mode="after")
    def validate_axes(self) -> DefinitionAxes:
        if self.retention == "SAVED" and self.confirmation != "CONFIRMED":
            raise ValueError("SAVED requires CONFIRMED")
        if self.publication == "PUBLISHED":
            if self.retention != "SAVED" or self.confirmation != "CONFIRMED":
                raise ValueError("PUBLISHED requires SAVED and CONFIRMED")
        if self.certification == "CERTIFIED" and self.publication != "PUBLISHED":
            raise ValueError("CERTIFIED requires PUBLISHED")
        return self


def derive_parameter_contract(calculation: CalculationSpec) -> ParameterContract:
    """The ParameterContract is DERIVED from CalculationSpec.parameters.

    The reusable business contract and the runtime-validated surface must never
    describe different parameter sets.
    """

    return ParameterContract(parameters=tuple(calculation.parameters))


class DefinitionVersion(_StrictFrozenModel):
    """One IMMUTABLE definition version.  The version holds the semantics.

    The checksum covers the STABLE business semantics, the parameter CONTRACT
    and the lineage link.  It deliberately EXCLUDES: current authorization, its
    revision, the active release, the schema snapshot, request budget, concrete
    per-run parameter values, and timestamps.
    """

    schema_version: Literal["1.0"] = "1.0"
    definition_id: DefinitionId
    version: int = Field(ge=1)
    calculation: CalculationSpec
    parameter_contract: ParameterContract = ParameterContract()
    title: str = Field(min_length=1, max_length=256)
    semantic_closed: bool = False
    # Fork lineage: the version this one was derived from, if any.
    derived_from_definition_id: DefinitionId | None = None
    derived_from_version: int | None = Field(default=None, ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_parameter_coherence(self) -> DefinitionVersion:
        """The declared contract must equal the calculation's parameters."""

        if self.parameter_contract.parameters != tuple(self.calculation.parameters):
            raise ValueError(
                "parameter contract must match the calculation parameters"
            )
        return self

    @property
    def checksum(self) -> str:
        return _checksum(
            {
                "schema_version": self.schema_version,
                "definition_id": self.definition_id,
                "version": self.version,
                "calculation_spec_checksum": self.calculation.checksum,
                "parameter_contract_checksum": self.parameter_contract.checksum,
                "title": self.title,
                "semantic_closed": self.semantic_closed,
                "derived_from_definition_id": self.derived_from_definition_id,
                "derived_from_version": self.derived_from_version,
            }
        )


class DefinitionVersionLifecycle(_StrictFrozenModel):
    """The PRIVATE per-version lifecycle of ONE exact definition version.

    ``CustomDefinition.axes`` is only a PROJECTION of the CURRENT version, so a
    revision resets it.  Historical publication eligibility must nevertheless be
    provable for an EXACT version, so the confirmation/retention state is stored
    PER (definition_id, version) and is never rewritten when a later revision is
    opened.

    Deliberately EXCLUDES publication/certification: that historical truth is
    owned by the PublicationCatalogue and must not be duplicated here.
    """

    confirmation: ConfirmationAxis = "DRAFT"
    retention: RetentionAxis = "SESSION"

    @model_validator(mode="after")
    def validate_lifecycle(self) -> DefinitionVersionLifecycle:
        if self.retention == "SAVED" and self.confirmation != "CONFIRMED":
            raise ValueError("SAVED requires CONFIRMED")
        return self


class CustomDefinition(_StrictFrozenModel):
    """Stable identity with its axes and current immutable version."""

    definition_id: DefinitionId
    owner_user_id: str = Field(min_length=1, max_length=256)
    axes: DefinitionAxes = DefinitionAxes()
    current_version: DefinitionVersion


class DefinitionExecutionBinding(_StrictFrozenModel):
    """Concrete per-run parameter VALUES for one exact definition version.

    Changing a parameter VALUE produces a new binding and NEVER a new
    DefinitionVersion.
    """

    definition_id: DefinitionId
    version: int = Field(ge=1)
    definition_checksum: Checksum
    binding: CalculationExecutionBinding


def new_definition_id() -> str:
    from uuid import uuid4

    return "def_" + uuid4().hex


__all__ = [
    "CertificationAxis",
    "ConfirmationAxis",
    "CustomDefinition",
    "DefinitionAxes",
    "DefinitionExecutionBinding",
    "DefinitionId",
    "DefinitionVersion",
    "DefinitionVersionLifecycle",
    "ParameterContract",
    "derive_parameter_contract",
    "PublicationAxis",
    "RetentionAxis",
    "new_definition_id",
    "utcnow",
]
