"""Per-run records for a SAVED Custom Definition rerun (A7, 5.2.2).

Every rerun records its OWN Execution Binding, execution plan, validation and
receipt.  A previous run's result is never presented as the current data result,
and a normal parameter rebinding never creates a new Definition Version (A5).

The store is an explicit PORT: the process-local implementation below is
deliberately non-durable and exists so the durable Control-PostgreSQL backend
can be swapped in behind the SAME interface.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

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
    "DefinitionRunRecordConflict",
    "DefinitionRunRecordStore",
    "DefinitionRunRecordStoreUnavailable",
    "DefinitionRunValidation",
    "InMemoryDefinitionRunRecordStore",
    "definition_run_record_checksum",
    "new_run_id",
]

_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return "run_" + uuid4().hex


def definition_run_record_checksum(record: DefinitionRunRecord) -> str:
    """Return the stable checksum of the typed record payload.

    JSON key ordering is canonicalized so PostgreSQL JSONB's key order does
    not affect integrity verification after a restart.
    """

    payload = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

    schema_version: Literal["1.0"] = "1.0"
    owner_user_id: str = Field(min_length=1, max_length=256)
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

    @field_validator("owner_user_id")
    @classmethod
    def _owner_is_canonical(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("definition_run_record_owner_invalid")
        return value

    @model_validator(mode="after")
    def _identities_are_coherent(self) -> DefinitionRunRecord:
        execution_binding = self.binding.binding
        if (
            self.binding.definition_id != self.definition_id
            or self.binding.version != self.version
            or self.binding.definition_checksum != self.definition_checksum
            or execution_binding.calculation_id != self.plan.calculation_id
            or execution_binding.spec_checksum != self.plan.spec_checksum
            or execution_binding.checksum != self.plan.binding_checksum
        ):
            raise ValueError("definition_run_record_identity_incoherent")
        if self.receipt is not None:
            result = self.receipt.result
            if (
                result.calculation_id != self.plan.calculation_id
                or result.spec_checksum != self.plan.spec_checksum
                or result.binding_checksum != self.plan.binding_checksum
            ):
                raise ValueError("definition_run_record_receipt_incoherent")
        return self


class DefinitionRunRecordConflict(ValueError):
    """A run identity already exists with a different immutable payload."""

    def __init__(self, reason: str = "definition_run_record_immutable_conflict") -> None:
        super().__init__(reason)
        self.reason = reason


class DefinitionRunRecordStoreUnavailable(RuntimeError):
    """The required run-record store cannot be reached safely."""

    code = "definition_run_record_store_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


class DefinitionRunRecordStore(Protocol):
    """Queryable per-run record port (process-local or Control-PostgreSQL)."""

    async def ping(self) -> None: ...

    async def close(self) -> None: ...

    async def put(self, *, record: DefinitionRunRecord) -> None: ...

    async def get(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        run_id: str,
    ) -> DefinitionRunRecord | None: ...

    async def list_for_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> tuple[DefinitionRunRecord, ...]: ...


class InMemoryDefinitionRunRecordStore:
    """Process-local, insertion-ordered, deliberately non-durable."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, int, str], DefinitionRunRecord] = {}

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def put(self, *, record: DefinitionRunRecord) -> None:
        key = (
            record.owner_user_id,
            record.definition_id,
            record.version,
            record.run_id,
        )
        existing = self._records.get(key)
        if existing is not None:
            if definition_run_record_checksum(existing) != definition_run_record_checksum(
                record
            ):
                raise DefinitionRunRecordConflict()
            return
        self._records[key] = record

    async def get(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        run_id: str,
    ) -> DefinitionRunRecord | None:
        return self._records.get((owner_user_id, definition_id, version, run_id))

    async def list_for_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> tuple[DefinitionRunRecord, ...]:
        return tuple(
            record
            for (
                record_owner,
                record_definition_id,
                record_version,
                _,
            ), record in self._records.items()
            if record_owner == owner_user_id
            and record_definition_id == definition_id
            and record_version == version
        )
