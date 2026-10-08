"""Definition / Publication HTTP router (10 product endpoints).

Route bodies stay THIN: request validation, server-side authenticated identity,
service call, response projection.  All lifecycle policy lives in
CustomDefinitionService / PublicationService.

The client may never supply owner identity, authority, scope, lifecycle axes,
publication state, certification or revision pointers: the strict request
contracts below reject every such field with 422 from Pydantic.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, model_validator

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.build_run import require_build_run
from src.nl2sql.artifacts.custom_definition_execution_service import (
    CalculationInputResolverUnavailable,
    CustomDefinitionExecutionService,
)
from src.nl2sql.artifacts.publication_service import (
    PublicationConflict,
    PublicationNotEligible,
    PublicationService,
)
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound
from src.nl2sql.contracts import StrictContract, TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    CalculationInputProvenance,
    CustomCalculationExecutionError,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
)


class CreateDefinitionRequest(StrictContract):
    """A private Definition create.  ParameterContract is DERIVED server-side."""

    title: str = Field(min_length=1, max_length=256)
    calculation: CalculationSpec


class UpdateDraftRequest(StrictContract):
    """A DRAFT edit.  There is deliberately NO client-editable contract field."""

    title: str | None = Field(default=None, min_length=1, max_length=256)
    calculation: CalculationSpec | None = None


class ExecuteDefinitionRequest(StrictContract):
    """Concrete per-run parameter VALUES for ONE exact version.

    The version identity comes from the PATH, never from the body, so a caller
    cannot redirect a run at a different definition or version.  Only the
    nested per-run VALUES are accepted here.
    """

    binding: CalculationExecutionBinding
    execution_context: DefinitionExecutionContext = Field(
        default_factory=DefinitionExecutionContext
    )
    # Accept the compact top-level spelling as well; both spellings normalize to
    # the same typed context and no other authority fields are admitted.
    date_mode: Literal["latest_authoritative", "exact_date"] | None = None
    exact_date: date | None = None

    @model_validator(mode="after")
    def normalize_execution_context(self) -> "ExecuteDefinitionRequest":
        if self.date_mode is None and self.exact_date is None:
            return self
        context = DefinitionExecutionContext(
            date_mode=self.date_mode or "latest_authoritative",
            exact_date=self.exact_date,
        )
        if (
            self.execution_context != DefinitionExecutionContext()
            and self.execution_context != context
        ):
            raise ValueError("execution context has conflicting values")
        object.__setattr__(self, "execution_context", context)
        return self


class DefinitionVersionView(StrictContract):
    version: int
    title: str
    calculation: CalculationSpec
    calculation_checksum: str
    parameter_contract_checksum: str
    semantic_closed: bool
    checksum: str
    confirmation: str
    retention: str
    derived_from_definition_id: str | None = None
    derived_from_version: int | None = None
    # Catalogue state for THIS EXACT version only; never inferred from the
    # current Definition axes.
    publication: str = "UNPUBLISHED"
    certification: str = "UNCERTIFIED"
    withdrawn: bool = False


class DefinitionView(StrictContract):
    definition_id: str
    owned_by_current_user: bool = True
    current_version: int
    title: str
    calculation: CalculationSpec
    parameter_contract_parameters: tuple[str, ...]
    semantic_closed: bool
    checksum: str
    confirmation: str
    retention: str
    publication: str
    certification: str
    derived_from_definition_id: str | None = None
    derived_from_version: int | None = None


class DefinitionListResponse(StrictContract):
    definitions: tuple[DefinitionView, ...]


class ExecuteDefinitionResponse(StrictContract):
    """Executed reusable definition result from server-resolved inputs."""

    status: Literal["executed"] = "executed"
    definition_id: str
    version: int
    definition_checksum: str
    calculation_id: str
    spec_checksum: str
    binding_checksum: str
    value: Decimal
    unit: str
    input_provenance: tuple[CalculationInputProvenance, ...]
    data_as_of: datetime | None = None
    time_range: TimeRange | None = None
    calculation_scope: Literal["reusable_custom_definition"]


def owner_identity(auth_user: AuthUser) -> str:
    """The authenticated owner as a STRING.

    ``AuthUser.user_id`` is ``int | str`` because a Backend token may carry a
    numeric subject.  Every personal store here is keyed by a string identity, so
    the conversion happens ONCE at this boundary rather than being guessed deeper.
    """

    return str(auth_user.user_id)


def definition_service(request: Request) -> CustomDefinitionService:
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "custom_definition_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    service: CustomDefinitionService = container.custom_definition_service()
    return service


def definition_execution_service(request: Request) -> CustomDefinitionExecutionService:
    container = getattr(request.app.state, "container", None)
    accessor = getattr(container, "custom_definition_execution_service", None)
    if not callable(accessor):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="calculation_input_resolver_unavailable",
        )
    return cast(CustomDefinitionExecutionService, accessor())


async def publication_service(request: Request) -> PublicationService:
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "publication_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    return await container.publication_service()


def _not_found() -> HTTPException:
    """ONE stable response for foreign AND absent (no existence oracle)."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="definition_not_found"
    )


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)


def _definition_view(definition: Any) -> DefinitionView:
    axes = definition.axes
    current = definition.current_version
    return DefinitionView(
        definition_id=definition.definition_id,
        current_version=current.version,
        title=current.title,
        calculation=current.calculation,
        parameter_contract_parameters=tuple(
            parameter.name for parameter in current.parameter_contract.parameters
        ),
        semantic_closed=current.semantic_closed,
        checksum=current.checksum,
        confirmation=axes.confirmation,
        retention=axes.retention,
        publication=axes.publication,
        certification=axes.certification,
        derived_from_definition_id=current.derived_from_definition_id,
        derived_from_version=current.derived_from_version,
    )


async def _exact_catalogue_state(
    request: Request, *, identity_id: str, version: int
) -> tuple[str, str, bool]:
    """Publication state of THIS EXACT version, never the current axes."""

    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "publication_catalogue"):
        return ("UNPUBLISHED", "UNCERTIFIED", False)
    catalogue = await container.publication_catalogue()
    if await catalogue.get(identity_id, version) is None:
        return ("UNPUBLISHED", "UNCERTIFIED", False)
    certification = await catalogue.certification_state(identity_id, version)
    return (
        "PUBLISHED",
        "CERTIFIED" if certification == "certified" else "UNCERTIFIED",
        bool(await catalogue.is_withdrawn(identity_id, version)),
    )


def register_definition_routes(app: Any) -> None:
    """Register the 10 Definition/Publication product endpoints."""

    router = APIRouter(prefix="/api/v2/nl2sql", tags=["nl2sql-v2-definitions"])

    @router.post("/definitions", response_model=DefinitionView)
    async def create_definition(
        request: Request,
        body: CreateDefinitionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        draft = await service.create_draft(
            owner_user_id=owner_identity(auth_user),
            title=body.title,
            calculation=body.calculation,
        )
        definition = await service.get_owned_definition(
            owner_user_id=owner_identity(auth_user), definition_id=draft.definition_id
        )
        return _definition_view(definition)

    @router.get("/definitions", response_model=DefinitionListResponse)
    async def list_definitions(
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionListResponse:
        service = definition_service(request)
        owned = await service.list_owned(owner_user_id=owner_identity(auth_user))
        return DefinitionListResponse(
            definitions=tuple(_definition_view(item) for item in owned)
        )

    @router.get(
        "/definitions/{definition_id}/versions/{version}",
        response_model=DefinitionVersionView,
    )
    async def get_definition_version(
        definition_id: str,
        version: int,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionVersionView:
        service = definition_service(request)
        try:
            exact = await service.get_exact_version(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
            lifecycle = await service.get_version_lifecycle(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        publication, certification, withdrawn = await _exact_catalogue_state(
            request, identity_id=definition_id, version=version
        )
        return DefinitionVersionView(
            version=exact.version,
            title=exact.title,
            calculation=exact.calculation,
            calculation_checksum=exact.calculation.checksum,
            parameter_contract_checksum=exact.parameter_contract.checksum,
            semantic_closed=exact.semantic_closed,
            checksum=exact.checksum,
            confirmation=lifecycle.confirmation,
            retention=lifecycle.retention,
            derived_from_definition_id=exact.derived_from_definition_id,
            derived_from_version=exact.derived_from_version,
            publication=publication,
            certification=certification,
            withdrawn=withdrawn,
        )

    @router.patch("/definitions/{definition_id}/draft", response_model=DefinitionView)
    async def update_definition_draft(
        definition_id: str,
        request: Request,
        body: UpdateDraftRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            await service.update_draft(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                calculation=body.calculation,
                title=body.title,
            )
            definition = await service.get_owned_definition(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("definition_not_mutable") from exc
        return _definition_view(definition)

    @router.post(
        "/definitions/{definition_id}/semantic-close",
        response_model=DefinitionView,
    )
    async def semantic_close(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            await service.mark_semantic_closed(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
            definition = await service.get_owned_definition(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("semantic_closure_rejected") from exc
        return _definition_view(definition)

    @router.post("/definitions/{definition_id}/confirm", response_model=DefinitionView)
    async def confirm_definition(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            definition = await service.confirm(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("definition_lifecycle_invalid") from exc
        return _definition_view(definition)

    @router.post("/definitions/{definition_id}/save", response_model=DefinitionView)
    async def save_definition(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            definition = await service.save(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("definition_lifecycle_invalid") from exc
        return _definition_view(definition)

    @router.post(
        "/definitions/{definition_id}/revisions",
        response_model=DefinitionView,
    )
    async def create_definition_revision(
        definition_id: str,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        try:
            await service.create_revision(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
            definition = await service.get_owned_definition(
                owner_user_id=owner_identity(auth_user), definition_id=definition_id
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except ValueError as exc:
            raise _conflict("revision_lifecycle_invalid") from exc
        return _definition_view(definition)

    @router.post(
        "/definitions/{definition_id}/versions/{version}/publish",
        response_model=DefinitionVersionView,
    )
    async def publish_definition_version(
        definition_id: str,
        version: int,
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> DefinitionVersionView:
        await require_build_run(request, auth_user)
        service = definition_service(request)
        publications = await publication_service(request)
        try:
            await publications.publish(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
            exact = await service.get_exact_version(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
            lifecycle = await service.get_version_lifecycle(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except PublicationConflict as exc:
            raise _conflict(exc.reason) from exc
        except PublicationNotEligible as exc:
            raise _conflict(exc.reason) from exc
        publication, certification, withdrawn = await _exact_catalogue_state(
            request, identity_id=definition_id, version=version
        )
        return DefinitionVersionView(
            version=exact.version,
            title=exact.title,
            calculation=exact.calculation,
            calculation_checksum=exact.calculation.checksum,
            parameter_contract_checksum=exact.parameter_contract.checksum,
            semantic_closed=exact.semantic_closed,
            checksum=exact.checksum,
            confirmation=lifecycle.confirmation,
            retention=lifecycle.retention,
            derived_from_definition_id=exact.derived_from_definition_id,
            derived_from_version=exact.derived_from_version,
            publication=publication,
            certification=certification,
            withdrawn=withdrawn,
        )

    @router.post(
        "/definitions/{definition_id}/versions/{version}/execute",
        response_model=ExecuteDefinitionResponse,
    )
    async def execute_definition_version(
        definition_id: str,
        version: int,
        request: Request,
        body: ExecuteDefinitionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ExecuteDefinitionResponse:
        try:
            outcome = await definition_execution_service(request).execute(
                owner_user_id=owner_identity(auth_user),
                definition_id=definition_id,
                version=version,
                binding=body.binding,
                execution_context=body.execution_context,
            )
        except DefinitionNotFound as exc:
            raise _not_found() from exc
        except CalculationInputResolverUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=exc.code,
            ) from exc
        except CustomCalculationExecutionError as exc:
            raise _conflict(exc.code) from exc
        except ValueError as exc:
            raise _conflict("execution_binding_invalid") from exc
        result = outcome.result
        return ExecuteDefinitionResponse(
            definition_id=outcome.definition_id,
            version=outcome.version,
            definition_checksum=outcome.definition_checksum,
            calculation_id=result.calculation_id,
            spec_checksum=result.spec_checksum,
            binding_checksum=result.binding_checksum,
            value=result.value,
            unit=result.unit,
            input_provenance=result.input_provenance,
            data_as_of=result.data_as_of,
            time_range=result.time_range,
            calculation_scope=result.calculation_scope,
        )

    app.include_router(router)


__all__ = [
    "CreateDefinitionRequest",
    "DefinitionListResponse",
    "DefinitionVersionView",
    "DefinitionView",
    "ExecuteDefinitionRequest",
    "ExecuteDefinitionResponse",
    "UpdateDraftRequest",
    "register_definition_routes",
]
