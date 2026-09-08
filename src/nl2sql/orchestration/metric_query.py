"""Deterministic, release-bound aggregate queries through QueryGateway.

Only deployment code supplies sources, eligibility policies and identity. Plans
cannot select physical names, executable expressions, or disable predicates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, JsonValue, model_validator

from src.nl2sql.contracts import ContextBundle, FetchMetricStep, QueryPlan, RequestIdentity
from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.orchestration.candidates import rowset_sha256
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PlanStepError,
    PreparedMetricStep,
)
from src.nl2sql.orchestration.planning import PlanValidator
from src.nl2sql.semantic.metric_contract import (
    ContractId,
    FrozenContract,
    Identifier,
    MetricContract,
    Predicate,
)
from src.nl2sql.semantic.registry import SemanticRelease, SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import SchemaSnapshot, SchemaSnapshotState


class EligibilityPolicy(FrozenContract):
    policy_id: ContractId
    # The base eligibility is unconditional in the compiler; these are extra
    # deployment requirements, which a metric definition cannot remove.
    predicates: tuple[Predicate, ...] = ()


class OrganizationDimensionBinding(FrozenContract):
    """Deployment maps semantic organization scopes to stable ID columns only."""

    dimension: Literal["city_company", "area", "team"]
    field: Identifier | None = None
    value_type: Literal["text", "integer"] | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> OrganizationDimensionBinding:
        if self.dimension == "city_company":
            if self.field is not None or self.value_type is not None:
                raise ValueError("city company is a total scope without an ID column")
        elif self.field is None or self.value_type is None:
            raise ValueError("area/team require a typed stable ID column")
        return self


class RelationBinding(FrozenContract):
    source_ref: ContractId
    relation_asset_id: str = Field(min_length=1)
    schema_name: Identifier
    relation_name: Identifier
    allowed_columns: tuple[Identifier, ...] = Field(min_length=1)
    required_permissions: tuple[str, ...] = Field(min_length=1)
    approved: Literal[True]
    timestamp_kind: Literal["timestamp", "timestamptz"]
    max_days: int = Field(default=366, ge=1, le=3660)
    organization_dimensions: tuple[OrganizationDimensionBinding, ...] = (
        OrganizationDimensionBinding(dimension="city_company"),
    )

    @model_validator(mode="after")
    def unique_dimensions(self) -> RelationBinding:
        names = [item.dimension for item in self.organization_dimensions]
        fields = [item.field for item in self.organization_dimensions if item.field is not None]
        if len(set(names)) != len(names) or len(set(fields)) != len(fields):
            raise ValueError("duplicate organization dimension binding")
        return self

    @property
    def relation_id(self) -> str:
        return f"{self.schema_name}.{self.relation_name}"


@dataclass(frozen=True)
class CompiledMetricQuery:
    sql: str = field(repr=False)
    params: dict[str, Any] = field(repr=False)
    release_id: str
    snapshot_id: str
    snapshot_checksum: str
    query_plan: QueryPlan = field(repr=False)
    context: ContextBundle = field(repr=False)
    operation: Literal["count", "ratio"] = "count"
    dimension: OrganizationDimensionBinding | None = None


class MetricQueryCompiler:
    def __init__(
        self,
        *,
        read_active: Callable[[], Awaitable[SemanticRelease | None]],
        read_snapshot: Callable[[str], Awaitable[SchemaSnapshot | None]],
        bindings: tuple[RelationBinding, ...],
        eligibility_policies: tuple[EligibilityPolicy, ...],
        identity: RequestIdentity,
    ) -> None:
        self._read_active = read_active
        self._read_snapshot = read_snapshot
        self._bindings = {item.source_ref: item for item in bindings}
        self._policies = {item.policy_id: item for item in eligibility_policies}
        if len(self._bindings) != len(bindings) or len(self._policies) != len(eligibility_policies):
            raise ValueError("duplicate deployment binding or eligibility policy")
        self._identity = RequestIdentity.model_validate_json(identity.model_dump_json())

    async def compile(self, plan: QueryPlan, context: ContextBundle) -> CompiledMetricQuery:
        plan = QueryPlan.model_validate_json(plan.model_dump_json())
        context = ContextBundle.model_validate_json(context.model_dump_json())
        validation = PlanValidator().validate_query_plan(
            plan=plan, context=context, identity=self._identity,
        )
        if validation.outcome != "allow":
            raise PlanStepError("metric_plan_denied")
        if len(plan.metric_keys) != 1 or plan.intent not in {"metric", "trend", "comparison", "ranking"}:
            raise PlanStepError("metric_operation_unsupported")
        release = await self._read_active()
        if (release is None or release.state != SemanticReleaseState.ACTIVE
                or release.release_id != str(context.semantic_release_id)):
            raise PlanStepError("metric_active_release_mismatch")
        snapshot = await self._read_snapshot(str(context.schema_snapshot_id))
        if (snapshot is None or snapshot.state != SchemaSnapshotState.VALIDATED
                or snapshot.snapshot_id != str(context.schema_snapshot_id)
                or release.schema_snapshot_id != snapshot.snapshot_id
                or release.schema_snapshot_checksum != snapshot.checksum):
            raise PlanStepError("metric_schema_snapshot_mismatch")
        documents = [doc for doc in release.documents if doc.document_id == plan.metric_keys[0]]
        if len(documents) != 1:
            raise PlanStepError("metric_contract_missing")
        document = documents[0]
        try:
            metric = MetricContract.model_validate_json(document.metadata.get("execution_contract", ""))
        except ValueError as exc:
            raise PlanStepError("metric_contract_invalid") from exc
        if (metric.asset_id != document.document_id or metric.domain != plan.domain
                or document.metadata.get("status") != "active"
                or document.metadata.get("domain") != metric.domain
                or document.metadata.get("owner") != metric.owner
                or metric.release_status != "active" or not metric.assistant_enabled):
            raise PlanStepError("metric_contract_inactive")
        binding = self._bindings.get(metric.source_ref)
        policy = self._policies.get(metric.eligibility_policy_id)
        if binding is None or policy is None:
            raise PlanStepError("metric_source_or_policy_missing")
        permissions = set(metric.required_permissions) | set(binding.required_permissions)
        if (any(not permission.strip() for permission in permissions)
                or ("*" not in self._identity.permissions
                    and not permissions <= self._identity.permissions)):
            raise PlanStepError("metric_permission_denied")
        if binding.relation_asset_id not in context.approved_relation_ids:
            raise PlanStepError("metric_relation_unapproved")
        # A context relation id alone is not evidence: require the active release
        # relation document and the approved physical snapshot relation as well.
        relation_docs = [doc for doc in release.documents
                         if doc.document_id == binding.relation_asset_id
                         and doc.metadata.get("status") == "active"
                         and doc.metadata.get("asset_type") == "relation"]
        relations = [item for item in snapshot.candidate.relations
                     if item.relation_id == binding.relation_id]
        if len(relation_docs) != 1 or len(relations) != 1:
            raise PlanStepError("metric_relation_unapproved")
        relation = relations[0]
        columns = {column.name: column.data_type.lower() for column in relation.columns}
        predicates = (Predicate(field="is_valid_for_metrics", operator="is_true"),
                      *policy.predicates, *metric.predicates)
        formula_predicates = (*predicates, *metric.formula_predicates)
        dimension, filter_dimensions = _organization_scope(plan, metric, binding)
        used = {metric.business_time_column, *(item.field for item in formula_predicates),
                *(item.field for item in metric.filters)}
        used.update(item.field for item in filter_dimensions.values() if item.field is not None)
        if dimension is not None and dimension.field is not None:
            used.add(dimension.field)
        if (not used <= set(binding.allowed_columns) or not used <= columns.keys()
                or used & set(relation.sensitive_columns)):
            raise PlanStepError("metric_column_unapproved")
        time_type = columns[metric.business_time_column]
        expected_types = ({"timestamp without time zone", "timestamp"}
                          if binding.timestamp_kind == "timestamp"
                          else {"timestamp with time zone", "timestamptz"})
        if time_type not in expected_types or any(
            item.operator == "is_true" and columns[item.field] not in {"boolean", "bool"}
            for item in formula_predicates
        ):
            raise PlanStepError("metric_column_type_mismatch")
        for rule in metric.filters:
            _validate_column_type(rule.value_type, columns[rule.field])
        for organization in (*filter_dimensions.values(), *((dimension,) if dimension else ())):
            assert organization.field is not None and organization.value_type is not None
            _validate_column_type(organization.value_type, columns[organization.field])
        if (plan.grain not in metric.supported_grains
                or any(item not in metric.supported_dimensions for item in plan.dimensions)):
            raise PlanStepError("metric_grain_or_dimension_unsupported")
        if plan.time_range.timezone != "Asia/Shanghai":
            raise PlanStepError("metric_timezone_unsupported")
        days = (plan.time_range.end - plan.time_range.start).days + 1
        if days > binding.max_days:
            raise PlanStepError("metric_time_range_too_large")
        try:
            # Existing TimeRange is a pair of inclusive dates (same-day allowed).
            # Convert once to a half-open interval; month/day grain only groups.
            start = datetime.combine(plan.time_range.start, time.min)
            end = datetime.combine(plan.time_range.end + timedelta(days=1), time.min)
        except OverflowError as exc:
            raise PlanStepError("metric_time_range_overflow") from exc
        if binding.timestamp_kind == "timestamptz":
            start = start.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            end = end.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        params: dict[str, Any] = {"start_at": start, "end_at": end}
        business_time = _quote(metric.business_time_column)
        where = [f"{business_time} >= :start_at", f"{business_time} < :end_at"]
        where.extend(_predicate_sql(item) for item in predicates)
        if dimension is not None:
            assert dimension.field is not None
            where.append(f"{_quote(dimension.field)} IS NOT NULL")
        filters = {item.field: item for item in metric.filters}
        seen_filters: set[str] = set()
        for index, item in enumerate(plan.filters):
            if item.field_ref in seen_filters:
                raise PlanStepError("metric_filter_unsupported")
            seen_filters.add(item.field_ref)
            organization = filter_dimensions.get(item.field_ref)
            rule = filters.get(item.field_ref)
            if organization is not None:
                assert organization.field is not None and organization.value_type is not None
                field_name, value_type = organization.field, organization.value_type
                if item.operator not in {"eq", "in"}:
                    raise PlanStepError("metric_filter_unsupported")
            elif rule is not None and item.operator == "eq":
                field_name, value_type = rule.field, rule.value_type
            else:
                raise PlanStepError("metric_filter_unsupported")
            key = f"filter_{index}"
            values = item.value if item.operator == "in" else [item.value]
            if not isinstance(values, list) or not 1 <= len(values) <= 100:
                raise PlanStepError("metric_filter_list_invalid")
            for value in values:
                _validate_filter_value(value, value_type, columns[field_name])
            if item.operator == "in":
                if len(set(values)) != len(values):
                    raise PlanStepError("metric_filter_list_invalid")
                keys = [f"{key}_{position}" for position in range(len(values))]
                where.append(f"{_quote(field_name)} IN ({', '.join(':' + name for name in keys)})")
                params.update(zip(keys, values, strict=True))
            else:
                where.append(f"{_quote(field_name)} = :{key}")
                params[key] = item.value
        group = ""
        order = ""
        prefix = ""
        select = "COUNT(*) AS value"
        if plan.intent == "trend":
            local_time = (f"{business_time} AT TIME ZONE 'Asia/Shanghai'"
                          if binding.timestamp_kind == "timestamptz" else business_time)
            # grain is a typed allowlist, not a raw SQL fragment.
            bucket = f"DATE_TRUNC('{plan.grain}', {local_time})"
            prefix = f"{bucket} AS period, "
            group = f" GROUP BY {bucket}"
            order = " ORDER BY period"
        elif dimension is not None:
            assert dimension.field is not None
            identifier = _quote(dimension.field)
            if dimension.value_type == "text":
                identifier += ' COLLATE "C"'
            prefix = f"{identifier} AS dimension_id, "
            group = f" GROUP BY {identifier}"
            order = " ORDER BY dimension_id"
            if plan.intent == "ranking":
                # QueryGateway accepts a literal LIMIT. It comes exclusively from
                # the validated finite integer, never from a question or filter.
                order = f" ORDER BY value DESC NULLS LAST, dimension_id LIMIT {plan.ranking_limit}"
        if metric.ratio is not None:
            denominator = " AND ".join(_predicate_sql(item) for item in metric.ratio.denominator_predicates)
            numerator = " AND ".join(_predicate_sql(item) for item in metric.ratio.numerator_predicates)
            select = (f"COUNT(*) FILTER (WHERE {denominator} AND {numerator}) AS numerator, "
                      f"COUNT(*) FILTER (WHERE {denominator}) AS denominator")
        sql = (f"SELECT {prefix}{select} FROM {_quote(binding.schema_name)}.{_quote(binding.relation_name)}"
               f" WHERE {' AND '.join(where)}{group}")
        if metric.ratio is not None:
            output_prefix = "period, " if plan.intent == "trend" else "dimension_id, " if dimension else ""
            sql = (f"SELECT {output_prefix}numerator, denominator, "
                   "ROUND(100 * CAST(numerator AS numeric) / NULLIF(denominator, 0), 2) AS value, "
                   "CASE WHEN denominator = 0 THEN 'no_data' ELSE 'success' END AS status "
                   f"FROM ({sql}) AS metric_counts")
        sql += order
        return CompiledMetricQuery(sql, params, release.release_id, snapshot.snapshot_id,
                                   snapshot.checksum, plan, context, metric.operation, dimension)


class GatewayMetricStepRunner:
    def __init__(self, compiler: MetricQueryCompiler, gateway: QueryGateway) -> None:
        self._compiler = compiler
        self._gateway = gateway

    async def prepare(self, *, step: FetchMetricStep, query_plan: QueryPlan,
                      context: ContextBundle) -> PreparedMetricStep:
        if step.metric_keys != query_plan.metric_keys:
            raise PlanStepError("metric_step_mismatch")
        query = await self._compiler.compile(query_plan, context)
        prepared = self._gateway.prepare(query.sql)
        return PreparedMetricStep(prepared.fingerprint, 0, query)

    async def execute(self, prepared: PreparedMetricStep, *, timeout_ms: int) -> MetricStepResult:
        query = prepared.payload
        if not isinstance(query, CompiledMetricQuery):
            raise PlanStepError("metric_prepared_query_invalid")
        async with asyncio.timeout(timeout_ms / 1000):
            # Re-read active authority immediately before gateway execution.
            current = await self._compiler.compile(query.query_plan, query.context)
            if (current.sql != query.sql or current.params != query.params
                    or current.snapshot_checksum != query.snapshot_checksum
                    or current.operation != query.operation or current.dimension != query.dimension
                    or self._gateway.prepare(current.sql).fingerprint != prepared.sql_fingerprint):
                raise PlanStepError("metric_prepared_query_changed")
            result = await self._gateway.execute(current.sql, current.params)
        if not result.accepted:
            raise PlanStepError("metric_gateway_denied")
        # Reaching an explicit top-N boundary is complete for a ranking, but
        # reaching a stricter gateway cap may have discarded requested rows.
        ranking_complete = (query.query_plan.intent == "ranking"
                            and result.max_rows is not None
                            and query.query_plan.ranking_limit <= result.max_rows)
        if result.max_rows is not None and (
            result.row_count > result.max_rows
            or (result.row_count == result.max_rows and not ranking_complete)
        ):
            raise PlanStepError("metric_result_may_be_truncated")
        if result.row_count != len(result.rows):
            raise PlanStepError("metric_result_shape_invalid")
        _validate_rows(result.rows, query)
        digest = rowset_sha256(result.rows)
        # Decimal is an exact two-place JSON string; hash the typed database
        # rowset before serialization so decimal and text remain distinct.
        rows: list[JsonValue] = []
        for row in result.rows:
            json_row: dict[str, JsonValue] = {}
            for key, value in row.items():
                if type(value) is int:
                    json_row[key] = value
                elif isinstance(value, datetime):
                    json_row[key] = value.isoformat()
                elif isinstance(value, Decimal):
                    json_row[key] = format(value, ".2f")
                elif value is None or isinstance(value, str):
                    json_row[key] = value
                else:
                    raise PlanStepError("metric_result_shape_invalid")
            rows.append(json_row)
        receipt = result.execution_receipt.model_copy(update={"rowset_sha256": digest})
        output: dict[str, JsonValue] = {
            "rows": rows,
            "no_data": not rows or (query.operation == "ratio"
                                    and all(row["status"] == "no_data" for row in result.rows)),
        }
        return MetricStepResult(value=output, receipt=receipt)


def metric_plan_executor(compiler: MetricQueryCompiler, gateway: QueryGateway) -> PlanExecutor:
    """Explicit request-scoped wiring; default AppContainer remains fail closed."""
    return PlanExecutor(metric_runner=GatewayMetricStepRunner(compiler, gateway))


def _quote(identifier: str) -> str:
    # All identifiers originate from typed, deployment-controlled contracts.
    return f'"{identifier}"'


def _predicate_sql(predicate: Predicate) -> str:
    operation = {"is_true": "TRUE", "is_null": "NULL", "is_not_null": "NOT NULL"}
    return f"{_quote(predicate.field)} IS {operation[predicate.operator]}"


def _organization_scope(
    plan: QueryPlan, metric: MetricContract, binding: RelationBinding,
) -> tuple[OrganizationDimensionBinding | None, dict[str, OrganizationDimensionBinding]]:
    bindings = {item.dimension: item for item in binding.organization_dimensions}
    if any(item not in metric.supported_dimensions or item not in bindings for item in plan.dimensions):
        raise PlanStepError("metric_grain_or_dimension_unsupported")
    if len(plan.dimensions) > 1:
        raise PlanStepError("metric_dimension_combination_unsupported")
    dimension_name = next(iter(plan.dimensions), None)
    grouped = plan.intent in {"comparison", "ranking"}
    if grouped and (dimension_name is None or dimension_name == "city_company"):
        raise PlanStepError("metric_operation_unsupported")
    if not grouped and any(item != "city_company" for item in plan.dimensions):
        raise PlanStepError("metric_dimension_combination_unsupported")
    dimension = bindings[dimension_name] if grouped and dimension_name is not None else None
    organization_filters = {}
    for item in plan.filters:
        if item.field_ref in {"city_company", "area", "team"}:
            if (item.source != "entity_alias"
                    or item.field_ref == "city_company" or item.field_ref not in bindings
                    or item.field_ref not in metric.supported_dimensions):
                raise PlanStepError("metric_filter_unsupported")
            organization_filters[item.field_ref] = bindings[item.field_ref]
    scopes = set(plan.dimensions) | set(organization_filters)
    if len(scopes) > 1:
        raise PlanStepError("metric_dimension_combination_unsupported")
    # Ordinary YAML filters cannot provide a second route to physical org IDs.
    org_fields = {item.field for item in bindings.values() if item.field is not None}
    if any(item.field in org_fields | {"city_company", "area", "team"} for item in metric.filters):
        raise PlanStepError("metric_filter_unsupported")
    return dimension, organization_filters


def _validate_column_type(value_type: str, column_type: str) -> None:
    allowed = ({"text", "character varying", "varchar"} if value_type == "text"
               else {"smallint", "int2", "integer", "int4", "bigint", "int8"})
    if column_type not in allowed:
        raise PlanStepError("metric_column_type_mismatch")


def _validate_filter_value(value: Any, value_type: str, column_type: str) -> None:
    if value_type == "text":
        valid = isinstance(value, str) and 1 <= len(value) <= 256 and "\x00" not in value
    else:
        bits = 16 if column_type in {"smallint", "int2"} else 32 if column_type in {"integer", "int4"} else 64
        valid = type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
    if not valid:
        raise PlanStepError("metric_filter_type_mismatch")


def _validate_rows(rows: list[dict[str, Any]], query: CompiledMetricQuery) -> None:
    plan = query.query_plan
    expected = {"value"} if query.operation == "count" else {"numerator", "denominator", "value", "status"}
    if plan.intent == "trend":
        expected.add("period")
    if query.dimension is not None:
        expected.add("dimension_id")
    if ((plan.intent == "metric" and len(rows) != 1)
            or (plan.intent == "ranking" and len(rows) > plan.ranking_limit)):
        raise PlanStepError("metric_result_shape_invalid")
    keys: list[Any] = []
    for row in rows:
        if set(row) != expected:
            raise PlanStepError("metric_result_shape_invalid")
        value = row["value"]
        if query.operation == "count":
            if type(value) is not int or value < 0:
                raise PlanStepError("metric_result_shape_invalid")
        else:
            _validate_ratio_row(row)
        if "period" in row:
            period = row["period"]
            if (not isinstance(period, datetime) or period.tzinfo is not None
                    or period.time() != time.min
                    or (plan.grain == "month" and period.day != 1)):
                raise PlanStepError("metric_result_shape_invalid")
            first = plan.time_range.start
            if plan.grain == "month":
                first = first.replace(day=1)
            if not first <= period.date() <= plan.time_range.end:
                raise PlanStepError("metric_result_shape_invalid")
            keys.append(period)
        if query.dimension is not None:
            identifier = row["dimension_id"]
            value_type = query.dimension.value_type
            if ((value_type == "text" and (not isinstance(identifier, str) or not identifier
                                          or len(identifier) > 256 or "\x00" in identifier))
                    or (value_type == "integer" and (type(identifier) is not int
                                                     or not -(2 ** 63) <= identifier < 2 ** 63))):
                raise PlanStepError("metric_result_shape_invalid")
            keys.append(identifier)
    if len(set(keys)) != len(keys):
        raise PlanStepError("metric_result_shape_invalid")
    if plan.intent == "ranking":
        # Stable passes preserve ascending ID ties without Decimal arithmetic
        # (unary minus would round under the caller's ambient context).
        ordered = sorted(rows, key=lambda row: row["dimension_id"])
        ordered = sorted(ordered, key=lambda row: (
            row["value"] is not None, row["value"] if row["value"] is not None else 0,
        ), reverse=True)
        if rows != ordered:
            raise PlanStepError("metric_result_shape_invalid")
    elif keys != sorted(keys):
        raise PlanStepError("metric_result_shape_invalid")


def _validate_ratio_row(row: dict[str, Any]) -> None:
    numerator, denominator, value = row["numerator"], row["denominator"], row["value"]
    if (type(numerator) is not int or type(denominator) is not int
            or not 0 <= numerator <= denominator <= 2 ** 63 - 1):
        raise PlanStepError("metric_result_shape_invalid")
    if denominator == 0:
        valid = value is None and row["status"] == "no_data"
    else:
        # PostgreSQL COUNT is int8; this precision is ample for exact rounding
        # of its ratio. Never use the ambient Decimal context or binary float.
        with localcontext() as context:
            context.prec = 64
            expected = (Decimal(100) * numerator / denominator).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )
        valid = (isinstance(value, Decimal) and value.is_finite()
                 and value.as_tuple().exponent == -2
                 and value == expected and row["status"] == "success")
    if not valid:
        raise PlanStepError("metric_result_shape_invalid")
