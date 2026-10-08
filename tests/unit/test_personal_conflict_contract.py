"""Personal semantic-conflict contract tests."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from src.nl2sql.semantic import personal_conflict_contract as pc


def _candidate(cid: str, **overrides: object) -> pc.PersonalCandidateRef:
    base: dict[str, object] = {
        "candidate_id": cid,
        "kind": "saved_custom_definition",
        "origin": "own",
        "definition_id": "def-" + cid,
        "definition_version": 1,
        "display_name": "Revenue rate",
    }
    base.update(overrides)
    return pc.PersonalCandidateRef(**base)  # type: ignore[arg-type]


def test_identical_identity_and_version_is_deduplicated() -> None:
    first = _candidate("a")
    duplicate = _candidate("a2", definition_id="def-a", definition_version=1)
    assert first.identity == duplicate.identity
    deduped = pc.deduplicate_candidates((first, duplicate))
    assert len(deduped) == 1


def test_same_name_different_identity_is_never_collapsed() -> None:
    """Name similarity must never merge candidates."""

    own = _candidate("a", display_name="Repair rate")
    installed = _candidate(
        "b",
        kind="installed_published_metric",
        origin="installed",
        definition_id="def-b",
        display_name="Repair rate",
    )
    assert own.display_name == installed.display_name
    assert len(pc.deduplicate_candidates((own, installed))) == 2
    resolution, conflict = pc.resolve_personal_conflict((own, installed))
    assert resolution.outcome == "clarification_required"
    assert conflict is not None
    assert len(conflict.candidates) == 2


def test_materially_distinct_candidates_require_user_clarification() -> None:
    pairs = (
        ("saved_custom_definition", "own", "installed_published_metric", "installed"),
        ("installed_derivative", "installed", "installed_published_metric", "installed"),
        ("independently_published_metric", "installed", "installed_published_metric", "installed"),
    )
    for left_kind, left_origin, right_kind, right_origin in pairs:
        left = _candidate("left", kind=left_kind, origin=left_origin, definition_id="d-left")
        right = _candidate(
            "right", kind=right_kind, origin=right_origin, definition_id="d-right"
        )
        resolution, conflict = pc.resolve_personal_conflict((left, right))
        assert resolution.outcome == "clarification_required"
        assert conflict is not None


def test_resolution_never_selects_a_winner() -> None:
    """No ranking signal and no chosen-candidate field may exist."""

    assert "winner" not in pc.SemanticConflict.model_fields
    assert "selected" not in pc.SemanticConflict.model_fields
    assert "chosen_candidate_id" not in pc.SemanticConflict.model_fields
    high_star = _candidate("hi", definition_id="d-hi", star_count=10_000)
    certified = _candidate(
        "cert", definition_id="d-cert", certification_state="certified", star_count=0
    )
    resolution, conflict = pc.resolve_personal_conflict((high_star, certified))
    assert resolution.outcome == "clarification_required"
    assert conflict is not None
    # presenting both is the only outcome; neither is pre-selected
    assert {c.candidate_id for c in conflict.candidates} == {"hi", "cert"}


def test_star_and_certification_do_not_change_identity() -> None:
    plain = _candidate("a")
    starred = _candidate("a", star_count=999, certification_state="certified")
    assert plain.identity_checksum == starred.identity_checksum
    assert plain.identity == starred.identity


def test_ranking_signals_are_enumerated_as_non_selecting() -> None:
    for signal in (
        "star_count",
        "certification_state",
        "popularity",
        "install_count",
        "owner_identity",
        "model_preference",
        "display_name_similarity",
    ):
        assert signal in pc.NON_SELECTING_SIGNALS


def test_single_candidate_resolves_and_none_has_no_definition() -> None:
    only = _candidate("a")
    resolution, conflict = pc.resolve_personal_conflict((only,))
    assert resolution.outcome == "resolved"
    assert conflict is None
    empty, none_conflict = pc.resolve_personal_conflict(())
    assert empty.outcome == "no_authoritative_definition"
    assert none_conflict is None


def test_no_fourth_resolution_outcome_is_introduced() -> None:
    assert set(get_args(pc.SemanticResolution.model_fields["outcome"].annotation)) == {
        "resolved",
        "clarification_required",
        "no_authoritative_definition",
    }


def test_selection_is_run_scoped_by_default() -> None:
    default = pc.PersonalSelection(
        conflict_id="c1", required_slot="metric", selected_candidate_id="a"
    )
    assert default.selection_scope == "run_scoped"
    assert default.explicit_user_request is False
    # persisting requires an EXPLICIT user request
    with pytest.raises(ValidationError):
        pc.PersonalSelection(
            conflict_id="c1",
            required_slot="metric",
            selected_candidate_id="a",
            selection_scope="persisted",
        )
    persisted = pc.PersonalSelection(
        conflict_id="c1",
        required_slot="metric",
        selected_candidate_id="a",
        selection_scope="persisted",
        explicit_user_request=True,
    )
    assert persisted.selection_scope == "persisted"


def test_conflict_requires_unique_identities_and_valid_difference_refs() -> None:
    left = _candidate("a")
    right = _candidate("b", definition_id="d-b")
    # identical identity+version must have been deduplicated, not presented
    with pytest.raises(ValidationError):
        pc.SemanticConflict(
            conflict_id="c1",
            required_slot="metric",
            candidates=(left, _candidate("a2", definition_id="def-a")),
        )
    difference = pc.MaterialDifference(
        kind="null_semantics",
        summary="one treats NULL as no-data, the other substitutes zero",
        candidate_ids=("a", "b"),
    )
    conflict = pc.SemanticConflict(
        conflict_id="c1",
        required_slot="metric",
        candidates=(left, right),
        differences=(difference,),
    )
    assert conflict.differences[0].kind == "null_semantics"
    with pytest.raises(ValidationError):
        pc.SemanticConflict(
            conflict_id="c1",
            required_slot="metric",
            candidates=(left, right),
            differences=(
                pc.MaterialDifference(
                    kind="unit", summary="different unit", candidate_ids=("ghost",)
                ),
            ),
        )


def test_conflict_is_deterministic_and_order_independent_in_identity() -> None:
    left = _candidate("a")
    right = _candidate("b", definition_id="d-b")
    first = pc.SemanticConflict(
        conflict_id="c1", required_slot="metric", candidates=(left, right)
    )
    second = pc.SemanticConflict(
        conflict_id="c1", required_slot="metric", candidates=(left, right)
    )
    assert first.checksum == second.checksum
    # conflict id derives from identity, not from presentation order
    forward, _ = pc.resolve_personal_conflict((left, right))
    backward, _ = pc.resolve_personal_conflict((right, left))
    assert forward.outcome == backward.outcome == "clarification_required"
