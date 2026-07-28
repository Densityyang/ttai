from decimal import Decimal
from typing import Any

from src.nl2sql.orchestration.candidates import (
    Candidate,
    CandidateSignals,
    rowset_sha256,
    select_candidate,
)


def _candidate(candidate_id: str, rowset: tuple[dict[str, Any], ...]) -> Candidate:
    signals = CandidateSignals(
        semantic_alignment=0.95,
        schema_validity=0.95,
        execution_signal=0.95,
        cost_score=0.95,
        approved_experience=0.0,
        policy_allowed=True,
        parsed=True,
        readonly=True,
        explain_allowed=True,
    )
    return Candidate(candidate_id, "SELECT 1", rowset, signals)


def test_typed_rowset_hash_is_canonical_and_not_text_rendering() -> None:
    left = ({"amount": Decimal("1.20"), "name": "A"},)
    right = ({"name": "A", "amount": Decimal("1.20")},)

    assert rowset_sha256(left) == rowset_sha256(right)


def test_deep_requires_two_thirds_rowset_consensus() -> None:
    rowset = ({"value": 1},)
    decision = select_candidate(
        [_candidate("a", rowset), _candidate("b", rowset), _candidate("c", ({"value": 2},))], route="deep"
    )

    assert decision.winner is not None
    assert decision.winner.consensus_ratio == 2 / 3
    assert decision.needs_hitl is False
    assert decision.receipts[0].rowset_sha256 == rowset_sha256(rowset)


def test_standard_requires_score_margin_and_hard_gates() -> None:
    same = ({"value": 1},)
    decision = select_candidate([_candidate("a", same), _candidate("b", same)], route="standard")

    assert decision.needs_hitl
    denied = _candidate("denied", same)
    denied = Candidate(denied.candidate_id, denied.sql, denied.rowset, CandidateSignals(
        **{**denied.signals.__dict__, "policy_allowed": False}
    ))
    assert select_candidate([denied], route="fast").winner is None
