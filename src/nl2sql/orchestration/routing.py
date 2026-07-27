"""Deterministic risk routing with replayable decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Route = Literal["fast", "standard", "deep"]


@dataclass(frozen=True)
class RiskSignals:
    restricted_data: bool = False
    table_count: int = 1
    ambiguous_metric_or_filter: bool = False
    dynamic_calculation: bool = False
    unknown_explain_cost: bool = False

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

    def replay_record(self) -> dict[str, object]:
        return {
            "route": self.route,
            "risk": self.risk,
            "confidence": self.confidence,
            "reason": self.reason,
            "signals": asdict(self.signals),
        }


def choose_route(*, signals: RiskSignals, confidence: float) -> RouteDecision:
    """Apply the v2 route policy without I/O or mutable global state."""
    risk = signals.score
    if risk <= 20 and confidence >= 0.85 and signals.table_count <= 1:
        route: Route = "fast"
        reason = "low_risk_high_confidence"
    elif risk <= 60 and confidence >= 0.55:
        route = "standard"
        reason = "bounded_risk"
    else:
        route = "deep"
        reason = "high_risk_or_low_confidence"
    return RouteDecision(route, risk, confidence, reason, signals)
