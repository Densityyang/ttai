"""Pure personal-library conflict projection over actual semantic packages.

Star and certification are copied for display only.  The service never ranks,
recommends, selects, persists, reads HTTP state, or infers current lifecycle
state for a historical version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.nl2sql.artifacts.custom_definition import (
    DefinitionVersion,
    DefinitionVersionLifecycle,
    ParameterContract,
)
from src.nl2sql.artifacts.publication import PublishedVersion
from src.nl2sql.semantic.calculation_contract import CalculationSpec, SemanticResolution
from src.nl2sql.semantic.personal_conflict_contract import (
    CandidateCertificationState,
    MaterialDifference,
    MaterialDifferenceKind,
    PersonalCandidateRef,
    PersonalSelection,
    SemanticConflict,
    resolve_personal_conflict,
)

__all__ = [
    "CandidateLineage",
    "ConflictComparison",
    "ConflictComparisonCandidate",
    "PersonalConflictProjection",
    "PersonalConflictServiceError",
    "ProjectedPersonalCandidate",
    "project_installed_published_candidate",
    "project_own_saved_candidate",
    "resolve_personal_candidate_conflict",
    "validate_personal_selection",
]

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class PersonalConflictServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CandidateLineage(_StrictFrozenModel):
    """Identity/version lineage, descriptive and never selection authority."""

    identity_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    derived_from_identity: str | None = Field(default=None, max_length=128)
    derived_from_version: int | None = Field(default=None, ge=1)
    source_definition_id: str = Field(min_length=1, max_length=128)
    source_definition_version: int = Field(ge=1)
    source_definition_checksum: str = Field(pattern=_CHECKSUM_PATTERN)

    @model_validator(mode="after")
    def validate_derived_pair(self) -> CandidateLineage:
        if (self.derived_from_identity is None) != (self.derived_from_version is None):
            raise ValueError("derived lineage identity and version must appear together")
        return self


class ProjectedPersonalCandidate(_StrictFrozenModel):
    """A resolver ref plus the exact reusable semantic package it represents."""

    reference: PersonalCandidateRef
    lineage: CandidateLineage
    semantic_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    calculation: CalculationSpec
    parameter_contract: ParameterContract

    @model_validator(mode="after")
    def validate_semantic_package(self) -> ProjectedPersonalCandidate:
        if (
            self.reference.definition_id != self.lineage.identity_id
            or self.reference.definition_version != self.lineage.version
        ):
            raise ValueError("candidate reference identity must match its lineage")
        if self.reference.origin == "own" and self.reference.kind != "saved_custom_definition":
            raise ValueError("own candidate must be a saved custom definition")
        if self.reference.origin == "installed" and self.reference.kind not in {
            "installed_published_metric",
            "installed_derivative",
            "independently_published_metric",
        }:
            raise ValueError("installed candidate must use an installed identity kind")
        if self.parameter_contract.parameters != tuple(self.calculation.parameters):
            raise ValueError("candidate parameter contract must match its calculation")
        expected = _semantic_checksum(self.calculation, self.parameter_contract)
        if self.semantic_checksum != expected:
            raise ValueError("candidate semantic checksum does not match its package")
        return self


class ConflictComparisonCandidate(_StrictFrozenModel):
    """UI-ready descriptive row.  Deliberately has no rank or recommendation."""

    candidate_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    origin: Literal["own", "installed"]
    owner_label: str | None = Field(default=None, max_length=128)
    source_label: str | None = Field(default=None, max_length=256)
    certification_state: CandidateCertificationState
    star_count: int = Field(ge=0)
    lineage: CandidateLineage
    semantic_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    material_difference_kinds: tuple[MaterialDifferenceKind, ...] = Field(
        default=(), max_length=16
    )


class ConflictComparison(_StrictFrozenModel):
    conflict_id: str = Field(min_length=1, max_length=128)
    required_slot: str = Field(min_length=1, max_length=64)
    candidates: tuple[ConflictComparisonCandidate, ...] = Field(
        min_length=2, max_length=16
    )

    @field_validator("candidates")
    @classmethod
    def validate_unique_candidates(
        cls, value: tuple[ConflictComparisonCandidate, ...]
    ) -> tuple[ConflictComparisonCandidate, ...]:
        ids = tuple(item.candidate_id for item in value)
        if len(set(ids)) != len(ids):
            raise ValueError("conflict comparison candidates must be unique")
        return value


class PersonalConflictProjection(_StrictFrozenModel):
    resolution: SemanticResolution
    semantic_conflict: SemanticConflict | None = None
    conflict_comparison: ConflictComparison | None = None
    # Present only after an explicit run-scoped selection has been consumed by
    # the subsequent semantic-resolution call.
    selected_candidate_id: str | None = Field(default=None, max_length=128)
    selected_definition_id: str | None = Field(default=None, max_length=128)
    selected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_projection(self) -> PersonalConflictProjection:
        requires_choice = self.resolution.outcome == "clarification_required"
        if requires_choice != (self.semantic_conflict is not None):
            raise ValueError("clarification outcome must carry a semantic conflict")
        if requires_choice != (self.conflict_comparison is not None):
            raise ValueError("clarification outcome must carry a conflict comparison")
        return self


def project_own_saved_candidate(
    *,
    version: DefinitionVersion,
    lifecycle: DefinitionVersionLifecycle,
    owner_user_id: str,
    owner_label: str | None = None,
    source_label: str = "Own saved custom definition",
    certification_state: CandidateCertificationState = "uncertified",
    star_count: int = 0,
) -> ProjectedPersonalCandidate:
    """Project one exact SAVED version using its exact historical lifecycle."""

    if lifecycle.retention != "SAVED" or lifecycle.confirmation != "CONFIRMED":
        raise PersonalConflictServiceError("personal_candidate_not_saved_confirmed")
    if not owner_user_id.strip():
        raise PersonalConflictServiceError("personal_candidate_owner_missing")
    lineage = CandidateLineage(
        identity_id=version.definition_id,
        version=version.version,
        derived_from_identity=version.derived_from_definition_id,
        derived_from_version=version.derived_from_version,
        source_definition_id=version.definition_id,
        source_definition_version=version.version,
        source_definition_checksum=version.checksum,
    )
    return _candidate(
        reference=PersonalCandidateRef(
            candidate_id=_candidate_id("own", version.definition_id, version.version),
            kind="saved_custom_definition",
            origin="own",
            definition_id=version.definition_id,
            definition_version=version.version,
            display_name=version.title,
            owner_label=owner_label or owner_user_id,
            source_label=source_label,
            certification_state=certification_state,
            star_count=star_count,
        ),
        lineage=lineage,
        calculation=version.calculation,
        parameter_contract=version.parameter_contract,
    )


def project_installed_published_candidate(
    *,
    published: PublishedVersion,
    certification_state: CandidateCertificationState,
    star_count: int,
) -> ProjectedPersonalCandidate:
    """Project one explicitly installed, semantically usable published version."""

    semantic = published.semantic
    if semantic is None:
        raise PersonalConflictServiceError("personal_candidate_semantics_unavailable")
    lineage = CandidateLineage(
        identity_id=published.identity_id,
        version=published.version,
        derived_from_identity=published.derived_from_identity,
        derived_from_version=published.derived_from_version,
        source_definition_id=semantic.source_definition_id,
        source_definition_version=semantic.source_definition_version,
        source_definition_checksum=semantic.source_definition_checksum,
    )
    return _candidate(
        reference=PersonalCandidateRef(
            candidate_id=_candidate_id(
                "installed", published.identity_id, published.version
            ),
            kind=(
                "installed_derivative"
                if published.derived_from_identity is not None
                else "installed_published_metric"
            ),
            origin="installed",
            definition_id=published.identity_id,
            definition_version=published.version,
            display_name=published.title,
            owner_label=published.owner_label,
            source_label=published.source_label,
            certification_state=certification_state,
            star_count=star_count,
        ),
        lineage=lineage,
        calculation=semantic.calculation,
        parameter_contract=semantic.parameter_contract,
    )


def resolve_personal_candidate_conflict(
    candidates: tuple[ProjectedPersonalCandidate, ...],
    *,
    required_slot: str = "metric",
) -> PersonalConflictProjection:
    """Compare actual packages, then reuse the frozen non-selecting resolver."""

    if len(candidates) > 16:
        raise PersonalConflictServiceError("personal_candidate_limit_exceeded")
    semantic_by_identity: dict[tuple[str, int], str] = {}
    for candidate in candidates:
        identity = candidate.reference.identity
        previous = semantic_by_identity.setdefault(identity, candidate.semantic_checksum)
        if previous != candidate.semantic_checksum:
            raise PersonalConflictServiceError(
                "personal_candidate_identity_semantic_mismatch"
            )
    candidate_ids = tuple(item.reference.candidate_id for item in candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise PersonalConflictServiceError("personal_candidate_id_duplicate")

    differences = _material_differences(candidates)
    resolution, conflict = resolve_personal_conflict(
        tuple(item.reference for item in candidates),
        required_slot=required_slot,
        differences=differences,
    )
    if conflict is None:
        return PersonalConflictProjection(resolution=resolution)

    by_id = {item.reference.candidate_id: item for item in candidates}
    comparison_rows: list[ConflictComparisonCandidate] = []
    for reference in conflict.candidates:
        candidate = by_id[reference.candidate_id]
        kinds = tuple(
            difference.kind
            for difference in conflict.differences
            if reference.candidate_id in difference.candidate_ids
        )
        comparison_rows.append(
            ConflictComparisonCandidate(
                candidate_id=reference.candidate_id,
                display_name=reference.display_name,
                origin=reference.origin,
                owner_label=reference.owner_label,
                source_label=reference.source_label,
                certification_state=reference.certification_state,
                star_count=reference.star_count,
                lineage=candidate.lineage,
                semantic_checksum=candidate.semantic_checksum,
                material_difference_kinds=cast(
                    "tuple[MaterialDifferenceKind, ...]", tuple(dict.fromkeys(kinds))
                ),
            )
        )
    return PersonalConflictProjection(
        resolution=resolution,
        semantic_conflict=conflict,
        conflict_comparison=ConflictComparison(
            conflict_id=conflict.conflict_id,
            required_slot=conflict.required_slot,
            candidates=tuple(comparison_rows),
        ),
    )


def validate_personal_selection(
    selection: PersonalSelection,
    *,
    projection: PersonalConflictProjection,
) -> PersonalSelection:
    """Validate an explicit selection against this exact conflict, without I/O."""

    conflict = projection.semantic_conflict
    if conflict is None:
        raise PersonalConflictServiceError("personal_selection_conflict_missing")
    if selection.conflict_id != conflict.conflict_id:
        raise PersonalConflictServiceError("personal_selection_conflict_mismatch")
    if selection.required_slot != conflict.required_slot:
        raise PersonalConflictServiceError("personal_selection_slot_mismatch")
    if selection.selected_candidate_id not in {
        candidate.candidate_id for candidate in conflict.candidates
    }:
        raise PersonalConflictServiceError("personal_selection_candidate_mismatch")
    return selection


def _candidate(
    *,
    reference: PersonalCandidateRef,
    lineage: CandidateLineage,
    calculation: CalculationSpec,
    parameter_contract: ParameterContract,
) -> ProjectedPersonalCandidate:
    return ProjectedPersonalCandidate(
        reference=reference,
        lineage=lineage,
        semantic_checksum=_semantic_checksum(calculation, parameter_contract),
        calculation=calculation,
        parameter_contract=parameter_contract,
    )


def _semantic_checksum(
    calculation: CalculationSpec,
    parameter_contract: ParameterContract,
) -> str:
    return _checksum(
        {
            "calculation": calculation.model_dump(mode="json"),
            "parameter_contract": parameter_contract.model_dump(mode="json"),
        }
    )


def _candidate_id(prefix: str, identity: str, version: int) -> str:
    return f"candidate-{prefix}-" + _checksum([identity, version])[:32]


def _material_differences(
    candidates: tuple[ProjectedPersonalCandidate, ...],
) -> tuple[MaterialDifference, ...]:
    if len(candidates) < 2:
        return ()
    candidate_ids = tuple(item.reference.candidate_id for item in candidates)
    differences: list[MaterialDifference] = []

    units = {
        (
            item.calculation.unit,
            item.calculation.custom_unit_id,
        )
        for item in candidates
    }
    if len(units) > 1:
        differences.append(
            MaterialDifference(
                kind="unit",
                summary="Candidates declare different result units.",
                risk_note="Values with different units are not interchangeable.",
                candidate_ids=candidate_ids,
            )
        )

    parameter_contracts = {item.parameter_contract.checksum for item in candidates}
    if len(parameter_contracts) > 1:
        differences.append(
            MaterialDifference(
                kind="parameter_contract",
                summary="Candidates declare different parameter contracts.",
                risk_note="A run binding valid for one candidate may be invalid for another.",
                candidate_ids=candidate_ids,
            )
        )

    input_contracts = {
        _canonical_json(
            [input_spec.model_dump(mode="json") for input_spec in item.calculation.inputs]
        )
        for item in candidates
    }
    if len(input_contracts) > 1:
        differences.append(
            MaterialDifference(
                kind="provenance",
                summary="Candidates require different metric identities or provenance.",
                risk_note="The candidates are bound to different governed inputs.",
                candidate_ids=candidate_ids,
            )
        )

    formula_shapes = {
        _canonical_json(
            {
                "expression": item.calculation.expression.model_dump(mode="json"),
                "precision": item.calculation.precision,
                "rounding": item.calculation.rounding,
            }
        )
        for item in candidates
    }
    lineages = {_canonical_json(item.lineage.model_dump(mode="json")) for item in candidates}
    if len(formula_shapes) > 1 or len(lineages) > 1:
        differences.append(
            MaterialDifference(
                kind="formula_version_lineage",
                summary="Candidates have different formula semantics or version lineage.",
                risk_note="The user must choose the intended reusable definition.",
                candidate_ids=candidate_ids,
            )
        )
    return tuple(differences)
