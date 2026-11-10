"""Immutable expected execution authority for validation and audit tooling.

The manifest is built from typed planning and publication objects.  It never
accepts a compiled SQL object as an authority source; rendered SQL fingerprints
are evidence that can be attached after compilation, not a definition of what
was authorized.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, time, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from src.nl2sql.contracts import AuthorizationContext, ContextBundle, ExecutionPlan, QueryPlan
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.orchestration.metric_query import EligibilityPolicy, RelationBinding
from src.nl2sql.semantic.metric_contract import MetricContract, Predicate
from src.nl2sql.semantic.registry import SemanticRelease
from src.nl2sql.semantic.schema_snapshot import SchemaSnapshot

Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExpectedExecutionManifest(BaseModel):
    """Replayable, server-side expected authority for one typed plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # ``schema_version`` is retained for checkpoints produced by the first
    # validation slice.  ``manifest_version`` is the explicit public identity
    # used by new builders; both are fixed and included in the checksum.
    schema_version: Literal["1.0"] = "1.0"
    manifest_version: Literal["1.0"] = "1.0"
    query_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # A dependency fetch may use a request-local child QueryPlan.  The root
    # plan remains the authority identity; the child checksum is evidence for
    # the specific FetchMetricStep being executed.
    fetch_query_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    grouping_dimensions: tuple[str, ...] = Field(default=(), max_length=32)
    ordering_expectations: tuple[str, ...] = Field(default=(), max_length=16)
    cardinality_expectation: str | None = None
    join_identities: tuple[str, ...] = Field(default=(), max_length=32)
    join_cardinality: tuple[str, ...] = Field(default=(), max_length=32)
    output_aliases: tuple[str, ...] = Field(default=(), max_length=32)
    output_logical_types: tuple[str, ...] = Field(default=(), max_length=32)
    placeholder_identities: tuple[str, ...] = Field(default=(), max_length=32)
    parameter_binding_fingerprint: Fingerprint | None = None
    compiler_identity: str | None = Field(default=None, max_length=256)
    expected_predicates: tuple[str, ...] = Field(default=(), max_length=128)
    expected_parameter_bindings: tuple[str, ...] = Field(default=(), max_length=128)
    expected_time_values: tuple[str, ...] = Field(default=(), max_length=8)
    metric_units: tuple[str | None, ...] = Field(default=(), max_length=16)
    authorization_scope_level: str | None = Field(default=None, max_length=64)
    authorization_scope_ids: tuple[str, ...] = Field(default=(), max_length=256)
    join_semantics: tuple[str, ...] = Field(default=(), max_length=32)
    output_semantics: tuple[str, ...] = Field(default=(), max_length=64)
    strategy: Literal["approved_aggregate", "approved_detail", "ad_hoc"] = "approved_detail"

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
    schema_snapshot: SchemaSnapshot | None = None,
    semantic_release: SemanticRelease | None = None,
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
    source_step_id: str | None = None,
) -> ExpectedExecutionManifest:
    """Build a deterministic manifest from typed authority only.

    The compiler and SQL text are deliberately absent from the authority
    inputs.  They may contribute fingerprints after compilation, but cannot
    define metric identity, predicates, relations or ratio roles.
    """
    metrics = tuple(metric_contracts)
    bindings = tuple(relation_bindings)
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
    selected_binding = next(
        (binding for binding in bindings if selected_metric is None or binding.source_ref == selected_metric.source_ref),
        None,
    )
    if selected_metric is None or selected_binding is None:
        raise ValueError("manifest authority selection is ambiguous")
    expected_params, expected_predicates = _expected_bindings_and_predicates(
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
    if authorization_context is not None:
        updates["authorization_scope_level"] = authorization_context.scope_level
        updates["authorization_scope_ids"] = tuple(authorization_context.allowed_scope_ids)
    updates["output_semantics"] = _expected_output_semantics(query_plan, selected_metric)
    updates["join_semantics"] = tuple(manifest.join_identities)
    return manifest.model_copy(update=updates)


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
) -> tuple[dict[str, object], list[str]]:
    params: dict[str, object] = {}
    predicates: list[str] = []
    tz = ZoneInfo(query_plan.time_range.timezone)
    start = datetime.combine(query_plan.time_range.start, time.min).replace(tzinfo=tz)
    end = datetime.combine(query_plan.time_range.end, time.min).replace(tzinfo=tz)
    end = end + timedelta(days=1)
    if binding.timestamp_kind == "timestamp":
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
    params.update({"start_at": start, "end_at": end})
    predicates.extend((
        _predicate_json(field=metric.business_time_column, operator="gte", value=start, parameter="start_at"),
        _predicate_json(field=metric.business_time_column, operator="lt", value=end, parameter="end_at"),
    ))
    if query_plan.available_dates:
        for index, value in enumerate(query_plan.available_dates):
            name = f"available_date_{index}"
            params[name] = value
            predicates.append(_predicate_json(field="__available_date__", operator="eq", value=value, parameter=name))
    relation = None
    if schema_snapshot is not None:
        relation = next((item for item in schema_snapshot.candidate.relations if item.relation_id == binding.relation_id), None)
    coverage = {item.scope_level: item for item in (relation.organization_coverage if relation is not None else ())}
    field_map = {
        getattr(item, "field_ref", ""): getattr(item, "field", None)
        for item in metric.filters
    }
    field_map.update({scope: item.field for scope, item in coverage.items() if item.field is not None})
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
            predicates.append(_predicate_json(field=field, operator="in", value=values, parameter=",".join(names)))
        else:
            predicates.append(_predicate_json(field=field, operator=item.operator, value=item.value, parameter=names[0]))
    for predicate in (Predicate(field="is_valid_for_metrics", operator="is_true"), *policy.predicates, *metric.predicates):
        predicates.append(_predicate_json(field=predicate.field, operator=predicate.operator))
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
            predicates.append(
                _predicate_json(
                    field=auth.field,
                    operator="in",
                    value=[params[name] for name in names],
                    parameter=",".join(names),
                )
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
                predicates.append(_predicate_json(field=column_map[key], operator="eq", value=value, parameter=key))
    return params, predicates


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


__all__ = ["ExpectedExecutionManifest", "build_expected_execution_manifest", "manifest_checksum"]
