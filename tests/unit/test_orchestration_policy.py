from __future__ import annotations

import pytest

from src.nl2sql.contracts import RouteBudgetRecord
from src.nl2sql.orchestration.budget import (
    BudgetExceeded,
    CallBudget,
    RouteBudgetLedger,
    bootstrap_routing_budget_policy,
    should_stop,
)
from src.nl2sql.orchestration.routing import (
    RiskSignals,
    bootstrap_route_policy,
    choose_route,
)


@pytest.mark.parametrize(
    ("signals", "confidence", "route", "reason"),
    [
        (RiskSignals(), 0.90, "fast", "low_risk_high_confidence"),
        (RiskSignals(requires_model=True), 0.90, "standard", "model_required"),
        (RiskSignals(table_count=2), 0.90, "standard", "bounded_risk"),
        (
            RiskSignals(
                restricted_data=True,
                table_count=3,
                ambiguous_metric_or_filter=True,
            ),
            0.90,
            "deep",
            "high_risk_or_low_confidence",
        ),
        (RiskSignals(), 0.40, "deep", "high_risk_or_low_confidence"),
    ],
)
def test_bootstrap_route_truth_table_is_replayable(
    signals: RiskSignals,
    confidence: float,
    route: str,
    reason: str,
) -> None:
    policy = bootstrap_route_policy()
    decision = choose_route(signals=signals, confidence=confidence, policy=policy)

    assert decision.route == route
    assert decision.reason == reason
    assert decision.policy_version == "route.bootstrap.v1"
    assert len(decision.policy_checksum) == 64
    assert (
        choose_route(signals=signals, confidence=confidence, policy=policy).replay_record()
        == decision.replay_record()
    )


def test_bootstrap_budget_policy_matches_the_versioned_plan_limits() -> None:
    policy = bootstrap_routing_budget_policy()

    assert policy.state == "bootstrap"
    assert policy.routes["fast"].model_dump() == {
        "deadline_ms": 4_000,
        "max_model_calls": 0,
        "max_sql_candidates": 1,
        "max_sql_executions": 1,
        "max_join_hops": 0,
        "max_repairs": 0,
    }
    assert policy.routes["standard"].model_dump() == {
        "deadline_ms": 10_000,
        "max_model_calls": 3,
        "max_sql_candidates": 2,
        "max_sql_executions": 2,
        "max_join_hops": 1,
        "max_repairs": 1,
    }
    assert policy.routes["deep"].model_dump() == {
        "deadline_ms": 30_000,
        "max_model_calls": 5,
        "max_sql_candidates": 2,
        "max_sql_executions": 2,
        "max_join_hops": 2,
        "max_repairs": 1,
    }
    assert policy.reserve_ms == 800
    assert policy.max_same_sql == 2
    assert policy.max_same_error == 2
    assert policy.max_retryable_provider_errors == 1
    assert len(policy.checksum) == 64


def test_fast_budget_blocks_a_model_call_before_it_is_counted() -> None:
    ledger = RouteBudgetLedger(route="fast")

    with pytest.raises(BudgetExceeded, match="model_call_budget_exhausted"):
        ledger.begin_model_call()

    record = ledger.checkpoint_record()
    assert record.usage.model_calls == 0
    assert record.stop_reason == "model_call_budget_exhausted"


def test_standard_model_budget_is_a_hard_upper_bound() -> None:
    ledger = RouteBudgetLedger(route="standard")

    for _ in range(3):
        ledger.begin_model_call()
    with pytest.raises(BudgetExceeded, match="model_call_budget_exhausted"):
        ledger.begin_model_call()

    assert ledger.model_calls == 3
    assert ledger.stop_reason == "model_call_budget_exhausted"


def test_repeated_sql_and_error_taxonomy_stop_at_policy_thresholds() -> None:
    sql_ledger = RouteBudgetLedger(route="standard")
    assert sql_ledger.record_sql_candidate("sql-sha") is None
    assert sql_ledger.record_sql_candidate("sql-sha") == "repeated_sql"

    error_ledger = RouteBudgetLedger(route="deep")
    assert error_ledger.record_error("syntax_error") is None
    assert error_ledger.record_error("syntax_error") == "repeated_error"


def test_policy_denial_is_terminal_and_cannot_be_retried() -> None:
    ledger = RouteBudgetLedger(route="standard")
    ledger.begin_model_call()

    assert ledger.record_error("model_alias_not_allowed", policy_denied=True) == "policy_denied"
    with pytest.raises(BudgetExceeded, match="policy_denied"):
        ledger.begin_model_call()

    assert ledger.model_calls == 1


def test_budget_checkpoint_round_trip_preserves_loop_breakers_without_payloads() -> None:
    policy = bootstrap_routing_budget_policy()
    ledger = RouteBudgetLedger(route="deep", policy=policy)
    ledger.begin_model_call()
    ledger.record_sql_candidate("sql-sha")
    ledger.begin_sql_execution()
    ledger.observe_join_hops(2)
    ledger.begin_repair()
    ledger.record_error("provider_timeout", retryable_provider=True)

    record = RouteBudgetRecord.model_validate_json(ledger.checkpoint_record().model_dump_json())
    restored = RouteBudgetLedger.from_record(policy=policy, record=record)

    assert restored.checkpoint_record() == record
    serialized = record.model_dump_json()
    assert "SELECT" not in serialized
    assert "api_key" not in serialized


def test_deadline_reserve_and_loop_breaker_boundaries_use_the_active_policy() -> None:
    policy = bootstrap_routing_budget_policy()
    budget = CallBudget(
        deadline_ms=4_000,
        token_budget=10,
        cost_budget=1,
        max_attempts=2,
        _started_at=10.0,
    )

    assert should_stop(budget=budget, policy=policy, now=13.199) is None
    assert should_stop(budget=budget, policy=policy, now=13.201) == "deadline_reserve"
    assert (
        should_stop(
            budget=CallBudget(4_000, 10, 1, 2),
            policy=policy,
            sql_fingerprint_seen=2,
        )
        == "repeated_sql"
    )
