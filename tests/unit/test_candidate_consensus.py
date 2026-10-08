from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

import pytest

from src.nl2sql.contracts import ExecutionReceipt
from src.nl2sql.infra.governance.query_gateway import PolicyEngine
from src.nl2sql.orchestration.budget import bootstrap_routing_budget_policy
from src.nl2sql.orchestration.candidates import (
    GATE_ORDER,
    Candidate,
    CandidateExpectedEvidence,
    CandidateMetadata,
    CandidateVerifier,
    GateOutcome,
    candidate_capacity,
    rowset_sha256,
    select_candidate,
)


def _candidate(candidate_id: str = "a", **changes: Any) -> Candidate:
    sql = "SELECT 1 AS value"
    fingerprint = PolicyEngine().prepare(sql).fingerprint
    rows = ({"value": 1},)
    metadata = CandidateMetadata(candidate_id, "deterministic", fingerprint, "a" * 64,
                                 ("relation.approved",), "approved_detail", 0, 1, 1.0)
    metadata = replace(metadata, **changes)
    receipt = ExecutionReceipt(datasource="synthetic", readonly_role="reader", elapsed_ms=1,
                               row_count=1, policy_outcome="allow", sql_fingerprint=fingerprint,
                               rowset_sha256=rowset_sha256(rows), estimated_rows=metadata.estimated_rows,
                               policy_version="synthetic.v1", source_kind=metadata.source_kind,
                               source_id="synthetic.source", source_checkpoint="synthetic.checkpoint",
                               selection_reason="approved_detail",
                               semantic_signature=metadata.semantic_signature,
                               plan_cost=metadata.estimated_cost)
    return Candidate(metadata, tuple(GateOutcome(name, True) for name in GATE_ORDER), receipt, sql, rows)


def _choose(*candidates: Candidate):
    return select_candidate(candidates, route="deep", capacity_available=True,
                            budget=bootstrap_routing_budget_policy().routes["deep"], verifier=_verifier(*candidates))


def _verifier(*candidates: Candidate) -> CandidateVerifier:
    # Synthetic compiler expectations; authority constants do not come from
    # the receipt being verified. Production adapters register these beforehand.
    return CandidateVerifier(expected={item.metadata.candidate_id: CandidateExpectedEvidence(
        metadata=item.metadata, policy_version="synthetic.v1", readonly_role="reader",
        source_id="synthetic.source", source_checkpoint="synthetic.checkpoint",
        selection_reason="approved_detail",
    ) for item in candidates})


def test_typed_rowset_hash_is_canonical_and_not_text_rendering() -> None:
    assert rowset_sha256([{"amount": Decimal("1.20"), "name": "A"}]) == rowset_sha256(
        [{"name": "A", "amount": Decimal("1.2")}])


@pytest.mark.parametrize("changes", [
    {"source_kind": "approved_aggregate"}, {"estimated_rows": 0}, {"estimated_cost": 0.0},
])
def test_deterministic_priority(changes: dict[str, Any]) -> None:
    result = _choose(_candidate("a"), _candidate("z", **changes))
    assert result.winner is not None and result.winner.metadata.candidate_id == "z"
    assert not result.needs_hitl


def test_relation_join_and_stable_id_priorities() -> None:
    result = _choose(_candidate("z"), _candidate("a", relation_ids=("r1", "r2")))
    assert result.winner is not None and result.winner.metadata.candidate_id == "z"
    result = _choose(_candidate("z"), _candidate("a", join_hops=1))
    assert result.winner is not None and result.winner.metadata.candidate_id == "z"
    assert _choose(_candidate("b"), _candidate("a")).winner == _choose(_candidate("a")).winner


@pytest.mark.parametrize("gate", GATE_ORDER)
def test_ordered_hard_gate_failure(gate) -> None:
    candidate = _candidate()
    candidate = replace(candidate, gates=tuple(GateOutcome(name, name != gate) for name in GATE_ORDER))
    result = _choose(candidate, _candidate("b"))
    assert result.winner is not None and result.winner.metadata.candidate_id == "b"
    assert result.receipts[0].failure_code == f"{gate}_denied"
    assert len(result.receipts[0].gates) == GATE_ORDER.index(gate) + 1
    assert _choose(candidate).reason == "no_candidate_passed_hard_gates"


def test_divergence_never_votes_or_selects() -> None:
    assert _choose(_candidate(), _candidate("b", semantic_signature="b" * 64)).reason == "semantic_divergence"
    second = _candidate("b")
    rows = ({"value": 2},)
    second = replace(second, rowset=rows,
                     execution_receipt=second.execution_receipt.model_copy(update={"rowset_sha256": rowset_sha256(rows)}))
    result = _choose(_candidate(), second)
    assert result.needs_hitl and result.winner is None and result.reason == "rowset_divergence"


def test_capacity_and_budget_preflight_never_select_one_on_exhaustion() -> None:
    policy = bootstrap_routing_budget_policy()
    for route in ("fast", "standard"):
        assert candidate_capacity(count=2, route=route, budget=policy.routes[route], capacity_available=True)
    assert candidate_capacity(count=3, route="deep", budget=policy.routes["deep"], capacity_available=True)
    result = select_candidate([_candidate(), _candidate("b")], route="deep", budget=policy.routes["deep"])
    assert result.reason == "candidate_capacity_unavailable" and result.winner is None
    assert candidate_capacity(count=2, route="deep", capacity_available=True,
                              budget=policy.routes["fast"]) == "candidate_budget_exhausted"


def test_evidence_validation_and_safe_decision() -> None:
    candidate = _candidate()
    verifier = _verifier(candidate)
    assert verifier.verify(replace(candidate, gates=())).failure_code == "gate_order_invalid"
    assert verifier.verify(replace(candidate, sql="DELETE FROM ai_views.x")).failure_code == "sql_denied"
    assert verifier.verify(replace(candidate, sql="SELECT 2 AS value")).failure_code == "sql_fingerprint_mismatch"
    assert verifier.verify(replace(candidate, rowset=())).failure_code == "execution_evidence_mismatch"
    payload = str(asdict(_choose(candidate)))
    for secret in ("SELECT", "rowset':", "params", "consensus_ratio", "score"):
        assert secret not in payload
    assert "SELECT" not in repr(candidate)


def test_metadata_rejects_invalid_estimates_and_digest() -> None:
    with pytest.raises(ValueError):
        _candidate(estimated_cost=float("nan"))
    with pytest.raises(ValueError):
        _candidate(semantic_signature="invalid")


@pytest.mark.parametrize("field,value", [
    ("semantic_signature", "f" * 64), ("source_kind", "approved_aggregate"),
    ("policy_version", ""), ("policy_outcome", "deny"), ("estimated_rows", 10),
    ("plan_cost", 10.0), ("sql_fingerprint", "f" * 64), ("error_taxonomy", "denied"),
])
def test_receipt_cannot_attest_different_candidate(field: str, value: Any) -> None:
    candidate = _candidate()
    candidate = replace(candidate, execution_receipt=candidate.execution_receipt.model_copy(update={field: value}))
    assert _verifier(candidate).verify(candidate).failure_code == "execution_evidence_mismatch"


def test_malformed_evidence_is_safe_rejection() -> None:
    candidate = _candidate()
    assert _verifier(candidate).verify(replace(candidate, rowset=({"value": float("nan")},))).failure_code == "rowset_invalid"
    assert _verifier(candidate).verify(replace(candidate, gates=tuple(reversed(candidate.gates)))).failure_code == "gate_order_invalid"
    assert _verifier(candidate).verify(replace(candidate, gates=(*candidate.gates, candidate.gates[0]))).failure_code == "gate_order_invalid"
    assert _choose().status == "rejected"
    assert _choose(candidate, candidate).reason == "duplicate_candidate_id"
    assert _choose(candidate).status == "accepted"
    assert _choose(candidate, _candidate("b", semantic_signature="b" * 64)).status == "hitl"


def test_online_candidate_contract_has_no_weighted_score() -> None:
    import ast
    from pathlib import Path

    from src.nl2sql.contracts import QueryCandidate

    # Inspect only candidate evaluation. Routing confidence/risk is independent.
    path = Path(__file__).resolve().parents[2] / "src/nl2sql/orchestration/candidates.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    names.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    assert not names & {"score", "consensus_ratio", "weighted_score"}
    assert "score" not in QueryCandidate.model_fields
    assert not any(isinstance(node, ast.Constant) and node.value in (0.78, 0.82, 0.88)
                   for node in ast.walk(tree))


@pytest.mark.parametrize("field,value", [
    ("policy_version", "forged.v2"), ("readonly_role", "admin"),
    ("source_id", "other.source"), ("source_checkpoint", "other.checkpoint"),
    ("selection_reason", "approved_detail_fallback"), ("source_degradation", ("aggregate_stale",)),
])
def test_authority_is_independent_of_candidate_receipt(field: str, value: Any) -> None:
    original = _candidate()
    verifier = _verifier(original)
    forged = replace(original, execution_receipt=original.execution_receipt.model_copy(update={field: value}))
    assert verifier.verify(forged).failure_code == "execution_evidence_mismatch"
    assert CandidateVerifier().verify(original).failure_code == "candidate_authority_missing"
    forged = replace(original, metadata=replace(original.metadata, semantic_signature="f" * 64))
    assert verifier.verify(forged).failure_code == "candidate_authority_mismatch"


@pytest.mark.parametrize("changes", [
    {"execution_receipt": None}, {"execution_receipt": object()}, {"metadata": None},
    {"gates": None}, {"gates": (object(),)}, {"rowset": None}, {"sql": None},
])
def test_malformed_candidate_boundary_returns_safe_failure(changes: dict[str, Any]) -> None:
    original = _candidate()
    malformed = replace(original, **changes)
    result = _verifier(original).verify(malformed)
    assert result.failure_code == "candidate_invalid" and result.rowset_sha256 is None
    decision = select_candidate([malformed], route="deep",
                                budget=bootstrap_routing_budget_policy().routes["deep"],
                                verifier=_verifier(original))
    assert decision.status == "rejected"
    assert _verifier(original).verify(object()).failure_code == "candidate_invalid"


def test_malformed_rowset_shape_is_not_an_exception() -> None:
    original = _candidate()
    malformed = replace(original, rowset=(None,))
    assert _verifier(original).verify(malformed).failure_code == "rowset_invalid"
