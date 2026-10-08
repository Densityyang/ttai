"""Artifact repository port: owner-scoped and fail-closed.

The bundled implementation is a process-local implementation of the swappable
port; a Control-PG implementation is selected by configuration.

Every read/mutation resolves the owner from the AUTHENTICATED identity passed in
by the caller (the request context), never from a request field.  A cross-user
access returns the SAME failure as a never-existing id, so there is no existence
oracle.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import uuid4

from src.nl2sql.artifacts.contracts import (
    AnalysisArtifact,
    ArtifactEnvelope,
    CustomDefinitionArtifact,
    utcnow,
)


class ArtifactTypeMismatch(ValueError):
    """A payload replacement would change the artifact's TYPE."""

    def __init__(self) -> None:
        super().__init__("artifact_type_immutable")


class ArtifactNotFound(LookupError):
    """The artifact does not exist FOR THIS CALLER.

    Deliberately indistinguishable between "never existed" and "belongs to
    someone else": an existence oracle would leak other users' data.
    """

    def __init__(self) -> None:
        super().__init__("artifact_not_found")


def new_artifact_id() -> str:
    """Server-minted, opaque, not derived from any client input."""
    return "art_" + uuid4().hex


@runtime_checkable
class ArtifactRepository(Protocol):
    async def create(
        self,
        *,
        owner_user_id: str,
        payload: AnalysisArtifact | CustomDefinitionArtifact,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactEnvelope: ...

    async def get(
        self, *, owner_user_id: str, artifact_id: str
    ) -> ArtifactEnvelope: ...

    async def list_for_owner(
        self, *, owner_user_id: str
    ) -> tuple[ArtifactEnvelope, ...]: ...

    async def replace_payload(
        self,
        *,
        owner_user_id: str,
        artifact_id: str,
        payload: AnalysisArtifact | CustomDefinitionArtifact,
    ) -> ArtifactEnvelope: ...


class InMemoryArtifactRepository:
    """Process-local, in-memory implementation of the artifact port.

    Per-instance state only (no module/class-level dict), so tests cannot share
    state and a foreign caller can never observe another user's artifacts.
    """

    def __init__(self) -> None:
        self._records: dict[str, ArtifactEnvelope] = {}

    async def ping(self) -> None:
        """Readiness probe.  A process-local store is trivially reachable."""


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
        artifact_type = (
            "analysis"
            if isinstance(payload, AnalysisArtifact)
            else "custom_definition"
        )
        now = utcnow()
        record = ArtifactEnvelope(
            artifact_id=new_artifact_id(),
            artifact_type=artifact_type,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
            created_at=now,
            updated_at=now,
            payload=payload,
        )
        self._records[record.artifact_id] = record
        return record

    async def get(
        self, *, owner_user_id: str, artifact_id: str
    ) -> ArtifactEnvelope:
        record = self._records.get(artifact_id)
        if record is None or record.owner_user_id != owner_user_id:
            # Identical failure for "absent" and "not yours".
            raise ArtifactNotFound()
        return record

    async def list_for_owner(
        self, *, owner_user_id: str
    ) -> tuple[ArtifactEnvelope, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.owner_user_id == owner_user_id
        )

    async def replace_payload(
        self,
        *,
        owner_user_id: str,
        artifact_id: str,
        payload: AnalysisArtifact | CustomDefinitionArtifact,
    ) -> ArtifactEnvelope:
        current = await self.get(
            owner_user_id=owner_user_id, artifact_id=artifact_id
        )
        # The artifact TYPE is immutable: replacing an analysis payload with a
        # custom-definition payload (or vice versa) would leave an envelope whose
        # declared type contradicts its payload.
        incoming_type = (
            "analysis" if isinstance(payload, AnalysisArtifact) else "custom_definition"
        )
        if incoming_type != current.artifact_type:
            raise ArtifactTypeMismatch()
        updated = current.model_copy(
            update={"payload": payload, "updated_at": utcnow()}
        )
        self._records[artifact_id] = updated
        return updated


__all__ = [
    "ArtifactNotFound",
    "ArtifactTypeMismatch",
    "ArtifactRepository",
    "InMemoryArtifactRepository",
    "new_artifact_id",
]
