"""Server-validated REQUEST ENTRY for a QUERY run-scoped AD_HOC calculation.

This layer reuses the frozen shared typed calculation semantic core
(``CalculationSpec`` + ``CalculationExecutionBinding``, A9) and adds NO second
formula representation.  It grants no authority: the carrier is a NONCANONICAL,
run-scoped derivation request, and the server either resolves it onto metrics
already authorized in this run's ``ContextBundle`` or fails closed with a stable
code.  There is deliberately no canonical / approved / saved / definition field.

A8 boundary (semantic resolution vs invention)
----------------------------------------------
* an explicit formula/relation with UNIQUELY resolved inputs may be executed
  under this run's capability, authorization and computation gate;
* two or more materially different meanings require the MINIMUM clarification
  and are never auto-selected or executed here;
* no authoritative meaning and no user-provided formula means NO invention.

No natural-language -> formula parser lives here.  The caller supplies the
explicit typed carrier; the server only validates and resolves it.

A4 boundary
-----------
An AD_HOC request NEVER enters the Custom Definition lifecycle: this module has
no definition/repository/SAVED reference and performs no persistence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ConfigDict, model_validator

from src.nl2sql.contracts import ContextBundle, QueryPlan, StrictContract
from src.nl2sql.orchestration.approved_compute import ApprovedCalculationCatalog
from src.nl2sql.semantic.calculation_contract import (
    FORBIDDEN_AUTHORITY_FIELDS,
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationSpec,
    Expression,
    _children,
    derived_output_id,
    referenced_input_roles,
)

SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

# The ratio-like units whose denominator must be DATA-DERIVED.  A denominator
# that is only a constant is not a denominator: it is a missing one.
_RATIO_UNITS: Final[frozenset[str]] = frozenset({"ratio", "percent"})


class AdHocRequestError(RuntimeError):
    """A fail-closed request-entry rejection carrying a stable code."""

    def __init__(self, code: str) -> None:
        normalized = code.strip()
        if not normalized:
            raise ValueError("ad hoc request error code must be non-empty")
        super().__init__(normalized)
        self.code = normalized


class AdHocCalculationRequest(StrictContract):
    """Strict, authority-free carrier for one run-scoped AD_HOC calculation.

    It carries ONLY the shared typed semantics plus the per-run parameter
    binding.  Authority and lifecycle live outside the shared semantic core, so
    a carrier can never self-declare canonicality; the strict nested
    ``CalculationSpec`` and the ``extra="forbid"`` carrier both reject injected
    authority/lifecycle fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    calculation_spec: CalculationSpec
    execution_binding: CalculationExecutionBinding

    @model_validator(mode="before")
    @classmethod
    def _reject_authority_and_lifecycle_fields(cls, data: object) -> object:
        if isinstance(data, Mapping):
            injected = sorted(FORBIDDEN_AUTHORITY_FIELDS.intersection(data))
            if injected:
                raise ValueError(
                    "an ad hoc request must not carry authority/lifecycle "
                    "fields: " + ", ".join(injected)
                )
        return data


@dataclass(frozen=True, slots=True)
class ResolvedAdHocCalculation:
    """One server-validated carrier resolved onto authorized context metrics."""

    spec: CalculationSpec
    binding: CalculationExecutionBinding
    resolved_inputs: tuple[tuple[str, str], ...]
    derived_output_id: str

    @property
    def input_metrics(self) -> tuple[str, ...]:
        return tuple(metric_key for _, metric_key in self.resolved_inputs)


def _expression_nodes(expression: Expression) -> tuple[Expression, ...]:
    """Bounded iterative pre-order walk of the frozen expression grammar.

    It reuses the frozen core's OWN child relation so the traversal can never
    diverge from the shared grammar.
    """

    seen: list[Expression] = []
    stack: list[Expression] = [expression]
    while stack:
        node = stack.pop()
        seen.append(node)
        stack.extend(reversed(_children(node)))
    return tuple(seen)


def _denominator_semantics_missing(spec: CalculationSpec) -> bool:
    """True when a ratio-like calculation has no DATA-DERIVED denominator.

    An explicit ``divide`` whose right subtree references no declared input role
    divides by a constant, not by a business denominator.  A ratio/percent unit
    with no ``divide`` at all needs at least two declared inputs to have a
    denominator to speak of.  This is a completeness check only: it never
    invents a denominator and never rewrites the expression.
    """

    divides = [
        node
        for node in _expression_nodes(spec.expression)
        if isinstance(node, BinaryOperand) and node.op == "divide"
    ]
    if not divides:
        return spec.unit in _RATIO_UNITS and len(spec.inputs) < 2
    return any(not referenced_input_roles(node.right) for node in divides)


def resolve_ad_hoc_request(
    *,
    request: AdHocCalculationRequest,
    context: ContextBundle,
    query_plan: QueryPlan,
    catalog: ApprovedCalculationCatalog | None = None,
) -> ResolvedAdHocCalculation:
    """Validate one explicit carrier and resolve it onto authorized context metrics.

    Every failure is fail-closed with a stable code.  Nothing here executes SQL,
    opens a gateway, persists anything or grants canonical authority.
    """

    spec = request.calculation_spec
    binding = request.execution_binding

    if binding.calculation_id != spec.calculation_id:
        raise AdHocRequestError("ad_hoc_request_binding_identity_mismatch")
    if binding.spec_checksum != spec.checksum:
        raise AdHocRequestError("ad_hoc_request_binding_spec_mismatch")
    if binding.binding_failures(spec):
        raise AdHocRequestError("ad_hoc_request_binding_invalid")

    # A8: a declared input role that the expression never consumes is a role
    # mismatch, not a silently ignored input.
    declared_roles = {item.role for item in spec.inputs}
    if declared_roles != set(referenced_input_roles(spec.expression)):
        raise AdHocRequestError("ad_hoc_request_input_role_mismatch")

    # A8: the run's semantic resolution must be a single authoritative meaning.
    # A conflict or two-or-more materially different meanings require the minimum
    # clarification; the server never auto-selects a winner here.
    if context.conflict_ids or context.resolution_status == "conflict":
        raise AdHocRequestError("ad_hoc_request_input_conflict")
    if context.resolution_status == "ambiguous":
        raise AdHocRequestError("ad_hoc_request_input_ambiguous")

    unresolved = set(query_plan.unresolved_slots) | set(context.unresolved_slots)
    if "time" in unresolved:
        raise AdHocRequestError("ad_hoc_request_time_semantics_missing")
    if context.resolution_status == "incomplete":
        raise AdHocRequestError("ad_hoc_request_input_incomplete")
    if unresolved:
        raise AdHocRequestError("ad_hoc_request_unresolved_slots")

    # AD_HOC V1 combines independent aggregate SCALARS.  It has NO plan-level
    # join/grouping semantics (that is a BUILD capability), so a non-metric or
    # dimensioned plan cannot be served by this carrier.
    if query_plan.intent != "metric" or query_plan.dimensions:
        raise AdHocRequestError("ad_hoc_request_join_semantics_missing")

    if _denominator_semantics_missing(spec):
        raise AdHocRequestError("ad_hoc_request_denominator_semantics_missing")

    resolved: list[tuple[str, str]] = []
    authorized = set(context.asset_ids)
    for item in spec.inputs:
        if item.provenance == "ad_hoc_metric":
            # V1 has no nested AD_HOC dependency: an AD_HOC output is never an
            # input to another AD_HOC calculation.
            raise AdHocRequestError("ad_hoc_request_nested_input_unsupported")
        if item.metric_key is None:
            # No resolved input identity: never invented.
            raise AdHocRequestError("ad_hoc_request_input_unresolved")
        if item.metric_key not in authorized:
            # No AUTHORITATIVE meaning in this run's authorized context: the
            # server does not invent one and does not widen the context.
            raise AdHocRequestError("ad_hoc_request_input_unresolved")
        resolved.append((item.role, item.metric_key))

    metrics = [metric_key for _, metric_key in resolved]
    if len(set(metrics)) != len(metrics):
        raise AdHocRequestError("ad_hoc_request_input_duplicate")
    if set(metrics) != set(query_plan.metric_keys):
        raise AdHocRequestError("ad_hoc_request_source_plan_mismatch")
    if catalog is not None and any(
        catalog.binding_for(metric_key) is not None for metric_key in metrics
    ):
        # A catalog-bound input already HAS a canonical authority.  Recomputing
        # it as AD_HOC would create a second, noncanonical authority for the
        # same business value, so it is refused outright.
        raise AdHocRequestError("ad_hoc_request_input_catalog_bound")

    return ResolvedAdHocCalculation(
        spec=spec,
        binding=binding,
        resolved_inputs=tuple(resolved),
        derived_output_id=derived_output_id(spec, binding),
    )


__all__ = [
    "AdHocCalculationRequest",
    "AdHocRequestError",
    "ResolvedAdHocCalculation",
    "SCHEMA_VERSION",
    "resolve_ad_hoc_request",
]
