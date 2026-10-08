"""Product-mode / run-envelope capability contract tests."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import ProductMode, RouteName
from src.nl2sql.orchestration import mode_contract as mc


def test_mode_vocabulary_is_exactly_query_analyze_build() -> None:
    assert set(get_args(ProductMode)) == {"QUERY", "ANALYZE", "BUILD"}
    assert set(get_args(mc.RequestedMode)) == {"auto", "QUERY", "ANALYZE", "BUILD"}
    # "auto" is a REQUEST token, never an effective mode
    assert "auto" not in get_args(ProductMode)


def test_route_axis_is_not_the_mode_axis() -> None:
    """fast/standard/deep is an execution/budget axis, not a product mode."""

    assert set(get_args(RouteName)) == {"fast", "standard", "deep"}
    assert set(get_args(RouteName)).isdisjoint(set(get_args(ProductMode)))


def test_auto_resolves_to_analyze_and_is_not_a_classifier() -> None:
    assert mc.resolve_requested_mode("auto") == "ANALYZE"
    # deterministic and total: the same input always yields the same mode
    assert {mc.resolve_requested_mode("auto") for _ in range(5)} == {"ANALYZE"}
    for mode in ("QUERY", "ANALYZE", "BUILD"):
        assert mc.resolve_requested_mode(mode) == mode


def test_run_envelope_has_one_immutable_effective_mode() -> None:
    envelope = mc.RunEnvelope(run_id="run-1", requested_mode="auto", effective_mode="ANALYZE")
    assert envelope.effective_mode == "ANALYZE"
    with pytest.raises(ValidationError):
        envelope.effective_mode = "BUILD"  # type: ignore[misc]
    # the effective mode must be the resolution of the requested mode
    with pytest.raises(ValidationError):
        mc.RunEnvelope(run_id="run-1", requested_mode="auto", effective_mode="BUILD")
    with pytest.raises(ValidationError):
        mc.RunEnvelope(run_id="run-1", requested_mode="QUERY", effective_mode="ANALYZE")


def test_mode_switch_is_a_new_run_that_migrates_context_not_permission() -> None:
    switched = mc.RunEnvelope(
        run_id="run-2",
        requested_mode="BUILD",
        effective_mode="BUILD",
        switched_from_run_id="run-1",
        migrated_context_ref="thread-1",
    )
    assert switched.switched_from_run_id == "run-1"
    # a switch cannot be the same run
    with pytest.raises(ValidationError):
        mc.RunEnvelope(
            run_id="run-1",
            requested_mode="BUILD",
            effective_mode="BUILD",
            switched_from_run_id="run-1",
        )
    # migrated context requires lineage
    with pytest.raises(ValidationError):
        mc.RunEnvelope(
            run_id="run-2",
            requested_mode="ANALYZE",
            effective_mode="ANALYZE",
            migrated_context_ref="thread-1",
        )
    # the envelope carries NO permission field at all
    assert "permissions" not in mc.RunEnvelope.model_fields
    assert "authorization" not in mc.RunEnvelope.model_fields
    assert "roles" not in mc.RunEnvelope.model_fields


def test_no_mode_confers_a_forbidden_capability() -> None:
    registry = mc.mode_capability_registry()
    assert set(registry) == {"QUERY", "ANALYZE", "BUILD"}
    for mode, caps in registry.items():
        granted = set(caps)
        assert granted & mc.FORBIDDEN_CAPABILITIES == set(), mode
        assert granted <= set(get_args(mc.Capability)), mode
    # QUERY strictly grants retrieval only - no model participation
    assert registry["QUERY"] == ("deterministic_retrieval",)
    assert "model_analysis" not in registry["QUERY"]
    assert "run_scoped_derivation" not in registry["QUERY"]
    # semantic authoring is BUILD-only
    assert "semantic_authoring" in registry["BUILD"]
    assert "semantic_authoring" not in registry["ANALYZE"]


def test_mode_switch_proposal_is_a_suggestion_not_an_execution() -> None:
    proposal = mc.ModeSwitchProposal(
        current_run_id="run-1",
        current_mode="QUERY",
        proposed_mode="ANALYZE",
        reason="deterministic resolution could not identify the metric",
    )
    assert proposal.proposed_mode == "ANALYZE"
    # the proposal carries no capability and no permission of its own
    assert "capabilities" not in mc.ModeSwitchProposal.model_fields
    assert "permissions" not in mc.ModeSwitchProposal.model_fields
    assert "effective_mode" not in mc.ModeSwitchProposal.model_fields
    with pytest.raises(ValidationError):
        mc.ModeSwitchProposal(
            current_run_id="run-1",
            current_mode="QUERY",
            proposed_mode="QUERY",
            reason="no change",
        )


def test_cannot_resolve_offers_an_explicit_switch_and_never_a_silent_model() -> None:
    outcome = mc.ModeCapabilityOutcome(
        run_id="run-1",
        effective_mode="QUERY",
        outcome="cannot_resolve",
        suggested_mode="ANALYZE",
    )
    assert outcome.suggested_mode == "ANALYZE"
    # the outcome records a suggestion only - it never invokes anything
    assert "model_calls" not in mc.ModeCapabilityOutcome.model_fields
    assert "executed" not in mc.ModeCapabilityOutcome.model_fields
    with pytest.raises(ValidationError):
        mc.ModeCapabilityOutcome(
            run_id="run-1", effective_mode="QUERY", outcome="cannot_resolve"
        )
    with pytest.raises(ValidationError):
        mc.ModeCapabilityOutcome(
            run_id="run-1",
            effective_mode="QUERY",
            outcome="cannot_resolve",
            suggested_mode="QUERY",
        )
    with pytest.raises(ValidationError):
        mc.ModeCapabilityOutcome(
            run_id="run-1",
            effective_mode="QUERY",
            outcome="resolved",
            suggested_mode="ANALYZE",
        )
    with pytest.raises(ValidationError):
        mc.ModeCapabilityOutcome(
            run_id="run-1", effective_mode="QUERY", outcome="clarification_required"
        )


def test_envelope_checksum_is_deterministic_and_mode_sensitive() -> None:
    first = mc.RunEnvelope(run_id="run-1", requested_mode="auto", effective_mode="ANALYZE")
    second = mc.RunEnvelope(run_id="run-1", requested_mode="auto", effective_mode="ANALYZE")
    assert first.checksum == second.checksum
    other = mc.RunEnvelope(run_id="run-1", requested_mode="BUILD", effective_mode="BUILD")
    assert other.checksum != first.checksum


def test_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        mc.RunEnvelope(
            run_id="run-1",
            requested_mode="auto",
            effective_mode="ANALYZE",
            permission="admin",  # pyright: ignore[reportCallIssue]
        )
