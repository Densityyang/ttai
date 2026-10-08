"""Deterministic slot-bound replan capability (time/grain, user source only)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import RequestIdentity, TimeRange
from src.nl2sql.orchestration.decision_contract import SlotBinding
from src.nl2sql.orchestration.deterministic_query_plan import (
    UNRESOLVED_TIME,
    DeterministicQueryPlanProvider,
    ProposalIntent,
    QueryPlanProposalError,
    _apply_slot_bindings,
    build_plan,
    resolve_time_expression,
)
from src.nl2sql.semantic.metric_match import detect_metric_candidates
from tests.metric_fixtures import MetricAuthority

DISPLAY_NAME = "投诉在途量"
SELECTOR = "metric=complaint_in_transit_count"
FIXED_NOW = datetime(2026, 9, 19, 16, 30, tzinfo=UTC)
CARRIER = TimeRange(start=date(1970, 1, 1), end=date(1970, 1, 1))
REQUEST_ID = __import__("uuid").UUID("33333333-3333-3333-3333-333333333333")


def _identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        permissions=frozenset({"metrics:read"}),
    )


def _clock() -> datetime:
    return FIXED_NOW


def _candidate(authority: MetricAuthority):
    assert authority.release is not None
    return detect_metric_candidates(
        DISPLAY_NAME, authority.release, frozenset(authority.context.asset_ids)
    )[0]


def _intent(authority: MetricAuthority, **overrides: object) -> ProposalIntent:
    base: dict[str, object] = {
        "candidate": _candidate(authority),
        "time_text": None,
        "time_conflict": True,
        "grain": "month",
        "dimension": None,
        "intent": "metric",
        "result_limit": None,
        "filters": (),
    }
    base.update(overrides)
    return ProposalIntent(**base)


class _FakeRegistry:
    def __init__(self, release: object) -> None:
        self._release = release

    def active_release(self) -> object:
        return self._release


def test_time_binding_replaces_the_inert_unresolved_carrier() -> None:
    authority = MetricAuthority()
    intent = _intent(authority, time_conflict=True, time_text=None)
    bound = _apply_slot_bindings(intent, (SlotBinding(slot="time", value="today"),))
    assert bound.time_conflict is False
    assert bound.time_text == "today"

    resolved = resolve_time_expression(bound.time_text, clock=_clock())
    plan, unresolved = build_plan(bound, resolved, authority.context, bound.candidate.contract)
    assert unresolved == ()
    assert plan.unresolved_slots == ()
    assert plan.time_range != CARRIER


def test_time_conflict_can_only_be_resolved_by_an_explicit_binding() -> None:
    authority = MetricAuthority()
    conflicted = _intent(authority, time_conflict=True, time_text="today")
    plan, unresolved = build_plan(
        conflicted, UNRESOLVED_TIME, authority.context, conflicted.candidate.contract
    )
    assert unresolved == ("time",)
    assert plan.time_range == CARRIER

    bound = _apply_slot_bindings(conflicted, (SlotBinding(slot="time", value="today"),))
    resolved = resolve_time_expression(bound.time_text, clock=_clock())
    replanned, unresolved = build_plan(
        bound, resolved, authority.context, bound.candidate.contract
    )
    assert unresolved == ()
    assert replanned.time_range != CARRIER


def test_invalid_time_binding_remains_fail_closed() -> None:
    authority = MetricAuthority()
    intent = _intent(authority, time_conflict=False, time_text=None)
    bound = _apply_slot_bindings(
        intent, (SlotBinding(slot="time", value="not-a-time-expression"),)
    )
    resolved = resolve_time_expression(bound.time_text, clock=_clock())
    plan, unresolved = build_plan(bound, resolved, authority.context, bound.candidate.contract)
    assert unresolved == ("time",)
    assert plan.time_range == CARRIER


def test_grain_binding_accepts_only_the_closed_grain_vocabulary() -> None:
    authority = MetricAuthority()
    intent = _intent(authority, grain="day")
    bound = _apply_slot_bindings(intent, (SlotBinding(slot="grain", value="month"),))
    assert bound.grain == "month"
    with pytest.raises(QueryPlanProposalError, match="slot_binding_grain_invalid"):
        _apply_slot_bindings(intent, (SlotBinding(slot="grain", value="week"),))


def test_grain_unsupported_by_the_contract_remains_unresolved() -> None:
    authority = MetricAuthority()
    intent = _intent(authority, grain="month", time_conflict=False, time_text="today")
    bound = _apply_slot_bindings(intent, (SlotBinding(slot="grain", value="day"),))
    contract = bound.candidate.contract.model_copy(
        update={"supported_grains": ("month",)}
    )
    resolved = resolve_time_expression("today", clock=_clock())
    plan, unresolved = build_plan(bound, resolved, authority.context, contract)
    assert unresolved == ("grain",)
    assert plan.unresolved_slots == ("grain",)


@pytest.mark.parametrize("slot", ["metric", "source", "dimension", "store"])
def test_unsupported_slot_names_fail_closed(slot: str) -> None:
    authority = MetricAuthority()
    with pytest.raises(QueryPlanProposalError, match="unsupported_slot_binding"):
        _apply_slot_bindings(
            _intent(authority), (SlotBinding(slot=slot, value="metric.revenue"),)
        )


@pytest.mark.parametrize("slot", ["authorization", "canonical", "sql", "permissions"])
def test_reserved_slot_names_are_rejected_at_construction(slot: str) -> None:
    with pytest.raises(ValidationError):
        SlotBinding(slot=slot, value="x")


@pytest.mark.parametrize("source", ["semantic_default", "entity_alias"])
def test_non_user_slot_source_fails_closed(source: str) -> None:
    authority = MetricAuthority()
    binding = SlotBinding(slot="time", value="today", source=source)
    with pytest.raises(QueryPlanProposalError, match="slot_binding_source_not_executable"):
        _apply_slot_bindings(_intent(authority), (binding,))


def test_duplicate_and_non_string_bindings_fail_closed() -> None:
    authority = MetricAuthority()
    with pytest.raises(QueryPlanProposalError, match="duplicate_slot_binding"):
        _apply_slot_bindings(
            _intent(authority),
            (
                SlotBinding(slot="time", value="today"),
                SlotBinding(slot="time", value="last_month"),
            ),
        )
    with pytest.raises(QueryPlanProposalError, match="slot_binding_value_invalid"):
        _apply_slot_bindings(_intent(authority), (SlotBinding(slot="time", value=5),))


@pytest.mark.asyncio
async def test_propose_with_slot_bindings_replans_through_the_real_provider() -> None:
    authority = MetricAuthority()
    provider = DeterministicQueryPlanProvider(
        _FakeRegistry(authority.release), _clock
    )
    context = authority.context.model_copy(update={"unresolved_slots": ()})
    plan = await provider.propose_with_slot_bindings(
        question=SELECTOR,
        context=context,
        identity=_identity(),
        slot_bindings=(SlotBinding(slot="time", value="today"),),
    )
    assert plan.unresolved_slots == ()
    assert plan.time_range != CARRIER
    assert plan.checksum


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


@pytest.mark.asyncio
async def test_multi_round_replan_preserves_first_round_time_range() -> None:
    authority = MetricAuthority()
    clock = _MutableClock(FIXED_NOW)
    provider = DeterministicQueryPlanProvider(_FakeRegistry(authority.release), clock)
    context = authority.context.model_copy(update={"unresolved_slots": ()})
    question = SELECTOR + " time=2025-02-29"

    p1 = await provider.propose(question=question, context=context, identity=_identity())
    assert "time" in p1.unresolved_slots

    p2 = await provider.replan_with_slot_bindings(
        question=question,
        context=context,
        identity=_identity(),
        base_plan=p1,
        slot_bindings=(SlotBinding(slot="time", value="today"),),
    )
    assert "time" not in p2.unresolved_slots
    assert p2.time_range != CARRIER

    # Advance the wall clock: a later grain round must NOT re-sample the time.
    clock.instant = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
    p3 = await provider.replan_with_slot_bindings(
        question=question,
        context=context,
        identity=_identity(),
        base_plan=p2,
        slot_bindings=(SlotBinding(slot="grain", value="month"),),
    )
    assert p3.time_range == p2.time_range
    assert p3.grain == "month"
    assert p3.unresolved_slots == ()
    assert p3.checksum != p2.checksum


@pytest.mark.asyncio
async def test_second_round_bad_bindings_still_fail_closed() -> None:
    authority = MetricAuthority()
    provider = DeterministicQueryPlanProvider(
        _FakeRegistry(authority.release), _clock
    )
    context = authority.context.model_copy(update={"unresolved_slots": ()})
    base = await provider.propose(
        question=SELECTOR, context=context, identity=_identity()
    )
    with pytest.raises(
        QueryPlanProposalError, match="slot_binding_source_not_executable"
    ):
        await provider.replan_with_slot_bindings(
            question=SELECTOR,
            context=context,
            identity=_identity(),
            base_plan=base,
            slot_bindings=(
                SlotBinding(slot="grain", value="month", source="semantic_default"),
            ),
        )
    with pytest.raises(QueryPlanProposalError, match="unsupported_slot_binding"):
        await provider.replan_with_slot_bindings(
            question=SELECTOR,
            context=context,
            identity=_identity(),
            base_plan=base,
            slot_bindings=(SlotBinding(slot="metric", value="metric.revenue"),),
        )


@pytest.mark.asyncio
async def test_carrier_time_binding_is_rejected() -> None:
    authority = MetricAuthority()
    provider = DeterministicQueryPlanProvider(
        _FakeRegistry(authority.release), _clock
    )
    context = authority.context.model_copy(update={"unresolved_slots": ()})
    with pytest.raises(
        QueryPlanProposalError, match="slot_binding_time_carrier_rejected"
    ):
        await provider.propose_with_slot_bindings(
            question=SELECTOR,
            context=context,
            identity=_identity(),
            slot_bindings=(SlotBinding(slot="time", value="1970-01-01"),),
        )
