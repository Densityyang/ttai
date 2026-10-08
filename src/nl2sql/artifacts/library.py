"""Library repository: PERSONAL user state over the SHARED publication catalogue.

Division of authority (no duplication):

* PublicationCatalogue owns SOURCE/PUBLISHED state: versions, the explicit
  current pointer, the immutable semantic package, certification, withdrawal.
* InMemoryLibraryRepository owns USER/PERSONAL state only: installs, Stars and
  withdrawal acknowledgement.

The repository is a DUMB store plus catalogue delegation.  Every authority and
lifecycle decision (who may certify, who may withdraw, what is forkable) lives
in ProductLibraryService, never in a route body and never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.nl2sql.artifacts.publication import (
    LOCAL_DEMO_CERTIFICATION_PROVENANCE,
    NOT_CONNECTED,
    PUBLICATION_WITHDRAWN,
    PublicationCatalogue,
)

UPGRADE_REQUIRES_NEWER_VERSION = "upgrade_requires_newer_version"


@dataclass(frozen=True, slots=True)
class InstalledMetricBinding:
    """A PINNED install of one exact published version."""

    identity_id: str
    version: int
    pinned: bool = True


class CertificationUnavailable(RuntimeError):
    """Production certification mutation is not connected."""

    code = NOT_CONNECTED

    def __init__(self) -> None:
        super().__init__(NOT_CONNECTED)


class LibraryIdentityNotFound(LookupError):
    """The requested catalogue identity/version does not exist."""

    def __init__(self, code: str = "library_version_not_found") -> None:
        super().__init__(code)
        self.code = code


@dataclass
class InMemoryLibraryRepository:
    """DEMO/local personal library over a SHARED PublicationCatalogue."""

    catalogue: PublicationCatalogue
    # (user_id, identity_id) -> starred.  IDENTITY scoped: survives upgrades.
    _stars: set[tuple[str, str]] = field(default_factory=set)
    _installs: dict[tuple[str, str], InstalledMetricBinding] = field(
        default_factory=dict
    )
    _acknowledged: set[tuple[str, str, int]] = field(default_factory=set)

    # --- catalogue reads (delegated; NO second current-version map) -------
    def current_version(self, identity_id: str) -> int:
        try:
            return self.catalogue.current_version(identity_id)
        except LookupError as exc:
            raise LibraryIdentityNotFound(str(exc)) from exc

    def published_versions(self, identity_id: str) -> tuple[int, ...]:
        return tuple(item.version for item in self.catalogue.versions(identity_id))

    def certification_state(self, *, identity_id: str, version: int) -> str:
        return self.catalogue.certification_state(identity_id, version)

    def is_withdrawn(self, *, identity_id: str, version: int) -> bool:
        return self.catalogue.is_withdrawn(identity_id, version)

    def forkable(self, *, identity_id: str, version: int) -> bool:
        """Forkable iff an exact published version carries a semantic package."""

        published = self.catalogue.get(identity_id, version)
        return published is not None and published.forkable

    # --- install ----------------------------------------------------------
    def install(
        self, *, user_id: str, identity_id: str, version: int
    ) -> InstalledMetricBinding:
        published = self.catalogue.get(identity_id, version)
        if published is None:
            raise LibraryIdentityNotFound()
        if self.catalogue.is_withdrawn(identity_id, version):
            raise LibraryIdentityNotFound(PUBLICATION_WITHDRAWN)
        binding = InstalledMetricBinding(identity_id=identity_id, version=version)
        self._installs[(user_id, identity_id)] = binding
        return binding

    def uninstall(self, *, user_id: str, identity_id: str) -> None:
        """Idempotent: removing an absent install is acceptable."""

        self._installs.pop((user_id, identity_id), None)

    def get_install(
        self, *, user_id: str, identity_id: str
    ) -> InstalledMetricBinding | None:
        return self._installs.get((user_id, identity_id))

    def installs_of(self, *, user_id: str) -> tuple[InstalledMetricBinding, ...]:
        return tuple(
            binding
            for (owner, _), binding in sorted(self._installs.items())
            if owner == user_id
        )

    def update_available(self, *, user_id: str, identity_id: str) -> bool:
        """Computed against the EXPLICIT pointer; never a numeric max."""

        binding = self._installs.get((user_id, identity_id))
        if binding is None:
            return False
        try:
            current = self.catalogue.current_version(identity_id)
        except LookupError:
            return False
        current_publication = self.catalogue.get(identity_id, current)
        if current_publication is None or self.catalogue.is_withdrawn(
            identity_id, current
        ):
            return False
        return current > binding.version

    def upgrade(
        self, *, user_id: str, identity_id: str, to_version: int
    ) -> InstalledMetricBinding:
        """EXPLICIT only; never a silent upgrade."""

        binding = self.get_install(user_id=user_id, identity_id=identity_id)
        if binding is None:
            raise LibraryIdentityNotFound("library_install_required")
        published = self.catalogue.get(identity_id, to_version)
        if published is None:
            raise LibraryIdentityNotFound()
        if self.catalogue.is_withdrawn(identity_id, to_version):
            raise LibraryIdentityNotFound(PUBLICATION_WITHDRAWN)
        if to_version <= binding.version:
            raise LibraryIdentityNotFound(UPGRADE_REQUIRES_NEWER_VERSION)
        return self.install(
            user_id=user_id, identity_id=identity_id, version=to_version
        )

    # --- star (IDENTITY scoped) ------------------------------------------
    def star(self, *, user_id: str, identity_id: str) -> int:
        """Star an EXISTING catalogue identity; never invent one."""

        if not self.catalogue.versions(identity_id):
            raise LibraryIdentityNotFound("library_identity_not_found")
        self._stars.add((user_id, identity_id))
        return self.star_count(identity_id=identity_id)

    def unstar(self, *, user_id: str, identity_id: str) -> int:
        self._stars.discard((user_id, identity_id))
        return self.star_count(identity_id=identity_id)

    def star_count(self, *, identity_id: str) -> int:
        return sum(1 for _, starred in self._stars if starred == identity_id)

    def starred_of(self, *, user_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(identity for owner, identity in self._stars if owner == user_id)
        )

    def is_starred(self, *, user_id: str, identity_id: str) -> bool:
        return (user_id, identity_id) in self._stars

    # --- withdrawal acknowledgement (personal only) ----------------------
    def acknowledge_withdrawal(
        self, *, user_id: str, identity_id: str, version: int
    ) -> None:
        """Notification dismissal ONLY; never clears catalogue withdrawal.

        Requires an install of THAT EXACT version, so a user cannot
        acknowledge a withdrawal for a version they never received.
        """

        binding = self._installs.get((user_id, identity_id))
        if binding is None:
            raise LibraryIdentityNotFound("library_install_required")
        if binding.version != version:
            raise LibraryIdentityNotFound("library_acknowledgement_version_mismatch")
        self._acknowledged.add((user_id, identity_id, version))

    def is_acknowledged(
        self, *, user_id: str, identity_id: str, version: int
    ) -> bool:
        return (user_id, identity_id, version) in self._acknowledged

    # --- local-demo lifecycle (authority checked by the SERVICE) ---------
    def certify_local_demo(
        self, *, identity_id: str, version: int, certified_by: str
    ) -> str:
        self.catalogue.certify_local_demo(
            identity_id, version, certified_by=certified_by
        )
        return LOCAL_DEMO_CERTIFICATION_PROVENANCE

    def withdraw(self, *, identity_id: str, version: int) -> None:
        self.catalogue.withdraw(identity_id, version)


def seed_catalogue_from_fixtures(catalogue: PublicationCatalogue) -> None:
    """Adapt legacy demo fixtures into the shared catalogue (bootstrap only).

    Seeded entries carry NO semantic package, so they are discoverable,
    installable and star-able but NOT forkable - their display value is never
    treated as published semantics.
    """

    from src.nl2sql.artifacts.publication import PublishedVersion
    from src.nl2sql.demo.fixtures import (
        DEMO_PUBLISHED_CURRENT_VERSION,
        DEMO_PUBLISHED_IDENTITY,
        DEMO_PUBLISHED_VERSIONS,
    )

    current_versions = DEMO_PUBLISHED_CURRENT_VERSION
    # Establish explicit current publications before loading historical
    # versions.  Seed intentionally fails closed when an identity already has
    # versions but no current pointer, so bootstrap must not repair that state.
    ordered_fixtures = tuple(
        sorted(
            DEMO_PUBLISHED_VERSIONS,
            key=lambda fixture: (
                0
                if current_versions.get(fixture.identity_id) == fixture.version
                else 1
            ),
        )
    )
    for fixture in ordered_fixtures:
        catalogue.seed(
            PublishedVersion(
                identity_id=fixture.identity_id,
                version=fixture.version,
                title=DEMO_PUBLISHED_IDENTITY.display_name,
                owner_user_id=DEMO_PUBLISHED_IDENTITY.owner_label,
                owner_label=DEMO_PUBLISHED_IDENTITY.owner_label,
                source_label=DEMO_PUBLISHED_IDENTITY.owner_label,
                definition_checksum="0" * 64,
                published_at="fixture",
                unit=fixture.unit,
                value=fixture.value,
            ),
            current=current_versions.get(fixture.identity_id) == fixture.version,
        )
        if fixture.certification_state == "certified":
            catalogue.certify_local_demo(
                fixture.identity_id, fixture.version, certified_by="fixture"
            )


__all__ = [
    "CertificationUnavailable",
    "InMemoryLibraryRepository",
    "InstalledMetricBinding",
    "LibraryIdentityNotFound",
    "NOT_CONNECTED",
    "PUBLICATION_WITHDRAWN",
    "UPGRADE_REQUIRES_NEWER_VERSION",
    "seed_catalogue_from_fixtures",
]
