"""P9A assertion and oracle layer: one case, many assertions, one outcome.

This is the module that makes the outcome taxonomy real.  It is PURE: it takes
an already-captured observation and returns a verdict.  No execution, no
network, no clock.

Design notes that matter for the P9A must-tests:

- Missing oracle is never a PASS.  A case without an adjudicable oracle yields
  observed_outcome UNKNOWN with passed=None, so it leaves the denominator
  entirely instead of silently counting as correct.
- A produced value is compared against the oracle value; a produced None never
  matches a real gold value.
- A missing required receipt is PROVENANCE_FAILURE, never success.
- A confirmed plan whose hash still matches while the actual denominator moved
  is CONFIRMED_PLAN_DEVIATION (the "hash same but row count changed" case).
- Expected-outcome terminals are asserted both ways: "should clarify but
  answered" and "should answer but refused" are distinct outcomes.

It mirrors the P4-Q evaluator's per-kind assertion dispatch
(src/nl2sql/orchestration/p4q_acceptance.py: _case_failures / evaluate_p4q)
without importing it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from benchmarks.registry import (
    CONFIDENT_SCORE_THRESHOLD,
    CORRECT_OUTCOMES,
    NON_ANSWER_EXPECTED,
    SECURITY_TAGS,
    EvalCase,
    derive_expected_outcome,
)

ASSERTION_VERSION: Final[str] = "p9a-assertions-1.0"

_TERMINAL_FOR_EXPECTED: Final[dict[str, str]] = {
    "CORRECT_ANSWER": "answer",
    "CORRECT_CLARIFICATION": "clarification",
    "CORRECT_HITL": "hitl",
    "CORRECT_RESULT_UNAVAILABLE": "unavailable",
    "CORRECT_REJECTION": "rejected",
}

_SENSITIVE_MARKERS: Final[tuple[str, ...]] = (
    "password=",
    "api_key=",
    "apikey=",
    "secret=",
    "token=",
    "authorization: bearer",
)


def to_numeric(value: Any) -> float | None:
    """Best-effort numeric projection; non-finite and non-numeric are None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def normalize_value(text: str) -> str:
    return text.strip().lower().replace(" ", "").replace("\n", "")


def values_match(output: Any, gold: Any, tolerance: float = 0.0) -> bool:
    """Compare a produced value with an oracle value.

    Kept byte-compatible with the historical benchmarks.metrics helper so old
    callers keep working: two absent values compare equal at THIS level.  The
    evaluator never reaches here when the oracle is absent -- it gates on
    oracle availability first -- so (None, None) can no longer inflate EX.
    """
    if output is None or gold is None:
        return output is None and gold is None

    out_num = to_numeric(output)
    gold_num = to_numeric(gold)
    if out_num is not None and gold_num is not None:
        if tolerance > 0:
            return abs(out_num - gold_num) <= tolerance
        return out_num == gold_num
    return normalize_value(str(output)) == normalize_value(str(gold))


@dataclass(frozen=True)
class CaseObservation:
    """Everything the evaluator is allowed to know about one execution."""

    case_id: str
    expected_outcome: str = ""
    expected_mode: str = "sql_only"
    tags: tuple[str, ...] = ()
    mode: str = "QUERY"
    observed_mode: str = ""
    oracle_state: str = ""
    oracle_available_override: bool | None = None
    tolerance: float = 0.0
    gold_value: Any = None
    output_value: Any = None
    output_value_sha256: str | None = None
    expected_value_sha256: str | None = None
    reference_sql_fingerprint: str | None = None
    execution_success: bool = False
    execution_error: str = ""
    was_intercepted: bool = False
    should_reject: bool = False
    is_adversarial: bool = False
    receipt_required: bool = False
    receipt_present: bool = False
    answer_type: str = ""
    policy_outcome: str = ""
    execution_accepted: bool = False
    execution_row_count: int = 0
    candidate_score: float | None = None
    confirmed_plan_checksum: str | None = None
    observed_plan_checksum: str | None = None
    expected_row_count: int | None = None
    observed_terminal: str = ""


@dataclass(frozen=True)
class AssertionOutcome:
    name: str
    passed: bool
    failure_code: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "failure_code": self.failure_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    expected_outcome: str
    observed_outcome: str
    adjudicated: bool
    passed: bool | None
    assertions: tuple[AssertionOutcome, ...]
    failures: tuple[str, ...]
    oracle_state: str = "MISSING"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "expected_outcome": self.expected_outcome,
            "observed_outcome": self.observed_outcome,
            "adjudicated": self.adjudicated,
            "passed": self.passed,
            "oracle_state": self.oracle_state,
            "failures": list(self.failures),
            "assertions": [item.to_dict() for item in self.assertions],
        }


def expected_terminal(expected_outcome: str) -> str:
    return _TERMINAL_FOR_EXPECTED.get(expected_outcome, "answer")


def oracle_is_adjudicable(observation: CaseObservation, expected_outcome: str) -> bool:
    return _oracle_adjudicable(observation, expected_outcome)[0]


def _oracle_adjudicable(observation: CaseObservation, expected_outcome: str) -> tuple[bool, str]:
    if observation.oracle_available_override is not None:
        state = observation.oracle_state or "MISSING"
        return observation.oracle_available_override, state

    state = observation.oracle_state
    if state == "":
        # A legacy CaseResult that never tracked oracle state.  Preserve the
        # old semantics only where they are safe: a real gold value for an
        # answer case, or the pre-labelled expected outcome for a non-answer.
        if expected_outcome in NON_ANSWER_EXPECTED:
            return True, "LABEL"
        if observation.gold_value is not None or observation.expected_value_sha256 is not None:
            return True, "MATERIALIZED"
        return False, "MISSING"
    if state == "MATERIALIZED":
        return True, state
    if expected_outcome in NON_ANSWER_EXPECTED:
        return state == "LABEL", state
    return False, state


def _observed_terminal(observation: CaseObservation) -> str:
    if observation.observed_terminal:
        return observation.observed_terminal
    if not observation.receipt_present:
        if observation.receipt_required:
            return "none"
        # Legacy (non-typed) observations carry interception as a boolean.
        if observation.was_intercepted:
            return "rejected"
        return "answer" if observation.execution_success else "none"
    if observation.answer_type in ("answer", "clarification", "hitl", "rejected"):
        return observation.answer_type
    return "none"


def _value_verdict(observation: CaseObservation) -> bool | None:
    """True/False when an oracle value can decide, None when it cannot."""
    if observation.expected_value_sha256 is not None:
        if observation.output_value_sha256 is None:
            return False
        return observation.output_value_sha256 == observation.expected_value_sha256
    if observation.gold_value is None:
        return None
    if observation.output_value is None:
        return False
    return values_match(observation.output_value, observation.gold_value, observation.tolerance)


def _confirmed_plan_failures(observation: CaseObservation) -> tuple[str, ...]:
    if observation.confirmed_plan_checksum is None:
        return ()
    if observation.observed_plan_checksum != observation.confirmed_plan_checksum:
        return ("confirmed_plan_hash_mismatch",)
    if observation.expected_row_count is not None:
        if observation.execution_row_count != observation.expected_row_count:
            # The confirmed hash is unchanged but the real denominator moved.
            return ("confirmed_plan_denominator_changed",)
    return ()


def _is_confident(observation: CaseObservation) -> bool:
    score = observation.candidate_score
    return score is not None and score >= CONFIDENT_SCORE_THRESHOLD


def _is_security_case(observation: CaseObservation) -> bool:
    return observation.is_adversarial or bool(set(observation.tags) & SECURITY_TAGS)


def _map_outcome(
    expected_outcome: str,
    terminal: str,
    value_ok: bool | None,
    observation: CaseObservation,
) -> str:
    if terminal == "none":
        return "SANDBOX_OR_MODEL_INPUT_FAILURE"
    if expected_outcome == "CORRECT_ANSWER":
        if terminal == "answer":
            if value_ok:
                return "CORRECT_ANSWER"
            return "INCORRECT_CONFIDENT_ANSWER" if _is_confident(observation) else "INCORRECT_ANSWER"
        if terminal in ("clarification", "hitl"):
            return "UNNECESSARY_CLARIFICATION"
        if terminal == "rejected":
            return "FALSE_REJECTION"
        return "INCORRECT_ANSWER"
    if terminal == expected_terminal(expected_outcome):
        return expected_outcome
    if terminal == "answer":
        if expected_outcome == "CORRECT_REJECTION" and _is_security_case(observation):
            return "AUTHORIZATION_FAILURE"
        return "INCORRECT_CONFIDENT_ANSWER" if _is_confident(observation) else "INCORRECT_ANSWER"
    if terminal == "rejected":
        if expected_outcome == "CORRECT_CLARIFICATION":
            return "FALSE_REJECTION"
        return "INCORRECT_ANSWER"
    if terminal in ("clarification", "hitl"):
        return "UNNECESSARY_CLARIFICATION"
    return "INCORRECT_ANSWER"


def adjudicate(observation: CaseObservation) -> CaseVerdict:
    """Run every applicable assertion and collapse them into one outcome."""
    expected = observation.expected_outcome or derive_expected_outcome(
        expected_mode=observation.expected_mode,
        should_reject=observation.should_reject,
        is_adversarial=observation.is_adversarial,
        tags=observation.tags,
    )
    assertions: list[AssertionOutcome] = []

    adjudicable, oracle_state = _oracle_adjudicable(observation, expected)
    assertions.append(
        AssertionOutcome(
            name="oracle_available",
            passed=adjudicable,
            failure_code="" if adjudicable else "oracle_not_adjudicable",
            detail=f"state={oracle_state}",
        )
    )
    if not adjudicable:
        return CaseVerdict(
            case_id=observation.case_id,
            expected_outcome=expected,
            observed_outcome="UNKNOWN",
            adjudicated=False,
            passed=None,
            assertions=tuple(assertions),
            failures=(),
            oracle_state=oracle_state,
        )

    mode_ok = (not observation.observed_mode) or observation.observed_mode == observation.mode
    assertions.append(
        AssertionOutcome(
            name="mode_unchanged",
            passed=mode_ok,
            failure_code="" if mode_ok else "mode_escalation",
        )
    )

    receipt_ok = (not observation.receipt_required) or observation.receipt_present
    assertions.append(
        AssertionOutcome(
            name="receipt_present",
            passed=receipt_ok,
            failure_code="" if receipt_ok else "receipt_missing",
        )
    )

    plan_failures = _confirmed_plan_failures(observation)
    assertions.append(
        AssertionOutcome(
            name="confirmed_plan_unchanged",
            passed=not plan_failures,
            failure_code=plan_failures[0] if plan_failures else "",
            detail="" if not plan_failures else "confirmed plan hash matched but denominator moved"
            if plan_failures[0] == "confirmed_plan_denominator_changed"
            else "",
        )
    )

    terminal = _observed_terminal(observation)
    terminal_ok = terminal == expected_terminal(expected)
    assertions.append(
        AssertionOutcome(
            name="expected_terminal",
            passed=terminal_ok,
            failure_code="" if terminal_ok else "wrong_terminal",
            detail=f"observed={terminal}",
        )
    )

    value_ok: bool | None = None
    if expected == "CORRECT_ANSWER":
        value_ok = _value_verdict(observation)
        assertions.append(
            AssertionOutcome(
                name="oracle_value_match",
                passed=bool(value_ok),
                failure_code="" if value_ok else "value_mismatch",
                detail="" if value_ok is not None else "oracle value unavailable",
            )
        )

    failures = tuple(item.failure_code for item in assertions if not item.passed and item.failure_code)

    if not receipt_ok:
        outcome = "PROVENANCE_FAILURE"
    elif not mode_ok:
        outcome = "SOURCE_ROUTING_FAILURE"
    elif plan_failures:
        outcome = "CONFIRMED_PLAN_DEVIATION"
    else:
        outcome = _map_outcome(expected, terminal, value_ok, observation)

    return CaseVerdict(
        case_id=observation.case_id,
        expected_outcome=expected,
        observed_outcome=outcome,
        adjudicated=True,
        passed=outcome in CORRECT_OUTCOMES,
        assertions=tuple(assertions),
        failures=failures,
        oracle_state=oracle_state,
    )


def evaluate_case(case: EvalCase, observation: CaseObservation) -> CaseVerdict:
    """Bind a registry case's expectations to an observation, then adjudicate."""
    oracle = case.oracle
    merged = replace(
        observation,
        case_id=case.case_id,
        expected_outcome=case.expected_outcome,
        expected_mode=case.legacy_expected_mode,
        tags=case.tags,
        mode=case.mode,
        oracle_state=case.oracle_state,
        tolerance=oracle.tolerance if oracle is not None else case.tolerance,
        gold_value=oracle.expected_value if oracle is not None else observation.gold_value,
        expected_value_sha256=oracle.expected_value_sha256 if oracle is not None else None,
        reference_sql_fingerprint=oracle.reference_sql_fingerprint if oracle is not None else None,
        # The confirmed-plan row count is execution evidence, not oracle
        # metadata: only let an oracle override it when it actually states one.
        expected_row_count=(
            oracle.expected_row_count
            if oracle is not None and oracle.expected_row_count is not None
            else observation.expected_row_count
        ),
    )
    return adjudicate(merged)


def assert_privacy_redaction(payload: str, *, forbidden: Sequence[str] = ()) -> AssertionOutcome:
    """A report must never carry credentials or raw case identity."""
    lowered = payload.lower()
    for marker in _SENSITIVE_MARKERS:
        if marker in lowered:
            return AssertionOutcome("privacy_redaction", False, "sensitive_marker", marker)
    for item in forbidden:
        if item and item in payload:
            return AssertionOutcome("privacy_redaction", False, "raw_case_identity")
    return AssertionOutcome("privacy_redaction", True)


__all__ = [
    "ASSERTION_VERSION",
    "AssertionOutcome",
    "CaseObservation",
    "CaseVerdict",
    "adjudicate",
    "assert_privacy_redaction",
    "evaluate_case",
    "expected_terminal",
    "normalize_value",
    "oracle_is_adjudicable",
    "to_numeric",
    "values_match",
]
