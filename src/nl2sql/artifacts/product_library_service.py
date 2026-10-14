"""Product Library orchestration: catalogue, personal library and authority.

Every authority and lifecycle decision for the product library lives HERE, not
in a FastAPI route body:

* certification is a LOCAL-DEMO act, granted ONLY to the configured
  certification administrator under an explicit local-real demo deployment;
* withdrawal is granted ONLY to the owner of that exact publication;
* acknowledgement requires an install of THAT EXACT version.

Installing or starring content NEVER confers any authority over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.nl2sql.artifacts.custom_definition import DefinitionVersion
from src.nl2sql.artifacts.library import (
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.publication import (
    LOCAL_DEMO_CERTIFICATION_PROVENANCE,
    NOT_CONNECTED,
    PublishedVersion,
)
from src.nl2sql.artifacts.publication_service import PublicationService
from src.nl2sql.artifacts.service import CustomDefinitionService

if TYPE_CHECKING:
    from src.nl2sql.artifacts.ports import CataloguePort, LibraryPort




class CertificationForbidden(PermissionError):
    """The caller is not the configured local-demo certification administrator."""

    def __init__(self, reason: str = "local_demo_certification_forbidden") -> None:
        super().__init__(reason)
        self.reason = reason


class WithdrawalForbidden(PermissionError):
    """The caller does not own that exact publication."""

    def __init__(self, reason: str = "withdrawal_owner_required") -> None:
        super().__init__(reason)
        self.reason = reason


class PublicationNotForkable(ValueError):
    """The exact published version carries no reusable semantic package."""

    code = "publication_not_forkable"

    def __init__(self) -> None:
        super().__init__(self.code)


class LibraryVersionNotFound(LookupError):
    """The requested catalogue version does not exist."""

    def __init__(self, reason: str = "library_version_not_found") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class InstallResult:
    """The resulting personal install state."""

    identity_id: str
    installed_version: int


@dataclass(frozen=True, slots=True)
class CertificationResult:
    """A LOCAL-DEMO certification outcome; never a production claim."""

    identity_id: str
    version: int
    certification_state: str
    authority_provenance: str
    production_certification: str


@dataclass(frozen=True, slots=True)
class WithdrawalResult:
    """A source-owner withdrawal outcome."""

    identity_id: str
    version: int
    withdrawn: bool


@dataclass(frozen=True, slots=True)
class AcknowledgementResult:
    """A personal dismissal; the catalogue withdrawal is untouched."""

    identity_id: str
    version: int
    withdrawn: bool
    withdrawal_acknowledged: bool


@dataclass(frozen=True, slots=True)
class CertificationAuthority:
    """The immutable deployment facts that decide certification authority."""

    service_mode: str
    typed_runtime_activation: str
    admin_user_id: str | None

    @property
    def available(self) -> bool:
        return (
            self.service_mode == "infra-dev"
            and self.typed_runtime_activation == "local_real_data_demo"
            and bool(self.admin_user_id)
        )

    def allows(self, *, user_id: str) -> bool:
        """Only the configured administrator; no role or ownership inference."""

        return self.available and bool(self.admin_user_id) and user_id == self.admin_user_id


class ProductLibraryService:
    """Application-scoped orchestration over the SHARED catalogue and services."""

    def __init__(
        self,
        *,
        catalogue: CataloguePort,
        library: LibraryPort,
        definitions: CustomDefinitionService,
        publications: PublicationService,
        certification_authority: CertificationAuthority,
    ) -> None:
        self._catalogue = catalogue
        self._library = library
        self._definitions = definitions
        self._publications = publications
        self._authority = certification_authority

    # --- catalogue / personal library assembly ---------------------------
    async def catalogue_entries(self) -> tuple[dict[str, object], ...]:
        """Discoverable identities.  NO private Draft content is exposed."""

        entries: list[dict[str, object]] = []
        for identity_id in await self._catalogue.identities():
            versions = await self._catalogue.versions(identity_id)
            if not versions:
                continue
            try:
                current = await self._catalogue.current_version(identity_id)
            except LookupError:
                # A missing/corrupt pointer is not a valid product projection.
                continue
            current_publication = await self._catalogue.get(identity_id, current)
            if current_publication is None or await self._catalogue.is_withdrawn(
                identity_id, current
            ):
                # Do not fall back to an older non-withdrawn version.
                continue
            entries.append(
                {
                    "identity_id": identity_id,
                    "title": await self._catalogue.title(identity_id),
                    "owner_label": current_publication.owner_label,
                    "source_label": current_publication.source_label,
                    "current_version": current,
                    "star_count": await self._library.star_count(
                        identity_id=identity_id
                    ),
                    "versions": tuple(
                        [
                            await self._version_view(identity_id, item)
                            for item in versions
                        ]
                    ),
                }
            )
        return tuple(entries)

    async def _version_view(
        self, identity_id: str, item: PublishedVersion
    ) -> dict[str, object]:
        return {
            "version": item.version,
            "title": item.title,
            "published_at": item.published_at,
            "unit": item.unit,
            "certification_state": await self._catalogue.certification_state(
                identity_id, item.version
            ),
            "withdrawn": await self._catalogue.is_withdrawn(
                identity_id, item.version
            ),
            "forkable": item.forkable,
            "derived_from_identity": item.derived_from_identity,
            "derived_from_version": item.derived_from_version,
        }

    async def library_entries(self, *, user_id: str) -> tuple[dict[str, object], ...]:
        """The caller's PERSONAL state.  No winner, no ranking, no advice."""

        entries: list[dict[str, object]] = []
        for binding in await self._library.installs_of(user_id=user_id):
            identity_id = binding.identity_id
            published = await self._catalogue.get(identity_id, binding.version)
            if published is None:
                continue
            try:
                current = await self._catalogue.current_version(identity_id)
            except LookupError:
                # A personal install survives withdrawal, but not an invalid
                # catalogue pointer.  Never fabricate a current version.
                continue
            entries.append(
                {
                    "identity_id": identity_id,
                    "installed_version": binding.version,
                    "pinned": binding.pinned,
                    "current_version": current,
                    "update_available": await self._library.update_available(
                        user_id=user_id, identity_id=identity_id
                    ),
                    "starred": await self._library.is_starred(
                        user_id=user_id, identity_id=identity_id
                    ),
                    "star_count": await self._library.star_count(
                        identity_id=identity_id
                    ),
                    "certification_state": await self._catalogue.certification_state(
                        identity_id, binding.version
                    ),
                    "withdrawn": await self._catalogue.is_withdrawn(
                        identity_id, binding.version
                    ),
                    "withdrawal_acknowledged": await self._library.is_acknowledged(
                        user_id=user_id,
                        identity_id=identity_id,
                        version=binding.version,
                    ),
                    "forkable": published.forkable,
                    "title": published.title,
                    "unit": published.unit,
                    "derived_from_identity": published.derived_from_identity,
                    "derived_from_version": published.derived_from_version,
                }
            )
        return tuple(entries)

    # --- personal mutations ----------------------------------------------
    async def install(
        self, *, user_id: str, identity_id: str, version: int
    ) -> InstallResult:
        binding = await self._library.install(
            user_id=user_id, identity_id=identity_id, version=version
        )
        return InstallResult(identity_id=identity_id, installed_version=binding.version)

    async def uninstall(self, *, user_id: str, identity_id: str) -> dict[str, object]:
        await self._library.uninstall(user_id=user_id, identity_id=identity_id)
        return {"identity_id": identity_id, "installed": False}

    async def star(self, *, user_id: str, identity_id: str) -> int:
        return await self._library.star(user_id=user_id, identity_id=identity_id)

    async def unstar(self, *, user_id: str, identity_id: str) -> int:
        return await self._library.unstar(user_id=user_id, identity_id=identity_id)

    async def upgrade(
        self, *, user_id: str, identity_id: str, to_version: int
    ) -> InstallResult:
        """EXPLICIT only.  The target publication must exist; no auto-upgrade."""

        if await self._catalogue.get(identity_id, to_version) is None:
            raise LibraryVersionNotFound()
        binding = await self._library.upgrade(
            user_id=user_id, identity_id=identity_id, to_version=to_version
        )
        return InstallResult(identity_id=identity_id, installed_version=binding.version)

    async def acknowledge_withdrawal(
        self, *, user_id: str, identity_id: str, version: int
    ) -> AcknowledgementResult:
        """Dismissal only; the catalogue withdrawal is NEVER cleared."""

        if await self._catalogue.get(identity_id, version) is None:
            raise LibraryVersionNotFound()
        try:
            await self._library.acknowledge_withdrawal(
                user_id=user_id, identity_id=identity_id, version=version
            )
        except LibraryIdentityNotFound as exc:
            # A personal-state precondition surfaces as the SAME error family the
            # service already uses, so route mapping stays uniform.
            raise LibraryVersionNotFound(str(exc)) from exc
        return AcknowledgementResult(
            identity_id=identity_id,
            version=version,
            withdrawn=await self._catalogue.is_withdrawn(identity_id, version),
            withdrawal_acknowledged=True,
        )

    # --- fork -------------------------------------------------------------
    async def fork(
        self, *, user_id: str, identity_id: str, version: int, title: str
    ) -> DefinitionVersion:
        """Fork an INSTALLED exact version into a NEW private Definition.

        The caller must hold that exact version, and the published version must
        carry a reusable semantic package.  The new Definition inherits neither
        Star nor certification and is not auto-advanced in any way.
        """

        published = await self._catalogue.get(identity_id, version)
        if published is None:
            raise LibraryVersionNotFound()
        binding = await self._library.get_install(
            user_id=user_id, identity_id=identity_id
        )
        if binding is None or binding.version != version:
            raise LibraryVersionNotFound("library_install_required")
        if published.semantic is None:
            raise PublicationNotForkable()
        # Fork ONLY from a source definition that actually resolves and whose
        # checksum matches the published package.  A dangling source fails closed
        # instead of silently copying an unprovable semantic package.
        source = await self._publications.resolve_published_source(
            identity_id=identity_id, version=version
        )
        return await self._definitions.create_fork(
            owner_user_id=user_id,
            title=title,
            calculation=source.calculation,
            source_definition_id=source.definition_id,
            source_version=source.version,
        )

    # --- local-demo certification ----------------------------------------
    async def certify(
        self, *, user_id: str, identity_id: str, version: int
    ) -> CertificationResult:
        """LOCAL-DEMO certification; never a production certification claim."""

        if not self._authority.allows(user_id=user_id):
            raise CertificationForbidden()
        if await self._catalogue.get(identity_id, version) is None:
            raise LibraryVersionNotFound()
        await self._library.certify_local_demo(
            identity_id=identity_id, version=version, certified_by=user_id
        )
        # Project ONLY onto the CURRENT definition axes: certifying a historical
        # version must never rewrite the current version's axes.
        owner = await self._catalogue.get(identity_id, version)
        if owner is not None:
            await self._publications.project_certified(
                identity_id=identity_id, version=version, owner_user_id=owner.owner_user_id
            )
        return CertificationResult(
            identity_id=identity_id,
            version=version,
            certification_state=await self._catalogue.certification_state(
                identity_id, version
            ),
            authority_provenance=LOCAL_DEMO_CERTIFICATION_PROVENANCE,
            production_certification=NOT_CONNECTED,
        )

    # --- withdrawal -------------------------------------------------------
    async def withdraw(
        self, *, user_id: str, identity_id: str, version: int
    ) -> WithdrawalResult:
        """Source-owner withdrawal.  Nothing is deleted and nobody is evicted."""

        published = await self._catalogue.get(identity_id, version)
        if published is None:
            raise LibraryVersionNotFound()
        if published.owner_user_id != user_id:
            # Installing or starring content NEVER grants withdrawal authority.
            raise WithdrawalForbidden()
        await self._library.withdraw(identity_id=identity_id, version=version)
        return WithdrawalResult(
            identity_id=identity_id,
            version=version,
            withdrawn=await self._catalogue.is_withdrawn(identity_id, version),
        )


def build_product_library_service(
    *,
    catalogue: CataloguePort,
    library: LibraryPort,
    definitions: CustomDefinitionService,
    publications: PublicationService,
    certification_authority: CertificationAuthority,
) -> ProductLibraryService:
    return ProductLibraryService(
        catalogue=catalogue,
        library=library,
        definitions=definitions,
        publications=publications,
        certification_authority=certification_authority,
    )


__all__ = [
    "AcknowledgementResult",
    "CertificationAuthority",
    "CertificationResult",
    "InstallResult",
    "WithdrawalResult",
    "CertificationForbidden",
    "LibraryVersionNotFound",
    "ProductLibraryService",
    "PublicationNotForkable",
    "WithdrawalForbidden",
    "build_product_library_service",
]
