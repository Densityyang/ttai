"""Focused P1 slice 4 tests for the inventory release bridge."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from src.nl2sql.semantic.authoring import (
    AssetStatus,
    AuthoringIR,
    MetricAsset,
    ViewAsset,
    validate_authoring_ir,
)
from src.nl2sql.semantic.inventory_release import (
    InventoryReleaseError,
    MetricIdentityIndex,
    MetricResolutionError,
    ResolutionCode,
    bridge_metric_contract,
    build_inventory_authoring_ir,
    build_inventory_release_candidate,
    build_metric_identity_index,
)
from src.nl2sql.semantic.materialization import materialize_authoring_ir
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    MetricContract,
    load_metric_catalog,
)
from src.nl2sql.semantic.metric_inventory import build_metric_inventory
from src.nl2sql.semantic.planner_metric_projection import (
    CurrentStateSnapshot,
    ExplicitMetricLifecycle,
    ExplicitMetricState,
    LifecycleSnapshot,
)
from src.nl2sql.semantic.registry import (
    SemanticRegistry,
    SemanticReleaseError,
    SemanticReleaseState,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v4_p1" / "authoritative"
REPO_ROOT = Path(__file__).resolve().parents[2]
OLD_CONTRACT_PATH = REPO_ROOT / "config" / "metrics" / "complaint.yaml"

RELATION = "ai_views.complaint_orders"
RELATION_COLUMNS = {
    RELATION: (
        "acceptance_time",
        "completion_time",
        "has_valid_bandwidth",
        "is_first_response_on_time",
        "is_valid_for_metrics",
    )
}


def _inventory():
    return build_metric_inventory(FIXTURE_ROOT / "canonical", FIXTURE_ROOT / "legacy" / "semantic.md")


def _legal_candidate():
    """A complete, executable synthetic release that is not from the inventory."""

    view = ViewAsset(
        asset_id="view.complaint_orders_view",
        name="complaint_orders_view",
        source_relation=RELATION,
        source_alias="co",
        columns=("complaint_total_count_overall_day",),
        owner="synthetic-owner",
        sensitivity="internal",
        freshness_sla_seconds=86_400,
    )
    metric = MetricAsset(
        asset_id="metric.complaint_in_transit_count",
        metric_key="complaint_in_transit_count",
        display_name="complaint in transit",
        source_relation=RELATION,
        status=AssetStatus.ACTIVE,
        domain="complaint",
        owner="synthetic-owner",
        sensitivity="internal",
        freshness_sla_seconds=86_400,
    )
    ir = AuthoringIR(metrics=(metric,), views=(view,))
    report = validate_authoring_ir(ir)
    assert report.ok, report.to_dict()
    return materialize_authoring_ir(ir, report)


def _issue_payload(report) -> list[dict[str, str]]:
    return [issue.to_dict() for issue in report.issues]


def test_exact_resolution_resolves_canonical_and_legacy_namespaces() -> None:
    snapshot = _inventory()
    index = build_metric_identity_index(snapshot)
    canonical_key = snapshot.canonical[0].metric_key
    legacy_key = snapshot.legacy[0].metric_key

    canonical = index.resolve(canonical_key)
    legacy = index.resolve(legacy_key)

    assert canonical.namespace == "canonical_gold"
    assert canonical.legacy_only is False
    assert canonical.is_canonical is True
    assert canonical.provenance == snapshot.canonical[0].provenance
    assert canonical.definition_sha256 == snapshot.canonical[0].definition_sha256
    assert canonical.lifecycle is None and canonical.lifecycle_state == "UNSPECIFIED"
    assert legacy.namespace == "legacy_semantic"
    assert legacy.legacy_only is True
    assert legacy.is_canonical is False
    assert legacy.provenance.source_kind == "legacy_semantic"


def test_exact_resolution_rejects_unknown_and_non_exact_key_shapes() -> None:
    snapshot = _inventory()
    index = build_metric_identity_index(snapshot)

    unknown = index.try_resolve("not_a_real_metric_identity")
    assert unknown.code is ResolutionCode.UNKNOWN
    assert unknown.canonical_match is False and unknown.legacy_match is False

    # The legacy compact declaration is a source alias, never an exact identity,
    # and it must not be mapped implicitly to either time grain.
    compact = index.try_resolve("installation_in_transit_count_day/month")
    assert compact.code is ResolutionCode.UNKNOWN
    assert index.resolve("installation_in_transit_count_day").namespace == "canonical_gold"
    assert index.resolve("installation_in_transit_count_month").namespace == "canonical_gold"

    approximate = index.try_resolve("installation_in_transit_count_da")
    assert approximate.code is ResolutionCode.UNKNOWN
    padded = index.try_resolve(" installation_in_transit_count_day")
    assert padded.code is ResolutionCode.UNKNOWN
    assert index.try_resolve(" ").code is ResolutionCode.INVALID
    assert index.try_resolve("").code is ResolutionCode.INVALID
    assert index.try_resolve(1234).code is ResolutionCode.INVALID


def test_ambiguous_namespace_resolution_fails_closed() -> None:
    snapshot = _inventory()
    canonical = snapshot.canonical[0]
    legacy = snapshot.legacy[0].model_copy(update={"metric_key": canonical.metric_key})
    index = MetricIdentityIndex.from_namespaces((canonical,), (legacy,))

    with pytest.raises(MetricResolutionError) as error:
        index.resolve(canonical.metric_key)

    failure = error.value.failure
    assert failure.code is ResolutionCode.AMBIGUOUS
    assert failure.canonical_match is True and failure.legacy_match is True


def test_retired_lifecycle_fails_closed_while_inactive_is_recorded() -> None:
    snapshot = _inventory()
    retired_key = snapshot.canonical[0].metric_key
    inactive_key = snapshot.legacy[0].metric_key
    lifecycle = LifecycleSnapshot(
        source_id="probe.lifecycle.v1",
        source_sha256="a" * 64,
        records=(
            ExplicitMetricLifecycle(metric_key=retired_key, lifecycle="retired"),
            ExplicitMetricLifecycle(metric_key=inactive_key, lifecycle="inactive"),
        ),
    )
    index = build_metric_identity_index(snapshot, lifecycle=lifecycle)

    with pytest.raises(MetricResolutionError) as error:
        index.resolve(retired_key)
    assert error.value.failure.code is ResolutionCode.RETIRED
    assert error.value.failure.lifecycle == "retired"

    inactive = index.resolve(inactive_key)
    assert inactive.lifecycle == "inactive"
    assert inactive.lifecycle_state == "CURRENT_STATE"

    with pytest.raises(InventoryReleaseError, match="unknown metric"):
        build_metric_identity_index(
            snapshot,
            lifecycle=LifecycleSnapshot(
                source_id="probe.lifecycle.v2",
                source_sha256="b" * 64,
                records=(ExplicitMetricLifecycle(metric_key="ghost_metric", lifecycle="active"),),
            ),
        )

    conflicting = CurrentStateSnapshot(
        source_id="probe.current-state.v1",
        source_sha256="c" * 64,
        records=(ExplicitMetricState(metric_key=retired_key, state="active"),),
    )
    with pytest.raises(InventoryReleaseError, match="contradictory explicit lifecycle"):
        build_metric_identity_index(snapshot, lifecycle=lifecycle, current_state=conflicting)


def test_candidate_release_is_deterministic_and_never_marked_active() -> None:
    first = build_inventory_release_candidate(_inventory())
    second = build_inventory_release_candidate(_inventory())

    assert first.release_checksum == second.release_checksum
    assert first.authoring_checksum == second.authoring_checksum
    assert first.candidate.checksum == first.release_checksum
    assert first.candidate == second.candidate
    assert _issue_payload(first.report) == _issue_payload(second.report)
    assert first.report.ok is False
    assert first.is_activatable is False
    assert first.materializable is False
    assert first.blocked_reason is not None
    assert len(first.metric_keys) == 294
    assert len(first.materialized_metric_keys) == 288
    assert {asset.asset_id for asset in first.candidate.assets} == {
        f"metric.{key}" for key in first.materialized_metric_keys
    }
    assert all(asset.status == "error" for asset in first.candidate.assets)
    assert len(first.candidate.documents) == 288


def test_candidate_is_one_unmodified_materialize_output() -> None:
    candidate = build_inventory_release_candidate(_inventory())

    rebuilt = materialize_authoring_ir(candidate.ir, candidate.report)

    assert rebuilt == candidate.candidate
    assert rebuilt.checksum == candidate.release_checksum
    assert candidate.release_checksum == candidate.candidate.checksum
    assert candidate.release_checksum != candidate.authoring_checksum


def test_candidate_reports_every_missing_authoring_binding() -> None:
    snapshot = _inventory()
    candidate = build_inventory_release_candidate(snapshot)
    gaps: dict[str, set[str]] = defaultdict(set)
    for gap in candidate.binding_gaps:
        gaps[gap.metric_key].add(gap.field)

    assert all("source_relation" in gaps[key] for key in candidate.metric_keys)
    assert all("owner" in gaps[key] for key in candidate.metric_keys)
    derived = {
        record.metric_key
        for record in snapshot.canonical
        if record.metric_type == "derived"
    }
    assert derived and all("formula" in gaps[key] for key in derived)
    codes = {issue.code for issue in candidate.report.issues}
    assert {"missing_source_relation", "missing_formula", "missing_owner"} <= codes
    assert "inventory_lifecycle_unspecified" in codes

    ir = build_inventory_authoring_ir(snapshot)
    assert len(ir.metrics) == 294
    assert all(metric.source_relation is None for metric in ir.metrics)
    assert all(metric.formula is None for metric in ir.metrics)
    assert all(metric.owner is None for metric in ir.metrics)
    assert all(metric.status is not AssetStatus.ACTIVE for metric in ir.metrics)


def test_candidate_exposes_alias_collisions_and_excludes_both_members() -> None:
    snapshot = _inventory()
    candidate = build_inventory_release_candidate(snapshot)
    expected_pairs = {
        "complaint_total_count_overall_day": "fault_reporting_total_count_overall_day",
        "complaint_calc_total_count_overall_day": (
            "fault_reporting_calc_total_count_overall_day"
        ),
        "complaint_calc_total_count_team_day": "fault_reporting_calc_total_count_team_day",
    }
    expected_excluded = set(expected_pairs) | set(expected_pairs.values())
    peers = {
        member: peer
        for left, right in expected_pairs.items()
        for member, peer in ((left, right), (right, left))
    }

    assert candidate.materializable is False
    assert len(candidate.alias_collisions) == 3
    for collision in candidate.alias_collisions:
        keys = {member.metric_key for member in collision.members}
        assert len(keys) == 2
        assert keys in [{left, right} for left, right in expected_pairs.items()]
        assert collision.normalized_label
        assert collision.display_name
        assert (
            collision.decision_required
            == "source_display_name_disambiguation_or_reviewed_materializer_change"
        )
        assert collision.message
        for member in collision.members:
            assert member.source_file in {"complaint.yaml", "repair_service.yaml"}

    excluded = {item.metric_key: item for item in candidate.excluded_identities}
    assert set(excluded) == expected_excluded
    for key, item in excluded.items():
        assert item.reason == "display_alias_collision"
        assert item.source_file in {"complaint.yaml", "repair_service.yaml"}
        assert item.peer_metric_keys == (peers[key],)
        assert item.collision_label

    materialized = set(candidate.materialized_metric_keys)
    assert len(materialized) == 288
    assert materialized.isdisjoint(excluded)
    assert len(materialized) + len(excluded) == 294
    assert set(excluded) == set(candidate.metric_keys) - materialized

    pruned = candidate.pruned_dependency_edges
    assert len(pruned) == 8
    assert all(edge.target_metric_key in excluded for edge in pruned)
    assert all(edge.source_metric_key in materialized for edge in pruned)
    assert all(edge.reason == "target_excluded_by_alias_collision" for edge in pruned)
    dependency_edges = [
        edge for edge in candidate.candidate.edges if edge.edge_type == "metric_dependency"
    ]
    assert len(dependency_edges) == 113 - len(pruned)


def test_candidate_creates_draft_without_activating() -> None:
    candidate = build_inventory_release_candidate(_inventory())
    registry = SemanticRegistry()

    release = candidate.create_draft(registry, change_summary="inventory candidate")

    assert release.state is SemanticReleaseState.DRAFT
    assert registry.active_release_id is None
    assert registry.active_release() is None
    with pytest.raises(SemanticReleaseError):
        candidate.submit(registry, change_summary="inventory candidate")
    assert registry.active_release_id is None


def test_old_contract_bridge_walks_through_pending_and_active_contracts() -> None:
    snapshot = _inventory()
    content = OLD_CONTRACT_PATH.read_text(encoding="utf-8")

    pending = bridge_metric_contract(content, relations={}, inventory=snapshot)

    assert pending.report.ok is True
    assert pending.candidate.documents
    assert pending.in_inventory == ()
    assert pending.not_in_inventory == (
        "complaint_in_transit_count",
        "complaint_first_response_rate",
    )
    assert all(match.code == "not_in_inventory" for match in pending.matches)

    canonical_key = snapshot.canonical[0].metric_key
    seed = load_metric_catalog(content).metrics[0]
    payload = seed.model_dump(mode="json")
    payload.update(
        metric_key=canonical_key,
        release_status="active",
        owner="synthetic-owner",
        approver="synthetic-approver",
        freshness_sla_seconds=86_400,
    )
    active_contract = MetricContract.model_validate(payload)
    active_content = MetricCatalog(metrics=(active_contract,)).model_dump_json()

    active = bridge_metric_contract(
        active_content,
        relations={"complaint_orders": RELATION},
        inventory=snapshot,
        relation_columns=RELATION_COLUMNS,
    )

    assert active.report.ok is True
    assert active.in_inventory == (canonical_key,)
    assert active.matches[0].namespace == "canonical_gold"
    assert active.matches[0].legacy_only is False

    with pytest.raises(ValueError, match="active metric source binding missing"):
        bridge_metric_contract(active_content, relations={}, inventory=snapshot)


def test_failed_inventory_candidate_never_moves_the_active_pointer() -> None:
    registry = SemanticRegistry()
    legal = _legal_candidate()
    active = registry.publish(
        legal.documents,
        change_summary="legal executable release",
        validator=lambda _: legal.validation_report,
    )
    assert active.state is SemanticReleaseState.ACTIVE
    assert registry.active_release_id == active.release_id

    candidate = build_inventory_release_candidate(_inventory())
    assert candidate.is_activatable is False

    with pytest.raises(SemanticReleaseError):
        candidate.submit(registry, change_summary="inventory candidate")

    assert registry.active_release_id == active.release_id
    current = registry.active_release()
    assert current is not None
    assert current.state is SemanticReleaseState.ACTIVE
    assert current.release_id == active.release_id


def test_failed_inventory_candidate_never_becomes_validated() -> None:
    registry = SemanticRegistry()
    legal = _legal_candidate()
    active = registry.publish(
        legal.documents,
        change_summary="legal executable release",
        validator=lambda _: legal.validation_report,
    )
    candidate = build_inventory_release_candidate(_inventory())
    draft = candidate.create_draft(registry, change_summary="inventory candidate")

    with pytest.raises(SemanticReleaseError):
        registry.validate(draft.release_id, lambda _: candidate.candidate.validation_report)

    assert registry.get(draft.release_id).state is SemanticReleaseState.DRAFT
    assert registry.active_release_id == active.release_id
