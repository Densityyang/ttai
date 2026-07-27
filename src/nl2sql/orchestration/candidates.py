"""Typed candidate scoring and SHA-256 rowset consensus."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Sequence

Route = Literal["fast", "standard", "deep"]


@dataclass(frozen=True)
class CandidateSignals:
    semantic_alignment: float
    schema_validity: float
    execution_signal: float
    cost_score: float
    approved_experience: float
    policy_allowed: bool
    parsed: bool
    readonly: bool
    explain_allowed: bool

    def hard_gate_passed(self) -> bool:
        return self.policy_allowed and self.parsed and self.readonly and self.explain_allowed


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    sql: str
    rowset: tuple[dict[str, Any], ...]
    signals: CandidateSignals


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    rowset_sha256: str
    consensus_ratio: float
    score: float


@dataclass(frozen=True)
class CandidateReceipt:
    """Replay-safe evidence of how a typed candidate was evaluated."""

    candidate_id: str
    rowset_sha256: str
    hard_gates_passed: bool
    consensus_ratio: float
    score: float


@dataclass(frozen=True)
class CandidateDecision:
    winner: ScoredCandidate | None
    needs_hitl: bool
    reason: str
    receipts: tuple[CandidateReceipt, ...] = ()


def rowset_sha256(rowset: Sequence[dict[str, Any]]) -> str:
    """Hash a canonical, typed rowset rather than a natural-language rendering."""
    normalized = [_normalize_value(row) for row in rowset]
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_candidate(candidates: Sequence[Candidate], *, route: Route) -> CandidateDecision:
    hashes = {candidate.candidate_id: rowset_sha256(candidate.rowset) for candidate in candidates}
    eligible = [candidate for candidate in candidates if candidate.signals.hard_gate_passed()]
    if not eligible:
        return CandidateDecision(
            None,
            True,
            "no_candidate_passed_hard_gates",
            tuple(
                CandidateReceipt(candidate.candidate_id, hashes[candidate.candidate_id], False, 0.0, 0.0)
                for candidate in candidates
            ),
        )
    counts: dict[str, int] = {}
    for candidate in eligible:
        digest = hashes[candidate.candidate_id]
        counts[digest] = counts.get(digest, 0) + 1
    total = len(eligible)
    scored = sorted(
        (
            ScoredCandidate(
                candidate=candidate,
                rowset_sha256=hashes[candidate.candidate_id],
                consensus_ratio=counts[hashes[candidate.candidate_id]] / total,
                score=_score(candidate.signals, counts[hashes[candidate.candidate_id]] / total),
            )
            for candidate in eligible
        ),
        key=lambda item: (-item.score, item.candidate.candidate_id),
    )
    winner = scored[0]
    threshold = {"fast": 0.88, "standard": 0.82, "deep": 0.78}[route]
    margin = winner.score - scored[1].score if len(scored) > 1 else 1.0
    accepted = winner.score >= threshold
    if route == "standard":
        accepted = accepted and margin >= 0.10
    if route == "deep":
        accepted = accepted and winner.consensus_ratio >= 2 / 3
    scored_by_id = {item.candidate.candidate_id: item for item in scored}
    receipts = tuple(
        CandidateReceipt(
            candidate_id=candidate.candidate_id,
            rowset_sha256=hashes[candidate.candidate_id],
            hard_gates_passed=candidate.signals.hard_gate_passed(),
            consensus_ratio=scored_by_id[candidate.candidate_id].consensus_ratio
            if candidate.candidate_id in scored_by_id
            else 0.0,
            score=scored_by_id[candidate.candidate_id].score if candidate.candidate_id in scored_by_id else 0.0,
        )
        for candidate in candidates
    )
    return CandidateDecision(
        winner,
        not accepted,
        "accepted" if accepted else "clarification_or_hitl_required",
        receipts,
    )


def _score(signals: CandidateSignals, consensus_ratio: float) -> float:
    return round(
        0.30 * signals.semantic_alignment
        + 0.20 * signals.schema_validity
        + 0.20 * consensus_ratio
        + 0.15 * signals.execution_signal
        + 0.10 * signals.cost_score
        + 0.05 * signals.approved_experience,
        6,
    )


def _normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"$float": format(value, ".17g")}
    if isinstance(value, Decimal):
        return {"$decimal": format(value, "f")}
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return {"$type": type(value).__name__, "$value": str(value)}
