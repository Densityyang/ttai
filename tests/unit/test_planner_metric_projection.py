"""Focused P1 tests for the Planner-safe metric projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.nl2sql.semantic.metric_inventory import build_metric_inventory
from src.nl2sql.semantic.planner_metric_projection import (
    CurrentStateSnapshot,
    ExplicitMetricState,
    PlannerMetricProjection,
    ProjectionValidationError,
    project_metric_inventory,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v4_p1" / "authoritative"
EXPECTED_PROJECTION_FINGERPRINT = "eb082b113fca3bd1596a002dd7003291b65f6c3e8570a73dc084f45af6c77157"


def _inventory():
    return build_metric_inventory(FIXTURE_ROOT / "canonical", FIXTURE_ROOT / "legacy" / "semantic.md")


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = {str(key).lower() for key in value}
        for item in value.values():
            result.update(_keys(item))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_keys(item))
        return result
    return set()


def test_projection_is_stable_ordered_and_planner_safe() -> None:
    projection = project_metric_inventory(_inventory())
    payload = projection.model_dump(mode="json")
    forbidden = {
        "formula",
        "sql",
        "physical",
        "relation",
        "table",
        "tables",
        "column",
        "columns",
        "db_id",
        "owner",
        "approver",
        "permission",
        "permissions",
        "raw_payload",
        "legacy_opaque",
        "expression",
    }

    assert len(projection.metrics) == 294
    assert [item.metric_key for item in projection.metrics] == sorted(item.metric_key for item in projection.metrics)
    assert {item.provenance for item in projection.metrics} == {"canonical_gold", "legacy_semantic"}
    assert sum(item.provenance == "canonical_gold" for item in projection.metrics) == 280
    assert sum(item.provenance == "legacy_semantic" for item in projection.metrics) == 14
    assert all(item.source_state == "UNSPECIFIED" and item.state is None for item in projection.metrics)
    assert not forbidden.intersection(_keys(payload))
    assert projection.fingerprint == EXPECTED_PROJECTION_FINGERPRINT


def test_current_state_marker_requires_explicit_independent_input() -> None:
    snapshot = _inventory()
    current = CurrentStateSnapshot(
        source_id="fixture.current-state.v1",
        source_sha256="1" * 64,
        records=(
            ExplicitMetricState(metric_key=snapshot.canonical[0].metric_key, state="active"),
            ExplicitMetricState(metric_key=snapshot.legacy[0].metric_key, state="inactive"),
        ),
    )
    projection = project_metric_inventory(snapshot, current_state=current)
    by_key = {item.metric_key: item for item in projection.metrics}

    assert by_key[snapshot.canonical[0].metric_key].source_state == "CURRENT_STATE"
    assert by_key[snapshot.canonical[0].metric_key].state == "active"
    assert by_key[snapshot.legacy[0].metric_key].source_state == "CURRENT_STATE"
    assert by_key[snapshot.legacy[0].metric_key].state == "inactive"
    assert sum(item.source_state == "CURRENT_STATE" for item in projection.metrics) == 2
    assert all(
        item.source_state == "UNSPECIFIED"
        for item in projection.metrics
        if item.metric_key not in {snapshot.canonical[0].metric_key, snapshot.legacy[0].metric_key}
    )


def test_current_state_unknown_metric_and_duplicates_fail_closed() -> None:
    snapshot = _inventory()
    with pytest.raises(ProjectionValidationError, match="unknown metric"):
        project_metric_inventory(
            snapshot,
            current_state=CurrentStateSnapshot(
                source_id="fixture.current-state.v1",
                source_sha256="2" * 64,
                records=(ExplicitMetricState(metric_key="not_in_inventory", state="active"),),
            ),
        )
    with pytest.raises(ValueError, match="duplicate explicit current-state"):
        CurrentStateSnapshot(
            source_id="fixture.current-state.v1",
            source_sha256="3" * 64,
            records=(
                ExplicitMetricState(metric_key=snapshot.canonical[0].metric_key, state="active"),
                ExplicitMetricState(metric_key=snapshot.canonical[0].metric_key, state="retired"),
            ),
        )


def test_projection_accepts_only_a_verified_snapshot() -> None:
    with pytest.raises(TypeError, match="MetricInventorySnapshot"):
        PlannerMetricProjection.from_snapshot({"metrics": []})  # type: ignore[arg-type]


def test_projection_fingerprint_changes_for_inventory_or_explicit_state_drift() -> None:
    snapshot = _inventory()
    baseline = project_metric_inventory(snapshot)
    changed_record = snapshot.canonical[0].model_copy(update={"display_name": "changed"})
    changed_snapshot = snapshot.__class__(
        canonical=(changed_record, *snapshot.canonical[1:]),
        legacy=snapshot.legacy,
        canonical_summary=snapshot.canonical_summary,
        legacy_summary=snapshot.legacy_summary,
    )
    state = CurrentStateSnapshot(
        source_id="fixture.current-state.v1",
        source_sha256="4" * 64,
        records=(ExplicitMetricState(metric_key=snapshot.canonical[0].metric_key, state="active"),),
    )

    assert project_metric_inventory(changed_snapshot).fingerprint != baseline.fingerprint
    assert project_metric_inventory(snapshot, current_state=state).fingerprint != baseline.fingerprint
