"""Per-run records for a SAVED Custom Definition rerun (A7, 5.2.2).

Every rerun records its OWN Execution Binding, execution plan, validation and
receipt.  A previous run's result is never presented as the current data result,
and a normal parameter rebinding never creates a new Definition Version (A5).

The store is an explicit PORT: the process-local implementation below is
deliberately non-durable and exists so the durable Control-PostgreSQL backend
can be swapped in behind the SAME interface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import Field

from src.nl2sql.artifacts.custom_definition import DefinitionExecutionBinding
from src.nl2sql.artifacts.definition_revalidation import (
    DefinitionRevalidationResult,
    InputFreshnessDQ,
    RevalidationBranch,
    RevalidationReason,
)
from src.nl2sql.contracts import StrictContract
from src.nl2sql.orchestration.custom_calculation_execution import (
    CustomCalculationExecutionResult,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
)

__all__ = [
    "DefinitionRunPlan",
    "DefinitionRunReceipt",
    "DefinitionRunRecord",
    "DefinitionRunRecordStore",
    "DefinitionRunValidation",
    "InMemoryDefinitionRunRecordStore",
    "new_run_id",
]

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return "run_" + uuid4().hex


class DefinitionRunPlan(StrictContract):
    """The resolved execution plan of ONE rerun (never a reusable authority)."""

    calculation_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    spec_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    binding_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    declared_input_roles: tuple[str, ...] = Field(default=(), max_length=32)
    parameter_names: tuple[str, ...] = Field(default=(), max_length=32)
    execution_context: DefinitionExecutionContext


class DefinitionRunValidation(StrictContract):
    """What was actually re-validated for THIS rerun, by branch."""

    branch: RevalidationBranch
    reasons: tuple[RevalidationReason, ...] = Field(default=(), max_length=16)
    binding_failures: tuple[str, ...] = Field(default=(), max_length=32)
    authorization_revision: str | None = Field(default=None, max_length=256)
    release_id: str | None = Field(default=None, max_length=256)
    snapshot_id: str | None = Field(default=None, max_length=256)
    freshness: tuple[InputFreshnessDQ, ...] = Field(default=(), max_length=32)
    before_resolution: DefinitionRevalidationResult
    resolved_inputs: DefinitionRevalidationResult | None = None
    validated_at: datetime


class DefinitionRunReceipt(StrictContract):
    """The executed receipt of THIS rerun; absent for a refused rerun."""

    result: CustomCalculationExecutionResult


class DefinitionRunRecord(StrictContract):
    """One immutable per-run record: binding + plan + validation + receipt."""

    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    definition_id: str = Field(pattern=r"^def_[0-9a-f]{32}$")
    version: int = Field(ge=1)
    definition_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    binding: DefinitionExecutionBinding
    plan: DefinitionRunPlan
    validation: DefinitionRunValidation
    # Every CURRENT-authority check that was SKIPPED because this deployment has
    # no provider for it.  Empty in product mode by construction.  This is the
    # authoritative audit landing point for a degraded rerun.
    degradations: tuple[str, ...] = Field(default=(), max_length=16)
    receipt: DefinitionRunReceipt | None = None
    created_at: datetime


class DefinitionRunRecordStore(Protocol):
    """Queryable per-run record port (process-local or Control-PostgreSQL)."""

    async def put(self, *, record: DefinitionRunRecord) -> None: ...

    async def get(
        self, *, definition_id: str, version: int, run_id: str
    ) -> DefinitionRunRecord | None: ...

    async def list_for_version(
        self, *, definition_id: str, version: int
    ) -> tuple[DefinitionRunRecord, ...]: ...


class InMemoryDefinitionRunRecordStore:
    """Process-local, insertion-ordered, deliberately non-durable."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int, str], DefinitionRunRecord] = {}

    async def put(self, *, record: DefinitionRunRecord) -> None:
        self._records[(record.definition_id, record.version, record.run_id)] = record

    async def get(
        self, *, definition_id: str, version: int, run_id: str
    ) -> DefinitionRunRecord | None:
        return self._records.get((definition_id, version, run_id))

    async def list_for_version(
        self, *, definition_id: str, version: int
    ) -> tuple[DefinitionRunRecord, ...]:
        return tuple(
            record
            for (record_definition_id, record_version, _), record in self._records.items()
            if record_definition_id == definition_id and record_version == version
        )
