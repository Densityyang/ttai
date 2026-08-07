"""Request deadline, route budgets, call accounting, and deterministic early stop."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from src.nl2sql.contracts import (
    RouteBudget,
    RouteBudgetRecord,
    RouteBudgetUsage,
    RouteName,
    RoutingBudgetPolicy,
)


def bootstrap_routing_budget_policy() -> RoutingBudgetPolicy:
    """Return the versioned bootstrap limits from MASTER_PR_PLAN_V3."""

    return RoutingBudgetPolicy(
        version="routing-budget.bootstrap.v1",
        state="bootstrap",
        routes={
            "fast": RouteBudget(
                deadline_ms=4_000,
                max_model_calls=0,
                max_sql_candidates=1,
                max_sql_executions=1,
                max_join_hops=0,
                max_repairs=0,
            ),
            "standard": RouteBudget(
                deadline_ms=10_000,
                max_model_calls=3,
                max_sql_candidates=2,
                max_sql_executions=2,
                max_join_hops=1,
                max_repairs=1,
            ),
            "deep": RouteBudget(
                deadline_ms=30_000,
                max_model_calls=5,
                max_sql_candidates=2,
                max_sql_executions=2,
                max_join_hops=2,
                max_repairs=1,
            ),
        },
        reserve_ms=800,
        max_same_sql=2,
        max_same_error=2,
        max_retryable_provider_errors=1,
    )


@dataclass
class CallBudget:
    deadline_ms: int
    token_budget: int
    cost_budget: float
    max_attempts: int
    _started_at: float = 0.0
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_used: float = 0.0

    def __post_init__(self) -> None:
        if self._started_at == 0.0:
            self._started_at = monotonic()

    @property
    def tokens_used(self) -> int:
        return self.input_tokens + self.output_tokens

    def remaining_ms(self, *, now: float | None = None) -> int:
        elapsed_ms = int(((now if now is not None else monotonic()) - self._started_at) * 1000)
        return max(0, self.deadline_ms - elapsed_ms)

    def reserve_ms(self) -> int:
        return max(1, self.deadline_ms // 5)

    def can_charge(self, *, tokens: int, cost: float) -> bool:
        return (
            tokens >= 0
            and cost >= 0
            and self.tokens_used + tokens <= self.token_budget
            and self.cost_used + cost <= self.cost_budget
        )

    def begin_attempt(self) -> None:
        if self.attempts >= self.max_attempts:
            raise BudgetExceeded("attempt_budget_exhausted")
        self.attempts += 1

    def charge(self, *, input_tokens: int, output_tokens: int, cost: float) -> None:
        tokens = input_tokens + output_tokens
        if not self.can_charge(tokens=tokens, cost=cost):
            raise BudgetExceeded("model_call_budget_exceeded")
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_used += cost


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class RouteBudgetLedger:
    """Mutable request-local accounting with a typed, secret-free checkpoint form."""

    route: RouteName
    policy: RoutingBudgetPolicy = field(default_factory=bootstrap_routing_budget_policy)
    model_calls: int = 0
    sql_candidates: int = 0
    sql_executions: int = 0
    join_hops: int = 0
    repairs: int = 0
    retryable_provider_errors: int = 0
    sql_fingerprint_counts: dict[str, int] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None

    @property
    def limits(self) -> RouteBudget:
        return self.policy.routes[self.route]

    @classmethod
    def from_record(
        cls,
        *,
        policy: RoutingBudgetPolicy,
        record: RouteBudgetRecord | dict[str, object],
    ) -> RouteBudgetLedger:
        parsed = record if isinstance(record, RouteBudgetRecord) else RouteBudgetRecord.model_validate(record)
        if parsed.policy_version != policy.version or parsed.policy_checksum != policy.checksum:
            raise ValueError("checkpoint routing budget policy does not match the active policy")
        if parsed.limits != policy.routes[parsed.route]:
            raise ValueError("checkpoint route limits do not match the active policy")
        usage = parsed.usage
        return cls(
            route=parsed.route,
            policy=policy,
            model_calls=usage.model_calls,
            sql_candidates=usage.sql_candidates,
            sql_executions=usage.sql_executions,
            join_hops=usage.join_hops,
            repairs=usage.repairs,
            retryable_provider_errors=usage.retryable_provider_errors,
            sql_fingerprint_counts=dict(parsed.sql_fingerprint_counts),
            error_counts=dict(parsed.error_counts),
            stop_reason=parsed.stop_reason,
        )

    def begin_model_call(self) -> None:
        self._increment("model_calls", self.limits.max_model_calls, "model_call_budget_exhausted")

    def record_sql_candidate(self, fingerprint: str) -> str | None:
        normalized = fingerprint.strip()
        if not normalized:
            raise ValueError("SQL fingerprint must be non-empty")
        self._increment(
            "sql_candidates",
            self.limits.max_sql_candidates,
            "sql_candidate_budget_exhausted",
        )
        count = self.sql_fingerprint_counts.get(normalized, 0) + 1
        self.sql_fingerprint_counts[normalized] = count
        if count >= self.policy.max_same_sql:
            self.halt("repeated_sql")
        return self.stop_reason

    def begin_sql_execution(self) -> None:
        self._increment(
            "sql_executions",
            self.limits.max_sql_executions,
            "sql_execution_budget_exhausted",
        )

    def observe_join_hops(self, hops: int) -> None:
        self._ensure_active()
        if hops < 0:
            raise ValueError("join hops cannot be negative")
        if hops > self.limits.max_join_hops:
            self.halt("join_hop_budget_exhausted")
            raise BudgetExceeded("join_hop_budget_exhausted")
        self.join_hops = max(self.join_hops, hops)

    def begin_repair(self) -> None:
        self._increment("repairs", self.limits.max_repairs, "repair_budget_exhausted")

    def record_provider_retries(self, retries: int) -> str | None:
        self._ensure_active()
        if retries < 0:
            raise ValueError("provider retries cannot be negative")
        self.retryable_provider_errors += retries
        if self.retryable_provider_errors > self.policy.max_retryable_provider_errors:
            self.halt("retryable_provider_error_budget_exhausted")
        return self.stop_reason

    def record_error(
        self,
        taxonomy: str,
        *,
        policy_denied: bool = False,
        retryable_provider: bool = False,
    ) -> str | None:
        normalized = taxonomy.strip()
        if not normalized:
            raise ValueError("error taxonomy must be non-empty")
        self._ensure_active()
        count = self.error_counts.get(normalized, 0) + 1
        self.error_counts[normalized] = count
        if retryable_provider:
            self.record_provider_retries(1)
        if policy_denied:
            self.halt("policy_denied")
        elif count >= self.policy.max_same_error:
            self.halt("repeated_error")
        return self.stop_reason

    def halt(self, reason: str) -> str:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("stop reason must be non-empty")
        if self.stop_reason is None:
            self.stop_reason = normalized
        return self.stop_reason

    def checkpoint_record(self) -> RouteBudgetRecord:
        return RouteBudgetRecord(
            policy_version=self.policy.version,
            policy_state=self.policy.state,
            policy_checksum=self.policy.checksum,
            route=self.route,
            limits=self.limits,
            usage=RouteBudgetUsage(
                model_calls=self.model_calls,
                sql_candidates=self.sql_candidates,
                sql_executions=self.sql_executions,
                join_hops=self.join_hops,
                repairs=self.repairs,
                retryable_provider_errors=self.retryable_provider_errors,
            ),
            sql_fingerprint_counts=dict(self.sql_fingerprint_counts),
            error_counts=dict(self.error_counts),
            stop_reason=self.stop_reason,
        )

    def _ensure_active(self) -> None:
        if self.stop_reason is not None:
            raise BudgetExceeded(self.stop_reason)

    def _increment(self, attribute: str, limit: int, reason: str) -> None:
        self._ensure_active()
        current = int(getattr(self, attribute))
        if current >= limit:
            self.halt(reason)
            raise BudgetExceeded(reason)
        setattr(self, attribute, current + 1)


def should_stop(
    *,
    budget: CallBudget,
    sql_fingerprint_seen: int = 0,
    same_error_seen: int = 0,
    policy_denied: bool = False,
    policy: RoutingBudgetPolicy | None = None,
    now: float | None = None,
) -> str | None:
    resolved_policy = policy or bootstrap_routing_budget_policy()
    reserve_ms = (
        max(resolved_policy.reserve_ms, budget.reserve_ms())
        if policy is not None
        else budget.reserve_ms()
    )
    if policy_denied:
        return "policy_denied"
    if budget.remaining_ms(now=now) <= reserve_ms:
        return "deadline_reserve"
    if budget.attempts >= budget.max_attempts:
        return "attempt_budget_exhausted"
    if sql_fingerprint_seen >= resolved_policy.max_same_sql:
        return "repeated_sql"
    if same_error_seen >= resolved_policy.max_same_error:
        return "repeated_error"
    return None
