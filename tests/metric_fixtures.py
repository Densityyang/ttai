"""Synthetic published metric authority shared by unit and Docker contracts."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from src.nl2sql.contracts import ContextBundle, QueryPlan, RequestIdentity, TimeRange
from src.nl2sql.orchestration.metric_query import (
    EligibilityPolicy,
    MetricQueryCompiler,
    RelationBinding,
)
from src.nl2sql.semantic.authoring import validate_authoring_ir
from src.nl2sql.semantic.materialization import materialize_authoring_ir
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    MetricContract,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.registry import SemanticRelease, SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import (
    ColumnSnapshot,
    IndexSnapshot,
    RelationSnapshot,
    SchemaSnapshot,
    SchemaSnapshotCandidate,
    SchemaSnapshotState,
)

RELEASE_ID = "11111111-1111-1111-1111-111111111111"
SNAPSHOT_ID = "22222222-2222-2222-2222-222222222222"
METRIC_ID = "metric.complaint_in_transit_count"
RELATION = "ai_views.complaint_orders"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def seed_contract(**changes: Any) -> MetricContract:
    source = Path(__file__).resolve().parents[1] / "config/metrics/complaint.yaml"
    seed = load_metric_catalog(source.read_text(encoding="utf-8")).metrics[0]
    payload = seed.model_dump(mode="json")
    payload.update(owner="synthetic-owner", approver="synthetic-approver",
                   release_status="active", freshness_sla_seconds=86400)
    payload.update(changes)
    return MetricContract.model_validate(payload)


class MetricAuthority:
    def __init__(self, metric: MetricContract | None = None, *, timestamptz: bool = True) -> None:
        self.metric = metric or seed_contract()
        types = {
            "acceptance_time": "timestamp with time zone" if timestamptz else "timestamp without time zone",
            "completion_time": "timestamp with time zone",
            "is_valid_for_metrics": "boolean", "has_valid_bandwidth": "boolean",
            "category": "text", "priority": "integer",
        }
        relation = RelationSnapshot(
            relation_id=RELATION, schema_name="ai_views", relation_name="complaint_orders",
            relation_kind="table", columns=tuple(ColumnSnapshot(key, value, True, index)
                                                 for index, (key, value) in enumerate(types.items(), 1)),
            primary_key=(), foreign_keys=(),
            indexes=(IndexSnapshot("complaint_time", ("acceptance_time",), False, False),),
            partition_key=None, parent_relation_id=None, estimated_rows=12, total_bytes=8192,
            sensitivity="internal", sensitive_columns=(), aggregate_coverage=(),
            freshness_sla_seconds=None,
        )
        candidate = SchemaSnapshotCandidate("generated-fixture", ("ai_views",), (relation,), "a" * 64, "b" * 64)
        self.snapshot: SchemaSnapshot | None = SchemaSnapshot(
            SNAPSHOT_ID, SchemaSnapshotState.VALIDATED, candidate, {"ok": True}, NOW, NOW,
        )
        ir = metric_catalog_ir(MetricCatalog(metrics=(self.metric,)), relations={"complaint_orders": RELATION})
        report = validate_authoring_ir(ir, relation_columns={RELATION: types})
        assert report.ok, report.to_dict()
        release_candidate = materialize_authoring_ir(ir, report)
        relation_asset = next(asset for asset in release_candidate.assets if asset.asset_type == "relation")
        self.release: SemanticRelease | None = SemanticRelease(
            release_id=RELEASE_ID, version=1, checksum=release_candidate.checksum,
            state=SemanticReleaseState.ACTIVE, documents=release_candidate.documents,
            validation_report=report.to_dict(), change_summary="synthetic fixture",
            previous_release_id=None, created_at=NOW, schema_snapshot_id=SNAPSHOT_ID,
            schema_snapshot_checksum=candidate.checksum,
        )
        self.binding = RelationBinding(
            source_ref="complaint_orders", relation_asset_id=relation_asset.asset_id,
            schema_name="ai_views", relation_name="complaint_orders",
            allowed_columns=tuple(types), required_permissions=("metrics:read",), approved=True,
            timestamp_kind="timestamptz" if timestamptz else "timestamp",
        )
        self.identity = RequestIdentity(request_id=UUID(RELEASE_ID), user_id="synthetic-reader",
                                        permissions=frozenset({"metrics:read", "nl2sql:invoke"}))
        self.context = ContextBundle(
            semantic_release_id=UUID(RELEASE_ID), schema_snapshot_id=UUID(SNAPSHOT_ID),
            domains=("complaint",), asset_ids=(self.metric.asset_id,),
            approved_relation_ids=(relation_asset.asset_id,), resolution_status="resolved",
        )

    async def read_active(self) -> SemanticRelease | None:
        return self.release

    async def read_snapshot(self, snapshot_id: str) -> SchemaSnapshot | None:
        assert snapshot_id == SNAPSHOT_ID
        return self.snapshot

    def compiler(self, **overrides: Any) -> MetricQueryCompiler:
        options: dict[str, Any] = dict(
            read_active=self.read_active, read_snapshot=self.read_snapshot,
            bindings=(self.binding,),
            eligibility_policies=(EligibilityPolicy(policy_id=self.metric.eligibility_policy_id),),
            identity=self.identity,
        )
        options.update(overrides)
        return MetricQueryCompiler(**options)

    def plan(self, **changes: Any) -> QueryPlan:
        payload: dict[str, Any] = dict(
            intent="metric", domain="complaint", metric_keys=(self.metric.asset_id,),
            time_range=TimeRange(start=date(2024, 2, 29), end=date(2024, 2, 29)),
            grain="day", source_strategy="aggregate_first",
        )
        payload.update(changes)
        return QueryPlan.model_validate(payload)

    def change_snapshot_relation(self, **changes: Any) -> None:
        assert self.snapshot is not None
        relation = replace(self.snapshot.candidate.relations[0], **changes)
        self.snapshot = replace(self.snapshot, candidate=replace(self.snapshot.candidate, relations=(relation,)))
