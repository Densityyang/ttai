"""Product publication service: Definition -> immutable published version.

Lifecycle logic lives HERE, not in route bodies.  The catalogue is the sole
authority for published/historical state; the Definition axes are only a
projection of its CURRENT version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.nl2sql.artifacts.custom_definition import DefinitionVersion, utcnow
from src.nl2sql.artifacts.publication import (
    PublicationCatalogue,
    PublishedSemanticPackage,
    PublishedVersion,
)
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound

if TYPE_CHECKING:
    from src.nl2sql.artifacts.ports import CataloguePort




class PublicationNotEligible(ValueError):
    """The exact definition version is not publishable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PublicationConflict(ValueError):
    """The exact version is already published (immutable)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PublicationSourceUnresolved(ValueError):
    """A published version's source definition cannot be resolved or verified.

    This is the fail-closed answer to the HALF-persisted state: a publication
    row whose source_definition_id does not resolve (or whose checksum no longer
    matches) must never be served as if its provenance were provable.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PublicationService:
    """Coordinates publication of exact, eligible Definition versions."""

    def __init__(
        self,
        *,
        definitions: CustomDefinitionService,
        catalogue: CataloguePort,
    ) -> None:
        self._definitions = definitions
        self._catalogue = catalogue

    async def publish(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
    ) -> PublishedVersion:
        # Owner + exact version: a foreign definition raises the SAME failure as
        # an absent one (no existence oracle).  Only PUBLIC service methods are
        # used across services.
        await self._definitions.get_owned_definition(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        exact = await self._definitions.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )
        if await self._catalogue.get(definition_id, version) is not None:
            # A published version is IMMUTABLE; re-publishing is a conflict, not
            # a silent no-op or an overwrite.
            raise PublicationConflict("publication_already_exists")
        # Eligibility is proved by the EXACT version's OWN persisted lifecycle,
        # never by the current-axis projection: an explicitly selected historical
        # saved version stays publishable after a later Draft exists.
        lifecycle = await self._definitions.get_version_lifecycle(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )
        if lifecycle.confirmation != "CONFIRMED":
            raise PublicationNotEligible("publication_requires_confirmed")
        if lifecycle.retention != "SAVED":
            raise PublicationNotEligible("publication_requires_saved")
        if not exact.semantic_closed:
            raise PublicationNotEligible("publication_requires_semantic_closure")
        package = PublishedSemanticPackage(
            calculation=exact.calculation,
            parameter_contract=exact.parameter_contract,
            source_definition_id=exact.definition_id,
            source_definition_version=exact.version,
            source_definition_checksum=exact.checksum,
        )
        published = PublishedVersion(
            identity_id=definition_id,
            version=version,
            title=exact.title,
            owner_user_id=owner_user_id,
            owner_label=owner_user_id,
            source_label=owner_user_id,
            definition_checksum=exact.checksum,
            published_at=utcnow().isoformat(),
            unit=exact.calculation.unit,
            derived_from_identity=exact.derived_from_definition_id,
            derived_from_version=exact.derived_from_version,
            semantic=package,
        )
        await self._catalogue.publish(published)
        # Project the CURRENT definition axes: published + still UNCERTIFIED.
        await self._definitions.project_published(
            owner_user_id=owner_user_id, definition_id=definition_id, version=version
        )
        return published

    async def resolve_published_source(
        self, *, identity_id: str, version: int
    ) -> DefinitionVersion:
        """Resolve and VERIFY the exact definition version a publication names.

        A published semantic package carries source_definition_id/version/
        checksum.  With durable definitions that reference must resolve, and the
        resolved exact version's checksum must equal the published one.  When it
        does not (a legacy or externally written row, or a lost definition
        store) this FAILS CLOSED with a typed error instead of returning a
        package whose provenance cannot be proved.
        """

        published = await self._catalogue.get(identity_id, version)
        if published is None:
            raise PublicationSourceUnresolved("publication_version_not_found")
        package = published.semantic
        if package is None:
            raise PublicationSourceUnresolved("publication_semantic_package_missing")
        try:
            exact = await self._definitions.get_exact_version(
                owner_user_id=published.owner_user_id,
                definition_id=package.source_definition_id,
                version=package.source_definition_version,
            )
        except DefinitionNotFound as exc:
            raise PublicationSourceUnresolved(
                "publication_source_definition_missing"
            ) from exc
        if exact.checksum != package.source_definition_checksum:
            raise PublicationSourceUnresolved("publication_source_checksum_mismatch")
        return exact

    async def project_certified(
        self, *, identity_id: str, version: int, owner_user_id: str
    ) -> None:
        """Project certification onto the CURRENT definition axes only.

        Certifying an older historical version must NOT alter the current
        definition axes, and the catalogue stays authoritative historically.
        """

        try:
            current = await self._definitions.get_owned_definition(
                owner_user_id=owner_user_id, definition_id=identity_id
            )
        except DefinitionNotFound:
            return
        if current.current_version.version != version:
            # A historical version must never rewrite the CURRENT axes.
            return
        await self._definitions.project_certified(
            owner_user_id=owner_user_id, definition_id=identity_id
        )


def build_publication_service(
    *, definitions: CustomDefinitionService, catalogue: PublicationCatalogue
) -> PublicationService:
    return PublicationService(definitions=definitions, catalogue=catalogue)


__all__ = [
    "PublicationConflict",
    "PublicationNotEligible",
    "PublicationService",
    "PublicationSourceUnresolved",
    "build_publication_service",
]
