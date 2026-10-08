"""Bounded ANALYZE evidence and model-input projection.

The module accepts only the already-grounded answer plus its checkpoint-safe
execution record.  It never accepts SQL, database rows, credentials or an
authorization context.  Every projected fact is rebound to a successful step
receipt and every business value is a bounded JSON scalar.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.nl2sql.contracts import (
    AnswerFact,
    PlanExecutionRecord,
    PlanStepId,
    SourceDegradation,
    TimeRange,
)
from src.nl2sql.orchestration.grounding import GroundedAnswer

__all__ = [
    "AnalysisAuthorityProvenance",
    "AnalysisEvidenceBundle",
    "AnalysisEvidenceError",
    "AnalysisEvidenceFact",
    "AnalysisInterpretation",
    "AnalysisModelInput",
    "AnalysisStatement",
    "MAX_ANALYSIS_FACTS",
    "build_analysis_evidence",
    "project_analysis_model_input",
    "validate_analysis_interpretation",
]

MAX_ANALYSIS_FACTS = 16
MAX_ANALYSIS_GOAL_LENGTH = 1_024
_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])")

AnalysisScalar = str | bool | int | float | None


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class AnalysisEvidenceError(RuntimeError):
    """Stable, secret-free refusal to construct or accept ANALYZE evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AnalysisEvidenceFact(_StrictFrozenModel):
    """One scalar business fact copied from grounded execution authority."""

    fact_id: str = Field(pattern=_CHECKSUM_PATTERN)
    step_id: PlanStepId
    metric_key: str = Field(min_length=1, max_length=256)
    status: Literal["grounded", "unavailable"]
    value: AnalysisScalar = None
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    time_range: TimeRange | None = None
    observed_at: datetime | None = None
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    source_degradation: tuple[SourceDegradation, ...] = Field(default=(), max_length=32)
    source_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$"
    )
    source_kind: Literal["approved_aggregate", "approved_detail"] | None = None
    source_checkpoint: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$"
    )
    semantic_signature: str | None = Field(default=None, pattern=_CHECKSUM_PATTERN)
    rowset_sha256: str | None = Field(default=None, pattern=_CHECKSUM_PATTERN)
    output_digest: str = Field(pattern=_CHECKSUM_PATTERN)

    @field_validator("value")
    @classmethod
    def validate_scalar(cls, value: AnalysisScalar) -> AnalysisScalar:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("analysis evidence value must be finite")
        if isinstance(value, str) and len(value) > 1_024:
            raise ValueError("analysis evidence string value is too large")
        return value

    @model_validator(mode="after")
    def validate_status_value(self) -> AnalysisEvidenceFact:
        if self.status == "unavailable" and self.value is not None:
            raise ValueError("unavailable analysis evidence cannot carry a value")
        if self.status == "grounded" and self.value is None:
            raise ValueError("grounded analysis evidence requires a scalar value")
        return self


class AnalysisAuthorityProvenance(_StrictFrozenModel):
    """Safe execution identities proving where a bundle came from."""

    execution_plan_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    receipt_step_ids: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)
    source_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_checkpoints: tuple[str, ...] = Field(default=(), max_length=16)
    semantic_signatures: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator(
        "receipt_step_ids", "source_ids", "source_checkpoints", "semantic_signatures"
    )
    @classmethod
    def validate_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("analysis authority identifiers must be unique")
        return value


class AnalysisEvidenceBundle(_StrictFrozenModel):
    """Non-checkpoint ANALYZE input containing bounded, proven scalar facts."""

    schema_version: Literal["1.0"] = "1.0"
    metric_keys: tuple[str, ...] = Field(min_length=1, max_length=16)
    facts: tuple[AnalysisEvidenceFact, ...] = Field(min_length=1, max_length=16)
    analysis_window: TimeRange
    data_as_of: datetime | None = None
    authority_provenance: AnalysisAuthorityProvenance
    degradation_flags: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("metric_keys", "degradation_flags")
    @classmethod
    def validate_unique_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("analysis evidence identifiers must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError("analysis evidence identifiers must be unique")
        return value

    @model_validator(mode="after")
    def validate_bundle_coherence(self) -> AnalysisEvidenceBundle:
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("analysis evidence facts must be unique")
        projected_metrics = tuple(dict.fromkeys(fact.metric_key for fact in self.facts))
        if self.metric_keys != projected_metrics:
            raise ValueError("analysis metric identities must match the facts")
        fact_steps = tuple(dict.fromkeys(fact.step_id for fact in self.facts))
        if self.authority_provenance.receipt_step_ids != fact_steps:
            raise ValueError("analysis authority receipts must match the facts")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class AnalysisStatement(_StrictFrozenModel):
    """One bounded model-authored statement with explicit fact references."""

    text: str = Field(min_length=1, max_length=2_048)
    fact_ids: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("fact_ids")
    @classmethod
    def validate_fact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("analysis statement fact references must be unique")
        if any(re.fullmatch(_CHECKSUM_PATTERN, item) is None for item in value):
            raise ValueError("analysis statement fact reference is invalid")
        return value


class AnalysisInterpretation(_StrictFrozenModel):
    """Bounded, noncanonical Mode-2 interpretation returned by a model."""

    summary: AnalysisStatement
    observations: tuple[AnalysisStatement, ...] = Field(default=(), max_length=16)
    caveats: tuple[AnalysisStatement, ...] = Field(default=(), max_length=16)


class AnalysisModelMessage(_StrictFrozenModel):
    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=64_000)


class AnalysisModelInput(_StrictFrozenModel):
    """Deterministic content that can be placed inside a governed ModelRequest."""

    evidence_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    messages: tuple[AnalysisModelMessage, AnalysisModelMessage]

    @property
    def response_schema(self) -> dict[str, object]:
        return AnalysisInterpretation.model_json_schema()


class _AnalysisGoal(_StrictFrozenModel):
    value: str = Field(min_length=1, max_length=MAX_ANALYSIS_GOAL_LENGTH)

    @field_validator("value")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("analysis goal must be non-blank")
        return normalized


def build_analysis_evidence(
    *,
    grounded: GroundedAnswer,
    record: PlanExecutionRecord,
    execution_plan_checksum: str,
    analysis_window: TimeRange,
) -> AnalysisEvidenceBundle:
    """Rebind grounded facts to successful execution receipts and bound them."""

    if record.status != "succeeded":
        raise AnalysisEvidenceError("analysis_execution_not_succeeded")
    if (
        re.fullmatch(_CHECKSUM_PATTERN, execution_plan_checksum) is None
        or record.execution_plan_checksum != execution_plan_checksum
    ):
        raise AnalysisEvidenceError("analysis_execution_plan_mismatch")
    if grounded.artifact.facts != grounded.facts:
        raise AnalysisEvidenceError("analysis_grounded_artifact_mismatch")
    if len(grounded.facts) > MAX_ANALYSIS_FACTS:
        raise AnalysisEvidenceError("analysis_evidence_fact_limit_exceeded")

    receipts = {receipt.step_id: receipt for receipt in record.step_receipts}
    facts: list[AnalysisEvidenceFact] = []
    flags = list(grounded.artifact.degradation_flags)
    for fact in grounded.facts:
        receipt = receipts.get(fact.step_id)
        if (
            fact.metric_key is None
            or fact.derived_output_id is not None
            or receipt is None
            or receipt.status != "succeeded"
            or receipt.kind not in {"fetch_metric", "trusted_calculation"}
            or fact.output_digest != receipt.output_digest
            or fact.rowset_sha256 != receipt.rowset_sha256
            or fact.source_id != receipt.source_id
            or fact.semantic_signature != receipt.semantic_signature
            or fact.data_as_of != receipt.data_as_of
            or fact.freshness_status != receipt.freshness_status
            or fact.source_kind != receipt.source_kind
            or fact.source_degradation != receipt.source_degradation
            or fact.fact_id != _grounded_fact_id(fact)
            or not _is_scalar(fact.value)
        ):
            flags.append("analysis_fact_omitted_unproven")
            continue
        facts.append(
            AnalysisEvidenceFact(
                fact_id=fact.fact_id,
                step_id=fact.step_id,
                metric_key=fact.metric_key,
                status=fact.status,
                value=cast("AnalysisScalar", fact.value),
                unit=fact.unit,
                time_range=fact.time_range,
                # Grounding exposes no independent observed_at evidence.
                observed_at=None,
                data_as_of=fact.data_as_of,
                freshness_status=fact.freshness_status,
                source_degradation=fact.source_degradation,
                source_id=fact.source_id,
                source_kind=fact.source_kind,
                source_checkpoint=receipt.source_checkpoint,
                semantic_signature=fact.semantic_signature,
                rowset_sha256=fact.rowset_sha256,
                output_digest=cast("str", fact.output_digest),
            )
        )

    if not facts:
        raise AnalysisEvidenceError("analysis_evidence_no_proven_facts")
    if len({fact.fact_id for fact in facts}) != len(facts):
        raise AnalysisEvidenceError("analysis_evidence_duplicate_fact")

    data_as_of = _conservative_data_as_of(tuple(facts))
    if data_as_of is None:
        flags.append("analysis_data_as_of_incomplete")
    for fact in facts:
        if fact.freshness_status == "stale":
            flags.append("analysis_source_stale")
        elif fact.freshness_status == "unknown":
            flags.append("analysis_source_freshness_unknown")
        flags.extend(fact.source_degradation)

    fact_tuple = tuple(facts)
    return AnalysisEvidenceBundle(
        metric_keys=tuple(dict.fromkeys(fact.metric_key for fact in fact_tuple)),
        facts=fact_tuple,
        analysis_window=analysis_window,
        data_as_of=data_as_of,
        authority_provenance=AnalysisAuthorityProvenance(
            execution_plan_checksum=record.execution_plan_checksum,
            receipt_step_ids=tuple(dict.fromkeys(fact.step_id for fact in fact_tuple)),
            source_ids=_unique_present(fact.source_id for fact in fact_tuple),
            source_checkpoints=_unique_present(
                fact.source_checkpoint for fact in fact_tuple
            ),
            semantic_signatures=_unique_present(
                fact.semantic_signature for fact in fact_tuple
            ),
        ),
        degradation_flags=tuple(dict.fromkeys(flags)),
    )


def project_analysis_model_input(
    bundle: AnalysisEvidenceBundle,
    *,
    analysis_goal: str,
) -> AnalysisModelInput:
    """Project bounded evidence and a bounded goal into deterministic messages."""

    goal = _AnalysisGoal(value=analysis_goal).value
    source_facts = _canonical_json(
        {
            "evidence_checksum": bundle.checksum,
            "metric_keys": bundle.metric_keys,
            "analysis_window": bundle.analysis_window.model_dump(mode="json"),
            "data_as_of": bundle.data_as_of.isoformat() if bundle.data_as_of else None,
            "degradation_flags": bundle.degradation_flags,
            "facts": [fact.model_dump(mode="json") for fact in bundle.facts],
        }
    )
    # DeepSeek (this deployment's provider) supports only
    # response_format=json_object, which does NOT constrain the shape.  The
    # exact schema therefore lives in the SYSTEM message so the model cannot
    # guess field names that local validation would then reject, while the
    # user message (SOURCE FACTS + goal) stays machine-parseable.
    schema = json.dumps(
        AnalysisInterpretation.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    system = (
        "Interpret only the supplied SOURCE FACTS. Never invent values, fill missing "
        "dates, change metric definitions, claim data newer than data_as_of, or treat "
        "SQL/model text as business authority. Every statement MUST list in fact_ids "
        "every fact it relies on. HARD NUMERIC RULE: a number may appear in a "
        "statement text ONLY if that exact digit sequence appears verbatim in the "
        "value, unit, time_range, observed_at or data_as_of of one of the cited "
        "fact_ids. Do NOT write day counts, window lengths, percentages, years or "
        "averages that are not literally present in the cited facts, and do not "
        "reformat numbers (write 93.20, never 93.2 or 93.2%). When in doubt, cite "
        "fewer facts and write fewer numbers. Return only the requested JSON object "
        "whose exact required shape is: "
        + schema
    )
    user = (
        "SOURCE FACTS\n"
        f"{source_facts}\n\n"
        "REQUESTED INTERPRETATION\n"
        f"{goal}"
    )
    return AnalysisModelInput(
        evidence_checksum=bundle.checksum,
        messages=(
            AnalysisModelMessage(role="system", content=system),
            AnalysisModelMessage(role="user", content=user),
        ),
    )


def validate_analysis_interpretation(
    interpretation: AnalysisInterpretation,
    *,
    evidence: AnalysisEvidenceBundle,
) -> AnalysisInterpretation:
    """Reject unknown references and numeric claims not traceable to cited facts."""

    facts = {fact.fact_id: fact for fact in evidence.facts}
    statements = (
        interpretation.summary,
        *interpretation.observations,
        *interpretation.caveats,
    )
    for statement in statements:
        unknown = set(statement.fact_ids) - set(facts)
        if unknown:
            raise AnalysisEvidenceError("analysis_interpretation_unknown_fact")
        allowed_numbers: set[str] = set()
        for fact_id in statement.fact_ids:
            fact = facts[fact_id]
            traceable = {
                "value": fact.value,
                "unit": fact.unit,
                "time_range": (
                    fact.time_range.model_dump(mode="json") if fact.time_range else None
                ),
                "observed_at": fact.observed_at.isoformat() if fact.observed_at else None,
                "data_as_of": fact.data_as_of.isoformat() if fact.data_as_of else None,
            }
            allowed_numbers.update(_numeric_tokens(_canonical_json(traceable)))
        actual = _numeric_tokens(statement.text)
        if not actual <= allowed_numbers:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "unproven numeric statement text=%r missing=%s fact_ids=%s",
                statement.text,
                sorted(actual - allowed_numbers),
                list(statement.fact_ids),
            )
            raise AnalysisEvidenceError("analysis_interpretation_numeric_fact_unproven")
    return interpretation


def _is_scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, (str, bool, int, float))
        and not (isinstance(value, float) and not math.isfinite(value))
    )


def _grounded_fact_id(fact: AnswerFact) -> str:
    payload: dict[str, object] = {
        "step_id": fact.step_id,
        "metric_key": fact.metric_key,
        "status": fact.status,
        "value": fact.value,
    }
    if fact.time_range is not None:
        payload["time_range"] = fact.time_range.model_dump(mode="json")
    return _checksum(payload)


def _conservative_data_as_of(
    facts: tuple[AnalysisEvidenceFact, ...],
) -> datetime | None:
    values = tuple(fact.data_as_of for fact in facts)
    if any(value is None or value.tzinfo is None for value in values):
        return None
    return min(value.astimezone(UTC) for value in values if value is not None)


def _unique_present(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))


def _numeric_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _NUMBER_PATTERN.finditer(text):
        try:
            number = Decimal(match.group(0))
        except InvalidOperation:  # pragma: no cover - regex only admits decimals
            continue
        tokens.add(format(number.normalize(), "f"))
    return tokens
