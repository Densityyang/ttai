"""§8.16 P7B: an EXPLORATION confirmation never stands in for a DEFINITION
confirmation — execution-level proof.

The frozen requirement is the literal sentence "探索确认不代替定义确认".  The
failure mode this module exists to catch is that "exploration confirmed" is
silently read as "definition confirmed": the definition's confirmation axis
moves, a version appears, a checksum changes, semantic_closed flips, or a
client can own the actor/time.

Every assertion below EXECUTES the real services and inspects the REAL stored
state (a write-counting store wrapper plus full before/after payloads); none of
it asserts on source text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.nl2sql.artifacts.custom_definition import (
    CustomDefinition,
    DefinitionVersion,
    DefinitionVersionLifecycle,
)
from src.nl2sql.artifacts.definition_store import (
    DefinitionStore,
    InMemoryDefinitionStore,
)
from src.nl2sql.artifacts.exploration_confirmation import (
    ExplorationConfirmation,
    ExplorationConfirmationService,
    ExploreConfirmationRequest,
)
from src.nl2sql.artifacts.service import (
    CustomDefinitionService,
    DefinitionNotFound,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)

_OWNER = "alice"


def _fixed_clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


# The six definition axes.  An exploration confirmation must not move ANY of
# them, so each test asserts them one by one rather than as an aggregate.
_AXIS_FIELDS = (
    "confirmation",
    "retention",
    "publication",
    "certification",
    "governance",
    "authority",
)

# Every spelling a client could try to inject into an exploration confirmation:
# a server-owned actor/time, a server-generated identity, or an
# authority/lifecycle/canonical field the object deliberately does not have.
_INJECTED_FIELDS = (
    "confirmed_by",
    "confirmed_at",
    "actor",
    "identity",
    "user_id",
    "owner_user_id",
    "timestamp",
    "exploration_id",
    "schema_version",
    "authority",
    "canonical",
    "permission",
    "permissions",
    "replaces_definition_confirmation",
)


class _CountingStore:
    """A DefinitionStore delegate that COUNTS every mutating call.

    The count is the point: a single put_definition / put_version /
    put_lifecycle reached by an exploration confirmation is a failure of the
    frozen invariant, and no amount of "the axes look the same" can hide it.
    """

    def __init__(self, inner: InMemoryDefinitionStore | None = None) -> None:
        self.inner = inner if inner is not None else InMemoryDefinitionStore()
        self.put_definition_calls = 0
        self.put_version_calls = 0
        self.put_lifecycle_calls = 0

    @property
    def mutation_calls(self) -> int:
        return (
            self.put_definition_calls
            + self.put_version_calls
            + self.put_lifecycle_calls
        )

    async def ping(self) -> None:
        await self.inner.ping()

    async def close(self) -> None:
        await self.inner.close()

    async def get_definition(self, *, definition_id: str) -> CustomDefinition | None:
        return await self.inner.get_definition(definition_id=definition_id)

    async def list_definitions(self) -> tuple[CustomDefinition, ...]:
        return await self.inner.list_definitions()

    async def put_definition(self, *, definition: CustomDefinition) -> None:
        self.put_definition_calls += 1
        await self.inner.put_definition(definition=definition)

    async def get_version(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersion | None:
        return await self.inner.get_version(
            definition_id=definition_id, version=version
        )

    async def list_versions(
        self, *, definition_id: str
    ) -> tuple[DefinitionVersion, ...]:
        return await self.inner.list_versions(definition_id=definition_id)

    async def put_version(self, *, version: DefinitionVersion) -> None:
        self.put_version_calls += 1
        await self.inner.put_version(version=version)

    async def get_lifecycle(
        self, *, definition_id: str, version: int
    ) -> DefinitionVersionLifecycle | None:
        return await self.inner.get_lifecycle(
            definition_id=definition_id, version=version
        )

    async def put_lifecycle(
        self,
        *,
        definition_id: str,
        version: int,
        lifecycle: DefinitionVersionLifecycle,
    ) -> None:
        self.put_lifecycle_calls += 1
        await self.inner.put_lifecycle(
            definition_id=definition_id, version=version, lifecycle=lifecycle
        )


def _service(store: _CountingStore) -> CustomDefinitionService:
    # The store must satisfy the real port, so a wrapper that drifted from it
    # fails here instead of silently bypassing the definition policy.
    assert isinstance(store, DefinitionStore)
    return CustomDefinitionService(store=store, governed_metric_keys={"demo.revenue"})


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.p7b.exploration",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
    )


async def _closed_draft(service: CustomDefinitionService) -> str:
    draft = await service.create_draft(
        owner_user_id=_OWNER, title="M", calculation=_spec()
    )
    await service.mark_semantic_closed(
        owner_user_id=_OWNER, definition_id=draft.definition_id
    )
    return draft.definition_id


async def _definition_json(
    service: CustomDefinitionService, definition_id: str
) -> str:
    definition = await service.get_owned_definition(
        owner_user_id=_OWNER, definition_id=definition_id
    )
    return definition.model_dump_json()


async def _version_inventory(
    store: _CountingStore, definition_id: str
) -> tuple[tuple[int, str, bool], ...]:
    versions = await store.list_versions(definition_id=definition_id)
    return tuple((v.version, v.checksum, v.semantic_closed) for v in versions)


# --- (1) exploration confirmation leaves the definition in DRAFT -------------


async def test_exploration_confirmation_leaves_the_definition_in_draft() -> None:
    store = _CountingStore()
    service = _service(store)
    definition_id = await _closed_draft(service)
    explorations = ExplorationConfirmationService(
        definitions=service, clock=_fixed_clock
    )

    record = await explorations.confirm_exploration(
        owner_user_id=_OWNER,
        run_id="run-a",
        subject="explore the closed draft",
        definition_id=definition_id,
        version=1,
    )

    # The exploration object is a DIFFERENT object, and it says so itself.
    assert record.replaces_definition_confirmation is False
    # The actor and the clock are SERVER-owned, never echoed from the payload.
    assert record.confirmed_by == _OWNER
    assert record.confirmed_at == _fixed_clock()

    current = await service.get_owned_definition(
        owner_user_id=_OWNER, definition_id=definition_id
    )
    # THE assertion of this task: after an exploration confirmation the
    # definition is STILL a draft on the confirmation axis.
    assert current.axes.confirmation == "DRAFT"

    # CONTRAST: only the REAL definition confirmation moves the axis.  If the
    # exploration confirmation had secretly confirmed anything, this contrast
    # would be unreachable.
    confirmed = await service.confirm(
        owner_user_id=_OWNER, definition_id=definition_id
    )
    assert confirmed.axes.confirmation == "CONFIRMED"


# --- (2) no version is created or modified, and no axis moves ----------------


async def test_exploration_confirmation_creates_and_modifies_no_version() -> None:
    store = _CountingStore()
    service = _service(store)
    # An UNCONFIRMED draft has NO exact version yet, so its count is 0...
    draft_id = await _closed_draft(service)
    # ...and a CONFIRMED definition has exactly one, so its count is 1.  Both
    # are exercised, so "version count unchanged" is not trivially true.
    confirmed_id = await _closed_draft(service)
    await service.confirm(owner_user_id=_OWNER, definition_id=confirmed_id)

    explorations = ExplorationConfirmationService(
        definitions=service, clock=_fixed_clock
    )
    subjects = ((draft_id, 1), (confirmed_id, 1))

    before_json = {
        definition_id: await _definition_json(service, definition_id)
        for definition_id, _version in subjects
    }
    before_inventory = {
        definition_id: await _version_inventory(store, definition_id)
        for definition_id, _version in subjects
    }
    before_axes = {}
    before_closed = {}
    for definition_id, _version in subjects:
        definition = await service.get_owned_definition(
            owner_user_id=_OWNER, definition_id=definition_id
        )
        before_axes[definition_id] = definition.axes
        before_closed[definition_id] = definition.current_version.semantic_closed
    mutations_before = store.mutation_calls

    for definition_id, version in subjects:
        await explorations.confirm_exploration(
            owner_user_id=_OWNER,
            run_id="run-b",
            subject="explore",
            definition_id=definition_id,
            version=version,
        )

    # (2a) ZERO mutating definition-store calls were reached.
    assert store.mutation_calls == mutations_before

    for definition_id, _version in subjects:
        definition = await service.get_owned_definition(
            owner_user_id=_OWNER, definition_id=definition_id
        )
        # (2b) the whole payload is byte-identical before and after.
        assert (
            await _definition_json(service, definition_id)
        ) == before_json[definition_id]
        # (2c) the version inventory (number, checksum, closure) is identical.
        assert (
            await _version_inventory(store, definition_id)
        ) == before_inventory[definition_id]
        # (2d) semantic_closed is unchanged.
        assert (
            definition.current_version.semantic_closed
            is before_closed[definition_id]
        )
        # (2e) EVERY one of the six axes is unchanged, field by field.
        assert definition.axes == before_axes[definition_id]
        for field in _AXIS_FIELDS:
            assert getattr(definition.axes, field) == getattr(
                before_axes[definition_id], field
            )

    # The counts are the non-trivial ones: 0 for the draft, 1 for the confirmed.
    assert len(await _version_inventory(store, draft_id)) == 0
    assert len(await _version_inventory(store, confirmed_id)) == 1
    confirmed_after = await service.get_owned_definition(
        owner_user_id=_OWNER, definition_id=confirmed_id
    )
    assert confirmed_after.axes.retention == "SESSION"


# --- (3) a client can never own the exploration identity or authority --------


@pytest.mark.parametrize("field", _INJECTED_FIELDS)
async def test_a_client_cannot_inject_an_exploration_identity_or_authority(
    field: str,
) -> None:
    store = _CountingStore()
    service = _service(store)
    definition_id = await _closed_draft(service)
    explorations = ExplorationConfirmationService(
        definitions=service, clock=_fixed_clock
    )
    before = await _definition_json(service, definition_id)
    mutations_before = store.mutation_calls

    with pytest.raises(ValidationError) as failure:
        await explorations.confirm_from_client_payload(
            owner_user_id=_OWNER,
            payload={"run_id": "run-c", "subject": "explore", field: "forged"},
        )
    # The refusal names the injected field, so it is self-describing.
    assert field in str(failure.value)

    # ZERO state change: the definition payload is byte-identical, no
    # exploration record was written, and no mutating store call happened.
    assert (await _definition_json(service, definition_id)) == before
    assert await explorations.list_for_run(run_id="run-c") == ()
    assert store.mutation_calls == mutations_before


def test_the_exploration_contracts_are_closed_and_authority_free() -> None:
    # The request surface is exactly the four exploration inputs; every
    # identity/time/authority spelling is refused by the closed contract.
    assert set(ExploreConfirmationRequest.model_fields) == {
        "run_id",
        "subject",
        "definition_id",
        "version",
    }
    authority_fields = {
        "authority",
        "canonical",
        "canonicality",
        "permission",
        "permissions",
        "role",
        "roles",
        "confirmation",
        "retention",
        "publication",
        "certification",
        "governance",
    }
    assert authority_fields.isdisjoint(ExplorationConfirmation.model_fields)
    assert authority_fields.isdisjoint(ExploreConfirmationRequest.model_fields)
    # The invariant is a literal: even an internal constructor cannot flip it.
    with pytest.raises(ValidationError):
        ExplorationConfirmation(
            exploration_id="exp_" + "0" * 32,
            run_id="run-x",
            subject="s",
            confirmed_by=_OWNER,
            confirmed_at=_fixed_clock(),
            replaces_definition_confirmation=True,
        )


async def test_an_exploration_reference_is_owner_scoped_and_read_only() -> None:
    store = _CountingStore()
    service = _service(store)
    definition_id = await _closed_draft(service)
    explorations = ExplorationConfirmationService(
        definitions=service, clock=_fixed_clock
    )
    before = await _definition_json(service, definition_id)

    # A foreign caller gets the SAME not-found as an absent definition and
    # writes NO exploration record, so the reference cannot be probed.
    with pytest.raises(DefinitionNotFound):
        await explorations.confirm_exploration(
            owner_user_id="bob",
            run_id="run-e",
            subject="probe",
            definition_id=definition_id,
            version=1,
        )
    assert await explorations.list_for_run(run_id="run-e") == ()
    assert (await _definition_json(service, definition_id)) == before


# --- (4) an exploration confirmation cannot stand in for definition confirm --


async def test_exploration_confirmation_does_not_unlock_save() -> None:
    store = _CountingStore()
    service = _service(store)
    definition_id = await _closed_draft(service)
    explorations = ExplorationConfirmationService(
        definitions=service, clock=_fixed_clock
    )

    await explorations.confirm_exploration(
        owner_user_id=_OWNER,
        run_id="run-d",
        subject="explore",
        definition_id=definition_id,
        version=1,
    )
    # The exploration confirmation really exists and is queryable...
    assert len(await explorations.list_for_run(run_id="run-d")) == 1

    # ...yet the definition still cannot be used as a confirmed definition:
    # save() requires CONFIRMED, and only the REAL confirmation supplies it.
    with pytest.raises(ValueError) as failure:
        await service.save(owner_user_id=_OWNER, definition_id=definition_id)
    assert str(failure.value) == "SAVED requires CONFIRMED"

    current = await service.get_owned_definition(
        owner_user_id=_OWNER, definition_id=definition_id
    )
    assert current.axes.confirmation == "DRAFT"
    assert current.axes.retention == "SESSION"

    # CONTRAST: the real definition confirmation is what unlocks save.
    await service.confirm(owner_user_id=_OWNER, definition_id=definition_id)
    saved = await service.save(owner_user_id=_OWNER, definition_id=definition_id)
    assert saved.axes.retention == "SAVED"
    assert saved.axes.confirmation == "CONFIRMED"
