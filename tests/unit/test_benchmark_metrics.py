"""Tests for Phase 5 benchmark metrics computation."""


from benchmarks.metrics import (
    CaseResult,
    _values_match,
    compute_dynamic_metric_success,
    compute_execution_accuracy,
    compute_failure_rates,
    compute_layer_accuracy,
    compute_mape,
    compute_p95_latency,
    compute_repair_success_rate,
    compute_safety_interception_rate,
    compute_smape,
    generate_report,
    statistical_significance,
)


def _make_result(
    case_id: str = "test",
    success: bool = True,
    output: float | None = 100.0,
    gold: float | None = 100.0,
    layer: str = "L1",
    domain: str = "test",
    mode: str = "sql_only",
    latency: float = 500.0,
    tolerance: float = 0.0,
    repair_attempts: int = 0,
    repair_success: bool = False,
    is_adversarial: bool = False,
    was_intercepted: bool = False,
    should_reject: bool = False,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        layer=layer,
        domain=domain,
        expected_mode=mode,
        execution_success=success,
        output_value=output,
        gold_value=gold,
        latency_ms=latency,
        tolerance=tolerance,
        repair_attempts=repair_attempts,
        repair_success=repair_success,
        is_adversarial=is_adversarial,
        was_intercepted=was_intercepted,
        should_reject=should_reject,
    )


class TestExecutionAccuracy:
    def test_all_correct(self) -> None:
        results = [_make_result(output=10.0, gold=10.0) for _ in range(5)]
        assert compute_execution_accuracy(results) == 1.0

    def test_half_correct(self) -> None:
        results = [
            _make_result(output=10.0, gold=10.0),
            _make_result(output=20.0, gold=10.0),
        ]
        assert compute_execution_accuracy(results) == 0.5

    def test_empty(self) -> None:
        assert compute_execution_accuracy([]) == 0.0

    def test_with_tolerance(self) -> None:
        r = _make_result(output=10.1, gold=10.0, tolerance=0.2)
        assert compute_execution_accuracy([r]) == 1.0

    def test_string_match(self) -> None:
        r = _make_result(output="hello", gold="HELLO")
        assert compute_execution_accuracy([r]) == 1.0


class TestDynamicMetricSuccess:
    def test_success(self) -> None:
        results = [_make_result(mode="sql_plus_code", success=True, output=42.0, gold=42.0)]
        assert compute_dynamic_metric_success(results) == 1.0

    def test_failure(self) -> None:
        results = [_make_result(mode="sql_plus_code", success=False, output=None, gold=42.0)]
        assert compute_dynamic_metric_success(results) == 0.0

    def test_no_dynamic_cases(self) -> None:
        results = [_make_result(mode="sql_only")]
        assert compute_dynamic_metric_success(results) == 0.0


class TestMAPE:
    def test_perfect(self) -> None:
        results = [_make_result(output=100.0, gold=100.0)]
        assert compute_mape(results) == 0.0

    def test_ten_percent_error(self) -> None:
        results = [_make_result(output=110.0, gold=100.0)]
        assert abs(compute_mape(results) - 0.1) < 0.001

    def test_zero_gold_skipped(self) -> None:
        results = [_make_result(output=10.0, gold=0.0)]
        assert compute_mape(results) == 0.0


class TestSMAPE:
    def test_perfect(self) -> None:
        results = [_make_result(output=100.0, gold=100.0)]
        assert compute_smape(results) == 0.0

    def test_nonzero(self) -> None:
        results = [_make_result(output=110.0, gold=100.0)]
        assert compute_smape(results) > 0

    def test_symmetric(self) -> None:
        r1 = [_make_result(output=110.0, gold=100.0)]
        r2 = [_make_result(output=100.0, gold=110.0)]
        assert abs(compute_smape(r1) - compute_smape(r2)) < 0.001


class TestFailureRates:
    def test_no_failures(self) -> None:
        results = [_make_result(success=True, mode="sql_only")]
        sql_fail, code_fail = compute_failure_rates(results)
        assert sql_fail == 0.0

    def test_sql_failure(self) -> None:
        results = [_make_result(success=False, mode="sql_only")]
        results[0].generated_sql = "SELECT 1"
        sql_fail, _ = compute_failure_rates(results)
        assert sql_fail == 1.0


class TestRepairRate:
    def test_no_repairs(self) -> None:
        results = [_make_result()]
        assert compute_repair_success_rate(results) == 0.0

    def test_successful_repair(self) -> None:
        results = [_make_result(repair_attempts=2, repair_success=True)]
        assert compute_repair_success_rate(results) == 1.0


class TestP95Latency:
    def test_single_value(self) -> None:
        results = [_make_result(latency=100.0)]
        assert compute_p95_latency(results) == 100.0

    def test_multiple_values(self) -> None:
        results = [_make_result(latency=float(i)) for i in range(1, 101)]
        p95 = compute_p95_latency(results)
        assert p95 >= 95.0


class TestSafetyInterception:
    def test_all_intercepted(self) -> None:
        results = [_make_result(is_adversarial=True, was_intercepted=True, should_reject=True)]
        assert compute_safety_interception_rate(results) == 1.0

    def test_none_intercepted(self) -> None:
        results = [_make_result(is_adversarial=True, was_intercepted=False, should_reject=True)]
        assert compute_safety_interception_rate(results) == 0.0

    def test_no_adversarial(self) -> None:
        results = [_make_result()]
        assert compute_safety_interception_rate(results) == 1.0


class TestLayerAccuracy:
    def test_by_layer(self) -> None:
        results = [
            _make_result(layer="L1", output=10.0, gold=10.0),
            _make_result(layer="L1", output=20.0, gold=10.0),
            _make_result(layer="L2", output=10.0, gold=10.0),
        ]
        acc = compute_layer_accuracy(results)
        assert acc["L1"] == 0.5
        assert acc["L2"] == 1.0


class TestGenerateReport:
    def test_report_fields(self) -> None:
        results = [_make_result()]
        report = generate_report("test-run", results)
        assert report.run_id == "test-run"
        assert report.total_cases == 1
        d = report.to_dict()
        assert "execution_accuracy" in d


class TestStatisticalSignificance:
    def test_identical_scores(self) -> None:
        scores = [0.8] * 30
        sig = statistical_significance(scores, scores)
        assert sig["mean_diff"] == 0.0
        assert not sig["significant"]

    def test_different_scores(self) -> None:
        baseline = [0.5] * 50
        experiment = [0.9] * 50
        sig = statistical_significance(baseline, experiment)
        assert sig["mean_diff"] > 0
        assert sig["significant"]

    def test_small_sample(self) -> None:
        sig = statistical_significance([1.0], [0.0])
        assert sig["n"] == 1
        assert not sig["significant"]


class TestValuesMatch:
    def test_numeric_exact(self) -> None:
        assert _values_match(10.0, 10.0)

    def test_numeric_tolerance(self) -> None:
        assert _values_match(10.1, 10.0, tolerance=0.2)
        assert not _values_match(10.5, 10.0, tolerance=0.2)

    def test_string_case_insensitive(self) -> None:
        assert _values_match("Hello", "hello")

    def test_none_match(self) -> None:
        assert _values_match(None, None)
        assert not _values_match(None, 10.0)
