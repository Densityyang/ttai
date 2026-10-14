"""§8.16 P7B: server-owned confirmation audit records — execution-level proof.

The failure mode this module exists to catch is "the confirmation transition
happens, but WHO confirmed it, WHEN, and against WHICH server-side decision is
unrecorded, and a client could own those fields".  Every assertion below
EXECUTES the service and inspects the REAL stored state; none of it asserts on
source text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.nl2sql.artifacts.custom_definition import (
    CustomDefinition,
    DefinitionAxes,
    DefinitionVersion,
    derive_parameter_contract,
)
from src.nl2sql.artifacts.definition_confirmation_audit import (
    CONFIRMATION_IDENTITY_IS_SERVER_OWNED,
    CONFIRMATION_RECORD_NOT_FOUND,
    ConfirmationIdentityInjection,
    ConfirmationRecordNotFound,
    ConfirmDefinitionRequest,
    parse_confirm_definition_request,
)
from src.nl2sql.artifacts.definition_store import InMemoryDefinitionStore
from src.nl2sql.artifacts.service import CustomDefinitionService, DefinitionNotFound
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
    ParameterSpec,
)

PINNED_DEFINITION_ID = "def_" + "a" * 32
# The EXACT legacy checksum of the pinned semantics-free fixture.  A LITERAL, not
# a recomputation, so an audit record that leaked into the version identity fails.
PINNED_SEMANTICS_FREE_CHECKSUM = (
    "c5ec4b2dad381ecd39597dbf1970794d868ee119e166043f6e0bf7efe7b8d817"
)

# Every client spelling of "who / when" that must be refused.
INJECTED_IDENTITY_FIELDS = (
    "confirmed_by",
    "confirmed_at",
    "confirmer",
    "actor",
    "identity",
    "user_id",
    "owner_user_id",
    "subject",
    "timestamp",
    "confirmed",
)


def _pinned_spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.checksum.pin",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
        parameters=(ParameterSpec(name="threshold", value_type="integer", required=True),),
    )


def _pinned_version() -> DefinitionVersion:
    spec = _pinned_spec()
    return DefinitionVersion(
        definition_id=PINNED_DEFINITION_ID,
        version=1,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="M",
        semantic_closed=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _pinned_service() -> tuple[CustomDefinitionService, InMemoryDefinitionStore]:
    store = InMemoryDefinitionStore()
    await store.put_definition(
        definition=CustomDefinition(
            definition_id=PINNED_DEFINITION_ID,
            owner_user_id="alice",
            axes=DefinitionAxes(),
            current_version=_pinned_version(),
        )
    )
    return (
        CustomDefinitionService(store=store, governed_metric_keys={"demo.revenue"}),
        store,
    )


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.p7b.audit",
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
        owner_user_id="alice", title="M", calculation=_spec()
    )
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    return draft.definition_id


# --- (1) the record is queryable and carries the SERVER actor and clock --------


async def test_confirmation_records_the_server_actor_and_clock() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    definition_id = await _closed_draft(service)

    # BEFORE the transition there is NO record, and the refusal is a stable code.
    with pytest.raises(ConfirmationRecordNotFound) as missing:
        await service.get_confirmation_record(
            owner_user_id="alice", definition_id=definition_id, version=1
        )
    assert str(missing.value) == CONFIRMATION_RECORD_NOT_FOUND

    started = datetime.now(UTC)
    confirmed = await service.confirm(
        owner_user_id="alice",
        definition_id=definition_id,
        decision_reference="hitl-request-42",
    )
    finished = datetime.now(UTC)

    record = await service.get_confirmation_record(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    assert record.definition_id == definition_id
    assert record.version == 1
    assert record.definition_checksum == confirmed.current_version.checksum
    # SERVER identity, never a client value.
    assert record.confirmed_by == "alice"
    # SERVER clock, observed between two service-side timestamps.
    assert started <= record.confirmed_at <= finished
    assert record.confirmed_at.tzinfo is not None
    # The server-side decision/validation reference is bound to the record.
    assert record.decision_reference == "hitl-request-42"
    assert confirmed.axes.confirmation == "CONFIRMED"

    # The record is owner-scoped: a foreign caller gets the SAME not-found as an
    # absent definition, so it cannot probe the audit trail.
    with pytest.raises(DefinitionNotFound):
        await service.get_confirmation_record(
            owner_user_id="bob", definition_id=definition_id, version=1
        )
    with pytest.raises(DefinitionNotFound):
        await service.list_confirmation_records(
            owner_user_id="bob", definition_id=definition_id
        )
    assert (
        len(
            await service.list_confirmation_records(
                owner_user_id="alice", definition_id=definition_id
            )
        )
        == 1
    )


# --- (2) a client can never own the confirmation actor/time -------------------


@pytest.mark.parametrize("field", INJECTED_IDENTITY_FIELDS)
async def test_a_client_cannot_own_the_confirmation_actor_or_time(field: str) -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    definition_id = await _closed_draft(service)
    before = (
        await service.get_owned_definition(
            owner_user_id="alice", definition_id=definition_id
        )
    ).model_dump_json()

    with pytest.raises(ConfirmationIdentityInjection) as failure:
        await service.confirm_from_client_payload(
            owner_user_id="alice",
            definition_id=definition_id,
            payload={field: "forged"},
        )
    assert failure.value.code == CONFIRMATION_IDENTITY_IS_SERVER_OWNED
    assert field in failure.value.fields

    # ZERO state change: the stored payload is byte-identical and no audit record
    # and no lifecycle transition exists.
    after = (
        await service.get_owned_definition(
            owner_user_id="alice", definition_id=definition_id
        )
    ).model_dump_json()
    assert after == before
    assert (
        await service.list_confirmation_records(
            owner_user_id="alice", definition_id=definition_id
        )
        == ()
    )
    with pytest.raises(ConfirmationRecordNotFound):
        await service.get_confirmation_record(
            owner_user_id="alice", definition_id=definition_id, version=1
        )


def test_the_confirmation_contract_is_closed_and_has_no_identity_field() -> None:
    # extra="forbid" (the shared strict-contract mechanism) refuses the injection.
    for payload in (
        {"confirmed_by": "mallory"},
        {"confirmed_at": "1999-01-01T00:00:00Z"},
        {"decision_reference": "ok", "unknown_field": 1},
    ):
        with pytest.raises(Exception):
            ConfirmDefinitionRequest.model_validate(payload)
    parsed = ConfirmDefinitionRequest.model_validate({"decision_reference": "hitl-1"})
    assert parsed.decision_reference == "hitl-1"
    assert parse_confirm_definition_request({}).decision_reference is None
    # The ONLY client-visible field is a bounded decision reference.
    assert set(ConfirmDefinitionRequest.model_fields) == {"decision_reference"}


async def test_confirm_from_client_payload_still_uses_the_server_identity() -> None:
    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    definition_id = await _closed_draft(service)
    confirmed = await service.confirm_from_client_payload(
        owner_user_id="alice",
        definition_id=definition_id,
        payload={"decision_reference": "hitl-7"},
    )
    assert confirmed.axes.confirmation == "CONFIRMED"
    record = await service.get_confirmation_record(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    assert record.confirmed_by == "alice"
    assert record.decision_reference == "hitl-7"


# --- (3) the record is NOT semantics: the checksum is bit-for-bit unchanged ---


async def test_the_audit_record_never_moves_the_definition_checksum() -> None:
    service, store = await _pinned_service()
    before = (await store.get_definition(definition_id=PINNED_DEFINITION_ID)).current_version
    assert before is not None
    assert before.checksum == PINNED_SEMANTICS_FREE_CHECKSUM

    first = await service.confirm(
        owner_user_id="alice",
        definition_id=PINNED_DEFINITION_ID,
        decision_reference="hitl-a",
    )
    second = await service.confirm(
        owner_user_id="alice",
        definition_id=PINNED_DEFINITION_ID,
        decision_reference="hitl-b",
    )

    # Adding the record moved NOTHING: the literal checksum is identical before,
    # after the first record and after the second.
    assert first.current_version.checksum == PINNED_SEMANTICS_FREE_CHECKSUM
    assert second.current_version.checksum == PINNED_SEMANTICS_FREE_CHECKSUM
    stored = await service.get_exact_version(
        owner_user_id="alice", definition_id=PINNED_DEFINITION_ID, version=1
    )
    assert stored.checksum == PINNED_SEMANTICS_FREE_CHECKSUM

    # Two DIFFERENT confirmations produce two records bound to the SAME checksum,
    # which is only possible because the record is audit and not semantics.
    records = await service.list_confirmation_records(
        owner_user_id="alice", definition_id=PINNED_DEFINITION_ID
    )
    assert len(records) == 2
    assert {record.decision_reference for record in records} == {"hitl-a", "hitl-b"}
    assert {record.definition_checksum for record in records} == {
        PINNED_SEMANTICS_FREE_CHECKSUM
    }
    # Structurally, the record is not a field of the definition or the version.
    assert "confirmed_by" not in DefinitionVersion.model_fields
    assert "confirmed_at" not in DefinitionVersion.model_fields
    assert "confirmation_record" not in CustomDefinition.model_fields
