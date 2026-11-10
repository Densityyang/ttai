from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import ExecutionPlan, FetchMetricStep, VerifyStep
from src.nl2sql.orchestration.metric_query import EligibilityPolicy
from src.nl2sql.semantic.execution_manifest import (
    ExpectedExecutionManifest,
    build_expected_execution_manifest,
)
from src.nl2sql.semantic.metric_contract import Predicate
from src.nl2sql.semantic.schema_snapshot import SchemaSnapshotState
from tests.metric_fixtures import AggregateAuthority, MetricAuthority, ratio_contract

HEX = "a" * 64


def test_manifest_checksum_is_deterministic_and_frozen() -> None:
    manifest = ExpectedExecutionManifest(
        query_plan_sha256=HEX,
        execution_plan_sha256="b" * 64,
        semantic_release_id="release-1",
        schema_snapshot_id="snapshot-1",
        policy_version="plan-compiler.v1",
        metric_contract_sha256=("c" * 64,),
        relation_binding_sha256=("d" * 64,),
        expected_sql_fingerprints=("e" * 64,),
        expected_parameter_binding_fingerprints=("f" * 64,),
    )
    assert manifest.checksum == manifest.checksum
    with pytest.raises(ValidationError):
        manifest.query_plan_sha256 = "bad"  # type: ignore[misc]


def test_manifest_rejects_invalid_authority_hashes() -> None:
    with pytest.raises(ValidationError):
        ExpectedExecutionManifest(
            query_plan_sha256="bad",
            execution_plan_sha256="b" * 64,
            semantic_release_id="release-1",
            schema_snapshot_id="snapshot-1",
            policy_version="plan-compiler.v1",
        )


def test_builder_binds_release_snapshot_policy_and_selected_fallback() -> None:
    authority = AggregateAuthority()
    plan = authority.plan()
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.value",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    manifest = build_expected_execution_manifest(
        query_plan=plan,
        execution_plan=execution_plan,
        context=authority.context,
        metric_contracts=(authority.metric,),
        relation_bindings=(authority.binding, authority.aggregate_binding),
        eligibility_policies=(EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),),
        schema_snapshot=authority.snapshot,
        semantic_release=authority.release,
        actual_source_kind="approved_detail",
        source_step_id="fetch",
    )
    assert manifest.strategy == "approved_detail"
    assert manifest.source_kind == ("approved_detail",)
    assert manifest.relation_identity == ("ai_views.complaint_orders",)
    assert manifest.metric_units == ("count",)
    assert len(manifest.relation_column_authority) == 1
    assert manifest.relation_column_authority[0].relation_id == "ai_views.complaint_orders"
    assert "acceptance_time" in manifest.relation_column_authority[0].columns
    assert manifest.manifest_version == "1.1"
    assert manifest.fetch_step_id == "fetch"
    assert manifest.metric_authorities[0].metric_key == authority.metric.metric_key
    assert manifest.metric_authorities[0].fetch_query_plan_sha256 == plan.checksum
    with pytest.raises(ValidationError, match="complete per-fetch authority"):
        ExpectedExecutionManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "schema_snapshot_checksum": None,
            }
        )


@pytest.mark.parametrize(
    "snapshot_state",
    (SchemaSnapshotState.CANDIDATE, SchemaSnapshotState.RETIRED),
)
def test_manifest_builder_rejects_unvalidated_or_retired_snapshot(
    snapshot_state: SchemaSnapshotState,
) -> None:
    authority = AggregateAuthority()
    plan = authority.plan()
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.value",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    snapshot = authority.snapshot
    assert snapshot is not None
    invalid_snapshot = replace(snapshot, state=snapshot_state)
    with pytest.raises(ValueError, match="release or snapshot authority mismatch"):
        build_expected_execution_manifest(
            query_plan=plan,
            execution_plan=execution_plan,
            context=authority.context,
            metric_contracts=(authority.metric,),
            relation_bindings=(authority.binding,),
            eligibility_policies=(EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),),
            schema_snapshot=invalid_snapshot,
            semantic_release=authority.release,
            actual_source_kind="approved_detail",
            source_step_id="fetch",
        )


def test_manifest_builder_rejects_release_snapshot_drift_and_wrong_fetch_owner() -> None:
    authority = AggregateAuthority()
    plan = authority.plan()
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.value",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    release = authority.release
    assert release is not None
    for changed_release in (
        replace(release, schema_snapshot_id="other-snapshot"),
        replace(release, schema_snapshot_checksum="f" * 64),
    ):
        with pytest.raises(ValueError, match="release or snapshot authority mismatch"):
            build_expected_execution_manifest(
                query_plan=plan,
                execution_plan=execution_plan,
                context=authority.context,
                metric_contracts=(authority.metric,),
                relation_bindings=(authority.binding,),
                eligibility_policies=(EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),),
                schema_snapshot=authority.snapshot,
                semantic_release=changed_release,
                actual_source_kind="approved_detail",
                source_step_id="fetch",
            )
    with pytest.raises(ValueError, match="not bound to one requested metric"):
        build_expected_execution_manifest(
            query_plan=plan,
            execution_plan=execution_plan,
            context=authority.context,
            metric_contracts=(authority.metric,),
            relation_bindings=(authority.binding,),
            eligibility_policies=(EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id),),
            schema_snapshot=authority.snapshot,
            semantic_release=release,
            actual_source_kind="approved_detail",
            source_step_id="unknown_fetch",
        )


def test_ratio_manifest_carries_complete_predicate_authority() -> None:
    authority = MetricAuthority(metric=ratio_contract())
    plan = authority.plan()
    eligibility_policy = EligibilityPolicy(
        policy_id=authority.metric.eligibility_policy_id,
        predicates=(Predicate(field="priority", operator="is_not_null"),),
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=authority.context.semantic_release_id,
        schema_snapshot_id=authority.context.schema_snapshot_id,
        policy_version="plan-compiler.v1",
        steps=(
            FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
            VerifyStep(
                step_id="verify",
                input_refs=("fetch.value",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch",),
            ),
        ),
    )
    manifest = build_expected_execution_manifest(
        query_plan=plan,
        execution_plan=execution_plan,
        context=authority.context,
        metric_contracts=(authority.metric,),
        relation_bindings=(authority.binding,),
        eligibility_policies=(eligibility_policy,),
        schema_snapshot=authority.snapshot,
        semantic_release=authority.release,
        actual_source_kind="approved_detail",
        source_step_id="fetch",
    )
    assert manifest.ratio_denominator_predicates == tuple(
        '{"field":"' + predicate.field + '","operator":"' + predicate.operator + '"}'
        for predicate in authority.metric.ratio.denominator_authority  # type: ignore[union-attr]
    )
    assert manifest.ratio_numerator_predicates == tuple(
        '{"field":"' + predicate.field + '","operator":"' + predicate.operator + '"}'
        for predicate in authority.metric.ratio.numerator_authority  # type: ignore[union-attr]
    )
    assert manifest.metric_units == ("percent",)
    role_authority = tuple(json.loads(item) for item in manifest.predicate_authority)
    assert any(item["role"] == "metric_base" for item in role_authority)
    assert any(item["role"] == "eligibility" for item in role_authority)
    assert any(item["role"] == "ratio_denominator" for item in role_authority)
    assert any(item["role"] == "ratio_numerator_condition" for item in role_authority)

    # The active semantic release independently owns the complete ratio
    # definition; a mutated copy must fail at that boundary before execution.
    ratio = authority.metric.ratio
    assert ratio is not None
    corrupt_ratio = ratio.model_copy(
        update={
            "numerator_predicates": (
                *ratio.numerator_predicates,
                ratio.denominator_predicates[0],
            )
        }
    )
    corrupt_metric = authority.metric.model_copy(update={"ratio": corrupt_ratio})
    with pytest.raises(ValueError, match="metric contract does not match semantic release"):
        build_expected_execution_manifest(
            query_plan=plan,
            execution_plan=execution_plan,
            context=authority.context,
            metric_contracts=(corrupt_metric,),
            relation_bindings=(authority.binding,),
            eligibility_policies=(eligibility_policy,),
            schema_snapshot=authority.snapshot,
            semantic_release=authority.release,
            actual_source_kind="approved_detail",
            source_step_id="fetch",
        )
