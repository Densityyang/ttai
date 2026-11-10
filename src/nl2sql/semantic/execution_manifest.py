"""Immutable expected execution authority for validation and audit tooling.

The manifest is built from typed planning and publication objects.  It never
accepts a compiled SQL object as an authority source; rendered SQL fingerprints
are evidence that can be attached after compilation, not a definition of what
was authorized.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.nl2sql.contracts import (
    AuthorizationContext,
    ContextBundle,
    ExecutionPlan,
    FetchMetricStep,
    QueryPlan,
)
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.orchestration.metric_query import EligibilityPolicy, RelationBinding
from src.nl2sql.semantic.metric_contract import MetricContract
from src.nl2sql.semantic.registry import SemanticRelease
from src.nl2sql.semantic.schema_snapshot import SchemaSnapshot

Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RelationColumnAuthority(BaseModel):
    """Physical columns the selected, server-resolved relation may expose."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relation_id: str = Field(min_length=1, max_length=256)
    columns: tuple[str, ...] = Field(min_length=1, max_length=512)


class OutputFieldAuthority(BaseModel):
    """Typed physical binding for a pass-through date/dimension output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    alias: Literal["period", "dimension_id"]
    relation_id: str = Field(min_length=1, max_length=256)
    field: str = Field(min_length=1, max_length=128)
    kind: Literal["time_bucket", "dimension"]
    logical_type: Literal["date", "text", "integer"]
    grain: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)
    timestamp_kind: Literal["timestamp", "timestamptz"] | None = None
    collation: Literal["C"] | None = None


class SparseDateAuthority(BaseModel):
    """Authority for compiler DATE(time-column) IN (:available_date_*) filters."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relation_id: str = Field(min_length=1, max_length=256)
    column: str = Field(min_length=1, max_length=128)
    timezone: str = Field(min_length=1, max_length=64)
    timestamp_kind: Literal["timestamp", "timestamptz"]
    dates: tuple[date, ...] = Field(min_length=1, max_length=31)
    parameters: tuple[str, ...] = Field(min_length=1, max_length=31)


class RankingOrderTerm(BaseModel):
    """One frozen ORDER BY output alias and its exact SQL ordering semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    alias: Literal["value", "dimension_id"]
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"]


class MetricExecutionAuthority(BaseModel):
    """One FetchStep's independently typed metric execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fetch_step_id: str = Field(min_length=1, max_length=64)
    metric_key: str = Field(min_length=1, max_length=256)
    query_plan_sha256: Fingerprint
    fetch_query_plan_sha256: Fingerprint
    execution_plan_sha256: Fingerprint
    metric_contract_sha256: Fingerprint
    relation_binding_sha256: Fingerprint
    semantic_release_id: str = Field(min_length=1, max_length=128)
    semantic_release_checksum: Fingerprint
    schema_snapshot_id: str = Field(min_length=1, max_length=128)
    schema_snapshot_checksum: Fingerprint
    eligibility_policy_id: str = Field(min_length=1, max_length=128)
    predicate_authority: tuple[str, ...] = Field(min_length=1, max_length=192)
    time_column: str = Field(min_length=1, max_length=128)
    time_bounds: tuple[str, ...] = Field(min_length=2, max_length=8)
    grain: str = Field(min_length=1, max_length=32)
    timezone: str = Field(min_length=1, max_length=64)
    aggregation_identity: str | None = Field(default=None, max_length=64)
    ratio_numerator_roles: tuple[str, ...] = Field(default=(), max_length=16)
    ratio_denominator_roles: tuple[str, ...] = Field(default=(), max_length=16)
    unit: str | None = Field(default=None, max_length=64)
    source_kind: Literal["approved_aggregate", "approved_detail"]
    relation_id: str = Field(min_length=1, max_length=256)
    output_aliases: tuple[str, ...] = Field(min_length=1, max_length=32)
    output_logical_types: tuple[str, ...] = Field(min_length=1, max_length=32)
    authorization_scope_level: str | None = Field(default=None, max_length=64)
    authorization_scope_ids: tuple[str, ...] = Field(default=(), max_length=256)
    freshness_evidence_sha256: Fingerprint | None = None
    freshness_source_id: str | None = Field(default=None, max_length=128)
    freshness_checkpoint: str | None = Field(default=None, max_length=128)
    freshness_data_as_of: datetime | None = None


class ExpectedExecutionManifest(BaseModel):
    """Replayable, server-side expected authority for one typed plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # ``schema_version`` is retained for checkpoints produced by the first
    # validation slice.  ``manifest_version`` is the explicit public identity
    # used by new builders; both are fixed and included in the checksum.
    schema_version: Literal["1.0"] = "1.0"
    # v1.0 is the explicit legacy read shape. New server-built enforce
    # manifests use v1.1 and require complete release/snapshot/FetchStep authority.
    manifest_version: Literal["1.0", "1.1"] = "1.0"
    query_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # A dependency fetch may use a request-local child QueryPlan.  The root
    # plan remains the authority identity; the child checksum is evidence for
    # the specific FetchMetricStep being executed.
    fetch_query_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    fetch_step_id: str | None = Field(default=None, min_length=1, max_length=64)
    metric_authorities: tuple[MetricExecutionAuthority, ...] = Field(default=(), max_length=16)
    execution_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_release_id: str = Field(min_length=1, max_length=128)
    schema_snapshot_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    metric_contract_sha256: tuple[Fingerprint, ...] = Field(max_length=16)
    relation_binding_sha256: tuple[Fingerprint, ...] = Field(max_length=16)
    expected_sql_fingerprints: tuple[Fingerprint, ...] = Field(max_length=16)
    expected_parameter_binding_fingerprints: tuple[Fingerprint, ...] = Field(max_length=16)
    semantic_release_checksum: Fingerprint | None = None
    schema_snapshot_checksum: Fingerprint | None = None
    metric_identity: tuple[str, ...] = Field(default=(), max_length=16)
    metric_operation: tuple[str, ...] = Field(default=(), max_length=16)
    source_kind: tuple[Literal["approved_aggregate", "approved_detail", "ad_hoc"], ...] = Field(default=(), max_length=16)
    relation_identity: tuple[str, ...] = Field(default=(), max_length=16)
    time_column: tuple[str, ...] = Field(default=(), max_length=16)
    grain: tuple[str, ...] = Field(default=(), max_length=16)
    timezone: tuple[str, ...] = Field(default=(), max_length=16)
    time_bounds: tuple[str, ...] = Field(default=(), max_length=32)
    eligibility_predicates: tuple[str, ...] = Field(default=(), max_length=64)
    authorization_predicate_fields: tuple[str, ...] = Field(default=(), max_length=16)
    requested_filter_fields: tuple[str, ...] = Field(default=(), max_length=32)
    authorization_predicate_fingerprint: Fingerprint | None = None
    aggregate_identity: tuple[str, ...] = Field(default=(), max_length=16)
    ratio_numerator_roles: tuple[str, ...] = Field(default=(), max_length=16)
    ratio_denominator_roles: tuple[str, ...] = Field(default=(), max_length=16)
    # Ratio predicate authority is kept separately from the flattened
    # ``expected_predicates`` list.  The flattened list is useful for replay
    # equality, while these two complete sets prove the population relationship
    # (numerator = denominator population + numerator-only conditions).
    ratio_numerator_predicates: tuple[str, ...] = Field(default=(), max_length=64)
    ratio_denominator_predicates: tuple[str, ...] = Field(default=(), max_length=64)
    ratio_aggregate_numerator_column: str | None = Field(default=None, max_length=128)
    ratio_aggregate_denominator_column: str | None = Field(default=None, max_length=128)
    grouping_dimensions: tuple[str, ...] = Field(default=(), max_length=32)
    ordering_expectations: tuple[str, ...] = Field(default=(), max_length=16)
    relation_column_authority: tuple[RelationColumnAuthority, ...] = Field(default=(), max_length=16)
    output_field_authority: tuple[OutputFieldAuthority, ...] = Field(default=(), max_length=2)
    sparse_date_authority: SparseDateAuthority | None = None
    ranking_order_authority: tuple[RankingOrderTerm, ...] = Field(default=(), max_length=4)
    ranking_limit: int | None = Field(default=None, ge=1, le=100)
    cardinality_expectation: str | None = None
    join_identities: tuple[str, ...] = Field(default=(), max_length=32)
    join_cardinality: tuple[str, ...] = Field(default=(), max_length=32)
    output_aliases: tuple[str, ...] = Field(default=(), max_length=32)
    output_logical_types: tuple[str, ...] = Field(default=(), max_length=32)
    placeholder_identities: tuple[str, ...] = Field(default=(), max_length=32)
    parameter_binding_fingerprint: Fingerprint | None = None
    compiler_identity: str | None = Field(default=None, max_length=256)
    expected_predicates: tuple[str, ...] = Field(default=(), max_length=128)
    # Every executable WHERE predicate is copied from a typed authority source
    # and labelled by role. This keeps metric/base eligibility, request filters,
    # authorization and freshness evidence auditable without trusting SQL text.
    predicate_authority: tuple[str, ...] = Field(default=(), max_length=192)
    expected_parameter_bindings: tuple[str, ...] = Field(default=(), max_length=128)
    expected_time_values: tuple[str, ...] = Field(default=(), max_length=8)
    metric_units: tuple[str | None, ...] = Field(default=(), max_length=16)
    count_column: str | None = Field(default=None, max_length=128)
    aggregate_value_column: str | None = Field(default=None, max_length=128)
    authorization_scope_level: str | None = Field(default=None, max_length=64)
    authorization_scope_ids: tuple[str, ...] = Field(default=(), max_length=256)
    join_semantics: tuple[str, ...] = Field(default=(), max_length=32)
    output_semantics: tuple[str, ...] = Field(default=(), max_length=64)
    strategy: Literal["approved_aggregate", "approved_detail", "ad_hoc"] = "approved_detail"

    @model_validator(mode="after")
    def validate_authority_version(self) -> "ExpectedExecutionManifest":
        if self.manifest_version == "1.1":
            if (
                self.semantic_release_checksum is None
                or self.schema_snapshot_checksum is None
                or self.fetch_step_id is None
                or len(self.metric_authorities) != 1
            ):
                raise ValueError("manifest v1.1 requires complete per-fetch authority")
            authority = self.metric_authorities[0]
            unit_by_metric = (
                {
                    metric.removeprefix("metric."): unit
                    for metric, unit in zip(
                        self.metric_identity, self.metric_units, strict=True
                    )
                }
                if len(self.metric_units) == len(self.metric_identity)
                else {}
            )
            mismatches = []
            if authority.fetch_step_id != self.fetch_step_id:
                mismatches.append("fetch_step")
            if authority.query_plan_sha256 != self.query_plan_sha256:
                mismatches.append("root_plan")
            if authority.fetch_query_plan_sha256 != self.fetch_query_plan_sha256:
                mismatches.append("fetch_plan")
            if authority.execution_plan_sha256 != self.execution_plan_sha256:
                mismatches.append("execution_plan")
            if authority.semantic_release_id != self.semantic_release_id:
                mismatches.append("release_id")
            if authority.semantic_release_checksum != self.semantic_release_checksum:
                mismatches.append("release_checksum")
            if authority.schema_snapshot_id != self.schema_snapshot_id:
                mismatches.append("snapshot_id")
            if authority.schema_snapshot_checksum != self.schema_snapshot_checksum:
                mismatches.append("snapshot_checksum")
            if authority.relation_id not in self.relation_identity:
                mismatches.append("relation_id")
            if authority.metric_contract_sha256 not in self.metric_contract_sha256:
                mismatches.append("metric_contract")
            if authority.relation_binding_sha256 not in self.relation_binding_sha256:
                mismatches.append("relation_binding")
            if authority.metric_key not in unit_by_metric:
                mismatches.append("metric_key")
            elif authority.unit != unit_by_metric[authority.metric_key]:
                mismatches.append("unit")
            if authority.source_kind not in self.source_kind:
                mismatches.append("source_kind")
            if authority.output_aliases != self.output_aliases:
                mismatches.append("output_aliases")
            if authority.output_logical_types != self.output_logical_types:
                mismatches.append("output_types")
            if authority.predicate_authority != self.predicate_authority:
                mismatches.append("predicates")
            if self.time_column and authority.time_column != self.time_column[0]:
                mismatches.append("time_column")
            if self.grain and authority.grain != self.grain[0]:
                mismatches.append("grain")
            if self.timezone and authority.timezone != self.timezone[0]:
                mismatches.append("timezone")
            if mismatches:
                raise ValueError(
                    "manifest per-fetch authority identity mismatch: "
                    + ",".join(mismatches)
                )
        elif self.metric_authorities or self.fetch_step_id is not None:
            raise ValueError("legacy manifest cannot declare v1.1 fetch authority")
        return self

    @property
    def checksum(self) -> str:
        return canonical_json_digest(self.model_dump(mode="json"))

    @classmethod
    def from_authority(
        cls,
        *,
        query_plan: QueryPlan,
        context: ContextBundle,
        execution_plan: ExecutionPlan,
        metric_contracts: tuple[MetricContract, ...],
        relation_bindings: tuple[RelationBinding, ...],
        root_query_plan: QueryPlan | None = None,
        expected_sql_fingerprints: tuple[str, ...] = (),
        expected_parameter_binding_fingerprints: tuple[str, ...] = (),
    ) -> "ExpectedExecutionManifest":
        """Construct only from typed authority objects and their identities."""

        metric_keys = {
            item.removeprefix("metric.") for item in query_plan.metric_keys
        }
        supplied_keys = {
            item.metric_key if item.metric_key.startswith("metric.")
            else f"metric.{item.metric_key}"
            for item in metric_contracts
        }
        if supplied_keys != {f"metric.{item}" for item in metric_keys}:
            raise ValueError("metric contracts do not cover the query plan")
        authority_plan = root_query_plan or query_plan
        if execution_plan.query_plan_sha256 != authority_plan.checksum:
            raise ValueError("execution plan is not bound to the query plan")
        if execution_plan.semantic_release_id != context.semantic_release_id:
            raise ValueError("execution plan release does not match context")
        if execution_plan.schema_snapshot_id != context.schema_snapshot_id:
            raise ValueError("execution plan snapshot does not match context")
        if any(
            binding.relation_asset_id not in context.approved_relation_ids
            for binding in relation_bindings
        ):
            raise ValueError("relation bindings are not approved by context")

        metric_by_key = {
            item.metric_key.removeprefix("metric."): item
            for item in metric_contracts
        }
        ordered_metrics = tuple(metric_by_key[key] for key in sorted(metric_by_key))
        ordered_bindings = tuple(
            sorted(
                relation_bindings,
                key=lambda item: (
                    item.source_ref,
                    item.relation_id,
                    "aggregate" if item.aggregate is not None else "detail",
                ),
            )
        )
        release_checksum = getattr(context, "semantic_release_checksum", None)
        snapshot_checksum = getattr(context, "schema_snapshot_checksum", None)
        return cls(
            query_plan_sha256=authority_plan.checksum,
            fetch_query_plan_sha256=(query_plan.checksum if query_plan.checksum != authority_plan.checksum else None),
            execution_plan_sha256=execution_plan.checksum,
            semantic_release_id=str(context.semantic_release_id),
            schema_snapshot_id=str(context.schema_snapshot_id),
            policy_version=execution_plan.policy_version,
            metric_contract_sha256=tuple(
                canonical_json_digest(item.model_dump(mode="json"))
                for item in ordered_metrics
            ),
            relation_binding_sha256=tuple(
                canonical_json_digest(item.model_dump(mode="json"))
                for item in ordered_bindings
            ),
            expected_sql_fingerprints=expected_sql_fingerprints,
            expected_parameter_binding_fingerprints=(
                expected_parameter_binding_fingerprints
            ),
            semantic_release_checksum=release_checksum,
            schema_snapshot_checksum=snapshot_checksum,
            metric_identity=tuple(item.asset_id for item in ordered_metrics),
            metric_operation=tuple(item.operation for item in ordered_metrics),
            source_kind=tuple(
                "approved_aggregate" if binding.aggregate is not None else "approved_detail"
                for binding in ordered_bindings
            ) or ("approved_detail",),
            relation_identity=tuple(item.relation_id for item in ordered_bindings),
            time_column=tuple(item.business_time_column for item in ordered_metrics),
            grain=tuple(query_plan.grain for _ in ordered_metrics),
            timezone=tuple(item.timezone for item in ordered_metrics),
            time_bounds=(
                query_plan.time_range.start.isoformat(),
                query_plan.time_range.end.isoformat(),
                query_plan.time_range.timezone,
            ),
            eligibility_predicates=tuple(
                f"{predicate.field}:{predicate.operator}"
                for item in ordered_metrics
                for predicate in item.formula_predicates
            ) + tuple(
                f"{predicate.field}:{predicate.operator}"
                for binding in ordered_bindings
                for predicate in ()
            ),
            aggregate_identity=tuple(
                canonical_json_digest(binding.aggregate.model_dump(mode="json"))
                for binding in ordered_bindings if binding.aggregate is not None
            ),
            ratio_numerator_roles=tuple(
                predicate.field
                for item in ordered_metrics
                if item.ratio is not None
                for predicate in item.ratio.numerator_predicates
            ),
            ratio_denominator_roles=tuple(
                predicate.field
                for item in ordered_metrics
                if item.ratio is not None
                for predicate in item.ratio.denominator_predicates
            ),
            grouping_dimensions=tuple(query_plan.dimensions),
            ordering_expectations=("result_limit", str(query_plan.ranking_limit)) if query_plan.intent == "ranking" else (),
            cardinality_expectation="single_row" if query_plan.intent in {"metric", "detail"} else "bounded",
            output_aliases=(
                (("period",) if query_plan.intent == "trend" else ())
                + (("dimension_id",) if query_plan.dimensions else ())
                + ("numerator", "denominator", "value", "status")
                if any(item.operation == "ratio" for item in ordered_metrics)
                else (("period",) if query_plan.intent == "trend" else ())
                + (("dimension_id",) if query_plan.dimensions else ())
                + ("value",)
            ),
            output_logical_types=(
                ("date",) if query_plan.intent == "trend" else ()
            ) + (("text",) if query_plan.dimensions else ()) + (
                ("integer", "integer", "numeric", "text")
                if any(item.operation == "ratio" for item in ordered_metrics)
                else ("integer",)
            ),
            strategy=("approved_detail" if query_plan.source_strategy == "detail_required" else "approved_aggregate"),
        )


def build_expected_execution_manifest(
    *,
    query_plan: QueryPlan,
    execution_plan: ExecutionPlan,
    context: ContextBundle,
    metric_contracts: Iterable[MetricContract],
    relation_bindings: Iterable[RelationBinding],
    eligibility_policies: Iterable[EligibilityPolicy] = (),
    schema_snapshot: SchemaSnapshot,
    semantic_release: SemanticRelease,
    authorization_predicate_fingerprint: str | None = None,
    authorization_context: AuthorizationContext | None = None,
    placeholder_identities: Iterable[str] = (),
    parameter_binding_fingerprint: str | None = None,
    compiler_identity: str | None = None,
    expected_sql_fingerprints: Iterable[str] = (),
    expected_parameter_binding_fingerprints: Iterable[str] = (),
    actual_source_kind: Literal["approved_aggregate", "approved_detail", "ad_hoc"] | None = None,
    root_query_plan: QueryPlan | None = None,
    freshness: Any | None = None,
    source_step_id: str,
) -> ExpectedExecutionManifest:
    """Build a deterministic manifest from typed authority only.

    The compiler and SQL text are deliberately absent from the authority
    inputs.  They may contribute fingerprints after compilation, but cannot
    define metric identity, predicates, relations or ratio roles.
    """
    metrics = tuple(metric_contracts)
    bindings = tuple(relation_bindings)
    if not isinstance(schema_snapshot, SchemaSnapshot):
        raise ValueError("validated schema snapshot authority missing")
    if not isinstance(semantic_release, SemanticRelease):
        raise ValueError("semantic release authority missing")
    authority_plan = root_query_plan or query_plan
    if (
        schema_snapshot.state.value != "validated"
        or schema_snapshot.snapshot_id != str(context.schema_snapshot_id)
        or schema_snapshot.snapshot_id != str(execution_plan.schema_snapshot_id)
        or semantic_release.state.value != "active"
        or semantic_release.release_id != str(context.semantic_release_id)
        or semantic_release.release_id != str(execution_plan.semantic_release_id)
        or semantic_release.schema_snapshot_id != schema_snapshot.snapshot_id
        or semantic_release.schema_snapshot_checksum != schema_snapshot.checksum
        or execution_plan.query_plan_sha256 != authority_plan.checksum
    ):
        raise ValueError("manifest release or snapshot authority mismatch")
    if not isinstance(source_step_id, str) or not source_step_id:
        raise ValueError("manifest fetch step identity missing")
    source_step = next(
        (item for item in execution_plan.steps if item.step_id == source_step_id),
        None,
    )
    if (
        not isinstance(source_step, FetchMetricStep)
        or len(source_step.metric_keys) != 1
        or source_step.metric_keys[0] not in query_plan.metric_keys
    ):
        raise ValueError("manifest fetch step is not bound to one requested metric")
    # AD_HOC is a property of the final calculation result, never of a
    # dependency FetchMetricStep.  The fetch still has an approved detail or
    # aggregate source and must be validated against that source independently.
    if actual_source_kind is None:
        binding_kinds = {
            "approved_aggregate" if binding.aggregate is not None else "approved_detail"
            for binding in bindings
        }
        if len(binding_kinds) == 1:
            actual_source_kind = next(iter(binding_kinds))  # type: ignore[assignment]
        elif len(binding_kinds) > 1:
            raise ValueError("selected source strategy is required for aggregate/detail bindings")
    for metric in metrics:
        if not any(binding.source_ref == metric.source_ref for binding in bindings):
            raise ValueError("relation binding does not cover metric source")
    policies = tuple(eligibility_policies)
    if not policies:
        raise ValueError("eligibility policy authority missing")
    for metric in metrics:
        matching = [policy for policy in policies if policy.policy_id == metric.eligibility_policy_id]
        if not matching:
            raise ValueError("eligibility policy does not cover metric")
    manifest = ExpectedExecutionManifest.from_authority(
        query_plan=query_plan,
        context=context,
        execution_plan=execution_plan,
        metric_contracts=metrics,
        relation_bindings=bindings,
        root_query_plan=root_query_plan,
        expected_sql_fingerprints=tuple(expected_sql_fingerprints),
        expected_parameter_binding_fingerprints=tuple(expected_parameter_binding_fingerprints),
    )
    updates: dict[str, Any] = {
        "semantic_release_checksum": semantic_release.checksum if semantic_release else manifest.semantic_release_checksum,
        "schema_snapshot_checksum": schema_snapshot.checksum if schema_snapshot else manifest.schema_snapshot_checksum,
        "authorization_predicate_fingerprint": authorization_predicate_fingerprint,
        "placeholder_identities": tuple(placeholder_identities),
        "parameter_binding_fingerprint": parameter_binding_fingerprint,
        "compiler_identity": compiler_identity or "metric-query-compiler-v1",
    }
    if query_plan.dimensions:
        if schema_snapshot is None:
            raise ValueError("dimension output type authority missing")
        scope_level = query_plan.dimensions[0]
        selected_relation_ids = {binding.relation_id for binding in bindings}
        coverage_types = {
            coverage.value_type
            for relation in schema_snapshot.candidate.relations
            if relation.relation_id in selected_relation_ids
            for coverage in relation.organization_coverage
            if coverage.scope_level == scope_level and coverage.field is not None
        }
        if len(coverage_types) != 1 or next(iter(coverage_types)) not in {
            "text",
            "integer",
        }:
            raise ValueError("dimension output type authority invalid")
        raw_dimension_type = next(iter(coverage_types))
        if raw_dimension_type == "text":
            dimension_type: Literal["text", "integer"] = "text"
        elif raw_dimension_type == "integer":
            dimension_type = "integer"
        else:
            raise ValueError("dimension output type authority invalid")
        dimension_index = 1 if query_plan.intent == "trend" else 0
        output_aliases = manifest.output_aliases
        output_types = list(manifest.output_logical_types)
        if (
            dimension_index >= len(output_aliases)
            or output_aliases[dimension_index] != "dimension_id"
            or dimension_index >= len(output_types)
        ):
            raise ValueError("dimension output manifest shape invalid")
        output_types[dimension_index] = dimension_type
        updates["output_logical_types"] = tuple(output_types)
    if query_plan.checksum != (root_query_plan or query_plan).checksum:
        updates["fetch_query_plan_sha256"] = query_plan.checksum
    if policies:
        updates["eligibility_predicates"] = ("is_valid_for_metrics:is_true",) + tuple(
            f"{policy.policy_id}:{predicate.field}:{predicate.operator}"
            for policy in sorted(policies, key=lambda item: item.policy_id)
            for predicate in policy.predicates
        ) + tuple(
            f"{predicate.field}:{predicate.operator}"
            for metric in sorted(metrics, key=lambda item: item.metric_key)
            for predicate in metric.formula_predicates
        )
    if schema_snapshot is not None and schema_snapshot.snapshot_id != str(context.schema_snapshot_id):
        raise ValueError("schema snapshot does not match context")
    if semantic_release is not None and semantic_release.release_id != str(context.semantic_release_id):
        raise ValueError("semantic release does not match context")
    if semantic_release is not None:
        if (
            semantic_release.schema_snapshot_id is not None
            and semantic_release.schema_snapshot_id != str(context.schema_snapshot_id)
        ):
            raise ValueError("semantic release snapshot does not match context")
        if schema_snapshot is not None and (
            semantic_release.schema_snapshot_checksum is not None
            and semantic_release.schema_snapshot_checksum != schema_snapshot.checksum
        ):
            raise ValueError("semantic release snapshot checksum mismatch")
        documents = {document.document_id: document for document in semantic_release.documents}
        for metric in metrics:
            document = documents.get(metric.asset_id)
            if document is None or document.metadata.get("status") != "active":
                raise ValueError("metric contract is not bound to the semantic release")
            execution_contract = document.metadata.get("execution_contract")
            if execution_contract:
                try:
                    published = json.loads(execution_contract)
                except (TypeError, ValueError) as exc:
                    raise ValueError("published metric contract is invalid") from exc
                if canonical_json_digest(published) != canonical_json_digest(metric.model_dump(mode="json")):
                    raise ValueError("metric contract does not match semantic release")
    if schema_snapshot is not None:
        relations = {relation.relation_id: relation for relation in schema_snapshot.candidate.relations}
        for binding in bindings:
            relation = relations.get(binding.relation_id)
            if relation is None:
                raise ValueError("relation binding is absent from schema snapshot")
            if not set(binding.allowed_columns).issubset({column.name for column in relation.columns}):
                raise ValueError("relation binding contains an unapproved column")
    if authorization_context is not None and authorization_predicate_fingerprint is None:
        authorization_predicate_fingerprint = canonical_json_digest(
            authorization_context.model_dump(mode="json")
        )
        updates["authorization_predicate_fingerprint"] = authorization_predicate_fingerprint
    if authorization_context is not None and schema_snapshot is not None:
        selected_relation_ids = set(
            updates.get("relation_identity", manifest.relation_identity)
        )
        auth_fields: list[str] = []
        for relation in schema_snapshot.candidate.relations:
            if relation.relation_id not in selected_relation_ids:
                continue
            for coverage in relation.organization_coverage:
                if (
                    coverage.scope_level == authorization_context.scope_level
                    and coverage.field is not None
                ):
                    auth_fields.append(coverage.field)
        updates["authorization_predicate_fields"] = tuple(dict.fromkeys(auth_fields))
    updates["requested_filter_fields"] = tuple(
        dict.fromkeys(item.field_ref for item in query_plan.filters)
    )
    if schema_snapshot is not None:
        grouping_fields: list[str] = []
        selected_relation_ids = set(updates.get("relation_identity", manifest.relation_identity))
        for relation in schema_snapshot.candidate.relations:
            if relation.relation_id not in selected_relation_ids:
                continue
            for dimension in query_plan.dimensions:
                for coverage in relation.organization_coverage:
                    if coverage.scope_level == dimension and coverage.field is not None:
                        grouping_fields.append(coverage.field)
        if grouping_fields:
            updates["grouping_dimensions"] = tuple(dict.fromkeys(grouping_fields))
    if authorization_predicate_fingerprint is not None and len(authorization_predicate_fingerprint) != 64:
        raise ValueError("authorization predicate fingerprint is invalid")
    # The source kind is selected from typed authority, never from SQL text.
    if actual_source_kind is not None:
        updates["source_kind"] = (actual_source_kind,)
        updates["strategy"] = actual_source_kind
        if actual_source_kind in {"approved_aggregate", "approved_detail"}:
            selected = tuple(
                binding for binding in bindings
                if (binding.aggregate is not None) == (actual_source_kind == "approved_aggregate")
            )
            if selected:
                updates["relation_binding_sha256"] = tuple(
                    canonical_json_digest(binding.model_dump(mode="json"))
                    for binding in sorted(selected, key=lambda item: (item.source_ref, item.relation_id))
                )
                updates["relation_identity"] = tuple(
                    binding.relation_id
                    for binding in sorted(selected, key=lambda item: (item.source_ref, item.relation_id))
                )
                updates["aggregate_identity"] = tuple(
                    canonical_json_digest(binding.aggregate.model_dump(mode="json"))
                    for binding in selected if binding.aggregate is not None
                )
    # Build the complete expected parameter/predicate authority from the typed
    # plan, contracts, snapshot, authorization context and freshness evidence.
    selected_metric = metrics[0] if len(metrics) == 1 else None
    selected_source_kind = actual_source_kind or manifest.source_kind[0]
    selected_binding = next(
        (
            binding
            for binding in bindings
            if (selected_metric is None or binding.source_ref == selected_metric.source_ref)
            and (binding.aggregate is not None)
            == (selected_source_kind == "approved_aggregate")
        ),
        None,
    )
    if selected_metric is None or selected_binding is None:
        raise ValueError("manifest authority selection is ambiguous")
    updates["relation_column_authority"] = (
        RelationColumnAuthority(
            relation_id=selected_binding.relation_id,
            columns=tuple(sorted(set(selected_binding.allowed_columns))),
        ),
    )
    output_authority: list[OutputFieldAuthority] = []
    if query_plan.intent == "trend":
        output_authority.append(
            OutputFieldAuthority(
                alias="period",
                relation_id=selected_binding.relation_id,
                field=selected_metric.business_time_column,
                kind="time_bucket",
                logical_type="date",
                grain=query_plan.grain,
                timezone=query_plan.time_range.timezone,
                timestamp_kind=selected_binding.timestamp_kind,
            )
        )
    if query_plan.dimensions:
        if schema_snapshot is None or len(query_plan.dimensions) != 1:
            raise ValueError("dimension output binding authority missing")
        selected_relation = next(
            (
                item for item in schema_snapshot.candidate.relations
                if item.relation_id == selected_binding.relation_id
            ),
            None,
        )
        coverage = [
            item for item in (selected_relation.organization_coverage if selected_relation else ())
            if item.scope_level == query_plan.dimensions[0] and item.field is not None
        ]
        if len(coverage) != 1 or coverage[0].field is None:
            raise ValueError("dimension output binding authority invalid")
        dimension = coverage[0]
        dimension_field = dimension.field
        if dimension_field is None:
            raise ValueError("dimension output binding authority invalid")
        if dimension.value_type == "text":
            dimension_type: Literal["text", "integer"] = "text"
        elif dimension.value_type == "integer":
            dimension_type = "integer"
        else:
            raise ValueError("dimension output binding authority invalid")
        output_authority.append(
            OutputFieldAuthority(
                alias="dimension_id",
                relation_id=selected_binding.relation_id,
                field=dimension_field,
                kind="dimension",
                logical_type=dimension_type,
                collation="C" if dimension_type == "text" else None,
            )
        )
    updates["output_field_authority"] = tuple(output_authority)
    if query_plan.available_dates:
        updates["sparse_date_authority"] = SparseDateAuthority(
            relation_id=selected_binding.relation_id,
            column=selected_metric.business_time_column,
            timezone=query_plan.time_range.timezone,
            timestamp_kind=selected_binding.timestamp_kind,
            dates=query_plan.available_dates,
            parameters=tuple(
                f"available_date_{index}"
                for index, _ in enumerate(query_plan.available_dates)
            ),
        )
    if query_plan.intent == "ranking":
        if not query_plan.dimensions:
            raise ValueError("ranking dimension authority missing")
        updates["ranking_order_authority"] = (
            RankingOrderTerm(alias="value", direction="desc", nulls="last"),
            RankingOrderTerm(alias="dimension_id", direction="asc", nulls="last"),
        )
        updates["ranking_limit"] = query_plan.ranking_limit
    expected_params, expected_predicates, predicate_authority = _expected_bindings_and_predicates(
        query_plan=query_plan,
        metric=selected_metric,
        binding=selected_binding,
        policy=next(policy for policy in policies if policy.policy_id == selected_metric.eligibility_policy_id),
        schema_snapshot=schema_snapshot,
        authorization_context=authorization_context,
        source_kind=actual_source_kind or manifest.source_kind[0],
        freshness=freshness,
    )
    placeholder_ids = tuple(sorted(expected_params))
    updates["placeholder_identities"] = placeholder_ids
    updates["expected_parameter_bindings"] = tuple(
        _canonical_json({"name": name, "value": expected_params[name]})
        for name in placeholder_ids
    )
    updates["parameter_binding_fingerprint"] = canonical_json_digest(expected_params)
    updates["expected_predicates"] = tuple(expected_predicates)
    # Check that the role-tagged source records describe exactly the executable
    # predicate set. The role is metadata; removing it must reproduce the
    # already-built canonical expected WHERE predicates byte for byte.
    projected_authority = tuple(
        _canonical_json({key: value for key, value in json.loads(item).items() if key != "role"})
        for item in predicate_authority
    )
    if set(projected_authority) != set(expected_predicates):
        raise ValueError("predicate authority does not match executable predicate set")
    updates["predicate_authority"] = tuple(predicate_authority)
    if selected_metric.ratio is not None:
        # These are predicates inside COUNT(..) FILTER.  Global validity,
        # policy, metric and request filters are already applied by the outer
        # WHERE and therefore constrain both counts equally; only the ratio's
        # declared denominator and numerator conditions belong here.
        denominator_by_field: dict[str, str] = {}
        for predicate in selected_metric.ratio.denominator_predicates:
            if predicate.field in denominator_by_field:
                raise ValueError("ratio denominator predicate authority is duplicated or conflicting")
            denominator_by_field[predicate.field] = predicate.operator
        numerator_by_field: dict[str, str] = {}
        for predicate in selected_metric.ratio.numerator_predicates:
            if predicate.field in numerator_by_field:
                raise ValueError("ratio numerator predicate authority is duplicated or conflicting")
            if predicate.field in denominator_by_field:
                relation = "duplicate" if denominator_by_field[predicate.field] == predicate.operator else "conflicting"
                raise ValueError(f"ratio numerator predicate authority is {relation} with denominator")
            numerator_by_field[predicate.field] = predicate.operator
        updates["ratio_denominator_predicates"] = tuple(
            _predicate_json(field=item.field, operator=item.operator)
            for item in selected_metric.ratio.denominator_authority
        )
        updates["ratio_numerator_predicates"] = tuple(
            _predicate_json(field=item.field, operator=item.operator)
            for item in selected_metric.ratio.numerator_authority
        )
        ratio_role_authority = tuple(
            _role_predicate_json(
                role="ratio_denominator", field=item.field, operator=item.operator
            )
            for item in selected_metric.ratio.denominator_authority
        ) + tuple(
            _role_predicate_json(
                role="ratio_numerator_condition", field=item.field, operator=item.operator
            )
            for item in selected_metric.ratio.numerator_predicates
        )
        updates["predicate_authority"] = (
            *predicate_authority,
            *ratio_role_authority,
        )
    expected_time_values: list[str] = []
    for name in ("start_at", "end_at"):
        if name not in expected_params:
            continue
        value = expected_params[name]
        expected_time_values.append(
            value.isoformat() if isinstance(value, (datetime, time)) else str(value)
        )
    updates["expected_time_values"] = tuple(expected_time_values)
    updates["metric_units"] = (
        selected_metric.ratio.unit if selected_metric.ratio is not None else "count",
    )
    updates["count_column"] = selected_binding.count_column
    if selected_binding.aggregate is not None:
        updates["aggregate_value_column"] = selected_binding.aggregate.columns.value
    if authorization_context is not None:
        updates["authorization_scope_level"] = authorization_context.scope_level
        updates["authorization_scope_ids"] = tuple(authorization_context.allowed_scope_ids)
    updates["output_semantics"] = _expected_output_semantics(query_plan, selected_metric)
    if selected_metric.ratio is not None and selected_binding.aggregate is not None:
        updates["ratio_aggregate_numerator_column"] = selected_binding.aggregate.columns.numerator
        updates["ratio_aggregate_denominator_column"] = selected_binding.aggregate.columns.denominator
    updates["join_semantics"] = tuple(manifest.join_identities)
    metric_contract_digest = canonical_json_digest(
        selected_metric.model_dump(mode="json")
    )
    selected_binding_digest = canonical_json_digest(
        selected_binding.model_dump(mode="json")
    )
    source_kind = actual_source_kind or manifest.source_kind[0]
    if source_kind == "ad_hoc":
        raise ValueError(
            "AD_HOC provenance cannot be used as a metric source execution kind"
        )
    freshness_dump = (
        freshness.model_dump(mode="json")
        if freshness is not None and callable(getattr(freshness, "model_dump", None))
        else freshness
    )
    metric_authority = MetricExecutionAuthority(
        fetch_step_id=source_step_id,
        metric_key=selected_metric.metric_key,
        query_plan_sha256=authority_plan.checksum,
        fetch_query_plan_sha256=query_plan.checksum,
        execution_plan_sha256=execution_plan.checksum,
        metric_contract_sha256=metric_contract_digest,
        relation_binding_sha256=selected_binding_digest,
        semantic_release_id=semantic_release.release_id,
        semantic_release_checksum=semantic_release.checksum,
        schema_snapshot_id=schema_snapshot.snapshot_id,
        schema_snapshot_checksum=schema_snapshot.checksum,
        eligibility_policy_id=selected_metric.eligibility_policy_id,
        predicate_authority=tuple(updates["predicate_authority"]),
        time_column=selected_metric.business_time_column,
        time_bounds=tuple(updates.get("expected_time_values", ()))
        or (
            query_plan.time_range.start.isoformat(),
            query_plan.time_range.end.isoformat(),
        ),
        grain=query_plan.grain,
        timezone=query_plan.time_range.timezone,
        aggregation_identity=(
            canonical_json_digest(selected_binding.aggregate.model_dump(mode="json"))
            if selected_binding.aggregate is not None
            else None
        ),
        ratio_numerator_roles=manifest.ratio_numerator_roles,
        ratio_denominator_roles=manifest.ratio_denominator_roles,
        unit=(
            updates["metric_units"][0]
            if updates.get("metric_units")
            else None
        ),
        source_kind=source_kind,
        relation_id=selected_binding.relation_id,
        output_aliases=manifest.output_aliases,
        output_logical_types=tuple(
            updates.get("output_logical_types", manifest.output_logical_types)
        ),
        authorization_scope_level=updates.get(
            "authorization_scope_level", manifest.authorization_scope_level
        ),
        authorization_scope_ids=tuple(
            updates.get("authorization_scope_ids", manifest.authorization_scope_ids)
        ),
        freshness_evidence_sha256=(
            canonical_json_digest(freshness_dump)
            if freshness_dump is not None
            else None
        ),
        freshness_source_id=getattr(freshness, "source_id", None),
        freshness_checkpoint=getattr(freshness, "checkpoint", None),
        freshness_data_as_of=getattr(freshness, "data_as_of", None),
    )
    manifest_data = {
        **manifest.model_dump(mode="python"),
        **updates,
        "manifest_version": "1.1",
        "fetch_query_plan_sha256": query_plan.checksum,
        "fetch_step_id": source_step_id,
        "metric_authorities": (metric_authority,),
    }
    return ExpectedExecutionManifest.model_validate(manifest_data)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda item: item.isoformat() if isinstance(item, (datetime,)) else str(item),
    )


def _predicate_json(*, field: str, operator: str, value: object = None, parameter: str | None = None) -> str:
    payload: dict[str, object] = {"field": field.lower(), "operator": operator}
    if parameter is not None:
        payload["parameter"] = parameter
    if operator not in {"is_true", "is_false", "is_null", "is_not_null"}:
        payload["value"] = value
    return _canonical_json(payload)


def _role_predicate_json(*, role: str, field: str, operator: str) -> str:
    payload = json.loads(_predicate_json(field=field, operator=operator))
    payload["role"] = role
    return _canonical_json(payload)


def _expected_bindings_and_predicates(
    *,
    query_plan: QueryPlan,
    metric: MetricContract,
    binding: RelationBinding,
    policy: EligibilityPolicy,
    schema_snapshot: SchemaSnapshot | None,
    authorization_context: AuthorizationContext | None,
    source_kind: str,
    freshness: Any | None,
) -> tuple[dict[str, object], list[str], list[str]]:
    params: dict[str, object] = {}
    predicates: list[str] = []
    role_predicates: list[str] = []

    def add_predicate(
        *,
        role: str,
        field: str,
        operator: str,
        value: object = None,
        parameter: str | None = None,
    ) -> None:
        encoded = _predicate_json(
            field=field, operator=operator, value=value, parameter=parameter
        )
        predicates.append(encoded)
        payload = json.loads(encoded)
        payload["role"] = role
        role_predicates.append(_canonical_json(payload))

    tz = ZoneInfo(query_plan.time_range.timezone)
    start = datetime.combine(query_plan.time_range.start, time.min).replace(tzinfo=tz)
    end = datetime.combine(query_plan.time_range.end, time.min).replace(tzinfo=tz)
    end = end + timedelta(days=1)
    if binding.timestamp_kind == "timestamp":
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
    params.update({"start_at": start, "end_at": end})
    add_predicate(
        role="business_time", field=metric.business_time_column,
        operator="gte", value=start, parameter="start_at",
    )
    add_predicate(
        role="business_time", field=metric.business_time_column,
        operator="lt", value=end, parameter="end_at",
    )
    if query_plan.available_dates:
        for index, value in enumerate(query_plan.available_dates):
            name = f"available_date_{index}"
            params[name] = value
            add_predicate(
                role="sparse_availability", field="__available_date__",
                operator="eq", value=value, parameter=name,
            )
    relation = None
    if schema_snapshot is not None:
        relation = next((item for item in schema_snapshot.candidate.relations if item.relation_id == binding.relation_id), None)
    coverage = {item.scope_level: item for item in (relation.organization_coverage if relation is not None else ())}
    field_map = {
        getattr(item, "field_ref", ""): getattr(item, "field", None)
        for item in metric.filters
    }
    field_map.update({scope: item.field for scope, item in coverage.items() if item.field is not None})
    if query_plan.dimensions:
        dimension = coverage.get(query_plan.dimensions[0])
        if dimension is None or dimension.field is None:
            raise ValueError("dimension presence predicate authority missing")
        add_predicate(
            role="dimension_presence",
            field=dimension.field,
            operator="is_not_null",
        )
    for index, item in enumerate(query_plan.filters):
        field = field_map.get(item.field_ref)
        if field is None:
            raise ValueError("manifest filter authority missing")
        values = item.value if item.operator == "in" else [item.value]
        if not isinstance(values, list):
            raise ValueError("manifest filter value authority invalid")
        names = []
        for position, value in enumerate(values):
            name = f"filter_{index}_{position}" if item.operator == "in" else f"filter_{index}"
            params[name] = value
            names.append(name)
        if item.operator == "in":
            add_predicate(
                role="request_filter", field=field, operator="in",
                value=values, parameter=",".join(names),
            )
        else:
            add_predicate(
                role="request_filter", field=field, operator=item.operator,
                value=item.value, parameter=names[0],
            )
    add_predicate(
        role="metric_base", field="is_valid_for_metrics", operator="is_true"
    )
    for predicate in metric.predicates:
        add_predicate(role="metric_base", field=predicate.field, operator=predicate.operator)
    for predicate in policy.predicates:
        add_predicate(role="eligibility", field=predicate.field, operator=predicate.operator)
    if authorization_context is not None and relation is not None:
        auth = next((item for item in relation.organization_coverage if item.scope_level == authorization_context.scope_level), None)
        # A scope can be enforced by the published view itself and therefore
        # have no physical predicate column (for example city_company).  The
        # scope level and IDs remain bound in the manifest; only add SQL
        # placeholders when the typed schema explicitly exposes a field.
        if auth is not None and auth.field is not None:
            names = []
            for position, value in enumerate(authorization_context.allowed_scope_ids):
                name = f"auth_scope_{position}"
                params[name] = int(value) if auth.value_type == "integer" else value
                names.append(name)
            add_predicate(
                role="authorization",
                field=auth.field,
                operator="in",
                value=[params[name] for name in names],
                parameter=",".join(names),
            )
    if source_kind == "approved_aggregate" and binding.aggregate is not None:
        aggregate = binding.aggregate
        source_values = {
            "source_metric": metric.metric_key,
            "source_formula": metric.formula_version,
            "source_release": getattr(freshness, "release_id", None),
            "source_snapshot": getattr(freshness, "snapshot_id", None),
            "source_checkpoint": getattr(freshness, "checkpoint", None),
            "source_data_as_of": getattr(freshness, "data_as_of", None),
            "source_grain": "day",
            "source_dimension": query_plan.dimensions[0] if query_plan.dimensions else (authorization_context.scope_level if authorization_context else "city_company"),
        }
        column_map = {
            "source_metric": aggregate.columns.metric_key,
            "source_formula": aggregate.columns.formula_version,
            "source_release": aggregate.columns.release_id,
            "source_snapshot": aggregate.columns.snapshot_id,
            "source_checkpoint": aggregate.columns.checkpoint,
            "source_data_as_of": aggregate.columns.data_as_of,
            "source_grain": aggregate.columns.grain,
            "source_dimension": aggregate.columns.dimension,
        }
        for key, value in source_values.items():
            if value is not None:
                params[key] = value
                add_predicate(
                    role="aggregate_freshness", field=column_map[key],
                    operator="eq", value=value, parameter=key,
                )
    return params, predicates, role_predicates


def _expected_output_semantics(query_plan: QueryPlan, metric: MetricContract) -> tuple[str, ...]:
    if metric.operation == "ratio":
        return ("ratio", "numerator:count", "denominator:count", "round:2", "zero_denominator:calculation_error", "unit:percent")
    return ("count", "alias:value", "unit:count", "cardinality:single_row" if query_plan.intent != "ranking" else f"limit:{query_plan.ranking_limit}")


def manifest_checksum(manifest: ExpectedExecutionManifest | object) -> str:
    """Return the canonical immutable identity of an expected manifest."""
    if isinstance(manifest, ExpectedExecutionManifest):
        return manifest.checksum
    if isinstance(manifest, dict):
        return canonical_json_digest(manifest)
    raise TypeError("manifest_checksum requires an ExpectedExecutionManifest or mapping")


__all__ = [
    "ExpectedExecutionManifest",
    "MetricExecutionAuthority",
    "build_expected_execution_manifest",
    "manifest_checksum",
]
