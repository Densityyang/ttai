"""Shared typed calculation semantic contract (contract-only slice).

MODE_SEMANTIC_SHARED_CALC_CONTRACT_V1.

This module freezes the SHARED semantic representation that QUERY AD_HOC,
Custom Definition and -- where applicable -- canonical approved compute may
reuse, so those product wrappers do not diverge into incompatible expressions.
It is the semantic / parameter / provenance contract only.

INVARIANTS
----------
* A shared semantic representation is NOT shared authority.  Nothing in this
  module grants, encodes or implies canonical metric authority, definition
  identity, confirmation, retention or governance.  Canonical authority stays
  in the formal ApprovedCalculationBinding/catalog path; Custom Definition
  identity/lifecycle stays outside these expression semantics.  A strict
  CalculationSpec therefore REJECTS injected authority/lifecycle fields.
* A reusable CalculationSpec DECLARES parameters; a per-run binding supplies
  concrete values.  A contract-valid parameter value change never changes the
  spec identity/checksum and never mechanically requires business confirmation.
* Expression structure is typed and bounded.  There is no arbitrary Python and
  no untyped formula-string-as-authority shortcut.
* Input REQUIREMENTS are implemented now: a declared role plus the provenance/
  identity it must resolve to.  Concrete per-run runtime input dataset/receipt
  binding is deliberately NOT part of this slice and is deferred until a
  non-speculative shape exists.  CalculationExecutionBinding therefore binds
  parameter values only, not input datasets or receipts.

This module performs no I/O and enables no execution behaviour: no AD_HOC
runtime, no mode classifier, no persistence, no catalog wiring.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Final[Literal["1.1"]] = "1.1"

RoleName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
ParameterName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
CalculationId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

# --- typed business-value policy vocabularies ---------------------------------
# V1 has NO anonymous unit: every unit is either a closed built-in literal or a
# named bounded custom unit.  Anonymous "custom" is intentionally absent so two
# different business-significant custom units can never collapse.
UnitPolicy = Literal["count", "percent", "ratio", "currency_cny", "seconds", "custom_unit"]
# Bounded, non-degenerate custom-unit identity: no leading/trailing/doubled
# separators, and (see CalculationSpec) it must not shadow a built-in literal.
CustomUnitId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$")]
BUILTIN_UNIT_LITERALS: Final[frozenset[str]] = frozenset((*get_args(UnitPolicy), "custom"))
RoundingPolicy = Literal["half_up", "half_even", "half_down", "floor", "ceil"]
Precision = Annotated[int, Field(ge=0, le=12)]

# Declared parameter value types (declaration side of the Parameter Contract).
ParameterValueType = Literal["string", "integer", "decimal", "date", "boolean"]

# --- typed provenance requirement ---------------------------------------------
# Stable input identity/provenance roles a declared input must resolve to.  They
# describe reusable business meaning, never the current run's auth/data state.
InputProvenance = Literal[
    "published_gold",
    "definition_backed_computed",
    "product_published_state",
    "ad_hoc_metric",
]

# --- bounded expression tree --------------------------------------------------
# V1 needs only the operations required to safely express the currently planned
# shared calculation semantics.  This is deliberately NOT an unlimited
# expression language.
AggregateFunction = Literal["sum", "count", "count_distinct", "min", "max", "avg"]
BinaryOperator = Literal["add", "subtract", "multiply", "divide"]
# Only the comparators actually evidenced by the migrated legacy Gold formulas.
# "==", ">=", "<" and "<>" are deliberately absent: they never appear in an
# expression there (equality lives in the aggregation filter operator).
ComparisonOperator = Literal["gt", "le"]
# NULL predicate used by migrated CASE guards (IS NULL / IS NOT NULL).
NullTestOperator = Literal["is_null", "is_not_null"]

MAX_EXPRESSION_DEPTH: Final[int] = 16
MAX_EXPRESSION_NODES: Final[int] = 64
MAX_CONDITION_BRANCHES: Final[int] = 8

# Authority/lifecycle vocabulary that must never live inside shared semantics.
# CalculationSpec rejects these explicitly (and extra="forbid" is the backstop).
FORBIDDEN_AUTHORITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "canonical",
        "approved",
        "saved",
        "governance_candidate",
        "template_registered",
        "authority",
        "canonical_metric_key",
        "approved_calculation_binding",
        "binding_checksum",
        "definition_id",
        "definition_version",
        "confirmation",
        "confirmed",
        "retention",
        "governance",
        "lifecycle",
    }
)

_FORMAL_PROVENANCE: Final[frozenset[str]] = frozenset(
    {"published_gold", "definition_backed_computed", "product_published_state"}
)


class _StrictFrozenModel(BaseModel):
    """Unknown fields are rejected; instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


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


# --- expression operands ------------------------------------------------------


class LiteralOperand(_StrictFrozenModel):
    """A finite numeric literal."""

    op: Literal["literal"] = "literal"
    value: Decimal

    @field_validator("value")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("literal value must be finite")
        return value


class NullLiteralOperand(_StrictFrozenModel):
    """An explicitly AUTHORED NULL result (legacy "THEN NULL" / "ELSE NULL").

    It is NOT numeric zero, NOT NO_DATA, NOT a missing input and NOT a failure.
    It carries no payload: NULL is a value-state, never a number.
    """

    op: Literal["null"] = "null"


class InputRefOperand(_StrictFrozenModel):
    """A reference to one declared input role."""

    op: Literal["input"] = "input"
    role: RoleName


class ParameterRefOperand(_StrictFrozenModel):
    """A reference to one declared parameter."""

    op: Literal["parameter"] = "parameter"
    name: ParameterName


class AggregateOperand(_StrictFrozenModel):
    """A bounded aggregate over one declared input role."""

    op: Literal["aggregate"] = "aggregate"
    function: AggregateFunction
    role: RoleName


class CompareOperand(_StrictFrozenModel):
    """A bounded comparison over one expression.  Three-valued: NULL is not true."""

    op: Literal["compare"] = "compare"
    operator: ComparisonOperator
    left: Expression
    right: Expression


class NullTestOperand(_StrictFrozenModel):
    """IS NULL / IS NOT NULL over one expression."""

    op: Literal["null_test"] = "null_test"
    operator: NullTestOperator
    operand: Expression


class CoalesceOperand(_StrictFrozenModel):
    """Explicit NULL substitution.  The default MUST be an author-stated literal."""

    op: Literal["coalesce"] = "coalesce"
    operand: Expression
    default: LiteralOperand


class RoundOperand(_StrictFrozenModel):
    """Explicit ROUND over one expression.  Never applied implicitly."""

    op: Literal["round"] = "round"
    operand: Expression
    digits: Precision


class AllOperand(_StrictFrozenModel):
    """Conjunction of bounded comparisons.  No disjunction/negation node."""

    op: Literal["all"] = "all"
    conditions: tuple[CompareOperand | NullTestOperand, ...] = Field(min_length=1, max_length=MAX_CONDITION_BRANCHES)


class AnyOperand(_StrictFrozenModel):
    """Disjunction of bounded conditions.  No negation and no generic boolean
    language: exactly the OR the migrated legacy guards actually use."""

    op: Literal["any"] = "any"
    conditions: tuple[CompareOperand | NullTestOperand, ...] = Field(min_length=1, max_length=MAX_CONDITION_BRANCHES)


class WhenBranch(_StrictFrozenModel):
    """One CASE branch: a three-valued condition and its result."""

    condition: CompareOperand | NullTestOperand | AllOperand | AnyOperand
    then: Expression


class CaseOperand(_StrictFrozenModel):
    """Bounded CASE.  An absent else branch yields NULL (matching legacy)."""

    op: Literal["case"] = "case"
    whens: tuple[WhenBranch, ...] = Field(min_length=1, max_length=MAX_CONDITION_BRANCHES)
    otherwise: Expression | None = None


class BinaryOperand(_StrictFrozenModel):
    """A bounded binary arithmetic node."""

    op: BinaryOperator
    left: Expression
    right: Expression


Expression = Annotated[
    LiteralOperand
    | NullLiteralOperand
    | InputRefOperand
    | ParameterRefOperand
    | AggregateOperand
    | BinaryOperand
    | CompareOperand
    | NullTestOperand
    | CoalesceOperand
    | RoundOperand
    | AllOperand
    | AnyOperand
    | CaseOperand,
    Field(discriminator="op"),
]

BinaryOperand.model_rebuild()
CompareOperand.model_rebuild()
NullTestOperand.model_rebuild()
CoalesceOperand.model_rebuild()
RoundOperand.model_rebuild()
AllOperand.model_rebuild()
AnyOperand.model_rebuild()
WhenBranch.model_rebuild()
CaseOperand.model_rebuild()


def _children(expression: Expression) -> tuple[Expression, ...]:
    """Every direct child node, for every node kind in the bounded grammar."""

    if isinstance(expression, BinaryOperand):
        return (expression.left, expression.right)
    if isinstance(expression, CompareOperand):
        return (expression.left, expression.right)
    if isinstance(expression, NullTestOperand):
        return (expression.operand,)
    if isinstance(expression, CoalesceOperand):
        return (expression.operand,)
    if isinstance(expression, RoundOperand):
        return (expression.operand,)
    if isinstance(expression, (AllOperand, AnyOperand)):
        return tuple(expression.conditions)
    if isinstance(expression, CaseOperand):
        return (
            *(branch.condition for branch in expression.whens),
            *(branch.then for branch in expression.whens),
            *((expression.otherwise,) if expression.otherwise is not None else ()),
        )
    return ()


def _walk(expression: Expression) -> list[Expression]:
    """Depth-first pre-order walk, ITERATIVE so it cannot blow the stack.

    The walk is bounded by MAX_EXPRESSION_NODES: an oversized tree is rejected
    while walking instead of after an unbounded traversal.
    """

    seen: list[Expression] = []
    stack: list[Expression] = [expression]
    while stack:
        node = stack.pop()
        seen.append(node)
        if len(seen) > MAX_EXPRESSION_NODES:
            raise ValueError("calculation expression exceeds the bounded node count")
        stack.extend(reversed(_children(node)))
    return seen


def expression_depth(expression: Expression) -> int:
    """Iterative depth (longest path), so deep chains cannot recurse."""

    best = 0
    stack: list[tuple[Expression, int]] = [(expression, 1)]
    visited = 0
    while stack:
        node, depth = stack.pop()
        visited += 1
        if visited > MAX_EXPRESSION_NODES:
            raise ValueError("calculation expression exceeds the bounded node count")
        best = max(best, depth)
        stack.extend((child, depth + 1) for child in _children(node))
    return best


def expression_node_count(expression: Expression) -> int:
    """Expanded node count, bounded and iterative (no recursion, no blow-up)."""

    return len(_walk(expression))


def referenced_input_roles(expression: Expression) -> tuple[str, ...]:
    roles = [
        node.role
        for node in _walk(expression)
        if isinstance(node, (InputRefOperand, AggregateOperand))
    ]
    return tuple(dict.fromkeys(roles))


def referenced_parameter_names(expression: Expression) -> tuple[str, ...]:
    names = [
        node.name for node in _walk(expression) if isinstance(node, ParameterRefOperand)
    ]
    return tuple(dict.fromkeys(names))


# --- declarations (reusable semantics) ----------------------------------------


class CalculationInputSpec(_StrictFrozenModel):
    """One declared input role and the provenance/identity it must resolve to.

    Implemented now as a STABLE semantic requirement.  Concrete runtime dataset/
    receipt input binding for the role is DEFERRED and is not represented by this
    slice: no dataset/receipt reference type exists here.
    """

    role: RoleName
    provenance: InputProvenance
    metric_key: str | None = Field(default=None, min_length=1, max_length=256)
    required: bool = True

    @model_validator(mode="after")
    def _formal_provenance_names_an_identity(self) -> CalculationInputSpec:
        if self.provenance in _FORMAL_PROVENANCE and self.metric_key is None:
            raise ValueError("formal provenance requires an input metric identity")
        return self


class ParameterSpec(_StrictFrozenModel):
    """A declared parameter of the Parameter Contract (declaration, not value)."""

    name: ParameterName
    value_type: ParameterValueType
    required: bool = True
    allowed_values: tuple[str, ...] = Field(default=(), max_length=64)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("allowed_values")
    @classmethod
    def _allowed_values_unique_non_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("allowed parameter values must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError("allowed parameter values must be unique")
        return value


class CalculationSpec(_StrictFrozenModel):
    """Strict, immutable, reusable calculation semantics.

    Carries NO authority: authority/lifecycle fields are rejected outright.
    Canonical authority, definition identity and lifecycle live outside.
    """

    schema_version: Literal["1.1"] = SCHEMA_VERSION
    calculation_id: CalculationId
    expression: Expression
    inputs: tuple[CalculationInputSpec, ...] = Field(min_length=1, max_length=32)
    parameters: tuple[ParameterSpec, ...] = Field(default=(), max_length=32)
    unit: UnitPolicy
    # Present EXACTLY when unit == "custom_unit"; a named, bounded custom-unit
    # identity that participates in the spec checksum.
    custom_unit_id: CustomUnitId | None = None
    precision: Precision | None = None
    rounding: RoundingPolicy | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_authority_and_lifecycle_fields(cls, data: object) -> object:
        if isinstance(data, Mapping):
            injected = sorted(FORBIDDEN_AUTHORITY_FIELDS.intersection(data))
            if injected:
                raise ValueError(
                    "shared calculation semantics must not carry authority/lifecycle "
                    "fields: " + ", ".join(injected)
                )
        return data

    @field_validator("inputs")
    @classmethod
    def _unique_input_roles(
        cls, value: tuple[CalculationInputSpec, ...]
    ) -> tuple[CalculationInputSpec, ...]:
        roles = [item.role for item in value]
        if len(set(roles)) != len(roles):
            raise ValueError("calculation input roles must be unique")
        return value

    @field_validator("parameters")
    @classmethod
    def _unique_parameter_names(
        cls, value: tuple[ParameterSpec, ...]
    ) -> tuple[ParameterSpec, ...]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("calculation parameter names must be unique")
        return value

    @model_validator(mode="after")
    def _validate_semantics(self) -> CalculationSpec:
        if expression_depth(self.expression) > MAX_EXPRESSION_DEPTH:
            raise ValueError("calculation expression exceeds the bounded depth")
        if expression_node_count(self.expression) > MAX_EXPRESSION_NODES:
            raise ValueError("calculation expression exceeds the bounded node count")
        declared_roles = {item.role for item in self.inputs}
        if not set(referenced_input_roles(self.expression)) <= declared_roles:
            raise ValueError("expression references an undeclared input role")
        declared_parameters = {item.name for item in self.parameters}
        if not set(referenced_parameter_names(self.expression)) <= declared_parameters:
            raise ValueError("expression references an undeclared parameter")
        if self.rounding is not None and self.precision is None:
            raise ValueError("rounding requires an explicit precision")
        if (self.unit == "custom_unit") != (self.custom_unit_id is not None):
            raise ValueError("custom_unit requires exactly one bounded custom_unit_id")
        if (
            self.custom_unit_id is not None
            and self.custom_unit_id in BUILTIN_UNIT_LITERALS
        ):
            raise ValueError("custom_unit_id must not shadow a built-in unit literal")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


# --- per-run binding (never part of the spec identity) ------------------------


ParameterValue = str | bool | int | float | Decimal

_ISO_CALENDAR_DATE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _is_iso_calendar_date(value: str) -> bool:
    if not _ISO_CALENDAR_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def value_matches_type(value: ParameterValue, value_type: ParameterValueType) -> bool:
    """Strict type contract for one per-run parameter value.

    ``bool`` never satisfies ``integer`` (Python bool is an int subclass) and a
    float never satisfies ``decimal`` (inexact).  ``decimal`` accepts an exact
    Decimal or an exact int.  ``date`` is a strict ISO calendar-date string.
    """

    if value_type == "string":
        return isinstance(value, str)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "decimal":
        return isinstance(value, Decimal) or (
            isinstance(value, int) and not isinstance(value, bool)
        )
    if value_type == "date":
        return isinstance(value, str) and _is_iso_calendar_date(value)
    return False


class ParameterBinding(_StrictFrozenModel):
    """One concrete per-run parameter value.  Not part of the spec identity."""

    name: ParameterName
    value: ParameterValue

    @field_validator("value")
    @classmethod
    def _finite(cls, value: ParameterValue) -> ParameterValue:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("parameter value must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("parameter value must be finite")
        return value


class CalculationExecutionBinding(_StrictFrozenModel):
    """Per-run binding of concrete PARAMETER values to one CalculationSpec.

    It currently carries parameter values only; it does NOT bind input datasets
    or receipts (runtime input binding is deferred).  Structurally SEPARATE from
    the reusable spec: a contract-valid value change produces a NEW binding,
    never a new spec identity/checksum.
    """

    schema_version: Literal["1.1"] = SCHEMA_VERSION
    calculation_id: CalculationId
    spec_checksum: Checksum
    parameters: tuple[ParameterBinding, ...] = Field(default=(), max_length=32)

    @field_validator("parameters")
    @classmethod
    def _unique_parameter_names(
        cls, value: tuple[ParameterBinding, ...]
    ) -> tuple[ParameterBinding, ...]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("parameter bindings must be unique by name")
        return value

    def binding_failures(self, spec: CalculationSpec) -> tuple[str, ...]:
        """Contract-validity of this binding against one reusable spec."""

        failures: list[str] = []
        if spec.calculation_id != self.calculation_id or spec.checksum != self.spec_checksum:
            failures.append("execution_binding_spec_mismatch")
        declared = {item.name: item for item in spec.parameters}
        bound = {item.name: item for item in self.parameters}
        for name in bound:
            if name not in declared:
                failures.append("execution_binding_unknown_parameter")
        for name, parameter in declared.items():
            binding = bound.get(name)
            if binding is None:
                if parameter.required:
                    failures.append("execution_binding_missing_required_parameter")
                continue
            if not value_matches_type(binding.value, parameter.value_type):
                failures.append("execution_binding_value_type_mismatch")
            elif (
                parameter.allowed_values
                and str(binding.value) not in parameter.allowed_values
            ):
                failures.append("execution_binding_value_not_allowed")
        return tuple(dict.fromkeys(failures))

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


# --- noncanonical derived-output identity ------------------------------------


def derived_output_id(
    spec: CalculationSpec, binding: CalculationExecutionBinding
) -> str:
    """Deterministic NONCANONICAL derived-output identity.

    The id identifies CalculationSpec + CalculationExecutionBinding CONTENT
    only.  It is NOT a canonical identity, a metric identity, a SAVED identity,
    a global execution/result id, or a datasource/time/filter/run identity.
    Two executions with the same spec + parameter binding MAY share the id even
    when their runtime data differs; AnswerFact.fact_id is the concrete
    grounded-fact identity.  Equal ids mean equal content ONLY: never canonical,
    never SAVED, never a reusable definition, and never a metric_key.
    """

    return "adhoc_" + _checksum(
        {"spec_checksum": spec.checksum, "binding_checksum": binding.checksum}
    )[:32]


# --- semantic resolution vocabulary (additive) --------------------------------


SemanticResolutionOutcome = Literal[
    "resolved",
    "clarification_required",
    "no_authoritative_definition",
]


class SemanticResolution(_StrictFrozenModel):
    """Minimum shared vocabulary for the frozen resolution outcomes.

    ADDITIVE: this does not replace ContextBundle.resolution_status and does not
    invent business meaning.  It only names the three frozen outcomes:

    * resolved                     -- exactly one authoritative valid meaning
    * clarification_required       -- two or more materially different meanings
    * no_authoritative_definition  -- no authoritative meaning; do not invent
    """

    outcome: SemanticResolutionOutcome
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("unresolved_slots")
    @classmethod
    def _slots_unique_non_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("resolution slots must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError("resolution slots must be unique")
        return value

    @model_validator(mode="after")
    def _resolved_has_no_open_slots(self) -> SemanticResolution:
        if self.outcome == "resolved" and self.unresolved_slots:
            raise ValueError("resolved outcome must not carry unresolved slots")
        return self


__all__ = [
    "AggregateFunction",
    "AllOperand",
    "AnyOperand",
    "CaseOperand",
    "CoalesceOperand",
    "CompareOperand",
    "ComparisonOperator",
    "MAX_CONDITION_BRANCHES",
    "NullTestOperand",
    "NullTestOperator",
    "AnyOperand",
    "RoundOperand",
    "WhenBranch",
    "AggregateOperand",
    "BinaryOperand",
    "BUILTIN_UNIT_LITERALS",
    "BinaryOperator",
    "CalculationExecutionBinding",
    "CalculationId",
    "CalculationInputSpec",
    "CalculationSpec",
    "Checksum",
    "CustomUnitId",
    "FORBIDDEN_AUTHORITY_FIELDS",
    "InputProvenance",
    "InputRefOperand",
    "LiteralOperand",
    "MAX_EXPRESSION_DEPTH",
    "MAX_EXPRESSION_NODES",
    "NullLiteralOperand",
    "ParameterBinding",
    "ParameterName",
    "ParameterRefOperand",
    "ParameterSpec",
    "ParameterValue",
    "ParameterValueType",
    "Precision",
    "RoleName",
    "RoundingPolicy",
    "SCHEMA_VERSION",
    "SemanticResolution",
    "SemanticResolutionOutcome",
    "UnitPolicy",
    "derived_output_id",
    "expression_depth",
    "expression_node_count",
    "referenced_input_roles",
    "referenced_parameter_names",
    "value_matches_type",
]
