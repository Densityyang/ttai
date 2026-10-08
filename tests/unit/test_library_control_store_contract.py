"""Port-parity contract for ControlLibraryRepository (no database required).

The DURABLE behaviour is exercised on real PostgreSQL by
tests/integration/test_library_control_store.py.  This module pins the parts
that must hold even before a connection exists:

* the constructor's "exactly one of database_url or engine" contract;
* the catalogue-delegation surface, including the literal error codes;
* method signatures and exception identities shared with
  InMemoryLibraryRepository, so the two ports stay interchangeable.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from src.nl2sql.artifacts import library as memory_library
from src.nl2sql.artifacts.library import (
    CertificationUnavailable,
    InMemoryLibraryRepository,
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.library_control_store import (
    LOCAL_DEMO_CERTIFICATION_PROVENANCE,
    NOT_CONNECTED,
    PUBLICATION_WITHDRAWN,
    UPGRADE_REQUIRES_NEWER_VERSION,
    ControlLibraryRepository,
)
from src.nl2sql.artifacts.publication import PublicationCatalogue, PublishedVersion

IDENTITY = "demo.complaint_rate"
OTHER_IDENTITY = "demo.order_volume"

REQUIRED_METHODS = (
    "current_version",
    "published_versions",
    "certification_state",
    "is_withdrawn",
    "forkable",
    "install",
    "uninstall",
    "get_install",
    "installs_of",
    "update_available",
    "upgrade",
    "star",
    "unstar",
    "star_count",
    "starred_of",
    "is_starred",
    "acknowledge_withdrawal",
    "is_acknowledged",
    "certify_local_demo",
    "withdraw",
)


class _StubEngine:
    """The engine is never touched by the delegated catalogue reads."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _publication(identity_id: str, version: int) -> PublishedVersion:
    return PublishedVersion(
        identity_id=identity_id,
        version=version,
        title=identity_id,
        owner_user_id="fixture-owner",
        owner_label="fixture",
        source_label="fixture",
        definition_checksum="0" * 64,
        published_at="fixture",
    )


async def _catalogue() -> PublicationCatalogue:
    catalogue = PublicationCatalogue()
    await catalogue.seed(_publication(IDENTITY, 2), current=True)
    await catalogue.seed(_publication(IDENTITY, 1))
    await catalogue.seed(_publication(OTHER_IDENTITY, 1), current=True)
    return catalogue


def test_constructor_requires_exactly_one_database_binding() -> None:
    catalogue = PublicationCatalogue()
    with pytest.raises(ValueError, match="exactly one"):
        ControlLibraryRepository(catalogue)
    with pytest.raises(ValueError, match="exactly one"):
        ControlLibraryRepository(
            catalogue, "postgresql+asyncpg://control_app:pw@127.0.0.1:5432/ttai_control",
            engine=_StubEngine(),  # type: ignore[arg-type]
        )


async def test_close_disposes_the_composed_engine() -> None:
    engine = _StubEngine()
    repository = ControlLibraryRepository(
        PublicationCatalogue(), engine=engine  # type: ignore[arg-type]
    )
    await repository.close()
    assert engine.disposed is True


async def test_catalogue_reads_are_delegated_without_a_connection() -> None:
    catalogue = await _catalogue()
    repository = ControlLibraryRepository(
        catalogue, engine=_StubEngine()  # type: ignore[arg-type]
    )
    try:
        assert await repository.published_versions(IDENTITY) == (1, 2)
        assert await repository.published_versions(OTHER_IDENTITY) == (1,)
        assert await repository.current_version(IDENTITY) == 2
        assert (
            await repository.certification_state(identity_id=IDENTITY, version=2)
            == "uncertified"
        )
        assert await repository.is_withdrawn(identity_id=IDENTITY, version=2) is False
        # Fixture entries carry no semantic package: display/install only.
        assert await repository.forkable(identity_id=IDENTITY, version=2) is False
        assert await repository.forkable(identity_id=IDENTITY, version=99) is False

        provenance = await repository.certify_local_demo(
            identity_id=IDENTITY, version=2, certified_by="local-cert-admin"
        )
        assert provenance == LOCAL_DEMO_CERTIFICATION_PROVENANCE
        assert (
            await repository.certification_state(identity_id=IDENTITY, version=2)
            == "certified"
        )
        assert catalogue._certified_by[(IDENTITY, 2)] == "local-cert-admin"

        await repository.withdraw(identity_id=IDENTITY, version=2)
        assert await repository.is_withdrawn(identity_id=IDENTITY, version=2) is True

        # The catalogue's OWN fail-closed codes are wrapped, never replaced.
        with pytest.raises(LibraryIdentityNotFound) as unknown:
            await repository.current_version("unknown.identity")
        assert str(unknown.value) == "publication_identity_not_found"
        with pytest.raises(LookupError) as uncertifiable:
            await repository.certify_local_demo(
                identity_id=IDENTITY, version=99, certified_by="local-cert-admin"
            )
        assert str(uncertifiable.value) == "publication_version_not_found"
    finally:
        await repository.close()


def test_ports_share_signatures_codes_and_exception_types() -> None:
    for name in REQUIRED_METHODS:
        control: Any = getattr(ControlLibraryRepository, name)
        memory: Any = getattr(InMemoryLibraryRepository, name)
        assert inspect.iscoroutinefunction(control), name
        assert inspect.signature(control) == inspect.signature(memory), name
    assert inspect.iscoroutinefunction(ControlLibraryRepository.close)

    assert memory_library.LibraryIdentityNotFound is LibraryIdentityNotFound
    assert memory_library.CertificationUnavailable is CertificationUnavailable
    assert CertificationUnavailable.code == NOT_CONNECTED == "NOT_CONNECTED"
    assert str(LibraryIdentityNotFound()) == "library_version_not_found"
    assert LibraryIdentityNotFound().code == "library_version_not_found"
    assert PUBLICATION_WITHDRAWN == "publication_withdrawn"
    assert UPGRADE_REQUIRES_NEWER_VERSION == "upgrade_requires_newer_version"
    assert LOCAL_DEMO_CERTIFICATION_PROVENANCE == "local_demo_certification"

    # No second current-version map exists on the durable port.
    assert not hasattr(ControlLibraryRepository, "set_current_version")
