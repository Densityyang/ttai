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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from pydantic import JsonValue

from src.nl2sql.contracts import (
    AdHocCalculationStep,
    AnswerArtifact,
    AnswerFact,
    ExecutionPlan,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    QueryPlan,
    TimeRange,
    TrustedCalculationStep,
)

__all__ = [
    "GroundedAnswer",
    "build_answer_facts",
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


def ground_execution_answer(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    record: PlanExecutionRecord,
    outputs: Mapping[str, JsonValue],
) -> GroundedAnswer:
    """Build grounded facts, render them, and package the request-local artifact."""

    facts, grounding_flags = _build_answer_facts_with_flags(
        query_plan=query_plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
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
) -> tuple[AnswerFact, ...]:
    """Project successful fetch-metric step outputs into typed grounded facts."""

    facts, _ = _build_answer_facts_with_flags(
        query_plan=query_plan,
        execution_plan=execution_plan,
        record=record,
        outputs=outputs,
    )
    return facts


def _build_answer_facts_with_flags(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    record: PlanExecutionRecord,
    outputs: Mapping[str, JsonValue],
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
        facts.append(
            _answer_fact(
                receipt=receipt,
                metric_key=metric_key,
                status=status,
                value=value,
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
        facts.append(
            _adhoc_answer_fact(
                receipt=receipt,
                derived_output_id=derived_output_id,
                status=status,
                value=value,
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
) -> AnswerFact:
    return AnswerFact(
        fact_id=_fact_id(
            step_id=receipt.step_id,
            metric_key=metric_key,
            status=status,
            value=value,
            time_range=time_range,
        ),
        step_id=receipt.step_id,
        metric_key=metric_key,
        status=cast("Any", status),
        value=value,
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
) -> AnswerFact:
    return AnswerFact(
        fact_id=_derived_fact_id(
            step_id=receipt.step_id,
            derived_output_id=derived_output_id,
            status=status,
            value=value,
        ),
        step_id=receipt.step_id,
        derived_output_id=derived_output_id,
        calculation_scope="ad_hoc_noncanonical",
        status=cast("Any", status),
        value=value,
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
    *, step_id: str, derived_output_id: str, status: str, value: JsonValue
) -> str:
    """Deterministic NONCANONICAL fact identity (separate from _fact_id)."""

    payload = json.dumps(
        {
            "step_id": step_id,
            "derived_output_id": derived_output_id,
            "status": status,
            "value": value,
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
) -> str:
    """Deterministic fact identity: a digest over grounded evidence only."""

    payload: dict[str, object] = {
        "step_id": step_id,
        "metric_key": metric_key,
        "status": status,
        "value": value,
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
