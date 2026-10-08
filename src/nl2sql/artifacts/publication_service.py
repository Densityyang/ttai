"""Product publication service: Definition -> immutable published version.

Lifecycle logic lives HERE, not in route bodies.  The catalogue is the sole
authority for published/historical state; the Definition axes are only a
projection of its CURRENT version.
"""

from __future__ import annotations

from src.nl2sql.artifacts.custom_definition import utcnow
from src.nl2sql.artifacts.publication import (
    PublicationCatalogue,
    PublishedSemanticPackage,
    PublishedVersion,
)
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound


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


class PublicationService:
    """Coordinates publication of exact, eligible Definition versions."""

    def __init__(
        self,
        *,
        definitions: CustomDefinitionService,
        catalogue: PublicationCatalogue,
    ) -> None:
        self._definitions = definitions
        self._catalogue = catalogue

    def publish(
        self,
        *,
        owner_user_id: str,
        definition_id: str,
        version: int,
    ) -> PublishedVersion:
        # Owner + exact version: a foreign definition raises the SAME failure as
        # an absent one (no existence oracle).  Only PUBLIC service methods are
        # used across services.
        self._definitions.get_owned_definition(
            owner_user_id=owner_user_id, definition_id=definition_id
        )
        exact = self._definitions.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )
        if self._catalogue.get(definition_id, version) is not None:
            # A published version is IMMUTABLE; re-publishing is a conflict, not
            # a silent no-op or an overwrite.
            raise PublicationConflict("publication_already_exists")
        # Eligibility is proved by the EXACT version's OWN persisted lifecycle,
        # never by the current-axis projection: an explicitly selected historical
        # saved version stays publishable after a later Draft exists.
        lifecycle = self._definitions.get_version_lifecycle(
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
        self._catalogue.publish(published)
        # Project the CURRENT definition axes: published + still UNCERTIFIED.
        self._definitions.project_published(
            owner_user_id=owner_user_id, definition_id=definition_id, version=version
        )
        return published

    def project_certified(
        self, *, identity_id: str, version: int, owner_user_id: str
    ) -> None:
        """Project certification onto the CURRENT definition axes only.

        Certifying an older historical version must NOT alter the current
        definition axes, and the catalogue stays authoritative historically.
        """

        try:
            current = self._definitions.get_owned_definition(
                owner_user_id=owner_user_id, definition_id=identity_id
            )
        except DefinitionNotFound:
            return
        if current.current_version.version != version:
            # A historical version must never rewrite the CURRENT axes.
            return
        self._definitions.project_certified(
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
    "build_publication_service",
]
