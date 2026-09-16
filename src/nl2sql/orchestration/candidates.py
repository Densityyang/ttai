"""Deterministic candidate verification; only safe evidence leaves the request."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from src.nl2sql.contracts import ExecutionReceipt, RouteBudget
from src.nl2sql.infra.governance.query_gateway import PolicyEngine, QueryPolicyError

Route = Literal["fast", "standard", "deep"]


GateName = Literal["policy", "semantic", "sql", "explain", "execution"]
GATE_ORDER: tuple[GateName, ...] = ("policy", "semantic", "sql", "explain", "execution")


@dataclass(frozen=True)
class GateOutcome:
    gate: GateName
    passed: bool

    def __post_init__(self) -> None:
        if self.gate not in GATE_ORDER or type(self.passed) is not bool:
            raise ValueError("invalid hard gate evidence")


@dataclass(frozen=True)
class CandidateMetadata:
    candidate_id: str
    origin: Literal["deterministic", "semantic"]
    sql_fingerprint: str
    semantic_signature: str
    relation_ids: tuple[str, ...]
    source_kind: Literal["approved_aggregate", "approved_detail"]
    join_hops: int
    estimated_rows: int
    estimated_cost: float

    def __post_init__(self) -> None:
        if (type(self.join_hops) is not int or type(self.estimated_rows) is not int
                or type(self.estimated_cost) not in {int, float}
                or not isinstance(self.relation_ids, tuple)):
            raise ValueError("invalid candidate estimate types")
        for digest in (self.sql_fingerprint, self.semantic_signature):
            if len(digest) != 64 or set(digest) - set("0123456789abcdef"):
                raise ValueError("candidate evidence requires SHA256")
        safe_id = r"[a-z][a-z0-9_.-]{0,127}"
        if (re.fullmatch(safe_id, self.candidate_id) is None or not self.relation_ids
                or any(re.fullmatch(safe_id, item) is None for item in self.relation_ids)
                or self.origin not in {"deterministic", "semantic"}
                or self.source_kind not in {"approved_aggregate", "approved_detail"}
                or len(set(self.relation_ids)) != len(self.relation_ids)
                or self.join_hops < 0 or self.estimated_rows < 0
                or not math.isfinite(self.estimated_cost) or self.estimated_cost < 0):
            raise ValueError("invalid candidate metadata")


@dataclass(frozen=True)
class Candidate:
    metadata: CandidateMetadata
    gates: tuple[GateOutcome, ...]
    execution_receipt: ExecutionReceipt = field(repr=False)
    sql: str = field(repr=False)
    rowset: tuple[dict[str, Any], ...] = field(repr=False)
    bind_params: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CandidateExpectedEvidence:
    """Request-scoped compiler/Gateway authority, supplied independently of candidates."""

    metadata: CandidateMetadata
    policy_version: str
    readonly_role: str
    source_id: str
    source_checkpoint: str | None
    selection_reason: str
    source_degradation: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        replace(self.metadata)
        if not self.policy_version or not self.readonly_role or not self.source_id:
            raise ValueError("expected authority must be explicit")


@dataclass(frozen=True)
class CandidateReceipt:
    """Replay-safe evidence of how a typed candidate was evaluated."""

    metadata: CandidateMetadata
    rowset_sha256: str | None
    gates: tuple[GateOutcome, ...]
    failure_code: str | None = None

    @property
    def hard_gates_passed(self) -> bool:
        return self.failure_code is None


@dataclass(frozen=True)
class CandidateDecision:
    winner: CandidateReceipt | None
    needs_hitl: bool
    reason: str
    receipts: tuple[CandidateReceipt, ...] = ()

    @property
    def status(self) -> Literal["accepted", "hitl", "rejected"]:
        if self.winner is not None:
            return "accepted"
        return "hitl" if self.needs_hitl else "rejected"


def rowset_sha256(rowset: Sequence[dict[str, Any]]) -> str:
    """Hash a canonical, typed rowset rather than a natural-language rendering."""
    if any(not isinstance(row, dict) or any(not isinstance(key, str) for key in row) for row in rowset):
        raise ValueError("rowset must contain string-keyed rows")
    if rowset and any(set(row) != set(rowset[0]) for row in rowset):
        raise ValueError("rowset columns must be consistent")
    normalized = sorted(
        (_normalize_value(row) for row in rowset),
        key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_capacity(*, count: int, route: Route, budget: RouteBudget,
                       capacity_available: bool) -> str | None:
    """Must be called before generation/execution; repeat at decision boundary."""
    if route not in {"fast", "standard", "deep"} or count < 1:
        return "candidate_count_invalid"
    if count > (2 if route == "deep" else 1):
        return "candidate_limit_exceeded"
    if count > budget.max_sql_candidates or count > budget.max_sql_executions:
        return "candidate_budget_exhausted"
    if count == 2 and not capacity_available:
        return "candidate_capacity_unavailable"
    return None


class CandidateVerifier:
    """Validate ordered evidence from trusted compiler/Gateway adapters.

    This is an evidence protocol, not permission for a model to assert gates.
    SQL policy and EXPLAIN remain enforced by QueryGateway during execution.
    """

    def __init__(self, policy: PolicyEngine | None = None, *,
                 expected: Mapping[str, CandidateExpectedEvidence] | None = None) -> None:
        self._policy = policy or PolicyEngine()
        self._expected = {key: replace(value, metadata=replace(value.metadata))
                          for key, value in (expected or {}).items()}
        if any(key != value.metadata.candidate_id for key, value in self._expected.items()):
            raise ValueError("expected candidate identity mismatch")

    def verify(self, candidate: object) -> CandidateReceipt:
        # Only malformed boundary data is converted to a safe error. Failures in
        # compiler/policy implementation below are not hidden by a broad catch.
        metadata = CandidateMetadata("invalid", "deterministic", "0" * 64, "0" * 64,
                                     ("invalid",), "approved_detail", 0, 0, 0)
        try:
            if type(candidate) is not Candidate:
                raise TypeError("candidate type")
            if type(candidate.metadata) is not CandidateMetadata:
                raise TypeError("metadata type")
            metadata = replace(candidate.metadata)
            if (not isinstance(candidate.sql, str) or not isinstance(candidate.bind_params, dict)
                    or any(not isinstance(key, str) for key in candidate.bind_params)
                    or not isinstance(candidate.rowset, (list, tuple))
                    or not isinstance(candidate.gates, tuple)
                    or any(type(item) is not GateOutcome for item in candidate.gates)
                    or not isinstance(candidate.execution_receipt, ExecutionReceipt)):
                raise TypeError("candidate payload type")
            gates = tuple(replace(item) for item in candidate.gates)
            receipt = ExecutionReceipt.model_validate(candidate.execution_receipt.model_dump())
        except (TypeError, ValueError, AttributeError):
            return CandidateReceipt(metadata, None, (), "candidate_invalid")
        expected = self._expected.get(metadata.candidate_id)
        if expected is None:
            return CandidateReceipt(metadata, None, (), "candidate_authority_missing")
        if metadata != expected.metadata:
            return CandidateReceipt(metadata, None, (), "candidate_authority_mismatch")
        outcomes: list[GateOutcome] = []
        for position, name in enumerate(GATE_ORDER):
            if position >= len(gates) or gates[position].gate != name:
                return CandidateReceipt(metadata, None, tuple(outcomes), "gate_order_invalid")
            outcome = gates[position]
            outcomes.append(outcome)
            if not outcome.passed:
                return CandidateReceipt(metadata, None, tuple(outcomes), f"{name}_denied")
            if name == "sql":
                try:
                    prepared = self._policy.prepare(candidate.sql)
                    self._policy.validate_params(prepared, candidate.bind_params)
                except QueryPolicyError:
                    return CandidateReceipt(metadata, None, tuple(outcomes), "sql_denied")
                if prepared.fingerprint != metadata.sql_fingerprint:
                    return CandidateReceipt(metadata, None, tuple(outcomes), "sql_fingerprint_mismatch")
        if len(gates) != len(GATE_ORDER):
            return CandidateReceipt(metadata, None, tuple(outcomes), "gate_order_invalid")
        try:
            digest = rowset_sha256(candidate.rowset)
        except (ValueError, TypeError):
            return CandidateReceipt(metadata, None, tuple(outcomes), "rowset_invalid")
        if (receipt.policy_outcome != "allow" or receipt.error_taxonomy is not None
                or receipt.policy_version != expected.policy_version
                or receipt.readonly_role != expected.readonly_role
                or receipt.source_id != expected.source_id
                or receipt.source_checkpoint != expected.source_checkpoint
                or receipt.selection_reason != expected.selection_reason
                or receipt.source_degradation != expected.source_degradation
                or receipt.sql_fingerprint != metadata.sql_fingerprint
                or receipt.semantic_signature != metadata.semantic_signature
                or receipt.source_kind != metadata.source_kind
                or receipt.rowset_sha256 != digest or receipt.row_count != len(candidate.rowset)
                or receipt.estimated_rows != metadata.estimated_rows
                or receipt.plan_cost != metadata.estimated_cost):
            return CandidateReceipt(metadata, None, tuple(outcomes), "execution_evidence_mismatch")
        return CandidateReceipt(metadata, digest, tuple(outcomes))


def select_candidate(candidates: Sequence[Candidate], *, route: Route, budget: RouteBudget,
                     capacity_available: bool = False, verifier: CandidateVerifier | None = None) -> CandidateDecision:
    failure = candidate_capacity(count=len(candidates), route=route, budget=budget,
                                 capacity_available=capacity_available)
    if failure:
        return CandidateDecision(None, False, failure)
    verifier = verifier or CandidateVerifier()
    receipts = tuple(verifier.verify(item) for item in candidates)
    valid_ids = [item.metadata.candidate_id for item in receipts if item.failure_code != "candidate_invalid"]
    if len(set(valid_ids)) != len(valid_ids):
        return CandidateDecision(None, False, "duplicate_candidate_id", receipts)
    receipts = tuple(item if item.metadata.join_hops <= budget.max_join_hops
                     else CandidateReceipt(item.metadata, None, (), "join_budget_exhausted") for item in receipts)
    eligible = [item for item in receipts if item.hard_gates_passed]
    if not eligible:
        return CandidateDecision(None, False, "no_candidate_passed_hard_gates", receipts)
    if len({item.metadata.semantic_signature for item in eligible}) > 1:
        return CandidateDecision(None, True, "semantic_divergence", receipts)
    if len({item.rowset_sha256 for item in eligible}) > 1:
        return CandidateDecision(None, True, "rowset_divergence", receipts)
    winner = min(eligible, key=lambda item: (
        item.metadata.source_kind != "approved_aggregate", len(item.metadata.relation_ids),
        item.metadata.join_hops, item.metadata.estimated_rows, item.metadata.estimated_cost,
        item.metadata.candidate_id,
    ))
    return CandidateDecision(winner, False, "accepted", receipts)


def _normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite float is not canonical")
        return {"$float": format(value, ".17g")}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite decimal is not canonical")
        # String normalization avoids Decimal.normalize() context rounding.
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return {"$decimal": "0" if value == 0 else rendered}
    if isinstance(value, datetime):
        aware = value.tzinfo is not None and value.utcoffset() is not None
        return {"$datetime_utc" if aware else "$datetime_local": (
            value.astimezone(UTC) if aware else value
        ).isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical object keys must be strings")
        # Tag containers to prevent a business object impersonating a scalar tag.
        return {"$object": [[key, _normalize_value(item)] for key, item in sorted(value.items())]}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    raise ValueError("unsupported canonical value type")
