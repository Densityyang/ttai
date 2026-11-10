from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import ExecutionPlan, FetchMetricStep, VerifyStep
from src.nl2sql.orchestration.metric_query import EligibilityPolicy
from src.nl2sql.semantic.execution_manifest import (
    ExpectedExecutionManifest,
    build_expected_execution_manifest,
)
from tests.metric_fixtures import AggregateAuthority

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
    )
    assert manifest.strategy == "approved_detail"
    assert manifest.source_kind == ("approved_detail",)
    assert manifest.relation_identity == ("ai_views.complaint_orders",)
