from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any
from uuid import UUID

import pytest
from pydantic import JsonValue

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    ExecutionPlan,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    QueryPlan,
    TimeRange,
    VerifyStep,
)
from src.nl2sql.orchestration.analysis_evidence import (
    MAX_ANALYSIS_FACTS,
    AnalysisEvidenceBundle,
    AnalysisEvidenceError,
    AnalysisInterpretation,
    AnalysisStatement,
    build_analysis_evidence,
    project_analysis_model_input,
    validate_analysis_interpretation,
    validate_grounded_answer_checkpoint,
)
from src.nl2sql.orchestration.checkpoint_evidence_seal import (
    seal_grounded_execution_checkpoint,
)
from src.nl2sql.orchestration.grounding import (
    GroundedAnswer,
    conservative_data_as_of,
    render_grounded_answer,
)


def _fact_id(
    step_id: str,
    metric_key: str,
    status: str,
    value: object,
    time_range: TimeRange | None = None,
    unit: str | None = None,
) -> str:
    payload_data: dict[str, object] = {
        "step_id": step_id,
        "metric_key": metric_key,
        "status": status,
        "value": value,
        "unit": unit,
        "fact_id_version": 2,
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
    unit: str | None = "percent",
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
        unit=unit,
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
        fact_id=_fact_id(
            step_id, metric_key, status, value, time_range, unit=unit
        ),
        step_id=step_id,
        metric_key=metric_key,
        status=status,
        value=value,
        unit=unit,
        fact_id_version=2,
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


def _build_authorized_analysis_evidence(
    *,
    grounded: GroundedAnswer,
    record: PlanExecutionRecord,
    execution_plan_checksum: str,
    analysis_window: TimeRange,
    **kwargs: Any,
) -> AnalysisEvidenceBundle:
    """Supply explicit fixture authority for tests not exercising its absence."""

    if "authoritative_units" not in kwargs:
        kwargs["authoritative_units"] = {
            fact.metric_key: fact.unit
            for fact in grounded.facts
            if fact.metric_key is not None
        }
    return build_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=execution_plan_checksum,
        analysis_window=analysis_window,
        **kwargs,
    )


def _checkpoint_binding(
    grounded: GroundedAnswer,
    record: PlanExecutionRecord,
) -> tuple[QueryPlan, ExecutionPlan, PlanExecutionRecord]:
    metric_key = grounded.facts[0].metric_key
    assert metric_key is not None
    window = TimeRange(start=date(2026, 9, 1), end=date(2026, 9, 20))
    query_plan = QueryPlan(
        intent="metric",
        domain="controlled",
        metric_keys=(metric_key,),
        time_range=window,
        grain="day",
        source_strategy="aggregate_first",
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=UUID("11111111-1111-1111-1111-111111111111"),
        schema_snapshot_id=UUID("22222222-2222-2222-2222-222222222222"),
        policy_version="analysis-evidence.test.v1",
        steps=(
            FetchMetricStep(step_id="fetch_archive_rate", metric_keys=(metric_key,)),
            VerifyStep(
                step_id="verify_result",
                input_refs=("fetch_archive_rate",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch_archive_rate",),
            ),
        ),
    )
    verify_receipt = PlanStepReceipt(
        step_id="verify_result",
        kind="verify",
        status="succeeded",
        elapsed_ms=0,
        output_digest="e" * 64,
    )
    bound_record = record.model_copy(
        update={
            "execution_plan_checksum": execution_plan.checksum,
            "step_receipts": (*record.step_receipts, verify_receipt),
            "output_step_ids": (*record.output_step_ids, "verify_result"),
        }
    )
    return query_plan, execution_plan, bound_record


def test_analysis_evidence_rebinds_grounded_fact_to_receipt_provenance() -> None:
    grounded, record, window = _evidence()

    bundle = _build_authorized_analysis_evidence(
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
        _build_authorized_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_analysis_evidence_requires_the_exact_execution_plan_checksum() -> None:
    grounded, record, window = _evidence()

    with pytest.raises(AnalysisEvidenceError, match="analysis_execution_plan_mismatch"):
        _build_authorized_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum="0" * 64,
            analysis_window=window,
        )


def test_analysis_evidence_does_not_consume_composite_row_values() -> None:
    grounded, record, window = _evidence(value=[{"day": "2026-09-20", "value": 42}])

    with pytest.raises(AnalysisEvidenceError, match="analysis_evidence_no_proven_facts"):
        _build_authorized_analysis_evidence(
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
        _build_authorized_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_missing_dates_are_not_invented() -> None:
    grounded, record, window = _evidence(data_as_of=None, time_range=None)

    bundle = _build_authorized_analysis_evidence(
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
    bundle = _build_authorized_analysis_evidence(
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
    bundle = _build_authorized_analysis_evidence(
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


def test_date_year_cannot_authorize_a_metric_percentage() -> None:
    grounded, record, window = _evidence(
        value=42,
        time_range=TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20)),
    )
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )

    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_fact_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(
                    text="The rate is 2026%.", fact_ids=(bundle.facts[0].fact_id,)
                )
            ),
            evidence=bundle,
        )


def test_numeric_value_requires_the_cited_unit() -> None:
    grounded, record, window = _evidence(value=42)
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )

    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_unit_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(
                    text="The value is 42 count.", fact_ids=(bundle.facts[0].fact_id,)
                )
            ),
            evidence=bundle,
        )

    grounded, record, window = _evidence(value=42, unit=None)
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_unit_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(
                    text="The value is 42%.", fact_ids=(bundle.facts[0].fact_id,)
                )
            ),
            evidence=bundle,
        )


def test_decimal_representation_is_not_reformatted() -> None:
    grounded, record, window = _evidence(value="93.20")
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    fact_id = bundle.facts[0].fact_id

    assert validate_analysis_interpretation(
        AnalysisInterpretation(
            summary=AnalysisStatement(text="The rate is 93.20%.", fact_ids=(fact_id,))
        ),
        evidence=bundle,
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_fact_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(text="The rate is 93.2%.", fact_ids=(fact_id,))
            ),
            evidence=bundle,
        )


def test_checkpoint_unit_is_ignored_without_authoritative_metadata() -> None:
    grounded, record, window = _evidence(value=42)
    tampered_fact = grounded.facts[0].model_copy(update={"unit": "count"})
    grounded = GroundedAnswer(
        answer_text=grounded.answer_text,
        facts=(tampered_fact,),
        artifact=AnswerArtifact(facts=(tampered_fact,)),
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_fact_unit_authority_missing"
    ):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_checkpoint_unit_mutation_cannot_override_authoritative_metric_unit() -> None:
    grounded, record, window = _evidence(value=42, unit="percent")
    tampered_fact = grounded.facts[0].model_copy(
        update={
            "unit": "count",
            "fact_id": _fact_id(
                grounded.facts[0].step_id,
                grounded.facts[0].metric_key or "",
                grounded.facts[0].status,
                grounded.facts[0].value,
                grounded.facts[0].time_range,
                unit="count",
            ),
        }
    )
    tampered = GroundedAnswer(
        answer_text=grounded.answer_text,
        facts=(tampered_fact,),
        artifact=AnswerArtifact(facts=(tampered_fact,)),
    )
    with pytest.raises(AnalysisEvidenceError, match="analysis_evidence_no_proven_facts"):
        _build_authorized_analysis_evidence(
            grounded=tampered,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
            authoritative_units={
                grounded.facts[0].metric_key or "": "percent",
            },
        )


def test_checkpoint_cannot_promote_missing_metric_unit_authority() -> None:
    grounded, record, window = _evidence(value=42, unit=None)
    original_fact = grounded.facts[0]
    promoted_fact = original_fact.model_copy(
        update={
            "unit": "count",
            "fact_id": _fact_id(
                original_fact.step_id,
                original_fact.metric_key or "",
                original_fact.status,
                original_fact.value,
                original_fact.time_range,
                unit="count",
            ),
        }
    )
    promoted_receipt = record.step_receipts[0].model_copy(update={"unit": "count"})
    promoted_record = record.model_copy(update={"step_receipts": (promoted_receipt,)})
    promoted_grounded = GroundedAnswer(
        answer_text=grounded.answer_text,
        facts=(promoted_fact,),
        artifact=AnswerArtifact(facts=(promoted_fact,)),
    )

    with pytest.raises(AnalysisEvidenceError, match="analysis_evidence_no_proven_facts"):
        _build_authorized_analysis_evidence(
            grounded=promoted_grounded,
            record=promoted_record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
            authoritative_units={original_fact.metric_key or "": None},
        )


def test_free_text_receipt_unit_cannot_become_authority() -> None:
    grounded, record, window = _evidence(value=42, unit="orders")
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_fact_unit_authority_missing"
    ):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_build_analysis_evidence_cannot_project_unit_without_authority() -> None:
    grounded, record, window = _evidence(unit="percent")

    with pytest.raises(
        AnalysisEvidenceError, match="analysis_fact_unit_authority_missing"
    ):
        build_analysis_evidence(
            grounded=grounded,
            record=record,
            execution_plan_checksum=record.execution_plan_checksum,
            analysis_window=window,
        )


def test_checkpoint_requires_unit_authority_for_non_null_metric_receipt_unit() -> None:
    grounded, record, _window = _evidence(unit="percent")
    query_plan, execution_plan, bound_record = _checkpoint_binding(grounded, record)
    answer_text = render_grounded_answer(
        query_plan=query_plan,
        facts=grounded.facts,
    )

    key = "analysis-evidence-test-checkpoint-seal-key"
    identities = {grounded.facts[0].step_id: "a" * 64}
    sealed_record = seal_grounded_execution_checkpoint(
        key=key,
        query_plan=query_plan,
        execution_plan=execution_plan,
        execution_record=bound_record,
        answer_artifact=grounded.artifact,
        answer_text=answer_text,
        unit_authority_identities=identities,
    )
    with pytest.raises(AnalysisEvidenceError, match="analysis_fact_unit_authority_missing"):
        validate_grounded_answer_checkpoint(
            query_plan,
            grounded.artifact,
            answer_text,
            execution_plan=execution_plan,
            execution_record=sealed_record,
            evidence_seal_key=key,
            unit_authority_identities=identities,
        )


def test_checkpoint_without_unit_can_remain_unitless_without_authority() -> None:
    grounded, record, window = _evidence(unit=None)
    query_plan, execution_plan, bound_record = _checkpoint_binding(grounded, record)
    answer_text = render_grounded_answer(
        query_plan=query_plan,
        facts=grounded.facts,
    )

    validated = validate_grounded_answer_checkpoint(
        query_plan,
        grounded.artifact,
        answer_text,
        execution_plan=execution_plan,
        execution_record=bound_record,
        allow_legacy_checkpoint=True,
    )
    bundle = build_analysis_evidence(
        grounded=validated,
        record=bound_record,
        execution_plan_checksum=execution_plan.checksum,
        analysis_window=window,
        query_plan=query_plan,
        execution_plan=execution_plan,
    )
    assert bundle.facts[0].unit is None


def test_legacy_checkpoint_gate_rejects_unitful_unsealed_records() -> None:
    grounded, record, _window = _evidence(unit="percent")
    query_plan, execution_plan, bound_record = _checkpoint_binding(grounded, record)
    answer_text = render_grounded_answer(
        query_plan=query_plan,
        facts=grounded.facts,
    )
    with pytest.raises(
        AnalysisEvidenceError,
        match="analysis_checkpoint_legacy_compatibility_denied",
    ):
        validate_grounded_answer_checkpoint(
            query_plan,
            grounded.artifact,
            answer_text,
            execution_plan=execution_plan,
            execution_record=bound_record,
            allow_legacy_checkpoint=True,
        )


def test_authoritative_units_are_exact_and_parenthetical_units_are_checked() -> None:
    grounded, record, window = _evidence(value=42, unit="count")
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    fact_id = bundle.facts[0].fact_id
    assert validate_analysis_interpretation(
        AnalysisInterpretation(
            summary=AnalysisStatement(text="42 (counts).", fact_ids=(fact_id,))
        ),
        evidence=bundle,
    )
    for text in ("42 (stores).", "42 orders.", "42 users.", "42 people."):
        with pytest.raises(
            AnalysisEvidenceError, match="analysis_interpretation_numeric_unit_unproven"
        ):
            validate_analysis_interpretation(
                AnalysisInterpretation(
                    summary=AnalysisStatement(text=text, fact_ids=(fact_id,))
                ),
                evidence=bundle,
            )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_unit_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(
                    text="42 percentage points.", fact_ids=(fact_id,)
                )
            ),
            evidence=bundle,
        )


@pytest.mark.parametrize(
    "text",
    ("42%.", "42 percent.", "42, percent.", "42: percent.", "42 (percent)."),
)
def test_percent_unit_punctuation_forms_bind_to_the_authoritative_unit(
    text: str,
) -> None:
    grounded, record, window = _evidence(value=42, unit="percent")
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    assert validate_analysis_interpretation(
        AnalysisInterpretation(
            summary=AnalysisStatement(text=text, fact_ids=(bundle.facts[0].fact_id,))
        ),
        evidence=bundle,
    )


def test_percentage_points_remain_distinct_from_percent() -> None:
    grounded, record, window = _evidence(value=42, unit="percent")
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_unit_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(
                    text="42 percentage points.", fact_ids=(bundle.facts[0].fact_id,)
                )
            ),
            evidence=bundle,
        )


def test_cny_prefix_requires_the_authoritative_currency_unit() -> None:
    grounded, record, window = _evidence(value="93.20", unit="currency_cny")
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    fact_id = bundle.facts[0].fact_id
    assert validate_analysis_interpretation(
        AnalysisInterpretation(
            summary=AnalysisStatement(text="CNY 93.20.", fact_ids=(fact_id,))
        ),
        evidence=bundle,
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_numeric_unit_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(text="USD 93.20.", fact_ids=(fact_id,))
            ),
            evidence=bundle,
        )


def test_data_as_of_requires_the_complete_iso_reference() -> None:
    grounded, record, window = _evidence(
        data_as_of=datetime(2026, 9, 20, 12, 34, tzinfo=UTC)
    )
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )
    fact_id = bundle.facts[0].fact_id
    complete = AnalysisInterpretation(
        summary=AnalysisStatement(
            text="The source watermark is 2026-09-20T12:34:00+00:00.",
            fact_ids=(fact_id,),
        )
    )
    assert validate_analysis_interpretation(complete, evidence=bundle) is complete
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_date_fact_unproven"
    ):
        validate_analysis_interpretation(
            AnalysisInterpretation(
                summary=AnalysisStatement(
                    text="The source watermark is 2026-09-20.", fact_ids=(fact_id,)
                )
            ),
            evidence=bundle,
        )
    for text in (
        "The source watermark is 12:34:00.",
        "The source watermark is 2026-09-20T12:34:00Z.",
    ):
        with pytest.raises(
            AnalysisEvidenceError, match="analysis_interpretation_date_fact_unproven"
        ):
            validate_analysis_interpretation(
                AnalysisInterpretation(
                    summary=AnalysisStatement(text=text, fact_ids=(fact_id,))
                ),
                evidence=bundle,
            )


def test_conservative_timestamp_aggregation_is_complete_or_none() -> None:
    earlier = datetime(2026, 9, 19, tzinfo=UTC)
    later = datetime(2026, 9, 20, tzinfo=UTC)
    assert conservative_data_as_of((later, earlier)) == earlier
    assert conservative_data_as_of(()) is None
    assert conservative_data_as_of((earlier, None)) is None
    assert conservative_data_as_of((earlier, datetime(2026, 9, 20))) is None

    assert conservative_data_as_of(
        (
            datetime(2026, 9, 20, 3, tzinfo=UTC),
            datetime(2026, 9, 20, 10, tzinfo=timezone(timedelta(hours=8))),
        )
    ) == datetime(2026, 9, 20, 2, tzinfo=UTC)

    class _InvalidTimezone(tzinfo):
        def utcoffset(self, _value: datetime | None):
            return None

    invalid = datetime(2026, 9, 20, tzinfo=_InvalidTimezone())
    assert conservative_data_as_of((earlier, invalid)) is None


def test_complete_date_reference_remains_valid_evidence() -> None:
    grounded, record, window = _evidence(
        time_range=TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))
    )
    bundle = _build_authorized_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
    )

    interpretation = AnalysisInterpretation(
        summary=AnalysisStatement(
            text="The observation is dated 2026-09-20.",
            fact_ids=(bundle.facts[0].fact_id,),
        )
    )
    assert validate_analysis_interpretation(interpretation, evidence=bundle) is interpretation
