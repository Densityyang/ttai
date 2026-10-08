"""Owner-scoped personal semantic-conflict HTTP surface."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import Field

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.personal_conflict_product_service import (
    PersonalConflictProductError,
    PersonalConflictProductService,
)
from src.nl2sql.artifacts.service import DefinitionNotFound
from src.nl2sql.contracts import StrictContract
from src.nl2sql.semantic.calculation_contract import SemanticResolution
from src.nl2sql.semantic.personal_conflict_contract import (
    PersonalSelection,
    SemanticConflict,
)
from src.nl2sql.semantic.personal_conflict_service import PersonalConflictProjection
from src.nl2sql.supervisor.schemas import (
    ConflictCandidateBlock,
    ConflictComparisonBlock,
    ConflictLineageBlock,
)


class PersonalConflictResponse(StrictContract):
    resolution: SemanticResolution
    semantic_conflict: SemanticConflict | None = None
    conflict_comparison: ConflictComparisonBlock | None = None
    selected_candidate_id: str | None = None
    selected_definition_id: str | None = None
    selected_version: int | None = None


class PersonalSelectionRequest(StrictContract):
    thread_id: UUID
    run_id: str = Field(min_length=1, max_length=64)
    own_version: int | None = Field(default=None, ge=1)
    own_definition_id: str
    installed_identity_id: str
    selection: PersonalSelection


class PersonalSelectionResponse(StrictContract):
    valid: bool = True
    selection: PersonalSelection
    conflict_comparison: ConflictComparisonBlock


async def _service(request: Request) -> PersonalConflictProductService:
    container = getattr(request.app.state, "container", None)
    accessor = getattr(container, "personal_conflict_product_service", None)
    if not callable(accessor):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    return cast(
        PersonalConflictProductService, await cast(Awaitable[Any], accessor())
    )


def _owner(auth_user: AuthUser) -> str:
    return str(auth_user.user_id)


async def _require_run(
    request: Request, auth_user: AuthUser, thread_id: UUID, run_id: str
) -> None:
    container = getattr(request.app.state, "container", None)
    get_engine = getattr(container, "get_engine", None)
    if not callable(get_engine):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    from src.nl2sql.contracts import RequestContext, RequestIdentity
    from src.nl2sql.orchestration.run_lineage import (
        RunLineageInvalid,
        require_current_owned_run,
    )

    context = RequestContext(
        identity=RequestIdentity(
            request_id=UUID(int=0),
            user_id=_owner(auth_user),
            roles=frozenset(auth_user.roles),
            permissions=frozenset(auth_user.permissions),
        ),
        thread_id=thread_id,
        trace_id=f"conflict:{thread_id}:{run_id}",
    )
    try:
        await require_current_owned_run(
            await cast(Awaitable[Any], get_engine()),
            context=context,
            expected_run_id=run_id,
        )
    except RunLineageInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="personal_conflict_run_binding_invalid",
        ) from exc


def _comparison_block(
    projection: PersonalConflictProjection,
) -> ConflictComparisonBlock | None:
    conflict = projection.semantic_conflict
    comparison = projection.conflict_comparison
    if conflict is None or comparison is None:
        return None
    references = {item.candidate_id: item for item in conflict.candidates}
    rows: list[ConflictCandidateBlock] = []
    for candidate in comparison.candidates:
        reference = references[candidate.candidate_id]
        risk_notes = tuple(
            difference.risk_note
            for difference in conflict.differences
            if candidate.candidate_id in difference.candidate_ids
            and difference.risk_note is not None
        )
        rows.append(
            ConflictCandidateBlock(
                candidate_id=candidate.candidate_id,
                definition_id=reference.definition_id,
                version=reference.definition_version,
                display_name=candidate.display_name,
                origin=candidate.origin,
                owner_label=candidate.owner_label,
                source_label=candidate.source_label,
                certification_state=candidate.certification_state,
                star_count=candidate.star_count,
                semantic_difference_kinds=candidate.material_difference_kinds,
                lineage=ConflictLineageBlock(
                    **candidate.lineage.model_dump(mode="json")
                ),
                risk_notes=tuple(dict.fromkeys(risk_notes)),
            )
        )
    return ConflictComparisonBlock(
        conflict_id=comparison.conflict_id,
        required_slot=comparison.required_slot,
        candidates=tuple(rows),
    )


def _resolve_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DefinitionNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="personal_conflict_candidate_not_found",
        )
    if isinstance(exc, PersonalConflictProductError):
        code = exc.code
    else:
        code = "personal_conflict_invalid"
    status_code = (
        status.HTTP_404_NOT_FOUND
        if code
        in {
            "personal_conflict_install_required",
            "personal_conflict_publication_not_found",
        }
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=status_code, detail=code)


def register_conflict_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/api/v2/nl2sql/conflicts", tags=["nl2sql-v2-conflicts"])

    @router.get("/personal", response_model=PersonalConflictResponse)
    async def personal_conflict(
        request: Request,
        own_definition_id: str = Query(min_length=1, max_length=128),
        installed_identity_id: str = Query(min_length=1, max_length=128),
        own_version: int | None = Query(default=None, ge=1),
        auth_user: AuthUser = Depends(require_nl2sql_permission),
        thread_id: UUID = Query(...),
        run_id: str = Query(min_length=1, max_length=64),
    ) -> PersonalConflictResponse:
        await _require_run(request, auth_user, thread_id, run_id)
        service = await _service(request)
        try:
            projection = await service.resolve(
                user_id=_owner(auth_user),
                own_definition_id=own_definition_id,
                installed_identity_id=installed_identity_id,
                thread_id=str(thread_id),
                run_id=run_id,
                own_version=own_version,
            )
        except (DefinitionNotFound, PersonalConflictProductError) as exc:
            raise _resolve_error(exc) from exc
        return PersonalConflictResponse(
            resolution=projection.resolution,
            semantic_conflict=projection.semantic_conflict,
            conflict_comparison=_comparison_block(projection),
            selected_candidate_id=projection.selected_candidate_id,
            selected_definition_id=projection.selected_definition_id,
            selected_version=projection.selected_version,
        )

    @router.post("/personal/select", response_model=PersonalSelectionResponse)
    async def select_personal_conflict(
        request: Request,
        body: PersonalSelectionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> PersonalSelectionResponse:
        await _require_run(request, auth_user, body.thread_id, body.run_id)
        service = await _service(request)
        try:
            projection, selection = await service.validate_selection(
                user_id=_owner(auth_user),
                own_definition_id=body.own_definition_id,
                installed_identity_id=body.installed_identity_id,
                selection=body.selection,
                thread_id=str(body.thread_id),
                run_id=body.run_id,
                own_version=body.own_version,
            )
        except (DefinitionNotFound, PersonalConflictProductError) as exc:
            raise _resolve_error(exc) from exc
        block = _comparison_block(projection)
        if block is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="personal_conflict_not_active",
            )
        return PersonalSelectionResponse(selection=selection, conflict_comparison=block)

    app.include_router(router)


__all__ = [
    "PersonalConflictResponse",
    "PersonalSelectionRequest",
    "PersonalSelectionResponse",
    "register_conflict_routes",
]
