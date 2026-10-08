"""Server-side execution seam for reusable Custom Definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.nl2sql.artifacts.custom_definition import (
    DefinitionExecutionBinding,
    DefinitionVersion,
)
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.orchestration.custom_calculation_execution import (
    CustomCalculationExecutionResult,
    ResolvedCalculationInput,
    execute_custom_calculation,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
)
from src.nl2sql.semantic.calculation_contract import CalculationExecutionBinding

__all__ = [
    "CalculationInputResolver",
    "CalculationInputResolverUnavailable",
    "CustomDefinitionExecutionOutcome",
    "CustomDefinitionExecutionService",
    "DefinitionExecutionContext",
]


class CalculationInputResolver(Protocol):
    """Resolve governed values from server execution evidence, never HTTP."""

    async def resolve_inputs(
        self,
        *,
        owner_user_id: str,
        definition: DefinitionVersion,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext | None = None,
    ) -> tuple[ResolvedCalculationInput, ...]: ...


class CalculationInputResolverUnavailable(RuntimeError):
    code = "calculation_input_resolver_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CustomDefinitionExecutionOutcome:
    definition_id: str
    version: int
    definition_checksum: str
    result: CustomCalculationExecutionResult


class CustomDefinitionExecutionService:
    """Validate exact semantics/binding, resolve governed inputs, then execute."""

    def __init__(
        self,
        *,
        definitions: CustomDefinitionService,
        input_resolver: CalculationInputResolver | None,
    ) -> None:
        self._definitions = definitions
        self._input_resolver = input_resolver

    async def execute(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext | None = None,
    ) -> CustomDefinitionExecutionOutcome:
        exact = self._definitions.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )
        definition_binding = DefinitionExecutionBinding(
            definition_id=definition_id,
            version=version,
            definition_checksum=exact.checksum,
            binding=binding,
        )
        exact = self._definitions.execute_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
            binding=definition_binding,
        )
        if self._input_resolver is None:
            raise CalculationInputResolverUnavailable()
        resolved_inputs = await self._input_resolver.resolve_inputs(
            owner_user_id=owner_user_id,
            definition=exact,
            binding=binding,
            execution_context=execution_context or DefinitionExecutionContext(),
        )
        result = execute_custom_calculation(
            spec=exact.calculation,
            binding=binding,
            inputs=resolved_inputs,
        )
        return CustomDefinitionExecutionOutcome(
            definition_id=exact.definition_id,
            version=exact.version,
            definition_checksum=exact.checksum,
            result=result,
        )
