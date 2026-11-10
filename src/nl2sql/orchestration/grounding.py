"""Pure, deterministic grounded-answer construction from request evidence.

This module is the ONLY reader of PlanExecutionResult.outputs.  The executor
keeps that map request-local (see execution.PlanExecutionResult): it is the
sole grounding material and must never enter engine state, a checkpoint, or a
message.  Only the rendered answer text produced here may persist in
conversation history.

Chosen granularity (open choice b): ONE fact per successful fetch_metric step
output.  The typed metric path admits exactly one metric key per fetch step
(metric_query.py rejects any other shape), so this is one fact per step output
and never one fact per raw row.  A per-row rule is deliberately NOT used:
output rows are schemaless and carry no stable per-row identity that could be
grounded, so a row-derived fact id would not be reproducible.

Internal dependency fetches are excluded: a fetch step that declares a trusted
calculation input role supplies one governed calculation input, not a requested
answer.  Only the bound calculation's own receipt grounds the canonical metric,
so the requested value has exactly ONE authority and exactly one fact.

Confidence (open choice a): confidence_band is always absent.  No deterministic
producer for a band exists anywhere in src, and the planning specification bans
model-authored or fabricated confidence, so the honest value is None rather
than an invented band.

Every provenance field is copied verbatim from the matching step receipt.  When
the receipt does not carry it (None / "unknown"), the fact keeps it absent;
nothing is guessed, defaulted, or synthesised -- not a unit, not a time range,
not a quality label, not a freshness explanation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pydantic import JsonValue

from src.nl2sql.contracts import (
    AdHocCalculationStep,
    AnswerArtifact,
    AnswerFact,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    QueryPlan,
    TimeRange,
    TrustedCalculationStep,
)
from src.nl2sql.infra.governance.semantic_sql_validator import SemanticValidationRecord
from src.nl2sql.orchestration.candidates import GATE_ORDER, CandidateReceipt
from src.nl2sql.orchestration.evidence_digest import (
    canonical_json_digest,
    canonical_rowset_digest,
)
from src.nl2sql.orchestration.execution import (
    PlanStepError,
    RequestOutputContract,
    validate_execution_record_against_plan,
)
from src.nl2sql.semantic.execution_manifest import ExpectedExecutionManifest

__all__ = [
    "GroundedAnswer",
    "build_answer_facts",
    "conservative_data_as_of",
    "ground_execution_answer",
    "receipt_degradation_flags",
    "render_grounded_answer",
]

_STALE_FLAG = "GroundedAnswerStale"
_UNKNOWN_FRESHNESS_FLAG = "GroundedAnswerFreshnessUnknown"
# A successful calculation receipt claimed provenance that disagrees with the
# real execution step.  Grounding still refuses the fact; this flag makes the
# refusal operator-visible.  Reuses the established code from p4q_acceptance.
_MISMATCH_FLAG = "grounding_execution_mismatch"


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    """Request-local grounded projection; only answer_text may persist."""

    answer_text: str
    facts: tuple[AnswerFact, ...]
    artifact: AnswerArtifact


class GroundingIntegrityError(RuntimeError):
    """Stable refusal when request-local output evidence disagrees with receipts."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def conservative_data_as_of(
    values: Iterable[datetime | None],
) -> datetime | None:
    """Return a complete aggregate watermark only when every fact has one.

    A missing or naive timestamp makes the aggregate incomplete.  The earliest
    aware timestamp is the safe watermark for a multi-fact answer.
    """

    timestamps = tuple(values)
    if not timestamps:
        return None
    normalized: list[datetime] = []
    for value in timestamps:
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        try:
            if value.utcoffset() is None:
                return None
            normalized.append(value.astimezone(UTC))
        except (OverflowError, TypeError, ValueError):
            return None
    return min(normalized) if normalized else None


def ground_execution_answer(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    record: PlanExecutionRecord,
    outputs: Mapping[str, JsonValue],
    output_contracts: Mapping[str, RequestOutputContract] | None = None,
    require_semantic_contract: bool = False,
    authoritative_units_by_step: Mapping[str, str | None] | None = None,
    allow_legacy_unitless: bool = False,
) -> GroundedAnswer:
    """Build grounded facts, render them, and package the request-local artifact."""

    try:
        if execution_plan.query_plan_sha256 != query_plan.checksum:
            raise GroundingIntegrityError("grounding_query_plan_mismatch")
        validate_execution_record_against_plan(execution_plan, record)
    except PlanStepError as exc:
        raise GroundingIntegrityError(exc.code) from exc

    facts, grounding_flags = _build_answer_facts_with_flags(
        query_plan=query_plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
        output_contracts=output_contracts,
        require_semantic_contract=require_semantic_contract,
        authoritative_units_by_step=authoritative_units_by_step,
        allow_legacy_unitless=allow_legacy_unitless,
    )
    return GroundedAnswer(
        answer_text=render_grounded_answer(query_plan=query_plan, facts=facts),
        facts=facts,
        artifact=AnswerArtifact(
            facts=facts,
            # Deterministic union: receipt evidence first, then the grounding
            # mismatch reasons, deduplicated in order.
            degradation_flags=tuple(
                dict.fromkeys(
                    (*receipt_degradation_flags(record), *grounding_flags)
                )
            ),
        ),
    )


def build_answer_facts(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    record: PlanExecutionRecord,
    outputs: Mapping[str, JsonValue],
    output_contracts: Mapping[str, RequestOutputContract] | None = None,
    require_semantic_contract: bool = False,
    authoritative_units_by_step: Mapping[str, str | None] | None = None,
    allow_legacy_unitless: bool = False,
) -> tuple[AnswerFact, ...]:
    """Project successful fetch-metric step outputs into typed grounded facts."""

    facts, _ = _build_answer_facts_with_flags(
        query_plan=query_plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
        output_contracts=output_contracts,
        require_semantic_contract=require_semantic_contract,
        authoritative_units_by_step=authoritative_units_by_step,
        allow_legacy_unitless=allow_legacy_unitless,
    )
    return facts


def _build_answer_facts_with_flags(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    record: PlanExecutionRecord,
    outputs: Mapping[str, JsonValue],
    output_contracts: Mapping[str, RequestOutputContract] | None = None,
    require_semantic_contract: bool = False,
    authoritative_units_by_step: Mapping[str, str | None] | None = None,
    allow_legacy_unitless: bool = False,
) -> tuple[tuple[AnswerFact, ...], tuple[str, ...]]:
    """One traversal producing both the facts and the grounding mismatch flags.

    The flags are EVIDENCE metadata, not a second traversal: they record that a
    successful receipt claimed provenance that disagrees with the real execution
    step.  Grounding still fails closed (no fact is produced) and the execution
    record is NOT reclassified - only the refusal becomes operator-visible.
    """

    if record.status != "succeeded":
        # A failed execution must never ground business facts, even if it
        # contains a succeeded receipt.
        return (), ()
    if record.execution_plan_checksum != execution_plan.checksum:
        # A record from another execution plan must never ground facts against
        # the supplied plan.
        return (), ()
    _validate_output_evidence(
        record=record,
        outputs=outputs,
        query_plan=query_plan,
        execution_plan=execution_plan,
        output_contracts=output_contracts or {},
        require_semantic_contract=require_semantic_contract,
        authoritative_units_by_step=authoritative_units_by_step,
        allow_legacy_unitless=allow_legacy_unitless,
    )
    steps_by_id = {step.step_id: step for step in execution_plan.steps}
    facts: list[AnswerFact] = []
    flags: list[str] = []
    for receipt in record.step_receipts:
        if receipt.status != "succeeded" or receipt.kind != "fetch_metric":
            continue
        # RE-BIND to the real plan step: a missing or non-fetch step grounds
        # nothing, and never falls back to query_plan.metric_keys.
        step = steps_by_id.get(receipt.step_id)
        if not isinstance(step, FetchMetricStep):
            continue
        # Dead in practice: PlanExecutionRecord.validate_execution_status pins
        # output_step_ids to the succeeded receipt ids, so a succeeded receipt is
        # always a member.  Kept as a defensive cross-check, not a tested path.
        if receipt.step_id not in record.output_step_ids:
            continue
        if (
            step.calculation_input_role is not None
            or step.ad_hoc_input_role is not None
        ):
            # An internal dependency fetch feeds one calculation input role; it
            # is never a grounded answer fact for the requested metric.
            continue
        output = outputs.get(receipt.step_id)
        if isinstance(step, FetchMetricStep):
            output_contract = _manifest_from_request_contract(
                (output_contracts or {}).get(receipt.step_id)
            )
            _validate_typed_fetch_output(
                output,
                query_plan=query_plan,
                step=step,
                output_contract=output_contract,
            )
            authoritative_unit = _fetch_manifest_unit(
                output_contract, step, receipt.unit
            )
            if authoritative_units_by_step is not None:
                if receipt.step_id not in authoritative_units_by_step:
                    raise GroundingIntegrityError(
                        "grounding_unit_authority_missing"
                    )
                authority_unit = authoritative_units_by_step[receipt.step_id]
                if (
                    (output_contract is not None and authoritative_unit != authority_unit)
                    or receipt.unit != authority_unit
                ):
                    raise GroundingIntegrityError(
                        "grounding_unit_authority_mismatch"
                    )
                authoritative_unit = authority_unit
            elif output_contract is None:
                if receipt.unit is not None or not allow_legacy_unitless:
                    raise GroundingIntegrityError(
                        "grounding_unit_authority_missing"
                    )
        for metric_key in step.metric_keys:
            if query_plan.intent == "trend":
                points = _trend_points(output)
                if points:
                    for period, point_value in points:
                        status, value = _project_output(
                            {"rows": [{"value": point_value}], "no_data": False},
                            metric_key=metric_key,
                        )
                        facts.append(
                            _answer_fact(
                                receipt=receipt,
                                metric_key=metric_key,
                                status=status,
                                value=value,
                                time_range=TimeRange(
                                    start=period,
                                    end=period,
                                    timezone="Asia/Shanghai",
                                ),
                                unit=authoritative_unit,
                            )
                        )
                    continue
            status, value = _project_output(output, metric_key=metric_key)
            facts.append(
                _answer_fact(
                    receipt=receipt,
                    metric_key=metric_key,
                    status=status,
                    value=value,
                    unit=authoritative_unit,
                )
            )
    for receipt in record.step_receipts:
        if receipt.status != "succeeded" or receipt.kind != "trusted_calculation":
            continue
        # RE-BIND to the real executed step and require its canonical provenance
        # to agree with the receipt; receipt metadata alone is never authority.
        step = steps_by_id.get(receipt.step_id)
        if not isinstance(step, TrustedCalculationStep):
            # A calculation receipt that cannot be rebound to a real
            # TrustedCalculationStep (absent from the plan, or a different step
            # kind) is the same class of RE-BINDING mismatch as a provenance
            # disagreement - fail closed AND make the refusal visible.
            flags.append(_MISMATCH_FLAG)
            continue
        # Defensive cross-check only (see the fetch loop note above).
        if receipt.step_id not in record.output_step_ids:
            continue
        metric_key = step.output_metric_key
        if not metric_key:
            continue
        if (
            receipt.output_metric_key != step.output_metric_key
            or receipt.template_id != step.template_id
            or receipt.template_version != step.template_version
            or receipt.binding_checksum != step.binding_checksum
        ):
            # A genuine RE-BINDING mismatch (not a normal filter): the receipt
            # claims canonical provenance that disagrees with the real step.
            # Still fail closed, but make the refusal operator-visible.
            flags.append(_MISMATCH_FLAG)
            continue
        status, value = _project_output(outputs.get(receipt.step_id), metric_key=metric_key)
        authoritative_unit = _bound_step_unit(
            step_id=receipt.step_id,
            receipt_unit=receipt.unit,
            authoritative_units_by_step=authoritative_units_by_step,
                allow_legacy_unitless=allow_legacy_unitless,
        )
        facts.append(
            _answer_fact(
                receipt=receipt,
                metric_key=metric_key,
                status=status,
                value=value,
                unit=authoritative_unit,
            )
        )
    for receipt in record.step_receipts:
        if receipt.status != "succeeded" or receipt.kind != "ad_hoc_calculation":
            continue
        # RE-BIND the receipt to the real plan step: an orphan, non-AD_HOC or
        # mismatched receipt must never ground a derived fact.
        step = steps_by_id.get(receipt.step_id)
        if not isinstance(step, AdHocCalculationStep):
            # Same RE-BINDING mismatch class: an AD_HOC receipt whose step is
            # absent or of a different kind.
            flags.append(_MISMATCH_FLAG)
            continue
        # Defensive cross-check only (see the fetch loop note above).
        if receipt.step_id not in record.output_step_ids:
            continue
        derived_output_id = receipt.derived_output_id
        if (
            derived_output_id is None
            or derived_output_id != step.derived_output_id
            or receipt.calculation_spec_checksum != step.calculation_spec.checksum
            or receipt.execution_binding_checksum
            != step.execution_binding.checksum
            or receipt.calculation_scope != "ad_hoc_noncanonical"
        ):
            # Same class of RE-BINDING mismatch as the canonical branch above.
            flags.append(_MISMATCH_FLAG)
            continue
        if receipt.step_id not in outputs:
            # Presence is key-based: a missing output key is not the same as a
            # present JSON null, which remains a real (no_data) result here.
            continue
        status, value = _project_output(
            outputs[receipt.step_id], metric_key=derived_output_id
        )
        authoritative_unit = _calculation_spec_unit(step)
        if receipt.unit != authoritative_unit:
            raise GroundingIntegrityError("grounding_unit_authority_mismatch")
        facts.append(
            _adhoc_answer_fact(
                receipt=receipt,
                derived_output_id=derived_output_id,
                status=status,
                value=value,
                unit=authoritative_unit,
            )
        )
    return tuple(facts), tuple(dict.fromkeys(flags))


def receipt_degradation_flags(record: PlanExecutionRecord) -> tuple[str, ...]:
    """Derive degradation flags deterministically from the step receipts.

    Retires the placeholder GroundedAnswerPending flag: the answer is no longer
    pending, and every flag now names evidence actually observed on a
    successful receipt (source degradation or freshness status).
    """

    flags: list[str] = []
    for receipt in record.step_receipts:
        if receipt.status != "succeeded":
            continue
        flags.extend(receipt.source_degradation)
        if receipt.freshness_status == "stale":
            flags.append(_STALE_FLAG)
        elif receipt.freshness_status == "unknown":
            flags.append(_UNKNOWN_FRESHNESS_FLAG)
    return tuple(dict.fromkeys(flag for flag in flags if flag.strip()))


def _validate_output_evidence(
    *,
    record: PlanExecutionRecord,
    outputs: Mapping[str, JsonValue],
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    output_contracts: Mapping[str, RequestOutputContract],
    require_semantic_contract: bool,
    authoritative_units_by_step: Mapping[str, str | None] | None,
    allow_legacy_unitless: bool,
) -> None:
    steps_by_id = {step.step_id: step for step in execution_plan.steps}
    calculation_shape = any(
        isinstance(step, (TrustedCalculationStep, AdHocCalculationStep))
        for step in execution_plan.steps
    ) or any(
        isinstance(step, FetchMetricStep) and step.calculation_input_role is not None
        for step in execution_plan.steps
    )
    if not calculation_shape:
        direct_fetches = [
            step for step in execution_plan.steps
            if isinstance(step, FetchMetricStep)
        ]
        owners = [key for step in direct_fetches for key in step.metric_keys]
        if (
            any(len(step.metric_keys) != 1 for step in direct_fetches)
            or len(owners) != len(set(owners))
            or set(owners) != set(query_plan.metric_keys)
        ):
            raise GroundingIntegrityError("grounding_metric_ownership_mismatch")
    for receipt in record.step_receipts:
        if receipt.status != "succeeded":
            continue
        if receipt.step_id not in outputs:
            raise GroundingIntegrityError("grounding_output_missing")
        output = outputs[receipt.step_id]
        if receipt.output_digest is None:
            raise GroundingIntegrityError("grounding_output_digest_missing")
        if canonical_json_digest(output) != receipt.output_digest:
            raise GroundingIntegrityError("grounding_output_digest_mismatch")
        if receipt.kind != "fetch_metric":
            if receipt.kind == "trusted_calculation":
                if not isinstance(
                    steps_by_id.get(receipt.step_id), TrustedCalculationStep
                ):
                    continue
                _bound_step_unit(
                    step_id=receipt.step_id,
                    receipt_unit=receipt.unit,
                    authoritative_units_by_step=authoritative_units_by_step,
                        allow_legacy_unitless=allow_legacy_unitless,
                )
            elif receipt.kind == "ad_hoc_calculation":
                    # The fact projection rebinds valid AD_HOC receipts to the
                    # CalculationSpec. Orphan or mismatched provenance is
                    # omitted there before any unit is projected.
                    continue
            continue
        step = steps_by_id.get(receipt.step_id)
        if not isinstance(step, FetchMetricStep):
            # ``ground_execution_answer`` has already applied the strict plan
            # receipt gate.  The lower-level fact projection remains a
            # tolerant projection helper for legacy callers: an orphan receipt
            # contributes no fact and must not be allowed to authorize one.
            continue
        output_contract: ExpectedExecutionManifest | None = None
        candidate = output_contracts.get(receipt.step_id)
        if require_semantic_contract and candidate is None:
            raise GroundingIntegrityError("grounding_manifest_missing")
        if receipt.semantic_manifest_checksum is not None or candidate is not None:
            if not isinstance(candidate, RequestOutputContract):
                raise GroundingIntegrityError("grounding_manifest_missing")
            manifest = candidate.manifest
            semantic_record = candidate.semantic_validation_record
            if not isinstance(manifest, ExpectedExecutionManifest) or not isinstance(
                semantic_record, SemanticValidationRecord
            ):
                raise GroundingIntegrityError("grounding_manifest_missing")
            if (
                semantic_record.outcome != "allow"
                or receipt.semantic_validation_outcome != semantic_record.outcome
                or receipt.semantic_validator_version != semantic_record.validator_version
                or receipt.semantic_validation_identity != semantic_record.identity
                or receipt.semantic_manifest_checksum is None
                or manifest.checksum != receipt.semantic_manifest_checksum
                or semantic_record.manifest_checksum != manifest.checksum
                or receipt.sql_fingerprint != semantic_record.sql_fingerprint
                or receipt.parameter_binding_fingerprint
                != candidate.gateway_parameter_binding_fingerprint
                or semantic_record.parameter_fingerprint
                != manifest.parameter_binding_fingerprint
                or manifest.execution_plan_sha256 != execution_plan.checksum
                or manifest.query_plan_sha256 != query_plan.checksum
                or (
                    require_semantic_contract
                    and manifest.manifest_version != "1.1"
                )
            ):
                raise GroundingIntegrityError("grounding_manifest_mismatch")
            if manifest.manifest_version == "1.1":
                expected_child_checksum = query_plan.model_copy(
                    update={"metric_keys": step.metric_keys}
                ).checksum
                if (
                    manifest.fetch_step_id != step.step_id
                    or manifest.fetch_query_plan_sha256 != expected_child_checksum
                    or len(manifest.metric_authorities) != 1
                    or manifest.metric_authorities[0].fetch_step_id != step.step_id
                    or manifest.metric_authorities[0].metric_key
                    not in {
                        item.removeprefix("metric.") for item in step.metric_keys
                    }
                    or manifest.metric_authorities[0].fetch_query_plan_sha256
                    != expected_child_checksum
                ):
                    raise GroundingIntegrityError("grounding_manifest_fetch_authority_mismatch")
                candidate_receipt = candidate.candidate_receipt
                gateway_receipt = candidate.gateway_execution_receipt
                if require_semantic_contract and (
                    not isinstance(candidate_receipt, CandidateReceipt)
                    or not isinstance(gateway_receipt, ExecutionReceipt)
                ):
                    raise GroundingIntegrityError("grounding_candidate_evidence_missing")
                if candidate_receipt is not None or gateway_receipt is not None:
                    if (
                        not isinstance(candidate_receipt, CandidateReceipt)
                        or not isinstance(gateway_receipt, ExecutionReceipt)
                        or candidate_receipt.failure_code is not None
                        or tuple(item.gate for item in candidate_receipt.gates)
                        != GATE_ORDER
                        or not all(item.passed for item in candidate_receipt.gates)
                        or candidate_receipt.execution_receipt_sha256
                        != canonical_json_digest(
                            gateway_receipt.model_dump(mode="json")
                        )
                        or candidate_receipt.rowset_sha256
                        != gateway_receipt.rowset_sha256
                        or gateway_receipt.policy_outcome != "allow"
                        or gateway_receipt.error_taxonomy is not None
                        or candidate_receipt.metadata.sql_fingerprint
                        != gateway_receipt.sql_fingerprint
                        or candidate_receipt.metadata.semantic_signature
                        != gateway_receipt.semantic_signature
                        or candidate_receipt.metadata.relation_ids
                        != (manifest.metric_authorities[0].relation_id,)
                        or candidate_receipt.metadata.source_kind
                        != gateway_receipt.source_kind
                        or candidate_receipt.metadata.estimated_rows
                        != gateway_receipt.estimated_rows
                        or candidate_receipt.metadata.estimated_cost
                        != gateway_receipt.plan_cost
                        or gateway_receipt.unit
                        != manifest.metric_authorities[0].unit
                        or gateway_receipt.sql_fingerprint != receipt.sql_fingerprint
                        or gateway_receipt.rowset_sha256 != receipt.rowset_sha256
                        or gateway_receipt.parameter_binding_fingerprint
                        != receipt.parameter_binding_fingerprint
                        or gateway_receipt.semantic_manifest_checksum
                        != receipt.semantic_manifest_checksum
                        or gateway_receipt.source_id != receipt.source_id
                        or gateway_receipt.source_kind != receipt.source_kind
                        or gateway_receipt.source_checkpoint
                        != receipt.source_checkpoint
                        or gateway_receipt.selection_reason
                        != receipt.selection_reason
                        or gateway_receipt.source_degradation
                        != receipt.source_degradation
                        or gateway_receipt.unit != receipt.unit
                    ):
                        raise GroundingIntegrityError(
                            "grounding_candidate_evidence_mismatch"
                        )
            output_contract = manifest
            _fetch_manifest_unit(manifest, step, receipt.unit)
        elif receipt.semantic_validation_outcome is not None:
            raise GroundingIntegrityError("grounding_manifest_missing")
        _validate_typed_fetch_output(
            output,
            query_plan=query_plan,
            step=step,
            output_contract=output_contract,
        )
        if output is None:
            raise GroundingIntegrityError("grounding_malformed_output")
        _validate_snapshot_evidence(record)
        if not isinstance(output, Mapping):
            continue
        rows = output.get("rows")
        # Scalar metric runners may project a value directly.  Rowset
        # integrity is required whenever the fetch actually exposes rows;
        # a scalar projection has no rowset digest to validate.
        if rows is None and "rows" not in output:
            continue
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise GroundingIntegrityError("grounding_rowset_digest_mismatch")
        if receipt.rowset_sha256 is None:
            raise GroundingIntegrityError("grounding_rowset_digest_missing")
        try:
            digest = canonical_rowset_digest(
                cast("list[Mapping[str, Any]]", rows),
                result_columns=receipt.result_columns or None,
            )
        except (TypeError, ValueError):
            raise GroundingIntegrityError("grounding_rowset_digest_mismatch") from None
        if digest != receipt.rowset_sha256:
            raise GroundingIntegrityError("grounding_rowset_digest_mismatch")


def _manifest_from_request_contract(
    contract: RequestOutputContract | None,
) -> ExpectedExecutionManifest | None:
    if contract is None:
        return None
    manifest = contract.manifest
    return manifest if isinstance(manifest, ExpectedExecutionManifest) else None


def _fetch_manifest_unit(
    manifest: ExpectedExecutionManifest | None,
    step: FetchMetricStep,
    receipt_unit: str | None,
) -> str | None:
    """Return only the metric unit bound by the server-built manifest.

    Receipts and checkpoint text are claims, not unit authority. Legacy callers
    without a semantic manifest can still ground the value, but the unit is
    omitted because it cannot be rebound to authoritative metadata.
    """

    if manifest is None:
        return None
    if (
        len(manifest.metric_identity) != len(manifest.metric_units)
        or len(step.metric_keys) != 1
    ):
        raise GroundingIntegrityError("grounding_manifest_unit_authority_missing")
    unit_by_metric: dict[str, str | None] = {}
    for metric_identity, unit in zip(
        manifest.metric_identity, manifest.metric_units, strict=True
    ):
        normalized = metric_identity.removeprefix("metric.")
        if not normalized or normalized in unit_by_metric:
            raise GroundingIntegrityError("grounding_manifest_unit_authority_missing")
        unit_by_metric[normalized] = unit
    metric_key = step.metric_keys[0].removeprefix("metric.")
    if metric_key not in unit_by_metric:
        raise GroundingIntegrityError("grounding_manifest_unit_authority_missing")
    expected_unit = unit_by_metric[metric_key]
    if receipt_unit != expected_unit:
        raise GroundingIntegrityError("grounding_manifest_unit_mismatch")
    return expected_unit


def _bound_step_unit(
    *,
    step_id: str,
    receipt_unit: str | None,
    authoritative_units_by_step: Mapping[str, str | None] | None,
    allow_legacy_unitless: bool,
) -> str | None:
    """Use server-side unit authority; a receipt can only be checked against it."""

    if authoritative_units_by_step is None or step_id not in authoritative_units_by_step:
        if receipt_unit is not None:
            raise GroundingIntegrityError("grounding_unit_authority_missing")
        if not allow_legacy_unitless:
            raise GroundingIntegrityError("grounding_unit_authority_missing")
        return None
    expected_unit = authoritative_units_by_step[step_id]
    if receipt_unit != expected_unit:
        raise GroundingIntegrityError("grounding_unit_authority_mismatch")
    return expected_unit


def _calculation_spec_unit(step: AdHocCalculationStep) -> str:
    spec = step.calculation_spec
    if spec.unit == "custom_unit":
        if spec.custom_unit_id is None:
            raise GroundingIntegrityError("grounding_unit_authority_missing")
        return spec.custom_unit_id
    return spec.unit

def _validate_typed_fetch_output(
    output: JsonValue,
    *,
    query_plan: QueryPlan | None = None,
    step: FetchMetricStep | None = None,
    output_contract: ExpectedExecutionManifest | None = None,
) -> None:
    """Reject missing/null/malformed fetch results before projection."""

    if output is None:
        raise GroundingIntegrityError("grounding_malformed_output")
    if not isinstance(output, Mapping):
        # Older injected metric runners return the already-projected scalar.
        # Only numeric scalars are valid for the typed metric result contract;
        # arbitrary text and booleans cannot masquerade as a metric value.
        if type(output) is int or (
            type(output) is float and math.isfinite(output)
        ):
            return
        raise GroundingIntegrityError("grounding_malformed_output")
    if "no_data" in output:
        no_data = output.get("no_data")
        rows = output.get("rows")
        if not isinstance(no_data, bool) or not isinstance(rows, list):
            raise GroundingIntegrityError("grounding_malformed_output")
        if no_data and rows:
            # Current metric compilers represent no data with an empty rowset.
            # A typed placeholder row is not a second no-data representation.
            raise GroundingIntegrityError("grounding_result_cardinality_invalid")
        if output_contract is not None:
            _validate_manifest_typed_rows(
                rows,
                no_data=no_data,
                query_plan=query_plan,
                manifest=output_contract,
            )
        if step is not None and not step.calculation_input_role and not step.ad_hoc_input_role:
            # A metric/detail fetch is single-row by contract.  Empty rowsets
            # are allowed only for an explicit no-data result; a non-empty
            # successful result must never be reduced to a first row at the
            # grounding boundary.
            if query_plan is not None and query_plan.intent in {"metric", "detail"}:
                if not no_data and len(rows) != 1:
                    raise GroundingIntegrityError("grounding_result_cardinality_invalid")
            elif query_plan is not None and query_plan.intent == "ranking":
                if len(rows) > query_plan.ranking_limit:
                    raise GroundingIntegrityError("grounding_result_cardinality_invalid")
            elif query_plan is not None and query_plan.intent == "trend" and len(rows) > 31:
                raise GroundingIntegrityError("grounding_result_cardinality_invalid")
        if no_data:
            return
        if not rows or any(
            not isinstance(row, Mapping)
            or "value" not in row
            or row.get("value") is None
            for row in rows
        ):
            raise GroundingIntegrityError("grounding_malformed_output")
        _validate_grounded_rows(rows)
        return
    if "value" in output:
        if output.get("value") is None:
            raise GroundingIntegrityError("grounding_malformed_output")
        if output_contract is not None:
            raise GroundingIntegrityError("grounding_manifest_result_shape_mismatch")
        _validate_scalar_value(output.get("value"))
        return
    # Legacy typed runners may use the metric's short name as the sole key.
    # Keep that bounded projection compatible while rejecting arbitrary
    # multi-field or null mappings.
    if len(output) == 1:
        only = next(iter(output.values()))
        if only is not None and _is_scalar(only):
            if output_contract is not None:
                raise GroundingIntegrityError("grounding_manifest_result_shape_mismatch")
            _validate_scalar_value(only)
            return
    raise GroundingIntegrityError("grounding_malformed_output")


def _validate_grounded_rows(rows: Sequence[object]) -> None:
    """Validate the JSON row shape before any value can become a fact.

    Metric execution serializes Decimal values as exact decimal strings.  The
    grounding boundary therefore accepts finite JSON numbers and canonical
    decimal strings, while rejecting arbitrary text, nested objects and
    arrays that could otherwise be rendered as a grounded value.
    """

    for row in rows:
        if not isinstance(row, Mapping):
            raise GroundingIntegrityError("grounding_result_type_invalid")
        status = row.get("status")
        if status is not None and status not in {"success", "no_data"}:
            raise GroundingIntegrityError("grounding_result_type_invalid")
        if "value" in row:
            value = row.get("value")
            if value is None:
                if status != "no_data":
                    raise GroundingIntegrityError("grounding_result_type_invalid")
            else:
                _validate_scalar_value(value)
        for key in ("numerator", "denominator"):
            if key in row:
                value = row.get(key)
                if type(value) is not int or value < 0:
                    raise GroundingIntegrityError("grounding_result_type_invalid")
        if "numerator" in row and "denominator" in row:
            if row["numerator"] > row["denominator"]:
                raise GroundingIntegrityError("grounding_result_type_invalid")
        if "period" in row:
            period = row.get("period")
            if not _is_compiler_date_value(period):
                raise GroundingIntegrityError("grounding_result_type_invalid") from None
        for key, value in row.items():
            if key in {"value", "numerator", "denominator", "status", "period"}:
                continue
            if isinstance(value, (Mapping, list)) or value is None:
                raise GroundingIntegrityError("grounding_result_type_invalid")


def _validate_scalar_value(value: object) -> None:
    if type(value) is bool:
        raise GroundingIntegrityError("grounding_result_type_invalid")
    if type(value) is int:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise GroundingIntegrityError("grounding_result_type_invalid")
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            raise GroundingIntegrityError("grounding_result_type_invalid") from None
        if parsed.is_finite() and value.strip():
            return
    raise GroundingIntegrityError("grounding_result_type_invalid")


def _validate_manifest_typed_rows(
    rows: list[JsonValue],
    *,
    no_data: bool,
    query_plan: QueryPlan | None,
    manifest: ExpectedExecutionManifest,
) -> None:
    if no_data and rows:
        raise GroundingIntegrityError("grounding_result_cardinality_invalid")
    if not no_data and not rows:
        raise GroundingIntegrityError("grounding_result_cardinality_invalid")
    if len(manifest.output_aliases) != len(manifest.output_logical_types):
        raise GroundingIntegrityError("grounding_manifest_output_types_invalid")
    if not manifest.output_aliases:
        raise GroundingIntegrityError("grounding_manifest_output_types_missing")
    if manifest.cardinality_expectation == "single_row":
        if no_data:
            if rows:
                raise GroundingIntegrityError("grounding_result_cardinality_invalid")
        elif len(rows) != 1:
            raise GroundingIntegrityError("grounding_result_cardinality_invalid")
    elif query_plan is not None and query_plan.intent == "ranking":
        if len(rows) > query_plan.ranking_limit:
            raise GroundingIntegrityError("grounding_result_cardinality_invalid")
    elif query_plan is not None and query_plan.intent == "trend" and len(rows) > 31:
        raise GroundingIntegrityError("grounding_result_cardinality_invalid")
    if no_data:
        return
    aliases = tuple(manifest.output_aliases)
    logical_types = tuple(manifest.output_logical_types)
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(aliases):
            raise GroundingIntegrityError("grounding_manifest_result_shape_mismatch")
        for alias, logical_type in zip(aliases, logical_types, strict=True):
            value = row[alias]
            if not _matches_logical_type(value, logical_type):
                raise GroundingIntegrityError("grounding_result_type_invalid")


def _matches_logical_type(value: JsonValue, logical_type: str) -> bool:
    if logical_type == "integer":
        return type(value) is int
    if logical_type == "numeric":
        if not isinstance(value, str):
            return False
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            return False
        return (
            decimal_value.is_finite()
            and decimal_value.as_tuple().exponent == -2
        )
    if logical_type == "text":
        return isinstance(value, str)
    if logical_type == "date":
        return _is_compiler_date_value(value)
    return False


def _is_compiler_date_value(value: object) -> bool:
    """Accept only the exact naive midnight ISO form emitted by the runner."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is None
        and parsed.time().isoformat() == "00:00:00"
        and parsed.isoformat() == value
    )


def _validate_snapshot_evidence(record: PlanExecutionRecord) -> None:
    fetches = [
        receipt
        for receipt in record.step_receipts
        if receipt.status == "succeeded" and receipt.kind == "fetch_metric"
    ]
    if not fetches:
        return
    has_any = any(
        receipt.source_snapshot_id is not None
        or receipt.source_snapshot_fingerprint is not None
        for receipt in fetches
    )
    # Production fetch receipts carry a typed source identity.  Once that
    # identity is present, a snapshot is part of the same authority tuple and
    # cannot silently disappear at the grounding boundary.  Legacy scalar
    # fixtures with no source identity remain snapshot-neutral.
    requires_snapshot = any(
        receipt.source_version is not None
        for receipt in fetches
    )
    if not has_any and not requires_snapshot:
        return
    if not has_any:
        raise GroundingIntegrityError("grounding_snapshot_evidence_missing")
    if any(
        receipt.source_snapshot_id is None
        or receipt.source_snapshot_fingerprint is None
        for receipt in fetches
    ):
        raise GroundingIntegrityError("grounding_snapshot_evidence_missing")
    pairs = {
        (receipt.source_snapshot_id, receipt.source_snapshot_fingerprint)
        for receipt in fetches
    }
    if len(pairs) != 1:
        raise GroundingIntegrityError("grounding_mixed_source_snapshots")


def render_grounded_answer(
    *,
    query_plan: QueryPlan,
    facts: tuple[AnswerFact, ...],
) -> str:
    """Render facts to byte-stable text; never dump raw rows, SQL or hashes."""

    if not facts:
        metric_keys = ", ".join(query_plan.metric_keys)
        return f"No grounded value was produced for {metric_keys}."
    lines: list[str] = []
    for fact in facts:
        label = _fact_label(fact)
        if fact.status == "unavailable":
            lines.append(f"{label}: no data returned")
        elif _is_scalar(fact.value):
            lines.append(f"{label}: {_stable_json(fact.value)}")
        else:
            reference = fact.output_digest or fact.fact_id
            lines.append(f"{label}: grounded value recorded (digest {reference})")
    return "\n".join(lines)


def _fact_label(fact: AnswerFact) -> str:
    """A canonical metric name, or an explicit derived/noncanonical label."""

    if fact.metric_key is not None:
        label = fact.metric_key
        if fact.time_range is not None and fact.time_range.start == fact.time_range.end:
            label += f" [{fact.time_range.start.isoformat()}]"
        return label
    if fact.derived_output_id is not None:
        return f"Derived result ({fact.derived_output_id})"
    return fact.step_id


def _answer_fact(
    *,
    receipt: PlanStepReceipt,
    metric_key: str,
    status: str,
    value: JsonValue,
    time_range: TimeRange | None = None,
    unit: str | None = None,
) -> AnswerFact:
    return AnswerFact(
        fact_id=_fact_id(
            step_id=receipt.step_id,
            metric_key=metric_key,
            status=status,
            value=value,
            time_range=time_range,
            unit=unit,
        ),
        fact_id_version=2,
        step_id=receipt.step_id,
        metric_key=metric_key,
        status=cast("Any", status),
        value=value,
        unit=unit,
        rowset_sha256=receipt.rowset_sha256,
        output_digest=receipt.output_digest,
        source_id=receipt.source_id,
        semantic_signature=receipt.semantic_signature,
        time_range=time_range,
        data_as_of=receipt.data_as_of,
        freshness_status=receipt.freshness_status,
        source_kind=receipt.source_kind,
        selection_reason=receipt.selection_reason,
        source_degradation=receipt.source_degradation,
    )


def _adhoc_answer_fact(
    *,
    receipt: PlanStepReceipt,
    derived_output_id: str,
    status: str,
    value: JsonValue,
    unit: str | None = None,
) -> AnswerFact:
    return AnswerFact(
        fact_id=_derived_fact_id(
            step_id=receipt.step_id,
            derived_output_id=derived_output_id,
            status=status,
            value=value,
            unit=unit,
        ),
        fact_id_version=2,
        step_id=receipt.step_id,
        derived_output_id=derived_output_id,
        calculation_scope="ad_hoc_noncanonical",
        status=cast("Any", status),
        value=value,
        unit=unit,
        rowset_sha256=receipt.rowset_sha256,
        output_digest=receipt.output_digest,
        source_id=receipt.source_id,
        semantic_signature=receipt.semantic_signature,
        data_as_of=receipt.data_as_of,
        freshness_status=receipt.freshness_status,
        source_kind=receipt.source_kind,
        selection_reason=receipt.selection_reason,
        source_degradation=receipt.source_degradation,
    )


def _derived_fact_id(
    *,
    step_id: str,
    derived_output_id: str,
    status: str,
    value: JsonValue,
    unit: str | None,
) -> str:
    """Deterministic NONCANONICAL fact identity (separate from _fact_id)."""

    payload = json.dumps(
        {
            "step_id": step_id,
            "derived_output_id": derived_output_id,
            "status": status,
            "value": value,
            "unit": unit,
            "fact_id_version": 2,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _project_output(output: JsonValue, *, metric_key: str) -> tuple[str, JsonValue]:
    """Return (status, value) projected from one request-local step output.

    Known aggregate shape is {"rows": [...], "no_data": bool}; a bare mapping of
    metric identifier to scalar is also accepted.  No projection is fabricated:
    when the output yields no value, the outcome is a structured unavailable
    fact carrying None.
    """

    if output is None:
        return ("unavailable", None)
    if isinstance(output, Mapping):
        mapping = cast("Mapping[str, JsonValue]", output)
        no_data = mapping.get("no_data")
        if isinstance(no_data, bool):
            if no_data:
                return ("unavailable", None)
            rows = mapping.get("rows")
            if isinstance(rows, list):
                values = [
                    row["value"]
                    for row in rows
                    if isinstance(row, Mapping) and "value" in row
                ]
                if not values:
                    return ("unavailable", None)
                return ("grounded", values[0] if len(values) == 1 else values)
        for candidate in (metric_key, metric_key.rsplit(".", 1)[-1]):
            if candidate in mapping:
                return ("grounded", mapping[candidate])
        if len(mapping) == 1:
            only = next(iter(mapping.values()))
            if _is_scalar(only):
                return ("grounded", only)
        return ("grounded", dict(mapping))
    return ("grounded", output)


def _trend_points(output: JsonValue) -> tuple[tuple[date, JsonValue], ...]:
    """Project only bounded period/value pairs from a governed trend result."""

    if not isinstance(output, Mapping):
        return ()
    rows = output.get("rows")
    if not isinstance(rows, list) or len(rows) > 31:
        return ()
    points: list[tuple[date, JsonValue]] = []
    for row in rows:
        if not isinstance(row, Mapping) or "period" not in row or "value" not in row:
            return ()
        raw_period = row["period"]
        if isinstance(raw_period, datetime):
            period = raw_period.date()
        elif isinstance(raw_period, date):
            period = raw_period
        elif isinstance(raw_period, str):
            try:
                period = date.fromisoformat(raw_period[:10])
            except ValueError:
                return ()
        else:
            return ()
        points.append((period, cast(JsonValue, row["value"])))
    return tuple(points)


def _fact_id(
    *,
    step_id: str,
    metric_key: str,
    status: str,
    value: JsonValue,
    time_range: TimeRange | None = None,
    unit: str | None = None,
) -> str:
    """Deterministic fact identity: a digest over grounded evidence only."""

    payload: dict[str, object] = {
        "step_id": step_id,
        "metric_key": metric_key,
        "status": status,
        "value": value,
        "unit": unit,
        "fact_id_version": 2,
    }
    if time_range is not None:
        payload["time_range"] = time_range.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _is_scalar(value: JsonValue) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))
