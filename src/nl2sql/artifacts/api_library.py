"""Library HTTP router (11 product endpoints).

Every authority decision is delegated to ProductLibraryService; no route body
decides who may certify, who may withdraw, or what is forkable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.build_run import require_build_run
from src.nl2sql.artifacts.library import (
    PUBLICATION_WITHDRAWN,
    UPGRADE_REQUIRES_NEWER_VERSION,
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.product_library_service import (
    CertificationForbidden,
    LibraryVersionNotFound,
    ProductLibraryService,
    PublicationNotForkable,
    WithdrawalForbidden,
)
from src.nl2sql.artifacts.service import DefinitionNotFound
from src.nl2sql.contracts import StrictContract


class ExactVersionRequest(StrictContract):
    """Install/star/withdraw target: ONE exact published version."""

    identity_id: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)


class IdentityRequest(StrictContract):
    """Identity-scoped personal mutation with no exact version."""

    identity_id: str = Field(min_length=1, max_length=256)


class UpgradeRequest(StrictContract):
    """EXPLICIT upgrade target; there is never an implicit upgrade."""

    identity_id: str = Field(min_length=1, max_length=256)
    to_version: int = Field(ge=1)


class ForkRequest(StrictContract):
    """Fork an INSTALLED exact version into a NEW private Definition."""

    identity_id: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=256)


class MutationResponse(StrictContract):
    identity_id: str
    version: int | None = None
    installed_version: int | None = None
    installed: bool | None = None
    star_count: int | None = None
    starred: bool | None = None
    certified: bool | None = None
    certification_state: str | None = None
    withdrawn: bool | None = None
    withdrawal_acknowledged: bool | None = None
    authority_provenance: str | None = None
    production_certification: str | None = None
    forked_definition_id: str | None = None
    forked_version: int | None = None
    derived_from_definition_id: str | None = None
    derived_from_version: int | None = None


class CatalogueVersionView(StrictContract):
    version: int
    title: str
    published_at: str
    unit: str
    certification_state: str
    withdrawn: bool
    forkable: bool
    derived_from_identity: str | None = None
    derived_from_version: int | None = None


class CatalogueEntryView(StrictContract):
    identity_id: str
    title: str
    owner_label: str
    source_label: str
    current_version: int
    star_count: int
    versions: tuple[CatalogueVersionView, ...]


class CatalogueResponse(StrictContract):
    entries: tuple[CatalogueEntryView, ...]


class LibraryEntryView(StrictContract):
    identity_id: str
    title: str
    installed_version: int
    pinned: bool
    current_version: int
    update_available: bool
    starred: bool
    star_count: int
    certification_state: str
    withdrawn: bool
    withdrawal_acknowledged: bool
    forkable: bool
    unit: str
    derived_from_identity: str | None = None
    derived_from_version: int | None = None


class LibraryResponse(StrictContract):
    entries: tuple[LibraryEntryView, ...]


def owner_identity(auth_user: AuthUser) -> str:
    """The authenticated user as a STRING, converted ONCE at the boundary."""

    return str(auth_user.user_id)


async def library_service(request: Request) -> ProductLibraryService:
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "product_library_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime dependency unavailable",
        )
    service: ProductLibraryService = await container.product_library_service()
    return service


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="library_version_not_found"
    )


def _forbidden(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=code)


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)


def casting_int(value: object) -> int:
    """Narrow an untyped projection value to int without a silent cast."""

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("expected an integer projection value")
    return int(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return casting_int(value)


def _library_entry(entry: Mapping[str, object]) -> LibraryEntryView:
    """Explicit projection: the response shape is never inferred from a bare dict."""

    return LibraryEntryView(
        identity_id=str(entry["identity_id"]),
        title=str(entry["title"]),
        installed_version=casting_int(entry["installed_version"]),
        pinned=bool(entry["pinned"]),
        current_version=casting_int(entry["current_version"]),
        update_available=bool(entry["update_available"]),
        starred=bool(entry["starred"]),
        star_count=casting_int(entry["star_count"]),
        certification_state=str(entry["certification_state"]),
        withdrawn=bool(entry["withdrawn"]),
        withdrawal_acknowledged=bool(entry["withdrawal_acknowledged"]),
        forkable=bool(entry["forkable"]),
        unit=str(entry["unit"]),
        derived_from_identity=_optional_str(entry.get("derived_from_identity")),
        derived_from_version=_optional_int(entry.get("derived_from_version")),
    )


def _catalogue_version(item: Mapping[str, object]) -> CatalogueVersionView:
    return CatalogueVersionView(
        version=casting_int(item["version"]),
        title=str(item["title"]),
        published_at=str(item["published_at"]),
        unit=str(item["unit"]),
        certification_state=str(item["certification_state"]),
        withdrawn=bool(item["withdrawn"]),
        forkable=bool(item["forkable"]),
        derived_from_identity=_optional_str(item.get("derived_from_identity")),
        derived_from_version=_optional_int(item.get("derived_from_version")),
    )


def _catalogue_entry(entry: Mapping[str, object]) -> CatalogueEntryView:
    raw_versions = entry["versions"]
    versions: tuple[CatalogueVersionView, ...] = ()
    if isinstance(raw_versions, (tuple, list)):
        versions = tuple(
            _catalogue_version(item)
            for item in raw_versions
            if isinstance(item, Mapping)
        )
    return CatalogueEntryView(
        identity_id=str(entry["identity_id"]),
        title=str(entry["title"]),
        owner_label=str(entry["owner_label"]),
        source_label=str(entry["source_label"]),
        current_version=casting_int(entry["current_version"]),
        star_count=casting_int(entry["star_count"]),
        versions=versions,
    )


def register_library_routes(app: Any) -> None:
    """Register the 11 Library product endpoints."""

    router = APIRouter(prefix="/api/v2/nl2sql/library", tags=["nl2sql-v2-library"])

    @router.get("", response_model=LibraryResponse)
    async def personal_library(
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> LibraryResponse:
        service = await library_service(request)
        entries = await service.library_entries(user_id=owner_identity(auth_user))
        return LibraryResponse(entries=tuple(_library_entry(entry) for entry in entries))

    @router.get("/catalogue", response_model=CatalogueResponse)
    async def catalogue(
        request: Request,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> CatalogueResponse:
        del auth_user
        service = await library_service(request)
        entries = await service.catalogue_entries()
        return CatalogueResponse(entries=tuple(_catalogue_entry(e) for e in entries))

    @router.post("/install", response_model=MutationResponse)
    async def install(
        request: Request,
        body: ExactVersionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        service = await library_service(request)
        try:
            result = await service.install(
                user_id=owner_identity(auth_user),
                identity_id=body.identity_id,
                version=body.version,
            )
        except (LibraryIdentityNotFound, LibraryVersionNotFound) as exc:
            if isinstance(exc, LibraryIdentityNotFound) and exc.code in {
                PUBLICATION_WITHDRAWN,
                UPGRADE_REQUIRES_NEWER_VERSION,
            }:
                raise _conflict(exc.code) from exc
            raise _not_found() from exc
        return MutationResponse(
            identity_id=body.identity_id,
            installed_version=result.installed_version,
            installed=True,
        )

    @router.post("/uninstall", response_model=MutationResponse)
    async def uninstall(
        request: Request,
        body: IdentityRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        """Idempotent: an absent install is not an error."""

        service = await library_service(request)
        await service.uninstall(
            user_id=owner_identity(auth_user), identity_id=body.identity_id
        )
        return MutationResponse(identity_id=body.identity_id, installed=False)

    @router.post("/star", response_model=MutationResponse)
    async def star(
        request: Request,
        body: IdentityRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        service = await library_service(request)
        try:
            count = await service.star(
                user_id=owner_identity(auth_user), identity_id=body.identity_id
            )
        except LibraryIdentityNotFound as exc:
            raise _not_found() from exc
        return MutationResponse(
            identity_id=body.identity_id, starred=True, star_count=count
        )

    @router.post("/unstar", response_model=MutationResponse)
    async def unstar(
        request: Request,
        body: IdentityRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        """Idempotent: unstar of an unstarred identity is not an error."""

        service = await library_service(request)
        count = await service.unstar(
            user_id=owner_identity(auth_user), identity_id=body.identity_id
        )
        return MutationResponse(
            identity_id=body.identity_id, starred=False, star_count=count
        )

    @router.post("/upgrade", response_model=MutationResponse)
    async def upgrade(
        request: Request,
        body: UpgradeRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        service = await library_service(request)
        try:
            result = await service.upgrade(
                user_id=owner_identity(auth_user),
                identity_id=body.identity_id,
                to_version=body.to_version,
            )
        except LibraryVersionNotFound as exc:
            raise _not_found() from exc
        except LibraryIdentityNotFound as exc:
            raise _conflict(str(exc)) from exc
        return MutationResponse(
            identity_id=body.identity_id,
            installed_version=result.installed_version,
        )

    @router.post("/acknowledge-withdrawal", response_model=MutationResponse)
    async def acknowledge_withdrawal(
        request: Request,
        body: ExactVersionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        service = await library_service(request)
        try:
            result = await service.acknowledge_withdrawal(
                user_id=owner_identity(auth_user),
                identity_id=body.identity_id,
                version=body.version,
            )
        except (LibraryIdentityNotFound, LibraryVersionNotFound) as exc:
            raise _conflict(str(exc)) from exc
        return MutationResponse(
            identity_id=body.identity_id,
            version=body.version,
            withdrawn=result.withdrawn,
            withdrawal_acknowledged=result.withdrawal_acknowledged,
        )

    @router.post("/fork", response_model=MutationResponse)
    async def fork(
        request: Request,
        body: ForkRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        await require_build_run(request, auth_user)
        service = await library_service(request)
        try:
            fork_version = await service.fork(
                user_id=owner_identity(auth_user),
                identity_id=body.identity_id,
                version=body.version,
                title=body.title,
            )
        except PublicationNotForkable as exc:
            raise _conflict(exc.code) from exc
        except (LibraryVersionNotFound, DefinitionNotFound) as exc:
            raise _not_found() from exc
        return MutationResponse(
            identity_id=body.identity_id,
            version=body.version,
            forked_definition_id=fork_version.definition_id,
            forked_version=fork_version.version,
            derived_from_definition_id=fork_version.derived_from_definition_id,
            derived_from_version=fork_version.derived_from_version,
        )

    @router.post("/certify", response_model=MutationResponse)
    async def certify(
        request: Request,
        body: ExactVersionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        # Certification is a DEFINITION lifecycle change: the product service
        # projects CERTIFIED onto the CURRENT definition axes through
        # project_certified().  It therefore requires the SAME current
        # server-persisted BUILD run as create / confirm / save / publish /
        # revision / fork, so no definition axis can move outside BUILD.
        await require_build_run(request, auth_user)
        service = await library_service(request)
        try:
            result = await service.certify(
                user_id=owner_identity(auth_user),
                identity_id=body.identity_id,
                version=body.version,
            )
        except CertificationForbidden as exc:
            raise _forbidden(exc.reason) from exc
        except LibraryVersionNotFound as exc:
            raise _not_found() from exc
        return MutationResponse(
            identity_id=body.identity_id,
            version=body.version,
            certified=result.certification_state == "certified",
            certification_state=result.certification_state,
            authority_provenance=result.authority_provenance,
            production_certification=result.production_certification,
        )

    @router.post("/withdraw", response_model=MutationResponse)
    async def withdraw(
        request: Request,
        body: ExactVersionRequest,
        auth_user: AuthUser = Depends(require_nl2sql_permission),
    ) -> MutationResponse:
        service = await library_service(request)
        try:
            result = await service.withdraw(
                user_id=owner_identity(auth_user),
                identity_id=body.identity_id,
                version=body.version,
            )
        except WithdrawalForbidden as exc:
            raise _forbidden(exc.reason) from exc
        except LibraryVersionNotFound as exc:
            raise _not_found() from exc
        return MutationResponse(
            identity_id=body.identity_id,
            version=body.version,
            withdrawn=result.withdrawn,
        )

    app.include_router(router)


__all__ = [
    "CatalogueResponse",
    "ExactVersionRequest",
    "ForkRequest",
    "IdentityRequest",
    "LibraryResponse",
    "MutationResponse",
    "UpgradeRequest",
    "register_library_routes",
]
