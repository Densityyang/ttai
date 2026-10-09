"""Port-parity contract for the two confirmation stores (no database required).

The DURABLE behaviour is exercised on real PostgreSQL by
tests/integration/test_confirmation_control_store.py.  This module pins what must
hold even before a connection exists:

* the constructor's "exactly one of database_url or engine" contract;
* method signatures shared by the in-memory and Control-PostgreSQL
  implementations, so the two ports stay interchangeable;
* the frozen error codes the stores report;
* the in-memory stores' observable behaviour (append-only audit, run-keyed
  exploration, last-record-wins reads);
* the memory-restart contrast: a NEW in-memory store holds NOTHING, which is
  exactly the gap the durable implementations close;
* the typed integrity error that a corrupted row becomes, instead of a bare
  pydantic ValidationError;
* the container wiring that selects a confirmation store by backend and hands
  the SAME instance to the service that writes through it.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from src.nl2sql.artifacts.confirmation_control_store import (
    ConfirmationStoreConflict,
    ConfirmationStoreIntegrityError,
    ControlConfirmationAuditStore,
    ControlExplorationConfirmationStore,
)
from src.nl2sql.artifacts.definition_confirmation_audit import (
    ConfirmationRecord,
    InMemoryConfirmationAuditStore,
)
from src.nl2sql.artifacts.exploration_confirmation import (
    ExplorationConfirmation,
    ExplorationDefinitionReference,
    InMemoryExplorationConfirmationStore,
)

IDENTITY = "def_" + "a" * 32
CHECKSUM = "b" * 64
EXPLORATION_ID = "exp_" + "c" * 32
CONFIRMED_AT = datetime(2026, 1, 1, tzinfo=UTC)

AUDIT_METHODS = ("ping", "close", "put", "get", "list_for_definition")
EXPLORATION_METHODS = ("ping", "close", "put", "get", "list_for_run")


class _StubMappings:
    def __init__(self, row: Any) -> None:
        self._row = row

    def one_or_none(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return [] if self._row is None else [self._row]


class _StubResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def mappings(self) -> _StubMappings:
        return _StubMappings(self._row)


class _StubConnection:
    """The minimum async context manager the store's read path needs."""

    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, *_args: Any, **_kwargs: Any) -> _StubResult:
        return _StubResult(self._row)

    async def __aenter__(self) -> "_StubConnection":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _StubEngine:
    """A connection that returns ONE scripted row; no database is touched."""

    def __init__(self, row: Any = None) -> None:
        self._row = row
        self.disposed = False

    def connect(self) -> _StubConnection:
        return _StubConnection(self._row)

    def begin(self) -> _StubConnection:
        return _StubConnection(self._row)

    async def dispose(self) -> None:
        self.disposed = True


def _record(*, decision_reference: str | None = "hitl-1") -> ConfirmationRecord:
    return ConfirmationRecord(
        definition_id=IDENTITY,
        version=1,
        definition_checksum=CHECKSUM,
        confirmed_by="alice",
        confirmed_at=CONFIRMED_AT,
        decision_reference=decision_reference,
    )


def _reference() -> ExplorationDefinitionReference:
    return ExplorationDefinitionReference(
        definition_id=IDENTITY,
        version=1,
        definition_checksum=CHECKSUM,
        semantic_closed=True,
    )


def _exploration(
    *,
    run_id: str = "run-1",
    exploration_id: str = EXPLORATION_ID,
    reference: ExplorationDefinitionReference | None = None,
) -> ExplorationConfirmation:
    return ExplorationConfirmation(
        exploration_id=exploration_id,
        run_id=run_id,
        subject="explore the closed draft",
        confirmed_by="alice",
        confirmed_at=CONFIRMED_AT,
        definition_reference=reference,
    )


# --- constructor / lifecycle contract -----------------------------------------


def test_constructor_requires_exactly_one_database_binding() -> None:
    for store in (ControlConfirmationAuditStore, ControlExplorationConfirmationStore):
        with pytest.raises(ValueError, match="exactly one"):
            store()
        with pytest.raises(ValueError, match="exactly one"):
            store(
                "postgresql+asyncpg://control_app:pw@127.0.0.1:5432/ttai_control",
                engine=_StubEngine(),  # type: ignore[arg-type]
            )


async def test_close_disposes_the_composed_engine() -> None:
    for store in (ControlConfirmationAuditStore, ControlExplorationConfirmationStore):
        engine = _StubEngine()
        control = store(engine=engine)  # type: ignore[arg-type]
        await control.close()
        assert engine.disposed is True


def test_ports_share_signatures_and_are_coroutine_functions() -> None:
    control_audit = ControlConfirmationAuditStore(engine=_StubEngine())  # type: ignore[arg-type]
    control_exploration = ControlExplorationConfirmationStore(
        engine=_StubEngine()  # type: ignore[arg-type]
    )
    for name in AUDIT_METHODS:
        control_method: Any = getattr(ControlConfirmationAuditStore, name)
        memory_method: Any = getattr(InMemoryConfirmationAuditStore, name)
        assert inspect.iscoroutinefunction(control_method), name
        assert inspect.iscoroutinefunction(memory_method), name
        assert inspect.signature(control_method) == inspect.signature(memory_method), name
    for name in EXPLORATION_METHODS:
        control_method = getattr(ControlExplorationConfirmationStore, name)
        memory_method = getattr(InMemoryExplorationConfirmationStore, name)
        assert inspect.iscoroutinefunction(control_method), name
        assert inspect.iscoroutinefunction(memory_method), name
        assert inspect.signature(control_method) == inspect.signature(memory_method), name
    # Neither implementation is a subclass of the other; both satisfy the port.
    assert not issubclass(
        ControlConfirmationAuditStore, InMemoryConfirmationAuditStore
    )
    assert not issubclass(
        InMemoryConfirmationAuditStore, ControlConfirmationAuditStore
    )
    assert not issubclass(
        ControlExplorationConfirmationStore, InMemoryExplorationConfirmationStore
    )
    assert not issubclass(
        InMemoryExplorationConfirmationStore, ControlExplorationConfirmationStore
    )
    assert isinstance(control_audit, ControlConfirmationAuditStore)
    assert isinstance(control_exploration, ControlExplorationConfirmationStore)


def test_frozen_error_codes_are_shared_across_the_port() -> None:
    assert str(ConfirmationStoreIntegrityError()) == "confirmation_store_row_incoherent"
    assert issubclass(ConfirmationStoreIntegrityError, RuntimeError)
    conflict = ConfirmationStoreConflict("confirmation_audit_append_only")
    assert str(conflict) == "confirmation_audit_append_only"
    assert conflict.reason == "confirmation_audit_append_only"
    assert issubclass(ConfirmationStoreConflict, ValueError)


# --- in-memory behaviour -------------------------------------------------------


async def test_in_memory_audit_store_is_append_only_and_last_wins_on_get() -> None:
    store = InMemoryConfirmationAuditStore()
    await store.ping()
    missing = "def_" + "0" * 32
    assert await store.get(definition_id=missing, version=1) is None
    assert await store.list_for_definition(definition_id=missing) == ()

    first = _record(decision_reference="hitl-a")
    second = _record(decision_reference="hitl-b")
    await store.put(record=first)
    await store.put(record=second)
    # Append-only: BOTH records are retained, and the LATEST is the single read.
    assert await store.list_for_definition(definition_id=IDENTITY) == (first, second)
    assert await store.get(definition_id=IDENTITY, version=1) == second
    await store.close()


async def test_in_memory_exploration_store_is_run_keyed_and_last_wins_on_get() -> None:
    store = InMemoryExplorationConfirmationStore()
    await store.ping()
    assert await store.get(run_id="run-1", exploration_id=EXPLORATION_ID) is None
    assert await store.list_for_run(run_id="run-1") == ()

    without_reference = _exploration()
    with_reference = _exploration(reference=_reference())
    await store.put(record=without_reference)
    await store.put(record=with_reference)
    assert await store.get(
        run_id="run-1", exploration_id=EXPLORATION_ID
    ) == with_reference
    assert await store.list_for_run(run_id="run-1") == (with_reference,)
    await store.close()


async def test_in_memory_confirmation_stores_lose_everything_across_instances() -> None:
    """The memory-restart contrast: a NEW instance holds NOTHING.

    This is the exact gap the durable stores close.  A process-local dict is
    discarded with the process, so "who confirmed this definition" has no
    answer after a restart - and no amount of in-process reuse changes that.
    """

    record = _record()
    first = InMemoryConfirmationAuditStore()
    await first.put(record=record)
    assert await first.get(definition_id=IDENTITY, version=1) == record

    restarted = InMemoryConfirmationAuditStore()
    assert await restarted.get(definition_id=IDENTITY, version=1) is None
    assert await restarted.list_for_definition(definition_id=IDENTITY) == ()

    exploration = _exploration(reference=_reference())
    first_exploration = InMemoryExplorationConfirmationStore()
    await first_exploration.put(record=exploration)
    assert (
        await first_exploration.get(
            run_id="run-1", exploration_id=EXPLORATION_ID
        )
        == exploration
    )
    restarted_exploration = InMemoryExplorationConfirmationStore()
    assert (
        await restarted_exploration.get(
            run_id="run-1", exploration_id=EXPLORATION_ID
        )
        is None
    )
    assert await restarted_exploration.list_for_run(run_id="run-1") == ()


# --- typed integrity error instead of a bare pydantic error --------------------


async def test_a_corrupt_audit_row_is_the_typed_integrity_error() -> None:
    corrupt = {
        "schema_version": "1.0",
        "definition_id": IDENTITY,
        "version": 1,
        # Not a 64-hex checksum: the model refuses it.  The store must NOT leak
        # the pydantic ValidationError.
        "definition_checksum": "not-a-checksum",
        "confirmed_by": "alice",
        "confirmed_at": CONFIRMED_AT,
        "decision_reference": None,
    }
    store = ControlConfirmationAuditStore(engine=_StubEngine(corrupt))  # type: ignore[arg-type]
    with pytest.raises(ConfirmationStoreIntegrityError) as failure:
        await store.get(definition_id=IDENTITY, version=1)
    assert str(failure.value) == "confirmation_store_row_incoherent"
    assert not isinstance(failure.value, ValidationError)


async def test_a_corrupt_exploration_row_is_the_typed_integrity_error() -> None:
    corrupt = {
        "schema_version": "1.0",
        "exploration_id": EXPLORATION_ID,
        "run_id": "run-1",
        "subject": "s",
        "confirmed_by": "alice",
        "confirmed_at": CONFIRMED_AT,
        # extra="forbid" refuses the forged member of the reference.
        "definition_reference": {
            "definition_id": IDENTITY,
            "version": 1,
            "definition_checksum": CHECKSUM,
            "semantic_closed": True,
            "forged": "x",
        },
        "replaces_definition_confirmation": False,
    }
    store = ControlExplorationConfirmationStore(engine=_StubEngine(corrupt))  # type: ignore[arg-type]
    with pytest.raises(ConfirmationStoreIntegrityError) as failure:
        await store.get(run_id="run-1", exploration_id=EXPLORATION_ID)
    assert str(failure.value) == "confirmation_store_row_incoherent"
    assert not isinstance(failure.value, ValidationError)


async def test_a_stored_true_replaces_flag_is_the_typed_integrity_error() -> None:
    corrupt = {
        "schema_version": "1.0",
        "exploration_id": EXPLORATION_ID,
        "run_id": "run-1",
        "subject": "s",
        "confirmed_by": "alice",
        "confirmed_at": CONFIRMED_AT,
        "definition_reference": None,
        "replaces_definition_confirmation": True,
    }
    store = ControlExplorationConfirmationStore(engine=_StubEngine(corrupt))  # type: ignore[arg-type]
    with pytest.raises(ConfirmationStoreIntegrityError):
        await store.get(run_id="run-1", exploration_id=EXPLORATION_ID)


# --- container wiring ----------------------------------------------------------


async def test_container_wires_the_backend_selected_confirmation_stores() -> None:
    from src.core.settings import get_settings
    from src.nl2sql.container import AppContainer

    get_settings.cache_clear()
    container = AppContainer(governed_metric_key_resolver=lambda _key: False)
    audit = container._confirmation_audit_store_instance()
    exploration_store = container._exploration_confirmation_store_instance()
    # The default backend is the process-local one; the durable implementation
    # is selected only by the immutable control setting.
    assert isinstance(audit, InMemoryConfirmationAuditStore)
    assert isinstance(exploration_store, InMemoryExplorationConfirmationStore)
    # The SERVICE writes through the SAME instance the accessor returned, so
    # readiness and the request path cannot observe two different stores.
    assert container.custom_definition_service().confirmation_audit is audit
    service = container.exploration_confirmation_service()
    record = await service.confirm_exploration(
        owner_user_id="alice", run_id="run-wired", subject="s"
    )
    assert (
        await exploration_store.get(
            run_id="run-wired", exploration_id=record.exploration_id
        )
        == record
    )
