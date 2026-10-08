"""Generic immutable PUBLICATION core (product path, not a fixture type).

Invariants:

* a published VERSION is IMMUTABLE and carries its OWN reusable SEMANTIC
  package (CalculationSpec + ParameterContract), so it stays usable even when
  the caller is not the private Definition owner;
* a runtime business VALUE is never the semantic authority - the value changes
  with data while the SEMANTICS are reusable;
* the current-version pointer is EXPLICIT, never max(version);
* certification and withdrawal are SEPARATE axes: changing either never
  rewrites the immutable semantic package.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.nl2sql.artifacts.custom_definition import ParameterContract
from src.nl2sql.semantic.calculation_contract import CalculationSpec

NOT_CONNECTED = "NOT_CONNECTED"
LOCAL_DEMO_CERTIFICATION_PROVENANCE = "local_demo_certification"
PUBLICATION_WITHDRAWN = "publication_withdrawn"


@dataclass(frozen=True, slots=True)
class PublishedSemanticPackage:
    """The reusable, immutable semantics of ONE published version.

    A legacy/fixture entry that carries no such package is display/install-only
    and MUST fail closed for semantic operations such as Fork.
    """

    calculation: CalculationSpec
    parameter_contract: ParameterContract
    source_definition_id: str
    source_definition_version: int
    source_definition_checksum: str

    def __post_init__(self) -> None:
        # A semantically INCOHERENT package must never enter the catalogue.
        if self.parameter_contract.parameters != tuple(self.calculation.parameters):
            raise ValueError(
                "published package requires a parameter contract matching the "
                "calculation parameters"
            )


@dataclass(frozen=True, slots=True)
class PublishedVersion:
    """One IMMUTABLE published version of a metric identity."""

    identity_id: str
    version: int
    title: str
    owner_user_id: str
    owner_label: str
    source_label: str
    definition_checksum: str
    published_at: str
    unit: str = "ratio"
    # Display value of a LEGACY fixture entry.  This is deliberately NOT the
    # semantic authority of the generic model.
    value: str | None = None
    derived_from_identity: str | None = None
    derived_from_version: int | None = None
    # The reusable semantics.  REQUIRED for NEW product publications; absent
    # for display/install-only legacy fixtures.
    semantic: PublishedSemanticPackage | None = None

    @property
    def forkable(self) -> bool:
        return self.semantic is not None


@dataclass
class PublicationCatalogue:
    """Immutable-version catalogue with EXPLICIT state axes."""

    _versions: dict[tuple[str, int], PublishedVersion] = field(default_factory=dict)
    _current: dict[str, int] = field(default_factory=dict)
    _titles: dict[str, str] = field(default_factory=dict)
    # Certification and withdrawal are SEPARATE from the semantic package.
    _certification: dict[tuple[str, int], str] = field(default_factory=dict)
    _certified_by: dict[tuple[str, int], str] = field(default_factory=dict)
    _withdrawn: dict[tuple[str, int], bool] = field(default_factory=dict)

    def get(self, identity_id: str, version: int) -> PublishedVersion | None:
        return self._versions.get((identity_id, version))

    def versions(self, identity_id: str) -> tuple[PublishedVersion, ...]:
        return tuple(
            sorted(
                (
                    item
                    for (candidate, _), item in self._versions.items()
                    if candidate == identity_id
                ),
                key=lambda item: item.version,
            )
        )

    def identities(self) -> tuple[str, ...]:
        return tuple(sorted({identity for identity, _ in self._versions}))

    def current_version(self, identity_id: str) -> int:
        """The EXPLICIT pointer; never max(version)."""
        if not self.versions(identity_id):
            raise LookupError("publication_identity_not_found")
        current = self._current.get(identity_id)
        if isinstance(current, bool) or not isinstance(current, int):
            raise LookupError("publication_current_version_unbound")
        if self.get(identity_id, current) is None:
            raise LookupError("publication_current_version_invalid")
        return current

    def title(self, identity_id: str) -> str:
        return self._titles.get(identity_id, identity_id)

    def certification_state(self, identity_id: str, version: int) -> str:
        return self._certification.get((identity_id, version), "uncertified")

    def certified_by(self, identity_id: str, version: int) -> str | None:
        return self._certified_by.get((identity_id, version))

    def is_withdrawn(self, identity_id: str, version: int) -> bool:
        return bool(self._withdrawn.get((identity_id, version), False))

    def seed(self, item: PublishedVersion, *, current: bool = False) -> PublishedVersion:
        """Fixture/bootstrap ONLY, with fail-closed overwrite protection.

        Absent            -> seed allowed.
        Present, equal    -> idempotent (acceptable).
        Present, different-> fail closed; a fixture must never silently replace a
                             publication.  Product publication uses publish().
        """

        key = (item.identity_id, item.version)
        existing = self._versions.get(key)
        if existing is not None and existing != item:
            raise ValueError("seed refuses to overwrite an existing publication")

        # ``seed`` is allowed to establish or advance bootstrap state, but it
        # is still subject to the same explicit monotonic-current invariant as
        # product publication.  In particular, an existing identity with a
        # missing/corrupt pointer must not be repaired by guessing a maximum.
        if current:
            existing_versions = self.versions(item.identity_id)
            if not existing_versions:
                if item.identity_id in self._current:
                    raise ValueError("publication_current_version_invalid")
                self._current[item.identity_id] = item.version
            else:
                current_version = self.current_version(item.identity_id)
                if item.version < current_version:
                    raise ValueError("publication_current_version_regression")
                if item.version > current_version:
                    self._current[item.identity_id] = item.version
        self._versions[key] = item
        self._titles.setdefault(item.identity_id, item.title)
        return item

    def publish(self, item: PublishedVersion) -> PublishedVersion:
        """Append ONE immutable semantic version and monotonically advance.

        Publishing a historical version preserves the already established
        current pointer.  Existing identities with a missing or corrupt
        pointer fail closed instead of being repaired by inference.
        """
        key = (item.identity_id, item.version)
        if key in self._versions:
            raise ValueError("published version is immutable")
        if item.semantic is None:
            raise ValueError("publication requires a semantic package")

        existing_versions = self.versions(item.identity_id)
        if existing_versions:
            current = self.current_version(item.identity_id)
        else:
            current = None

        self._versions[key] = item
        self._titles.setdefault(item.identity_id, item.title)
        # The first published version establishes current.  Later historical
        # publications never move that pointer backward.
        if current is None or item.version > current:
            self._current[item.identity_id] = item.version
        # A NEW version starts UNCERTIFIED; historical certification is untouched.
        self._certification.pop(key, None)
        self._certified_by.pop(key, None)
        self._withdrawn.pop(key, None)
        return item

    def certify_local_demo(
        self, identity_id: str, version: int, *, certified_by: str
    ) -> None:
        """LOCAL-DEMO certification; the semantic package is NOT touched."""
        if self.get(identity_id, version) is None:
            raise LookupError("publication_version_not_found")
        self._certification[(identity_id, version)] = "certified"
        self._certified_by[(identity_id, version)] = certified_by

    def withdraw(self, identity_id: str, version: int) -> None:
        """Source withdrawal; the semantic package is NOT touched."""
        if self.get(identity_id, version) is None:
            raise LookupError("publication_version_not_found")
        self._withdrawn[(identity_id, version)] = True


__all__ = [
    "LOCAL_DEMO_CERTIFICATION_PROVENANCE",
    "NOT_CONNECTED",
    "PUBLICATION_WITHDRAWN",
    "PublicationCatalogue",
    "PublishedSemanticPackage",
    "PublishedVersion",
]
