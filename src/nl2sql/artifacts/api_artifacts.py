"""Result-artifact HTTP router: the owner-scoped SAVE surface for results.

MASTER_PR_PLAN_V4.md 5.4.1 puts the typed JSON artifacts, their hashes, their
references and their lifecycle in Control PostgreSQL.  The A3/A4 freeze is the
reason this surface is a SEPARATE router rather than part of the Definition
router: saving a RESULT artifact is an ARTIFACT operation.  It never creates or
materially modifies a reusable definition, never selects BUILD, never
canonicalizes an original object and never executes anything.  A
CustomDefinitionArtifact only POINTS AT an already-existing immutable version.

Route bodies stay thin: strict request validation, server-side authenticated
identity, one repository call, response projection.  The client can never supply
the artifact id, its type, its owner, its schema version or its timestamps: the
strict request contracts below reject every such field with 422 from Pydantic
before any store is touched.

Owner isolation is NOT re-implemented here.  Every read and mutation is
delegated to the selected ArtifactRepository, whose port contract already makes
a foreign caller indistinguishable from a never-existing id (ArtifactNotFound),
and whose two implementations (InMemoryArtifactRepository and
ControlArtifactRepository) both enforce it.  This router only maps that ONE
failure onto ONE stable 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.contracts import (
    AnalysisArtifact,
    ArtifactEnvelope,
    CustomDefinitionArtifact,
)
from src.nl2sql.artifacts.repository import (
    ArtifactNotFound,
    ArtifactRepository,
    ArtifactTypeMismatch,
)
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound
from src.nl2sql.contracts import StrictContract

# The payload union is deliberately NOT discriminated by a client-supplied
# "type" field: the artifact TYPE is a SERVER-owned fact derived from the
# payload's own shape.  The two members have disjoint required fields, so the
# union is unambiguous and an attempt to smuggle an artifact_type is a 422.
ArtifactPayload = AnalysisArtifact | CustomDefinitionArtifact


class CreateArtifactRequest(StrictContract):
    """A result-artifact SAVE.  The SERVER mints the id, type, owner and stamps.

    Only the typed payload and optional, NON-AUTHORITATIVE lineage are accepted.
    thread_id / run_id record provenance for lineage only and grant nothing;
    neither is required and neither makes this a BUILD or an execution.
    """

    payload: ArtifactPayload
    thread_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)


class ReplaceArtifactRequest(StrictContract):
    """Replace the PAYLOAD of one existing artifact, never its type or owner."""

    payload: ArtifactPayload


class ArtifactListResponse(StrictContract):
    """Every artifact OWNED by the authenticated caller, in store order."""

    artifacts: tuple[ArtifactEnvelope, ...]


def owner_identity(auth_user: AuthUser) -> str:
    """The authenticated owner as a STRING, converted ONCE at the boundary.

    AuthUser.user_id is int | str because a Backend token may carry a numeric
    subject.  Every personal store here is keyed by a string identity, so the
    conversion happens at this boundary exactly like the Definition and Library
    routers do it.
    """

    return str(auth_user.user_id)


async def artifact_repository(request: Request) -> ArtifactRepository:
    """The container's application-scoped artifact repository.

    Reuses the EXISTING container.artifact_repository() accessor, which already
    selects the Control-PG or the process-local implementation from the
    immutable product_store_backend setting.  No second store is built here.
    """

    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "artifact_repository"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    repository: ArtifactRepository = await container.artifact_repository()
    return repository


def definition_service(request: Request) -> CustomDefinitionService:
    """The container's application-scoped definition service (READ-only use)."""

    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "custom_definition_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    service: CustomDefinitionService = container.custom_definition_service()
    return service


def _artifact_not_found() -> HTTPException:
    """ONE stable response for a foreign AND an absent artifact id."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="artifact_not_found"
    )


def _definition_version_not_found() -> HTTPException:
    """ONE stable response for a foreign AND an absent referenced version.

    A foreign definition raises the SAME DefinitionNotFound the owner-scoped
    reader raises for an absent one, so this route is never an existence oracle.
    """

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="artifact_definition_version_not_found",
    )


def _definition_checksum_mismatch() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="artifact_definition_checksum_mismatch",
    )


async def _verify_definition_reference(
    request: Request,
    *,
    owner_user_id: str,
    payload: ArtifactPayload,
) -> None:
    """Prove a CustomDefinitionArtifact names an EXISTING exact version.

    READ ONLY by construction: it resolves the version through the SAME
    owner-scoped reader every other definition surface uses, so a foreign and an
    absent version are indistinguishable.  It creates NO definition and NO
    version, mutates NO axis, and never reaches the execution service.
    """

    if not isinstance(payload, CustomDefinitionArtifact):
        return
    service = definition_service(request)
    try:
        exact = await service.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=payload.definition_id,
            version=payload.definition_version,
        )
    except DefinitionNotFound as exc:
        raise _definition_version_not_found() from exc
    # The artifact must name the version it CLAIMS to point at.  A mismatched
    # checksum would be a reference to an object that does not exist.
    if exact.checksum != payload.definition_checksum:
        raise _definition_checksum_mismatch()


def register_artifact_routes(app: Any) -> None:
    """Register the owner-scoped result-artifact endpoints."""

    router = APIRouter(prefix="/api/v2/nl2sql/artifacts", tags=["nl2sql-v2-artifacts"])

    @router.post("", response_model=ArtifactEnvelope)
    async def create_artifact(
        request: Request,
        body: CreateArtifactRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ArtifactEnvelope:
        owner_user_id = owner_identity(auth_user)
        # Validate the reference BEFORE the write, so a rejected reference leaves
        # ZERO state behind (no artifact row, no definition, no version).
        await _verify_definition_reference(
            request, owner_user_id=owner_user_id, payload=body.payload
        )
        repository = await artifact_repository(request)
        return await repository.create(
            owner_user_id=owner_user_id,
            payload=body.payload,
            thread_id=body.thread_id,
            run_id=body.run_id,
        )

    @router.get("", response_model=ArtifactListResponse)
    async def list_artifacts(
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ArtifactListResponse:
        repository = await artifact_repository(request)
        owned = await repository.list_for_owner(
            owner_user_id=owner_identity(auth_user)
        )
        return ArtifactListResponse(artifacts=owned)

    @router.get("/{artifact_id}", response_model=ArtifactEnvelope)
    async def get_artifact(
        artifact_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ArtifactEnvelope:
        repository = await artifact_repository(request)
        try:
            return await repository.get(
                owner_user_id=owner_identity(auth_user), artifact_id=artifact_id
            )
        except ArtifactNotFound as exc:
            raise _artifact_not_found() from exc

    @router.put("/{artifact_id}", response_model=ArtifactEnvelope)
    async def replace_artifact(
        artifact_id: str,
        request: Request,
        body: ReplaceArtifactRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ArtifactEnvelope:
        owner_user_id = owner_identity(auth_user)
        await _verify_definition_reference(
            request, owner_user_id=owner_user_id, payload=body.payload
        )
        repository = await artifact_repository(request)
        try:
            return await repository.replace_payload(
                owner_user_id=owner_user_id,
                artifact_id=artifact_id,
                payload=body.payload,
            )
        except ArtifactNotFound as exc:
            raise _artifact_not_found() from exc
        except ArtifactTypeMismatch as exc:
            # The artifact TYPE is immutable: a payload that would change it is
            # refused instead of leaving an envelope that contradicts itself.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="artifact_type_immutable",
            ) from exc

    app.include_router(router)


__all__ = [
    "ArtifactListResponse",
    "CreateArtifactRequest",
    "ReplaceArtifactRequest",
    "artifact_repository",
    "owner_identity",
    "register_artifact_routes",
]
