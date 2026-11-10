"""Business-semantic SQL validation layered above the SQL security policy."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from src.nl2sql.infra.governance.query_gateway import PolicyEngine, QueryPolicyError
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.semantic.execution_manifest import ExpectedExecutionManifest


class SemanticSQLValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SemanticSQLValidationResult:
    sql_fingerprint: str
    relations: tuple[str, ...]
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticValidationRecord:
    manifest_checksum: str
    sql_fingerprint: str
    parameter_fingerprint: str
    outcome: str
    denial_codes: tuple[str, ...]
    validator_version: str
    relations: tuple[str, ...]
    columns: tuple[str, ...]

    @property
    def identity(self) -> str:
        return canonical_json_digest(
            {
                "manifest_checksum": self.manifest_checksum,
                "sql_fingerprint": self.sql_fingerprint,
                "parameter_fingerprint": self.parameter_fingerprint,
                "outcome": self.outcome,
                "denial_codes": self.denial_codes,
                "validator_version": self.validator_version,
            }
        )


_PLACEHOLDER_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)|\$([0-9]+)")


class SemanticSQLValidator:
    """Validate actual SQL against a typed expected execution manifest."""

    VERSION = "semantic-sql-validator-v2"

    @classmethod
    def _primary_query_scopes(
        cls, query: exp.Query, manifest: ExpectedExecutionManifest
    ) -> tuple[exp.Select, exp.Select]:
        """Return predicate and aggregate scopes; allow one exact ratio wrapper.

        Ratio SQL is emitted as an outer calculation over the single aggregate
        SELECT. Every other nested/set-query structure remains unsupported.
        """

        if not isinstance(query, exp.Select) or query.args.get("with") is not None:
            raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
        subqueries = list(query.find_all(exp.Subquery))
        if not subqueries:
            if any(
                isinstance(node, (exp.CTE, exp.Exists, exp.Union, exp.Intersect, exp.Except))
                for node in query.walk()
            ):
                raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
            return query, query
        if manifest.metric_operation != ("ratio",) or len(subqueries) != 1:
            raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
        from_clause = query.args.get("from_")
        wrapper = from_clause.this if isinstance(from_clause, exp.From) else None
        inner = wrapper.this if isinstance(wrapper, exp.Subquery) else None
        alias = wrapper.args.get("alias") if isinstance(wrapper, exp.Subquery) else None
        if (
            wrapper is not subqueries[0]
            or not isinstance(inner, exp.Select)
            or not isinstance(alias, exp.TableAlias)
            or str(alias.this).lower() != "metric_counts"
            or query.args.get("with") is not None
            or query.args.get("where") is not None
            or query.args.get("group") is not None
            or query.args.get("having") is not None
            or query.args.get("joins")
            or any(
                isinstance(node, (exp.CTE, exp.Exists, exp.Subquery, exp.Union, exp.Intersect, exp.Except))
                for node in inner.walk()
            )
            or inner.args.get("with") is not None
            or inner.args.get("joins")
            or inner.args.get("having") is not None
            or inner.args.get("limit") is not None
            or inner.args.get("offset") is not None
            or inner.args.get("order") is not None
        ):
            raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
        cls._validate_ratio_wrapper_projection(query, manifest)
        return inner, inner

    @staticmethod
    def _validate_ratio_wrapper_projection(
        query: exp.Select, manifest: ExpectedExecutionManifest
    ) -> None:
        actual: list[str] = []
        for projection in query.expressions:
            if isinstance(projection, exp.Alias):
                name = str(projection.alias).lower()
                if name not in {"value", "status"}:
                    raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
                actual.append(name)
                continue
            if isinstance(projection, exp.Column):
                name = projection.name.lower()
                if name not in {"period", "dimension_id", "numerator", "denominator"}:
                    raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
                if projection.table not in {"", "metric_counts"}:
                    raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
                actual.append(name)
                continue
            raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
        if tuple(actual) != tuple(item.lower() for item in manifest.output_aliases):
            raise SemanticSQLValidationError("semantic_sql_scope_unsupported")
        SemanticSQLValidator._validate_ratio_outer_pass_through(query, manifest)

    @staticmethod
    def _validate_ratio_outer_pass_through(
        query: exp.Select, manifest: ExpectedExecutionManifest
    ) -> None:
        """Bind outer date/dimension outputs directly to inner typed aliases."""

        expected = {
            item.alias.lower()
            for item in manifest.output_field_authority
        }
        for alias in expected:
            matches = [
                projection
                for projection in query.expressions
                if isinstance(projection, exp.Column)
                and projection.name.lower() == alias
            ]
            if len(matches) != 1:
                raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
            column = matches[0]
            if (
                column.table not in {"", "metric_counts"}
                or column.db
                or column.catalog
            ):
                raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")

    @staticmethod
    def _root_where(query: exp.Query) -> exp.Where | None:
        where = query.args.get("where")
        return where if isinstance(where, exp.Where) else None

    @staticmethod
    def _validate_single_row_shape(
        query: exp.Query, manifest: ExpectedExecutionManifest
    ) -> None:
        if manifest.cardinality_expectation != "single_row":
            return
        # An aggregate without GROUP BY already yields exactly one row even
        # when its WHERE filters every source row. HAVING can remove that row;
        # LIMIT/OFFSET can hide malformed cardinality rather than prove it.
        if (
            query.args.get("group") is not None
            or query.args.get("having") is not None
            or query.args.get("offset") is not None
            or query.args.get("limit") is not None
        ):
            raise SemanticSQLValidationError("semantic_sql_cardinality_mismatch")

    def __init__(
        self,
        *,
        policy: PolicyEngine | None = None,
        require_complete_authority: bool = False,
    ) -> None:
        self._policy = policy
        self._require_complete_authority = require_complete_authority

    def validate(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        manifest: ExpectedExecutionManifest | None = None,
        *,
        prepared: Any | None = None,
        expected_step_id: str | None = None,
        expected_relation_ids: Iterable[str] = (),
        required_columns: Iterable[str] = (),
    ) -> SemanticValidationRecord | SemanticSQLValidationResult:
        if manifest is None:
            return self._legacy_validate(
                sql,
                expected_relation_ids=expected_relation_ids,
                required_columns=required_columns,
            )
        try:
            manifest = ExpectedExecutionManifest.model_validate(
                manifest.model_dump(mode="python")
            )
        except (TypeError, ValueError) as exc:
            raise SemanticSQLValidationError(
                "semantic_sql_manifest_authority_invalid"
            ) from exc
        if not isinstance(sql, str) or not sql.strip():
            raise SemanticSQLValidationError("semantic_sql_parse_failed")
        if self._require_complete_authority:
            if (
                manifest.manifest_version != "1.1"
                or manifest.semantic_release_checksum is None
                or manifest.schema_snapshot_checksum is None
                or manifest.fetch_step_id is None
                or manifest.fetch_query_plan_sha256 is None
                or len(manifest.metric_authorities) != 1
            ):
                raise SemanticSQLValidationError(
                    "semantic_sql_snapshot_authority_missing"
                )
            authority = manifest.metric_authorities[0]
            if (
                expected_step_id is None
                or manifest.fetch_step_id != expected_step_id
                or authority.fetch_step_id != expected_step_id
            ):
                raise SemanticSQLValidationError(
                    "semantic_sql_fetch_step_authority_mismatch"
                )
        parameters = dict(params or {})
        try:
            statements = sqlglot.parse(sql, read="postgres")
            if len(statements) != 1 or not isinstance(statements[0], exp.Query):
                raise SemanticSQLValidationError("semantic_sql_parse_failed")
            query = statements[0]
        except SemanticSQLValidationError:
            raise
        except Exception as exc:
            raise SemanticSQLValidationError("semantic_sql_parse_failed") from exc

        relations = self._physical_relations(query)
        expected_relations = {
            self._normalize_relation(value) for value in manifest.relation_identity
        }
        if expected_relations and relations != expected_relations:
            raise SemanticSQLValidationError("semantic_sql_relation_mismatch")
        # Join identities are not yet backed by a typed relation-pair and
        # cardinality proof. A nonempty label in a manifest is not sufficient
        # authority to admit a join.
        if any(isinstance(node, exp.Join) for node in query.walk()):
            raise SemanticSQLValidationError("semantic_sql_join_cardinality_mismatch")
        if manifest.cardinality_expectation == "single_row" and query.args.get("group") is not None:
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        if any(item == "result_limit" for item in manifest.ordering_expectations):
            limit = query.args.get("limit")
            if limit is None:
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            expected_limit = next(
                (item for item in manifest.ordering_expectations if item != "result_limit"),
                None,
            )
            if expected_limit is not None and limit.expression.sql() != expected_limit:
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")

        if any(isinstance(node, exp.Parameter) for node in query.walk()):
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
        placeholders = tuple(sorted(str(node.name) for node in query.find_all(exp.Placeholder)))
        expected_placeholders = tuple(sorted(manifest.placeholder_identities))
        if expected_placeholders and placeholders != expected_placeholders:
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
        if expected_placeholders and set(parameters) != set(expected_placeholders):
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
        if set(parameters) != set(placeholders):
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
        parameter_fingerprint = canonical_json_digest(parameters)
        legacy_parameter_fingerprint = canonical_json_digest(
            {"names": placeholders, "values": parameters}
        )
        if (
            manifest.parameter_binding_fingerprint
            and parameter_fingerprint != manifest.parameter_binding_fingerprint
            and legacy_parameter_fingerprint != manifest.parameter_binding_fingerprint
        ):
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
        if (
            manifest.expected_parameter_binding_fingerprints
            and parameter_fingerprint not in manifest.expected_parameter_binding_fingerprints
        ):
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")

        if manifest.expected_parameter_bindings:
            expected_parameters = self._expected_parameters(manifest)
            if set(expected_parameters) != set(parameters):
                raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
            if any(
                self._normal_value(parameters[name])
                != self._normal_value(expected_parameters[name])
                for name in expected_parameters
            ):
                raise SemanticSQLValidationError("semantic_sql_parameter_mismatch")
        columns = tuple(
            sorted({column.name.lower() for column in query.find_all(exp.Column) if column.name})
        )
        predicate_scope, aggregation_scope = self._primary_query_scopes(query, manifest)
        self._validate_single_row_shape(query, manifest)
        if aggregation_scope is not query:
            self._validate_single_row_shape(aggregation_scope, manifest)
        self._validate_time(predicate_scope, manifest, parameters)
        self._validate_predicate_authority(manifest)
        self._validate_predicates(predicate_scope, manifest, parameters)
        self._validate_aggregation(query, manifest, aggregation_scope=aggregation_scope)
        self._validate_grouping(aggregation_scope, manifest)
        self._validate_bound_output_fields(aggregation_scope, manifest)
        self._validate_column_authority(aggregation_scope, manifest)
        self._validate_output(query, manifest)
        self._validate_ranking_order(query, manifest)
        sql_fingerprint = (
            prepared.fingerprint
            if prepared is not None and getattr(prepared, "fingerprint", None)
            else hashlib.sha256(sql.encode("utf-8")).hexdigest()
        )
        if manifest.expected_sql_fingerprints and sql_fingerprint not in manifest.expected_sql_fingerprints:
            raise SemanticSQLValidationError("semantic_sql_source_strategy_mismatch")
        return SemanticValidationRecord(
            manifest_checksum=manifest.checksum,
            sql_fingerprint=sql_fingerprint,
            parameter_fingerprint=parameter_fingerprint,
            outcome="allow",
            denial_codes=(),
            validator_version=self.VERSION,
            relations=tuple(sorted(relations)),
            columns=columns,
        )

    def _legacy_validate(
        self,
        sql: str,
        *,
        expected_relation_ids: Iterable[str],
        required_columns: Iterable[str],
    ) -> SemanticSQLValidationResult:
        try:
            prepared_sql = self._policy.prepare(sql).sql if self._policy is not None else sql
        except QueryPolicyError as exc:
            raise SemanticSQLValidationError(f"security:{exc.code}") from exc
        try:
            query = sqlglot.parse_one(prepared_sql, read="postgres")
        except Exception as exc:
            raise SemanticSQLValidationError("semantic_sql_parse_failed") from exc
        if not isinstance(query, exp.Query):
            raise SemanticSQLValidationError("semantic_sql_not_query")
        expected = {self._normalize_relation(item) for item in expected_relation_ids}
        actual = self._physical_relations(query)
        if not actual:
            raise SemanticSQLValidationError("semantic_sql_relation_missing")
        if not actual <= expected:
            raise SemanticSQLValidationError("semantic_sql_relation_unbound")
        required = {item.strip().lower() for item in required_columns if item.strip()}
        projected = {column.name.lower() for column in query.find_all(exp.Column) if column.name}
        if required - projected:
            raise SemanticSQLValidationError("semantic_sql_required_column_missing")
        return SemanticSQLValidationResult(
            sql_fingerprint=hashlib.sha256(prepared_sql.encode("utf-8")).hexdigest(),
            relations=tuple(sorted(actual)),
            columns=tuple(sorted(projected)),
        )

    @classmethod
    def _physical_relations(cls, query: exp.Query) -> set[str]:
        relations: set[str] = set()
        for scope in traverse_scope(query):
            for source in scope.selected_sources.values():
                if not isinstance(source, tuple) or len(source) != 2:
                    continue
                node, resolved = source
                if not isinstance(node, exp.Table) or not isinstance(resolved, exp.Table):
                    continue
                name = str(resolved.name).strip('"').lower()
                schema = str(resolved.db).strip('"').lower() if resolved.db else ""
                relations.add(f"{schema}.{name}" if schema else name)
        return relations

    @staticmethod
    def _placeholders(sql: str) -> set[str]:
        return {first or second for first, second in _PLACEHOLDER_RE.findall(sql)}

    @staticmethod
    def _normalize_relation(value: str) -> str:
        return value.replace('"', '').strip().lower()

    @staticmethod
    def _validate_time(
        query: exp.Query,
        manifest: ExpectedExecutionManifest,
        parameters: Mapping[str, Any],
    ) -> None:
        if not manifest.time_column:
            return
        names = {column.name.lower() for column in query.find_all(exp.Column) if column.name}
        if manifest.time_column[0].lower() not in names:
            raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        where = SemanticSQLValidator._root_where(query)
        if where is None:
            raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        time_column = manifest.time_column[0].lower()
        lower_ok = False
        upper_ok = False
        lower_placeholder: str | None = None
        upper_placeholder: str | None = None
        for node in where.walk():
            if isinstance(node, exp.GTE) and isinstance(node.left, exp.Column) and node.left.name.lower() == time_column:
                lower_ok = True
                if isinstance(node.right, exp.Placeholder):
                    lower_placeholder = str(node.right.name)
            if isinstance(node, exp.LT) and isinstance(node.left, exp.Column) and node.left.name.lower() == time_column:
                upper_ok = True
                if isinstance(node.right, exp.Placeholder):
                    upper_placeholder = str(node.right.name)
            if isinstance(node, exp.LTE) and isinstance(node.left, exp.Column) and node.left.name.lower() == time_column:
                raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        if not lower_ok or not upper_ok:
            raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        if manifest.time_bounds and manifest.time_bounds[0].startswith(":") and lower_placeholder != manifest.time_bounds[0][1:]:
            raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        if len(manifest.time_bounds) > 1 and manifest.time_bounds[1].startswith(":") and upper_placeholder != manifest.time_bounds[1][1:]:
            raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        if manifest.expected_time_values:
            actual = []
            for name in ("start_at", "end_at"):
                if name not in parameters:
                    raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
                actual.append(SemanticSQLValidator._normal_value(parameters[name]))
            if tuple(actual) != tuple(manifest.expected_time_values[:2]):
                raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
        if manifest.time_bounds:
            rendered = query.sql(dialect="postgres")
            placeholder_names = {
                str(node.name) for node in query.find_all(exp.Placeholder)
            }
            for bound in manifest.time_bounds[:2]:
                if bound.startswith(":") and bound[1:] not in {
                    str(node.name) for node in query.find_all(exp.Placeholder)
                }:
                    raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")
                if (
                    not bound.startswith(":")
                    and not placeholder_names
                    and bound not in rendered
                ):
                    raise SemanticSQLValidationError("semantic_sql_time_predicate_mismatch")

    @classmethod
    def _validate_predicates(
        cls,
        query: exp.Query,
        manifest: ExpectedExecutionManifest,
        parameters: Mapping[str, Any],
    ) -> None:
        if manifest.expected_predicates:
            expected = {
                cls._canonical_json(json.loads(item))
                for item in manifest.expected_predicates
            }
            where = cls._root_where(query)
            if where is None:
                raise SemanticSQLValidationError("semantic_sql_predicate_mismatch")
            actual_items: list[dict[str, object]] = []
            for node in cls._flatten_and(where.this):
                parsed = cls._predicates_from_ast(node, parameters, manifest)
                if parsed is None:
                    if (
                        isinstance(node, exp.EQ)
                        and isinstance(node.left, exp.Literal)
                        and isinstance(node.right, exp.Literal)
                    ):
                        raise SemanticSQLValidationError(
                            "semantic_sql_authorization_mismatch"
                        )
                    raise SemanticSQLValidationError("semantic_sql_predicate_mismatch")
                actual_items.extend(parsed)
            actual = {cls._canonical_json(item) for item in actual_items}
            if len(actual) != len(actual_items):
                raise SemanticSQLValidationError("semantic_sql_predicate_mismatch")
            if actual != expected:
                raise SemanticSQLValidationError("semantic_sql_predicate_mismatch")
            return
        where = cls._root_where(query)
        if where is not None:
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_missing")
        if manifest.expected_predicates or manifest.eligibility_predicates or manifest.authorization_predicate_fingerprint:
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_missing")

    @classmethod
    def _predicates_from_ast(
        cls,
        node: Any,
        parameters: Mapping[str, Any],
        manifest: ExpectedExecutionManifest,
    ) -> list[dict[str, object]] | None:
        authority = manifest.sparse_date_authority
        if (
            isinstance(node, exp.In)
            and authority is not None
            and isinstance(node.this, exp.Date)
        ):
            date_expression = node.this
            source = date_expression.this
            if authority.timestamp_kind == "timestamptz":
                if not isinstance(source, exp.AtTimeZone):
                    return None
                zone = source.args.get("zone")
                if not isinstance(zone, exp.Literal) or zone.this != authority.timezone:
                    return None
                source = source.this
            elif isinstance(source, exp.AtTimeZone):
                return None
            if (
                not isinstance(source, exp.Column)
                or source.name.lower() != authority.column.lower()
                or source.table
                or source.db
                or source.catalog
            ):
                return None
            expressions = tuple(node.expressions)
            if (
                len(expressions) != len(authority.parameters)
                or any(
                    not isinstance(item, exp.Placeholder)
                    for item in expressions
                )
                or tuple(str(item.name) for item in expressions)
                != authority.parameters
            ):
                return None
            for name, expected_date in zip(
                authority.parameters, authority.dates, strict=True
            ):
                value = parameters.get(name)
                if isinstance(value, datetime) or not isinstance(value, date):
                    return None
                if value != expected_date:
                    return None
            return [
                {
                    "field": "__available_date__",
                    "operator": "eq",
                    "value": value.isoformat(),
                    "parameter": name,
                }
                for name, value in zip(
                    authority.parameters, authority.dates, strict=True
                )
            ]
        predicate = cls._predicate_from_ast(node, parameters)
        return [predicate] if predicate is not None else None

    @classmethod
    def _validate_column_authority(
        cls, query: exp.Select, manifest: ExpectedExecutionManifest
    ) -> None:
        authorities = manifest.relation_column_authority
        if len(authorities) != 1:
            raise SemanticSQLValidationError("semantic_sql_column_authority_mismatch")
        authority = authorities[0]
        expected_relations = {
            cls._normalize_relation(item) for item in manifest.relation_identity
        }
        if (
            cls._normalize_relation(authority.relation_id) not in expected_relations
            or not expected_relations
        ):
            raise SemanticSQLValidationError("semantic_sql_column_authority_mismatch")
        allowed = {item.lower() for item in authority.columns}
        names = {
            column.name.lower()
            for column in query.find_all(exp.Column)
            if column.name
        }
        if not allowed or names - allowed:
            raise SemanticSQLValidationError("semantic_sql_column_authority_mismatch")

    @classmethod
    def _validate_bound_output_fields(
        cls, query: exp.Select, manifest: ExpectedExecutionManifest
    ) -> None:
        authorities = {item.alias: item for item in manifest.output_field_authority}
        if len(authorities) != len(manifest.output_field_authority):
            raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
        expected_aliases = {
            name.lower()
            for name in manifest.output_aliases
            if name.lower() in {"period", "dimension_id"}
        }
        if set(authorities) != expected_aliases:
            raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
        relation_authorities = manifest.relation_column_authority
        if authorities and len(relation_authorities) != 1:
            raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
        if authorities:
            relation_authority = relation_authorities[0]
            allowed_columns = {item.lower() for item in relation_authority.columns}
            for authority in authorities.values():
                if (
                    cls._normalize_relation(authority.relation_id)
                    != cls._normalize_relation(relation_authority.relation_id)
                    or authority.field.lower() not in allowed_columns
                ):
                    raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
        projections: dict[str, Any] = {}
        for projection in query.expressions:
            if isinstance(projection, exp.Alias) and projection.alias:
                key = projection.alias.lower()
                if key in projections:
                    raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
                projections[key] = projection.this
        period_authority = authorities.get("period")
        if period_authority is not None:
            expression = projections.get("period")
            expected = cls._expected_period_expression(query, manifest)
            if expression is None or expected is None or expression != expected:
                raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")
        dimension_authority = authorities.get("dimension_id")
        if dimension_authority is not None:
            expression = projections.get("dimension_id")
            column: exp.Column | None = None
            if dimension_authority.logical_type == "text":
                if (
                    isinstance(expression, exp.Collate)
                    and isinstance(expression.this, exp.Column)
                    and isinstance(expression.args.get("expression"), exp.Identifier)
                    and expression.args["expression"].name == dimension_authority.collation
                ):
                    column = expression.this
            elif dimension_authority.logical_type == "integer" and isinstance(
                expression, exp.Column
            ):
                column = expression
            if (
                column is None
                or column.name.lower() != dimension_authority.field.lower()
                or column.table
                or column.db
                or column.catalog
            ):
                raise SemanticSQLValidationError("semantic_sql_output_authority_mismatch")

    @classmethod
    def _validate_ranking_order(
        cls, query: exp.Query, manifest: ExpectedExecutionManifest
    ) -> None:
        declared_limit = "result_limit" in manifest.ordering_expectations
        if not declared_limit and not manifest.ranking_order_authority:
            return
        if (
            not manifest.ranking_order_authority
            or manifest.ranking_limit is None
            or query.args.get("offset") is not None
        ):
            raise SemanticSQLValidationError("semantic_sql_output_mismatch")
        order = query.args.get("order")
        nodes = list(order.expressions) if isinstance(order, exp.Order) else []
        if len(nodes) != len(manifest.ranking_order_authority):
            raise SemanticSQLValidationError("semantic_sql_output_mismatch")
        for node, expected in zip(
            nodes, manifest.ranking_order_authority, strict=True
        ):
            if not isinstance(node, exp.Ordered) or not isinstance(
                node.this, exp.Column
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            column = node.this
            direction = "desc" if node.args.get("desc") is True else "asc"
            nulls_first = node.args.get("nulls_first")
            if nulls_first is None:
                nulls_first = direction == "desc"
            nulls = "first" if nulls_first else "last"
            if (
                column.name.lower() != expected.alias
                or column.table
                or column.db
                or column.catalog
                or direction != expected.direction
                or nulls != expected.nulls
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
        limit = query.args.get("limit")
        expression = limit.expression if isinstance(limit, exp.Limit) else None
        if (
            not isinstance(expression, exp.Literal)
            or expression.is_string
            or not str(expression.this).isdigit()
            or int(expression.this) != manifest.ranking_limit
        ):
            raise SemanticSQLValidationError("semantic_sql_output_mismatch")

    @classmethod
    def _validate_predicate_authority(cls, manifest: ExpectedExecutionManifest) -> None:
        """Prove role-tagged typed predicates project to the exact SQL authority."""

        if not manifest.predicate_authority:
            if manifest.metric_operation == ("ratio",) or manifest.expected_predicates:
                raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch")
            return
        root_roles = {
            "business_time", "sparse_availability", "request_filter",
            "metric_base", "eligibility", "authorization", "aggregate_freshness",
            "dimension_presence",
        }
        ratio_roles = {"ratio_denominator", "ratio_numerator_condition"}
        root_projection: list[str] = []
        root_role_authority: list[dict[str, object]] = []
        denominator_authority: list[dict[str, object]] = []
        numerator_conditions: list[dict[str, object]] = []
        try:
            for raw in manifest.predicate_authority:
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise ValueError("predicate authority item is not an object")
                role = item.get("role")
                field = item.get("field")
                operator = item.get("operator")
                if not isinstance(role, str) or role not in root_roles | ratio_roles:
                    raise ValueError("predicate authority role is unknown")
                if not isinstance(field, str) or not field or not isinstance(operator, str):
                    raise ValueError("predicate authority field/operator is invalid")
                allowed_operators_by_role = {
                    "business_time": {"gte", "lt"},
                    "sparse_availability": {"eq"},
                    "request_filter": {"eq", "ne", "gt", "gte", "lt", "lte", "in"},
                    "metric_base": {"is_true", "is_false", "is_null", "is_not_null"},
                    "eligibility": {"is_true", "is_false", "is_null", "is_not_null"},
                    "authorization": {"eq", "in"},
                    "aggregate_freshness": {"eq"},
                    "dimension_presence": {"is_not_null"},
                    "ratio_denominator": {"is_true", "is_false", "is_null", "is_not_null"},
                    "ratio_numerator_condition": {"is_true", "is_false", "is_null", "is_not_null"},
                }
                allowed_operators = allowed_operators_by_role.get(role)
                if allowed_operators is None or operator not in allowed_operators:
                    raise ValueError("predicate authority operator is invalid for its role")
                if role in ratio_roles and set(item) != {"role", "field", "operator"}:
                    raise ValueError("ratio predicate authority has unsupported value fields")
                projection = {key: value for key, value in item.items() if key != "role"}
                if role in root_roles:
                    root_projection.append(cls._canonical_json(projection))
                    root_role_authority.append(item)
                elif role == "ratio_denominator":
                    denominator_authority.append(projection)
                else:
                    numerator_conditions.append(projection)
        except (TypeError, ValueError) as exc:
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch") from exc

        expected_root = {
            cls._canonical_json(json.loads(item))
            for item in manifest.expected_predicates
        }
        if (
            len(root_projection) != len(set(root_projection))
            or set(root_projection) != expected_root
        ):
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch")
        authorized_fields = {
            str(item["field"]).lower()
            for item in root_role_authority
            if item.get("role") == "authorization"
        }
        if set(item.lower() for item in manifest.authorization_predicate_fields) - authorized_fields:
            raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")
        requested_fields = {
            str(item["field"]).lower()
            for item in root_role_authority
            if item.get("role") == "request_filter"
        }
        if set(item.lower() for item in manifest.requested_filter_fields) - requested_fields:
            raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")

        if manifest.metric_operation == ("ratio",):
            expected_denominator = cls._manifest_predicate_payloads(
                manifest.ratio_denominator_predicates
            )
            expected_numerator = cls._manifest_predicate_payloads(
                manifest.ratio_numerator_predicates
            )
            denominator_keys = {
                cls._canonical_json(item) for item in denominator_authority
            }
            numerator_condition_keys = {
                cls._canonical_json(item) for item in numerator_conditions
            }
            expected_denominator_keys = {
                cls._canonical_json(item) for item in expected_denominator
            }
            expected_numerator_keys = {
                cls._canonical_json(item) for item in expected_numerator
            }
            # The full numerator authority is exactly denominator population
            # plus numerator-only conditions. Duplicate or overlapping fields
            # fail closed rather than being normalized through set conversion.
            denominator_fields = [str(item.get("field", "")).lower() for item in denominator_authority]
            numerator_fields = [str(item.get("field", "")).lower() for item in numerator_conditions]
            if (
                len(denominator_fields) != len(set(denominator_fields))
                or len(numerator_fields) != len(set(numerator_fields))
                or set(denominator_fields) & set(numerator_fields)
                or denominator_keys != expected_denominator_keys
                or expected_numerator_keys != denominator_keys | numerator_condition_keys
            ):
                raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch")
        elif denominator_authority or numerator_conditions:
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch")

    @classmethod
    def _manifest_predicate_payloads(
        cls, values: tuple[str, ...]
    ) -> list[dict[str, object]]:
        try:
            payloads = [json.loads(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch") from exc
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("field"), str)
            or item.get("operator") not in {"is_true", "is_false", "is_null", "is_not_null"}
            or set(item) != {"field", "operator"}
            for item in payloads
        ):
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch")
        fields = [str(item["field"]).lower() for item in payloads]
        if len(fields) != len(set(fields)):
            raise SemanticSQLValidationError("semantic_sql_predicate_authority_mismatch")
        return payloads

    @classmethod
    def _validate_aggregation(
        cls,
        query: exp.Query,
        manifest: ExpectedExecutionManifest,
        *,
        aggregation_scope: exp.Select,
    ) -> None:
        output_aliases = {
            str(alias.alias).lower(): alias.this
            for alias in query.expressions
            if isinstance(alias, exp.Alias) and alias.alias
        }
        aggregate_aliases = {
            str(alias.alias).lower(): alias.this
            for alias in aggregation_scope.expressions
            if isinstance(alias, exp.Alias)
            if alias.alias
        }
        if manifest.metric_operation and manifest.metric_operation[0] == "count":
            value_expression = aggregate_aliases.get("value")
            if value_expression is None or not cls._is_count_or_approved_sum(value_expression, manifest):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
        if manifest.metric_operation and manifest.metric_operation[0] == "ratio":
            numerator = aggregate_aliases.get("numerator")
            denominator = aggregate_aliases.get("denominator")
            value = output_aliases.get("value")
            status = output_aliases.get("status")
            if numerator is None or denominator is None or value is None or status is None:
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not cls._ratio_roles_match(numerator, denominator, manifest):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not cls._ratio_value_matches(value):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not cls._ratio_status_matches(status):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if any(isinstance(node, exp.Count) and bool(node.args.get("distinct")) for node in query.walk()):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if manifest.source_kind != ("approved_aggregate",) and not all(
                isinstance(node, exp.Filter) for node in (numerator, denominator)
            ):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
        if manifest.source_kind == ("approved_aggregate",) and not any(
            isinstance(node, (exp.Sum, exp.Count)) for node in query.walk()
        ):
            raise SemanticSQLValidationError("semantic_sql_source_strategy_mismatch")
        if manifest.source_kind == ("approved_detail",) and manifest.metric_operation == ("count",) and not any(
            isinstance(node, exp.Count) for node in query.walk()
        ):
            raise SemanticSQLValidationError("semantic_sql_source_strategy_mismatch")
        if manifest.aggregate_identity and not any(isinstance(node, exp.AggFunc) for node in query.walk()):
            raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")

    @classmethod
    def _is_count_or_approved_sum(cls, expression: Any, manifest: ExpectedExecutionManifest) -> bool:
        if isinstance(expression, exp.Count):
            return cls._count_input_matches(expression, manifest.count_column)
        if manifest.source_kind == ("approved_aggregate",):
            return bool(
                manifest.aggregate_value_column
                and cls._coalesced_bigint_sum_matches(
                    expression, manifest.aggregate_value_column
                )
            )
        return False

    @staticmethod
    def _count_input_matches(count: exp.Count, count_column: str | None) -> bool:
        if count.args.get("distinct"):
            return False
        count_input = count.this
        if count_column is None:
            return isinstance(count_input, exp.Star)
        return (
            isinstance(count_input, exp.Column)
            and count_input.name.lower() == count_column.lower()
        )

    @classmethod
    def _coalesced_bigint_sum_matches(cls, expression: Any, column: str) -> bool:
        """Require exactly CAST(COALESCE(SUM(bound_column), 0) AS BIGINT)."""

        if not isinstance(expression, exp.Cast):
            return False
        target = expression.args.get("to")
        if not isinstance(target, exp.DataType) or target.sql().lower() != "bigint":
            return False
        coalesce = expression.this
        if not isinstance(coalesce, exp.Coalesce):
            return False
        defaults = coalesce.args.get("expressions") or []
        if len(defaults) != 1 or SemanticSQLValidator._literal_value(defaults[0]) != 0:
            return False
        total = coalesce.this
        return (
            isinstance(total, exp.Sum)
            and isinstance(total.this, exp.Column)
            and total.this.name.lower() == column.lower()
        )

    @classmethod
    def _ratio_roles_match(cls, numerator: Any, denominator: Any, manifest: ExpectedExecutionManifest) -> bool:
        if manifest.source_kind == ("approved_aggregate",):
            # Aggregate facts are only accepted when the authority explicitly
            # names the additive numerator and denominator columns.  The SQL
            # projection must sum those exact roles.
            num_column = manifest.ratio_aggregate_numerator_column
            den_column = manifest.ratio_aggregate_denominator_column
            return bool(
                num_column
                and den_column
                and cls._coalesced_bigint_sum_matches(numerator, num_column)
                and cls._coalesced_bigint_sum_matches(denominator, den_column)
            )

        numerator_predicates = cls._ratio_filter_predicates(
            numerator, count_column=manifest.count_column
        )
        denominator_predicates = cls._ratio_filter_predicates(
            denominator, count_column=manifest.count_column
        )
        if numerator_predicates is None or denominator_predicates is None:
            return False
        expected_num = cls._manifest_predicate_set(manifest.ratio_numerator_predicates)
        expected_den = cls._manifest_predicate_set(manifest.ratio_denominator_predicates)
        if not expected_num or not expected_den:
            return False
        # The metric contract declares numerator-only conditions; the complete
        # numerator population must preserve every denominator condition.
        if not expected_den <= expected_num:
            return False
        if denominator_predicates != expected_den or numerator_predicates != expected_num:
            return False
        expected_num_roles = {item.lower() for item in manifest.ratio_numerator_roles}
        expected_den_roles = {item.lower() for item in manifest.ratio_denominator_roles}
        return expected_num_roles <= {field for field, _ in numerator_predicates} and (
            expected_den_roles <= {field for field, _ in denominator_predicates}
        )

    @classmethod
    def _ratio_filter_predicates(
        cls, expression: Any, *, count_column: str | None
    ) -> set[tuple[str, str]] | None:
        """Return the exact field/operator set for a COUNT FILTER expression."""

        if not isinstance(expression, exp.Filter):
            return None
        count = expression.this
        where = expression.args.get("expression")
        if not isinstance(count, exp.Count) or not cls._count_input_matches(
            count, count_column
        ):
            return None
        if not isinstance(where, exp.Where):
            return None
        actual: set[tuple[str, str]] = set()
        for node in cls._flatten_and(where.this):
            predicate = cls._predicate_from_ast(node)
            if predicate is None:
                return None
            field = predicate.get("field")
            operator = predicate.get("operator")
            if not isinstance(field, str) or not isinstance(operator, str):
                return None
            actual.add((field.lower(), operator))
        return actual

    @staticmethod
    def _manifest_predicate_set(values: tuple[str, ...]) -> set[tuple[str, str]]:
        try:
            result: set[tuple[str, str]] = set()
            for raw in values:
                item = json.loads(raw)
                if not isinstance(item, dict):
                    return set()
                field, operator = item.get("field"), item.get("operator")
                # Ratio predicates are closed boolean/null predicates. A value
                # predicate in this authority surface is unsupported.
                if not isinstance(field, str) or operator not in {
                    "is_true", "is_false", "is_null", "is_not_null",
                }:
                    return set()
                result.add((field.lower(), operator))
            return result
        except (TypeError, ValueError):
            return set()

    @classmethod
    def _ratio_value_matches(cls, expression: Any) -> bool:
        """Require value = NULL on denominator zero, else ROUND(100*n/d, 2)."""

        case = expression.this if isinstance(expression, exp.Alias) else expression
        if not isinstance(case, exp.Case):
            return False
        ifs = case.args.get("ifs") or []
        if len(ifs) != 1:
            return False
        branch = ifs[0]
        if not cls._is_zero_denominator(branch.args.get("this")):
            return False
        if not isinstance(branch.args.get("true"), exp.Null):
            return False
        rounded = case.args.get("default")
        if not isinstance(rounded, exp.Round) or cls._literal_value(rounded.args.get("decimals")) != 2:
            return False
        division = rounded.this
        if not isinstance(division, exp.Div):
            return False
        if cls._column_name(division.expression) != "denominator":
            return False
        product = division.this
        if not isinstance(product, exp.Mul):
            return False
        left, right = product.this, product.expression
        if cls._literal_value(left) == 100:
            scaled = right
        elif cls._literal_value(right) == 100:
            scaled = left
        else:
            return False
        if not isinstance(scaled, exp.Cast) or cls._column_name(scaled.this) != "numerator":
            return False
        data_type = scaled.args.get("to")
        type_name = data_type.sql().lower() if isinstance(data_type, exp.DataType) else ""
        return "numeric" in type_name or "decimal" in type_name

    @classmethod
    def _ratio_status_matches(cls, expression: Any) -> bool:
        case = expression.this if isinstance(expression, exp.Alias) else expression
        if not isinstance(case, exp.Case):
            return False
        ifs = case.args.get("ifs") or []
        if len(ifs) != 1:
            return False
        branch = ifs[0]
        return (
            cls._is_zero_denominator(branch.args.get("this"))
            and isinstance(branch.args.get("true"), exp.Literal)
            and branch.args["true"].is_string
            and branch.args["true"].this == "calculation_error"
            and isinstance(case.args.get("default"), exp.Literal)
            and case.args["default"].is_string
            and case.args["default"].this == "success"
        )

    @classmethod
    def _is_zero_denominator(cls, expression: Any) -> bool:
        if not isinstance(expression, exp.EQ):
            return False
        left, right = expression.this, expression.expression
        return (
            cls._column_name(left) == "denominator" and cls._literal_value(right) == 0
        ) or (
            cls._column_name(right) == "denominator" and cls._literal_value(left) == 0
        )

    @classmethod
    def _ratio_zero_guard(cls, value: Any, status: Any) -> bool:
        def guarded(expr: Any) -> bool:
            for case in expr.find_all(exp.Case):
                ifs = case.args.get("ifs") or []
                if not ifs:
                    continue
                condition = ifs[0].args.get("this")
                if isinstance(condition, exp.EQ) and cls._column_name(condition.left) == "denominator" and cls._literal_value(condition.right) == 0:
                    return True
            return False
        return guarded(value) and guarded(status)

    @classmethod
    def _validate_grouping(
        cls, query: exp.Query, manifest: ExpectedExecutionManifest
    ) -> None:
        expected_dimensions = {
            value.lower() for value in manifest.grouping_dimensions
        }
        period_expression = cls._expected_period_expression(query, manifest)
        group = query.args.get("group")
        expressions = list(group.expressions) if group is not None else []
        if not expressions:
            if expected_dimensions or period_expression is not None:
                raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
            return

        actual_dimensions: list[str] = []
        period_count = 0
        for expression in expressions:
            if period_expression is not None and expression == period_expression:
                period_count += 1
                continue
            column: exp.Column | None = None
            if isinstance(expression, exp.Column):
                column = expression
            elif (
                isinstance(expression, exp.Collate)
                and isinstance(expression.this, exp.Column)
            ):
                # Text dimensions are ordered under the compiler's frozen C
                # collation. The authority is still the underlying bound
                # dimension field.
                collation = expression.args.get("expression")
                if (
                    not isinstance(collation, exp.Identifier)
                    or collation.name.lower() != "c"
                ):
                    raise SemanticSQLValidationError(
                        "semantic_sql_grouping_mismatch"
                    )
                column = expression.this
            if column is None:
                raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
            actual_dimensions.append(column.name.lower())

        if (
            set(actual_dimensions) != expected_dimensions
            or len(actual_dimensions) != len(set(actual_dimensions))
            or period_count != (1 if period_expression is not None else 0)
        ):
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")

    @classmethod
    def _expected_period_expression(
        cls, query: exp.Query, manifest: ExpectedExecutionManifest
    ) -> Any | None:
        if "period" not in {item.lower() for item in manifest.output_aliases}:
            return None
        type_by_alias = {
            alias.lower(): logical_type.lower()
            for alias, logical_type in zip(
                manifest.output_aliases, manifest.output_logical_types, strict=False
            )
        }
        field_authority = next(
            (item for item in manifest.output_field_authority if item.alias == "period"),
            None,
        )
        if (
            type_by_alias.get("period") != "date"
            or field_authority is None
            or field_authority.kind != "time_bucket"
            or field_authority.logical_type != "date"
            or not field_authority.grain
            or not field_authority.timezone
            or not field_authority.timestamp_kind
            or not manifest.grain
            or not manifest.time_column
            or field_authority.field.lower() != manifest.time_column[0].lower()
            or field_authority.grain.lower() != manifest.grain[0].lower()
        ):
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        period_aliases = [
            item
            for item in query.expressions
            if isinstance(item, exp.Alias) and item.alias.lower() == "period"
        ]
        if len(period_aliases) != 1:
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        expression = period_aliases[0].this
        if not isinstance(expression, exp.TimestampTrunc):
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        unit = expression.args.get("unit")
        if not isinstance(unit, exp.Var) or unit.this.lower() != field_authority.grain.lower():
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        base_time = expression.this
        if field_authority.timestamp_kind == "timestamptz":
            if not isinstance(base_time, exp.AtTimeZone):
                raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
            zone = base_time.args.get("zone")
            if not isinstance(zone, exp.Literal) or zone.this != field_authority.timezone:
                raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
            base_time = base_time.this
        elif isinstance(base_time, exp.AtTimeZone):
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        if (
            not isinstance(base_time, exp.Column)
            or base_time.name.lower() != field_authority.field.lower()
            or base_time.table
            or base_time.db
            or base_time.catalog
        ):
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")
        return expression

    @classmethod
    def _validate_output(
        cls, query: exp.Query, manifest: ExpectedExecutionManifest
    ) -> None:
        if not manifest.output_aliases:
            return
        projections = list(query.expressions)
        names: list[str] = []
        projection_by_name: dict[str, Any] = {}
        for projection in projections:
            if isinstance(projection, exp.Alias) and projection.alias:
                name = projection.alias.lower()
                expression = projection.this
            elif isinstance(projection, exp.Column):
                name = projection.name.lower()
                expression = projection
            else:
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            if name in projection_by_name:
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            names.append(name)
            projection_by_name[name] = expression
        expected_names = [item.lower() for item in manifest.output_aliases]
        if names != expected_names or len(manifest.output_logical_types) != len(names):
            raise SemanticSQLValidationError("semantic_sql_output_mismatch")
        for alias_name, logical_type in zip(
            manifest.output_aliases,
            manifest.output_logical_types,
            strict=True,
        ):
            expression_node = projection_by_name[alias_name.lower()]
            expression = expression_node.sql(dialect="postgres").lower()
            ratio_period_passthrough = (
                logical_type == "date"
                and alias_name.lower() == "period"
                and manifest.metric_operation == ("ratio",)
                and isinstance(expression_node, exp.Column)
            )
            if (
                logical_type == "date"
                and "date_trunc" not in expression
                and not ratio_period_passthrough
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            if logical_type == "numeric" and not any(
                token in expression for token in ("round(", "numeric", "decimal")
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            if logical_type == "text" and alias_name.lower() == "status" and not any(
                token in expression for token in ("'success'", "'calculation_error'")
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")


    @staticmethod
    def _expected_parameters(manifest: ExpectedExecutionManifest) -> dict[str, object]:
        try:
            return {
                str(item["name"]): item["value"]
                for item in (json.loads(raw) for raw in manifest.expected_parameter_bindings)
            }
        except (TypeError, ValueError, KeyError) as exc:
            raise SemanticSQLValidationError("semantic_sql_parameter_mismatch") from exc

    @staticmethod
    def _normal_value(value: object) -> object:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (list, tuple)):
            return [SemanticSQLValidator._normal_value(item) for item in value]
        return value

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)

    @classmethod
    def _flatten_and(cls, node: Any) -> tuple[Any, ...]:
        if isinstance(node, exp.And):
            return (*cls._flatten_and(node.left), *cls._flatten_and(node.right))
        return (node,)

    @staticmethod
    def _column_name(node: object) -> str | None:
        return node.name.lower() if isinstance(node, exp.Column) else None

    @classmethod
    def _literal_value(cls, node: object, params: Mapping[str, Any] | None = None) -> object:
        if isinstance(node, exp.Placeholder):
            return (params or {}).get(str(node.name))
        if isinstance(node, exp.Literal):
            if node.is_string:
                return node.this
            try:
                return int(node.this)
            except (TypeError, ValueError):
                try:
                    return float(node.this)
                except (TypeError, ValueError):
                    return node.this
        if isinstance(node, exp.Boolean):
            return bool(node.this)
        if isinstance(node, exp.Null):
            return None
        return None

    @classmethod
    def _predicate_from_ast(cls, node: Any, params: Mapping[str, Any] | None = None) -> dict[str, object] | None:
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
            field = cls._column_name(node.this.this)
            return {"field": field, "operator": "is_not_null"} if field is not None else None
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GTE, exp.GT, exp.LTE, exp.LT)):
            field = cls._column_name(node.left)
            if field is None:
                return None
            op = {exp.EQ: "eq", exp.NEQ: "ne", exp.GTE: "gte", exp.GT: "gt", exp.LTE: "lte", exp.LT: "lt"}[type(node)]
            right = node.right
            value = cls._literal_value(right, params)
            payload: dict[str, object] = {"field": field, "operator": op, "value": cls._normal_value(value)}
            if isinstance(right, exp.Placeholder):
                payload["parameter"] = str(right.name)
            return payload
        if isinstance(node, exp.In):
            field = cls._column_name(node.this)
            if field is None:
                return None
            values = [cls._normal_value(cls._literal_value(item, params)) for item in node.expressions]
            parameters = [str(item.name) for item in node.expressions if isinstance(item, exp.Placeholder)]
            return {"field": field, "operator": "in", "value": values, "parameter": ",".join(parameters)}
        if isinstance(node, exp.Is):
            field = cls._column_name(node.this)
            if field is None:
                return None
            expression = node.expression
            if isinstance(expression, exp.Boolean):
                return {"field": field, "operator": "is_true" if expression.this else "is_false"}
            if isinstance(expression, exp.Null):
                return {"field": field, "operator": "is_null"}
        return None


__all__ = [
    "SemanticSQLValidationError",
    "SemanticSQLValidationResult",
    "SemanticValidationRecord",
    "SemanticSQLValidator",
]
