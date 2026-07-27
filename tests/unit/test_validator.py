"""Tests for result validator."""


from src.nl2sql.agents.codeact_engine.plan_card import ValidationCriteria
from src.nl2sql.agents.codeact_engine.validator import validate_result


def test_validate_passes_for_correct_result() -> None:
    criteria = ValidationCriteria(
        expected_type="float",
        value_range=(0.0, 100.0),
        allow_null=False,
    )
    report = validate_result(result=85.5, stats={}, criteria=criteria)
    assert report.passed is True
    assert len(report.errors) == 0


def test_validate_fails_for_null_result() -> None:
    criteria = ValidationCriteria(expected_type="float", allow_null=False)
    report = validate_result(result=None, stats={}, criteria=criteria)
    assert report.passed is False
    assert any("空" in e for e in report.errors)


def test_validate_allows_null_when_configured() -> None:
    criteria = ValidationCriteria(expected_type="float", allow_null=True)
    report = validate_result(result=None, stats={}, criteria=criteria)
    assert report.passed is True


def test_validate_fails_type_mismatch() -> None:
    criteria = ValidationCriteria(expected_type="int")
    report = validate_result(result="not a number", stats={}, criteria=criteria)
    assert report.passed is False
    assert any("类型不匹配" in e for e in report.errors)


def test_validate_fails_out_of_range() -> None:
    criteria = ValidationCriteria(
        expected_type="float",
        value_range=(0.0, 100.0),
    )
    report = validate_result(result=150.0, stats={}, criteria=criteria)
    assert report.passed is False
    assert any("超出预期范围" in e for e in report.errors)


def test_validate_warns_on_nan() -> None:
    criteria = ValidationCriteria(expected_type="float")
    report = validate_result(result=float("nan"), stats={}, criteria=criteria)
    assert any("NaN" in w for w in report.warnings)


def test_validate_fails_on_inf() -> None:
    criteria = ValidationCriteria(expected_type="float")
    report = validate_result(result=float("inf"), stats={}, criteria=criteria)
    assert report.passed is False
    assert any("Inf" in e for e in report.errors)


def test_validate_list_of_dicts() -> None:
    criteria = ValidationCriteria(
        expected_type="list",
        value_range=(0.0, 100.0),
    )
    result = [{"area": "A", "rate": 85.5}, {"area": "B", "rate": 92.3}]
    report = validate_result(result=result, stats={}, criteria=criteria)
    assert report.passed is True


def test_validate_any_type_always_passes() -> None:
    criteria = ValidationCriteria(expected_type="Any")
    report = validate_result(result="anything", stats={}, criteria=criteria)
    assert report.passed is True
