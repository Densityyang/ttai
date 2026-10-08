"""Artifact contracts: typed, owner-scoped, process-local (DEMO/local only).

Foundation B deliberately ships a NON-DURABLE repository.  Nothing here claims
production persistence; a future Control-PG repository must be swappable behind
the same Protocol.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ArtifactType = Literal["analysis", "custom_definition"]
ArtifactId = Annotated[str, Field(pattern=r"^art_[0-9a-f]{32}$")]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OwnerId = Annotated[str, Field(min_length=1, max_length=256)]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class AnalysisArtifact(_StrictFrozenModel):
    """A run-scoped, NONCANONICAL analysis result."""

    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=4096)
    # Run-scoped and noncanonical by construction: an analysis artifact never
    # carries reusable business semantics.
    noncanonical: Literal[True] = True


class CustomDefinitionArtifact(_StrictFrozenModel):
    """Points at an immutable definition version (the version holds semantics)."""

    definition_id: str = Field(min_length=1, max_length=128)
    definition_version: int = Field(ge=1)
    definition_checksum: Checksum


class ArtifactEnvelope(_StrictFrozenModel):
    """Owner-scoped artifact record.  The SERVER mints the id and stamps owner.

    owner_user_id is resolved from the authenticated identity at creation and
    is never accepted from a request.  Conversation/run provenance is recorded
    for lineage only and grants nothing.
    """

    schema_version: Literal["1.0"] = "1.0"
    artifact_id: ArtifactId
    artifact_type: ArtifactType
    owner_user_id: OwnerId
    thread_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    created_at: datetime
    updated_at: datetime
    payload: AnalysisArtifact | CustomDefinitionArtifact

    @property
    def payload_checksum(self) -> str:
        return _checksum(self.payload.model_dump(mode="json"))


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "AnalysisArtifact",
    "ArtifactEnvelope",
    "ArtifactId",
    "ArtifactType",
    "CustomDefinitionArtifact",
    "OwnerId",
    "utcnow",
]
