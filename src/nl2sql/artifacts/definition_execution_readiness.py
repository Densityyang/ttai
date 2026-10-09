"""Execution readiness of a DECLARED semantic surface (§8.16 P7B).

Frozen requirement: a definition that lacks **denominator / business-time /
join** semantics is NOT executable.  The A6 contract makes every semantic axis
optional BY CONSTRUCTION -- a semantics-free version keeps its exact legacy
identity and stays executable -- so this gate deliberately applies to a version
that DECLARES semantics: once a definition opts in to declaring its business
meaning, the execution-critical axes must be present.

The three required axes are exactly the ones the frozen requirement names:
``denominator``, ``time_semantics`` and ``join_semantics``.  This is the chosen
expression of the requirement.  CONTRACT GAP: the contract carries no separate
"execution complete" flag and no "is a ratio" flag, so a version that declares
any semantics at all is held to all three axes; adding a ratio/execution flag
would be a substantive schema change owned by another slice.

The refusal subclasses the shared stable-refusal error, so the EXISTING HTTP
execution route surfaces the stable code (409 + code) without being modified.
"""

from __future__ import annotations

from typing import Final

from src.nl2sql.artifacts.custom_definition import DefinitionVersion
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
    """The execution-critical axes a version declares but does not define.

    A semantics-free version is the legacy identity and reports NO gap (A6
    compatibility).  A version that declares semantics must define all three
    execution-critical axes; the gap is returned in the frozen axis order.
    """

    semantics = version.semantics
    if semantics is None:
        return ()
    gaps: list[str] = []
    if semantics.denominator is None:
        gaps.append("denominator")
    if semantics.time_semantics is None:
        gaps.append("time_semantics")
    if semantics.join_semantics is None:
        gaps.append("join_semantics")
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
