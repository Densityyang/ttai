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
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    PlanExecutionRecord,
    PlanStepId,
    QueryPlan,
    SourceDegradation,
    TimeRange,
)
from src.nl2sql.orchestration.checkpoint_evidence_seal import (
    CheckpointEvidenceSealError,
    verify_grounded_execution_checkpoint,
)
from src.nl2sql.orchestration.execution import PlanStepError, validate_execution_record_against_plan
from src.nl2sql.orchestration.grounding import (
    GroundedAnswer,
    conservative_data_as_of,
    render_grounded_answer,
)
from src.nl2sql.semantic.metric_contract import MetricContract

__all__ = [
    "AnalysisAuthorityProvenance",
    "AnalysisCausalSupport",
    "AnalysisEvidenceBundle",
    "AnalysisEvidenceError",
    "AnalysisEvidenceFact",
    "AnalysisInterpretation",
    "AnalysisManualOriginEvidence",
    "AnalysisModelInput",
    "AnalysisScopeProvenance",
    "AnalysisSourceKind",
    "AnalysisStatement",
    "DenominatorBasis",
    "EvidenceOrigin",
    "MAX_ANALYSIS_FACTS",
    "SamplingScope",
    "build_analysis_evidence",
    "project_analysis_model_input",
    "validate_grounded_answer_checkpoint",
    "validate_analysis_interpretation",
]

MAX_ANALYSIS_FACTS = 16
MAX_ANALYSIS_GOAL_LENGTH = 1_024
_CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?[0-9]+(?:\.[0-9]+)?(?![A-Za-z0-9_])"
)
_DATE_TIME_PATTERN = re.compile(
    r"(?<![0-9])(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"(?:[Tt ](?P<time>[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?)"
    r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})?)?(?![0-9])"
)
_CLOCK_TIME_PATTERN = re.compile(
    r"(?<![0-9])[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?![0-9])"
)
_UNIT_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "about", "above", "after", "and", "are", "as", "at", "below",
        "by", "for", "from", "in", "is", "less", "more", "of", "on", "or",
        "over", "per", "than", "the", "to", "under", "was", "were", "with",
        "today", "yesterday", "tomorrow", "now", "overall", "total",
        "approximately", "roughly", "respectively", "higher", "lower",
        "increased", "decreased", "point", "points", "observed", "value",
    }
)
_PUNCTUATION_UNITS: Final[dict[str, str]] = {
    "%": "percent",
    "％": "percent",
    "¥": "currency_cny",
}
_UNIT_ALIASES: Final[dict[str, str]] = {
    "percentage points": "percentage_points",
    "percentage point": "percentage_points",
    "percent": "percent",
    "percentage": "percent",
    "%": "percent",
    "％": "percent",
    "counts": "count",
    "count": "count",
    "orders": "order_count",
    "order": "order_count",
    "stores": "store_count",
    "store": "store_count",
    "users": "user_count",
    "user": "user_count",
    "people": "person_count",
    "person": "person_count",
    "cny": "currency_cny",
    "currency_cny": "currency_cny",
    "¥": "currency_cny",
}
_UNIT_ALIASES_LONGEST: Final[tuple[str, ...]] = tuple(
    sorted(_UNIT_ALIASES, key=len, reverse=True)
)
_UNIT_WORD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*|^[\u3400-\u9fff]{1,16}")

AnalysisScalar = str | bool | int | float | None

# --- evidence vocabulary ------------------------------------------------------
# Automated governed sources are stamped by execution receipts.  "manual_origin"
# is human-asserted context: it is a DIFFERENT basis from an automated base
# table, so the vocabulary names it explicitly instead of letting a manual input
# borrow an approved aggregate/detail identity.
AutomatedSourceKind = Literal["approved_aggregate", "approved_detail"]
AnalysisSourceKind = Literal["approved_aggregate", "approved_detail", "manual_origin"]
EvidenceOrigin = Literal["automated_governed_source", "manual_origin"]

# Denominator selection and sampling range are analysis-scope choices that must
# travel with the provenance instead of being silently fixed by the metric
# contract.  "not_declared" is the honest default: it claims nothing.
DenominatorBasis = Literal[
    "metric_contract_denominator",
    "scoped_row_count",
    "explicit_declared_filter",
    "not_declared",
]
SamplingScope = Literal[
    "full_scope_census",
    "bounded_sample",
    "top_ranked_subset",
    "not_declared",
]

_BOUNDED_REF_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"

# Causal/attribution connectives.  The bare noun "因果" is deliberately NOT a
# marker so a non-causal disclaimer ("不构成因果结论") is never mistaken for a
# claim; only an assertion-shaped connective triggers the causal gate.
_CAUSAL_MARKERS: Final[tuple[str, ...]] = (
    "导致",
    "造成",
    "引起",
    "引发",
    "致使",
    "使得",
    "归因",
    "根本原因",
    "原因在于",
    "因为",
    "由于",
    "源于",
    "起因",
    "促成",
    "得益于",
    "决定性因素",
    "because",
    "cause",
    "caused",
    "causes",
    "due to",
    "leads to",
    "led to",
    "results in",
    "resulted in",
    "attributable",
    "root cause",
    "owing to",
    "driven by",
    "as a result of",
)

# A bounded, explicit disclaimer ("无法证明 X 导致 Y", "不表明 X 因为 Y") is not a
# causal assertion.  Only the negated verb forms are exempted: a bare "不" is not
# used because it appears inside ordinary words such as "不足".
_CAUSAL_DISCLAIMER_PATTERN = re.compile(
    r"(?:无法|不能|不应|不可|未能|没有|未)(?:证明|断言|推断|断定|确认)"
    r"[^。！？；\n]{0,24}?(?:导致|造成|引起|引发|致使|因为|由于|归因|根本原因)"
    r"|(?:不|未)(?:表明|代表|意味着|证明|说明)"
    r"[^。！？；\n]{0,24}?(?:导致|造成|引起|引发|致使|因为|由于|归因|根本原因)"
)


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
    metric_key: str | None = Field(default=None, min_length=1, max_length=256)
    derived_output_id: str | None = Field(
        default=None, pattern=r"^adhoc_[0-9a-f]{32}$"
    )
    calculation_scope: Literal["ad_hoc_noncanonical"] | None = None
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
    source_kind: AnalysisSourceKind | None = None
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
    def validate_identity_scope(self) -> AnalysisEvidenceFact:
        if self.metric_key is None:
            if (
                self.derived_output_id is None
                or self.calculation_scope != "ad_hoc_noncanonical"
            ):
                raise ValueError(
                    "analysis evidence requires a canonical metric or noncanonical derived id"
                )
        elif self.derived_output_id is not None or self.calculation_scope is not None:
            raise ValueError("canonical metric evidence cannot carry AD_HOC identity")
        return self

    @property
    def origin(self) -> EvidenceOrigin:
        """The evidence basis; manual origin is never an automated fact."""

        if self.source_kind == "manual_origin":
            return "manual_origin"
        return "automated_governed_source"

    @model_validator(mode="after")
    def validate_status_value(self) -> AnalysisEvidenceFact:
        if self.status == "unavailable" and self.value is not None:
            raise ValueError("unavailable analysis evidence cannot carry a value")
        if self.status == "grounded" and self.value is None:
            raise ValueError("grounded analysis evidence requires a scalar value")
        if self.source_kind == "manual_origin":
            raise ValueError(
                "manual-origin evidence must use AnalysisManualOriginEvidence, "
                "never the automated governed fact collection"
            )
        return self


class AnalysisAuthorityProvenance(_StrictFrozenModel):
    """Safe execution identities proving where a bundle came from."""

    execution_plan_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    receipt_step_ids: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)
    source_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_checkpoints: tuple[str, ...] = Field(default=(), max_length=16)
    semantic_signatures: tuple[str, ...] = Field(default=(), max_length=16)
    # Manual-origin evidence identities, kept in a SEPARATE provenance channel
    # so a human assertion can never be read back as an automated receipt.
    manual_origin_ids: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator(
        "receipt_step_ids",
        "source_ids",
        "source_checkpoints",
        "semantic_signatures",
        "manual_origin_ids",
    )
    @classmethod
    def validate_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("analysis authority identifiers must be unique")
        return value

    @field_validator("manual_origin_ids")
    @classmethod
    def validate_manual_origin_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_CHECKSUM_PATTERN, item) is None for item in value):
            raise ValueError("manual origin provenance identity is invalid")
        return value


class AnalysisManualOriginEvidence(_StrictFrozenModel):
    """Human-asserted context, kept structurally apart from automated facts.

    A manual-origin basis is never an execution receipt, never a semantic
    signature and never a rowset digest; it carries its own attested identity so
    downstream code can tell "an operator said so" from "an approved base table
    returned it".
    """

    evidence_id: str = Field(pattern=_CHECKSUM_PATTERN)
    source_kind: Literal["manual_origin"] = "manual_origin"
    label: str = Field(min_length=1, max_length=256)
    value: AnalysisScalar = None
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    asserted_by_role: Literal[
        "business_operator", "analyst", "reviewer", "system_operator"
    ]
    attestation_ref: str = Field(pattern=_BOUNDED_REF_PATTERN)
    asserted_at: datetime | None = None

    @field_validator("value")
    @classmethod
    def validate_scalar(cls, value: AnalysisScalar) -> AnalysisScalar:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("manual origin value must be finite")
        if isinstance(value, str) and len(value) > 1_024:
            raise ValueError("manual origin string value is too large")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> AnalysisManualOriginEvidence:
        expected = _manual_evidence_id(
            label=self.label,
            value=self.value,
            unit=self.unit,
            asserted_by_role=self.asserted_by_role,
            attestation_ref=self.attestation_ref,
            asserted_at=self.asserted_at,
        )
        if self.evidence_id != expected:
            raise ValueError("manual origin evidence identity must match its content")
        return self

    @classmethod
    def create(
        cls,
        *,
        label: str,
        value: AnalysisScalar = None,
        asserted_by_role: Literal[
            "business_operator", "analyst", "reviewer", "system_operator"
        ],
        attestation_ref: str,
        asserted_at: datetime | None = None,
        unit: str | None = None,
    ) -> AnalysisManualOriginEvidence:
        """Deterministically identify one manual-origin assertion."""

        return cls(
            evidence_id=_manual_evidence_id(
                label=label,
                value=value,
                unit=unit,
                asserted_by_role=asserted_by_role,
                attestation_ref=attestation_ref,
                asserted_at=asserted_at,
            ),
            label=label,
            value=value,
            unit=unit,
            asserted_by_role=asserted_by_role,
            attestation_ref=attestation_ref,
            asserted_at=asserted_at,
        )


class AnalysisScopeProvenance(_StrictFrozenModel):
    """Declared denominator choice and sampling range for one analysis.

    The metric contract may fix a ratio denominator internally; this record is
    where the ANALYSIS states which denominator basis and which sampling range
    the returned numbers actually use.  "not_declared" is the honest default and
    claims nothing.
    """

    denominator_basis: DenominatorBasis = "not_declared"
    denominator_ref: str | None = Field(default=None, pattern=_BOUNDED_REF_PATTERN)
    sampling_scope: SamplingScope = "not_declared"
    sample_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    sampled_row_count: int | None = Field(default=None, ge=0)
    population_row_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_scope_coherence(self) -> AnalysisScopeProvenance:
        if self.denominator_basis == "not_declared":
            if self.denominator_ref is not None:
                raise ValueError("undeclared denominator basis cannot carry a reference")
        elif self.denominator_ref is None:
            raise ValueError("declared denominator basis requires a reference")
        if self.sampling_scope in {"bounded_sample", "top_ranked_subset"}:
            if self.sample_limit is None:
                raise ValueError("bounded sampling requires an explicit sample limit")
        elif self.sample_limit is not None:
            raise ValueError("unbounded sampling must not carry a sample limit")
        if self.sampling_scope == "not_declared" and (
            self.sampled_row_count is not None or self.population_row_count is not None
        ):
            raise ValueError("undeclared sampling scope must not carry row counts")
        if (
            self.sampled_row_count is not None
            and self.population_row_count is not None
            and self.sampled_row_count > self.population_row_count
        ):
            raise ValueError("sampled rows cannot exceed the population")
        return self


class AnalysisCausalSupport(_StrictFrozenModel):
    """Declares that one projected fact may support a causal/attribution claim.

    Empty by default, so ANALYZE is non-causal by default: any causal wording is
    refused until a design is explicitly bound to a fact.
    """

    fact_id: str = Field(pattern=_CHECKSUM_PATTERN)
    design: Literal[
        "comparative_windows",
        "cohort_contrast",
        "interrupted_time_series",
        "explicit_mechanism",
    ]
    contrast_ref: str | None = Field(default=None, pattern=_BOUNDED_REF_PATTERN)


class AnalysisEvidenceBundle(_StrictFrozenModel):
    """Non-checkpoint ANALYZE input containing bounded, proven scalar facts."""

    schema_version: Literal["1.0"] = "1.0"
    metric_keys: tuple[str, ...] = Field(default=(), max_length=16)
    facts: tuple[AnalysisEvidenceFact, ...] = Field(min_length=1, max_length=16)
    analysis_window: TimeRange
    data_as_of: datetime | None = None
    authority_provenance: AnalysisAuthorityProvenance
    # Denominator choice and sampling range travel with the analysis provenance.
    scope_provenance: AnalysisScopeProvenance = Field(
        default_factory=AnalysisScopeProvenance
    )
    # Human-asserted context is a SEPARATE collection, never merged into facts.
    manual_origin_evidence: tuple[AnalysisManualOriginEvidence, ...] = Field(
        default=(), max_length=16
    )
    # Empty by default: ANALYZE is non-causal until a design is bound to a fact.
    causal_support: tuple[AnalysisCausalSupport, ...] = Field(default=(), max_length=16)
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
        projected_metrics = tuple(
            dict.fromkeys(fact.metric_key for fact in self.facts if fact.metric_key is not None)
        )
        if self.metric_keys != projected_metrics:
            raise ValueError("analysis metric identities must match the facts")
        if projected_metrics and any(fact.derived_output_id is not None for fact in self.facts):
            raise ValueError("analysis evidence cannot mix canonical and AD_HOC identities")
        fact_steps = tuple(dict.fromkeys(fact.step_id for fact in self.facts))
        if self.authority_provenance.receipt_step_ids != fact_steps:
            raise ValueError("analysis authority receipts must match the facts")
        manual_ids = tuple(item.evidence_id for item in self.manual_origin_evidence)
        if len(set(manual_ids)) != len(manual_ids):
            raise ValueError("analysis manual origin evidence must be unique")
        if set(manual_ids) & set(fact_ids):
            raise ValueError(
                "manual origin evidence must not reuse an automated fact identity"
            )
        if self.authority_provenance.manual_origin_ids != manual_ids:
            raise ValueError(
                "analysis authority manual origin ids must match the manual evidence"
            )
        support_ids = tuple(item.fact_id for item in self.causal_support)
        if len(set(support_ids)) != len(support_ids):
            raise ValueError("analysis causal support must be unique per fact")
        if set(support_ids) - set(fact_ids):
            raise ValueError("analysis causal support must reference a projected fact")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class AnalysisStatement(_StrictFrozenModel):
    """One bounded model-authored statement with explicit fact references."""

    text: str = Field(min_length=1, max_length=2_048)
    fact_ids: tuple[str, ...] = Field(default=(), max_length=16)
    # Manual-origin evidence is cited HERE, never in fact_ids, so a human
    # assertion can never be read back as an automated governed fact.
    manual_evidence_ids: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("fact_ids", "manual_evidence_ids")
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


def validate_grounded_answer_checkpoint(
    query_plan: QueryPlan,
    artifact: AnswerArtifact | dict[str, object],
    answer_text: str,
    *,
    execution_plan: object | None = None,
    execution_record: PlanExecutionRecord | None = None,
    authoritative_units: Mapping[str, str | None] | None = None,
    metric_contracts: Mapping[str, MetricContract] | None = None,
    evidence_seal_key: str | None = None,
    unit_authority_identities: Mapping[str, str] | None = None,
    allow_legacy_checkpoint: bool = False,
) -> GroundedAnswer:
    """Rebind checkpoint answer text to a validated deterministic artifact."""

    validated_artifact = AnswerArtifact.model_validate(artifact)
    canonical = render_grounded_answer(
        query_plan=query_plan,
        facts=validated_artifact.facts,
    )
    if answer_text != canonical:
        raise AnalysisEvidenceError("analysis_grounded_answer_mismatch")
    if (execution_plan is None) != (execution_record is None):
        raise AnalysisEvidenceError("analysis_execution_evidence_missing")
    if execution_plan is not None and execution_record is not None:
        if (
            allow_legacy_checkpoint
            and execution_record.evidence_seal is None
            and not _is_legacy_unitless_checkpoint(execution_record)
        ):
            raise AnalysisEvidenceError(
                "analysis_checkpoint_legacy_compatibility_denied"
            )
        try:
            verify_grounded_execution_checkpoint(
                key=evidence_seal_key,
                query_plan=query_plan,
                execution_plan=execution_plan,  # type: ignore[arg-type]
                execution_record=execution_record,
                answer_artifact=validated_artifact,
                answer_text=answer_text,
                unit_authority_identities=unit_authority_identities or {},
                required=not allow_legacy_checkpoint,
            )
        except CheckpointEvidenceSealError as exc:
            raise AnalysisEvidenceError(str(exc)) from exc
        try:
            validate_execution_record_against_plan(execution_plan, execution_record)  # type: ignore[arg-type]
        except PlanStepError as exc:
            raise AnalysisEvidenceError(exc.code) from exc
        _validate_checkpoint_fact_bindings(
            query_plan=query_plan,
            execution_plan=execution_plan,
            execution_record=execution_record,
            facts=validated_artifact.facts,
            authoritative_units=authoritative_units,
            metric_contracts=metric_contracts,
        )
    return GroundedAnswer(
        answer_text=canonical,
        facts=validated_artifact.facts,
        artifact=validated_artifact,
    )


def _is_legacy_unitless_checkpoint(record: PlanExecutionRecord) -> bool:
    """Narrow compatibility for pre-seal fetch-only, explicitly unitless records."""

    if record.status != "succeeded" or not record.step_receipts:
        return False
    return all(
        receipt.kind in {"fetch_metric", "verify"}
        and receipt.unit is None
        and receipt.semantic_manifest_checksum is None
        and receipt.semantic_validation_identity is None
        and receipt.semantic_validation_outcome is None
        for receipt in record.step_receipts
    )


def build_analysis_evidence(
    *,
    grounded: GroundedAnswer,
    record: PlanExecutionRecord,
    execution_plan_checksum: str,
    analysis_window: TimeRange,
    scope_provenance: AnalysisScopeProvenance | None = None,
    manual_origin_evidence: tuple[AnalysisManualOriginEvidence, ...] = (),
    causal_support: tuple[AnalysisCausalSupport, ...] = (),
    authoritative_units: Mapping[str, str | None] | None = None,
    query_plan: QueryPlan | None = None,
    execution_plan: object | None = None,
    metric_contracts: Mapping[str, MetricContract] | None = None,
) -> AnalysisEvidenceBundle:
    """Rebind grounded facts to successful execution receipts and bound them.

    Automated facts keep their execution-receipt authority.  Denominator/sampling
    scope, human-asserted manual evidence and causal designs are DECLARED inputs
    that default to "not declared"/empty, so an ordinary ANALYZE stays
    non-causal and never silently borrows a manual basis as an automated fact.
    """

    if record.status != "succeeded":
        raise AnalysisEvidenceError("analysis_execution_not_succeeded")
    if (
        re.fullmatch(_CHECKSUM_PATTERN, execution_plan_checksum) is None
        or record.execution_plan_checksum != execution_plan_checksum
    ):
        raise AnalysisEvidenceError("analysis_execution_plan_mismatch")
    if execution_plan is not None:
        try:
            validate_execution_record_against_plan(execution_plan, record)  # type: ignore[arg-type]
        except PlanStepError as exc:
            raise AnalysisEvidenceError(exc.code) from exc
    if grounded.artifact.facts != grounded.facts:
        raise AnalysisEvidenceError("analysis_grounded_artifact_mismatch")
    if len(grounded.facts) > MAX_ANALYSIS_FACTS:
        raise AnalysisEvidenceError("analysis_evidence_fact_limit_exceeded")

    receipts = {receipt.step_id: receipt for receipt in record.step_receipts}
    facts: list[AnalysisEvidenceFact] = []
    flags = list(grounded.artifact.degradation_flags)
    for fact in grounded.facts:
        receipt = receipts.get(fact.step_id)
        adhoc_fact = (
            fact.metric_key is None
            and fact.derived_output_id is not None
            and fact.calculation_scope == "ad_hoc_noncanonical"
        )
        identity_valid = (
            fact.metric_key is not None
            and fact.derived_output_id is None
            and fact.calculation_scope is None
        ) or adhoc_fact
        if (
            fact.metric_key is not None
            and receipt is not None
            and receipt.unit is not None
            and authoritative_units is None
        ):
            raise AnalysisEvidenceError("analysis_fact_unit_authority_missing")
        authoritative_unit = _lookup_authoritative_unit(
            authoritative_units, fact.metric_key
        )
        unit_authority_mismatch = (
            authoritative_units is not None
            and fact.metric_key is not None
            and receipt is not None
            and (
                not _has_authoritative_unit(authoritative_units, fact.metric_key)
                or receipt.unit != authoritative_unit
            )
        )
        # Receipt units are server-side identifiers. A free-text alias must
        # never become authority merely by surviving a checkpoint round trip.
        if (
            not identity_valid
            or receipt is None
            or receipt.status != "succeeded"
            or (
                receipt.kind
                not in (
                    {"ad_hoc_calculation"}
                    if adhoc_fact
                    else {"fetch_metric", "trusted_calculation"}
                )
            )
            or (
                adhoc_fact
                and (
                    receipt.calculation_scope != "ad_hoc_noncanonical"
                    or receipt.derived_output_id != fact.derived_output_id
                )
            )
            or fact.output_digest != receipt.output_digest
            or fact.rowset_sha256 != receipt.rowset_sha256
            or fact.source_id != receipt.source_id
            or fact.semantic_signature != receipt.semantic_signature
            # Legacy grounding paths may leave the fact unit unset even when
            # the server receipt is typed.  Never accept a checkpoint supplied
            # unit as authority: if present it must agree with the receipt,
            # and the receipt itself is checked against authoritative_units
            # above before the evidence bundle projects its unit.
            or (fact.unit is not None and fact.unit != receipt.unit)
            or unit_authority_mismatch
            or (
                receipt.unit is not None
                and _canonical_unit(receipt.unit) != receipt.unit
            )
            or fact.data_as_of != receipt.data_as_of
            or fact.freshness_status != receipt.freshness_status
            or fact.source_kind != receipt.source_kind
            or fact.source_degradation != receipt.source_degradation
            or fact.fact_id != _grounded_fact_id(fact)
            or not _is_scalar(fact.value)
            or (
                execution_plan is not None
                and not _fact_matches_execution_plan(
                    fact=fact,
                    execution_plan=execution_plan,
                    query_plan=query_plan,
                    metric_contracts=metric_contracts,
                )
            )
        ):
            flags.append("analysis_fact_omitted_unproven")
            continue
        facts.append(
            AnalysisEvidenceFact(
                fact_id=fact.fact_id,
                step_id=fact.step_id,
                metric_key=fact.metric_key,
                derived_output_id=fact.derived_output_id,
                calculation_scope=fact.calculation_scope,
                status=fact.status,
                value=cast("AnalysisScalar", fact.value),
                unit=receipt.unit,
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

    data_as_of = conservative_data_as_of(fact.data_as_of for fact in facts)
    if data_as_of is None:
        flags.append("analysis_data_as_of_incomplete")
    for fact in facts:
        if fact.freshness_status == "stale":
            flags.append("analysis_source_stale")
        elif fact.freshness_status == "unknown":
            flags.append("analysis_source_freshness_unknown")
        flags.extend(fact.source_degradation)

    manual_tuple = tuple(manual_origin_evidence)
    if len({item.evidence_id for item in manual_tuple}) != len(manual_tuple):
        raise AnalysisEvidenceError("analysis_manual_origin_evidence_duplicate")
    if {item.evidence_id for item in manual_tuple} & {
        fact.fact_id for fact in facts
    }:
        raise AnalysisEvidenceError("analysis_manual_origin_identity_conflict")
    if manual_tuple:
        flags.append("analysis_manual_origin_evidence_present")
    resolved_scope = scope_provenance or AnalysisScopeProvenance()
    if (
        resolved_scope.denominator_basis == "not_declared"
        and resolved_scope.sampling_scope == "not_declared"
    ):
        flags.append("analysis_scope_undeclared")
    support_tuple = tuple(causal_support)
    if {item.fact_id for item in support_tuple} - {fact.fact_id for fact in facts}:
        raise AnalysisEvidenceError("analysis_causal_support_unknown_fact")

    fact_tuple = tuple(facts)
    return AnalysisEvidenceBundle(
        metric_keys=tuple(
            dict.fromkeys(
                fact.metric_key
                for fact in fact_tuple
                if fact.metric_key is not None
            )
        ),
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
            manual_origin_ids=tuple(item.evidence_id for item in manual_tuple),
        ),
        scope_provenance=resolved_scope,
        manual_origin_evidence=manual_tuple,
        causal_support=support_tuple,
        degradation_flags=tuple(dict.fromkeys(flags)),
    )


def _fact_matches_execution_plan(
    *,
    fact: AnswerFact,
    execution_plan: object,
    query_plan: QueryPlan | None,
    metric_contracts: Mapping[str, MetricContract] | None,
) -> bool:
    steps = getattr(execution_plan, "steps", ())
    step = next((item for item in steps if getattr(item, "step_id", None) == fact.step_id), None)
    if step is None:
        return False
    if fact.metric_key is not None:
        if getattr(step, "kind", None) == "fetch_metric":
            keys = tuple(getattr(step, "metric_keys", ()))
            if fact.metric_key not in keys:
                return False
        elif getattr(step, "kind", None) == "trusted_calculation":
            if fact.metric_key != getattr(step, "output_metric_key", None):
                return False
        else:
            return False
        if query_plan is not None:
            if fact.time_range is not None and query_plan.intent != "trend" and fact.time_range != query_plan.time_range:
                return False
        if metric_contracts is not None:
            metric = metric_contracts.get(fact.metric_key) or metric_contracts.get(fact.metric_key.removeprefix("metric."))
            if metric is None:
                return False
            expected_unit = metric.ratio.unit if metric.ratio is not None else "count"
            # Some pre-manifest injected runners do not project a unit onto
            # AnswerFact.  The independently rebound receipt is the authority
            # in that case; any checkpoint supplied unit must still match.
            if fact.unit is not None and fact.unit != expected_unit:
                return False
    elif getattr(step, "kind", None) == "ad_hoc_calculation":
        if (
            fact.calculation_scope != "ad_hoc_noncanonical"
            or fact.derived_output_id != getattr(step, "derived_output_id", None)
        ):
            return False
        spec = getattr(step, "calculation_spec", None)
        if spec is None:
            return False
        custom_unit_id = getattr(spec, "custom_unit_id", None)
        expected_unit = (
            custom_unit_id if getattr(spec, "unit", None) == "custom_unit"
            else getattr(spec, "unit", None)
        )
        if not isinstance(expected_unit, str) or fact.unit != expected_unit:
            return False
    else:
        return False
    if (
        getattr(step, "kind", None) == "trusted_calculation"
        and getattr(step, "output_metric_key", None) is None
        and fact.unit is not None
    ):
        return False
    return True


def _lookup_authoritative_unit(
    authoritative_units: Mapping[str, str | None] | None,
    metric_key: str | None,
) -> str | None:
    if authoritative_units is None or metric_key is None:
        return None
    if metric_key in authoritative_units:
        return authoritative_units[metric_key]
    return authoritative_units.get(metric_key.removeprefix("metric."))


def _has_authoritative_unit(
    authoritative_units: Mapping[str, str | None], metric_key: str | None
) -> bool:
    if metric_key is None:
        return False
    return (
        metric_key in authoritative_units
        or metric_key.removeprefix("metric.") in authoritative_units
    )


def _validate_checkpoint_fact_bindings(
    *,
    query_plan: QueryPlan,
    execution_plan: object,
    execution_record: PlanExecutionRecord,
    facts: tuple[AnswerFact, ...],
    authoritative_units: Mapping[str, str | None] | None,
    metric_contracts: Mapping[str, MetricContract] | None,
) -> None:
    receipts = {item.step_id: item for item in execution_record.step_receipts}
    for fact in facts:
        receipt = receipts.get(fact.step_id)
        if receipt is None or receipt.status != "succeeded":
            raise AnalysisEvidenceError("analysis_fact_execution_mismatch")
        if fact.output_digest != receipt.output_digest or fact.rowset_sha256 != receipt.rowset_sha256:
            raise AnalysisEvidenceError("analysis_fact_execution_mismatch")
        # Reconstruct an omitted legacy fact unit from the receipt only after
        # receipt.unit is independently rebound to authoritative_units below.
        # A checkpoint cannot introduce or change a unit through this path.
        if fact.unit is not None and fact.unit != receipt.unit:
            raise AnalysisEvidenceError("analysis_fact_unit_authority_missing")
        if receipt.unit is not None and _canonical_unit(receipt.unit) != receipt.unit:
            raise AnalysisEvidenceError("analysis_fact_unit_authority_missing")
        if fact.metric_key is not None:
            if authoritative_units is None:
                if receipt.unit is not None:
                    raise AnalysisEvidenceError(
                        "analysis_fact_unit_authority_missing"
                    )
            elif (
                not _has_authoritative_unit(authoritative_units, fact.metric_key)
                or receipt.unit
                != _lookup_authoritative_unit(authoritative_units, fact.metric_key)
            ):
                raise AnalysisEvidenceError("analysis_fact_unit_authority_missing")
        if not _fact_matches_execution_plan(
            fact=fact,
            execution_plan=execution_plan,
            query_plan=query_plan,
            metric_contracts=metric_contracts,
        ):
            raise AnalysisEvidenceError("analysis_fact_execution_mismatch")


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
            "scope_provenance": bundle.scope_provenance.model_dump(mode="json"),
            "degradation_flags": bundle.degradation_flags,
            "facts": [fact.model_dump(mode="json") for fact in bundle.facts],
            "manual_origin_evidence": [
                item.model_dump(mode="json") for item in bundle.manual_origin_evidence
            ],
            "causal_support_fact_ids": sorted(
                item.fact_id for item in bundle.causal_support
            ),
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
        "value or authoritative unit of one of the cited fact_ids or manual_evidence_ids. A unit attached to a value must match "
        "that same cited evidence's unit. Date/time fields may support only a "
        "complete ISO date/time reference in the exact supplied form; their year, "
        "month, day, hour or other components NEVER authorize a metric number. Do "
        "NOT write day counts, window lengths, percentages, years or averages that "
        "are not literally present in the cited values, and do not reformat numbers "
        "(write 93.20, never 93.2 or 93.2%). When in doubt, cite "
        "no unit unless the cited evidence has one. Unit text uses the closed "
        "runtime vocabulary: count/counts, percent/percentage/%, percentage "
        "point(s), order(s), store(s), user(s), person/people and explicit "
        "currency IDs; count never means an order, store or user, and percentage "
        "points never means percent. Do not invent a free-text unit alias. "
        "fewer facts and write fewer numbers. CAUSAL RULE: never assert that one "
        "thing caused, drove, explains or is the root cause of another unless the "
        "statement cites at least one fact listed in causal_support_fact_ids; "
        "otherwise describe association only and avoid causal connectives "
        "(导致/造成/引起/引发/因为/由于/归因/根本原因/cause/because/due to/led to). "
        "MANUAL ORIGIN RULE: manual_origin_evidence is human-asserted context, "
        "never an automated governed base-table fact; cite its evidence_id only in "
        "manual_evidence_ids, never in fact_ids. Return only the requested JSON "
        "object whose exact required shape is: "
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
    """Reject unknown references, untraceable numbers and unsupported causality.

    Three independent gates:
    * every cited identity must resolve (automated facts and manual evidence are
      separate namespaces);
    * a number may only appear when it is traceable to a cited basis;
    * a causal/attribution assertion must cite a fact that was explicitly
      declared as causal support -- otherwise the statement is refused with a
      stable code instead of being passed through as prose.
    """

    facts = {fact.fact_id: fact for fact in evidence.facts}
    manual_evidence = {
        item.evidence_id: item for item in evidence.manual_origin_evidence
    }
    causal_fact_ids = {item.fact_id for item in evidence.causal_support}
    statements = (
        interpretation.summary,
        *interpretation.observations,
        *interpretation.caveats,
    )
    for statement in statements:
        unknown = set(statement.fact_ids) - set(facts)
        if unknown:
            raise AnalysisEvidenceError("analysis_interpretation_unknown_fact")
        unknown_manual = set(statement.manual_evidence_ids) - set(manual_evidence)
        if unknown_manual:
            raise AnalysisEvidenceError(
                "analysis_interpretation_unknown_manual_evidence"
            )
        allowed_numbers: set[str] = set()
        allowed_dates: set[str] = set()
        numeric_units: dict[str, set[str | None]] = {}
        manual_numeric_units: dict[str, set[str | None]] = {}
        for fact_id in statement.fact_ids:
            fact = facts[fact_id]
            value_numbers = _numeric_tokens(_scalar_text(fact.value))
            allowed_numbers.update(value_numbers)
            allowed_dates.update(_fact_date_tokens(fact))
            for token in value_numbers:
                numeric_units.setdefault(token, set()).add(_canonical_unit(fact.unit))
        for evidence_id in statement.manual_evidence_ids:
            item = manual_evidence[evidence_id]
            value_numbers = _numeric_tokens(_scalar_text(item.value))
            allowed_numbers.update(value_numbers)
            for token in value_numbers:
                manual_numeric_units.setdefault(token, set()).add(
                    _canonical_unit(item.unit)
                )
        actual = _numeric_tokens(statement.text)
        actual_dates = _date_tokens(statement.text)
        if not actual_dates <= allowed_dates:
            raise AnalysisEvidenceError("analysis_interpretation_date_fact_unproven")
        if not actual <= allowed_numbers:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "unproven numeric statement text=%r missing=%s fact_ids=%s",
                statement.text,
                sorted(actual - allowed_numbers),
                list(statement.fact_ids),
            )
            raise AnalysisEvidenceError("analysis_interpretation_numeric_fact_unproven")
        for token, unit in _statement_numeric_units(statement.text):
            if unit is None:
                continue
            proven_units = numeric_units.get(token, set()) | manual_numeric_units.get(
                token, set()
            )
            if unit not in proven_units:
                raise AnalysisEvidenceError("analysis_interpretation_numeric_unit_unproven")
        if _causal_markers(statement.text):
            if not statement.fact_ids:
                raise AnalysisEvidenceError(
                    "analysis_interpretation_causal_claim_unproven"
                )
            if not set(statement.fact_ids) & causal_fact_ids:
                raise AnalysisEvidenceError(
                    "analysis_interpretation_causal_evidence_insufficient"
                )
    return interpretation


def _is_scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, (str, bool, int, float))
        and not (isinstance(value, float) and not math.isfinite(value))
    )


def _grounded_fact_id(fact: AnswerFact) -> str:
    if (
        fact.metric_key is None
        and fact.derived_output_id is not None
        and fact.calculation_scope == "ad_hoc_noncanonical"
    ):
        return _checksum(
            {
                "step_id": fact.step_id,
                "derived_output_id": fact.derived_output_id,
                "status": fact.status,
                "value": fact.value,
                "unit": fact.unit,
                "fact_id_version": 2,
            }
        )
    payload: dict[str, object] = {
        "step_id": fact.step_id,
        "metric_key": fact.metric_key,
        "status": fact.status,
        "value": fact.value,
    }
    if fact.fact_id_version in (None, 1):
        # Historical facts omitted both unit and version from their identity.
        pass
    elif fact.fact_id_version == 2:
        payload["unit"] = fact.unit
        payload["fact_id_version"] = 2
    if fact.time_range is not None:
        payload["time_range"] = fact.time_range.model_dump(mode="json")
    return _checksum(payload)


def _manual_evidence_id(
    *,
    label: str,
    value: AnalysisScalar,
    unit: str | None,
    asserted_by_role: str,
    attestation_ref: str,
    asserted_at: datetime | None,
) -> str:
    return _checksum(
        {
            "source_kind": "manual_origin",
            "label": label,
            "value": value,
            "unit": unit,
            "asserted_by_role": asserted_by_role,
            "attestation_ref": attestation_ref,
            "asserted_at": asserted_at.isoformat() if asserted_at is not None else None,
        }
    )


def _unique_present(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))


def _numeric_tokens(text: str) -> set[str]:
    masked = _mask_date_time(text)
    return {match.group(0) for match in _NUMBER_PATTERN.finditer(masked)}


def _scalar_text(value: AnalysisScalar) -> str:
    """Serialize only the scalar value; labels and provenance are not authority."""

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _fact_date_tokens(fact: AnalysisEvidenceFact) -> set[str]:
    values: list[str] = list(_date_tokens(_scalar_text(fact.value)))
    if fact.time_range is not None:
        values.extend(
            (fact.time_range.start.isoformat(), fact.time_range.end.isoformat())
        )
    for value in (fact.observed_at, fact.data_as_of):
        if value is not None:
            values.append(value.isoformat())
    return set(values)


def _date_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    covered: list[tuple[int, int]] = []
    for match in _DATE_TIME_PATTERN.finditer(text):
        token = match.group(0)
        if _is_complete_date_or_time(token):
            tokens.add(token)
        covered.append(match.span())
    for pattern in (_DATE_TIME_PATTERN, _CLOCK_TIME_PATTERN):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in covered):
                continue
            token = match.group(0)
            if _is_complete_date_or_time(token):
                tokens.add(token)
    return tokens


def _is_complete_date_or_time(value: str) -> bool:
    if _DATE_TIME_PATTERN.fullmatch(value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                date.fromisoformat(value)
            except ValueError:
                return False
            return True
        return True
    if _CLOCK_TIME_PATTERN.fullmatch(value):
        try:
            time.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def _mask_date_time(text: str) -> str:
    chars = list(text)
    spans = [match.span() for match in _DATE_TIME_PATTERN.finditer(text)]
    spans.extend(match.span() for match in _CLOCK_TIME_PATTERN.finditer(text))
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _canonical_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    normalized = " ".join(unit.strip().lower().split())
    return _PUNCTUATION_UNITS.get(
        normalized, _UNIT_ALIASES.get(normalized, normalized)
    )


def _match_unit_text(text: str, *, from_end: bool = False) -> str | None:
    normalized = text.lower().strip(" \t,:=()[]{}\"'")
    aliases = _UNIT_ALIASES_LONGEST
    if from_end:
        for alias in aliases:
            if not normalized.endswith(alias):
                continue
            start = len(normalized) - len(alias)
            if start == 0 or not normalized[start - 1].isalnum():
                return _canonical_unit(alias)
    else:
        for alias in aliases:
            if not normalized.startswith(alias):
                continue
            end = len(alias)
            if end == len(normalized) or not normalized[end].isalnum():
                return _canonical_unit(alias)
    match = (
        re.search(
            r"(?:[A-Za-z][A-Za-z0-9_.:-]*|[\u3400-\u9fff]{1,16})$",
            normalized,
        )
        if from_end
        else _UNIT_WORD_PATTERN.search(normalized)
    )
    if match is None:
        return None
    raw = match.group(0)
    if raw in _UNIT_STOP_WORDS:
        return None
    return _canonical_unit(raw)


def _statement_numeric_units(text: str) -> tuple[tuple[str, str | None], ...]:
    masked = _mask_date_time(text)
    result: list[tuple[str, str | None]] = []
    for match in _NUMBER_PATTERN.finditer(masked):
        token = match.group(0)
        suffix = masked[match.end() : match.end() + 64].lstrip(" \t,:=([{\"")
        unit: str | None = None
        if suffix:
            unit = _match_unit_text(suffix)
        if unit is None and match.start() > 0:
            prefix_text = masked[max(0, match.start() - 64) : match.start()].rstrip(
                " \t,:=([{"
            )
            if prefix_text:
                unit = _match_unit_text(prefix_text, from_end=True)
        result.append((token, unit))
    return tuple(result)


def _causal_markers(text: str) -> tuple[str, ...]:
    """Return causal/attribution connectives that assert a claim.

    A bounded negated form ("无法证明 X 导致 Y") is scrubbed first, so an explicit
    non-causal disclaimer is not mistaken for a causal assertion.
    """

    scrubbed = _CAUSAL_DISCLAIMER_PATTERN.sub(" ", text)
    lowered = scrubbed.lower()
    return tuple(marker for marker in _CAUSAL_MARKERS if marker in lowered)
