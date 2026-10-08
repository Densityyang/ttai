"""Control-PG definition store: the DURABLE implementation of the port.

MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
metadata, the typed JSON artifacts, the hashes, the references and the
lifecycle.  This module is that layer for Custom Definitions; the process-local
InMemoryDefinitionStore remains the default for infra-dev and for tests.

The service keeps EVERY lifecycle decision.  This store performs no policy: it
reads and writes the stable identity with its current axes/version, the exact
immutable versions and the per-version private lifecycle.  What it DOES add is
that the documented invariants are also enforced by the database itself
(docker/migrations/control/006_definition_store.sql):

* an EXACT version is immutable, so a confirmed definition can never be
  rewritten in place;
* the owner and the creation time of a definition are immutable;
* the current-version pointer is MONOTONIC, so a revision never moves it
  backwards;
* exact version numbers are MONOTONIC, so a version cannot be skipped;
* the axis implications (SAVED=>CONFIRMED, PUBLISHED=>SAVED+CONFIRMED,
  CERTIFIED=>PUBLISHED) hold on both the identity and the per-version
  lifecycle, so the INDEPENDENT axes cannot be collapsed or contradicted;
* Governance is persisted as an ORTHOGONAL proposal axis, and a Custom
  Definition ORIGINAL OBJECT can never be canonicalized in place (authority is
  noncanonical for its whole life);
* a published version REFERENCES a real exact definition version and its
  checksum, so a dangling source_definition_id cannot be written.

Two invariants cannot be expressed by the database and are therefore
re-verified on every read: a row must decode as the model it declares, and an
exact version's recomputed checksum must equal the checksum recorded with it.  A
row written by a direct SQL writer that violates either one is REFUSED as the
one typed DefinitionStoreIntegrityError instead of being served.
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
from src.nl2sql.artifacts.custom_definition import (
    CustomDefinition,
    DefinitionAxes,
    DefinitionVersion,
    DefinitionVersionLifecycle,
    ParameterContract,
)

_DEFINITION_COLUMNS = """
    definition_id, owner_user_id, confirmation, retention, publication,
    certification, governance, authority, current_version, current_payload
"""
_VERSION_COLUMNS = "definition_id, version, payload, checksum"
_LIFECYCLE_COLUMNS = "definition_id, version, confirmation, retention"

# The identity/axes row is written FIRST and in ONE statement, so a definition is
# never observable without its current version.  owner_user_id and created_at are
# deliberately NOT in the UPDATE list: the database freezes them anyway.
_UPSERT_DEFINITION = """
    INSERT INTO custom_definitions (
      definition_id, owner_user_id, confirmation, retention, publication,
      certification, governance, authority, current_version, current_payload,
      created_at, updated_at
    ) VALUES (
      :definition_id, :owner_user_id, :confirmation, :retention, :publication,
      :certification, :governance, :authority, :current_version,
      CAST(:current_payload AS jsonb), now(), now()
    )
    ON CONFLICT (definition_id) DO UPDATE SET
      confirmation = EXCLUDED.confirmation,
      retention = EXCLUDED.retention,
      publication = EXCLUDED.publication,
      certification = EXCLUDED.certification,
      governance = EXCLUDED.governance,
      authority = EXCLUDED.authority,
      current_version = EXCLUDED.current_version,
      current_payload = EXCLUDED.current_payload,
      updated_at = now()
"""

# An exact version is INSERTed once.  DO NOTHING keeps a concurrent duplicate
# from aborting the transaction, so the caller can decide idempotent vs conflict.
_INSERT_VERSION = """
    INSERT INTO custom_definition_versions (
      definition_id, version, payload, checksum, created_at
    ) VALUES (
      :definition_id, :version, CAST(:payload AS jsonb), :checksum, :created_at
    )
    ON CONFLICT (definition_id, version) DO NOTHING
    RETURNING version
"""

_UPSERT_LIFECYCLE = """
    INSERT INTO custom_definition_lifecycles (
      definition_id, version, confirmation, retention
    ) VALUES (
      :definition_id, :version, :confirmation, :retention
    )
    ON CONFLICT (definition_id, version) DO UPDATE SET
      confirmation = EXCLUDED.confirmation,
      retention = EXCLUDED.retention
"""

# The database trigger names are the stable reason codes reported to callers.
_CONFLICT_REASONS = (
    "definition_identity_immutable",
    "definition_owner_immutable",
    "definition_created_at_immutable",
    "definition_updated_at_regression",
    "definition_current_version_regression",
    "definition_version_not_monotonic",
    "exact_definition_version_immutable",
    "custom_definitions_authority_noncanonical",
    "custom_definitions_governance_known",
    "custom_definitions_retention_requires_confirmation",
    "custom_definitions_publication_requires_saved",
    "custom_definitions_certification_requires_publication",
)


class DefinitionStoreIntegrityError(RuntimeError):
    """A stored definition row cannot be trusted as the model it declares."""

    def __init__(self) -> None:
        super().__init__("definition_store_row_incoherent")


class DefinitionStoreConflict(ValueError):
    """A database-enforced definition invariant refused the write."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _conflict_reason(exc: IntegrityError, default: str) -> str:
    rendered = str(exc.orig)
    for reason in _CONFLICT_REASONS:
        if reason in rendered:
            return reason
    return default


def _payload_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_object(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else json.loads(str(raw))


def _version_from_payload(raw: Any) -> DefinitionVersion:
    data = _json_object(raw)
    # A JSON array is not a Python tuple and the parameter models are strict;
    # strict=False is a JSON-SHAPE allowance only and relaxes neither
    # extra="forbid" nor the coherence validator on DefinitionVersion.
    data["parameter_contract"] = ParameterContract.model_validate(
        data["parameter_contract"], strict=False
    )
    return DefinitionVersion.model_validate(data, strict=False)


def _axes_from_row(row: Mapping[Any, Any]) -> DefinitionAxes:
    """Rebuild the axes THROUGH the model validator.

    The database CHECKs guarantee the stored axes are legal, but a row edited
    outside the application must still be REFUSED rather than coerced: the
    model's own Literal + implication validators run here, and any violation is
    reported as the one typed integrity error.  This is deliberately a
    validation, not a cast, so an illegal stored value cannot be hidden.
    """

    try:
        return DefinitionAxes.model_validate(
            {
                "confirmation": row["confirmation"],
                "retention": row["retention"],
                "publication": row["publication"],
                "certification": row["certification"],
                "governance": row["governance"],
                "authority": row["authority"],
            }
        )
    except (ValueError, TypeError) as exc:
        raise DefinitionStoreIntegrityError() from exc


def _definition_from_row(row: Mapping[Any, Any]) -> CustomDefinition:
    try:
        version = _version_from_payload(row["current_payload"])
        definition = CustomDefinition(
            definition_id=str(row["definition_id"]),
            owner_user_id=str(row["owner_user_id"]),
            axes=_axes_from_row(row),
            current_version=version,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise DefinitionStoreIntegrityError() from exc
    if version.version != int(row["current_version"]):
        raise DefinitionStoreIntegrityError()
    return definition


def _version_from_row(row: Mapping[Any, Any]) -> DefinitionVersion:
    try:
        version = _version_from_payload(row["payload"])
    except (ValueError, TypeError, KeyError) as exc:
        raise DefinitionStoreIntegrityError() from exc
    # The recorded checksum is RE-VERIFIED, so a row edited outside the
    # application is refused instead of being served.
    if version.checksum != str(row["checksum"]):
        raise DefinitionStoreIntegrityError()
    if version.definition_id != str(row["definition_id"]):
        raise DefinitionStoreIntegrityError()
    if version.version != int(row["version"]):
        raise DefinitionStoreIntegrityError()
    return version


def _lifecycle_from_row(row: Mapping[Any, Any]) -> DefinitionVersionLifecycle:
    try:
        return DefinitionVersionLifecycle.model_validate(
            {
                "confirmation": row["confirmation"],
                "retention": row["retention"],
            }
        )
    except (ValueError, TypeError) as exc:
        raise DefinitionStoreIntegrityError() from exc


class ControlDefinitionStore:
    """Durable, fail-closed definition store on Control PostgreSQL."""

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
                application_name="ttai-product-definitions",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        """Prove the definition store is actually reachable, not merely
        configured, so startup readiness cannot claim a store it cannot reach.
        """

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def get_definition(self, *, definition_id: str) -> CustomDefinition | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_DEFINITION_COLUMNS} FROM custom_definitions "
                            "WHERE definition_id = :definition_id"
                        ),
                        {"definition_id": definition_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _definition_from_row(row)

    async def list_definitions(self) -> tuple[CustomDefinition, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_DEFINITION_COLUMNS} FROM custom_definitions "
                            "ORDER BY created_at, definition_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_definition_from_row(row) for row in rows)

    async def put_definition(self, *, definition: CustomDefinition) -> None:
        values = {
            "definition_id": definition.definition_id,
            "owner_user_id": definition.owner_user_id,
            "confirmation": definition.axes.confirmation,
            "retention": definition.axes.retention,
            "publication": definition.axes.publication,
            "certification": definition.axes.certification,
            "governance": definition.axes.governance,
            "authority": definition.axes.authority,
            "current_version": definition.current_version.version,
            "current_payload": _payload_json(
                definition.current_version.model_dump(mode="json")
            ),
        }
        try:
            async with self._engine.begin() as connection:
                await connection.execute(text(_UPSERT_DEFINITION), values)
        except IntegrityError as exc:
            raise DefinitionStoreConflict(
                _conflict_reason(exc, "definition_store_conflict")
            ) from exc

    async def get_version(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersion | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_VERSION_COLUMNS} "
                            "FROM custom_definition_versions "
                            "WHERE definition_id = :definition_id "
                            "AND version = :version"
                        ),
                        {"definition_id": definition_id, "version": version},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _version_from_row(row)

    async def list_versions(
        self, *, definition_id: str
    ) -> tuple[DefinitionVersion, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_VERSION_COLUMNS} "
                            "FROM custom_definition_versions "
                            "WHERE definition_id = :definition_id ORDER BY version"
                        ),
                        {"definition_id": definition_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_version_from_row(row) for row in rows)

    async def put_version(self, *, version: DefinitionVersion) -> None:
        values = {
            "definition_id": version.definition_id,
            "version": version.version,
            "payload": _payload_json(version.model_dump(mode="json")),
            "checksum": version.checksum,
            "created_at": version.created_at,
        }
        try:
            async with self._engine.begin() as connection:
                inserted = (
                    await connection.execute(text(_INSERT_VERSION), values)
                ).scalar_one_or_none()
        except IntegrityError as exc:
            raise DefinitionStoreConflict(
                _conflict_reason(exc, "definition_version_immutable")
            ) from exc
        if inserted is None:
            # The row already existed.  An IDENTICAL re-write is idempotent; a
            # DIFFERENT one is refused, because an exact version is immutable.
            existing = await self.get_version(
                definition_id=version.definition_id, version=version.version
            )
            if existing == version:
                return
            raise DefinitionStoreConflict("definition_version_immutable")

    async def get_lifecycle(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersionLifecycle | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_LIFECYCLE_COLUMNS} "
                            "FROM custom_definition_lifecycles "
                            "WHERE definition_id = :definition_id "
                            "AND version = :version"
                        ),
                        {"definition_id": definition_id, "version": version},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _lifecycle_from_row(row)

    async def put_lifecycle(
        self,
        *,
        definition_id: str,
        version: int,
        lifecycle: DefinitionVersionLifecycle,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    text(_UPSERT_LIFECYCLE),
                    {
                        "definition_id": definition_id,
                        "version": version,
                        "confirmation": lifecycle.confirmation,
                        "retention": lifecycle.retention,
                    },
                )
        except IntegrityError as exc:
            raise DefinitionStoreConflict(
                _conflict_reason(exc, "definition_lifecycle_invalid")
            ) from exc


__all__ = [
    "ControlDefinitionStore",
    "DefinitionStoreConflict",
    "DefinitionStoreIntegrityError",
]
