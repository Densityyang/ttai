"""Exploration-confirmation HTTP router: the owner-scoped, run-scoped surface.

§8.16 P7B requires that "an exploration confirmation never stands in for a
definition confirmation".  That invariant is only PRODUCT-REACHABLE if a real
route records an exploration confirmation and the definition then STILL refuses
``save()``.  This router is that route: it mounts next to the Definition and
Artifact routers and records a run-scoped EXPLORATION confirmation.

Route bodies stay thin: strict request parsing, server-side authenticated
identity, one service call, response projection.  The client can NEVER supply
``confirmed_by``, ``confirmed_at`` or ``exploration_id``: those are
server-owned, and a payload that tries to inject any of them (or any other
identity/authority spelling) is refused with the STABLE typed code
``exploration_identity_is_server_owned`` BEFORE any store is touched.

Owner isolation reuses the repo-wide convention: a foreign run / record is
indistinguishable from a never-existing one (the SAME 404, never a 403), so this
surface is never an existence oracle.

This module holds NO definition mutation capability: it injects the container's
read-only definition reader through the exploration service, which itself is
typed against the narrow ``DefinitionExactVersionReader`` protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import ConfigDict, ValidationError

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.exploration_confirmation import (
    EXPLORATION_CONFIRMATION_INVALID,
    EXPLORATION_CONFIRMATION_NOT_FOUND,
    EXPLORATION_IDENTITY_IS_SERVER_OWNED,
    ExplorationConfirmation,
    ExplorationConfirmationNotFound,
    ExplorationConfirmationService,
    ExplorationDefinitionReference,
    ExplorationIdentityInjection,
)
from src.nl2sql.artifacts.service import DefinitionNotFound
from src.nl2sql.contracts import StrictContract

# The ONE stable response for a foreign AND an absent definition reference.
EXPLORATION_DEFINITION_REFERENCE_NOT_FOUND = (
    "exploration_definition_reference_not_found"
)


class ExplorationConfirmationView(StrictContract):
    """ONE server-owned exploration confirmation, as exposed on the wire.

    It mirrors the stored record field-for-field.  ``replaces_definition_confirmation``
    is the literal ``False``, so a caller cannot read an exploration
    confirmation as a definition confirmation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    exploration_id: str
    run_id: str
    subject: str
    confirmed_by: str
    confirmed_at: datetime
    definition_reference: ExplorationDefinitionReference | None = None
    replaces_definition_confirmation: Literal[False] = False


class ExplorationConfirmationListResponse(StrictContract):
    """Every confirmation the AUTHENTICATED caller recorded for ONE run."""

    run_id: str
    confirmations: tuple[ExplorationConfirmationView, ...]


def owner_identity(auth_user: AuthUser) -> str:
    """The authenticated owner as a STRING, converted ONCE at the boundary."""

    return str(auth_user.user_id)


def exploration_confirmation_service(
    request: Request,
) -> ExplorationConfirmationService:
    """The container's application-scoped exploration-confirmation service.

    The service is constructed by the container with a READ-ONLY definition
    reader, so this router can never reach a definition mutation through it.
    """

    container = getattr(request.app.state, "container", None)
    accessor = getattr(container, "exploration_confirmation_service", None)
    if not callable(accessor):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    return cast(ExplorationConfirmationService, accessor())


def _view(record: ExplorationConfirmation) -> ExplorationConfirmationView:
    return ExplorationConfirmationView(
        schema_version=record.schema_version,
        exploration_id=record.exploration_id,
        run_id=record.run_id,
        subject=record.subject,
        confirmed_by=record.confirmed_by,
        confirmed_at=record.confirmed_at,
        definition_reference=record.definition_reference,
        replaces_definition_confirmation=record.replaces_definition_confirmation,
    )


def _identity_injection_refusal(
    error: ExplorationIdentityInjection,
) -> HTTPException:
    """ONE stable response for a client that tried to own a server field."""

    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": EXPLORATION_IDENTITY_IS_SERVER_OWNED,
            "fields": list(error.fields),
        },
    )


def _invalid_payload_refusal(error: ValidationError) -> HTTPException:
    """ONE stable response for a structurally invalid exploration payload."""

    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": EXPLORATION_CONFIRMATION_INVALID,
            "fields": [
                str(part) for detail in error.errors() for part in detail["loc"]
            ],
        },
    )


def _confirmation_not_found() -> HTTPException:
    """ONE stable response for a foreign AND an absent exploration record."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=EXPLORATION_CONFIRMATION_NOT_FOUND,
    )


def _definition_reference_not_found() -> HTTPException:
    """ONE stable response for a foreign AND an absent referenced version."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=EXPLORATION_DEFINITION_REFERENCE_NOT_FOUND,
    )


def register_exploration_confirmation_routes(app: Any) -> None:
    """Register the owner-scoped, run-scoped exploration-confirmation endpoints."""

    router = APIRouter(
        prefix="/api/v2/nl2sql/exploration-confirmations",
        tags=["nl2sql-v2-exploration-confirmations"],
    )

    @router.post(
        "",
        response_model=ExplorationConfirmationView,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["run_id", "subject"],
                            "properties": {
                                "run_id": {"type": "string"},
                                "subject": {"type": "string"},
                                "definition_id": {"type": "string"},
                                "version": {"type": "integer"},
                            },
                        }
                    }
                },
            }
        },
    )
    async def record_exploration_confirmation(
        request: Request,
        # The raw mapping is parsed by the STRICT exploration contract inside the
        # service, so an injected server-owned field is refused with the STABLE
        # typed code instead of FastAPI's generic 422 error array.
        body: Annotated[dict[str, Any], Body()],
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ExplorationConfirmationView:
        service = exploration_confirmation_service(request)
        try:
            record = await service.confirm_from_client_payload(
                owner_user_id=owner_identity(auth_user), payload=body
            )
        except ExplorationIdentityInjection as exc:
            raise _identity_injection_refusal(exc) from exc
        except ValidationError as exc:
            raise _invalid_payload_refusal(exc) from exc
        except DefinitionNotFound as exc:
            raise _definition_reference_not_found() from exc
        return _view(record)

    @router.get("", response_model=ExplorationConfirmationListResponse)
    async def list_exploration_confirmations(
        request: Request,
        run_id: Annotated[str, Query(min_length=1, max_length=128)],
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ExplorationConfirmationListResponse:
        service = exploration_confirmation_service(request)
        owned = await service.list_owned_for_run(
            owner_user_id=owner_identity(auth_user), run_id=run_id
        )
        return ExplorationConfirmationListResponse(
            run_id=run_id,
            confirmations=tuple(_view(record) for record in owned),
        )

    @router.get("/{exploration_id}", response_model=ExplorationConfirmationView)
    async def get_exploration_confirmation(
        exploration_id: str,
        request: Request,
        run_id: Annotated[str, Query(min_length=1, max_length=128)],
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> ExplorationConfirmationView:
        service = exploration_confirmation_service(request)
        try:
            record = await service.get_owned_exploration_confirmation(
                owner_user_id=owner_identity(auth_user),
                run_id=run_id,
                exploration_id=exploration_id,
            )
        except ExplorationConfirmationNotFound as exc:
            raise _confirmation_not_found() from exc
        return _view(record)

    app.include_router(router)


__all__ = [
    "EXPLORATION_DEFINITION_REFERENCE_NOT_FOUND",
    "ExplorationConfirmationListResponse",
    "ExplorationConfirmationView",
    "exploration_confirmation_service",
    "owner_identity",
    "register_exploration_confirmation_routes",
]
