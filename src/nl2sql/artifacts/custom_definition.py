"""Custom Definition: six INDEPENDENT axes over a shared CalculationSpec.

Reuses CalculationSpec as the ONLY expression language - there is deliberately
no second formula AST.  The axes are independent fields, NOT a linear lifecycle
enum: DRAFT+SESSION+UNPUBLISHED+UNCERTIFIED+NONE+noncanonical is legal, and so
is CONFIRMED+SAVED+PUBLISHED+CERTIFIED+GOVERNANCE_CANDIDATE+noncanonical.

Governance and Authority are ORTHOGONAL to the four confirmation/retention/
publication/certification axes.  Proposing an object for formal governance never
changes its publication state, and a custom definition ORIGINAL OBJECT stays
noncanonical for its whole life: formal governance creates or links a SEPARATE
canonical identity instead of rewriting this one in place.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.nl2sql.artifacts.definition_semantics import DefinitionSemantics
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
    ParameterSpec,
)

ConfirmationAxis = Literal["DRAFT", "CONFIRMED"]
RetentionAxis = Literal["SESSION", "SAVED"]
PublicationAxis = Literal["UNPUBLISHED", "PUBLISHED"]
CertificationAxis = Literal["UNCERTIFIED", "CERTIFIED"]
# Governance is a PROPOSAL axis, not a lifecycle step.  GOVERNANCE_CANDIDATE
# says "this object has been proposed for formal governance"; it neither
# requires nor implies SAVED/PUBLISHED/CERTIFIED.
GovernanceAxis = Literal["NONE", "GOVERNANCE_CANDIDATE", "UNDER_REVIEW"]
# Authority is the canonicality axis.  It is deliberately two-valued so the axis
# is explicit, but a Custom Definition ORIGINAL OBJECT may only ever be
# "noncanonical": see reject_in_place_canonicalization.
AuthorityAxis = Literal["noncanonical", "canonical"]
DefinitionId = Annotated[str, Field(pattern=r"^def_[0-9a-f]{32}$")]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

# The stable error code shared by every in-place canonicalize refusal.
CUSTOM_DEFINITION_CANONICAL_AUTHORITY_ERROR: Final[str] = (
    "custom_definition_authority_must_remain_noncanonical"
)


class CustomDefinitionAuthorityViolation(ValueError):
    """Typed error: an in-place canonicalize of a Custom Definition is refused.

    A Custom Definition is a NONCANONICAL original object for its whole life.
    Formal governance must create or link a SEPARATE canonical identity and keep
    a provenance link back to this object; it may never flip this object's own
    authority, because doing so would retroactively re-authorize historical
    results that were produced under noncanonical authority.
    """

    code: Final[str] = CUSTOM_DEFINITION_CANONICAL_AUTHORITY_ERROR


def reject_in_place_canonicalization(authority: AuthorityAxis) -> None:
    """The ONE gate: a custom definition original object stays noncanonical."""

    if authority == "canonical":
        raise CustomDefinitionAuthorityViolation(
            "a custom definition original object is noncanonical by invariant; "
            "formal governance must create or link a separate canonical identity "
            "and must not rewrite this object's authority in place"
        )


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
    """Six INDEPENDENT axes with the frozen implication invariants.

    Confirmation/Retention/Publication/Certification keep their existing
    implications.  Governance and Authority are ORTHOGONAL to them: there is no
    aggregate status and no linear chain.  CONFIRMED+SAVED+GOVERNANCE_CANDIDATE+
    noncanonical is a legal, representable state, and so is
    CONFIRMED+SAVED+PUBLISHED+CERTIFIED+GOVERNANCE_CANDIDATE+noncanonical.
    """

    confirmation: ConfirmationAxis = "DRAFT"
    retention: RetentionAxis = "SESSION"
    publication: PublicationAxis = "UNPUBLISHED"
    certification: CertificationAxis = "UNCERTIFIED"
    # The governance PROPOSAL axis.  Independent of publication/certification:
    # being a governance candidate never publishes anything and publishing never
    # nominates anything.
    governance: GovernanceAxis = "NONE"
    # The canonicality axis.  Always "noncanonical" on a Custom Definition.
    authority: AuthorityAxis = "noncanonical"

    def __init__(self, **data: Any) -> None:
        # Direct construction is checked BEFORE pydantic validation so the
        # refusal is a directly catchable typed error, not a generic
        # ValidationError.  Deserialization (model_validate /
        # model_validate_json) still hits validate_axes below.
        if "authority" in data:
            reject_in_place_canonicalization(data["authority"])
        super().__init__(**data)

    @model_validator(mode="after")
    def validate_axes(self) -> DefinitionAxes:
        if self.retention == "SAVED" and self.confirmation != "CONFIRMED":
            raise ValueError("SAVED requires CONFIRMED")
        if self.publication == "PUBLISHED":
            if self.retention != "SAVED" or self.confirmation != "CONFIRMED":
                raise ValueError("PUBLISHED requires SAVED and CONFIRMED")
        if self.certification == "CERTIFIED" and self.publication != "PUBLISHED":
            raise ValueError("CERTIFIED requires PUBLISHED")
        # The custom ORIGINAL OBJECT is noncanonical for its whole life.  Formal
        # governance creates or links a SEPARATE canonical identity and keeps a
        # provenance link; this object's authority is NEVER rewritten in place,
        # so historical noncanonical results can never be retroactively
        # re-authorized.
        reject_in_place_canonicalization(self.authority)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> DefinitionAxes:
        """Guard the ONE documented in-place rewrite path.

        Pydantic's model_copy(update=...) deliberately SKIPS validation, so
        without this guard an authority update to "canonical" would silently
        canonicalize the original object.  The implication axes still copy
        exactly as before.
        """

        if update is not None:
            reject_in_place_canonicalization(
                update.get("authority", self.authority)
            )
        return super().model_copy(update=update, deep=deep)


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
    # A6: the DECLARED definition-level semantics (population, numerator /
    # denominator, deduplication, grain, scope, business time, join, NULL, unit
    # precision, provenance).  OPTIONAL BY CONSTRUCTION: a version that declares
    # no semantics keeps its EXACT pre-existing bytes AND checksum, so every
    # already-persisted definition stays bit-for-bit identical.
    semantics: DefinitionSemantics | None = None
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
        payload: dict[str, object] = {
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
        # COMPATIBILITY (frozen): the semantics checksum is added ONLY when a
        # declaration actually exists.  A semantics-free version therefore
        # hashes the EXACT payload it hashed before this field existed, which
        # keeps every persisted definition bit-for-bit identical, while a
        # version WITH semantics still has a checksum that covers them (two
        # versions differing only by semantics can never share an identity).
        if self.semantics is not None:
            payload["definition_semantics_checksum"] = self.semantics.checksum
        return _checksum(payload)


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
    "CUSTOM_DEFINITION_CANONICAL_AUTHORITY_ERROR",
    "AuthorityAxis",
    "CertificationAxis",
    "ConfirmationAxis",
    "CustomDefinition",
    "CustomDefinitionAuthorityViolation",
    "DefinitionAxes",
    "DefinitionExecutionBinding",
    "DefinitionId",
    "DefinitionSemantics",
    "DefinitionVersion",
    "DefinitionVersionLifecycle",
    "GovernanceAxis",
    "ParameterContract",
    "derive_parameter_contract",
    "PublicationAxis",
    "RetentionAxis",
    "new_definition_id",
    "reject_in_place_canonicalization",
    "utcnow",
]
