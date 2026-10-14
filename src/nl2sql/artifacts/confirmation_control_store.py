"""Control-PG storage for the two server-owned confirmation records.

MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
metadata, the typed JSON artifacts, the hashes, the references and the
lifecycle.  This module is that layer for the two confirmation records that were
still process-local:

* ControlConfirmationAuditStore - the DURABLE implementation of
  ConfirmationAuditStore: WHO confirmed an EXACT definition version, WHEN, and
  against WHICH server-side decision.  It is append-only, exactly like the
  in-memory store it replaces in a control-backed deployment.
* ControlExplorationConfirmationStore - the DURABLE implementation of
  ExplorationConfirmationStore: run-scoped exploration confirmations, which
  never stand in for a definition confirmation.

The process-local InMemoryConfirmationAuditStore /
InMemoryExplorationConfirmationStore remain the default for infra-dev and for
tests; neither is removed and neither is subclassed here.

What the database itself enforces
(docker/migrations/control/007_confirmation_audit.sql):

* an audit record is APPEND-ONLY: UPDATE and DELETE are refused by a trigger, so
  the trail can never be rewritten;
* an audit record is bound by a COMPOSITE FOREIGN KEY to the EXACT
  custom_definition_versions row it confirms, so a dangling definition version
  or a checksum mismatch cannot be written;
* confirmed_by / confirmed_at can never be NULL and the actor can never be
  blank;
* an exploration confirmation replaces_definition_confirmation is the literal
  FALSE, and (run_id, exploration_id) is UNIQUE.

One invariant cannot be expressed by the database and is therefore re-verified
on every read: a row must decode as the model it declares.  A row written by a
direct SQL writer that violates it is REFUSED as the one typed
ConfirmationStoreIntegrityError instead of being served, so no bare pydantic
ValidationError ever escapes this layer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.artifacts.definition_confirmation_audit import (
    ConfirmationRecord,
)
from src.nl2sql.artifacts.exploration_confirmation import (
    ExplorationConfirmation,
    ExplorationDefinitionReference,
)

_AUDIT_COLUMNS = (
    "schema_version, definition_id, version, definition_checksum, "
    "confirmed_by, confirmed_at, decision_reference"
)
_EXPLORATION_COLUMNS = (
    "schema_version, exploration_id, run_id, subject, confirmed_by, "
    "confirmed_at, definition_reference, replaces_definition_confirmation"
)

# The audit record is INSERTed once and never rewritten; a repeated confirmation
# of the same version appends ANOTHER row, exactly like the in-memory store.
_INSERT_AUDIT = """
    INSERT INTO definition_confirmation_audit (
      schema_version, definition_id, version, definition_checksum,
      confirmed_by, confirmed_at, decision_reference
    ) VALUES (
      :schema_version, :definition_id, :version, :definition_checksum,
      :confirmed_by, :confirmed_at, :decision_reference
    )
"""

# (run_id, exploration_id) is UNIQUE.  The in-memory store OVERWRITES a repeated
# key, so the control store does the same; the CHECK that keeps
# replaces_definition_confirmation FALSE is enforced on the updated row too.
_INSERT_EXPLORATION = """
    INSERT INTO exploration_confirmations (
      schema_version, exploration_id, run_id, subject, confirmed_by,
      confirmed_at, definition_reference, replaces_definition_confirmation
    ) VALUES (
      :schema_version, :exploration_id, :run_id, :subject, :confirmed_by,
      :confirmed_at, CAST(:definition_reference AS jsonb), FALSE
    )
    ON CONFLICT (run_id, exploration_id) DO UPDATE SET
      subject = EXCLUDED.subject,
      confirmed_by = EXCLUDED.confirmed_by,
      confirmed_at = EXCLUDED.confirmed_at,
      definition_reference = EXCLUDED.definition_reference,
      replaces_definition_confirmation = FALSE
"""

# The database constraint/trigger names are the stable reason codes reported to
# callers.  A raw SQL writer that violates a documented invariant is refused by
# one of these, never by a silently coerced value.
_CONFLICT_REASONS = (
    "confirmation_audit_append_only",
    "definition_confirmation_audit_version_exists",
    "definition_confirmation_audit_actor_present",
    "definition_confirmation_audit_id_shape",
    "definition_confirmation_audit_version_positive",
    "definition_confirmation_audit_checksum_shape",
    "definition_confirmation_audit_reference_bounded",
    "definition_confirmation_audit_schema_version_known",
    "exploration_confirmations_replaces_never",
    "exploration_confirmations_run_exploration_key",
    "exploration_confirmations_actor_present",
    "exploration_confirmations_id_shape",
    "exploration_confirmations_run_id_shape",
    "exploration_confirmations_subject_bounded",
    "exploration_confirmations_reference_complete",
    "exploration_confirmations_schema_version_known",
)


class ConfirmationStoreIntegrityError(RuntimeError):
    """A stored confirmation row cannot be trusted as the model it declares."""

    def __init__(self) -> None:
        super().__init__("confirmation_store_row_incoherent")


class ConfirmationStoreConflict(ValueError):
    """A database-enforced confirmation invariant refused the write."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _conflict_reason(exc: IntegrityError, default: str) -> str:
    rendered = str(exc.orig)
    for reason in _CONFLICT_REASONS:
        if reason in rendered:
            return reason
    return default


def _json_object(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else json.loads(str(raw))


def _reference_json(reference: ExplorationDefinitionReference | None) -> str | None:
    if reference is None:
        return None
    return json.dumps(
        reference.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )


def _reference_from_raw(raw: Any) -> ExplorationDefinitionReference | None:
    if raw is None:
        return None
    # A JSON object arrives as a string from asyncpg for a raw text() query;
    # strict=False is a JSON-SHAPE allowance only and relaxes neither
    # extra="forbid" nor the field patterns.
    return ExplorationDefinitionReference.model_validate(
        _json_object(raw), strict=False
    )


def _confirmation_record_from_row(row: Mapping[Any, Any]) -> ConfirmationRecord:
    try:
        return ConfirmationRecord.model_validate(
            {
                "schema_version": row["schema_version"],
                "definition_id": row["definition_id"],
                "version": row["version"],
                "definition_checksum": row["definition_checksum"],
                "confirmed_by": row["confirmed_by"],
                "confirmed_at": row["confirmed_at"],
                "decision_reference": row["decision_reference"],
            },
            strict=False,
        )
    except (ValueError, TypeError, KeyError) as exc:
        # pydantic ValidationError is a ValueError, so a corrupted row becomes
        # the ONE typed integrity error instead of leaking the raw validation
        # failure to the caller.
        raise ConfirmationStoreIntegrityError() from exc


def _exploration_from_row(row: Mapping[Any, Any]) -> ExplorationConfirmation:
    try:
        return ExplorationConfirmation.model_validate(
            {
                "schema_version": row["schema_version"],
                "exploration_id": row["exploration_id"],
                "run_id": row["run_id"],
                "subject": row["subject"],
                "confirmed_by": row["confirmed_by"],
                "confirmed_at": row["confirmed_at"],
                "definition_reference": _reference_from_raw(
                    row["definition_reference"]
                ),
                "replaces_definition_confirmation": row[
                    "replaces_definition_confirmation"
                ],
            },
            strict=False,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfirmationStoreIntegrityError() from exc


class ControlConfirmationAuditStore:
    """Durable, append-only confirmation audit on Control PostgreSQL."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_runtime_async_engine(
                database_url or "",
                purpose=DatabasePurpose.CONTROL_APP,
                application_name="ttai-confirmation-audit",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        """Prove the audit store is actually reachable, not merely configured."""

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def put(self, *, record: ConfirmationRecord) -> None:
        values = {
            "schema_version": record.schema_version,
            "definition_id": record.definition_id,
            "version": record.version,
            "definition_checksum": record.definition_checksum,
            "confirmed_by": record.confirmed_by,
            "confirmed_at": record.confirmed_at,
            "decision_reference": record.decision_reference,
        }
        try:
            async with self._engine.begin() as connection:
                await connection.execute(text(_INSERT_AUDIT), values)
        except IntegrityError as exc:
            raise ConfirmationStoreConflict(
                _conflict_reason(exc, "confirmation_audit_invalid")
            ) from exc

    async def get(
        self, *, definition_id: str, version: int
    ) -> ConfirmationRecord | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_AUDIT_COLUMNS} "
                            "FROM definition_confirmation_audit "
                            "WHERE definition_id = :definition_id "
                            "AND version = :version "
                            "ORDER BY audit_seq DESC LIMIT 1"
                        ),
                        {"definition_id": definition_id, "version": version},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _confirmation_record_from_row(row)

    async def list_for_definition(
        self, *, definition_id: str
    ) -> tuple[ConfirmationRecord, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_AUDIT_COLUMNS} "
                            "FROM definition_confirmation_audit "
                            "WHERE definition_id = :definition_id "
                            "ORDER BY audit_seq"
                        ),
                        {"definition_id": definition_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_confirmation_record_from_row(row) for row in rows)


class ControlExplorationConfirmationStore:
    """Durable, run-scoped exploration confirmations on Control PostgreSQL."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_runtime_async_engine(
                database_url or "",
                purpose=DatabasePurpose.CONTROL_APP,
                application_name="ttai-exploration-confirmations",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        """Prove the exploration store is reachable, not merely configured."""

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def put(self, *, record: ExplorationConfirmation) -> None:
        values = {
            "schema_version": record.schema_version,
            "exploration_id": record.exploration_id,
            "run_id": record.run_id,
            "subject": record.subject,
            "confirmed_by": record.confirmed_by,
            "confirmed_at": record.confirmed_at,
            "definition_reference": _reference_json(record.definition_reference),
        }
        try:
            async with self._engine.begin() as connection:
                await connection.execute(text(_INSERT_EXPLORATION), values)
        except IntegrityError as exc:
            raise ConfirmationStoreConflict(
                _conflict_reason(exc, "exploration_confirmation_invalid")
            ) from exc

    async def get(
        self, *, run_id: str, exploration_id: str
    ) -> ExplorationConfirmation | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_EXPLORATION_COLUMNS} "
                            "FROM exploration_confirmations "
                            "WHERE run_id = :run_id "
                            "AND exploration_id = :exploration_id"
                        ),
                        {"run_id": run_id, "exploration_id": exploration_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _exploration_from_row(row)

    async def list_for_run(
        self, *, run_id: str
    ) -> tuple[ExplorationConfirmation, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_EXPLORATION_COLUMNS} "
                            "FROM exploration_confirmations "
                            "WHERE run_id = :run_id ORDER BY recorded_seq"
                        ),
                        {"run_id": run_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_exploration_from_row(row) for row in rows)


__all__ = [
    "ConfirmationStoreConflict",
    "ConfirmationStoreIntegrityError",
    "ControlConfirmationAuditStore",
    "ControlExplorationConfirmationStore",
]
