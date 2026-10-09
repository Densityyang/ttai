"""A6 material-semantic axes for a Custom Definition.

A6 (frozen): a SUBSTANTIVE semantic change -- numerator, denominator,
population, deduplication, business time, join, NULL semantics, business
significant unit precision, or a Parameter Contract extension -- requires a NEW
Draft/version and a NEW business decision.  A5 (frozen): auth revision, active
release, data snapshot, a concrete month/store_scope, budget, freshness and
ordinary rerun time must NOT create a version.

This module carries ONLY the declarative definition-level semantics and a PURE
diff over them.  It deliberately does NOT extend the shared typed calculation
core that A9 froze: the expression language stays `CalculationSpec`, and these
axes describe the DECLARATION around it.  INVARIANT (A9): a shared semantic
representation is never a shared authority -- nothing here grants canonicality.

Everything is OPTIONAL so a definition that declares no semantics keeps its
exact current bytes and checksums; the version boundary is wired in separately.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The definition-level axes.  The vocabulary is SHARED with the candidate
# conflict contract (src/nl2sql/semantic/personal_conflict_contract.py) so the
# product has ONE semantic vocabulary rather than two.
SemanticAxis = Literal[
    "expression",
    "parameter_contract",
    "population",
    "numerator_denominator",
    "deduplication",
    "grain",
    "scope",
    "time_semantics",
    "join_semantics",
    "null_semantics",
    "unit_precision",
    "provenance",
]

# Reporting order.  A diff ALWAYS returns axes in this order, so the same change
# can never produce two different tuples (replayable and comparable).
AXIS_ORDER: tuple[SemanticAxis, ...] = (
    "expression",
    "parameter_contract",
    "population",
    "numerator_denominator",
    "deduplication",
    "grain",
    "scope",
    "time_semantics",
    "join_semantics",
    "null_semantics",
    "unit_precision",
    "provenance",
)

MATERIAL_AXES: frozenset[SemanticAxis] = frozenset(AXIS_ORDER)

Deduplication = Literal["none", "distinct_rows", "distinct_key"]
NullSemantics = Literal["exclude", "include", "propagate"]

_DECLARED = 512


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class DefinitionSemantics(_StrictFrozenModel):
    """The DECLARED definition-level semantics.

    Every field is optional: `None` means "this definition does not declare that
    axis", which is DIFFERENT from declaring an explicit value.  The declarative
    strings are bounded and carry no authority -- they describe the business
    meaning, they never select data, scope or permission.
    """

    population: str | None = Field(default=None, max_length=_DECLARED)
    numerator: str | None = Field(default=None, max_length=_DECLARED)
    denominator: str | None = Field(default=None, max_length=_DECLARED)
    deduplication: Deduplication | None = None
    grain: str | None = Field(default=None, max_length=_DECLARED)
    scope: str | None = Field(default=None, max_length=_DECLARED)
    time_semantics: str | None = Field(default=None, max_length=_DECLARED)
    join_semantics: str | None = Field(default=None, max_length=_DECLARED)
    null_semantics: NullSemantics | None = None
    unit_precision: str | None = Field(default=None, max_length=_DECLARED)
    provenance: str | None = Field(default=None, max_length=_DECLARED)

    @property
    def checksum(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()

    def axes(self) -> tuple[SemanticAxis, ...]:
        """The axes this declaration actually DECLARES, in reporting order."""

        declared: list[SemanticAxis] = []
        if self.population is not None:
            declared.append("population")
        if self.numerator is not None or self.denominator is not None:
            declared.append("numerator_denominator")
        if self.deduplication is not None:
            declared.append("deduplication")
        if self.grain is not None:
            declared.append("grain")
        if self.scope is not None:
            declared.append("scope")
        if self.time_semantics is not None:
            declared.append("time_semantics")
        if self.join_semantics is not None:
            declared.append("join_semantics")
        if self.null_semantics is not None:
            declared.append("null_semantics")
        if self.unit_precision is not None:
            declared.append("unit_precision")
        if self.provenance is not None:
            declared.append("provenance")
        return tuple(axis for axis in AXIS_ORDER if axis in declared)


class SemanticDeclaration(_StrictFrozenModel):
    """The full declarative surface a version boundary must compare.

    Holds the CHECKSUMS of the expression and the Parameter Contract rather than
    the models themselves, so this module never imports the definition module
    (that would be a cycle) and never re-derives someone else's identity.
    """

    expression_checksum: str | None = Field(default=None, max_length=128)
    parameter_contract_checksum: str | None = Field(default=None, max_length=128)
    semantics: DefinitionSemantics | None = None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def _differs(before: str | None, after: str | None) -> bool:
    return before != after


def semantic_axes(
    before: SemanticDeclaration | None,
    after: SemanticDeclaration | None,
) -> tuple[SemanticAxis, ...]:
    """The axes that DIFFER between two declarations, in AXIS_ORDER.

    PURE: it reads nothing, writes nothing and never mutates its inputs.  An
    absent declaration (`None`) is treated as "declares nothing", so the first
    declaration of any axis is reported as a change.
    """

    left = before if before is not None else SemanticDeclaration()
    right = after if after is not None else SemanticDeclaration()
    left_semantics = left.semantics or DefinitionSemantics()
    right_semantics = right.semantics or DefinitionSemantics()

    changed: set[SemanticAxis] = set()
    if _differs(left.expression_checksum, right.expression_checksum):
        changed.add("expression")
    if _differs(left.parameter_contract_checksum, right.parameter_contract_checksum):
        changed.add("parameter_contract")
    if _differs(left_semantics.population, right_semantics.population):
        changed.add("population")
    if _differs(left_semantics.numerator, right_semantics.numerator) or _differs(
        left_semantics.denominator, right_semantics.denominator
    ):
        changed.add("numerator_denominator")
    if _differs(left_semantics.deduplication, right_semantics.deduplication):
        changed.add("deduplication")
    if _differs(left_semantics.grain, right_semantics.grain):
        changed.add("grain")
    if _differs(left_semantics.scope, right_semantics.scope):
        changed.add("scope")
    if _differs(left_semantics.time_semantics, right_semantics.time_semantics):
        changed.add("time_semantics")
    if _differs(left_semantics.join_semantics, right_semantics.join_semantics):
        changed.add("join_semantics")
    if _differs(left_semantics.null_semantics, right_semantics.null_semantics):
        changed.add("null_semantics")
    if _differs(left_semantics.unit_precision, right_semantics.unit_precision):
        changed.add("unit_precision")
    if _differs(left_semantics.provenance, right_semantics.provenance):
        changed.add("provenance")
    return tuple(axis for axis in AXIS_ORDER if axis in changed)


def is_material_change(
    before: SemanticDeclaration | None,
    after: SemanticDeclaration | None,
) -> bool:
    """Whether the change is SUBSTANTIVE under A6 (any axis differs).

    A change with NO differing axis -- a title edit, a re-run timestamp, a new
    auth revision, a new data snapshot -- is NOT material and must not create a
    DefinitionVersion (A5).
    """

    return bool(semantic_axes(before, after))


__all__ = [
    "AXIS_ORDER",
    "MATERIAL_AXES",
    "DefinitionSemantics",
    "SemanticAxis",
    "SemanticDeclaration",
    "is_material_change",
    "semantic_axes",
]
