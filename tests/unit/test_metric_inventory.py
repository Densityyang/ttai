"""Focused P1 tests for the authoritative metric inventory boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.nl2sql.semantic.metric_inventory import (
    EXPECTED_CANONICAL_COUNT,
    EXPECTED_CANONICAL_DERIVED_COUNT,
    EXPECTED_CANONICAL_EXTERNAL_COUNT,
    EXPECTED_CANONICAL_RAW_COUNT,
    EXPECTED_LEGACY_ONLY_COUNT,
    EXPECTED_TOTAL_COUNT,
    InventoryValidationError,
    MetricInventorySnapshot,
    SourceProvenance,
    adapt_canonical_yaml_document,
    adapt_legacy_semantic_text,
    build_metric_inventory,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v4_p1" / "authoritative"
CANONICAL_ROOT = FIXTURE_ROOT / "canonical"
LEGACY_PATH = FIXTURE_ROOT / "legacy" / "semantic.md"

EXPECTED_LEGACY_ONLY_KEYS = {
    "complaint_arrival_on_time_count_area_day",
    "complaint_arrival_rate_area_day",
    "complaint_calc_total_count_area_day",
    "complaint_first_response_on_time_count_area_day",
    "complaint_first_response_rate_area_day",
    "complaint_same_day_repair_on_time_count_area_day",
    "complaint_same_day_repair_rate_area_day",
    "installation_arrival_on_time_count_area_day",
    "installation_arrival_rate_area_day",
    "installation_first_response_on_time_count_area_day",
    "installation_first_response_rate_area_day",
    "installation_same_day_archive_on_time_count_area_day",
    "installation_same_day_calc_total_count_area_day",
    "installation_same_day_rate_area_day",
}
EXPECTED_SNAPSHOT_FINGERPRINT = "6f8f9687eed492e126a30ac71f4936797a3ab93ef1b6c0d3d75c277ceeca31d0"


def _inventory() -> MetricInventorySnapshot:
    return build_metric_inventory(CANONICAL_ROOT, LEGACY_PATH)


def test_authoritative_inventory_locks_counts_provenance_and_legacy_boundary() -> None:
    snapshot = _inventory()

    assert len(snapshot.canonical) == EXPECTED_CANONICAL_COUNT == 280
    assert len(snapshot.legacy) == EXPECTED_LEGACY_ONLY_COUNT == 14
    assert snapshot.total_count == EXPECTED_TOTAL_COUNT == 294
    assert snapshot.canonical_summary.raw_count == EXPECTED_CANONICAL_RAW_COUNT == 180
    assert snapshot.canonical_summary.derived_count == EXPECTED_CANONICAL_DERIVED_COUNT == 56
    assert snapshot.canonical_summary.external_count == EXPECTED_CANONICAL_EXTERNAL_COUNT == 44
    assert snapshot.canonical_summary.file_count == 10
    assert snapshot.legacy_summary.declared_key_count == 93
    assert snapshot.legacy_summary.unique_declared_key_count == 93
    assert snapshot.legacy_summary.canonical_overlap_count == 79
    assert snapshot.legacy_summary.exact_canonical_overlap_count == 75
    assert snapshot.legacy_summary.compact_alias_count == 4
    assert {item.metric_key for item in snapshot.legacy} == EXPECTED_LEGACY_ONLY_KEYS
    assert all(item.provenance.source_kind == "canonical_gold" for item in snapshot.canonical)
    assert all(item.provenance.source_kind == "legacy_semantic" for item in snapshot.legacy)
    assert all(item.metric_type == "legacy_only" for item in snapshot.legacy)
    assert snapshot.fingerprint == EXPECTED_SNAPSHOT_FINGERPRINT


def test_inventory_fingerprint_is_stable_for_repeated_reads_and_record_order() -> None:
    snapshot = _inventory()
    repeated = _inventory()
    reordered = MetricInventorySnapshot(
        canonical=tuple(reversed(snapshot.canonical)),
        legacy=tuple(reversed(snapshot.legacy)),
        canonical_summary=snapshot.canonical_summary,
        legacy_summary=snapshot.legacy_summary,
    )

    assert snapshot.fingerprint == repeated.fingerprint == reordered.fingerprint


def test_inventory_fingerprint_changes_for_meaningful_field_or_provenance_drift() -> None:
    snapshot = _inventory()
    changed_record = snapshot.canonical[0].model_copy(update={"display_name": "changed"})
    provenance_payload = snapshot.canonical[0].provenance.model_dump(mode="python")
    provenance_payload["source_sha256"] = "f" * 64
    changed_provenance = SourceProvenance(**provenance_payload)
    provenance_record = snapshot.canonical[0].model_copy(update={"provenance": changed_provenance})

    changed = MetricInventorySnapshot(
        canonical=(changed_record, *snapshot.canonical[1:]),
        legacy=snapshot.legacy,
        canonical_summary=snapshot.canonical_summary,
        legacy_summary=snapshot.legacy_summary,
    )
    provenance_changed = MetricInventorySnapshot(
        canonical=(provenance_record, *snapshot.canonical[1:]),
        legacy=snapshot.legacy,
        canonical_summary=snapshot.canonical_summary,
        legacy_summary=snapshot.legacy_summary,
    )

    assert changed.fingerprint != snapshot.fingerprint
    assert provenance_changed.fingerprint != snapshot.fingerprint


def test_canonical_mapping_field_order_does_not_change_definition_fingerprint() -> None:
    document = yaml.safe_load((CANONICAL_ROOT / "complaint.yaml").read_text(encoding="utf-8"))
    metric = document["metrics"][0]
    reordered = dict(reversed(tuple(metric.items())))
    original_record = adapt_canonical_yaml_document(
        {"metrics": [metric]}, source_file="probe.yaml", source_sha256="a" * 64,
    )[0]
    reordered_record = adapt_canonical_yaml_document(
        {"metrics": [reordered]}, source_file="probe.yaml", source_sha256="a" * 64,
    )[0]

    assert reordered_record.definition_sha256 == original_record.definition_sha256


def test_canonical_duplicate_empty_identity_and_unknown_shape_fail_closed() -> None:
    document = yaml.safe_load((CANONICAL_ROOT / "complaint.yaml").read_text(encoding="utf-8"))
    first = document["metrics"][0]

    with pytest.raises(InventoryValidationError, match="duplicate canonical metric identity"):
        adapt_canonical_yaml_document(
            {"metrics": [*document["metrics"], first]},
            source_file="duplicate.yaml",
            source_sha256="b" * 64,
        )

    empty = dict(first)
    empty["name"] = ""
    with pytest.raises(InventoryValidationError, match="canonical metric name must be nonempty"):
        adapt_canonical_yaml_document(
            {"metrics": [empty]}, source_file="empty.yaml", source_sha256="c" * 64,
        )

    unknown = dict(first)
    unknown["not_authoritative"] = True
    with pytest.raises(InventoryValidationError, match="unknown canonical metric fields"):
        adapt_canonical_yaml_document(
            {"metrics": [unknown]}, source_file="unknown.yaml", source_sha256="d" * 64,
        )

    with pytest.raises(InventoryValidationError, match="unknown canonical document shape"):
        adapt_canonical_yaml_document(
            {"metrics": document["metrics"], "extra": []},
            source_file="unknown-document.yaml",
            source_sha256="e" * 64,
        )


def test_legacy_duplicate_and_unknown_shape_fail_closed() -> None:
    text = LEGACY_PATH.read_text(encoding="utf-8")
    snapshot = _inventory()

    duplicate = text.replace(
        "- **metric key（区县）**：`installation_same_day_rate_area_day`",
        "- **metric key（区县）**：`installation_same_day_rate_area_day`\n"
        "- **metric key（重复）**：`installation_same_day_rate_area_day`",
        1,
    )
    with pytest.raises(InventoryValidationError, match="duplicate legacy metric declaration"):
        adapt_legacy_semantic_text(
            duplicate,
            canonical_keys=snapshot.canonical_keys,
            enforce_expected_counts=False,
        )

    unknown_shape = text.replace(
        "`installation_in_transit_count_day/month`",
        "`unknown_alias/day`",
        1,
    )
    with pytest.raises(InventoryValidationError, match="unknown legacy slash key shape"):
        adapt_legacy_semantic_text(
            unknown_shape,
            canonical_keys=snapshot.canonical_keys,
            enforce_expected_counts=False,
        )


def test_cross_source_collision_is_rejected_instead_of_deduplicated() -> None:
    snapshot = _inventory()
    colliding = snapshot.legacy[0].model_copy(update={"metric_key": next(iter(snapshot.canonical_keys))})

    with pytest.raises(ValidationError, match="cross-source metric identity collision"):
        MetricInventorySnapshot(
            canonical=snapshot.canonical,
            legacy=(colliding, *snapshot.legacy[1:]),
            canonical_summary=snapshot.canonical_summary,
            legacy_summary=snapshot.legacy_summary,
        )


def test_records_are_strict_and_frozen() -> None:
    snapshot = _inventory()

    with pytest.raises(ValidationError):
        snapshot.canonical[0].__class__(
            metric_key=1,
            display_name="invalid",
            metric_type="raw",
            provenance=snapshot.canonical[0].provenance,
            definition_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        snapshot.canonical = ()  # type: ignore[misc]
