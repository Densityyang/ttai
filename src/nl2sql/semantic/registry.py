"""Immutable semantic release registry with an explicit active pointer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings


class SemanticReleaseState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class SemanticDocument:
    document_id: str
    content: str
    metadata: dict[str, str]
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class SemanticRelease:
    release_id: str
    version: int
    checksum: str
    state: SemanticReleaseState
    documents: tuple[SemanticDocument, ...]
    validation_report: dict[str, Any] | None
    change_summary: str
    previous_release_id: str | None
    created_at: datetime


class SemanticReleaseError(RuntimeError):
    pass


class SemanticRegistry:
    """In-process release model used by the indexer and API retrieval boundary.

    Persistence adapters can mirror this state machine in control PostgreSQL.  The
    pointer is intentionally changed only by :meth:`activate`, after validation.
    """

    def __init__(self) -> None:
        self._releases: dict[str, SemanticRelease] = {}
        self._active_release_id: str | None = None

    @property
    def active_release_id(self) -> str | None:
        return self._active_release_id

    def active_release(self) -> SemanticRelease | None:
        if self._active_release_id is None:
            return None
        return self._releases[self._active_release_id]

    def get(self, release_id: str) -> SemanticRelease:
        try:
            return self._releases[release_id]
        except KeyError as exc:
            raise SemanticReleaseError(f"unknown semantic release: {release_id}") from exc

    def create_draft(
        self,
        documents: Iterable[SemanticDocument],
        *,
        change_summary: str,
    ) -> SemanticRelease:
        docs = tuple(documents)
        if not docs:
            raise SemanticReleaseError("a semantic release requires at least one document")
        if any(not document.content.strip() for document in docs):
            raise SemanticReleaseError("semantic documents must not be empty")

        release = SemanticRelease(
            release_id=str(uuid4()),
            version=len(self._releases) + 1,
            checksum=_checksum(docs),
            state=SemanticReleaseState.DRAFT,
            documents=docs,
            validation_report=None,
            change_summary=change_summary.strip(),
            previous_release_id=self._active_release_id,
            created_at=datetime.now(UTC),
        )
        self._releases[release.release_id] = release
        return release

    def validate(
        self,
        release_id: str,
        validator: Callable[[SemanticRelease], dict[str, Any]],
    ) -> SemanticRelease:
        release = self.get(release_id)
        if release.state is not SemanticReleaseState.DRAFT:
            raise SemanticReleaseError("only draft releases can be validated")

        report = validator(release)
        if report.get("ok") is not True:
            raise SemanticReleaseError("semantic release validation failed")
        validated = replace(
            release,
            state=SemanticReleaseState.VALIDATED,
            validation_report=report,
        )
        self._releases[release_id] = validated
        return validated

    def activate(self, release_id: str) -> SemanticRelease:
        candidate = self.get(release_id)
        if candidate.state is not SemanticReleaseState.VALIDATED:
            raise SemanticReleaseError("only validated releases can become active")

        old_active_id = self._active_release_id
        if old_active_id is not None:
            old_active = self._releases[old_active_id]
            self._releases[old_active_id] = replace(old_active, state=SemanticReleaseState.RETIRED)

        active = replace(candidate, state=SemanticReleaseState.ACTIVE)
        self._releases[release_id] = active
        self._active_release_id = release_id
        return active

    def rollback(self, release_id: str) -> SemanticRelease:
        target = self.get(release_id)
        if target.state is not SemanticReleaseState.RETIRED:
            raise SemanticReleaseError("rollback target must be a retired release")
        target = replace(target, state=SemanticReleaseState.VALIDATED)
        self._releases[release_id] = target
        return self.activate(release_id)

    def publish(
        self,
        documents: Iterable[SemanticDocument],
        *,
        change_summary: str,
        validator: Callable[[SemanticRelease], dict[str, Any]],
    ) -> SemanticRelease:
        """Build and validate a candidate without ever replacing the active release on failure."""
        draft = self.create_draft(documents, change_summary=change_summary)
        self.validate(draft.release_id, validator)
        return self.activate(draft.release_id)


class ControlSemanticReleasePublisher:
    """Persist validated semantic releases and atomically move the control-DB pointer."""

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
                application_name="ttai-semantic-publisher",
                settings=get_settings(),
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def publish(
        self,
        documents: Iterable[SemanticDocument],
        *,
        change_summary: str,
        validation_report: dict[str, Any],
    ) -> SemanticRelease:
        registry = SemanticRegistry()
        candidate = registry.create_draft(documents, change_summary=change_summary)
        if validation_report.get("ok") is not True:
            raise SemanticReleaseError("semantic release validation failed")

        async with self._engine.begin() as connection:
            previous_release_id = await _lock_active_pointer(connection)
            version_result = await connection.execute(
                text("SELECT nextval('semantic_release_version_seq')")
            )
            version = int(version_result.scalar_one())
            release = replace(candidate, version=version, previous_release_id=previous_release_id)
            await connection.execute(
                text(
                    """
                    INSERT INTO semantic_releases (
                      release_id, version, checksum, state, validation_report,
                      change_summary, previous_release_id, validated_at
                    ) VALUES (
                      CAST(:release_id AS uuid), :version, :checksum, 'validated',
                      CAST(:validation_report AS jsonb), :change_summary,
                      CAST(:previous_release_id AS uuid), now()
                    )
                    """
                ),
                {
                    "release_id": release.release_id,
                    "version": release.version,
                    "checksum": release.checksum,
                    "validation_report": json.dumps(validation_report),
                    "change_summary": release.change_summary,
                    "previous_release_id": previous_release_id,
                },
            )
            for document in release.documents:
                await connection.execute(
                    text(
                        """
                        INSERT INTO semantic_documents (
                          release_id, document_id, content, metadata, lexical, embedding
                        )
                        VALUES (
                          CAST(:release_id AS uuid), :document_id, :content,
                          CAST(:metadata AS jsonb), to_tsvector('simple', :content),
                          CAST(:embedding AS vector)
                        )
                        """
                    ),
                    {
                        "release_id": release.release_id,
                        "document_id": document.document_id,
                        "content": document.content,
                        "metadata": json.dumps(document.metadata),
                        "embedding": _vector_literal(document.embedding),
                    },
                )
            if previous_release_id is not None:
                await connection.execute(
                    text("UPDATE semantic_releases SET state = 'retired' WHERE release_id = CAST(:release_id AS uuid)"),
                    {"release_id": previous_release_id},
                )
            await connection.execute(
                text("UPDATE semantic_releases SET state = 'active', activated_at = now() WHERE release_id = CAST(:release_id AS uuid)"),
                {"release_id": release.release_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE semantic_release_pointers
                    SET release_id = CAST(:release_id AS uuid), updated_at = now()
                    WHERE pointer_name = 'active'
                    """
                ),
                {"release_id": release.release_id},
            )
        return replace(
            release,
            state=SemanticReleaseState.ACTIVE,
            validation_report=validation_report,
        )

    async def rollback(self, release_id: str) -> SemanticRelease:
        """Atomically move the active pointer to a previously validated release."""
        async with self._engine.begin() as connection:
            active_release_id = await _lock_active_pointer(connection)
            target = (
                await connection.execute(
                    text(
                        """
                        SELECT release_id::text, version, checksum, state, previous_release_id::text,
                               created_at, change_summary, validation_report
                        FROM semantic_releases
                        WHERE release_id = CAST(:release_id AS uuid)
                        FOR UPDATE
                        """
                    ),
                    {"release_id": release_id},
                )
            ).mappings().one_or_none()
            if target is None:
                raise SemanticReleaseError("rollback target does not exist")
            if str(target["state"]) not in {SemanticReleaseState.RETIRED, SemanticReleaseState.VALIDATED}:
                raise SemanticReleaseError("rollback target must be retired or validated")
            if active_release_id is not None:
                await connection.execute(
                    text(
                        """
                        UPDATE semantic_releases
                        SET state = 'retired'
                        WHERE release_id = CAST(:release_id AS uuid) AND state = 'active'
                        """
                    ),
                    {"release_id": active_release_id},
                )
            await connection.execute(
                text(
                    """
                    UPDATE semantic_releases
                    SET state = 'active', activated_at = now()
                    WHERE release_id = CAST(:release_id AS uuid)
                    """
                ),
                {"release_id": release_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE semantic_release_pointers
                    SET release_id = CAST(:release_id AS uuid), updated_at = now()
                    WHERE pointer_name = 'active'
                    """
                ),
                {"release_id": release_id},
            )
            document_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT document_id, content, metadata
                        FROM semantic_documents
                        WHERE release_id = CAST(:release_id AS uuid)
                        ORDER BY document_id
                        """
                    ),
                    {"release_id": release_id},
                )
            ).mappings().all()
        return SemanticRelease(
            release_id=str(target["release_id"]),
            version=int(target["version"]),
            checksum=str(target["checksum"]),
            state=SemanticReleaseState.ACTIVE,
            documents=tuple(
                SemanticDocument(
                    document_id=str(row["document_id"]),
                    content=str(row["content"]),
                    metadata={str(key): str(value) for key, value in dict(row["metadata"]).items()},
                )
                for row in document_rows
            ),
            validation_report=dict(target["validation_report"] or {}),
            change_summary=str(target["change_summary"]),
            previous_release_id=(
                str(target["previous_release_id"]) if target["previous_release_id"] is not None else None
            ),
            created_at=target["created_at"],
        )

    async def read_active(self) -> SemanticRelease | None:
        async with self._engine.connect() as connection:
            release_row = (
                await connection.execute(
                    text(
                        """
                        SELECT r.release_id::text, r.version, r.checksum, r.state,
                               r.validation_report, r.change_summary,
                               r.previous_release_id::text, r.created_at
                        FROM semantic_release_pointers p
                        JOIN semantic_releases r ON r.release_id = p.release_id
                        WHERE p.pointer_name = 'active' AND r.state = 'active'
                        """
                    )
                )
            ).mappings().one_or_none()
            if release_row is None:
                return None
            document_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT document_id, content, metadata
                        FROM semantic_documents
                        WHERE release_id = CAST(:release_id AS uuid)
                        ORDER BY document_id
                        """
                    ),
                    {"release_id": release_row["release_id"]},
                )
            ).mappings().all()
        return SemanticRelease(
            release_id=str(release_row["release_id"]),
            version=int(release_row["version"]),
            checksum=str(release_row["checksum"]),
            state=SemanticReleaseState(str(release_row["state"])),
            documents=tuple(
                SemanticDocument(
                    document_id=str(row["document_id"]),
                    content=str(row["content"]),
                    metadata={str(key): str(value) for key, value in dict(row["metadata"]).items()},
                )
                for row in document_rows
            ),
            validation_report=dict(release_row["validation_report"] or {}),
            change_summary=str(release_row["change_summary"]),
            previous_release_id=(
                str(release_row["previous_release_id"])
                if release_row["previous_release_id"] is not None
                else None
            ),
            created_at=release_row["created_at"],
        )

    async def search_lexical(self, query: str, *, limit: int) -> list[SemanticDocument]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT d.document_id, d.content, d.metadata
                        FROM semantic_release_pointers p
                        JOIN semantic_releases r ON r.release_id = p.release_id AND r.state = 'active'
                        JOIN semantic_documents d ON d.release_id = r.release_id
                        WHERE p.pointer_name = 'active'
                          AND d.lexical @@ websearch_to_tsquery('simple', :query)
                        ORDER BY ts_rank_cd(d.lexical, websearch_to_tsquery('simple', :query)) DESC,
                                 d.document_id
                        LIMIT :limit
                        """
                    ),
                    {"query": query, "limit": limit},
                )
            ).mappings().all()
        return [
            SemanticDocument(
                document_id=str(row["document_id"]),
                content=str(row["content"]),
                metadata={str(key): str(value) for key, value in dict(row["metadata"]).items()},
            )
            for row in rows
        ]

    async def search_vector(
        self,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> list[SemanticDocument]:
        """Search only the current active release using pgvector cosine distance."""
        vector = _vector_literal(embedding)
        if vector is None:
            return []
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT d.document_id, d.content, d.metadata
                        FROM semantic_release_pointers p
                        JOIN semantic_releases r ON r.release_id = p.release_id AND r.state = 'active'
                        JOIN semantic_documents d ON d.release_id = r.release_id
                        WHERE p.pointer_name = 'active' AND d.embedding IS NOT NULL
                        ORDER BY d.embedding <=> CAST(:embedding AS vector), d.document_id
                        LIMIT :limit
                        """
                    ),
                    {"embedding": vector, "limit": limit},
                )
            ).mappings().all()
        return [
            SemanticDocument(
                document_id=str(row["document_id"]),
                content=str(row["content"]),
                metadata={str(key): str(value) for key, value in dict(row["metadata"]).items()},
            )
            for row in rows
        ]


async def _lock_active_pointer(connection: AsyncConnection) -> str | None:
    """Lock the permanent pointer row before reading or changing its target."""
    await connection.execute(
        text(
            """
            INSERT INTO semantic_release_pointers (pointer_name, release_id)
            VALUES ('active', NULL)
            ON CONFLICT (pointer_name) DO NOTHING
            """
        )
    )
    result = await connection.execute(
        text(
            """
            SELECT release_id::text
            FROM semantic_release_pointers
            WHERE pointer_name = 'active'
            FOR UPDATE
            """
        )
    )
    release_id = result.scalar_one()
    return str(release_id) if release_id is not None else None


def _checksum(documents: tuple[SemanticDocument, ...]) -> str:
    normalized = [
        {
            "document_id": document.document_id,
            "content": document.content,
            "metadata": dict(sorted(document.metadata.items())),
        }
        for document in sorted(documents, key=lambda item: item.document_id)
    ]
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _vector_literal(embedding: Sequence[float] | None) -> str | None:
    if embedding is None:
        return None
    if not embedding:
        raise SemanticReleaseError("semantic embedding must not be empty")
    return "[" + ",".join(str(float(value)) for value in embedding) + "]"
