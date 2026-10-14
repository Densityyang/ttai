"""Durable owner-scoped Control-PG store for immutable definition run records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.artifacts.definition_run_record import (
    DefinitionRunRecord,
    DefinitionRunRecordConflict,
    DefinitionRunRecordStoreUnavailable,
    definition_run_record_checksum,
)

_RECORD_COLUMNS = (
    "schema_version, owner_user_id, definition_id, version, run_id, "
    "definition_checksum, payload, payload_checksum, created_at"
)

_INSERT_RECORD = """
    INSERT INTO definition_run_records (
      schema_version, owner_user_id, definition_id, version, run_id,
      definition_checksum, payload, payload_checksum, created_at
    ) VALUES (
      :schema_version, :owner_user_id, :definition_id, :version, :run_id,
      :definition_checksum, CAST(:payload AS jsonb), :payload_checksum,
      :created_at
    )
    ON CONFLICT (owner_user_id, definition_id, version, run_id) DO NOTHING
    RETURNING run_id
"""

_SELECT_IDENTITY = f"""
    SELECT {_RECORD_COLUMNS}
    FROM definition_run_records
    WHERE owner_user_id = :owner_user_id
      AND definition_id = :definition_id
      AND version = :version
      AND run_id = :run_id
"""

_SELECT_VERSION = f"""
    SELECT {_RECORD_COLUMNS}
    FROM definition_run_records
    WHERE owner_user_id = :owner_user_id
      AND definition_id = :definition_id
      AND version = :version
    ORDER BY created_at ASC, run_id ASC
"""


class DefinitionRunRecordIntegrityError(RuntimeError):
    """A durable row does not decode to its declared, checksummed identity."""

    def __init__(self) -> None:
        super().__init__("definition_run_record_row_incoherent")


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    decoded = json.loads(str(raw))
    if not isinstance(decoded, dict):
        raise ValueError("run_record_payload_not_object")
    return decoded


def _record_from_row(row: Mapping[Any, Any]) -> DefinitionRunRecord:
    try:
        record = DefinitionRunRecord.model_validate(
            _json_object(row["payload"]), strict=False
        )
        coherent = (
            record.schema_version == str(row["schema_version"])
            and record.owner_user_id == str(row["owner_user_id"])
            and record.definition_id == str(row["definition_id"])
            and record.version == int(row["version"])
            and record.run_id == str(row["run_id"])
            and record.definition_checksum == str(row["definition_checksum"])
            and definition_run_record_checksum(record)
            == str(row["payload_checksum"])
            and record.created_at == row["created_at"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DefinitionRunRecordIntegrityError() from exc
    if not coherent:
        raise DefinitionRunRecordIntegrityError()
    return record


def _payload_json(record: DefinitionRunRecord) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ControlDefinitionRunRecordStore:
    """Append-only Control-PostgreSQL persistence for rerun evidence."""

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
                application_name="ttai-definition-run-records",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT 1 FROM schema_migrations "
                        "WHERE version = '008_definition_run_records'"
                    )
                )
                if result.scalar_one_or_none() is None:
                    raise DefinitionRunRecordStoreUnavailable()
        except DefinitionRunRecordStoreUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise DefinitionRunRecordStoreUnavailable() from exc

    async def put(self, *, record: DefinitionRunRecord) -> None:
        payload_checksum = definition_run_record_checksum(record)
        values = {
            "schema_version": record.schema_version,
            "owner_user_id": record.owner_user_id,
            "definition_id": record.definition_id,
            "version": record.version,
            "run_id": record.run_id,
            "definition_checksum": record.definition_checksum,
            "payload": _payload_json(record),
            "payload_checksum": payload_checksum,
            "created_at": record.created_at,
        }
        identity = {
            "owner_user_id": record.owner_user_id,
            "definition_id": record.definition_id,
            "version": record.version,
            "run_id": record.run_id,
        }
        try:
            async with self._engine.begin() as connection:
                inserted = await connection.execute(text(_INSERT_RECORD), values)
                if inserted.scalar_one_or_none() is not None:
                    return
                existing_result = await connection.execute(
                    text(_SELECT_IDENTITY), identity
                )
                row = existing_result.mappings().one_or_none()
                if row is None:
                    raise DefinitionRunRecordConflict(
                        "definition_run_record_reference_invalid"
                    )
                existing = _record_from_row(row)
                if definition_run_record_checksum(existing) != payload_checksum:
                    raise DefinitionRunRecordConflict()
        except DefinitionRunRecordConflict:
            raise
        except IntegrityError as exc:
            raise DefinitionRunRecordConflict(
                "definition_run_record_reference_invalid"
            ) from exc
        except SQLAlchemyError as exc:
            raise DefinitionRunRecordStoreUnavailable() from exc

    async def get(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
        run_id: str,
    ) -> DefinitionRunRecord | None:
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(
                    text(_SELECT_IDENTITY),
                    {
                        "owner_user_id": owner_user_id,
                        "definition_id": definition_id,
                        "version": version,
                        "run_id": run_id,
                    },
                )
                row = result.mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise DefinitionRunRecordStoreUnavailable() from exc
        return None if row is None else _record_from_row(row)

    async def list_for_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> tuple[DefinitionRunRecord, ...]:
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(
                    text(_SELECT_VERSION),
                    {
                        "owner_user_id": owner_user_id,
                        "definition_id": definition_id,
                        "version": version,
                    },
                )
                rows = result.mappings().all()
        except SQLAlchemyError as exc:
            raise DefinitionRunRecordStoreUnavailable() from exc
        return tuple(_record_from_row(row) for row in rows)
