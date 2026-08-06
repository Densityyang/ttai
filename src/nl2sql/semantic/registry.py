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
class SemanticAssetRecord:
    asset_id: str
    asset_type: str
    status: str
    domain: str
    owner: str
    sensitivity: str
    content: str
    payload: dict[str, Any]
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class SemanticAliasRecord:
    asset_id: str
    alias: str
    normalized_alias: str
    language: str = "und"


@dataclass(frozen=True)
class SemanticEdgeRecord:
    edge_id: str
    source_asset_id: str
    target_asset_id: str
    edge_type: str
    status: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SemanticValidationIssueRecord:
    code: str
    severity: str
    message: str
    asset_id: str | None = None
    path: str = ""
    owner: str = "unassigned"
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class SemanticReleaseCandidate:
    checksum: str
    documents: tuple[SemanticDocument, ...]
    validation_report: dict[str, Any]
    assets: tuple[SemanticAssetRecord, ...] = ()
    aliases: tuple[SemanticAliasRecord, ...] = ()
    edges: tuple[SemanticEdgeRecord, ...] = ()
    validation_issues: tuple[SemanticValidationIssueRecord, ...] = ()
    schema_version: int = 3
    parser_version: str = "legacy-semantic-indexer-v1"
    schema_snapshot_id: str | None = None
    schema_snapshot_checksum: str | None = None
    embedding_profile: str | None = None
    embedding_dimension: int | None = None


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
    schema_version: int = 3
    parser_version: str = "legacy-semantic-indexer-v1"
    schema_snapshot_id: str | None = None
    schema_snapshot_checksum: str | None = None
    embedding_profile: str | None = None
    embedding_dimension: int | None = None


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
        preview = registry.create_draft(documents, change_summary=change_summary)
        embedding_dimension = _embedding_dimension(preview.documents, ())
        embedding_profile = (
            str(validation_report.get("embedding_model", "")).strip()
            if embedding_dimension is not None
            else None
        )
        candidate = SemanticReleaseCandidate(
            checksum=preview.checksum,
            documents=preview.documents,
            validation_report=dict(validation_report),
            embedding_profile=embedding_profile or None,
            embedding_dimension=embedding_dimension,
        )
        return await self.publish_candidate(candidate, change_summary=change_summary)

    async def publish_candidate(
        self,
        candidate: SemanticReleaseCandidate,
        *,
        change_summary: str,
    ) -> SemanticRelease:
        """Persist one validated candidate and atomically activate the complete typed release."""
        _validate_release_candidate(candidate)
        release = SemanticRelease(
            release_id=str(uuid4()),
            version=0,
            checksum=candidate.checksum,
            state=SemanticReleaseState.DRAFT,
            documents=candidate.documents,
            validation_report=None,
            change_summary=change_summary.strip(),
            previous_release_id=None,
            created_at=datetime.now(UTC),
            schema_version=candidate.schema_version,
            parser_version=candidate.parser_version,
            schema_snapshot_id=candidate.schema_snapshot_id,
            schema_snapshot_checksum=candidate.schema_snapshot_checksum,
            embedding_profile=candidate.embedding_profile,
            embedding_dimension=candidate.embedding_dimension,
        )

        async with self._engine.begin() as connection:
            await _validate_schema_snapshot_binding(connection, candidate)
            previous_release_id = await _lock_active_pointer(connection)
            version_result = await connection.execute(
                text("SELECT nextval('semantic_release_version_seq')")
            )
            version = int(version_result.scalar_one())
            release = replace(release, version=version, previous_release_id=previous_release_id)
            await connection.execute(
                text(
                    """
                    INSERT INTO semantic_releases (
                      release_id, version, checksum, state, validation_report,
                      change_summary, previous_release_id, validated_at,
                      schema_version, parser_version, schema_snapshot_id,
                      embedding_profile, embedding_dimension
                    ) VALUES (
                      CAST(:release_id AS uuid), :version, :checksum, 'validated',
                      CAST(:validation_report AS jsonb), :change_summary,
                      CAST(:previous_release_id AS uuid), now(),
                      :schema_version, :parser_version, CAST(:schema_snapshot_id AS uuid),
                      :embedding_profile, :embedding_dimension
                    )
                    """
                ),
                {
                    "release_id": release.release_id,
                    "version": release.version,
                    "checksum": release.checksum,
                    "validation_report": json.dumps(candidate.validation_report),
                    "change_summary": release.change_summary,
                    "previous_release_id": previous_release_id,
                    "schema_version": candidate.schema_version,
                    "parser_version": candidate.parser_version,
                    "schema_snapshot_id": candidate.schema_snapshot_id,
                    "embedding_profile": candidate.embedding_profile,
                    "embedding_dimension": candidate.embedding_dimension,
                },
            )
            await _persist_release_candidate(connection, release.release_id, candidate)
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
            validation_report=candidate.validation_report,
        )

    async def rollback(self, release_id: str) -> SemanticRelease:
        """Atomically move the active pointer to a previously validated release."""
        async with self._engine.begin() as connection:
            active_release_id = await _lock_active_pointer(connection)
            target = (
                await connection.execute(
                    text(
                        """
                        SELECT r.release_id::text, r.version, r.checksum, r.state,
                               r.previous_release_id::text, r.created_at, r.change_summary,
                               r.validation_report, r.schema_version, r.parser_version,
                               r.schema_snapshot_id::text,
                               s.checksum AS schema_snapshot_checksum,
                               r.embedding_profile, r.embedding_dimension
                        FROM semantic_releases r
                        LEFT JOIN schema_snapshots s ON s.snapshot_id = r.schema_snapshot_id
                        WHERE r.release_id = CAST(:release_id AS uuid)
                        FOR UPDATE OF r
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
            schema_version=int(target["schema_version"]),
            parser_version=str(target["parser_version"]),
            schema_snapshot_id=(
                str(target["schema_snapshot_id"])
                if target["schema_snapshot_id"] is not None
                else None
            ),
            schema_snapshot_checksum=(
                str(target["schema_snapshot_checksum"])
                if target["schema_snapshot_checksum"] is not None
                else None
            ),
            embedding_profile=(
                str(target["embedding_profile"])
                if target["embedding_profile"] is not None
                else None
            ),
            embedding_dimension=(
                int(target["embedding_dimension"])
                if target["embedding_dimension"] is not None
                else None
            ),
        )

    async def read_active(self) -> SemanticRelease | None:
        async with self._engine.connect() as connection:
            release_row = (
                await connection.execute(
                    text(
                        """
                        SELECT r.release_id::text, r.version, r.checksum, r.state,
                               r.validation_report, r.change_summary,
                               r.previous_release_id::text, r.created_at,
                               r.schema_version, r.parser_version, r.schema_snapshot_id::text,
                               s.checksum AS schema_snapshot_checksum,
                               r.embedding_profile, r.embedding_dimension
                        FROM semantic_release_pointers p
                        JOIN semantic_releases r ON r.release_id = p.release_id
                        LEFT JOIN schema_snapshots s ON s.snapshot_id = r.schema_snapshot_id
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
            schema_version=int(release_row["schema_version"]),
            parser_version=str(release_row["parser_version"]),
            schema_snapshot_id=(
                str(release_row["schema_snapshot_id"])
                if release_row["schema_snapshot_id"] is not None
                else None
            ),
            schema_snapshot_checksum=(
                str(release_row["schema_snapshot_checksum"])
                if release_row["schema_snapshot_checksum"] is not None
                else None
            ),
            embedding_profile=(
                str(release_row["embedding_profile"])
                if release_row["embedding_profile"] is not None
                else None
            ),
            embedding_dimension=(
                int(release_row["embedding_dimension"])
                if release_row["embedding_dimension"] is not None
                else None
            ),
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


_ASSET_TYPES = {"domain", "metric", "dimension", "relation", "example", "policy", "qa", "view"}
_ASSET_STATUSES = {"active", "retired", "error"}
_EDGE_TYPES = {"approved_join", "metric_dependency", "lineage"}
_EDGE_STATUSES = {"approved", "retired", "rejected"}
_ISSUE_SEVERITIES = {"error", "warning"}


def _validate_release_candidate(candidate: SemanticReleaseCandidate) -> None:
    if candidate.validation_report.get("ok") is not True:
        raise SemanticReleaseError("semantic release validation failed")
    if not candidate.checksum.strip():
        raise SemanticReleaseError("semantic release checksum must not be empty")
    if candidate.schema_version != 3:
        raise SemanticReleaseError("semantic release schema_version must be 3")
    if not candidate.parser_version.strip():
        raise SemanticReleaseError("semantic release parser_version must not be empty")
    snapshot_id = (candidate.schema_snapshot_id or "").strip()
    snapshot_checksum = (candidate.schema_snapshot_checksum or "").strip()
    if bool(snapshot_id) != bool(snapshot_checksum):
        raise SemanticReleaseError("semantic release schema snapshot binding is incomplete")
    if candidate.assets and not snapshot_id:
        raise SemanticReleaseError("typed semantic releases require a validated schema snapshot")

    document_ids = [document.document_id for document in candidate.documents]
    if not document_ids:
        raise SemanticReleaseError("a semantic release requires at least one document")
    if len(document_ids) != len(set(document_ids)):
        raise SemanticReleaseError("semantic document ids must be unique")
    if any(not document.document_id.strip() or not document.content.strip() for document in candidate.documents):
        raise SemanticReleaseError("semantic documents require non-empty ids and content")

    asset_ids = [asset.asset_id for asset in candidate.assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise SemanticReleaseError("semantic asset ids must be unique")
    document_id_set = set(document_ids)
    for asset in candidate.assets:
        if not all(
            value.strip()
            for value in (asset.asset_id, asset.domain, asset.owner, asset.sensitivity, asset.content)
        ):
            raise SemanticReleaseError("semantic assets require complete identity and governance metadata")
        if asset.asset_type not in _ASSET_TYPES:
            raise SemanticReleaseError(f"unsupported semantic asset type: {asset.asset_type}")
        if asset.status not in _ASSET_STATUSES:
            raise SemanticReleaseError(f"unsupported semantic asset status: {asset.status}")
        if asset.asset_id not in document_id_set:
            raise SemanticReleaseError(f"semantic asset is missing its compatibility document: {asset.asset_id}")

    asset_id_set = set(asset_ids)
    normalized_aliases: set[str] = set()
    for alias in candidate.aliases:
        if alias.asset_id not in asset_id_set:
            raise SemanticReleaseError(f"semantic alias references an unknown asset: {alias.asset_id}")
        if not alias.alias.strip() or not alias.normalized_alias.strip():
            raise SemanticReleaseError("semantic aliases must not be empty")
        if alias.normalized_alias in normalized_aliases:
            raise SemanticReleaseError(f"duplicate normalized semantic alias: {alias.normalized_alias}")
        normalized_aliases.add(alias.normalized_alias)

    edge_ids: set[str] = set()
    for edge in candidate.edges:
        if edge.edge_id in edge_ids:
            raise SemanticReleaseError(f"duplicate semantic edge id: {edge.edge_id}")
        edge_ids.add(edge.edge_id)
        if edge.source_asset_id not in asset_id_set or edge.target_asset_id not in asset_id_set:
            raise SemanticReleaseError(f"semantic edge references an unknown asset: {edge.edge_id}")
        if edge.source_asset_id == edge.target_asset_id:
            raise SemanticReleaseError(f"semantic edge must not be self-referential: {edge.edge_id}")
        if edge.edge_type not in _EDGE_TYPES:
            raise SemanticReleaseError(f"unsupported semantic edge type: {edge.edge_type}")
        if edge.status not in _EDGE_STATUSES:
            raise SemanticReleaseError(f"unsupported semantic edge status: {edge.status}")

    for issue in candidate.validation_issues:
        if issue.asset_id is not None and issue.asset_id not in asset_id_set:
            raise SemanticReleaseError(f"semantic issue references an unknown asset: {issue.asset_id}")
        if issue.severity not in _ISSUE_SEVERITIES:
            raise SemanticReleaseError(f"unsupported semantic issue severity: {issue.severity}")
        if not issue.code.strip() or not issue.message.strip() or not issue.owner.strip():
            raise SemanticReleaseError("semantic validation issues require code, message, and owner")

    report_issues = candidate.validation_report.get("issues")
    if isinstance(report_issues, list) and len(report_issues) != len(candidate.validation_issues):
        raise SemanticReleaseError("semantic validation issues do not match the validation report")
    report_schema_version = candidate.validation_report.get("schema_version")
    if report_schema_version is not None and int(report_schema_version) != candidate.schema_version:
        raise SemanticReleaseError("semantic candidate and validation report schema versions differ")

    embedding_dimension = _embedding_dimension(candidate.documents, candidate.assets)
    if embedding_dimension is None:
        if candidate.embedding_profile is not None or candidate.embedding_dimension is not None:
            raise SemanticReleaseError("embedding contract exists without semantic embeddings")
    elif (
        not candidate.embedding_profile
        or candidate.embedding_dimension != embedding_dimension
    ):
        raise SemanticReleaseError("semantic embeddings require one matching model profile and dimension")


async def _validate_schema_snapshot_binding(
    connection: AsyncConnection,
    candidate: SemanticReleaseCandidate,
) -> None:
    if candidate.schema_snapshot_id is None:
        return
    row = (
        await connection.execute(
            text(
                """
                SELECT checksum, state
                FROM schema_snapshots
                WHERE snapshot_id = CAST(:snapshot_id AS uuid)
                FOR SHARE
                """
            ),
            {"snapshot_id": candidate.schema_snapshot_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise SemanticReleaseError("semantic release schema snapshot does not exist")
    if str(row["state"]) != "validated":
        raise SemanticReleaseError("semantic release schema snapshot is not validated")
    if str(row["checksum"]) != candidate.schema_snapshot_checksum:
        raise SemanticReleaseError("semantic release schema snapshot checksum mismatch")


def _embedding_dimension(
    documents: Sequence[SemanticDocument],
    assets: Sequence[SemanticAssetRecord],
) -> int | None:
    vectors = [
        embedding
        for embedding in (
            *(document.embedding for document in documents),
            *(asset.embedding for asset in assets),
        )
        if embedding is not None
    ]
    if not vectors:
        return None
    dimensions = {len(vector) for vector in vectors}
    if 0 in dimensions:
        raise SemanticReleaseError("semantic embedding must not be empty")
    if len(dimensions) != 1:
        raise SemanticReleaseError("semantic embeddings must use one fixed dimension")
    return dimensions.pop()


async def _persist_release_candidate(
    connection: AsyncConnection,
    release_id: str,
    candidate: SemanticReleaseCandidate,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO semantic_documents (
              release_id, document_id, content, metadata, lexical, embedding
            ) VALUES (
              CAST(:release_id AS uuid), :document_id, :content,
              CAST(:metadata AS jsonb), to_tsvector('simple', :content),
              CAST(:embedding AS vector)
            )
            """
        ),
        [
            {
                "release_id": release_id,
                "document_id": document.document_id,
                "content": document.content,
                "metadata": json.dumps(document.metadata),
                "embedding": _vector_literal(document.embedding),
            }
            for document in candidate.documents
        ],
    )
    if candidate.assets:
        await connection.execute(
            text(
                """
                INSERT INTO semantic_assets (
                  release_id, asset_id, asset_type, status, domain,
                  owner, sensitivity, content, payload, embedding
                ) VALUES (
                  CAST(:release_id AS uuid), :asset_id, :asset_type, :status, :domain,
                  :owner, :sensitivity, :content, CAST(:payload AS jsonb),
                  CAST(:embedding AS vector)
                )
                """
            ),
            [
                {
                    "release_id": release_id,
                    "asset_id": asset.asset_id,
                    "asset_type": asset.asset_type,
                    "status": asset.status,
                    "domain": asset.domain,
                    "owner": asset.owner,
                    "sensitivity": asset.sensitivity,
                    "content": asset.content,
                    "payload": json.dumps(asset.payload),
                    "embedding": _vector_literal(asset.embedding),
                }
                for asset in candidate.assets
            ],
        )
    if candidate.aliases:
        await connection.execute(
            text(
                """
                INSERT INTO semantic_aliases (
                  release_id, asset_id, alias, normalized_alias, language
                ) VALUES (
                  CAST(:release_id AS uuid), :asset_id, :alias, :normalized_alias, :language
                )
                """
            ),
            [
                {
                    "release_id": release_id,
                    "asset_id": alias.asset_id,
                    "alias": alias.alias,
                    "normalized_alias": alias.normalized_alias,
                    "language": alias.language,
                }
                for alias in candidate.aliases
            ],
        )
    if candidate.edges:
        await connection.execute(
            text(
                """
                INSERT INTO semantic_edges (
                  release_id, edge_id, source_asset_id, target_asset_id,
                  edge_type, status, payload
                ) VALUES (
                  CAST(:release_id AS uuid), :edge_id, :source_asset_id, :target_asset_id,
                  :edge_type, :status, CAST(:payload AS jsonb)
                )
                """
            ),
            [
                {
                    "release_id": release_id,
                    "edge_id": edge.edge_id,
                    "source_asset_id": edge.source_asset_id,
                    "target_asset_id": edge.target_asset_id,
                    "edge_type": edge.edge_type,
                    "status": edge.status,
                    "payload": json.dumps(edge.payload),
                }
                for edge in candidate.edges
            ],
        )
    if candidate.validation_issues:
        await connection.execute(
            text(
                """
                INSERT INTO semantic_validation_issues (
                  release_id, asset_id, code, severity, message, path, owner, details
                ) VALUES (
                  CAST(:release_id AS uuid), :asset_id, :code, :severity,
                  :message, :path, :owner, CAST(:details AS jsonb)
                )
                """
            ),
            [
                {
                    "release_id": release_id,
                    "asset_id": issue.asset_id,
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "path": issue.path,
                    "owner": issue.owner,
                    "details": json.dumps(issue.details or {}),
                }
                for issue in candidate.validation_issues
            ],
        )


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
