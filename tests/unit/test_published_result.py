"""P3 Slice 1: immutable Effective Published Metric Result contract."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.nl2sql.semantic.published_result import (
    CURRENT_STATE,
    EffectivePublishedMetricResult,
    PublishedMetricKey,
)

AWARE = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
NAIVE = datetime(2026, 1, 2, 3, 4, 5)
REPO_ROOT = Path(__file__).resolve().parents[2]

KEY_FIELDS = {
    "metric_code",
    "time_grain",
    "time_value",
    "dimension_type",
    "area_id",
    "team_id",
    "employee_id",
    "category_code",
}


def _key(**overrides: object) -> PublishedMetricKey:
    base: dict[str, object] = {
        "metric_code": "metric.a",
        "time_grain": "month",
        "time_value": CURRENT_STATE,
        "dimension_type": "all",
        "area_id": None,
        "team_id": None,
        "employee_id": None,
        "category_code": "all",
    }
    base.update(overrides)
    return PublishedMetricKey(**base)  # type: ignore[arg-type]


def _result(status: str = "success", **overrides: object) -> EffectivePublishedMetricResult:
    base: dict[str, object] = {
        "key": _key(),
        "status": status,
        "value": Decimal("1"),
        "value_type": "decimal",
        "unit": "count",
        "computed_at": AWARE,
        "effective_origin": "automated",
        "effective_revision": "rev-1",
    }
    base.update(overrides)
    return EffectivePublishedMetricResult(**base)  # type: ignore[arg-type]


def _subprocess_key_checksum(hashseed: str) -> str:
    code = (
        "from src.nl2sql.semantic.published_result import PublishedMetricKey;"
        "print(PublishedMetricKey(metric_code='metric.a',time_grain='month',"
        "time_value='CURRENT_STATE',dimension_type='all',area_id=None,team_id=None,"
        "employee_id=None,category_code='all').checksum)"
    )
    env = dict(os.environ, PYTHONHASHSEED=hashseed)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


class _NullOffsetTzInfo(tzinfo):
    """A tzinfo whose utcoffset() is None despite the object being present."""

    def utcoffset(self, dt: datetime | None) -> None:
        del dt
        return None

    def dst(self, dt: datetime | None) -> None:
        del dt
        return None

    def tzname(self, dt: datetime | None) -> str:
        del dt
        return "NULL-OFFSET"


@pytest.mark.parametrize(
    "field", ["metric_code", "time_grain", "dimension_type", "category_code"]
)
def test_whitespace_only_identity_values_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _key(**{field: "   "})


def test_identity_values_are_preserved_verbatim_and_not_normalized() -> None:
    key = _key(metric_code=" metric.a ", category_code=" all ")
    assert key.metric_code == " metric.a "
    assert key.category_code == " all "
    assert key.checksum == _key(metric_code=" metric.a ", category_code=" all ").checksum


def test_business_key_has_exactly_the_eight_fields() -> None:
    assert set(PublishedMetricKey.model_fields) == KEY_FIELDS
    assert "stored_time_value" not in PublishedMetricKey.model_fields
    assert len(PublishedMetricKey.model_fields) == 8


def test_key_is_frozen_strict_and_rejects_unknown_or_coerced_fields() -> None:
    key = _key()
    with pytest.raises(ValidationError):
        key.metric_code = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PublishedMetricKey(**{**{name: getattr(key, name) for name in KEY_FIELDS}, "extra": 1})
    with pytest.raises(ValidationError):
        _key(area_id="1")
    with pytest.raises(ValidationError):
        _key(metric_code=123)
    with pytest.raises(ValidationError):
        _key(time_value="2000-01-01")


@pytest.mark.parametrize("status", ["success", "partial", "no_data", "failed", "missing"])
def test_all_five_statuses_are_constructible_and_preserved(status: str) -> None:
    value = None if status in {"no_data", "failed", "missing"} else Decimal("1")
    result = _result(status, value=value)
    assert result.status == status
    assert EffectivePublishedMetricResult.model_validate_json(result.model_dump_json()).status == status


def test_zero_is_a_real_value_and_never_becomes_no_data() -> None:
    result = _result("success", value=Decimal("0"))
    assert result.status == "success"
    assert result.value == Decimal("0")
    assert result.status != "no_data"
    assert result.value is not None


@pytest.mark.parametrize("status", ["success", "partial"])
def test_source_null_is_preserved_for_success_and_partial(status: str) -> None:
    result = _result(status, value=None)
    assert result.value is None
    assert result.status == status
    assert result.status not in {"no_data", "missing"}


def test_no_data_forbids_a_value_but_accepts_none() -> None:
    assert _result("no_data", value=None).value is None
    with pytest.raises(ValidationError):
        _result("no_data", value=Decimal("0"))


def test_failed_forbids_a_value_but_accepts_none() -> None:
    assert _result("failed", value=None).value is None
    with pytest.raises(ValidationError):
        _result("failed", value=Decimal("1"))


def test_missing_is_distinct_from_no_data() -> None:
    missing = _result(
        "missing",
        value=None,
        effective_origin=None,
        effective_revision=None,
        computed_at=None,
    )
    no_data = _result("no_data", value=None)
    assert missing.status == "missing"
    assert no_data.status == "no_data"
    assert missing.model_dump_json() != no_data.model_dump_json()
    assert missing.checksum != no_data.checksum
    with pytest.raises(ValidationError):
        _result("missing", value=Decimal("1"))


@pytest.mark.parametrize("status", ["success", "partial", "no_data", "failed"])
def test_non_missing_requires_a_complete_effective_binding(status: str) -> None:
    value = None if status in {"no_data", "failed"} else Decimal("1")
    with pytest.raises(ValidationError):
        _result(status, value=value, effective_origin=None)
    with pytest.raises(ValidationError):
        _result(status, value=value, effective_revision=None)
    with pytest.raises(ValidationError):
        _result(status, value=value, computed_at=None)
    with pytest.raises(ValidationError):
        _result(status, value=value, effective_origin="   ")
    with pytest.raises(ValidationError):
        _result(status, value=value, effective_revision="  ")


def test_current_state_is_semantic_and_preserved() -> None:
    key = _key(time_value=CURRENT_STATE)
    assert key.time_value == "CURRENT_STATE"
    assert key.is_current_state is True
    assert "2000-01-01" not in key.model_dump_json()


def test_a_real_historical_date_is_never_auto_mapped_to_current_state() -> None:
    key = _key(time_value=date(2000, 1, 1))
    assert key.time_value == date(2000, 1, 1)
    assert key.is_current_state is False
    assert key.model_dump(mode="json")["time_value"] == "2000-01-01"


@pytest.mark.parametrize("score", [None, 0, 100])
def test_data_quality_score_bounds_accept(score: int | None) -> None:
    assert _result("success", data_quality_score=score).data_quality_score == score


@pytest.mark.parametrize("score", [-1, 101])
def test_data_quality_score_bounds_reject(score: int) -> None:
    with pytest.raises(ValidationError):
        _result("success", data_quality_score=score)


def test_timestamps_must_be_timezone_aware() -> None:
    assert _result("success", computed_at=AWARE, data_as_of=AWARE).data_as_of == AWARE
    with pytest.raises(ValidationError):
        _result("success", computed_at=NAIVE)
    with pytest.raises(ValidationError):
        _result("success", data_as_of=NAIVE)


def test_tzinfo_without_a_real_offset_is_rejected() -> None:
    pseudo = datetime(2026, 1, 2, 3, 4, 5, tzinfo=_NullOffsetTzInfo())
    assert pseudo.tzinfo is not None
    assert pseudo.utcoffset() is None
    with pytest.raises(ValidationError):
        _result("success", computed_at=pseudo)
    with pytest.raises(ValidationError):
        _result("success", data_as_of=pseudo)


def test_value_type_and_unit_are_not_normalized_or_invented() -> None:
    result = _result("success", value_type="percent", unit=None)
    assert result.value_type == "percent"
    assert result.unit is None
    other = _result("success", value_type="percentage", unit="%")
    assert other.value_type == "percentage"
    assert other.unit == "%"
    assert result.checksum != other.checksum


def test_checksum_is_stable_field_sensitive_and_process_independent() -> None:
    base = _result("success")
    assert base.checksum == _result("success").checksum
    assert base.schema_version == "1.0"
    assert base.checksum != _result("success", value=Decimal("2")).checksum
    assert base.checksum != _result("success", effective_revision="rev-2").checksum
    assert base.checksum != _result("partial").checksum
    assert base.checksum != _result("no_data", value=None).checksum
    assert base.checksum != _result("success", key=_key(metric_code="metric.b")).checksum
    assert _key().checksum != _key(area_id=0).checksum
    assert _key(area_id=0).checksum == _key(area_id=0).checksum
    assert _key().checksum == _subprocess_key_checksum("0")
    assert _subprocess_key_checksum("0") == _subprocess_key_checksum("1")


def _subprocess_result_checksum(hashseed: str) -> str:
    code = (
        "from datetime import datetime,timezone;from decimal import Decimal;"
        "from src.nl2sql.semantic.published_result import CURRENT_STATE,"
        "EffectivePublishedMetricResult,PublishedMetricKey;"
        "k=PublishedMetricKey(metric_code='metric.a',time_grain='month',"
        "time_value=CURRENT_STATE,dimension_type='all',area_id=None,team_id=None,"
        "employee_id=None,category_code='all');"
        "r=EffectivePublishedMetricResult(key=k,status='success',value=Decimal('1.0'),"
        "value_type='decimal',unit='count',"
        "computed_at=datetime(2026,1,2,4,4,5,tzinfo=timezone.utc),"
        "effective_origin='automated',effective_revision='rev-1');"
        "print(r.checksum)"
    )
    env = dict(os.environ, PYTHONHASHSEED=hashseed)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_decimal_numeric_equivalence_canonicalizes_to_one_checksum() -> None:
    one = (Decimal("1"), Decimal("1.0"), Decimal("1.00"), Decimal("1E+0"))
    assert len({_result("success", value=v).checksum for v in one}) == 1
    hundred = (Decimal("100"), Decimal("100.000"), Decimal("1E+2"))
    assert len({_result("success", value=v).checksum for v in hundred}) == 1
    zero = (Decimal("0"), Decimal("-0"), Decimal("0.000"))
    assert len({_result("success", value=v).checksum for v in zero}) == 1


def test_decimal_difference_remains_distinct() -> None:
    assert (
        _result("success", value=Decimal("1")).checksum
        != _result("success", value=Decimal("1.0001")).checksum
    )


def test_computed_at_same_instant_across_offsets_is_canonical() -> None:
    utc = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    plus_one = datetime(2026, 1, 2, 4, 4, 5, tzinfo=timezone(timedelta(hours=1)))
    assert (
        _result("success", computed_at=utc).checksum
        == _result("success", computed_at=plus_one).checksum
    )


def test_data_as_of_same_instant_across_offsets_is_canonical() -> None:
    utc = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    plus_one = datetime(2026, 1, 2, 4, 4, 5, tzinfo=timezone(timedelta(hours=1)))
    assert (
        _result("success", data_as_of=utc).checksum
        == _result("success", data_as_of=plus_one).checksum
    )


def test_different_timestamp_instants_remain_distinct() -> None:
    a = _result("success", computed_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    b = _result("success", computed_at=datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc))
    assert a.checksum != b.checksum


class _FixedOffsetTz(tzinfo):
    def __init__(self, offset: timedelta) -> None:
        self._offset = offset

    def utcoffset(self, dt: datetime | None) -> timedelta:
        del dt
        return self._offset

    def dst(self, dt: datetime | None) -> timedelta:
        del dt
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        del dt
        return f"UTC{self._offset}"


def test_custom_tz_different_instants_remain_distinct() -> None:
    east = datetime(2026, 11, 1, 1, 30, tzinfo=_FixedOffsetTz(timedelta(hours=-4)))
    west = datetime(2026, 11, 1, 1, 30, tzinfo=_FixedOffsetTz(timedelta(hours=-5)))
    assert east != west
    assert (
        _result("success", computed_at=east).checksum
        != _result("success", computed_at=west).checksum
    )


def test_canonicalization_does_not_mutate_the_stored_values() -> None:
    plus_one = datetime(2026, 1, 2, 4, 4, 5, tzinfo=timezone(timedelta(hours=1)))
    result = _result("success", value=Decimal("1.00"), computed_at=plus_one, data_as_of=plus_one)
    _ = result.checksum
    assert result.computed_at == plus_one
    assert result.computed_at is not None
    assert result.computed_at.utcoffset() == timedelta(hours=1)
    assert result.data_as_of is not None
    assert result.data_as_of.utcoffset() == timedelta(hours=1)
    assert result.value == Decimal("1.00")
    assert str(result.value) == "1.00"


def test_key_checksum_regression_and_distinctness() -> None:
    assert _key().checksum == _key().checksum
    assert _key().checksum != _key(metric_code="metric.b").checksum
    assert _key(time_value=CURRENT_STATE).checksum != _key(time_value=date(2000, 1, 1)).checksum
    assert _key(area_id=None).checksum != _key(area_id=0).checksum


def test_result_checksum_is_process_deterministic() -> None:
    expected = _result(
        "success",
        value=Decimal("1.0"),
        computed_at=datetime(2026, 1, 2, 4, 4, 5, tzinfo=timezone.utc),
    ).checksum
    assert expected == _subprocess_result_checksum("0")
    assert _subprocess_result_checksum("0") == _subprocess_result_checksum("1")
