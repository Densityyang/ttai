"""P9A unified case registry and oracle contract.

This module is the single source of truth for what a benchmark case *is*:
stable identity, revision, mode, tags, the pre-labelled expected outcome, and
the independent oracle that makes an adjudicated verdict possible.

It deliberately reuses the *shape* of the P4-Q acceptance evaluator
(src/nl2sql/orchestration/p4q_acceptance.py): a strict frozen case spec, a
revisioned registry, an oracle that must declare independence from the system
under test, and a closed vocabulary of verdicts.  It does NOT import or modify
that module -- benchmarks/ must stay decoupled from src/ writers.

Pure module: no I/O, no network, no clock, no database.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

REGISTRY_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
ASSERTION_VERSION: Final[str] = "p9a-assertions-1.0"
ORACLE_VERSION: Final[str] = "p9a-oracle-1.0"

_SHA256 = r"^[0-9a-f]{64}$"

EvalMode = Literal["QUERY", "ANALYZE", "BUILD"]
EvalRisk = Literal["low", "medium", "high"]
OracleState = Literal["MISSING", "REFERENCE_ONLY", "MATERIALIZED", "LABEL"]

# The five outcomes a case may legitimately be expected to reach.
ExpectedOutcome = Literal[
    "CORRECT_ANSWER",
    "CORRECT_CLARIFICATION",
    "CORRECT_HITL",
    "CORRECT_RESULT_UNAVAILABLE",
    "CORRECT_REJECTION",
]

# The closed outcome taxonomy from MASTER_PR_PLAN_V4.md 6.6.1 (15 outcomes)
# plus UNKNOWN, which is not a verdict but the explicit "not adjudicated"
# state required when an oracle is absent or a case was never run.
OUTCOME_TAXONOMY: Final[tuple[str, ...]] = (
    "CORRECT_ANSWER",
    "CORRECT_CLARIFICATION",
    "CORRECT_HITL",
    "CORRECT_RESULT_UNAVAILABLE",
    "CORRECT_REJECTION",
    "INCORRECT_ANSWER",
    "INCORRECT_CONFIDENT_ANSWER",
    "FALSE_REJECTION",
    "UNNECESSARY_CLARIFICATION",
    "AUTHORIZATION_FAILURE",
    "SOURCE_ROUTING_FAILURE",
    "PROVENANCE_FAILURE",
    "DATA_QUALITY_FAILURE",
    "CONFIRMED_PLAN_DEVIATION",
    "SANDBOX_OR_MODEL_INPUT_FAILURE",
    "UNKNOWN",
)

EvalOutcome = Literal[
    "CORRECT_ANSWER",
    "CORRECT_CLARIFICATION",
    "CORRECT_HITL",
    "CORRECT_RESULT_UNAVAILABLE",
    "CORRECT_REJECTION",
    "INCORRECT_ANSWER",
    "INCORRECT_CONFIDENT_ANSWER",
    "FALSE_REJECTION",
    "UNNECESSARY_CLARIFICATION",
    "AUTHORIZATION_FAILURE",
    "SOURCE_ROUTING_FAILURE",
    "PROVENANCE_FAILURE",
    "DATA_QUALITY_FAILURE",
    "CONFIRMED_PLAN_DEVIATION",
    "SANDBOX_OR_MODEL_INPUT_FAILURE",
    "UNKNOWN",
]

CORRECT_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "CORRECT_ANSWER",
        "CORRECT_CLARIFICATION",
        "CORRECT_HITL",
        "CORRECT_RESULT_UNAVAILABLE",
        "CORRECT_REJECTION",
    }
)
NON_ANSWER_EXPECTED: Final[frozenset[str]] = frozenset(
    {
        "CORRECT_CLARIFICATION",
        "CORRECT_HITL",
        "CORRECT_RESULT_UNAVAILABLE",
        "CORRECT_REJECTION",
    }
)
# A "confident" wrong answer is worse than a hedged one; the plan lists it as a
# separate, higher-risk outcome.
CONFIDENT_SCORE_THRESHOLD: Final[float] = 0.8

CLARIFICATION_TAGS: Final[frozenset[str]] = frozenset(
    {
        "clarification_needed",
        "ambiguous",
        "insufficient_info",
        "too_vague",
        "context_dependent",
        "vague",
        "underspecified",
    }
)
SECURITY_TAGS: Final[frozenset[str]] = frozenset(
    {
        "adversarial",
        "injection",
        "sql_injection",
        "privilege_escalation",
        "data_exfil",
        "data_exfiltration",
        "delete_attempt",
        "dml_attempt",
        "ddl",
        "outfile",
        "copy_to",
        "cartesian_risk",
    }
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _non_blank(value: str, info: ValidationInfo) -> str:
    if not value.strip():
        raise ValueError(f"{info.field_name} must not be blank")
    return value


def _strict_true(value: object, info: ValidationInfo) -> bool:
    if value is not True:
        raise ValueError(f"{info.field_name} must be the boolean True")
    return True


def _canonical_value(value: object) -> object:
    """Deterministic JSON-compatible projection (mirrors P4-Q canonicalisation)."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite number")
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_json(payload: object) -> str:
    """Canonical JSON: sorted keys, no whitespace, no NaN. Stable across runs."""
    return json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_case_revision(payload: Mapping[str, Any]) -> str:
    """Content-derived case revision: identical input -> identical revision."""
    return sha256_hex(canonical_json(payload))[:16]


def derive_expected_outcome(
    *,
    expected_mode: str,
    should_reject: bool = False,
    is_adversarial: bool = False,
    tags: Sequence[str] = (),
) -> ExpectedOutcome:
    """Map legacy BenchmarkCase labels onto the 6.6.1 expected outcomes.

    "reject" is the legacy spelling for "do not answer"; whether that means a
    refusal or a clarification is decided by the reviewed tags, not guessed.
    """
    tag_set = set(tags)
    if expected_mode in ("reject", "refuse", "clarify", "clarification"):
        if should_reject or is_adversarial:
            return "CORRECT_REJECTION"
        if tag_set & CLARIFICATION_TAGS:
            return "CORRECT_CLARIFICATION"
        return "CORRECT_REJECTION"
    if tag_set & CLARIFICATION_TAGS:
        return "CORRECT_CLARIFICATION"
    return "CORRECT_ANSWER"


class CaseOracle(_StrictFrozenModel):
    """An independent oracle bound to one case.

    The state property is what the denominator logic keys on:

    - MATERIALIZED: an expected value (or its digest) is present, so a
      produced value can be compared without re-executing the system.
    - REFERENCE_ONLY: only an independent reference SQL fingerprint exists;
      a value verdict additionally requires executing that reference.
    - LABEL: the oracle is the reviewed expected outcome for a non-answer
      terminal (clarification / HITL / unavailable / rejection).
    - MISSING: no independent expectation at all -- never a PASS.
    """

    oracle_kind: Literal[
        "independent_reference_sql",
        "independent_calculation",
        "approved_fact",
        "reviewed_label",
    ]
    oracle_revision: str = Field(min_length=1, max_length=128)
    independent_of_system_under_test: Literal[True] = True
    expected_value: Any = None
    expected_value_sha256: str | None = Field(default=None, pattern=_SHA256)
    reference_sql_fingerprint: str | None = Field(default=None, pattern=_SHA256)
    expected_row_count: int | None = Field(default=None, ge=0)
    tolerance: float = Field(default=0.0, ge=0.0)

    @field_validator("oracle_revision")
    @classmethod
    def _revision_not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @field_validator("independent_of_system_under_test", mode="before")
    @classmethod
    def _must_be_true(cls, value: object, info: ValidationInfo) -> bool:
        return _strict_true(value, info)

    @model_validator(mode="after")
    def _kind_requires_its_evidence(self) -> CaseOracle:
        if self.oracle_kind == "independent_reference_sql":
            if self.reference_sql_fingerprint is None:
                raise ValueError("a reference-sql oracle requires a sql fingerprint")
        if self.oracle_kind == "independent_calculation":
            if self.expected_value is None and self.expected_value_sha256 is None:
                raise ValueError("an independent calculation oracle requires a value or digest")
        return self

    @property
    def state(self) -> OracleState:
        if self.expected_value is not None or self.expected_value_sha256 is not None:
            return "MATERIALIZED"
        if self.reference_sql_fingerprint is not None:
            return "REFERENCE_ONLY"
        if self.oracle_kind == "reviewed_label":
            return "LABEL"
        return "MISSING"


class EvalCase(_StrictFrozenModel):
    """A case as the unified evaluator sees it."""

    schema_version: Literal["1.0"] = REGISTRY_SCHEMA_VERSION
    case_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    layer: str = Field(min_length=1, max_length=32)
    domain: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    mode: EvalMode = "QUERY"
    capability: str = Field(default="fetch", min_length=1, max_length=64)
    risk: EvalRisk = "low"
    tags: tuple[str, ...] = ()
    expected_outcome: ExpectedOutcome
    oracle: CaseOracle | None = None
    legacy_expected_mode: str = "sql_only"
    difficulty: str = "medium"
    tolerance: float = Field(default=0.0, ge=0.0)
    seed: int = 0

    @field_validator("case_id", "revision", "source", "layer", "domain", "question")
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @property
    def oracle_state(self) -> OracleState:
        return "MISSING" if self.oracle is None else self.oracle.state

    @property
    def adjudicable(self) -> bool:
        """True only when an independent expectation can decide this case.

        Answer cases need a MATERIALIZED oracle; non-answer cases need the
        reviewed label (or better).  REFERENCE_ONLY and MISSING are UNKNOWN.
        """
        if self.expected_outcome in NON_ANSWER_EXPECTED:
            return self.oracle_state in ("LABEL", "MATERIALIZED")
        return self.oracle_state == "MATERIALIZED"


class CaseRegistry(_StrictFrozenModel):
    """A revisioned, checksummed set of cases."""

    schema_version: Literal["1.0"] = REGISTRY_SCHEMA_VERSION
    registry_revision: str = Field(min_length=1, max_length=128)
    assertion_version: str = ASSERTION_VERSION
    oracle_version: str = ORACLE_VERSION
    cases: tuple[EvalCase, ...] = ()

    @field_validator("registry_revision")
    @classmethod
    def _revision_not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @model_validator(mode="after")
    def _ids_unique_per_mode(self) -> CaseRegistry:
        keys = [(case.case_id, case.mode) for case in self.cases]
        if len(set(keys)) != len(keys):
            raise ValueError("registry cases must be unique per (case_id, mode)")
        return self

    @property
    def checksum(self) -> str:
        return case_set_checksum(self.cases)

    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)

    def by_id(self) -> dict[str, EvalCase]:
        return {case.case_id: case for case in self.cases}

    def mode_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.mode] = counts.get(case.mode, 0) + 1
        return counts

    def adjudicable_count(self) -> int:
        return sum(1 for case in self.cases if case.adjudicable)

    def oracle_state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.oracle_state] = counts.get(case.oracle_state, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_revision": self.registry_revision,
            "assertion_version": self.assertion_version,
            "oracle_version": self.oracle_version,
            "checksum": self.checksum,
            "case_count": len(self.cases),
            "mode_counts": self.mode_counts(),
            "adjudicable_count": self.adjudicable_count(),
            "oracle_state_counts": self.oracle_state_counts(),
            "cases": [case.model_dump(mode="json") for case in self.cases],
        }


def case_checksum(case: EvalCase) -> str:
    return sha256_hex(canonical_json(case.model_dump(mode="python")))


def case_set_checksum(cases: Sequence[EvalCase]) -> str:
    """Order-independent checksum of a case set (identical input -> identical)."""
    ordered = sorted(cases, key=lambda case: (case.case_id, case.mode))
    return sha256_hex(canonical_json([case.model_dump(mode="python") for case in ordered]))


def dedupe_cases(cases: Sequence[EvalCase]) -> tuple[EvalCase, ...]:
    """Drop byte-identical shared cases, but never collapse across modes.

    A case shared between two modes is two distinct evaluation obligations;
    keying only on case_id would silently drop one mode's coverage.  The dedup
    key is therefore (case_id, mode).
    """
    seen: set[tuple[str, str]] = set()
    kept: list[EvalCase] = []
    for case in cases:
        key = (case.case_id, case.mode)
        if key in seen:
            continue
        seen.add(key)
        kept.append(case)
    return tuple(kept)


def build_registry(
    cases: Sequence[EvalCase],
    *,
    registry_revision: str = "p9a-registry-v1",
) -> CaseRegistry:
    """Build a registry from cases, deduplicating shared cases per mode."""
    return CaseRegistry(registry_revision=registry_revision, cases=dedupe_cases(cases))


__all__ = [
    "ASSERTION_VERSION",
    "CLARIFICATION_TAGS",
    "CONFIDENT_SCORE_THRESHOLD",
    "CORRECT_OUTCOMES",
    "CaseOracle",
    "CaseRegistry",
    "EvalCase",
    "EvalMode",
    "EvalOutcome",
    "EvalRisk",
    "ExpectedOutcome",
    "NON_ANSWER_EXPECTED",
    "ORACLE_VERSION",
    "OUTCOME_TAXONOMY",
    "OracleState",
    "REGISTRY_SCHEMA_VERSION",
    "SECURITY_TAGS",
    "build_registry",
    "canonical_json",
    "case_checksum",
    "case_set_checksum",
    "dedupe_cases",
    "derive_case_revision",
    "derive_expected_outcome",
    "sha256_hex",
]
