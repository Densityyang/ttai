"""Control-PG artifact repository: the DURABLE implementation of the port.

MASTER_PR_PLAN_V4.md 5.4.1 freezes the layering: Control PostgreSQL owns the
metadata, the typed JSON artifacts, the hashes, the references and the
lifecycle, and large files are NOT persisted by default.  This module is that
layer for artifacts; the process-local repository remains the default for
infra-dev and for tests.

Every fail-closed rule of the port is preserved, and three of them are
additionally enforced by the database itself
(docker/migrations/control/005_product_artifacts.sql):

* "absent" and "not yours" stay the SAME failure, so there is no existence
  oracle;
* the artifact TYPE, its owner and its creation time are immutable;
* the recorded payload checksum is re-verified on every read, so a row edited
  outside the application is REFUSED instead of being served.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.artifacts.contracts import (
    AnalysisArtifact,
    ArtifactEnvelope,
    CustomDefinitionArtifact,
    utcnow,
)
from src.nl2sql.artifacts.repository import (
    ArtifactNotFound,
    ArtifactTypeMismatch,
    new_artifact_id,
)

# Every column the envelope needs, in one place so no read path can silently
# drop the checksum it is supposed to verify.
_SELECT_COLUMNS = """
    artifact_id, artifact_type, owner_user_id, thread_id, run_id,
    payload, payload_checksum, created_at, updated_at
"""


class ArtifactIntegrityError(RuntimeError):
    """The stored payload does not match the checksum recorded with it."""

    def __init__(self) -> None:
        super().__init__("artifact_payload_checksum_mismatch")


def _artifact_type_of(payload: AnalysisArtifact | CustomDefinitionArtifact) -> str:
    return "analysis" if isinstance(payload, AnalysisArtifact) else "custom_definition"


def _envelope_from_row(row: Mapping[Any, Any]) -> ArtifactEnvelope:
    """Rebuild one envelope and RE-VERIFY its integrity before returning it.

    A row that cannot be decoded as the type it declares, or whose payload no
    longer matches the checksum recorded with it, is an INTEGRITY failure: it is
    reported as that one typed error instead of leaking a decoder traceback or
    being served as if it were valid.
    """

    artifact_type = str(row["artifact_type"])
    raw_payload = row["payload"]
    try:
        payload_data: dict[str, Any] = (
            dict(raw_payload)
            if isinstance(raw_payload, Mapping)
            else json.loads(str(raw_payload))
        )
        if artifact_type == "analysis":
            payload: AnalysisArtifact | CustomDefinitionArtifact = (
                AnalysisArtifact.model_validate(payload_data)
            )
        elif artifact_type == "custom_definition":
            payload = CustomDefinitionArtifact.model_validate(payload_data)
        else:
            raise ArtifactIntegrityError()
        envelope = ArtifactEnvelope(
            artifact_id=str(row["artifact_id"]),
            artifact_type=artifact_type,  # type: ignore[arg-type]
            owner_user_id=str(row["owner_user_id"]),
            thread_id=(
                str(row["thread_id"]) if row["thread_id"] is not None else None
            ),
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            payload=payload,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ArtifactIntegrityError() from exc
    if envelope.payload_checksum != str(row["payload_checksum"]):
        raise ArtifactIntegrityError()
    return envelope


class ControlArtifactRepository:
    """Durable, owner-scoped artifact repository on Control PostgreSQL."""

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
                application_name="ttai-product-artifacts",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        """Prove the artifact store is actually reachable, not merely configured.

        Startup readiness must not claim a durable store it cannot reach, so the
        container calls this once while it owns the lifecycle.
        """

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def create(
        self,
        *,
        owner_user_id: str,
        payload: AnalysisArtifact | CustomDefinitionArtifact,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactEnvelope:
        if not owner_user_id or not owner_user_id.strip():
            raise ValueError("artifact owner must be a non-blank identity")
        now = utcnow()
        record = ArtifactEnvelope(
            artifact_id=new_artifact_id(),
            artifact_type=_artifact_type_of(payload),  # type: ignore[arg-type]
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
            created_at=now,
            updated_at=now,
            payload=payload,
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO product_artifacts (
                      artifact_id, artifact_type, owner_user_id, thread_id, run_id,
                      payload, payload_checksum, created_at, updated_at
                    ) VALUES (
                      :artifact_id, :artifact_type, :owner_user_id, :thread_id, :run_id,
                      CAST(:payload AS jsonb), :payload_checksum, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "artifact_id": record.artifact_id,
                    "artifact_type": record.artifact_type,
                    "owner_user_id": record.owner_user_id,
                    "thread_id": record.thread_id,
                    "run_id": record.run_id,
                    "payload": json.dumps(
                        record.payload.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "payload_checksum": record.payload_checksum,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                },
            )
        return record

    async def get(self, *, owner_user_id: str, artifact_id: str) -> ArtifactEnvelope:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_SELECT_COLUMNS} FROM product_artifacts "
                            "WHERE artifact_id = :artifact_id"
                        ),
                        {"artifact_id": artifact_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        # Identical failure for "absent" and "not yours".
        if row is None or str(row["owner_user_id"]) != owner_user_id:
            raise ArtifactNotFound()
        return _envelope_from_row(row)

    async def list_for_owner(
        self, *, owner_user_id: str
    ) -> tuple[ArtifactEnvelope, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_SELECT_COLUMNS} FROM product_artifacts "
                            "WHERE owner_user_id = :owner_user_id "
                            "ORDER BY created_at DESC, artifact_id"
                        ),
                        {"owner_user_id": owner_user_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_envelope_from_row(row) for row in rows)

    async def replace_payload(
        self,
        *,
        owner_user_id: str,
        artifact_id: str,
        payload: AnalysisArtifact | CustomDefinitionArtifact,
    ) -> ArtifactEnvelope:
        # The type guard is part of the WHERE clause, so a mismatched
        # replacement can never write the row it must not touch.
        incoming_type = _artifact_type_of(payload)
        updated_at = utcnow()
        checksum_source = ArtifactEnvelope(
            artifact_id=artifact_id,
            artifact_type=incoming_type,  # type: ignore[arg-type]
            owner_user_id=owner_user_id,
            created_at=updated_at,
            updated_at=updated_at,
            payload=payload,
        )
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                            UPDATE product_artifacts
                               SET payload = CAST(:payload AS jsonb),
                                   payload_checksum = :payload_checksum,
                                   updated_at = :updated_at
                             WHERE artifact_id = :artifact_id
                               AND owner_user_id = :owner_user_id
                               AND artifact_type = :artifact_type
                            RETURNING """
                            + _SELECT_COLUMNS
                        ),
                        {
                            "artifact_id": artifact_id,
                            "owner_user_id": owner_user_id,
                            "artifact_type": incoming_type,
                            "payload": json.dumps(
                                payload.model_dump(mode="json"),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            "payload_checksum": checksum_source.payload_checksum,
                            "updated_at": updated_at,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                # Distinguish "not yours / absent" from "wrong type" only AFTER
                # the guarded update proved it wrote nothing.
                existing = (
                    (
                        await connection.execute(
                            text(
                                "SELECT artifact_type FROM product_artifacts "
                                "WHERE artifact_id = :artifact_id "
                                "AND owner_user_id = :owner_user_id"
                            ),
                            {
                                "artifact_id": artifact_id,
                                "owner_user_id": owner_user_id,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    raise ArtifactNotFound()
                raise ArtifactTypeMismatch()
        return _envelope_from_row(row)


__all__ = [
    "ArtifactIntegrityError",
    "ControlArtifactRepository",
]
