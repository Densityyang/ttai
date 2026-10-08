"""Personal semantic-conflict contract (Agent-side, contract-only, no I/O).

FROZEN rule implemented here, not reinterpreted:

* normal product UX avoids duplicate ACTIVE entries for the exact same
  installed metric identity;
* the architecture MUST nevertheless tolerate materially distinct candidates
  for one user request (own SAVED Custom Definition, installed Published
  Metric, installed derivative/fork, independently published definitions);
* candidates are NEVER collapsed by title/name similarity;
* same stable identity + same immutable version => deduplicate;
* otherwise the outcome is ``clarification_required`` and the USER chooses;
* the Agent must not choose using Star, Certification, popularity, install
  count, author identity or model preference;
* the Agent describes material differences and risks - nothing more;
* the choice is RUN-SCOPED by default; a persisted library/default preference
  changes only when the user explicitly asks.

ADDITIVE: this reuses ``SemanticResolution``, ``conflict_ids`` and
``unresolved_slots`` and deliberately does NOT add a fourth resolution outcome.
It performs no I/O and implements no persistence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.nl2sql.semantic.calculation_contract import SemanticResolution

SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

# What a personal-library candidate IS.  These are identity kinds, not ranks.
CandidateKind = Literal[
    "saved_custom_definition",
    "installed_published_metric",
    "installed_derivative",
    "independently_published_metric",
]

# Where the candidate came from. Descriptive only.
CandidateOrigin = Literal["own", "installed"]

# Descriptive certification state.  It is NEVER authority and never selects.
CandidateCertificationState = Literal["unknown", "uncertified", "certified"]

# A material semantic difference axis.  Bounded and closed so "material" is
# testable rather than a free-text judgement.
MaterialDifferenceKind = Literal[
    "unit",
    "null_semantics",
    "population",
    "grain",
    "scope",
    "time_semantics",
    "dimension",
    "numerator_denominator",
    "deduplication",
    "formula_version_lineage",
    "parameter_contract",
    "provenance",
]

# The user choice is RUN-SCOPED unless the user explicitly asks otherwise.
SelectionScope = Literal["run_scoped", "persisted"]

# Signals that must NEVER influence selection.
NON_SELECTING_SIGNALS: Final[frozenset[str]] = frozenset(
    (
        "star_count",
        "certification_state",
        "popularity",
        "install_count",
        "owner_identity",
        "model_preference",
        "display_name_similarity",
    )
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class PersonalCandidateRef(_StrictFrozenModel):
    """The closed identity of ONE personal-library candidate.

    Carries exactly what the comparison needs and nothing that ranks.  Star and
    certification are DESCRIPTIVE context and are excluded from the identity
    checksum, mirroring how AnswerArtifact degradation flags are metadata.
    """

    candidate_id: str = Field(min_length=1, max_length=128)
    kind: CandidateKind
    origin: CandidateOrigin
    # Stable identity + immutable version: the ONLY legitimate dedupe key.
    definition_id: str = Field(min_length=1, max_length=128)
    definition_version: int = Field(ge=1)
    # Display identity participates in EXACT normalized equality only; it must
    # never collapse candidates on similarity.
    display_name: str = Field(min_length=1, max_length=256)
    owner_label: str | None = Field(default=None, max_length=128)
    source_label: str | None = Field(default=None, max_length=256)
    # Descriptive context.  Not authority; not part of the selection checksum.
    certification_state: CandidateCertificationState = "unknown"
    star_count: int = Field(default=0, ge=0)

    @property
    def identity(self) -> tuple[str, int]:
        """The stable identity used for exact-equivalence deduplication."""

        return (self.definition_id, self.definition_version)

    @property
    def identity_checksum(self) -> str:
        """Identity only: descriptive Star/certification are excluded."""

        return _checksum(
            {
                "definition_id": self.definition_id,
                "definition_version": self.definition_version,
                "kind": self.kind,
            }
        )


class MaterialDifference(_StrictFrozenModel):
    """One typed, material semantic difference between candidates."""

    kind: MaterialDifferenceKind
    summary: str = Field(min_length=1, max_length=512)
    risk_note: str | None = Field(default=None, max_length=512)
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=8)


class SemanticConflict(_StrictFrozenModel):
    """The comparison payload presented when materially distinct candidates remain.

    The Agent describes differences and risks; the USER chooses the intended
    business meaning.  There is deliberately NO ``winner``/``selected`` field.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    conflict_id: str = Field(min_length=1, max_length=128)
    required_slot: str = Field(min_length=1, max_length=64)
    candidates: tuple[PersonalCandidateRef, ...] = Field(min_length=2, max_length=16)
    differences: tuple[MaterialDifference, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_conflict(self) -> SemanticConflict:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("conflict candidates must be unique")
        # Every candidate must be materially distinct: two candidates sharing a
        # stable identity + version would have been deduplicated, never presented.
        identities = [candidate.identity for candidate in self.candidates]
        if len(set(identities)) != len(identities):
            raise ValueError("identical identity+version must be deduplicated")
        known = set(ids)
        for difference in self.differences:
            if not set(difference.candidate_ids) <= known:
                raise ValueError("difference must reference presented candidates")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class PersonalSelection(_StrictFrozenModel):
    """An explicit USER selection.  It always wins over resolver output."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    conflict_id: str = Field(min_length=1, max_length=128)
    required_slot: str = Field(min_length=1, max_length=64)
    selected_candidate_id: str = Field(min_length=1, max_length=128)
    # RUN-SCOPED by default.  A persisted preference is only ever produced on an
    # EXPLICIT user request, never by the resolver.
    selection_scope: SelectionScope = "run_scoped"
    explicit_user_request: bool = False

    @model_validator(mode="after")
    def validate_selection(self) -> PersonalSelection:
        if self.selection_scope == "persisted" and not self.explicit_user_request:
            raise ValueError(
                "a persisted preference requires an explicit user request"
            )
        return self


def deduplicate_candidates(
    candidates: tuple[PersonalCandidateRef, ...],
) -> tuple[PersonalCandidateRef, ...]:
    """Drop EXACT identity duplicates (same stable id + immutable version).

    Never collapses on display name, owner, source or any similarity.
    """

    seen: set[tuple[str, int]] = set()
    kept: list[PersonalCandidateRef] = []
    for candidate in candidates:
        if candidate.identity in seen:
            continue
        seen.add(candidate.identity)
        kept.append(candidate)
    return tuple(kept)


def resolve_personal_conflict(
    candidates: tuple[PersonalCandidateRef, ...],
    *,
    required_slot: str = "metric",
    differences: tuple[MaterialDifference, ...] = (),
) -> tuple[SemanticResolution, SemanticConflict | None]:
    """Resolve personal candidates WITHOUT choosing for the user.

    Returns the frozen ``SemanticResolution`` plus, when the user must choose,
    the ``SemanticConflict`` comparison to present.  There is no path that
    selects a winner from Star, Certification or any ranking signal.
    """

    deduped = deduplicate_candidates(candidates)
    if not deduped:
        return SemanticResolution(outcome="no_authoritative_definition"), None
    if len(deduped) == 1:
        return SemanticResolution(outcome="resolved"), None
    conflict = SemanticConflict(
        conflict_id="conflict-" + _checksum([c.identity_checksum for c in deduped])[:32],
        required_slot=required_slot,
        candidates=deduped,
        differences=differences,
    )
    return (
        SemanticResolution(
            outcome="clarification_required",
            unresolved_slots=(required_slot,),
        ),
        conflict,
    )


__all__ = [
    "NON_SELECTING_SIGNALS",
    "SCHEMA_VERSION",
    "CandidateCertificationState",
    "CandidateKind",
    "CandidateOrigin",
    "MaterialDifference",
    "MaterialDifferenceKind",
    "PersonalCandidateRef",
    "PersonalSelection",
    "SelectionScope",
    "SemanticConflict",
    "deduplicate_candidates",
    "resolve_personal_conflict",
]
