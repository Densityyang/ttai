"""Focused P1 tests for the canonical dependency graph and typed mappings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.nl2sql.semantic.metric_inventory import (
    EXPECTED_CANONICAL_CATEGORY_METRIC_COUNT,
    EXPECTED_CANONICAL_DEPENDENCY_EDGE_COUNT,
    EXPECTED_CANONICAL_DEPENDENCY_SOURCE_COUNT,
    InventoryValidationError,
    MetricCategory,
    MetricDependency,
    MetricInventoryRecord,
    MetricInventorySnapshot,
    adapt_canonical_yaml_document,
    analyze_canonical_dependencies,
    build_canonical_dependency_graph,
    build_metric_inventory,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v4_p1" / "authoritative"
CANONICAL_ROOT = FIXTURE_ROOT / "canonical"
LEGACY_PATH = FIXTURE_ROOT / "legacy" / "semantic.md"


def _metric(
    name: str,
    *,
    source_type: str = "derived",
    dependencies: dict[str, str] | None = None,
    dimensions: list[dict[str, object]] | None = None,
    time_grain: object = "day",
) -> dict[str, object]:
    metric: dict[str, object] = {
        "name": name,
        "display_name": name,
        "description": f"{name} description",
        "source_type": source_type,
        "calculation_type": "ratio",
        "domain": "complaint",
        "tags": ["p1"],
        "time_grain": time_grain,
        "value_type": "count",
        "unit": "件",
    }
    if dependencies is not None:
        metric["dependencies"] = dependencies
    if dimensions is not None:
        metric["dimensions"] = dimensions
    return metric


def _records(document: dict[str, object]) -> tuple[MetricInventoryRecord, ...]:
    return adapt_canonical_yaml_document(
        document, source_file="probe.yaml", source_sha256="a" * 64
    )


def test_authoritative_canonical_dependency_graph_is_a_valid_dag() -> None:
    snapshot = build_metric_inventory(CANONICAL_ROOT, LEGACY_PATH)
    graph = snapshot.dependency_graph

    assert len(graph.edges) == EXPECTED_CANONICAL_DEPENDENCY_EDGE_COUNT == 113
    assert set(graph.topological_order) == set(snapshot.canonical_keys)
    assert len(graph.topological_order) == len(snapshot.canonical_keys) == 280
    position = {key: index for index, key in enumerate(graph.topological_order)}
    for edge in graph.edges:
        assert edge.source in snapshot.canonical_keys
        assert edge.target in snapshot.canonical_keys
        assert position[edge.target] < position[edge.source]
    sources = {edge.source for edge in graph.edges}
    assert len(sources) == EXPECTED_CANONICAL_DEPENDENCY_SOURCE_COUNT == 56
    assert not sources.intersection(item.metric_key for item in snapshot.legacy)


def test_unknown_dependency_target_is_rejected() -> None:
    records = _records({"metrics": [_metric("alpha", dependencies={"x": "missing_metric"}), _metric("beta")]})
    issues = analyze_canonical_dependencies(records)

    assert [issue.code for issue in issues] == ["unknown_metric_dependency"]
    assert issues[0].metric_key == "alpha"
    assert issues[0].label == "x"
    assert issues[0].target == "missing_metric"
    with pytest.raises(InventoryValidationError, match="invalid canonical dependency graph"):
        build_canonical_dependency_graph(records)


def test_self_dependency_is_rejected() -> None:
    records = _records({"metrics": [_metric("alpha", dependencies={"self": "alpha"})]})
    issues = analyze_canonical_dependencies(records)

    assert [issue.code for issue in issues] == ["self_metric_dependency"]
    assert issues[0].metric_key == "alpha"
    assert issues[0].cycle == ()
    with pytest.raises(InventoryValidationError, match="refers to itself"):
        build_canonical_dependency_graph(records)


def test_dependency_cycle_is_rejected_and_reported_canonically() -> None:
    records = _records(
        {
            "metrics": [
                _metric("alpha", dependencies={"next": "beta"}),
                _metric("beta", dependencies={"next": "gamma"}),
                _metric("gamma", dependencies={"next": "alpha"}),
            ]
        }
    )
    issues = analyze_canonical_dependencies(records)

    assert [issue.code for issue in issues] == ["metric_dependency_cycle"]
    assert issues[0].metric_key == "alpha"
    assert issues[0].cycle == ("alpha", "beta", "gamma", "alpha")
    with pytest.raises(InventoryValidationError, match="metric dependency cycle detected"):
        build_canonical_dependency_graph(records)


def test_issue_list_is_deterministic_and_record_order_independent() -> None:
    metrics = [
        _metric("alpha", dependencies={"next": "beta"}),
        _metric("beta", dependencies={"next": "alpha", "ghost": "nowhere"}),
        _metric("gamma", dependencies={"self": "gamma"}),
        _metric("delta"),
    ]
    forward = analyze_canonical_dependencies(_records({"metrics": metrics}))
    backward = analyze_canonical_dependencies(_records({"metrics": list(reversed(metrics))}))
    shuffled = analyze_canonical_dependencies(
        _records({"metrics": [metrics[2], metrics[0], metrics[3], metrics[1]]})
    )

    assert forward == backward == shuffled
    assert [(issue.code, issue.metric_key) for issue in forward] == [
        ("metric_dependency_cycle", "alpha"),
        ("self_metric_dependency", "gamma"),
        ("unknown_metric_dependency", "beta"),
    ]


def test_legacy_only_records_do_not_participate_in_the_canonical_graph() -> None:
    snapshot = build_metric_inventory(CANONICAL_ROOT, LEGACY_PATH)
    graph = snapshot.dependency_graph
    graph_keys = {edge.source for edge in graph.edges} | {edge.target for edge in graph.edges}
    legacy_keys = {item.metric_key for item in snapshot.legacy}

    assert not graph_keys.intersection(legacy_keys)
    assert not set(graph.topological_order).intersection(legacy_keys)

    legacy = snapshot.legacy[0]
    payload = legacy.model_dump(mode="python")
    payload["dependencies"] = (MetricDependency(label="x", target=legacy.metric_key),)
    with pytest.raises(ValidationError, match="legacy-only records cannot carry canonical dependency data"):
        MetricInventoryRecord(**payload)


def test_category_and_time_mapping_is_typed_and_explicit() -> None:
    records = _records(
        {
            "metrics": [
                _metric(
                    "alpha",
                    dimensions=[
                        {"name": "area_id", "type": "area"},
                        {"name": "category_code", "type": "category", "required": True},
                    ],
                    time_grain="month",
                )
            ]
        }
    )
    record = records[0]

    assert record.categories == (MetricCategory(name="category_code", required=True),)
    assert record.categories[0].dimension_type == "category"
    assert record.time_grains == ("month",)
    plain = _records({"metrics": [_metric("beta")]})[0]
    assert plain.categories == ()
    assert plain.time_grains == ("day",)


def test_empty_duplicate_and_unknown_category_or_time_shapes_fail_closed() -> None:
    with pytest.raises(InventoryValidationError, match="dimension name for alpha must be nonempty"):
        _records({"metrics": [_metric("alpha", dimensions=[{"name": "", "type": "category"}])]})
    with pytest.raises(InventoryValidationError, match="duplicate dimension identity for alpha"):
        _records(
            {
                "metrics": [
                    _metric("alpha", dimensions=[
                        {"name": "category_code", "type": "category"},
                        {"name": "category_code", "type": "category"},
                    ])
                ]
            }
        )
    with pytest.raises(InventoryValidationError, match="unknown dimension shape for alpha"):
        _records(
            {
                "metrics": [
                    _metric("alpha", dimensions=[
                        {"name": "category_code", "type": "category", "invented": True}
                    ])
                ]
            }
        )
    with pytest.raises(InventoryValidationError, match="time_grain for alpha must be a string"):
        _records({"metrics": [_metric("alpha", time_grain=2026)]})
    with pytest.raises(InventoryValidationError, match="time_grain for alpha must be nonempty"):
        _records({"metrics": [_metric("alpha", time_grain="   ")]})


def test_category_projection_must_match_declared_dimensions() -> None:
    record = _records(
        {"metrics": [_metric("alpha", dimensions=[{"name": "category_code", "type": "category", "required": True}])]}
    )[0]
    payload = record.model_dump(mode="python")
    payload["categories"] = (MetricCategory(name="category_code", required=False),)
    with pytest.raises(ValidationError, match="category projection must match declared category dimensions"):
        MetricInventoryRecord(**payload)


def test_canonical_category_metric_count_is_locked() -> None:
    snapshot = build_metric_inventory(CANONICAL_ROOT, LEGACY_PATH)
    with_category = [item for item in snapshot.canonical if item.categories]

    assert len(with_category) == EXPECTED_CANONICAL_CATEGORY_METRIC_COUNT == 44
    assert {category.name for item in with_category for category in item.categories} == {"category_code"}


def test_inventory_fingerprint_changes_when_a_dependency_changes() -> None:
    snapshot = build_metric_inventory(CANONICAL_ROOT, LEGACY_PATH)
    baseline = snapshot.fingerprint
    target = next(item for item in snapshot.canonical if item.dependencies)
    raw_target = next(item.metric_key for item in snapshot.canonical if item.metric_type == "raw")
    changed_dependency = MetricDependency(
        label=target.dependencies[0].label, target=raw_target
    )
    changed_record = target.model_copy(update={"dependencies": (changed_dependency,)})
    changed = MetricInventorySnapshot(
        canonical=tuple(
            changed_record if item.metric_key == target.metric_key else item
            for item in snapshot.canonical
        ),
        legacy=snapshot.legacy,
        canonical_summary=snapshot.canonical_summary,
        legacy_summary=snapshot.legacy_summary,
    )

    assert changed.fingerprint != baseline
