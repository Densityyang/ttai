"""Deterministic risk routing with replayable decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from src.nl2sql.contracts import PolicyLifecycle, RoutePolicy

Route = Literal["fast", "standard", "deep"]


@dataclass(frozen=True)
class RiskSignals:
    restricted_data: bool = False
    table_count: int = 1
    ambiguous_metric_or_filter: bool = False
    dynamic_calculation: bool = False
    unknown_explain_cost: bool = False
    requires_model: bool = False

    def __post_init__(self) -> None:
        if self.table_count < 1:
            raise ValueError("table count must be positive")

    @property
    def score(self) -> int:
        return (
            35 * int(self.restricted_data)
            + 20 * int(self.table_count >= 3)
            + 20 * int(self.ambiguous_metric_or_filter)
            + 15 * int(self.dynamic_calculation)
            + 10 * int(self.unknown_explain_cost)
        )


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    risk: int
    confidence: float
    reason: str
    signals: RiskSignals
    policy_version: str
    policy_state: PolicyLifecycle
    policy_checksum: str

    def replay_record(self) -> dict[str, object]:
        return {
            "route": self.route,
            "risk": self.risk,
            "confidence": self.confidence,
            "reason": self.reason,
            "signals": asdict(self.signals),
            "policy_version": self.policy_version,
            "policy_state": self.policy_state,
            "policy_checksum": self.policy_checksum,
        }


def bootstrap_route_policy() -> RoutePolicy:
    return RoutePolicy(
        version="route.bootstrap.v1",
        state="bootstrap",
        fast_max_risk=20,
        fast_min_confidence=0.85,
        fast_max_tables=1,
        standard_max_risk=60,
        standard_min_confidence=0.55,
    )


def choose_route(
    *,
    signals: RiskSignals,
    confidence: float,
    policy: RoutePolicy | None = None,
) -> RouteDecision:
    """Apply the v2 route policy without I/O or mutable global state."""
    if not 0 <= confidence <= 1:
        raise ValueError("route confidence must be between 0 and 1")
    resolved_policy = policy or bootstrap_route_policy()
    risk = signals.score
    fast_candidate = (
        risk <= resolved_policy.fast_max_risk
        and confidence >= resolved_policy.fast_min_confidence
        and signals.table_count <= resolved_policy.fast_max_tables
    )
    if fast_candidate and not signals.requires_model:
        route: Route = "fast"
        reason = "low_risk_high_confidence"
    elif (
        risk <= resolved_policy.standard_max_risk
        and confidence >= resolved_policy.standard_min_confidence
    ):
        route = "standard"
        reason = "model_required" if fast_candidate else "bounded_risk"
    else:
        route = "deep"
        reason = "high_risk_or_low_confidence"
    return RouteDecision(
        route=route,
        risk=risk,
        confidence=confidence,
        reason=reason,
        signals=signals,
        policy_version=resolved_policy.version,
        policy_state=resolved_policy.state,
        policy_checksum=resolved_policy.checksum,
    )
