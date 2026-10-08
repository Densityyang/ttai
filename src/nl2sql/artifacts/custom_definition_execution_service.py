"""Server-side execution seam for reusable Custom Definitions.

A SAVED rerun is revalidated against the CURRENT state (A7 / 5.2.2) before any
governed input is fetched, and every rerun records its own binding, plan,
validation and receipt.  A normal parameter rebinding never creates a new
Definition Version (A5) and never mechanically re-asks for a business
confirmation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from src.nl2sql.artifacts.custom_definition import (
    DefinitionExecutionBinding,
    DefinitionVersion,
)
from src.nl2sql.artifacts.definition_revalidation import (
    DefinitionRevalidationGate,
    DefinitionRevalidationResult,
)
from src.nl2sql.artifacts.definition_run_record import (
    DefinitionRunPlan,
    DefinitionRunReceipt,
    DefinitionRunRecord,
    DefinitionRunRecordStore,
    DefinitionRunValidation,
    InMemoryDefinitionRunRecordStore,
    new_run_id,
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
    "CustomDefinitionExecutionRefused",
    "CustomDefinitionExecutionService",
    "DefinitionExecutionContext",
    "DefinitionExecutionRefused",
]


logger = logging.getLogger(__name__)


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
    # Every CURRENT-authority check this rerun SKIPPED because the deployment has
    # no provider for it.  Empty in product mode; never a silent pass elsewhere.
    degradations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CustomDefinitionExecutionRefused:
    """A typed non-EXECUTE revalidation branch.  Never a transport error."""

    definition_id: str
    version: int
    definition_checksum: str
    revalidation: DefinitionRevalidationResult
    run_id: str


class DefinitionExecutionRefused(RuntimeError):
    """Raised by execute() when revalidation refuses to EXECUTE."""

    def __init__(self, refusal: CustomDefinitionExecutionRefused) -> None:
        self.refusal = refusal
        super().__init__(refusal.revalidation.branch)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _merge_degradations(
    before: DefinitionRevalidationResult,
    after: DefinitionRevalidationResult | None,
) -> tuple[str, ...]:
    """The ordered, de-duplicated degradations of both revalidation phases."""

    merged = before.degradations + (() if after is None else after.degradations)
    return tuple(dict.fromkeys(merged))


def _log_degradations(
    *,
    definition_id: str,
    version: int,
    degradations: tuple[str, ...],
) -> None:
    """Warn ONCE per rerun about every CURRENT-authority check that was skipped.

    The message names the skipped checks so an operator can see that this
    deployment has not wired the external authority; it is never emitted in
    product mode, where an unconfigured provider is UNAVAILABLE instead.
    """

    if not degradations:
        return
    logger.warning(
        "definition_revalidation_degraded definition_id=%s version=%s "
        "degradations=%s",
        definition_id,
        version,
        ",".join(degradations),
        extra={
            "event": "definition_revalidation_degraded",
            "definition_id": definition_id,
            "version": version,
            "degradations": list(degradations),
        },
    )


class CustomDefinitionExecutionService:
    """Revalidate current state, resolve governed inputs, then execute.

    The revalidation gate is FAIL-CLOSED.  When no gate is injected, the service
    builds the default gate from the definition service's OWN governed-metric
    authority and leaves every other current-state provider absent; an absent
    provider is UNAVAILABLE, never a silent pass.
    """

    def __init__(
        self,
        *,
        definitions: CustomDefinitionService,
        input_resolver: CalculationInputResolver | None,
        revalidation: DefinitionRevalidationGate | None = None,
        run_records: DefinitionRunRecordStore | None = None,
    ) -> None:
        self._definitions = definitions
        self._input_resolver = input_resolver
        self._revalidation = (
            revalidation
            if revalidation is not None
            else DefinitionRevalidationGate(
                governed_metric_authority=definitions._is_governed_metric_key
            )
        )
        self._run_records: DefinitionRunRecordStore = (
            run_records if run_records is not None else InMemoryDefinitionRunRecordStore()
        )

    @property
    def run_records(self) -> DefinitionRunRecordStore:
        """The queryable per-run record port for this service."""

        return self._run_records

    async def execute(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext | None = None,
    ) -> CustomDefinitionExecutionOutcome:
        """Execute one SAVED rerun or raise the typed refusal."""

        outcome = await self.execute_revalidated(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
            binding=binding,
            execution_context=execution_context,
        )
        if isinstance(outcome, CustomDefinitionExecutionRefused):
            raise DefinitionExecutionRefused(outcome)
        return outcome

    async def execute_revalidated(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        binding: CalculationExecutionBinding,
        execution_context: DefinitionExecutionContext | None = None,
    ) -> CustomDefinitionExecutionOutcome | CustomDefinitionExecutionRefused:
        """The typed EXECUTE-or-refusal entry point used by the HTTP wire."""

        # A deployment without a governed input resolver cannot execute at all;
        # this is a configuration boundary, not a revalidation branch.
        if self._input_resolver is None:
            raise CalculationInputResolverUnavailable()
        context = execution_context or DefinitionExecutionContext()
        exact = await self._definitions.get_exact_version(
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
        exact = await self._definitions.execute_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
            binding=definition_binding,
        )
        lifecycle = await self._definitions.get_version_lifecycle(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )

        # Every gate that must be proven BEFORE any governed fetch.
        before = await self._revalidation.revalidate_before_resolution(
            owner_user_id=owner_user_id,
            definition=exact,
            lifecycle=lifecycle,
            binding=binding,
            execution_context=context,
        )
        if before.branch != "EXECUTE":
            return await self._record_refusal(
                exact=exact,
                definition_binding=definition_binding,
                context=context,
                before=before,
            )

        resolved_inputs = await self._input_resolver.resolve_inputs(
            owner_user_id=owner_user_id,
            definition=exact,
            binding=binding,
            execution_context=context,
        )

        # Freshness/DQ is judged on the ACTUAL evidence of THIS rerun.
        after = await self._revalidation.revalidate_resolved_inputs(
            owner_user_id=owner_user_id,
            definition=exact,
            resolved_inputs=resolved_inputs,
            execution_context=context,
        )
        if after.branch != "EXECUTE":
            return await self._record_refusal(
                exact=exact,
                definition_binding=definition_binding,
                context=context,
                before=before,
                after=after,
            )

        # The degradations of BOTH revalidation phases travel with the outcome,
        # the audit record and an explicit warning; a skipped check is never
        # silent.
        degradations = _merge_degradations(before, after)
        result = execute_custom_calculation(
            spec=exact.calculation,
            binding=binding,
            inputs=resolved_inputs,
        )
        outcome = CustomDefinitionExecutionOutcome(
            definition_id=exact.definition_id,
            version=exact.version,
            definition_checksum=exact.checksum,
            result=result,
            degradations=degradations,
        )
        _log_degradations(
            definition_id=exact.definition_id,
            version=exact.version,
            degradations=degradations,
        )
        await self._run_records.put(
            record=DefinitionRunRecord(
                run_id=new_run_id(),
                definition_id=exact.definition_id,
                version=exact.version,
                definition_checksum=exact.checksum,
                binding=definition_binding,
                plan=self._plan(exact, definition_binding, context),
                validation=self._validation(before, after, binding_failures=()),
                degradations=degradations,
                receipt=DefinitionRunReceipt(result=result),
                created_at=_utcnow(),
            )
        )
        return outcome

    async def _record_refusal(
        self,
        *,
        exact: DefinitionVersion,
        definition_binding: DefinitionExecutionBinding,
        context: DefinitionExecutionContext,
        before: DefinitionRevalidationResult,
        after: DefinitionRevalidationResult | None = None,
    ) -> CustomDefinitionExecutionRefused:
        run_id = new_run_id()
        branch = after if after is not None else before
        degradations = _merge_degradations(before, after)
        _log_degradations(
            definition_id=exact.definition_id,
            version=exact.version,
            degradations=degradations,
        )
        await self._run_records.put(
            record=DefinitionRunRecord(
                run_id=run_id,
                definition_id=exact.definition_id,
                version=exact.version,
                definition_checksum=exact.checksum,
                binding=definition_binding,
                plan=self._plan(exact, definition_binding, context),
                validation=self._validation(before, after, binding_failures=()),
                degradations=degradations,
                receipt=None,
                created_at=_utcnow(),
            )
        )
        return CustomDefinitionExecutionRefused(
            definition_id=exact.definition_id,
            version=exact.version,
            definition_checksum=exact.checksum,
            revalidation=branch,
            run_id=run_id,
        )

    @staticmethod
    def _plan(
        exact: DefinitionVersion,
        definition_binding: DefinitionExecutionBinding,
        context: DefinitionExecutionContext,
    ) -> DefinitionRunPlan:
        return DefinitionRunPlan(
            calculation_id=exact.calculation.calculation_id,
            spec_checksum=exact.calculation.checksum,
            binding_checksum=definition_binding.binding.checksum,
            declared_input_roles=tuple(
                item.role for item in exact.calculation.inputs
            ),
            parameter_names=tuple(
                item.name for item in exact.calculation.parameters
            ),
            execution_context=context,
        )

    @staticmethod
    def _validation(
        before: DefinitionRevalidationResult,
        after: DefinitionRevalidationResult | None,
        *,
        binding_failures: tuple[str, ...],
    ) -> DefinitionRunValidation:
        branch = after if after is not None else before
        return DefinitionRunValidation(
            branch=branch.branch,
            reasons=branch.reasons,
            binding_failures=binding_failures,
            authorization_revision=before.authorization_revision,
            release_id=before.release_id,
            snapshot_id=before.snapshot_id,
            freshness=branch.freshness,
            before_resolution=before,
            resolved_inputs=after,
            validated_at=_utcnow(),
        )
