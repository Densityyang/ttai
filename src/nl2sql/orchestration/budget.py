"""Request deadline, call budget, and deterministic early-stop rules."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


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

    def charge(self, *, input_tokens: int, output_tokens: int, cost: float) -> None:
        tokens = input_tokens + output_tokens
        if not self.can_charge(tokens=tokens, cost=cost):
            raise BudgetExceeded("model_call_budget_exceeded")
        self.attempts += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_used += cost


class BudgetExceeded(RuntimeError):
    pass


def should_stop(
    *,
    budget: CallBudget,
    sql_fingerprint_seen: int = 0,
    same_error_seen: int = 0,
    policy_denied: bool = False,
) -> str | None:
    if policy_denied:
        return "policy_denied"
    if budget.remaining_ms() <= budget.reserve_ms():
        return "deadline_reserve"
    if budget.attempts >= budget.max_attempts:
        return "attempt_budget_exhausted"
    if sql_fingerprint_seen >= 2:
        return "repeated_sql"
    if same_error_seen >= 2:
        return "repeated_error"
    return None
