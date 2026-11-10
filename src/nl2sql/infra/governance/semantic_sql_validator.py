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

    def __init__(self, *, policy: PolicyEngine | None = None) -> None:
        self._policy = policy

    def validate(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        manifest: ExpectedExecutionManifest | None = None,
        *,
        prepared: Any | None = None,
        expected_relation_ids: Iterable[str] = (),
        required_columns: Iterable[str] = (),
    ) -> SemanticValidationRecord | SemanticSQLValidationResult:
        if manifest is None:
            return self._legacy_validate(
                sql,
                expected_relation_ids=expected_relation_ids,
                required_columns=required_columns,
            )
        if not isinstance(sql, str) or not sql.strip():
            raise SemanticSQLValidationError("semantic_sql_parse_failed")
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
        if not manifest.join_identities and any(isinstance(node, exp.Join) for node in query.walk()):
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
        self._validate_time(query, manifest, parameters)
        self._validate_predicates(query, manifest, parameters)
        self._validate_aggregation(query, manifest)
        self._validate_grouping(query, manifest)
        self._validate_output(query, manifest)
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
        time_column = manifest.time_column[0].lower()
        lower_ok = False
        upper_ok = False
        lower_placeholder: str | None = None
        upper_placeholder: str | None = None
        for node in query.walk():
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
            actual = {
                cls._canonical_json(item)
                for where in query.find_all(exp.Where)
                if not isinstance(where.parent, exp.Filter)
                for node in cls._flatten_and(where.this)
                for item in [cls._predicate_from_ast(node, parameters)]
                if item is not None
            }
            if actual != expected:
                raise SemanticSQLValidationError("semantic_sql_predicate_mismatch")
            return
        where = query.args.get("where")
        if where is not None and any(
            isinstance(node, exp.EQ)
            and isinstance(node.left, exp.Literal)
            and isinstance(node.right, exp.Literal)
            for node in where.walk()
        ):
            raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")
        if not manifest.eligibility_predicates and not manifest.authorization_predicate_fingerprint:
            return
        if where is None:
            if manifest.authorization_predicate_fingerprint:
                raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")
            raise SemanticSQLValidationError("semantic_sql_eligibility_missing")
        rendered = where.sql(dialect="postgres").lower()
        for predicate in manifest.eligibility_predicates:
            parts = predicate.split(":")
            field = (parts[-2] if len(parts) >= 3 else parts[0]).lower()
            if field and field not in rendered:
                raise SemanticSQLValidationError("semantic_sql_eligibility_missing")
        for field in manifest.authorization_predicate_fields:
            if field.lower() not in rendered:
                raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")
        for field in manifest.requested_filter_fields:
            if field.lower() not in rendered:
                raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")
        if manifest.authorization_predicate_fingerprint and not any(
            isinstance(node, (exp.In, exp.EQ)) for node in where.walk()
        ):
            raise SemanticSQLValidationError("semantic_sql_authorization_mismatch")

    @classmethod
    def _validate_aggregation(cls, query: exp.Query, manifest: ExpectedExecutionManifest) -> None:
        aliases = {
            str(alias.alias).lower(): alias.this
            for alias in query.find_all(exp.Alias)
            if alias.alias
        }
        if manifest.metric_operation and manifest.metric_operation[0] == "count":
            value_expression = aliases.get("value")
            if value_expression is None or not cls._is_count_or_approved_sum(value_expression, manifest):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
        if manifest.metric_operation and manifest.metric_operation[0] == "ratio":
            numerator = aliases.get("numerator")
            denominator = aliases.get("denominator")
            value = aliases.get("value")
            status = aliases.get("status")
            if numerator is None or denominator is None or value is None or status is None:
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not cls._ratio_roles_match(numerator, denominator, manifest):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not any(isinstance(node, exp.Div) for node in value.walk()):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not any(isinstance(node, exp.Round) for node in value.walk()):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            round_nodes = list(value.find_all(exp.Round))
            if not round_nodes or cls._literal_value(round_nodes[0].args.get("decimals")) != 2:
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not any(isinstance(node, exp.Case) for node in value.walk()) or not any(
                isinstance(node, exp.Case) for node in status.walk()
            ):
                raise SemanticSQLValidationError("semantic_sql_aggregate_mismatch")
            if not cls._ratio_zero_guard(value, status):
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

    @staticmethod
    def _is_count_or_approved_sum(expression: Any, manifest: ExpectedExecutionManifest) -> bool:
        if isinstance(expression, exp.Count):
            return not bool(expression.args.get("distinct"))
        if manifest.source_kind == ("approved_aggregate",) and isinstance(expression, (exp.Cast, exp.Sum)):
            return any(isinstance(node, exp.Sum) for node in expression.walk())
        return False

    @classmethod
    def _ratio_roles_match(cls, numerator: Any, denominator: Any, manifest: ExpectedExecutionManifest) -> bool:
        expected_num = {item.lower() for item in manifest.ratio_numerator_roles}
        expected_den = {item.lower() for item in manifest.ratio_denominator_roles}
        if isinstance(numerator, exp.Filter) and isinstance(denominator, exp.Filter):
            num_fields = {column.name.lower() for column in numerator.find_all(exp.Column)}
            den_fields = {column.name.lower() for column in denominator.find_all(exp.Column)}
            # The numerator is the denominator population plus its success
            # predicate.  A denominator that only contains the success field
            # or a numerator that drops the denominator predicate is a
            # different business calculation even when both use COUNT.
            return expected_den <= num_fields and expected_den <= den_fields and expected_num <= num_fields
        return manifest.source_kind == ("approved_aggregate",)

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

    @staticmethod
    def _validate_grouping(query: exp.Query, manifest: ExpectedExecutionManifest) -> None:
        expected = {value.lower() for value in manifest.grouping_dimensions}
        if not expected:
            return
        group = query.args.get("group")
        actual = {
            item.name.lower()
            for item in (group.expressions if group is not None else ())
            if isinstance(item, exp.Column)
        }
        if actual != expected:
            raise SemanticSQLValidationError("semantic_sql_grouping_mismatch")

    @staticmethod
    def _validate_output(query: exp.Query, manifest: ExpectedExecutionManifest) -> None:
        if not manifest.output_aliases:
            return
        alias_nodes = {
            str(alias.alias).lower(): alias
            for alias in query.find_all(exp.Alias)
            if alias.alias
        }
        aliases = set(alias_nodes)
        if set(aliases) != set(manifest.output_aliases):
            raise SemanticSQLValidationError("semantic_sql_output_mismatch")
        for alias_name, logical_type in zip(
            manifest.output_aliases,
            manifest.output_logical_types,
            strict=False,
        ):
            alias = alias_nodes.get(alias_name.lower())
            if alias is None:
                continue
            expression = alias.this.sql(dialect="postgres").lower()
            if logical_type == "date" and "date_trunc" not in expression:
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            if logical_type == "numeric" and not any(
                token in expression for token in ("round(", "numeric", "decimal")
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")
            if logical_type == "text" and alias_name.lower() == "status" and not any(
                token in expression for token in ("'success'", "'calculation_error'")
            ):
                raise SemanticSQLValidationError("semantic_sql_output_mismatch")

        if manifest.ordering_expectations and "result_limit" in manifest.ordering_expectations:
            limit = query.args.get("limit")
            expected = next((item for item in manifest.ordering_expectations if item != "result_limit"), None)
            if limit is None or (expected is not None and limit.expression.sql() != expected):
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
