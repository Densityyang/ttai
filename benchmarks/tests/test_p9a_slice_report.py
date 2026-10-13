from __future__ import annotations

import json

from benchmarks.metrics import CaseResult, generate_report
from benchmarks.runner import _generate_markdown_report, save_report


def _case_result(
    case_id: str,
    *,
    mode: str = "QUERY",
    capability: str = "fetch",
    risk: str = "low",
    oracle_state: str = "MATERIALIZED",
    receipt_required: bool = True,
    receipt_present: bool = True,
    output_value: object = 42,
    gold_value: object = 42,
) -> CaseResult:
    has_receipt = receipt_present
    return CaseResult(
        case_id=case_id,
        layer="L1",
        domain="slice-test",
        expected_mode="sql_only",
        execution_success=True,
        output_value=output_value,
        gold_value=gold_value,
        expected_outcome="CORRECT_ANSWER",
        oracle_state=oracle_state,
        mode=mode,
        capability=capability,
        risk=risk,
        receipt_required=receipt_required,
        receipt_present=receipt_present,
        answer_type="answer" if has_receipt else "",
        policy_outcome="allow" if has_receipt else "",
        execution_accepted=has_receipt,
    )


def test_partial_receipt_coverage_keeps_failures_and_denominator() -> None:
    report = generate_report(
        "partial-coverage",
        [
            _case_result("valid-receipt"),
            _case_result("missing-receipt", receipt_present=False),
        ],
    )

    assert report.denominators["adjudicated_cases"] == 2
    assert report.denominators["adjudicated_typed_receipts_present"] == 1
    assert report.denominators["adjudicated_cases_without_valid_typed_receipt"] == 1
    assert report.denominators["adjudicated_typed_receipt_coverage"] == 0.5
    assert report.accuracy_established is False
    assert report.accuracy_evidence_state == "INCOMPLETE_RECEIPT_COVERAGE"
    assert report.execution_accuracy == 0.5
    assert report.outcome_counts["PROVENANCE_FAILURE"] == 1
    assert report.results[1].observed_outcome == "PROVENANCE_FAILURE"
    assert report.results[1].passed is False


def test_complete_receipt_coverage_can_measure_a_real_zero_percent() -> None:
    report = generate_report(
        "measured-zero",
        [_case_result("valid-receipt-wrong-answer", output_value=0)],
    )

    assert report.accuracy_established is True
    assert report.accuracy_evidence_state == "MEASURED_TYPED"
    assert report.execution_accuracy == 0.0
    assert report.denominators["adjudicated_cases"] == 1
    assert report.denominators["adjudicated_typed_receipts_present"] == 1
    assert "| Execution Accuracy | 0.00% |" in _generate_markdown_report(report)

    fully_evidenced = generate_report(
        "complete-coverage",
        [_case_result("complete-one"), _case_result("complete-two")],
    )
    assert fully_evidenced.accuracy_established is True
    assert fully_evidenced.accuracy_evidence_state == "MEASURED_TYPED"
    assert fully_evidenced.denominators["adjudicated_cases"] == 2
    assert fully_evidenced.denominators["adjudicated_typed_receipts_present"] == 2
    assert fully_evidenced.denominators["adjudicated_typed_receipt_coverage"] == 1.0


def test_zero_adjudicable_mode_capability_and_risk_slices_render_na() -> None:
    report = generate_report(
        "empty-slices",
        [
            _case_result(
                "query-unknown", mode="QUERY", capability="fetch", risk="low",
                oracle_state="MISSING",
            ),
            _case_result(
                "analyze-unknown", mode="ANALYZE", capability="analyze", risk="medium",
                oracle_state="MISSING",
            ),
            _case_result(
                "build-unknown", mode="BUILD", capability="build", risk="high",
                oracle_state="MISSING",
            ),
        ],
    )

    assert report.accuracy_established is False
    assert report.accuracy_evidence_state == "NO_ADJUDICABLE_CASES"
    assert report.execution_accuracy == 0.0
    assert isinstance(report.to_dict()["execution_accuracy"], float)

    for slices in (report.mode_slices, report.capability_slices, report.risk_slices):
        for data in slices.values():
            assert data["adjudicated_cases"] == 0
            assert data["execution_accuracy"] == 0.0
            assert data["accuracy_established"] is False
            assert data["accuracy_evidence_state"] == "NO_ADJUDICABLE_CASES"

    markdown = _generate_markdown_report(report)
    assert "| QUERY | N/A | 0 | 1 |" in markdown
    assert "| ANALYZE | N/A | 0 | 1 |" in markdown
    assert "| BUILD | N/A | 0 | 1 |" in markdown
    assert "| analyze | N/A | 0 | 1 |" in markdown
    assert "| high | N/A | 0 | 1 |" in markdown
    assert "| QUERY | 0.00%" not in markdown


def test_mixed_mode_slices_keep_independent_evidence_and_denominators() -> None:
    report = generate_report(
        "mixed-modes",
        [
            _case_result("query-valid", mode="QUERY"),
            _case_result("analyze-missing", mode="ANALYZE", receipt_present=False),
            _case_result("build-valid", mode="BUILD"),
        ],
    )

    assert report.accuracy_established is False
    assert report.denominators["adjudicated_cases"] == 3
    assert report.mode_slices["QUERY"]["adjudicated_cases"] == 1
    assert report.mode_slices["QUERY"]["accuracy_established"] is True
    assert report.mode_slices["ANALYZE"]["adjudicated_cases"] == 1
    assert report.mode_slices["ANALYZE"]["accuracy_established"] is False
    assert report.mode_slices["ANALYZE"]["accuracy_evidence_state"] == (
        "INCOMPLETE_RECEIPT_COVERAGE"
    )
    assert report.mode_slices["BUILD"]["adjudicated_cases"] == 1
    assert report.mode_slices["BUILD"]["accuracy_established"] is True

    markdown = _generate_markdown_report(report)
    assert "| QUERY | 100.00% | 1 | 0 |" in markdown
    assert "| ANALYZE | not established | 1 | 0 |" in markdown
    assert "| BUILD | 100.00% | 1 | 0 |" in markdown


def test_legacy_untyped_cases_keep_verdicts_without_typed_accuracy_claim() -> None:
    report = generate_report(
        "legacy-untyped",
        [
            _case_result(
                "legacy-case",
                receipt_required=False,
                receipt_present=False,
            ),
            _case_result(
                "non-required-present-receipt",
                receipt_required=False,
                receipt_present=True,
            ),
        ],
        evidence_kind="legacy-text",
    )

    assert report.results[0].observed_outcome == "CORRECT_ANSWER"
    assert report.results[0].passed is True
    assert report.results[0].receipt_required is False
    assert report.accuracy_established is False
    assert report.accuracy_evidence_state == "NO_TYPED_RECEIPTS"
    assert report.execution_accuracy == 1.0
    assert report.receipts_present == 1
    assert report.denominators["adjudicated_typed_receipts_present"] == 0


def test_json_and_markdown_expose_evidence_state_without_raw_case_ids(tmp_path) -> None:
    raw_case_id = "private-eval-case-8f3a"
    report = generate_report(
        "privacy-evidence-state",
        [
            _case_result(raw_case_id),
            _case_result("missing-case", receipt_present=False),
        ],
    )

    json_path = save_report(report, tmp_path)
    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = json_path.with_suffix(".md").read_text(encoding="utf-8")
    payload = json.loads(json_text)

    assert payload["accuracy_evidence_state"] == "INCOMPLETE_RECEIPT_COVERAGE"
    assert payload["accuracy_established"] is False
    assert payload["execution_accuracy"] == 0.5
    assert payload["denominators"]["adjudicated_cases"] == 2
    assert "INCOMPLETE_RECEIPT_COVERAGE" in markdown_text
    assert raw_case_id not in json_text
    assert raw_case_id not in markdown_text
