"""Focused P1 tests for the Planner-safe metric projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.nl2sql.semantic.metric_inventory import build_metric_inventory
from src.nl2sql.semantic.planner_metric_projection import (
    CurrentStateSnapshot,
    ExplicitMetricLifecycle,
    ExplicitMetricSourceReadiness,
    ExplicitMetricState,
    LifecycleSnapshot,
    PlannerMetricProjection,
    ProjectionValidationError,
    SourceReadinessSnapshot,
    project_metric_inventory,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v4_p1" / "authoritative"
EXPECTED_PROJECTION_FINGERPRINT = "3c6a43c11fe8deb1218d79b4df7196082fe49ce027b8137a19d5e5858fcecd4d"


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


def test_missing_lifecycle_and_readiness_stay_unspecified() -> None:
    projection = project_metric_inventory(_inventory())

    assert all(item.lifecycle is None for item in projection.metrics)
    assert all(item.source_readiness is None for item in projection.metrics)
    assert all(item.source_readiness_state == "UNSPECIFIED" for item in projection.metrics)
    assert all(item.planning_readiness == "lifecycle_unspecified" for item in projection.metrics)
    assert not any(item.is_planner_ready for item in projection.metrics)


def test_active_lifecycle_with_pending_source_is_expressible_and_not_ready() -> None:
    snapshot = _inventory()
    target = snapshot.canonical[0].metric_key
    lifecycle = LifecycleSnapshot(
        source_id="fixture.lifecycle.v1",
        source_sha256="5" * 64,
        records=(ExplicitMetricLifecycle(metric_key=target, lifecycle="active"),),
    )
    readiness = SourceReadinessSnapshot(
        source_id="fixture.readiness.v1",
        source_sha256="6" * 64,
        records=(ExplicitMetricSourceReadiness(metric_key=target, readiness="pending_source"),),
    )
    projection = project_metric_inventory(snapshot, lifecycle=lifecycle, source_readiness=readiness)
    entry = {item.metric_key: item for item in projection.metrics}[target]

    assert entry.lifecycle == "active"
    assert entry.source_readiness == "pending_source"
    assert entry.planning_readiness == "active_pending_source"
    assert entry.is_planner_ready is False
    assert entry.source_state == "CURRENT_STATE"
    assert entry.source_readiness_state == "SOURCE_READINESS"
    assert projection.lifecycle_fingerprint == lifecycle.fingerprint
    assert projection.source_readiness_fingerprint == readiness.fingerprint
    untouched = {item.metric_key: item for item in projection.metrics}[snapshot.canonical[1].metric_key]
    assert untouched.source_state == "UNSPECIFIED"
    assert untouched.planning_readiness == "lifecycle_unspecified"


def test_only_active_lifecycle_with_ready_source_is_fully_ready() -> None:
    snapshot = _inventory()
    target = snapshot.canonical[0].metric_key
    lifecycle = LifecycleSnapshot(
        source_id="fixture.lifecycle.v1",
        source_sha256="7" * 64,
        records=(ExplicitMetricLifecycle(metric_key=target, lifecycle="active"),),
    )
    ready = SourceReadinessSnapshot(
        source_id="fixture.readiness.v1",
        source_sha256="8" * 64,
        records=(ExplicitMetricSourceReadiness(metric_key=target, readiness="ready"),),
    )
    projection = project_metric_inventory(snapshot, lifecycle=lifecycle, source_readiness=ready)
    entry = {item.metric_key: item for item in projection.metrics}[target]

    assert entry.planning_readiness == "ready"
    assert entry.is_planner_ready is True


def test_readiness_without_lifecycle_never_reports_ready() -> None:
    snapshot = _inventory()
    target = snapshot.canonical[0].metric_key
    readiness = SourceReadinessSnapshot(
        source_id="fixture.readiness.v1",
        source_sha256="9" * 64,
        records=(ExplicitMetricSourceReadiness(metric_key=target, readiness="ready"),),
    )
    projection = project_metric_inventory(snapshot, source_readiness=readiness)
    entry = {item.metric_key: item for item in projection.metrics}[target]

    assert entry.source_readiness == "ready"
    assert entry.planning_readiness == "lifecycle_unspecified"
    assert entry.is_planner_ready is False


def test_explicit_inputs_are_type_checked() -> None:
    snapshot = _inventory()
    with pytest.raises(TypeError, match="lifecycle must be LifecycleSnapshot"):
        project_metric_inventory(snapshot, lifecycle={"records": []})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source_readiness must be SourceReadinessSnapshot"):
        project_metric_inventory(snapshot, source_readiness={"records": []})  # type: ignore[arg-type]


def test_contradictory_and_unknown_explicit_inputs_fail_closed() -> None:
    snapshot = _inventory()
    target = snapshot.canonical[0].metric_key
    current = CurrentStateSnapshot(
        source_id="fixture.current-state.v1",
        source_sha256="a" * 64,
        records=(ExplicitMetricState(metric_key=target, state="active"),),
    )
    lifecycle = LifecycleSnapshot(
        source_id="fixture.lifecycle.v1",
        source_sha256="b" * 64,
        records=(ExplicitMetricLifecycle(metric_key=target, lifecycle="active"),),
    )
    with pytest.raises(ProjectionValidationError, match="contradictory explicit lifecycle inputs"):
        project_metric_inventory(snapshot, current_state=current, lifecycle=lifecycle)

    retired = LifecycleSnapshot(
        source_id="fixture.lifecycle.v1",
        source_sha256="c" * 64,
        records=(ExplicitMetricLifecycle(metric_key=target, lifecycle="retired"),),
    )
    ready = SourceReadinessSnapshot(
        source_id="fixture.readiness.v1",
        source_sha256="d" * 64,
        records=(ExplicitMetricSourceReadiness(metric_key=target, readiness="ready"),),
    )
    with pytest.raises(ProjectionValidationError, match="retired lifecycle cannot be source ready"):
        project_metric_inventory(snapshot, lifecycle=retired, source_readiness=ready)

    with pytest.raises(ProjectionValidationError, match="source readiness references unknown metric"):
        project_metric_inventory(
            snapshot,
            source_readiness=SourceReadinessSnapshot(
                source_id="fixture.readiness.v1",
                source_sha256="e" * 64,
                records=(ExplicitMetricSourceReadiness(metric_key="not_in_inventory", readiness="ready"),),
            ),
        )
    with pytest.raises(ValueError, match="duplicate explicit source readiness"):
        SourceReadinessSnapshot(
            source_id="fixture.readiness.v1",
            source_sha256="f" * 64,
            records=(
                ExplicitMetricSourceReadiness(metric_key=target, readiness="ready"),
                ExplicitMetricSourceReadiness(metric_key=target, readiness="pending_source"),
            ),
        )


def test_projection_exposes_typed_categories_and_dependencies() -> None:
    projection = project_metric_inventory(_inventory())
    with_category = [item for item in projection.metrics if item.categories]
    with_dependency = [item for item in projection.metrics if item.dependencies]

    assert len(with_category) == 44
    assert len(with_dependency) == 56
    assert all(
        category.dimension_type == "category"
        for item in with_category
        for category in item.categories
    )
    assert all(item.provenance == "canonical_gold" for item in with_dependency)
    identity = {entry.metric_key for entry in projection.metrics}
    assert all(
        dependency.target in identity
        for item in with_dependency
        for dependency in item.dependencies
    )


def test_projection_fingerprint_changes_for_readiness_drift() -> None:
    snapshot = _inventory()
    baseline = project_metric_inventory(snapshot)
    readiness = SourceReadinessSnapshot(
        source_id="fixture.readiness.v1",
        source_sha256="1" * 64,
        records=(ExplicitMetricSourceReadiness(metric_key=snapshot.canonical[0].metric_key, readiness="ready"),),
    )

    assert project_metric_inventory(snapshot, source_readiness=readiness).fingerprint != baseline.fingerprint
