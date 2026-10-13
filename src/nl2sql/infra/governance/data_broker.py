"""Owner-bound broker seam and its private QueryGateway execution adapter.

The authority and compiler remain injected capabilities. The repository does
not register a production authority or wire this broker into a product route.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from src.nl2sql.infra.governance.query_gateway import (
    QueryGateway,
    is_sensitive_column_name,
)
from src.nl2sql.orchestration.data_request import (
    DataRequest,
    DataRequestBudget,
    DataRequestDenied,
    DataSourceSnapshot,
    EffectiveDataScope,
    FrozenDataset,
    FrozenRow,
    GovernedSQLCandidate,
    QueryGatewayReceiptReference,
    RelationIdentity,
    ReturnedColumnIdentity,
    TrustedExecutionResult,
    VerifiedDataAuthorization,
    build_frozen_dataset,
    derive_effective_scope,
)


class DataBrokerUnavailable(RuntimeError):
    """Safe refusal when a trusted broker component fails or is absent."""

    def __init__(self) -> None:
        super().__init__("governed_data_broker_unavailable")


@dataclass(frozen=True)
class CompiledGovernedQuery:
    """Private compiler-to-executor handoff; never serialized to a sandbox."""

    candidate: GovernedSQLCandidate
    sql: str = field(repr=False)
    parameters: Mapping[str, Any] = field(repr=False)
    projections: tuple[ReturnedColumnIdentity, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


class DataAuthorizationAuthority(Protocol):
    """Server-side source of owner-bound relation, column, and row grants."""

    async def verify_request(
        self,
        request: DataRequest,
    ) -> VerifiedDataAuthorization | None: ...


class DeterministicDataCompiler(Protocol):
    """Trusted compiler; it receives typed scope and never model-authored SQL."""

    def compile_request(
        self,
        request: DataRequest,
        authorization: VerifiedDataAuthorization,
        effective_scope: EffectiveDataScope,
    ) -> CompiledGovernedQuery: ...


class GovernedDataExecutor(Protocol):
    """Trusted adapter; it alone owns QueryGateway and database sessions."""

    async def execute_candidate(
        self,
        compiled: CompiledGovernedQuery,
        budget: DataRequestBudget,
    ) -> TrustedExecutionResult: ...


class GovernedDataBroker:
    """No-default-allow orchestration for owner-bound frozen data requests."""

    def __init__(
        self,
        *,
        authority: DataAuthorizationAuthority | None = None,
        compiler: DeterministicDataCompiler | None = None,
        executor: GovernedDataExecutor | None = None,
    ) -> None:
        self._authority = authority
        self._compiler = compiler
        self._executor = executor

    async def request_dataset(self, request: DataRequest) -> FrozenDataset:
        """Issue only a complete rowset matching this owner and exact request."""

        if self._authority is None or self._compiler is None or self._executor is None:
            raise DataBrokerUnavailable()

        try:
            authorization = await self._authority.verify_request(request)
            if authorization is None:
                raise DataRequestDenied()
            effective_scope = derive_effective_scope(request, authorization)
            compiled = self._compiler.compile_request(
                request,
                authorization,
                effective_scope,
            )
            _validate_compiled_query(
                request=request,
                authorization=authorization,
                effective_scope=effective_scope,
                compiled=compiled,
            )
            execution = await self._executor.execute_candidate(compiled, request.budget)
            return build_frozen_dataset(
                request=request,
                authorization=authorization,
                candidate=compiled.candidate,
                execution=execution,
            )
        except (DataBrokerUnavailable, DataRequestDenied):
            raise
        except Exception:
            # Adapter errors may contain SQL, private scopes, or credentials.
            raise DataBrokerUnavailable() from None


class QueryGatewayDataExecutor:
    """Execute private compiled SQL through QueryGateway under request limits."""

    def __init__(self, gateway: QueryGateway) -> None:
        self._gateway = gateway

    async def execute_candidate(
        self,
        compiled: CompiledGovernedQuery,
        budget: DataRequestBudget,
    ) -> TrustedExecutionResult:
        candidate = compiled.candidate
        max_result_bytes = budget.max_bytes - _dataset_metadata_reserve(compiled)
        if max_result_bytes < 2:
            raise DataRequestDenied()

        # Re-prepare the exact private SQL at the executor boundary. The
        # candidate fingerprint is accepted only when QueryGateway derives it
        # from this SQL under the same row ceiling used for execution.
        prepared = self._gateway.prepare(
            compiled.sql,
            max_rows=budget.max_rows,
            detect_row_truncation=True,
        )
        if (
            prepared.fingerprint != candidate.query_fingerprint
            or set(prepared.parameter_names) != set(compiled.parameters)
        ):
            raise DataRequestDenied()

        receipt = await self._gateway.execute(
            compiled.sql,
            dict(compiled.parameters),
            trace_id=str(candidate.request_id),
            max_rows=budget.max_rows,
            max_result_bytes=max_result_bytes,
            max_plan_cost=budget.max_cost,
            capture_source_snapshot=True,
            detect_row_truncation=True,
        )
        snapshot = receipt.source_snapshot
        expected_source = candidate.source_snapshot
        if (
            not receipt.accepted
            or receipt.error is not None
            or receipt.execution_id is None
            or receipt.sql_fingerprint != candidate.query_fingerprint
            or receipt.policy_version != prepared.policy_version
            or receipt.policy_outcome != "allow"
            or receipt.estimated_cost is None
            or receipt.estimated_cost > budget.max_cost
            or receipt.row_count != len(receipt.rows)
            or receipt.row_count > budget.max_rows
            or receipt.masked_columns
            or snapshot is None
            or snapshot.source_id != expected_source.source_id
            or snapshot.source_version != expected_source.source_version
            or snapshot.snapshot_id == ""
            or (
                expected_source.snapshot_id is not None
                and snapshot.snapshot_id != expected_source.snapshot_id
            )
        ):
            raise DataRequestDenied()

        projection_names = tuple(item.output_name for item in compiled.projections)
        if receipt.result_columns != projection_names:
            raise DataRequestDenied()

        rows = tuple(
            FrozenRow(
                values=tuple(_normalize_data_cell(row[name]) for name in projection_names)
            )
            for row in receipt.rows
            if tuple(row) == projection_names
        )
        if len(rows) != len(receipt.rows):
            raise DataRequestDenied()

        return TrustedExecutionResult(
            status="complete",
            query_fingerprint=receipt.sql_fingerprint,
            source_snapshot=DataSourceSnapshot(
                source_id=snapshot.source_id,
                source_version=snapshot.source_version,
                snapshot_id=snapshot.snapshot_id,
            ),
            gateway_receipt=QueryGatewayReceiptReference(
                receipt_id=receipt.execution_id,
                query_fingerprint=receipt.sql_fingerprint,
                policy_version=receipt.policy_version,
                policy_outcome="allow",
                source_snapshot_id=snapshot.snapshot_id,
            ),
            returned_columns=compiled.projections,
            rows=rows,
            row_count=receipt.row_count,
            estimated_cost=receipt.estimated_cost,
        )


@dataclass(frozen=True)
class _ColumnLineage:
    relation: RelationIdentity
    column_name: str


def _validate_compiled_query(
    *,
    request: DataRequest,
    authorization: VerifiedDataAuthorization,
    effective_scope: EffectiveDataScope,
    compiled: CompiledGovernedQuery,
) -> None:
    if not isinstance(compiled, CompiledGovernedQuery):
        raise DataRequestDenied()
    candidate = compiled.candidate
    if (
        candidate.request_id != request.request_id
        or candidate.request_checksum != request.request_checksum
        or candidate.authorization_checksum != authorization.authorization_checksum
        or candidate.effective_scope != effective_scope
        or not compiled.sql.strip()
        or candidate.parameter_binding_fingerprint
        != _parameter_binding_fingerprint(compiled.parameters)
    ):
        raise DataRequestDenied()

    authorized_columns = {
        _relation_key(item.relation): set(item.columns)
        for item in authorization.authorized_scope.relations
    }
    owner_scopes = {
        _relation_key(item.relation): item for item in authorization.owner_row_scopes
    }
    row_scope_columns = {
        key: scope.owner_column for key, scope in owner_scopes.items()
    }
    if set(owner_scopes) != set(authorized_columns):
        raise DataRequestDenied()

    try:
        statements = sqlglot.parse(compiled.sql, read="postgres")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise DataRequestDenied()
        query = statements[0]
        scopes = list(traverse_scope(query))
        if not scopes:
            raise DataRequestDenied()
    except DataRequestDenied:
        raise
    except Exception:
        raise DataRequestDenied() from None

    parameters = dict(compiled.parameters)
    placeholder_names = {
        placeholder.name for placeholder in query.find_all(exp.Placeholder)
    }
    if placeholder_names != set(parameters):
        raise DataRequestDenied()

    lineages: dict[int, dict[str, _ColumnLineage]] = {}
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            # Set operations and non-SELECT scopes have unclear projection
            # lineage for this row-oriented dataset contract.
            raise DataRequestDenied()
        if (
            scope.expression.args.get("limit") is not None
            or scope.expression.args.get("fetch") is not None
            or scope.expression.args.get("offset") is not None
        ):
            raise DataRequestDenied()
        physical_tables = _physical_tables(scope)
        for alias, relation in physical_tables:
            key = _relation_key(relation)
            if key not in authorized_columns or key not in row_scope_columns:
                raise DataRequestDenied()
            if not _has_owner_row_predicate(
                scope=scope,
                alias=alias,
                owner_column=row_scope_columns[key],
                owner_user_id=request.owner_user_id,
                parameters=parameters,
            ):
                raise DataRequestDenied()

        for column in scope.columns:
            if column.is_star or column.name == "*":
                raise DataRequestDenied()
            lineage = _resolve_column_lineage(scope, column, lineages)
            if is_sensitive_column_name(lineage.column_name):
                raise DataRequestDenied()
            key = _relation_key(lineage.relation)
            allowed = authorized_columns.get(key, set())
            if lineage.column_name not in allowed and lineage.column_name != row_scope_columns.get(key):
                raise DataRequestDenied()

        projected: dict[str, _ColumnLineage] = {}
        for expression in scope.expression.selects:
            if isinstance(expression, exp.Alias):
                output_name = _identifier_value(expression.args.get("alias"))
                source_expression = expression.this
            else:
                source_expression = expression
                output_name = (
                    _identifier_value(expression.this)
                    if isinstance(expression, exp.Column)
                    else ""
                )
            if not output_name or not isinstance(source_expression, exp.Column):
                raise DataRequestDenied()
            output_key = output_name
            if output_key in projected:
                raise DataRequestDenied()
            projected[output_key] = _resolve_column_lineage(
                scope,
                source_expression,
                lineages,
            )
        lineages[id(scope)] = projected

    final_scope = scopes[-1]
    expected = {
        (
            _relation_key(relation_scope.relation),
            column_name,
        )
        for relation_scope in request.requested_scope.relations
        for column_name in relation_scope.columns
    }
    actual: set[tuple[tuple[str, str], str]] = set()
    projection_names: set[str] = set()
    if not isinstance(final_scope.expression, exp.Select):
        raise DataRequestDenied()
    select_expressions = final_scope.expression.selects
    if len(compiled.projections) != len(select_expressions):
        raise DataRequestDenied()
    for expression, projection in zip(
        select_expressions,
        compiled.projections,
        strict=True,
    ):
        if isinstance(expression, exp.Alias):
            output_name = _identifier_value(expression.args.get("alias"))
            source_expression = expression.this
        else:
            source_expression = expression
            output_name = (
                _identifier_value(expression.this)
                if isinstance(expression, exp.Column)
                else ""
            )
        if (
            not output_name
            or not isinstance(source_expression, exp.Column)
            or output_name != projection.output_name
        ):
            raise DataRequestDenied()
        lineage = _resolve_column_lineage(final_scope, source_expression, lineages)
        identity = (
            _relation_key(projection.relation),
            projection.column_name,
        )
        actual_identity = (_relation_key(lineage.relation), lineage.column_name)
        if identity != actual_identity:
            raise DataRequestDenied()
        actual.add(identity)
        projection_names.add(projection.output_name)

    if actual != expected or len(actual) != len(compiled.projections):
        raise DataRequestDenied()
    if len(projection_names) != len(compiled.projections):
        raise DataRequestDenied()


def _physical_tables(scope: Scope) -> tuple[tuple[str, RelationIdentity], ...]:
    physical: list[tuple[str, RelationIdentity]] = []
    for table in scope.tables:
        source = scope.sources.get(_table_alias(table))
        if isinstance(source, Scope):
            continue
        if not isinstance(source, exp.Table):
            raise DataRequestDenied()
        schema = _identifier_value(source.args.get("db"))
        relation = _identifier_value(source.args.get("this"))
        alias = _table_alias(source)
        if not schema or not relation or not alias:
            raise DataRequestDenied()
        physical.append(
            (
                alias,
                RelationIdentity(schema_name=schema, relation_name=relation),
            )
        )
    if not physical and any(
        not isinstance(source, Scope) for source in scope.sources.values()
    ):
        raise DataRequestDenied()
    return tuple(physical)


def _resolve_column_lineage(
    scope: Scope,
    column: exp.Column,
    lineages: dict[int, dict[str, _ColumnLineage]],
) -> _ColumnLineage:
    source_alias = _identifier_value(column.args.get("table"))
    if not source_alias:
        sources = tuple(scope.sources)
        if len(sources) != 1:
            raise DataRequestDenied()
        source_alias = sources[0]
    source = scope.sources.get(source_alias)
    column_name = _identifier_value(column.args.get("this"))
    if isinstance(source, exp.Table):
        schema = _identifier_value(source.args.get("db"))
        relation = _identifier_value(source.args.get("this"))
        if not schema or not relation or not column_name:
            raise DataRequestDenied()
        return _ColumnLineage(
            relation=RelationIdentity(schema_name=schema, relation_name=relation),
            column_name=column_name,
        )
    if isinstance(source, Scope):
        lineage = lineages.get(id(source), {}).get(column_name)
        if lineage is None:
            raise DataRequestDenied()
        return lineage
    raise DataRequestDenied()


def _has_owner_row_predicate(
    *,
    scope: Scope,
    alias: str,
    owner_column: str,
    owner_user_id: str,
    parameters: Mapping[str, Any],
) -> bool:
    where = scope.expression.args.get("where")
    if not isinstance(where, exp.Where):
        return False
    conjuncts = _conjunctive_terms(where.this)
    if conjuncts is None:
        return False
    for term in conjuncts:
        if not isinstance(term, exp.EQ):
            continue
        left = term.this
        right = term.expression
        if _matches_owner_binding(left, right, alias, owner_column, owner_user_id, parameters):
            return True
        if _matches_owner_binding(right, left, alias, owner_column, owner_user_id, parameters):
            return True
    return False


def _conjunctive_terms(expression: exp.Expr) -> list[exp.Expr] | None:
    if isinstance(expression, exp.Or):
        return None
    if isinstance(expression, exp.And):
        left = _conjunctive_terms(expression.this)
        right = _conjunctive_terms(expression.expression)
        if left is None or right is None:
            return None
        return [*left, *right]
    return [expression]


def _matches_owner_binding(
    column_expression: exp.Expr,
    parameter_expression: exp.Expr,
    alias: str,
    owner_column: str,
    owner_user_id: str,
    parameters: Mapping[str, Any],
) -> bool:
    if (
        not isinstance(column_expression, exp.Column)
        or not isinstance(parameter_expression, exp.Placeholder)
        or _identifier_value(column_expression.args.get("table")) != alias
        or _identifier_value(column_expression.args.get("this")) != owner_column
    ):
        return False
    parameter = parameters.get(parameter_expression.name)
    return isinstance(parameter, str) and parameter == owner_user_id


def _dataset_metadata_reserve(compiled: CompiledGovernedQuery) -> int:
    snapshot = compiled.candidate.source_snapshot
    reserved_snapshot = {
        "source_id": snapshot.source_id,
        "source_version": snapshot.source_version,
        "snapshot_id": "x" * 256,
    }
    payload = {
        "source_snapshot": reserved_snapshot,
        "returned_columns": [item.model_dump(mode="json") for item in compiled.projections],
        "rows": [],
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(rendered) - 2


def _normalize_data_cell(value: Any) -> str | int | float | bool | None:
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataRequestDenied()
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DataRequestDenied()
        return str(value)
    if isinstance(value, (datetime, date, UUID)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    raise DataRequestDenied()


def _parameter_binding_fingerprint(parameters: Mapping[str, Any]) -> str:
    try:
        payload = {
            key: _canonical_binding_value(value)
            for key, value in sorted(parameters.items())
        }
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except Exception:
        raise DataRequestDenied() from None
    return hashlib.sha256(rendered).hexdigest()


def _canonical_binding_value(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return {"decimal": str(value)}
    if isinstance(value, (datetime, date)):
        return {"datetime": value.isoformat()}
    if isinstance(value, UUID):
        return {"uuid": str(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_binding_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _canonical_binding_value(item)
            for key, item in sorted(value.items())
        }
    raise ValueError("query parameter value is unsupported")


def _relation_key(relation: RelationIdentity) -> tuple[str, str]:
    return relation.schema_name, relation.relation_name


def _identifier_value(identifier: Any) -> str:
    if not isinstance(identifier, exp.Identifier):
        return ""
    value = identifier.name
    return value if identifier.args.get("quoted") else value.lower()


def _table_alias(table: exp.Table) -> str:
    alias = table.args.get("alias")
    if isinstance(alias, exp.TableAlias):
        return _identifier_value(alias.args.get("this"))
    return _identifier_value(table.args.get("this"))
