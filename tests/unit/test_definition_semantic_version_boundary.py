"""A6 semantic axes AT the DefinitionVersion boundary: execution-level proof.

The failure mode this module exists to catch is "the semantics module is wired
in, but no reachable path uses it".  Every assertion below EXECUTES the service
and inspects the REAL stored versions; none of it asserts on source text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from src.nl2sql.artifacts.custom_definition import (
    DefinitionExecutionBinding,
    DefinitionVersion,
    derive_parameter_contract,
)
from src.nl2sql.artifacts.definition_control_store import _version_from_payload
from src.nl2sql.artifacts.definition_semantics import (
    AXIS_ORDER,
    DefinitionSemantics,
)
from src.nl2sql.artifacts.service import (
    CustomDefinitionService,
    DefinitionNotFound,
    DraftSemanticUpdate,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
    ParameterBinding,
    ParameterSpec,
)

# The EXACT checksum this fixed, semantics-free fixture produced BEFORE the
# optional semantics field existed.  It is a LITERAL, not a recomputation, so a
# later edit that silently moves the legacy definition identity fails here.
LEGACY_SEMANTICS_FREE_CHECKSUM = (
    "c5ec4b2dad381ecd39597dbf1970794d868ee119e166043f6e0bf7efe7b8d817"
)

PINNED_DEFINITION_ID = "def_" + "a" * 32


def _spec(calculation_id: str = "calc.demo") -> CalculationSpec:
    """A REAL parameterized spec, so parameter binding is exercised."""

    return CalculationSpec(
        calculation_id=calculation_id,
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
        parameters=(
            ParameterSpec(name="threshold", value_type="integer", required=True),
        ),
    )


def _pinned_spec() -> CalculationSpec:
    """The EXACT spec of the legacy checksum fixture (frozen by literal)."""

    return _spec("calc.checksum.pin")


def _semantics(**fields: Any) -> DefinitionSemantics:
    return DefinitionSemantics(**fields)


def _service() -> CustomDefinitionService:
    return CustomDefinitionService(governed_metric_keys={"demo.revenue"})


def _binding(
    exact: DefinitionVersion, **overrides: object
) -> DefinitionExecutionBinding:
    base: dict[str, object] = {
        "definition_id": exact.definition_id,
        "version": exact.version,
        "definition_checksum": exact.checksum,
        "binding": CalculationExecutionBinding(
            calculation_id=exact.calculation.calculation_id,
            spec_checksum=exact.calculation.checksum,
            parameters=(ParameterBinding(name="threshold", value=2),),
        ),
    }
    base.update(overrides)
    return DefinitionExecutionBinding(**base)  # type: ignore[arg-type]


# --- (1) every A6 axis moves the version boundary ----------------------------

_AXIS_CASES: tuple[tuple[str, object, object, str], ...] = (
    ("population", "all orders", "paid orders", "population"),
    ("numerator", "revenue", "gross revenue", "numerator_denominator"),
    ("denominator", "all stores", "open stores", "numerator_denominator"),
    ("deduplication", "none", "distinct_key", "deduplication"),
    ("grain", "store", "region", "grain"),
    ("scope", "team-1", "team-2", "scope"),
    ("time_semantics", "calendar month", "business month", "time_semantics"),
    ("join_semantics", "inner on store_id", "left on store_id", "join_semantics"),
    ("null_semantics", "exclude", "include", "null_semantics"),
    ("unit_precision", "yuan", "wan yuan", "unit_precision"),
    ("provenance", "manual", "automatic base table", "provenance"),
)


@pytest.mark.parametrize(
    ("field", "before_value", "after_value", "axis"),
    _AXIS_CASES,
    ids=[case[0] for case in _AXIS_CASES],
)
async def test_each_semantic_axis_moves_the_version_boundary(
    field: str, before_value: object, after_value: object, axis: str
) -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice",
        title="M",
        calculation=_spec(),
        semantics=_semantics(**{field: before_value}),
    )
    assert draft.version == 1
    assert draft.semantics == _semantics(**{field: before_value})
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )

    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=_semantics(**{field: after_value}),
    )
    assert isinstance(outcome, DraftSemanticUpdate)
    assert outcome.semantic_axes == (axis,)
    assert outcome.material is True
    assert outcome.version_created is True
    assert outcome.requires_business_decision is True
    # A6: the change OPENS a new version and can never reuse the old closure.
    assert outcome.version.version == 2
    assert outcome.version.semantic_closed is False
    assert outcome.version.semantics == _semantics(**{field: after_value})
    with pytest.raises(ValueError, match="confirm requires semantic closure"):
        await service.confirm(
            owner_user_id="alice", definition_id=draft.definition_id
        )

    # A FRESH closure proof on the NEW semantics is what makes v2 confirmable.
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    confirmed = await service.confirm(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    assert confirmed.current_version.version == 2
    exact_v2 = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=2
    )
    assert exact_v2.semantics == _semantics(**{field: after_value})
    # The superseded draft was never confirmed as an exact version, so it is
    # not addressable as one.
    with pytest.raises(DefinitionNotFound):
        await service.get_exact_version(
            owner_user_id="alice", definition_id=draft.definition_id, version=1
        )


# --- (2) a title-only edit is NOT material and keeps closure ----------------


async def test_title_only_edit_is_not_material_and_preserves_closure() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice",
        title="M",
        calculation=_spec(),
        semantics=_semantics(population="paid orders"),
    )
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    before = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )

    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice", definition_id=draft.definition_id, title="Renamed"
    )
    assert outcome.semantic_axes == ()
    assert outcome.material is False
    assert outcome.version_created is False
    assert outcome.requires_business_decision is False
    assert outcome.version.version == 1
    assert outcome.version.semantic_closed is True
    assert outcome.version.semantics == DefinitionSemantics(population="paid orders")
    assert outcome.version.calculation.checksum == before.calculation.checksum
    assert outcome.version.parameter_contract.checksum == (
        before.parameter_contract.checksum
    )
    # The closure SURVIVED, so the definition is immediately confirmable.
    confirmed = await service.confirm(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    assert confirmed.current_version.title == "Renamed"
    assert confirmed.current_version.semantic_closed is True


async def test_diff_equivalent_declaration_does_not_churn_an_in_place_edit() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    assert draft.semantics is None
    before = (
        await service.get_owned_definition(
            owner_user_id="alice", definition_id=draft.definition_id
        )
    ).current_version
    # An all-None DefinitionSemantics declares NOTHING, exactly like absence, so
    # the A6 diff is EMPTY and the stored identity must not churn.
    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=DefinitionSemantics(),
    )
    assert outcome.semantic_axes == ()
    assert outcome.version_created is False
    assert outcome.version.version == 1
    assert outcome.version.semantics is None
    assert outcome.version.checksum == before.checksum

    # The REVERSE direction IS material: declaring an axis opens a version...
    declared = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=DefinitionSemantics(population="paid orders"),
    )
    assert declared.semantic_axes == ("population",)
    assert declared.version.version == 2
    # ...and clearing it again opens ANOTHER version.
    cleared = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=DefinitionSemantics(),
    )
    assert cleared.semantic_axes == ("population",)
    assert cleared.version.version == 3
    assert cleared.version.semantics == DefinitionSemantics()
    assert cleared.version.checksum != declared.version.checksum


# --- (3) in-contract rebinding creates NO version (A5) -----------------------


async def test_in_contract_parameter_rebinding_creates_no_version() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    # (a) An IDENTICAL parameter contract surface is not a contract change.
    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        parameter_contract=derive_parameter_contract(_spec()),
    )
    assert outcome.semantic_axes == ()
    assert outcome.version_created is False
    assert outcome.version.version == 1
    assert outcome.version.parameter_contract == derive_parameter_contract(_spec())

    # (b) A concrete per-run VALUE change is a new BINDING, never a version.
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    exact = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    await service.execute_version(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        version=1,
        binding=_binding(exact),
    )
    after = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    assert after.version == exact.version == 1
    assert after.checksum == exact.checksum
    assert after.parameter_contract.checksum == exact.parameter_contract.checksum


# --- (4) a material change is never a silent pass ----------------------------


async def test_material_change_requires_a_new_business_decision() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice", title="M", calculation=_spec()
    )
    # The diff is exposed BEFORE anything is mutated.
    preview = await service.preview_semantic_axes(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=_semantics(population="paid orders"),
    )
    assert preview == ("population",)

    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=_semantics(population="paid orders"),
    )
    assert outcome.semantic_axes == ("population",)
    # A new version OR an explicit new-decision requirement: never neither.
    assert outcome.version_created or outcome.requires_business_decision
    assert outcome.version_created is True
    assert outcome.requires_business_decision is True
    with pytest.raises(ValueError, match="confirm requires semantic closure"):
        await service.confirm(
            owner_user_id="alice", definition_id=draft.definition_id
        )


async def test_material_edit_after_confirmation_requires_a_new_version() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice",
        title="M",
        calculation=_spec(),
        semantics=_semantics(population="all orders"),
    )
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    await service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    with pytest.raises(
        ValueError, match="confirmed definition requires a new version"
    ):
        await service.update_draft_with_semantics(
            owner_user_id="alice",
            definition_id=draft.definition_id,
            semantics=_semantics(population="paid orders"),
        )
    frozen = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    assert frozen.semantics == DefinitionSemantics(population="all orders")


# --- (5) compatibility: a semantics-free version keeps its legacy identity ----


def test_semantics_free_version_checksum_is_bit_identical_to_legacy() -> None:
    spec = _pinned_spec()
    version = DefinitionVersion(
        definition_id=PINNED_DEFINITION_ID,
        version=1,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="M",
        semantic_closed=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert version.semantics is None
    assert version.checksum == LEGACY_SEMANTICS_FREE_CHECKSUM


def test_a_declared_semantic_axis_is_covered_by_the_checksum() -> None:
    spec = _pinned_spec()
    base = DefinitionVersion(
        definition_id=PINNED_DEFINITION_ID,
        version=1,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="M",
        semantic_closed=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    for field, _before, value, _axis in _AXIS_CASES:
        declared = base.model_copy(update={"semantics": _semantics(**{field: value})})
        assert declared.checksum != base.checksum, field
        other = base.model_copy(update={"semantics": _semantics(**{field: value})})
        assert declared.checksum == other.checksum, field


def test_durable_store_round_trips_a_version_with_semantics() -> None:
    spec = _pinned_spec()
    version = DefinitionVersion(
        definition_id=PINNED_DEFINITION_ID,
        version=1,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="M",
        semantic_closed=True,
        semantics=_semantics(
            population="paid orders",
            denominator="all stores",
            deduplication="distinct_key",
            time_semantics="business month",
            join_semantics="inner on store_id",
            unit_precision="yuan",
            provenance="manual",
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload = json.dumps(
        version.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
    assert '"semantics"' in payload
    # The DURABLE read path (ControlDefinitionStore) rebuilds the exact version,
    # including a checksum re-verification, with NO store change.
    restored = _version_from_payload(payload)
    assert restored == version
    assert restored.checksum == version.checksum
    # A semantics-free payload still round-trips to the legacy identity.
    legacy = version.model_copy(update={"semantics": None})
    legacy_payload = json.dumps(
        legacy.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
    assert (
        _version_from_payload(legacy_payload).checksum
        == LEGACY_SEMANTICS_FREE_CHECKSUM
    )


# --- (6) declaration survives the version lifecycle --------------------------


async def test_revision_inherits_the_declared_semantics() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice",
        title="M",
        calculation=_spec(),
        semantics=_semantics(population="paid orders"),
    )
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    await service.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    revision = await service.create_revision(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    assert revision.version == 2
    assert revision.semantics == DefinitionSemantics(population="paid orders")
    assert revision.semantic_closed is False
    # The revision starts a FRESH lifecycle; v1's confirmation never carries over.
    lifecycle = await service.get_version_lifecycle(
        owner_user_id="alice", definition_id=draft.definition_id, version=2
    )
    assert lifecycle.confirmation == "DRAFT"
    assert lifecycle.retention == "SESSION"


async def test_multiple_axis_changes_are_reported_in_the_frozen_order() -> None:
    service = _service()
    draft = await service.create_draft(
        owner_user_id="alice",
        title="M",
        calculation=_spec(),
        semantics=_semantics(provenance="manual", unit_precision="yuan"),
    )
    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=draft.definition_id,
        semantics=_semantics(
            provenance="automatic base table", unit_precision="wan yuan"
        ),
    )
    # AXIS_ORDER puts unit_precision BEFORE provenance; a naive sorted or
    # insertion-ordered diff would report the reverse.
    assert outcome.semantic_axes == ("unit_precision", "provenance")
    reported = list(outcome.semantic_axes)
    assert reported == [axis for axis in AXIS_ORDER if axis in set(reported)]
