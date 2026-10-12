"""Execution readiness of a DECLARED semantic surface (§8.16 P7B).

A semantic-bearing version must explicitly mark each execution-critical axis
REQUIRED or NOT_APPLICABLE.  REQUIRED needs the matching declared semantics;
NOT_APPLICABLE is valid only when no matching semantic value is declared.  An
unspecified axis fails closed.  A semantics-free version retains its legacy
identity and executable behavior.

The refusal subclasses the shared stable-refusal error, so the EXISTING HTTP
execution route surfaces the stable code (409 + code) without being modified.
"""

from __future__ import annotations

from typing import Final

from src.nl2sql.artifacts.custom_definition import DefinitionVersion
from src.nl2sql.artifacts.definition_semantics import ExecutionApplicabilityValue
from src.nl2sql.orchestration.custom_calculation_execution import (
    CustomCalculationExecutionError,
)

# The execution-critical axes, in the frozen AXIS_ORDER subsequence.
EXECUTION_CRITICAL_SEMANTIC_AXES: Final[tuple[str, ...]] = (
    "denominator",
    "time_semantics",
    "join_semantics",
)

DEFINITION_SEMANTICS_INCOMPLETE_FOR_EXECUTION: Final[str] = (
    "definition_semantics_incomplete_for_execution"
)


class DefinitionSemanticsIncompleteForExecution(CustomCalculationExecutionError):
    """Typed refusal: a DECLARED semantic surface is missing execution semantics.

    Subclasses the shared ``CustomCalculationExecutionError`` so the HTTP execution
    route reports the SAME stable code as every other pre-runtime refusal, with
    no route change.
    """

    code = DEFINITION_SEMANTICS_INCOMPLETE_FOR_EXECUTION

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(DEFINITION_SEMANTICS_INCOMPLETE_FOR_EXECUTION)


def semantic_execution_gaps(version: DefinitionVersion) -> tuple[str, ...]:
    """Return missing or contradictory applicability declarations.

    A semantics-free version is the legacy identity and reports NO gap (A6
    compatibility).  A semantic-bearing version must explicitly define all
    three applicability axes; the gap is returned in the frozen axis order.
    """

    semantics = version.semantics
    if semantics is None:
        return ()
    applicability = semantics.execution_applicability
    gaps: list[str] = []
    for axis in EXECUTION_CRITICAL_SEMANTIC_AXES:
        decision: ExecutionApplicabilityValue | None = (
            getattr(applicability, axis) if applicability is not None else None
        )
        declared_semantics = getattr(semantics, axis)
        if decision is None:
            gaps.append(f"{axis}_applicability_unspecified")
        elif decision == "REQUIRED" and declared_semantics is None:
            gaps.append(f"{axis}_required_value_missing")
        elif decision == "NOT_APPLICABLE" and declared_semantics is not None:
            gaps.append(f"{axis}_not_applicable_conflicts_with_value")
    return tuple(gaps)


def require_executable_semantics(version: DefinitionVersion) -> None:
    """Refuse to execute a DECLARED semantic surface that is incomplete."""

    gaps = semantic_execution_gaps(version)
    if gaps:
        raise DefinitionSemanticsIncompleteForExecution(gaps)


__all__ = [
    "DEFINITION_SEMANTICS_INCOMPLETE_FOR_EXECUTION",
    "EXECUTION_CRITICAL_SEMANTIC_AXES",
    "DefinitionSemanticsIncompleteForExecution",
    "require_executable_semantics",
    "semantic_execution_gaps",
]
