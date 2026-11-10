"""P4-Q acceptance harness: a PURE evaluator over already-captured typed evidence.

This module performs no I/O of any kind: no network, no HTTP, no database, no
QueryGateway call, no Backend call, no filesystem write, no environment read,
no clock read, no engine execution.  It consumes captured evidence and emits
exactly one of three verdicts: P4-Q-CONTRACT_READY, P4-Q-PASS, P4-Q-FAIL.

Fixture/local/synthetic evidence can never yield P4-Q-PASS: PASS additionally
requires every case to be explicitly "real" and a complete, consistent
RealnessWitness.  A supplied witness that contradicts actual evidence is a
FAIL, never a downgrade to CONTRACT_READY.  Positive data-bearing cases must
prove an INTACT evidence chain: the execution record is bound to the supplied
plan, the gateway rowset is bound to a successful fetch step receipt, and the
grounded answer facts are bound to the successful execution receipts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    ExecutionPlan,
    ExecutionReceipt,
    PlanExecutionRecord,
    PlanStepReceipt,
    ProductMode,
    TrustedCalculationStep,
)
from src.nl2sql.semantic.published_reader import PublishedMetricReadOutcome

P4Q_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

# Produced by grounding (AnswerArtifact.degradation_flags) when a successful
# calculation receipt could not be rebound to its real execution step.  The
# acceptance harness consumes it as a failure; it is NOT inferred from ordinary
# degradation (stale/unknown freshness, source_degradation, NO_DATA).
_GROUNDING_EXECUTION_MISMATCH: Final[str] = "grounding_execution_mismatch"

P4QVerdict = Literal["P4-Q-CONTRACT_READY", "P4-Q-PASS", "P4-Q-FAIL"]
P4QCaseKind = Literal[
    "published_gold",
    "approved_compute",
    "clarification_resume",
    "authorization_denied",
    "missing",
    "unavailable",
]
P4QTerminal = Literal["answered", "denied", "missing", "unavailable"]
# P4-Q observes the SAME shared product-mode vocabulary; this name is retained
# only as a compatibility alias and is no longer a duplicate literal.
P4QObservedMode = ProductMode
P4QEvidenceOrigin = Literal["fixture", "local_integration", "real"]
P4QAuthorizationOutcome = Literal["allow", "deny", "missing"]
P4QOracleKind = Literal[
    "independent_reference_sql",
    "independent_calculation",
    "approved_fact",
    "reviewed_label",
]

_SHA256 = r"^[0-9a-f]{64}$"
_MAX_CASES = 32

_KIND_TERMINAL: Final[dict[str, str]] = {
    "published_gold": "answered",
    "approved_compute": "answered",
    "clarification_resume": "answered",
    "authorization_denied": "denied",
    "missing": "missing",
    "unavailable": "unavailable",
}
_REQUIRED_KINDS: Final[tuple[str, ...]] = (
    "published_gold",
    "approved_compute",
    "clarification_resume",
    "authorization_denied",
    "missing",
    "unavailable",
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _strict_bool(value: object, info: ValidationInfo) -> bool:
    # Security flags must be genuine booleans: 1/1.0/"true" are rejected.
    if type(value) is not bool:
        raise ValueError(f"{info.field_name} must be a boolean")
    return value


def _strict_true(value: object, info: ValidationInfo) -> bool:
    if value is not True:
        raise ValueError(f"{info.field_name} must be the boolean True")
    return True


def _non_blank(value: str, info: ValidationInfo) -> str:
    if not value.strip():
        raise ValueError(f"{info.field_name} must not be blank")
    return value


class _FactDigestError(Exception):
    """Internal: an AnswerFact cannot be canonically hashed."""


def _canonical_value(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _FactDigestError("non-finite number")
        return value
    if isinstance(value, datetime):
        # A naive datetime must never be interpreted through the host timezone.
        if value.tzinfo is None or value.utcoffset() is None:
            raise _FactDigestError("naive datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _FactDigestError("non-finite number")
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _facts_sha256(artifact: AnswerArtifact) -> str:
    return _facts_sha256_from_facts(artifact.facts)


def _facts_sha256_from_facts(facts: Sequence[AnswerFact]) -> str:
    payload = [_canonical_value(fact.model_dump(mode="python")) for fact in facts]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class P4QCaseSpec(_StrictFrozenModel):
    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    case_id: str = Field(min_length=1, max_length=128)
    kind: P4QCaseKind
    question: str = Field(min_length=1, max_length=2000)
    expected_terminal: P4QTerminal

    @field_validator("case_id", "question")
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @model_validator(mode="after")
    def _kind_defines_terminal(self) -> "P4QCaseSpec":
        if self.expected_terminal != _KIND_TERMINAL[self.kind]:
            raise ValueError("case kind cannot redefine its expected terminal")
        return self


class P4QCaseRegistry(_StrictFrozenModel):
    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    registry_revision: str = Field(min_length=1, max_length=128)
    cases: tuple[P4QCaseSpec, ...] = Field(min_length=1, max_length=_MAX_CASES)

    @field_validator("registry_revision")
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @model_validator(mode="after")
    def _bounded_and_complete(self) -> "P4QCaseRegistry":
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("registry case ids must be unique")
        present = {case.kind for case in self.cases}
        missing = [kind for kind in _REQUIRED_KINDS if kind not in present]
        if missing:
            raise ValueError(f"registry is missing required case kind: {missing[0]}")
        return self


class P4QOracleEvidence(_StrictFrozenModel):
    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    oracle_kind: P4QOracleKind
    oracle_revision: str = Field(min_length=1, max_length=128)
    expected_terminal: P4QTerminal
    evidence_sha256: str = Field(pattern=_SHA256)
    independent_of_system_under_test: Literal[True]
    expected_answer_sha256: str | None = Field(default=None, pattern=_SHA256)
    expected_facts_sha256: str | None = Field(default=None, pattern=_SHA256)
    reference_sql_fingerprint: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("oracle_revision")
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @field_validator("independent_of_system_under_test", mode="before")
    @classmethod
    def _must_be_true(cls, value: object, info: ValidationInfo) -> bool:
        return _strict_true(value, info)

    @model_validator(mode="after")
    def _complete_oracle(self) -> "P4QOracleEvidence":
        if self.expected_terminal == "answered":
            if self.expected_answer_sha256 is None or self.expected_facts_sha256 is None:
                raise ValueError("an answered oracle requires answer and facts digests")
        if self.oracle_kind == "independent_reference_sql":
            if self.reference_sql_fingerprint is None:
                raise ValueError("a reference-sql oracle requires a sql fingerprint")
        return self


class P4QRealnessWitness(_StrictFrozenModel):
    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    implementation_revision: str = Field(min_length=1, max_length=256)
    backend_authorization_revision: str = Field(min_length=1, max_length=256)
    backend_authorization_evidence_sha256: str = Field(pattern=_SHA256)
    semantic_release_id: str = Field(min_length=1, max_length=128)
    semantic_release_checksum: str = Field(pattern=_SHA256)
    published_source_id: str = Field(min_length=1, max_length=128)
    active_binding_evidence_sha256: str = Field(pattern=_SHA256)
    datasource: str = Field(min_length=1, max_length=256)
    readonly_role: str = Field(min_length=1, max_length=128)
    product_identity: str = Field(min_length=1, max_length=256)
    query_gateway_evidence_sha256: str = Field(pattern=_SHA256)
    reference_oracle_bundle_sha256: str = Field(pattern=_SHA256)
    remote_business_db_evidence_sha256: str = Field(pattern=_SHA256)
    captured_at: datetime

    @field_validator(
        "implementation_revision",
        "backend_authorization_revision",
        "semantic_release_id",
        "published_source_id",
        "datasource",
        "readonly_role",
        "product_identity",
    )
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @field_validator("captured_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value


class P4QGroundedAnswerCapture(_StrictFrozenModel):
    """Transient trusted-harness capture of the production GroundedAnswer."""

    answer_text: str = Field(min_length=1, max_length=32_000)
    facts: tuple[AnswerFact, ...] = Field(max_length=64)
    renderer_version: str = Field(default="grounded-renderer-v1", min_length=1, max_length=128)
    answer_sha256: str | None = Field(default=None, pattern=_SHA256)
    facts_sha256: str | None = Field(default=None, pattern=_SHA256)
    query_plan_sha256: str | None = Field(default=None, pattern=_SHA256)
    execution_plan_sha256: str | None = Field(default=None, pattern=_SHA256)
    execution_record_sha256: str | None = Field(default=None, pattern=_SHA256)

    @classmethod
    def from_grounded_answer(
        cls,
        grounded: object,
        *,
        query_plan: object | None = None,
        execution_plan: object | None = None,
        execution_record: object | None = None,
    ) -> "P4QGroundedAnswerCapture":
        answer_text = str(getattr(grounded, "answer_text"))
        facts = tuple(getattr(grounded, "facts"))
        if query_plan is not None:
            from src.nl2sql.orchestration.grounding import render_grounded_answer

            if render_grounded_answer(query_plan=cast(Any, query_plan), facts=facts) != answer_text:
                raise ValueError("grounded answer is not canonical renderer output")
        record_checksum = None
        if execution_record is not None:
            record_checksum = hashlib.sha256(
                json.dumps(cast(Any, execution_record).model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        return cls(
            answer_text=answer_text,
            facts=facts,
            answer_sha256=hashlib.sha256(answer_text.replace("\r\n", "\n").encode("utf-8")).hexdigest(),
            facts_sha256=_facts_sha256_from_facts(facts),
            query_plan_sha256=getattr(query_plan, "checksum", None),
            execution_plan_sha256=getattr(execution_plan, "checksum", None),
            execution_record_sha256=record_checksum,
        )


def capture_grounded_answer(
    grounded: object,
    *,
    query_plan: object | None = None,
    execution_plan: object | None = None,
    execution_record: object | None = None,
) -> P4QGroundedAnswerCapture:
    """Capture a request-local production GroundedAnswer for P4-Q."""

    return P4QGroundedAnswerCapture.from_grounded_answer(
        grounded,
        query_plan=query_plan,
        execution_plan=execution_plan,
        execution_record=execution_record,
    )


class P4QCaseEvidence(_StrictFrozenModel):
    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    case_id: str = Field(min_length=1, max_length=128)
    evidence_origin: P4QEvidenceOrigin
    implementation_revision: str = Field(min_length=1, max_length=256)
    observed_terminal: P4QTerminal
    observed_mode: P4QObservedMode
    codeact_used: bool
    authorization_outcome: P4QAuthorizationOutcome
    hitl_pause_observed: bool
    hitl_resume_observed: bool
    pre_resume_sql_executions: int = Field(ge=0)
    sql_execution_count: int = Field(ge=0)
    execution_plan: ExecutionPlan | None = None
    execution_record: PlanExecutionRecord | None = None
    gateway_receipt: ExecutionReceipt | None = None
    published_read_outcome: PublishedMetricReadOutcome | None = None
    answer_artifact: AnswerArtifact | None = None
    captured_grounded_answer: P4QGroundedAnswerCapture | None = None
    # Optional harness capture from the rendered production response.  When
    # present, the observed answer digest is recomputed from this value; the
    # legacy digest-only field remains accepted for older fixture evidence.
    captured_answer_text: str | None = Field(default=None, max_length=32_000)
    observed_answer_sha256: str | None = Field(default=None, pattern=_SHA256)
    observed_facts_sha256: str | None = Field(default=None, pattern=_SHA256)
    oracle: P4QOracleEvidence

    @field_validator("codeact_used", "hitl_pause_observed", "hitl_resume_observed", mode="before")
    @classmethod
    def _flags_are_booleans(cls, value: object, info: ValidationInfo) -> bool:
        return _strict_bool(value, info)

    @field_validator("case_id", "implementation_revision")
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)


class P4QEvidenceBundle(_StrictFrozenModel):
    """Versioned, signed import unit for one P4-Q run.

    Checksums alone are not an authenticity boundary: the evaluator verifies
    the signature against a server-configured key before a bundle can remove
    the raw-evidence readiness gap.
    """

    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    bundle_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    source_revision: str = Field(min_length=1, max_length=256)
    signer_key_id: str = Field(min_length=1, max_length=128)
    registry: P4QCaseRegistry
    evidence: tuple[P4QCaseEvidence, ...] = Field(max_length=_MAX_CASES)
    realness_witness: P4QRealnessWitness | None = None
    bundle_sha256: str = Field(pattern=_SHA256)
    signature_sha256: str = Field(pattern=_SHA256)

    @field_validator("bundle_id", "source_id", "source_revision", "signer_key_id")
    @classmethod
    def _not_blank(cls, value: str, info: ValidationInfo) -> str:
        return _non_blank(value, info)

    @model_validator(mode="after")
    def _unique_known_cases(self) -> "P4QEvidenceBundle":
        evidence_ids = [item.case_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence bundle contains duplicate cases")
        known = {item.case_id for item in self.registry.cases}
        if set(evidence_ids) - known:
            raise ValueError("evidence bundle contains unknown cases")
        if any(
            item.implementation_revision != self.source_revision
            for item in self.evidence
        ):
            raise ValueError("evidence bundle implementation revision mismatch")
        if self.realness_witness is not None and (
            self.realness_witness.implementation_revision != self.source_revision
        ):
            raise ValueError("evidence bundle revision does not match realness witness")
        return self


class P4QCaseResult(_StrictFrozenModel):
    case_id: str = Field(min_length=1, max_length=128)
    kind: P4QCaseKind
    passed: bool
    failures: tuple[str, ...] = ()

    @field_validator("passed", mode="before")
    @classmethod
    def _passed_is_boolean(cls, value: object, info: ValidationInfo) -> bool:
        return _strict_bool(value, info)

    @model_validator(mode="after")
    def _passed_matches_failures(self) -> "P4QCaseResult":
        if self.passed != (len(self.failures) == 0):
            raise ValueError("passed must be True exactly when failures is empty")
        return self


class P4QManifest(_StrictFrozenModel):
    schema_version: Literal["1.0"] = P4Q_SCHEMA_VERSION
    implementation_revision: str = ""
    registry_revision: str = Field(min_length=1, max_length=128)
    verdict: P4QVerdict
    case_results: tuple[P4QCaseResult, ...] = ()
    readiness_gaps: tuple[str, ...] = ()
    integrity_failures: tuple[str, ...] = ()
    realness_witness_present: bool

    @field_validator("realness_witness_present", mode="before")
    @classmethod
    def _present_is_boolean(cls, value: object, info: ValidationInfo) -> bool:
        return _strict_bool(value, info)

    @model_validator(mode="after")
    def _verdict_is_consistent(self) -> "P4QManifest":
        all_passed = bool(self.case_results) and all(r.passed for r in self.case_results)
        has_failed = any(not r.passed for r in self.case_results)
        if self.verdict == "P4-Q-PASS":
            if not all_passed:
                raise ValueError("PASS requires a non-empty all-passing case set")
            if any(r.failures for r in self.case_results):
                raise ValueError("PASS requires every case to carry no failures")
            if self.readiness_gaps:
                raise ValueError("PASS requires no readiness gaps")
            if self.integrity_failures:
                raise ValueError("PASS requires no integrity failures")
            if not self.realness_witness_present:
                raise ValueError("PASS requires a realness witness")
        elif self.verdict == "P4-Q-CONTRACT_READY":
            if not all_passed:
                raise ValueError("CONTRACT_READY requires a non-empty all-passing case set")
            if any(r.failures for r in self.case_results):
                raise ValueError("CONTRACT_READY requires every case to carry no failures")
            if self.integrity_failures:
                raise ValueError("CONTRACT_READY requires no integrity failures")
            if not self.readiness_gaps:
                raise ValueError("CONTRACT_READY requires at least one readiness gap")
        elif not has_failed and not self.integrity_failures:
            raise ValueError("FAIL requires a failed case or an explicit contradiction")
        return self


def _plan_bound_successful_receipts(evidence: P4QCaseEvidence) -> dict[str, PlanStepReceipt]:
    """Successful receipts that are STRUCTURALLY bound to the supplied plan."""

    plan = evidence.execution_plan
    record = evidence.execution_record
    if plan is None or record is None:
        return {}
    kinds = {step.step_id: step.kind for step in plan.steps}
    return {
        receipt.step_id: receipt
        for receipt in record.step_receipts
        if receipt.status == "succeeded" and kinds.get(receipt.step_id) == receipt.kind
    }


def _plan_record_structure_failures(evidence: P4QCaseEvidence) -> list[str]:
    """A record must not expand execution authority beyond the supplied plan."""

    record = evidence.execution_record
    if record is None:
        return []
    plan = evidence.execution_plan
    if plan is None:
        return ["execution_plan_record_mismatch"]
    if any(step.kind == "verify" for step in plan.steps) or evidence.evidence_origin == "real":
        try:
            from src.nl2sql.orchestration.execution import validate_execution_record_against_plan

            validate_execution_record_against_plan(plan, record)
        except Exception as exc:
            code = getattr(exc, "code", "execution_plan_record_mismatch")
            return [str(code)]
    kinds = {step.step_id: step.kind for step in plan.steps}
    failures: list[str] = []
    for receipt in record.step_receipts:
        if kinds.get(receipt.step_id) != receipt.kind:
            failures.append("execution_plan_record_mismatch")
    if record.status == "succeeded":
        if {r.step_id for r in record.step_receipts} != set(kinds):
            failures.append("execution_plan_record_mismatch")
    return list(dict.fromkeys(failures))


def _receipt_failures(receipt: ExecutionReceipt) -> list[str]:
    failures: list[str] = []
    if receipt.policy_outcome != "allow":
        failures.append("gateway_receipt_invalid")
    if receipt.authorization_revision is None:
        failures.append("gateway_receipt_invalid")
    if not receipt.sql_fingerprint:
        failures.append("gateway_receipt_invalid")
    if receipt.rowset_sha256 is None:
        failures.append("gateway_receipt_invalid")
    if receipt.row_count <= 0:
        failures.append("gateway_receipt_invalid")
    if not receipt.datasource or not receipt.readonly_role:
        failures.append("gateway_receipt_invalid")
    return list(dict.fromkeys(failures))


def _execution_binding_failures(
    evidence: P4QCaseEvidence,
    *,
    require_receipt: bool,
) -> list[str]:
    failures: list[str] = []
    if evidence.authorization_outcome != "allow":
        failures.append("authorization_mismatch")
    plan = evidence.execution_plan
    record = evidence.execution_record
    if plan is None or record is None:
        failures.append("execution_missing")
    elif record.status != "succeeded":
        failures.append("execution_failed")
    if plan is not None and record is not None and record.execution_plan_checksum != plan.checksum:
        failures.append("execution_plan_record_mismatch")
    receipt = evidence.gateway_receipt
    if receipt is None:
        if require_receipt:
            failures.append("gateway_receipt_missing")
    else:
        # An OPTIONAL receipt that IS supplied is still evidence: validate it.
        failures.extend(_receipt_failures(receipt))
        rowset = receipt.rowset_sha256
        bound = rowset is not None and any(
            r.kind == "fetch_metric" and r.rowset_sha256 == rowset
            for r in _plan_bound_successful_receipts(evidence).values()
        )
        if not bound:
            failures.append("gateway_execution_evidence_mismatch")
    plan_bound = _plan_bound_successful_receipts(evidence)
    proven = receipt is not None or any(r.kind == "fetch_metric" for r in plan_bound.values())
    if proven and evidence.sql_execution_count == 0:
        failures.append("sql_execution_count_mismatch")
    return list(dict.fromkeys(failures))


def _grounding_failures(evidence: P4QCaseEvidence) -> list[str]:
    artifact = evidence.answer_artifact
    if artifact is None:
        return ["grounded_answer_missing"]
    grounded = [fact for fact in artifact.facts if fact.status == "grounded"]
    failures: list[str] = []
    if _GROUNDING_EXECUTION_MISMATCH in artifact.degradation_flags:
        # The producer (grounding) refused a fact because a successful
        # calculation receipt could not be rebound to its real execution step.
        # That is an acceptance failure, not ordinary degradation.
        failures.append(_GROUNDING_EXECUTION_MISMATCH)
    if not grounded or all(fact.value is None for fact in grounded):
        failures.append("grounded_answer_missing")
    receipts = _plan_bound_successful_receipts(evidence)
    for fact in grounded:
        receipt = receipts.get(fact.step_id)
        if receipt is None:
            failures.append("grounding_execution_mismatch")
            continue
        if receipt.kind not in ("fetch_metric", "trusted_calculation"):
            # A verify step proves an invariant, never a business value.
            failures.append("grounding_execution_mismatch")
            continue
        if fact.output_digest is None or fact.output_digest != receipt.output_digest:
            failures.append("grounding_execution_mismatch")
        if receipt.rowset_sha256 is not None and fact.rowset_sha256 != receipt.rowset_sha256:
            failures.append("grounding_execution_mismatch")
    return list(dict.fromkeys(failures))


def _answer_oracle_failures(evidence: P4QCaseEvidence) -> list[str]:
    failures: list[str] = []
    capture = evidence.captured_grounded_answer
    if capture is not None:
        captured_text = capture.answer_text.replace("\r\n", "\n")
        answer_digest = hashlib.sha256(captured_text.encode("utf-8")).hexdigest()
        try:
            facts_digest = _facts_sha256_from_facts(capture.facts)
        except (ValueError, _FactDigestError):
            failures.append("facts_digest_invalid")
            facts_digest = None
        # Caller supplied hashes are ignored when a trusted capture exists.
        artifact = evidence.answer_artifact
        if artifact is not None and tuple(artifact.facts) != tuple(capture.facts):
            failures.append("captured_facts_mismatch")
        if capture.answer_sha256 != answer_digest or capture.facts_sha256 != facts_digest:
            failures.append("capture_digest_mismatch")
        if evidence.execution_plan is not None and capture.execution_plan_sha256 != evidence.execution_plan.checksum:
            failures.append("capture_execution_plan_mismatch")
        if (
            evidence.execution_plan is not None
            and capture.query_plan_sha256 != evidence.execution_plan.query_plan_sha256
        ):
            failures.append("capture_query_plan_mismatch")
        if evidence.execution_record is not None:
            record_digest = hashlib.sha256(
                json.dumps(
                    evidence.execution_record.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if capture.execution_record_sha256 != record_digest:
                failures.append("capture_execution_record_mismatch")
    elif evidence.captured_answer_text is not None:
        captured_text = evidence.captured_answer_text.replace("\r\n", "\n")
        answer_digest = hashlib.sha256(captured_text.encode("utf-8")).hexdigest()
        if evidence.observed_answer_sha256 != answer_digest:
            failures.append("answer_capture_digest_mismatch")
        answer_digest = evidence.observed_answer_sha256
        try:
            facts_digest = _facts_sha256(evidence.answer_artifact) if evidence.answer_artifact is not None else evidence.observed_facts_sha256
        except (ValueError, _FactDigestError):
            failures.append("facts_digest_invalid")
            facts_digest = None
    else:
        answer_digest = evidence.observed_answer_sha256
        try:
            facts_digest = _facts_sha256(evidence.answer_artifact) if evidence.answer_artifact is not None else evidence.observed_facts_sha256
        except (ValueError, _FactDigestError):
            failures.append("facts_digest_invalid")
            facts_digest = None
        if evidence.answer_artifact is not None and evidence.observed_facts_sha256 != facts_digest:
            failures.append("facts_digest_mismatch")
    if facts_digest != evidence.oracle.expected_facts_sha256:
        failures.append("facts_digest_mismatch")
    if answer_digest != evidence.oracle.expected_answer_sha256:
        failures.append("answer_digest_mismatch")
    return list(dict.fromkeys(failures))


def _gold_failures(evidence: P4QCaseEvidence) -> list[str]:
    outcome = evidence.published_read_outcome
    if outcome is None:
        return ["published_read_missing"]
    failures: list[str] = []
    if outcome.receipt.status != "succeeded" or outcome.receipt.failure_code is not None:
        failures.append("published_read_failed")
    if len(outcome.results) != 1:
        failures.append("published_read_failed")
    else:
        result = outcome.results[0]
        if result.status not in ("success", "partial") or result.value is None:
            failures.append("published_read_failed")
    receipts = _plan_bound_successful_receipts(evidence)
    artifact = evidence.answer_artifact
    grounded = (
        []
        if artifact is None
        else [fact for fact in artifact.facts if fact.status == "grounded" and fact.value is not None]
    )
    if not any(
        (candidate := receipts.get(fact.step_id)) is not None and candidate.kind == "fetch_metric"
        for fact in grounded
    ):
        failures.append("grounding_execution_mismatch")
    return list(dict.fromkeys(failures))


def _approved_compute_failures(evidence: P4QCaseEvidence) -> list[str]:
    plan = evidence.execution_plan
    if plan is None:
        return ["execution_missing"]
    calculation_steps = [s for s in plan.steps if isinstance(s, TrustedCalculationStep)]
    if not calculation_steps:
        return ["trusted_calculation_missing"]
    failures: list[str] = []
    calculation_ids = {step.step_id for step in calculation_steps}
    receipts = _plan_bound_successful_receipts(evidence)
    succeeded_calculations = {
        step_id
        for step_id, receipt in receipts.items()
        if receipt.kind == "trusted_calculation" and step_id in calculation_ids
    }
    if succeeded_calculations != calculation_ids:
        failures.append("trusted_calculation_missing")
    artifact = evidence.answer_artifact
    grounded = [] if artifact is None else [f for f in artifact.facts if f.status == "grounded"]
    bound_to_calculation = any(
        (receipt := receipts.get(fact.step_id)) is not None
        and receipt.kind == "trusted_calculation"
        and fact.output_digest == receipt.output_digest
        for fact in grounded
    )
    if not bound_to_calculation:
        failures.append("trusted_calculation_grounding_mismatch")
    return list(dict.fromkeys(failures))


def _clarification_failures(evidence: P4QCaseEvidence) -> list[str]:
    failures: list[str] = []
    if not evidence.hitl_pause_observed or not evidence.hitl_resume_observed:
        failures.append("hitl_missing")
    if evidence.pre_resume_sql_executions != 0:
        failures.append("execution_before_resume")
    return failures


def _denied_failures(evidence: P4QCaseEvidence) -> list[str]:
    failures: list[str] = []
    if evidence.authorization_outcome not in ("deny", "missing"):
        failures.append("authorization_mismatch")
    if evidence.sql_execution_count != 0:
        failures.append("unexpected_sql_execution")
    if evidence.gateway_receipt is not None:
        failures.append("unexpected_gateway_receipt")
    if evidence.published_read_outcome is not None:
        failures.append("unexpected_published_read")
    record = evidence.execution_record
    if record is not None and (
        record.status == "succeeded"
        or any(r.status == "succeeded" for r in record.step_receipts)
    ):
        failures.append("unexpected_execution_evidence")
    artifact = evidence.answer_artifact
    if artifact is not None and any(
        fact.status == "grounded" and fact.value is not None for fact in artifact.facts
    ):
        failures.append("unexpected_grounded_facts")
    return list(dict.fromkeys(failures))


def _missing_failures(evidence: P4QCaseEvidence) -> list[str]:
    failures: list[str] = []
    if evidence.authorization_outcome != "allow":
        failures.append("authorization_mismatch")
    if evidence.sql_execution_count != 0:
        failures.append("unexpected_execution_evidence")
    if evidence.gateway_receipt is not None:
        failures.append("unexpected_execution_evidence")
    record = evidence.execution_record
    if record is not None and (
        record.status == "succeeded" or any(r.status == "succeeded" for r in record.step_receipts)
    ):
        failures.append("unexpected_execution_evidence")
    outcome = evidence.published_read_outcome
    if outcome is None:
        return [*failures, "published_read_missing"]
    if outcome.receipt.status != "succeeded" or outcome.receipt.failure_code is not None:
        failures.append("published_read_failed")
    if len(outcome.results) != 1 or outcome.results[0].status != "missing":
        failures.append("published_read_failed")
    elif outcome.results[0].value is not None:
        failures.append("published_read_failed")
    artifact = evidence.answer_artifact
    if artifact is not None and any(
        fact.status == "grounded" and fact.value is not None for fact in artifact.facts
    ):
        failures.append("unexpected_grounded_facts")
    return list(dict.fromkeys(failures))


def _unavailable_failures(evidence: P4QCaseEvidence) -> list[str]:
    failures: list[str] = []
    if evidence.sql_execution_count > 0:
        failures.append("unexpected_execution_evidence")
    record = evidence.execution_record
    if record is not None and (
        record.status == "succeeded" or any(r.status == "succeeded" for r in record.step_receipts)
    ):
        failures.append("unexpected_execution_evidence")
    receipt = evidence.gateway_receipt
    if receipt is not None and receipt.policy_outcome == "allow" and receipt.row_count > 0:
        failures.append("unexpected_execution_evidence")
    outcome = evidence.published_read_outcome
    if outcome is not None and outcome.receipt.status == "succeeded":
        failures.append("unexpected_execution_evidence")
        if any(result.status == "missing" for result in outcome.results):
            failures.append("synthesized_missing")
    artifact = evidence.answer_artifact
    if artifact is not None and any(
        fact.status == "grounded" and fact.value is not None for fact in artifact.facts
    ):
        failures.append("unexpected_grounded_facts")
    return list(dict.fromkeys(failures))


def _case_failures(case: P4QCaseSpec, evidence: P4QCaseEvidence) -> list[str]:
    failures: list[str] = []
    if evidence.observed_mode != "QUERY":
        failures.append("mode_escalation")
    if evidence.codeact_used:
        failures.append("codeact_used")
    if evidence.observed_terminal != case.expected_terminal:
        failures.append("wrong_terminal")
    if evidence.oracle.expected_terminal != case.expected_terminal:
        failures.append("oracle_invalid")
    failures.extend(_plan_record_structure_failures(evidence))
    if evidence.execution_record is not None and any(
        receipt.semantic_validation_outcome == "shadow_denied"
        for receipt in evidence.execution_record.step_receipts
    ):
        failures.append("not_certified")
    if evidence.evidence_origin == "real" and case.kind in {
        "published_gold",
        "approved_compute",
        "clarification_resume",
    } and evidence.captured_grounded_answer is None:
        failures.append("trusted_render_capture_missing")
    if case.kind == "published_gold":
        failures.extend(_execution_binding_failures(evidence, require_receipt=True))
        failures.extend(_gold_failures(evidence))
        failures.extend(_grounding_failures(evidence))
        failures.extend(_answer_oracle_failures(evidence))
    elif case.kind == "approved_compute":
        failures.extend(_execution_binding_failures(evidence, require_receipt=True))
        failures.extend(_approved_compute_failures(evidence))
        failures.extend(_grounding_failures(evidence))
        failures.extend(_answer_oracle_failures(evidence))
    elif case.kind == "clarification_resume":
        failures.extend(_clarification_failures(evidence))
        failures.extend(_execution_binding_failures(evidence, require_receipt=False))
        failures.extend(_grounding_failures(evidence))
        failures.extend(_answer_oracle_failures(evidence))
    elif case.kind == "authorization_denied":
        failures.extend(_denied_failures(evidence))
    elif case.kind == "missing":
        failures.extend(_missing_failures(evidence))
    else:
        failures.extend(_unavailable_failures(evidence))
    return list(dict.fromkeys(failures))


def _evidence_integrity_failures(
    registry: P4QCaseRegistry,
    evidence: Sequence[P4QCaseEvidence],
) -> list[str]:
    cases = [item.case_id for item in evidence]
    known = {case.case_id for case in registry.cases}
    failures: list[str] = []
    if any(cases.count(case_id) > 1 for case_id in cases):
        failures.append("duplicate_case_evidence")
    if any(case_id not in known for case_id in cases):
        failures.append("unknown_case_evidence")
    if len({item.implementation_revision for item in evidence}) > 1:
        failures.append("implementation_revision_mismatch")
    return failures


def _realness_mismatches(
    witness: P4QRealnessWitness,
    evidence: Sequence[P4QCaseEvidence],
) -> list[str]:
    mismatches: list[str] = []
    for item in evidence:
        if item.implementation_revision != witness.implementation_revision:
            mismatches.append("realness_mismatch")
        outcome = item.published_read_outcome
        if outcome is not None:
            binding = outcome.request.binding
            if (
                binding.semantic_release_id != witness.semantic_release_id
                or binding.semantic_release_checksum != witness.semantic_release_checksum
                or binding.published_source_id != witness.published_source_id
            ):
                mismatches.append("realness_mismatch")
        receipt = item.gateway_receipt
        if receipt is not None:
            if (
                receipt.datasource != witness.datasource
                or receipt.readonly_role != witness.readonly_role
                or receipt.authorization_revision != witness.backend_authorization_revision
            ):
                mismatches.append("realness_mismatch")
    return list(dict.fromkeys(mismatches))


def _bundle_payload(bundle: P4QEvidenceBundle) -> bytes:
    payload = bundle.model_dump(
        mode="json", exclude={"bundle_sha256", "signature_sha256"}
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _verify_bundle_signature(
    bundle: P4QEvidenceBundle,
    trusted_signing_keys: Mapping[str, bytes] | None,
) -> bool:
    if trusted_signing_keys is None:
        return False
    key = trusted_signing_keys.get(bundle.signer_key_id)
    if not isinstance(key, bytes) or len(key) < 32:
        return False
    payload = _bundle_payload(bundle)
    digest = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(digest, bundle.bundle_sha256):
        return False
    expected_signature = hmac.new(key, bytes.fromhex(digest), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_signature, bundle.signature_sha256)


def ingest_p4q_evidence_bundle(
    payload: Mapping[str, object],
    *,
    trusted_signing_keys: Mapping[str, bytes],
) -> P4QEvidenceBundle:
    """Validate schema, case identity, checksum, and trusted source signature."""

    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    bundle = P4QEvidenceBundle.model_validate_json(encoded)
    if not _verify_bundle_signature(bundle, trusted_signing_keys):
        raise ValueError("P4-Q evidence bundle authentication failed")
    return bundle


def evaluate_p4q(
    registry: P4QCaseRegistry,
    evidence: Sequence[P4QCaseEvidence],
    *,
    realness_witness: P4QRealnessWitness | None = None,
    evidence_bundle: P4QEvidenceBundle | None = None,
    trusted_signing_keys: Mapping[str, bytes] | None = None,
) -> P4QManifest:
    """Pure, deterministic evaluation of one bounded P4-Q acceptance run.

    Raw case evidence can establish contract readiness, but only a versioned
    bundle authenticated against the configured trusted signer can establish
    P4-Q-PASS eligibility. A self-recomputed unsigned hash is never authority.
    """

    bundle_verified = False
    if evidence_bundle is not None:
        if evidence or registry != evidence_bundle.registry:
            bundle_valid = False
        else:
            bundle_valid = _verify_bundle_signature(
                evidence_bundle, trusted_signing_keys
            )
        if bundle_valid:
            bundle_verified = True
            evidence = evidence_bundle.evidence
            realness_witness = evidence_bundle.realness_witness
        else:
            evidence = evidence_bundle.evidence
            realness_witness = evidence_bundle.realness_witness

    by_case_id: dict[str, P4QCaseEvidence] = {}
    for item in evidence:
        by_case_id.setdefault(item.case_id, item)
    case_results: list[P4QCaseResult] = []
    for case in registry.cases:
        item = by_case_id.get(case.case_id)
        if item is None:
            case_results.append(
                P4QCaseResult(
                    case_id=case.case_id,
                    kind=case.kind,
                    passed=False,
                    failures=("case_evidence_missing",),
                )
            )
            continue
        failures = tuple(_case_failures(case, item))
        case_results.append(
            P4QCaseResult(case_id=case.case_id, kind=case.kind, passed=not failures, failures=failures)
        )

    ordered = [by_case_id[case.case_id] for case in registry.cases if case.case_id in by_case_id]
    integrity_failures = _evidence_integrity_failures(registry, evidence)
    if evidence_bundle is not None and not bundle_verified:
        integrity_failures.append("evidence_bundle_authentication_failed")
    if realness_witness is not None:
        integrity_failures.extend(_realness_mismatches(realness_witness, ordered))

    any_failed = any(not result.passed for result in case_results)
    readiness_gaps: list[str] = []
    if not any_failed and not integrity_failures:
        if any(item.evidence_origin != "real" for item in ordered):
            readiness_gaps.append("non_real_evidence_origin")
        if realness_witness is None:
            readiness_gaps.append("realness_witness_absent")
        if not bundle_verified:
            readiness_gaps.append("trusted_evidence_bundle_unverified")

    if any_failed or integrity_failures:
        verdict: P4QVerdict = "P4-Q-FAIL"
    elif readiness_gaps:
        verdict = "P4-Q-CONTRACT_READY"
    else:
        verdict = "P4-Q-PASS"

    case_revisions = {item.implementation_revision for item in ordered}
    implementation_revision = ""
    if len(case_revisions) == 1:
        implementation_revision = next(iter(case_revisions))
    elif realness_witness is not None:
        implementation_revision = realness_witness.implementation_revision

    return P4QManifest(
        implementation_revision=implementation_revision,
        registry_revision=registry.registry_revision,
        verdict=verdict,
        case_results=tuple(case_results),
        readiness_gaps=tuple(readiness_gaps),
        integrity_failures=tuple(dict.fromkeys(integrity_failures)),
        realness_witness_present=realness_witness is not None,
    )


__all__ = [
    "P4Q_SCHEMA_VERSION",
    "P4QAuthorizationOutcome",
    "P4QCaseEvidence",
    "P4QCaseKind",
    "P4QCaseRegistry",
    "P4QCaseResult",
    "P4QCaseSpec",
    "P4QGroundedAnswerCapture",
    "P4QEvidenceOrigin",
    "P4QEvidenceBundle",
    "P4QManifest",
    "P4QObservedMode",
    "P4QOracleEvidence",
    "P4QOracleKind",
    "P4QRealnessWitness",
    "P4QTerminal",
    "P4QVerdict",
    "capture_grounded_answer",
    "evaluate_p4q",
    "ingest_p4q_evidence_bundle",
]
