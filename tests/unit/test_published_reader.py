"""P3 Slice 2: PublishedMetricReader contract/seam tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from src.nl2sql.semantic.published_reader import (
    PublishedMetricReadBinding,
    PublishedMetricReader,
    PublishedMetricReadEvidence,
    PublishedMetricReadOutcome,
    PublishedMetricReadReceipt,
    PublishedMetricReadRequest,
)
from src.nl2sql.semantic.published_result import (
    CURRENT_STATE,
    EffectivePublishedMetricResult,
    PublishedMetricKey,
)

AWARE = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
CHECKSUM = "a" * 64


def _binding(**overrides: object) -> PublishedMetricReadBinding:
    base: dict[str, object] = {
        "semantic_release_id": "release-1",
        "semantic_release_checksum": CHECKSUM,
        "published_source_id": "source-1",
        "binding_revision": "rev-1",
    }
    base.update(overrides)
    return PublishedMetricReadBinding(**base)  # type: ignore[arg-type]


def _key(
    metric_code: str = "metric.a", time_value: Any = CURRENT_STATE, **o: object
) -> PublishedMetricKey:
    base: dict[str, object] = {
        "metric_code": metric_code,
        "time_grain": "month",
        "time_value": time_value,
        "dimension_type": "all",
        "area_id": None,
        "team_id": None,
        "employee_id": None,
        "category_code": "all",
    }
    base.update(o)
    return PublishedMetricKey(**base)  # type: ignore[arg-type]


def _result(
    key: PublishedMetricKey, status: str = "success", **o: object
) -> EffectivePublishedMetricResult:
    base: dict[str, object] = {
        "key": key,
        "status": status,
        "value": Decimal("1"),
        "value_type": "decimal",
        "unit": "count",
        "computed_at": AWARE,
        "effective_origin": "automated",
        "effective_revision": "rev-1",
    }
    base.update(o)
    return EffectivePublishedMetricResult(**base)  # type: ignore[arg-type]


def _evidence(result: EffectivePublishedMetricResult, **o: object) -> PublishedMetricReadEvidence:
    base: dict[str, object] = {
        "key": result.key,
        "key_checksum": result.key.checksum,
        "result_checksum": result.checksum,
        "semantic_time": result.key.time_value,
        "effective_origin": result.effective_origin,
        "effective_revision": result.effective_revision,
        "data_as_of": result.data_as_of,
        "freshness_status": result.freshness_status,
        "data_quality_score": result.data_quality_score,
    }
    base.update(o)
    return PublishedMetricReadEvidence(**base)  # type: ignore[arg-type]


def _request(keys: tuple[PublishedMetricKey, ...], **o: object) -> PublishedMetricReadRequest:
    base: dict[str, object] = {"binding": _binding(), "keys": keys}
    base.update(o)
    return PublishedMetricReadRequest(**base)  # type: ignore[arg-type]


def _receipt(
    status: str = "succeeded",
    evidence: tuple[PublishedMetricReadEvidence, ...] = (),
    **o: object,
) -> PublishedMetricReadReceipt:
    base: dict[str, object] = {
        "binding": _binding(),
        "started_at": AWARE,
        "completed_at": AWARE + timedelta(seconds=1),
        "status": status,
        "failure_code": None,
        "evidence": evidence,
    }
    base.update(o)
    return PublishedMetricReadReceipt(**base)  # type: ignore[arg-type]


def _outcome(
    request: PublishedMetricReadRequest,
    results: tuple[EffectivePublishedMetricResult, ...],
    receipt: PublishedMetricReadReceipt,
) -> PublishedMetricReadOutcome:
    return PublishedMetricReadOutcome(request=request, results=results, receipt=receipt)


# --- binding / request ---


def test_binding_is_frozen_strict_and_extra_forbid() -> None:
    binding = _binding()
    with pytest.raises(ValidationError):
        binding.semantic_release_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PublishedMetricReadBinding(**{**_binding().model_dump(), "extra": 1})


@pytest.mark.parametrize(
    "field", ["semantic_release_id", "published_source_id", "binding_revision"]
)
def test_binding_rejects_blank_identifiers(field: str) -> None:
    with pytest.raises(ValidationError):
        _binding(**{field: "   "})


def test_binding_rejects_malformed_release_checksum() -> None:
    with pytest.raises(ValidationError):
        _binding(semantic_release_checksum="not-a-checksum")


def test_binding_preserves_valid_strings_verbatim() -> None:
    binding = _binding(semantic_release_id=" release-1 ", binding_revision=" rev-1 ")
    assert binding.semantic_release_id == " release-1 "
    assert binding.binding_revision == " rev-1 "


def test_request_requires_one_to_256_keys_and_rejects_duplicates() -> None:
    with pytest.raises(ValidationError):
        _request(())
    assert len(_request((_key(),)).keys) == 1
    many = tuple(_key(f"metric.{i}") for i in range(256))
    assert len(_request(many).keys) == 256
    too_many = tuple(_key(f"metric.{i}") for i in range(257))
    with pytest.raises(ValidationError):
        _request(too_many)
    with pytest.raises(ValidationError):
        _request((_key("dup"), _key("dup")))


def test_request_preserves_order_without_sorting() -> None:
    keys = (_key("m.b"), _key("m.a"), _key("m.c"))
    assert [k.metric_code for k in _request(keys).keys] == ["m.b", "m.a", "m.c"]


# --- receipt ---


def test_readonly_is_structural_true() -> None:
    assert _receipt().readonly is True
    with pytest.raises(ValidationError):
        _receipt(readonly=False)


@pytest.mark.parametrize("field", ["started_at", "completed_at"])
def test_receipt_timestamps_must_be_offset_aware(field: str) -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    with pytest.raises(ValidationError):
        _receipt(**{field: naive})


class _NullOffsetTz(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        del dt
        return None

    def dst(self, dt: datetime | None) -> None:
        del dt
        return None

    def tzname(self, dt: datetime | None) -> str:
        del dt
        return "NULL"


def test_receipt_rejects_tzinfo_without_real_offset() -> None:
    pseudo = datetime(2026, 1, 2, 3, 4, 5, tzinfo=_NullOffsetTz())
    with pytest.raises(ValidationError):
        _receipt(started_at=pseudo)


class _FoldTz(tzinfo):
    """Fold-dependent offset: fold=0 -> UTC-04:00, fold=1 -> UTC-05:00."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=-4)
        return timedelta(hours=-5) if dt.fold else timedelta(hours=-4)

    def dst(self, dt: datetime | None) -> timedelta:
        del dt
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        del dt
        return "FOLD"


def test_receipt_rejects_completed_before_started() -> None:
    with pytest.raises(ValidationError):
        _receipt(completed_at=AWARE - timedelta(seconds=1))


def test_receipt_rejects_fold_inverted_actual_instants() -> None:
    shared = _FoldTz()
    started = datetime(2026, 11, 1, 1, 30, tzinfo=shared, fold=1)  # 06:30 UTC
    completed = datetime(2026, 11, 1, 1, 30, tzinfo=shared, fold=0)  # 05:30 UTC
    assert started.tzinfo is completed.tzinfo is shared
    assert started.astimezone(timezone.utc) == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    assert completed.astimezone(timezone.utc) == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        _receipt(started_at=started, completed_at=completed)
    accepted = _receipt(started_at=completed, completed_at=started)
    assert accepted.completed_at.astimezone(timezone.utc) == datetime(
        2026, 11, 1, 6, 30, tzinfo=timezone.utc
    )


def test_receipt_accepts_later_instant_across_offsets() -> None:
    started = datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 1, 2, 4, 30, tzinfo=timezone(timedelta(hours=1)))
    receipt = _receipt(started_at=started, completed_at=completed)
    assert receipt.completed_at == completed


def test_receipt_accepts_equal_actual_instant() -> None:
    started = datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 1, 2, 4, 0, tzinfo=timezone(timedelta(hours=1)))
    receipt = _receipt(started_at=started, completed_at=completed)
    assert receipt.completed_at == completed


def test_receipt_preserves_caller_timestamp_representation() -> None:
    shared = _FoldTz()
    started = datetime(2026, 11, 1, 1, 30, tzinfo=shared, fold=1)
    completed = datetime(2026, 11, 1, 1, 30, tzinfo=shared, fold=0)
    receipt = _receipt(started_at=completed, completed_at=started)
    assert receipt.started_at == completed
    assert receipt.started_at.utcoffset() == completed.utcoffset()
    assert receipt.started_at.fold == completed.fold
    assert receipt.completed_at == started
    assert receipt.completed_at.utcoffset() == started.utcoffset()
    assert receipt.completed_at.fold == started.fold


def test_receipt_status_and_failure_code_consistency() -> None:
    with pytest.raises(ValidationError):
        _receipt(status="succeeded", failure_code="read_failed")
    with pytest.raises(ValidationError):
        _receipt(status="failed", failure_code=None)


@pytest.mark.parametrize(
    "code",
    [
        "binding_missing",
        "binding_incomplete",
        "source_unavailable",
        "read_failed",
        "duplicate_effective_result",
        "result_invalid",
    ],
)
def test_each_failure_code_is_distinct_and_retained(code: str) -> None:
    receipt = _receipt(status="failed", failure_code=code)
    assert receipt.failure_code == code


# --- success batch ---


def _batch() -> tuple[
    PublishedMetricReadRequest,
    tuple[EffectivePublishedMetricResult, ...],
    PublishedMetricReadReceipt,
]:
    keys = (
        _key("m.success"),
        _key("m.partial"),
        _key("m.no_data"),
        _key("m.missing"),
        _key("m.zero"),
    )
    results = (
        _result(keys[0], "success", value=Decimal("1")),
        _result(keys[1], "partial", value=None),
        _result(keys[2], "no_data", value=None),
        _result(
            keys[3],
            "missing",
            value=None,
            effective_origin=None,
            effective_revision=None,
            computed_at=None,
        ),
        _result(keys[4], "success", value=Decimal("0")),
    )
    evidences = tuple(_evidence(result) for result in results)
    return _request(keys), results, _receipt(evidence=evidences)


def test_success_batch_preserves_count_order_and_binding() -> None:
    request, results, receipt = _batch()
    outcome = _outcome(request, results, receipt)
    assert [r.status for r in outcome.results] == [
        "success",
        "partial",
        "no_data",
        "missing",
        "success",
    ]
    assert outcome.results[4].value == Decimal("0")
    assert outcome.receipt.status == "succeeded"
    assert outcome.receipt.failure_code is None
    assert outcome.receipt.binding == outcome.request.binding
    for index, key in enumerate(request.keys):
        assert outcome.results[index].key == key
        assert outcome.receipt.evidence[index].key == key
        assert outcome.receipt.evidence[index].key_checksum == key.checksum
        assert outcome.receipt.evidence[index].result_checksum == outcome.results[index].checksum


def test_business_missing_and_failed_are_succeeded_reads() -> None:
    missing = _result(
        _key("m.business"),
        "missing",
        value=None,
        effective_origin=None,
        effective_revision=None,
        computed_at=None,
    )
    failed = _result(_key("m.failed"), "failed", value=None, error_message="business failure")
    results = (missing, failed)
    request = _request((missing.key, failed.key))
    receipt = _receipt(evidence=tuple(_evidence(result) for result in results))
    outcome = _outcome(request, results, receipt)
    assert [r.status for r in outcome.results] == ["missing", "failed"]
    assert outcome.receipt.status == "succeeded"
    assert outcome.receipt.failure_code is None


@pytest.mark.parametrize(
    "code",
    [
        "binding_missing",
        "binding_incomplete",
        "source_unavailable",
        "read_failed",
        "duplicate_effective_result",
        "result_invalid",
    ],
)
def test_infrastructure_failure_has_no_results_or_evidence(code: str) -> None:
    request = _request((_key(),))
    outcome = _outcome(request, (), _receipt(status="failed", failure_code=code))
    assert outcome.receipt.status == "failed"
    assert outcome.receipt.failure_code == code
    assert outcome.results == ()
    assert outcome.receipt.evidence == ()


# --- evidence / outcome consistency ---


def test_evidence_rejects_key_checksum_and_semantic_time_mismatch() -> None:
    result = _result(_key())
    with pytest.raises(ValidationError):
        _evidence(result, key_checksum="b" * 64)
    with pytest.raises(ValidationError):
        _evidence(result, semantic_time=date(2000, 1, 1))


def test_outcome_rejects_count_and_order_mismatches() -> None:
    request, results, receipt = _batch()
    with pytest.raises(ValidationError):
        _outcome(request, results[:-1], receipt)
    short_evidence = _receipt(evidence=receipt.evidence[:-1])
    with pytest.raises(ValidationError):
        _outcome(request, results, short_evidence)
    swapped = (results[1], results[0], *results[2:])
    with pytest.raises(ValidationError):
        _outcome(request, swapped, receipt)


def test_outcome_rejects_receipt_binding_mismatch() -> None:
    request, results, receipt = _batch()
    other_binding = _binding(
        semantic_release_id="release-2", semantic_release_checksum="b" * 64
    )
    mismatched = _receipt(evidence=receipt.evidence, binding=other_binding)
    with pytest.raises(ValidationError):
        _outcome(request, results, mismatched)
    failed_mismatch = _receipt(
        status="failed", failure_code="read_failed", binding=other_binding
    )
    with pytest.raises(ValidationError):
        _outcome(request, (), failed_mismatch)


def test_outcome_rejects_result_checksum_mismatch() -> None:
    request, results, receipt = _batch()
    tampered = list(receipt.evidence)
    tampered[0] = _evidence(results[0], result_checksum="b" * 64)
    with pytest.raises(ValidationError):
        _outcome(request, results, _receipt(evidence=tuple(tampered)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("effective_origin", "different"),
        ("effective_revision", "different"),
        ("data_as_of", AWARE + timedelta(days=1)),
        ("freshness_status", "fresh"),
        ("data_quality_score", 50),
    ],
)
def test_outcome_rejects_provenance_mismatches(field: str, value: object) -> None:
    request, results, receipt = _batch()
    tampered = list(receipt.evidence)
    tampered[0] = _evidence(results[0], **{field: value})
    with pytest.raises(ValidationError):
        _outcome(request, results, _receipt(evidence=tuple(tampered)))


# --- CURRENT_STATE evidence (fixture-only) ---


def test_current_state_key_carries_sentinel_only_in_evidence() -> None:
    # Fixture evidence only: this is NOT a general storage inference rule.
    key = _key("m.current", time_value=CURRENT_STATE)
    result = _result(key, "success", value=Decimal("1"))
    evidence = _evidence(result, stored_time_value=date(2000, 1, 1))
    outcome = _outcome(_request((key,)), (result,), _receipt(evidence=(evidence,)))
    assert outcome.request.keys[0].time_value == CURRENT_STATE
    assert outcome.receipt.evidence[0].semantic_time == CURRENT_STATE
    assert outcome.receipt.evidence[0].stored_time_value == date(2000, 1, 1)
    assert "2000-01-01" not in outcome.request.keys[0].model_dump_json()


def test_historical_date_key_stays_a_date() -> None:
    key = _key("m.historical", time_value=date(2000, 1, 1))
    assert key.time_value == date(2000, 1, 1)
    assert key.is_current_state is False


# --- protocol ---


class _StubReader:
    async def read(self, request: PublishedMetricReadRequest) -> PublishedMetricReadOutcome:
        raise NotImplementedError


def test_protocol_is_runtime_checkable() -> None:
    assert isinstance(_StubReader(), PublishedMetricReader)
    assert not isinstance(object(), PublishedMetricReader)
