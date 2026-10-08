from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from pydantic import JsonValue

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    PlanExecutionRecord,
    PlanStepReceipt,
    TimeRange,
)
from src.nl2sql.orchestration.analysis_evidence import (
    MAX_ANALYSIS_FACTS,
    AnalysisEvidenceError,
    AnalysisInterpretation,
    AnalysisStatement,
    build_analysis_evidence,
    project_analysis_model_input,
    validate_analysis_interpretation,
)
from src.nl2sql.orchestration.grounding import GroundedAnswer


def _fact_id(
    step_id: str,
    metric_key: str,
    status: str,
    value: object,
    time_range: TimeRange | None = None,
) -> str:
    payload_data: dict[str, object] = {
        "step_id": step_id,
        "metric_key": metric_key,
        "status": status,
        "value": value,
    }
    if time_range is not None:
        payload_data["time_range"] = time_range.model_dump(mode="json")
    payload = json.dumps(
        payload_data,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence(
    *,
    value: JsonValue = 42,
    data_as_of: datetime | None = datetime(2026, 9, 20, tzinfo=UTC),
    fact_source_id: str | None = "gold.repair.archive",
    receipt_source_id: str | None = "gold.repair.archive",
    time_range: TimeRange | None = None,
) -> tuple[GroundedAnswer, PlanExecutionRecord, TimeRange]:
    step_id = "fetch_archive_rate"
    metric_key = "repair_service_archive_rate_overall_day"
    output_digest = "a" * 64
    receipt = PlanStepReceipt(
        step_id=step_id,
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=3,
        output_digest=output_digest,
        rowset_sha256="b" * 64,
        data_as_of=data_as_of,
        freshness_status="fresh" if data_as_of else "unknown",
        source_kind="approved_aggregate",
        source_id=receipt_source_id,
        selection_reason="fresh_approved_aggregate",
        source_checkpoint="gold-checkpoint-20260920",
        semantic_signature="c" * 64,
    )
    status = "unavailable" if value is None else "grounded"
    fact = AnswerFact(
        fact_id=_fact_id(step_id, metric_key, status, value, time_range),
        step_id=step_id,
        metric_key=metric_key,
        status=status,
        value=value,
        unit="percent",
        time_range=time_range,
        rowset_sha256=receipt.rowset_sha256,
        output_digest=receipt.output_digest,
        source_id=fact_source_id,
        semantic_signature=receipt.semantic_signature,
        data_as_of=data_as_of,
        freshness_status=receipt.freshness_status,
        source_kind=receipt.source_kind,
        selection_reason=receipt.selection_reason,
    )
    grounded = GroundedAnswer(
        answer_text="controlled answer",
        facts=(fact,),
        artifact=AnswerArtifact(facts=(fact,)),
    )
    record = PlanExecutionRecord(
        execution_plan_checksum="d" * 64,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=(step_id,),
    )
    window = TimeRange(start=date(2026, 9, 1), end=date(2026, 9, 20))
    return grounded, record, window


def test_analysis_evidence_rebinds_grounded_fact_to_receipt_provenance() -> None:
    grounded, record, window = _evidence()

    bundle = build_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )

    assert bundle.metric_keys == ("repair_service_archive_rate_overall_day",)
    assert bundle.facts[0].value == 42
    assert bundle.facts[0].source_checkpoint == "gold-checkpoint-20260920"
    assert bundle.authority_provenance.execution_plan_checksum == "d" * 64
    assert bundle.data_as_of == datetime(2026, 9, 20, tzinfo=UTC)
    assert len(bundle.checksum) == 64
    dumped = bundle.model_dump(mode="json")
    assert "sql" not in json.dumps(dumped).lower()
    assert "authorization" not in json.dumps(dumped).lower()


def test_analysis_evidence_omits_unproven_fact_and_fails_closed_if_none_remain() -> None:
    grounded, record, window = _evidence(fact_source_id="gold.other")

    with pytest.raises(AnalysisEvidenceError, match="analysis_evidence_no_proven_facts"):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_analysis_evidence_requires_the_exact_execution_plan_checksum() -> None:
    grounded, record, window = _evidence()

    with pytest.raises(AnalysisEvidenceError, match="analysis_execution_plan_mismatch"):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum="0" * 64,
            analysis_window=window,
        )


def test_analysis_evidence_does_not_consume_composite_row_values() -> None:
    grounded, record, window = _evidence(value=[{"day": "2026-09-20", "value": 42}])

    with pytest.raises(AnalysisEvidenceError, match="analysis_evidence_no_proven_facts"):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_analysis_evidence_is_bounded_before_projection() -> None:
    grounded, record, window = _evidence()
    oversized = tuple(grounded.facts[0] for _ in range(MAX_ANALYSIS_FACTS + 1))
    grounded = GroundedAnswer(
        answer_text="oversized",
        facts=oversized,
        artifact=AnswerArtifact(facts=oversized),
    )

    with pytest.raises(AnalysisEvidenceError, match="analysis_evidence_fact_limit_exceeded"):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_missing_dates_are_not_invented() -> None:
    grounded, record, window = _evidence(data_as_of=None, time_range=None)

    bundle = build_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )

    assert bundle.facts[0].time_range is None
    assert bundle.facts[0].observed_at is None
    assert bundle.facts[0].data_as_of is None
    assert bundle.data_as_of is None
    assert "analysis_data_as_of_incomplete" in bundle.degradation_flags
    assert "analysis_source_freshness_unknown" in bundle.degradation_flags


def test_model_projection_separates_source_facts_and_requested_interpretation() -> None:
    grounded, record, window = _evidence()
    bundle = build_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )

    projection = project_analysis_model_input(
        bundle, analysis_goal="Describe the controlled trend without filling gaps."
    )

    assert projection.evidence_checksum == bundle.checksum
    assert projection.messages[0].role == "system"
    assert "Never invent values" in projection.messages[0].content
    assert "SOURCE FACTS\n" in projection.messages[1].content
    assert "REQUESTED INTERPRETATION\n" in projection.messages[1].content
    assert "sql" not in bundle.model_dump_json().lower()
    assert projection.response_schema["additionalProperties"] is False


def test_structured_interpretation_requires_numeric_traceability() -> None:
    fact_range = TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))
    grounded, record, window = _evidence(time_range=fact_range)
    bundle = build_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    fact_id = bundle.facts[0].fact_id
    valid = AnalysisInterpretation(
        summary=AnalysisStatement(text="The controlled value is 42.", fact_ids=(fact_id,)),
        observations=(
            AnalysisStatement(
                text="The observation is dated 2026-09-20.", fact_ids=(fact_id,)
            ),
        ),
        caveats=(AnalysisStatement(text="No uncited numeric claim is made."),),
    )

    assert validate_analysis_interpretation(valid, evidence=bundle) is valid

    invalid = valid.model_copy(
        update={
            "summary": AnalysisStatement(
                text="The controlled value is 43.", fact_ids=(fact_id,)
            )
        }
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_fact_unproven"
    ):
        validate_analysis_interpretation(invalid, evidence=bundle)
