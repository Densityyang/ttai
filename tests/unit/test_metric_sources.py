from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.nl2sql.contracts import FetchMetricStep
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.orchestration.execution import PlanStepError
from src.nl2sql.orchestration.metric_query import (
    AggregateColumns,
    EligibilityPolicy,
    GatewayMetricStepRunner,
    SourceFreshnessRecord,
    aggregate_definition_checksum,
)
from src.nl2sql.semantic.metric_contract import Predicate
from src.nl2sql.semantic.schema_snapshot import IndexSnapshot
from tests.metric_fixtures import (
    NOW,
    AggregateAuthority,
    MetricAuthority,
    ratio_contract,
    seed_contract,
)


@pytest.mark.asyncio
async def test_fresh_aggregate_priority_and_source_independent_signature() -> None:
    authority = AggregateAuthority()
    compiler = authority.compiler()
    aggregate = await compiler.compile(authority.plan(), authority.context)
    detail = await compiler.compile(authority.plan(source_strategy="detail_required"), authority.context)
    assert aggregate.source_kind == "approved_aggregate" and detail.source_kind == "approved_detail"
    assert aggregate.semantic_signature == detail.semantic_signature
    assert '"approved_daily_facts"' in aggregate.sql and '"complaint_orders"' not in aggregate.sql
    assert "SUM" in aggregate.sql and "COUNT(*)" not in aggregate.sql
    assert aggregate.params["source_checkpoint"] == "synthetic.checkpoint.v1"
    assert "synthetic.checkpoint.v1" not in aggregate.sql


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stale", "unknown"])
async def test_freshness_fallback_requires_policy_and_records_reason(status: str) -> None:
    authority = AggregateAuthority()
    key = authority.aggregate_binding.deployment_source_id
    authority.freshness[key] = authority.freshness[key].model_copy(update={"status": status})
    with pytest.raises(PlanStepError, match="freshness_denied"):
        await authority.compiler().compile(authority.plan(), authority.context)
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"allow_detail_fallback": True})
    result = await authority.compiler().compile(authority.plan(), authority.context)
    assert result.source_kind == "approved_detail"
    assert result.degradation == (f"aggregate_{status}",)
    assert result.selection_reason == "approved_detail_fallback"
    with pytest.raises(PlanStepError, match="aggregate_freshness_denied"):
        await authority.compiler(bindings=(authority.aggregate_binding,)).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
async def test_aggregate_approval_covers_deployment_eligibility() -> None:
    authority = AggregateAuthority()
    policy = EligibilityPolicy(policy_id=authority.metric.eligibility_policy_id,
                               predicates=(Predicate(field="has_valid_bandwidth", operator="is_true"),))
    result = await authority.compiler(eligibility_policies=(policy,)).compile(authority.plan(), authority.context)
    assert result.source_kind == "approved_detail"
    assert '"has_valid_bandwidth" IS TRUE' in result.sql


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [
    IndexSnapshot("second_column", ("category", "acceptance_time"), False, False),
    IndexSnapshot("partial", ("acceptance_time",), False, False, "category IS NOT NULL"),
])
async def test_partial_or_nonleading_index_and_partition_expression_do_not_authorize_scan(index: IndexSnapshot) -> None:
    authority = MetricAuthority()
    authority.change_snapshot_relation(indexes=(index,), partition_key="RANGE (acceptance_time)")
    with pytest.raises(PlanStepError, match="time_index_required"):
        await authority.compiler().compile(authority.plan(), authority.context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["checkpoint", "watermark", "status", "missing"])
async def test_freshness_change_after_prepare_never_executes(change: str) -> None:
    authority = AggregateAuthority()
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"allow_detail_fallback": True})
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    gateway.execute = AsyncMock()
    runner = GatewayMetricStepRunner(authority.compiler(), gateway)
    plan = authority.plan()
    prepared = await runner.prepare(step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
                                    query_plan=plan, context=authority.context)
    source = authority.aggregate_binding.deployment_source_id
    record = authority.freshness[source]
    if change == "missing":
        authority.freshness.pop(source)
    else:
        assert record.data_as_of is not None
        changes = {"checkpoint": {"checkpoint": "synthetic.checkpoint.v2"},
                   "watermark": {"data_as_of": record.data_as_of + timedelta(seconds=1)},
                   "status": {"status": "stale"}}
        authority.freshness[source] = record.model_copy(update=changes[change])
    with pytest.raises(PlanStepError, match="prepared_query_changed|freshness_evidence_invalid"):
        await runner.execute(prepared, timeout_ms=1000)
    gateway.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["fresh", "stale", "unknown"])
async def test_only_selected_source_executes_and_receipt_records_authority(status: str) -> None:
    authority = AggregateAuthority()
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"allow_detail_fallback": True})
    source = authority.aggregate_binding.deployment_source_id
    authority.freshness[source] = authority.freshness[source].model_copy(update={"status": status})
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    runner = GatewayMetricStepRunner(authority.compiler(), gateway)
    plan = authority.plan()
    prepared = await runner.prepare(step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
                                    query_plan=plan, context=authority.context)
    gateway.execute = AsyncMock(return_value=QueryReceipt(
        accepted=True, sql="", sql_fingerprint=prepared.sql_fingerprint,
        rows=[{"value": 1}], row_count=1, policy_outcome="allow", max_rows=200,
    ))
    result = await runner.execute(prepared, timeout_ms=1000)
    gateway.execute.assert_awaited_once()
    assert result.receipt is not None
    assert result.receipt.source_kind == ("approved_aggregate" if status == "fresh" else "approved_detail")
    assert result.receipt.source_checkpoint == "synthetic.checkpoint.v1"
    sql = gateway.execute.call_args.args[0]
    assert ('"approved_daily_facts"' in sql) == (status == "fresh")
    assert ('"complaint_orders"' in sql) == (status != "fresh")


def test_freshness_and_mapping_require_complete_unambiguous_evidence() -> None:
    authority = AggregateAuthority()
    assert authority.aggregate_binding.aggregate is not None
    mapping = authority.aggregate_binding.aggregate.columns.model_dump()
    mapping["value"] = mapping["numerator"]
    with pytest.raises(ValidationError):
        AggregateColumns.model_validate(mapping)
    record = authority.freshness[authority.aggregate_binding.deployment_source_id]
    for field in ("checkpoint", "data_as_of", "snapshot_id", "snapshot_checksum", "release_id"):
        with pytest.raises(ValidationError):
            SourceFreshnessRecord.model_validate({**record.model_dump(), field: None})
    with pytest.raises(ValidationError):
        SourceFreshnessRecord.model_validate({**record.model_dump(), "checked_at": NOW - timedelta(seconds=1)})


@pytest.mark.asyncio
async def test_detail_scan_requires_index_or_explicit_bounded_bootstrap() -> None:
    authority = MetricAuthority()
    authority.change_snapshot_relation(indexes=())
    with pytest.raises(PlanStepError, match="time_index_required"):
        await authority.compiler().compile(authority.plan(), authority.context)
    binding = authority.binding.model_copy(update={"bootstrap_scan_max_rows": 12})
    await authority.compiler(bindings=(binding,)).compile(authority.plan(), authority.context)
    authority.change_snapshot_relation(estimated_rows=1_000_001)
    with pytest.raises(PlanStepError, match="scan_rows_exceeded"):
        await authority.compiler(bindings=(binding,)).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["permission", "release", "snapshot", "column", "type", "coverage", "freshness"])
async def test_aggregate_authority_is_not_implied_by_freshness(case: str) -> None:
    authority = AggregateAuthority(ratio_contract())
    assert authority.snapshot is not None and authority.release is not None
    options = {}
    if case == "permission":
        options["identity"] = authority.identity.model_copy(update={"permissions": frozenset()})
    elif case == "release":
        authority.release = replace(authority.release, documents=tuple(
            doc for doc in authority.release.documents if doc.document_id != authority.aggregate_binding.relation_asset_id))
    elif case == "snapshot":
        authority.snapshot = replace(authority.snapshot, candidate=replace(authority.snapshot.candidate,
                                    relations=authority.snapshot.candidate.relations[:1]))
    elif case == "column":
        authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"allowed_columns": ("day_at",)})
    elif case in {"type", "coverage"}:
        relation = authority.snapshot.candidate.relations[1]
        if case == "type":
            relation = replace(relation, columns=tuple(replace(column, data_type="text")
                               if column.name == "numerator" else column for column in relation.columns))
        else:
            relation = replace(relation, aggregate_coverage=())
        authority.snapshot = replace(authority.snapshot, candidate=replace(authority.snapshot.candidate,
                                    relations=(authority.snapshot.candidate.relations[0], relation)))
    else:
        key = authority.aggregate_binding.deployment_source_id
        authority.freshness[key] = authority.freshness[key].model_copy(update={"snapshot_checksum": "b" * 64})
    with pytest.raises(PlanStepError):
        await authority.compiler(**options).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
async def test_detail_required_policy_and_uncovered_formula() -> None:
    authority = AggregateAuthority()
    binding = authority.binding.model_copy(update={"allow_detail_required": False})
    with pytest.raises(PlanStepError, match="detail_strategy_denied"):
        await authority.compiler(bindings=(binding, authority.aggregate_binding)).compile(
            authority.plan(source_strategy="detail_required"), authority.context)
    assert authority.aggregate_binding.aggregate is not None
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"aggregate":
        authority.aggregate_binding.aggregate.model_copy(update={"formula_version": "other.v1"})})
    result = await authority.compiler().compile(authority.plan(), authority.context)
    assert result.source_kind == "approved_detail"


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected", [(0, "approved_aggregate"), (1, "approved_aggregate"),
                                         (1.000001, "approved_detail"), (3600, "approved_detail")])
async def test_sla_age_is_recomputed_with_inclusive_boundary(age: float, expected: str) -> None:
    authority = AggregateAuthority(seed_contract(freshness_sla_seconds=1))
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"allow_detail_fallback": True})
    query = await authority.compiler(clock=lambda: NOW + timedelta(seconds=age)).compile(
        authority.plan(), authority.context)
    assert query.source_kind == expected
    assert query.freshness is not None and query.freshness.data_as_of == NOW
    if expected == "approved_detail":
        assert query.degradation == ("aggregate_stale",)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["data_as_of", "checked_at"])
async def test_future_freshness_evidence_is_denied(field: str) -> None:
    authority = AggregateAuthority()
    source = authority.aggregate_binding.deployment_source_id
    authority.freshness[source] = authority.freshness[source].model_copy(
        update={field: NOW + timedelta(microseconds=1)})
    with pytest.raises(PlanStepError, match="freshness_evidence_invalid"):
        await authority.compiler().compile(authority.plan(), authority.context)


@pytest.mark.asyncio
async def test_sla_required_and_clock_does_not_replace_watermark() -> None:
    authority = AggregateAuthority()
    metric = authority.metric.model_copy(update={"freshness_sla_seconds": None})
    assert authority.release is not None and authority.aggregate_binding.aggregate is not None
    authority.metric = metric
    authority.release = replace(authority.release, documents=tuple(
        replace(document, metadata={**document.metadata, "execution_contract": metric.model_dump_json()})
        if document.document_id == metric.asset_id else document
        for document in authority.release.documents
    ))
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"aggregate":
        authority.aggregate_binding.aggregate.model_copy(update={
            "metric_contract_sha256": aggregate_definition_checksum(metric),
        })
    })
    with pytest.raises(PlanStepError, match="aggregate_sla_missing"):
        await authority.compiler().compile(authority.plan(), authority.context)
    authority = AggregateAuthority()
    calls = []

    def clock() -> datetime:
        calls.append(True)
        return NOW + timedelta(seconds=10)

    compiler = authority.compiler(clock=clock)
    first = await compiler.compile(authority.plan(), authority.context)
    second = await compiler.compile(authority.plan(), authority.context)
    assert len(calls) == 2 and first == second
    assert first.freshness is not None and first.freshness.data_as_of == NOW
    with pytest.raises(PlanStepError, match="clock_invalid"):
        await authority.compiler(clock=lambda: datetime(2026, 9, 1)).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
async def test_expiration_between_prepare_and_execute_blocks_gateway() -> None:
    authority = AggregateAuthority(seed_contract(freshness_sla_seconds=1))
    instants = iter((NOW, NOW + timedelta(seconds=2)))
    gateway = QueryGateway(async_sessionmaker(), schema="ai_views")
    gateway.execute = AsyncMock()
    runner = GatewayMetricStepRunner(authority.compiler(clock=lambda: next(instants)), gateway)
    plan = authority.plan()
    prepared = await runner.prepare(step=FetchMetricStep(step_id="fetch", metric_keys=plan.metric_keys),
                                    query_plan=plan, context=authority.context)
    with pytest.raises(PlanStepError, match="aggregate_freshness_denied"):
        await runner.execute(prepared, timeout_ms=1000)
    gateway.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("permission", "metric_permission_denied"), ("coverage", "metric_aggregate_coverage_unapproved"),
    ("relation", "metric_relation_unapproved"), ("sensitivity", "metric_aggregate_sensitivity_denied"),
    ("column", "metric_column_unapproved"), ("type", "metric_column_type_mismatch"),
])
async def test_source_specific_denial_falls_back_only_after_complete_detail_validation(case: str, code: str) -> None:
    authority = AggregateAuthority()
    assert authority.release is not None and authority.snapshot is not None
    binding = authority.aggregate_binding.model_copy(update={"allow_detail_fallback": True})
    if case == "permission":
        binding = binding.model_copy(update={"required_permissions": ("aggregate:restricted",)})
    elif case == "relation":
        authority.release = replace(authority.release, documents=tuple(
            doc for doc in authority.release.documents if doc.document_id != binding.relation_asset_id))
    elif case == "column":
        binding = binding.model_copy(update={"allowed_columns": ("day_at",)})
    else:
        detail, aggregate = authority.snapshot.candidate.relations
        if case == "coverage":
            aggregate = replace(aggregate, aggregate_coverage=())
        elif case == "sensitivity":
            aggregate = replace(aggregate, sensitivity="restricted")
        else:
            aggregate = replace(aggregate, columns=tuple(replace(column, data_type="text")
                                if column.name == "numerator" else column for column in aggregate.columns))
        authority.snapshot = replace(authority.snapshot, candidate=replace(
            authority.snapshot.candidate, relations=(detail, aggregate)))
    authority.aggregate_binding = binding
    query = await authority.compiler().compile(authority.plan(), authority.context)
    assert query.source_kind == "approved_detail" and query.degradation == (code,)
    assert query.selection_reason == "approved_detail_fallback"
    with pytest.raises(PlanStepError, match=code):
        await authority.compiler(bindings=(binding,)).compile(authority.plan(), authority.context)
    with pytest.raises(PlanStepError):
        await authority.compiler(bindings=(binding, authority.binding.model_copy(
            update={"required_permissions": ("detail:restricted",)}))).compile(authority.plan(), authority.context)
    with pytest.raises(PlanStepError, match=code):
        await authority.compiler(bindings=(binding.model_copy(update={"allow_detail_fallback": False}),
                                            authority.binding)).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
async def test_global_metric_permission_cannot_fall_back() -> None:
    authority = AggregateAuthority()
    authority.aggregate_binding = authority.aggregate_binding.model_copy(update={"allow_detail_fallback": True})
    identity = authority.identity.model_copy(update={"permissions": frozenset({"metrics:read"})})
    with pytest.raises(PlanStepError):
        await authority.compiler(identity=identity).compile(authority.plan(), authority.context)


@pytest.mark.asyncio
async def test_invalid_first_aggregate_does_not_hide_later_eligible_source() -> None:
    authority = AggregateAuthority()
    first = authority.aggregate_binding.model_copy(update={"source_id": "a.first", "allowed_columns": ("day_at",)})
    second = authority.aggregate_binding.model_copy(update={"source_id": "b.second"})
    original = authority.freshness[authority.aggregate_binding.deployment_source_id]
    for binding in (first, second):
        authority.freshness[binding.deployment_source_id] = original.model_copy(update={"source_id": binding.deployment_source_id})
    result = await authority.compiler(bindings=(authority.binding, second, first)).compile(authority.plan(), authority.context)
    assert result.source_id == "b.second"
