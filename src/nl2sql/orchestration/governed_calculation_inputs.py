"""Generic governed metric-input adapter for reusable calculations.

The fetcher is the only future live-data seam.  It returns the already accepted
``ResolvedCalculationInput`` scalar carrier: no raw rows, SQL, authorization
payload or credential shape is introduced here.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from pydantic import model_validator

from src.nl2sql.artifacts.custom_definition import DefinitionVersion
from src.nl2sql.contracts import StrictContract
from src.nl2sql.orchestration.custom_calculation_execution import (
    CustomCalculationExecutionError,
    ResolvedCalculationInput,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    InputProvenance,
)

__all__ = [
    "DefinitionExecutionContext",
    "GovernedMetricInputFetcher",
    "GovernedMetricInputResolutionError",
    "TypedMetricCalculationInputResolver",
]


class DefinitionExecutionContext(StrictContract):
    """The only client-selectable business-date authority for execution."""

    date_mode: Literal["latest_authoritative", "exact_date"] = (
        "latest_authoritative"
    )
    exact_date: date | None = None

    @model_validator(mode="after")
    def validate_date_selection(self) -> "DefinitionExecutionContext":
        if self.date_mode == "latest_authoritative" and self.exact_date is not None:
            raise ValueError("exact_date is only valid with date_mode=exact_date")
        if self.date_mode == "exact_date" and self.exact_date is None:
            raise ValueError("exact_date is required with date_mode=exact_date")
        return self


class GovernedMetricInputFetcher(Protocol):
    """Future live adapter: fetch one governed scalar with safe evidence."""

    async def fetch_metric_input(
        self,
        *,
        owner_user_id: str,
        role: str,
        metric_key: str,
        required_provenance: InputProvenance,
        execution_context: DefinitionExecutionContext,
    ) -> ResolvedCalculationInput: ...


class GovernedMetricInputResolutionError(CustomCalculationExecutionError):
    """Stable fail-closed error at the generic governed-input boundary."""


class TypedMetricCalculationInputResolver:
    """Resolve every declared role through one injected governed fetcher."""

    def __init__(
        self,
        fetcher: GovernedMetricInputFetcher,
        *,
        require_data_as_of: bool = True,
    ) -> None:
        self._fetcher = fetcher
        self._require_data_as_of = require_data_as_of

    async def resolve_inputs(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext | None = None,
    ) -> tuple[ResolvedCalculationInput, ...]:
        # Parameter values never influence governed business-input selection.
        # Their only consumer is the shared Calculation Runtime after binding
        # validation in CustomDefinitionExecutionService.
        del binding
        context = execution_context or DefinitionExecutionContext()
        declared = tuple(definition.calculation.inputs)
        declared_roles = tuple(item.role for item in declared)
        if not declared or len(set(declared_roles)) != len(declared_roles):
            raise GovernedMetricInputResolutionError(
                "calculation_input_role_contract_invalid"
            )

        resolved: list[ResolvedCalculationInput] = []
        for requirement in declared:
            if requirement.metric_key is None:
                raise GovernedMetricInputResolutionError(
                    "calculation_input_metric_unbound"
                )
            candidate = await self._fetcher.fetch_metric_input(
                owner_user_id=owner_user_id,
                role=requirement.role,
                metric_key=requirement.metric_key,
                required_provenance=requirement.provenance,
                execution_context=context,
            )
            if not isinstance(candidate, ResolvedCalculationInput):
                raise GovernedMetricInputResolutionError(
                    "calculation_input_evidence_invalid"
                )
            if candidate.role != requirement.role:
                raise GovernedMetricInputResolutionError(
                    "calculation_input_role_mismatch"
                )
            if candidate.metric_key != requirement.metric_key:
                raise GovernedMetricInputResolutionError(
                    "calculation_input_metric_mismatch"
                )
            if candidate.provenance != requirement.provenance:
                raise GovernedMetricInputResolutionError(
                    "calculation_input_provenance_mismatch"
                )
            if candidate.status != "resolved":
                raise GovernedMetricInputResolutionError(
                    "calculation_input_unavailable"
                )
            if self._require_data_as_of and candidate.data_as_of is None:
                raise GovernedMetricInputResolutionError(
                    "calculation_input_data_as_of_missing"
                )
            if (
                candidate.source_id is None
                or candidate.receipt_step_id is None
                or candidate.fact_id is None
            ):
                raise GovernedMetricInputResolutionError(
                    "calculation_input_provenance_incomplete"
                )
            resolved.append(candidate)

        roles = tuple(item.role for item in resolved)
        if roles != declared_roles or len(set(roles)) != len(roles):
            raise GovernedMetricInputResolutionError(
                "calculation_input_role_resolution_incomplete"
            )
        return tuple(resolved)
