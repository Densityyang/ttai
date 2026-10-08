"""Port-parity contract for the DefinitionStore (no database required).

The DURABLE behaviour is exercised on real PostgreSQL by
tests/integration/test_definition_control_store.py.  This module pins the parts
that must hold even before a connection exists:

* the constructor's "exactly one of database_url or engine" contract;
* method signatures shared by InMemoryDefinitionStore and
  ControlDefinitionStore, so the two ports stay interchangeable;
* the frozen error codes the service and the port report;
* the in-memory store's bit-for-bit equivalence with the service's historical
  process-local behaviour;
* the fail-closed publication -> definition source resolution that removes the
  dangling-reference hole.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from src.nl2sql.artifacts.custom_definition import (
    CustomDefinition,
    DefinitionAxes,
    DefinitionVersion,
    DefinitionVersionLifecycle,
    ParameterContract,
    utcnow,
)
from src.nl2sql.artifacts.definition_control_store import (
    ControlDefinitionStore,
    DefinitionStoreConflict,
    DefinitionStoreIntegrityError,
)
from src.nl2sql.artifacts.definition_store import (
    DefinitionStore,
    InMemoryDefinitionStore,
)
from src.nl2sql.artifacts.library import InMemoryLibraryRepository
from src.nl2sql.artifacts.product_library_service import (
    CertificationAuthority,
    build_product_library_service,
)
from src.nl2sql.artifacts.publication import (
    PublicationCatalogue,
    PublishedSemanticPackage,
    PublishedVersion,
)
from src.nl2sql.artifacts.publication_service import (
    PublicationService,
    PublicationSourceUnresolved,
)
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)

PORT_METHODS = (
    "ping",
    "close",
    "get_definition",
    "list_definitions",
    "put_definition",
    "get_version",
    "list_versions",
    "put_version",
    "get_lifecycle",
    "put_lifecycle",
)

IDENTITY = "def_" + "a" * 32


class _StubEngine:
    """The engine is never touched by the contract assertions."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _EmptyDefinitionStore(InMemoryDefinitionStore):
    """A store that holds nothing, to prove the SERVICE owns the error code."""

    async def get_definition(self, *, definition_id: str) -> CustomDefinition | None:
        return None


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.definition.store",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
    )


def _version(*, version: int = 1, semantic_closed: bool = True) -> DefinitionVersion:
    return DefinitionVersion(
        definition_id=IDENTITY,
        version=version,
        calculation=_spec(),
        parameter_contract=ParameterContract(),
        title="M",
        semantic_closed=semantic_closed,
        created_at=utcnow(),
    )


def _definition(version: DefinitionVersion) -> CustomDefinition:
    return CustomDefinition(
        definition_id=version.definition_id,
        owner_user_id="alice",
        axes=DefinitionAxes(confirmation="CONFIRMED"),
        current_version=version,
    )


def _published(
    identity_id: str,
    version: int,
    *,
    semantic: PublishedSemanticPackage | None,
) -> PublishedVersion:
    return PublishedVersion(
        identity_id=identity_id,
        version=version,
        title=identity_id,
        owner_user_id="alice",
        owner_label="alice",
        source_label="local_demo",
        definition_checksum="0" * 64,
        published_at="2026-01-01T00:00:00Z",
        unit="count",
        semantic=semantic,
    )


def _package(
    *,
    source_definition_id: str,
    source_definition_version: int,
    source_definition_checksum: str,
) -> PublishedSemanticPackage:
    return PublishedSemanticPackage(
        calculation=_spec(),
        parameter_contract=ParameterContract(),
        source_definition_id=source_definition_id,
        source_definition_version=source_definition_version,
        source_definition_checksum=source_definition_checksum,
    )


# --- constructor / lifecycle contract -----------------------------------------


def test_constructor_requires_exactly_one_database_binding() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ControlDefinitionStore()
    with pytest.raises(ValueError, match="exactly one"):
        ControlDefinitionStore(
            "postgresql+asyncpg://control_app:pw@127.0.0.1:5432/ttai_control",
            engine=_StubEngine(),  # type: ignore[arg-type]
        )


async def test_close_disposes_the_composed_engine() -> None:
    engine = _StubEngine()
    store = ControlDefinitionStore(engine=engine)  # type: ignore[arg-type]
    await store.close()
    assert engine.disposed is True


def test_ports_share_signatures_and_are_coroutine_functions() -> None:
    control = ControlDefinitionStore(engine=_StubEngine())  # type: ignore[arg-type]
    memory = InMemoryDefinitionStore()
    for name in PORT_METHODS:
        control_method: Any = getattr(ControlDefinitionStore, name)
        memory_method: Any = getattr(InMemoryDefinitionStore, name)
        assert inspect.iscoroutinefunction(control_method), name
        assert inspect.iscoroutinefunction(memory_method), name
        assert inspect.signature(control_method) == inspect.signature(
            memory_method
        ), name
    # Neither implementation is a subclass of the other; both satisfy the port.
    assert not issubclass(ControlDefinitionStore, InMemoryDefinitionStore)
    assert not issubclass(InMemoryDefinitionStore, ControlDefinitionStore)
    assert isinstance(memory, DefinitionStore)
    assert isinstance(control, DefinitionStore)


def test_frozen_error_codes_are_shared_across_the_port() -> None:
    assert str(DefinitionNotFound()) == "definition_not_found"
    assert str(DefinitionStoreIntegrityError()) == "definition_store_row_incoherent"
    conflict = DefinitionStoreConflict("definition_version_immutable")
    assert str(conflict) == "definition_version_immutable"
    assert conflict.reason == "definition_version_immutable"
    assert issubclass(DefinitionStoreConflict, ValueError)
    assert issubclass(DefinitionStoreIntegrityError, RuntimeError)


# --- in-memory store ----------------------------------------------------------


async def test_in_memory_store_round_trips_identity_version_and_lifecycle() -> None:
    store = InMemoryDefinitionStore()
    await store.ping()
    missing = "def_" + "0" * 32
    assert await store.get_definition(definition_id=missing) is None
    assert await store.list_definitions() == ()
    assert await store.get_version(definition_id=missing, version=1) is None
    assert await store.list_versions(definition_id=missing) == ()
    assert await store.get_lifecycle(definition_id=missing, version=1) is None

    version = _version()
    definition = _definition(version)
    lifecycle = DefinitionVersionLifecycle(confirmation="CONFIRMED", retention="SAVED")
    await store.put_definition(definition=definition)
    await store.put_version(version=version)
    await store.put_lifecycle(
        definition_id=IDENTITY, version=1, lifecycle=lifecycle
    )
    assert await store.get_definition(definition_id=IDENTITY) == definition
    assert await store.list_definitions() == (definition,)
    assert await store.get_version(definition_id=IDENTITY, version=1) == version
    assert await store.list_versions(definition_id=IDENTITY) == (version,)
    assert (
        await store.get_lifecycle(definition_id=IDENTITY, version=1) == lifecycle
    )
    await store.close()


async def _lifecycle_trace(service: CustomDefinitionService) -> tuple[Any, ...]:
    """One scripted lifecycle, observed without any id-dependent checksum."""

    draft = await service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    steps: list[Any] = [("draft", draft.version, draft.semantic_closed)]
    closed = await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    steps.append(("closed", closed.semantic_closed))
    confirmed = await service.confirm(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    steps.append(("confirmed", confirmed.axes.confirmation, confirmed.current_version.version))
    saved = await service.save(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    steps.append(("saved", saved.axes.retention))
    revision = await service.create_revision(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    steps.append(("revision", revision.version, revision.semantic_closed))
    v1 = await service.get_version_lifecycle(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    v2 = await service.get_version_lifecycle(
        owner_user_id="alice", definition_id=draft.definition_id, version=2
    )
    steps.append(
        ("lifecycle", v1.confirmation, v1.retention, v2.confirmation, v2.retention)
    )
    owned = await service.get_owned_definition(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    steps.append(
        (
            "axes",
            owned.axes.confirmation,
            owned.axes.retention,
            owned.axes.publication,
            owned.axes.certification,
            owned.current_version.version,
        )
    )
    exact_v1 = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    steps.append(("exact_v1", exact_v1.version, exact_v1.semantic_closed))
    saved_versions = await service.list_saved_versions(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    steps.append(("saved_versions", tuple(item.version for item in saved_versions)))
    return tuple(steps)


async def test_in_memory_store_is_bit_for_bit_the_service_default() -> None:
    default = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    injected = CustomDefinitionService(
        store=InMemoryDefinitionStore(), governed_metric_keys={"demo.revenue"}
    )
    assert await _lifecycle_trace(default) == await _lifecycle_trace(injected)


async def test_absent_definition_is_the_same_not_found_on_every_store() -> None:
    missing = "def_" + "0" * 32
    for store in (InMemoryDefinitionStore(), _EmptyDefinitionStore()):
        service = CustomDefinitionService(store=store)
        with pytest.raises(DefinitionNotFound) as failure:
            await service.get_owned_definition(
                owner_user_id="alice", definition_id=missing
            )
        assert str(failure.value) == "definition_not_found"


# --- publication -> definition source resolution ------------------------------


async def _confirmed_definition() -> tuple[CustomDefinitionService, str]:
    definitions = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    draft = await definitions.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    for step in ("mark_semantic_closed", "confirm"):
        await getattr(definitions, step)(
            owner_user_id="alice", definition_id=draft.definition_id
        )
    return definitions, draft.definition_id


async def test_resolve_published_source_returns_the_exact_version() -> None:
    definitions = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    catalogue = PublicationCatalogue()
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    draft = await definitions.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    for step in ("mark_semantic_closed", "confirm", "save"):
        await getattr(definitions, step)(
            owner_user_id="alice", definition_id=draft.definition_id
        )
    published = await publications.publish(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    assert published.semantic is not None
    resolved = await publications.resolve_published_source(
        identity_id=draft.definition_id, version=1
    )
    assert resolved.version == 1
    assert resolved.checksum == published.semantic.source_definition_checksum
    assert resolved.definition_id == published.semantic.source_definition_id


async def test_resolve_published_source_fails_closed_when_source_is_absent() -> None:
    definitions = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    catalogue = PublicationCatalogue()
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    await catalogue.seed(
        _published(
            IDENTITY,
            1,
            semantic=_package(
                source_definition_id=IDENTITY,
                source_definition_version=1,
                source_definition_checksum="c" * 64,
            ),
        ),
        current=True,
    )
    with pytest.raises(PublicationSourceUnresolved) as failure:
        await publications.resolve_published_source(identity_id=IDENTITY, version=1)
    assert str(failure.value) == "publication_source_definition_missing"


async def test_resolve_published_source_rejects_a_checksum_mismatch() -> None:
    definitions, definition_id = await _confirmed_definition()
    catalogue = PublicationCatalogue()
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    await catalogue.seed(
        _published(
            definition_id,
            1,
            semantic=_package(
                source_definition_id=definition_id,
                source_definition_version=1,
                source_definition_checksum="d" * 64,
            ),
        ),
        current=True,
    )
    with pytest.raises(PublicationSourceUnresolved) as failure:
        await publications.resolve_published_source(
            identity_id=definition_id, version=1
        )
    assert str(failure.value) == "publication_source_checksum_mismatch"


async def test_resolve_published_source_rejects_a_legacy_or_absent_entry() -> None:
    definitions = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    catalogue = PublicationCatalogue()
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    await catalogue.seed(_published(IDENTITY, 1, semantic=None), current=True)
    with pytest.raises(PublicationSourceUnresolved) as legacy:
        await publications.resolve_published_source(identity_id=IDENTITY, version=1)
    assert str(legacy.value) == "publication_semantic_package_missing"
    with pytest.raises(PublicationSourceUnresolved) as unknown:
        await publications.resolve_published_source(identity_id=IDENTITY, version=9)
    assert str(unknown.value) == "publication_version_not_found"


async def test_fork_fails_closed_when_the_published_source_is_dangling() -> None:
    definitions = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    catalogue = PublicationCatalogue()
    publications = PublicationService(definitions=definitions, catalogue=catalogue)
    library = InMemoryLibraryRepository(catalogue=catalogue)
    product = build_product_library_service(
        catalogue=catalogue,
        library=library,
        definitions=definitions,
        publications=publications,
        certification_authority=CertificationAuthority(
            service_mode="infra-dev",
            typed_runtime_activation="local_real_data_demo",
            admin_user_id="admin",
        ),
    )
    await catalogue.seed(
        _published(
            IDENTITY,
            1,
            semantic=_package(
                source_definition_id=IDENTITY,
                source_definition_version=1,
                source_definition_checksum="e" * 64,
            ),
        ),
        current=True,
    )
    await product.install(user_id="alice", identity_id=IDENTITY, version=1)
    with pytest.raises(PublicationSourceUnresolved):
        await product.fork(
            user_id="alice", identity_id=IDENTITY, version=1, title="Fork"
        )
