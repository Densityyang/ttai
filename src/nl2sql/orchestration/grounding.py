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
from typing import Any, cast

from pydantic import JsonValue

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    ExecutionPlan,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    QueryPlan,
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

    facts = build_answer_facts(
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
            degradation_flags=receipt_degradation_flags(record),
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

    steps_by_id = {step.step_id: step for step in execution_plan.steps}
    facts: list[AnswerFact] = []
    for receipt in record.step_receipts:
        if receipt.status != "succeeded" or receipt.kind != "fetch_metric":
            continue
        step = steps_by_id.get(receipt.step_id)
        if isinstance(step, FetchMetricStep) and step.calculation_input_role is not None:
            # An internal dependency fetch feeds one trusted calculation input
            # role; it is never a grounded answer fact for the requested metric.
            continue
        metric_keys = (
            step.metric_keys
            if isinstance(step, FetchMetricStep)
            else query_plan.metric_keys
        )
        output = outputs.get(receipt.step_id)
        for metric_key in metric_keys:
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
        metric_key = receipt.output_metric_key
        if not metric_key:
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
    return tuple(facts)


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
        if fact.status == "unavailable":
            lines.append(f"{fact.metric_key}: no data returned")
        elif _is_scalar(fact.value):
            lines.append(f"{fact.metric_key}: {_stable_json(fact.value)}")
        else:
            reference = fact.output_digest or fact.fact_id
            lines.append(f"{fact.metric_key}: grounded value recorded (digest {reference})")
    return "\n".join(lines)


def _answer_fact(
    *,
    receipt: PlanStepReceipt,
    metric_key: str,
    status: str,
    value: JsonValue,
) -> AnswerFact:
    return AnswerFact(
        fact_id=_fact_id(
            step_id=receipt.step_id,
            metric_key=metric_key,
            status=status,
            value=value,
        ),
        step_id=receipt.step_id,
        metric_key=metric_key,
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


def _fact_id(*, step_id: str, metric_key: str, status: str, value: JsonValue) -> str:
    """Deterministic fact identity: a digest over grounded evidence only."""

    payload = json.dumps(
        {
            "step_id": step_id,
            "metric_key": metric_key,
            "status": status,
            "value": value,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
