from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.config.settings import AgentConfig
from src.nl2sql.infra.governance.data_broker import (
    CompiledGovernedQuery,
    DataBrokerUnavailable,
    GovernedDataBroker,
    SqlGlotGovernedDataCompiler,
    _parameter_binding_fingerprint,
    _validate_compiled_query,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.orchestration.data_request import (
    AuthorizedDataScope,
    AuthorizedJoinScope,
    DataRequest,
    DataRequestBudget,
    DataRequestDenied,
    DataRequestInput,
    DataSourceSnapshot,
    EffectiveDataScope,
    FrozenDataset,
    FrozenRow,
    GovernedSQLCandidate,
    OwnerRowScope,
    QueryGatewayReceiptReference,
    RelationColumnScope,
    RelationIdentity,
    RequestedDataScope,
    ReturnedColumnIdentity,
    TrustedExecutionResult,
    VerifiedDataAuthorization,
    bind_data_request_owner,
    build_frozen_dataset,
    derive_effective_scope,
)
from src.nl2sql.orchestration.p5_runtime_readiness import assess_p5_runtime_readiness

REQUEST_ID = UUID("00000000-0000-4000-8000-000000000002")
RECEIPT_ID = UUID("00000000-0000-4000-8000-000000000003")
QUERY_FINGERPRINT = "a" * 64


def _scope(
    *,
    schema_name: str = "analytics",
    relation_name: str = "sales",
    columns: tuple[str, ...] = ("amount", "region"),
) -> RequestedDataScope:
    return RequestedDataScope(
        relations=(
            RelationColumnScope(
                relation=RelationIdentity(
                    schema_name=schema_name,
                    relation_name=relation_name,
                ),
                columns=columns,
            ),
        )
    )


def _request(
    *,
    owner: str = "owner-7",
    scope: RequestedDataScope | None = None,
    budget: DataRequestBudget | None = None,
) -> DataRequest:
    request_input = DataRequestInput(
        request_id=REQUEST_ID,
        requested_scope=scope or _scope(),
        budget=budget or DataRequestBudget(max_rows=20, max_bytes=50_000, max_cost=25_000.0),
    )
    return bind_data_request_owner(request_input, owner_user_id=owner)


def _authorization(
    request: DataRequest,
    *,
    owner: str | None = None,
    scope: AuthorizedDataScope | None = None,
    authorized_joins: tuple[AuthorizedJoinScope, ...] = (),
) -> VerifiedDataAuthorization:
    authorized_scope = scope or AuthorizedDataScope(relations=request.requested_scope.relations)
    return VerifiedDataAuthorization(
        schema_version=(
            "data-authorization-v2" if authorized_joins else "data-authorization-v1"
        ),
        request_id=request.request_id,
        request_checksum=request.request_checksum,
        owner_user_id=owner or request.owner_user_id,
        authority_id="synthetic-test-authority",
        authorization_revision="test-auth-rev-1",
        authorized_scope=authorized_scope,
        owner_row_scopes=tuple(
            OwnerRowScope(relation=item.relation, owner_column="owner_id")
            for item in authorized_scope.relations
        ),
        authorized_joins=authorized_joins,
    )


def test_deterministic_compiler_binds_owner_and_emits_typed_projection() -> None:
    request = _request()
    authorization = _authorization(request)
    effective_scope = derive_effective_scope(request, authorization)
    gateway = QueryGateway(MagicMock(), schema="analytics")

    compiled = SqlGlotGovernedDataCompiler(
        gateway,
        source_id="test-source",
        source_version="fixture-v1",
    ).compile_request(request, authorization, effective_scope)

    assert 'WHERE "source_row"."owner_id" = :owner_user_id' in compiled.sql
    assert compiled.parameters == {"owner_user_id": "owner-7"}
    assert compiled.candidate.compiler_id == "ttai-sqlglot-data-request-compiler"
    assert compiled.candidate.source_snapshot == DataSourceSnapshot(
        source_id="test-source",
        source_version="fixture-v1",
    )
    assert compiled.projections == (
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
            column_name="amount",
            output_name="amount",
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
            column_name="region",
            output_name="region",
        ),
    )


def test_deterministic_compiler_rejects_ambiguous_authorized_join_shapes() -> None:
    sales = RelationIdentity(schema_name="analytics", relation_name="sales")
    customers = RelationIdentity(schema_name="analytics", relation_name="customers")
    scope = RequestedDataScope(
        relations=(
            RelationColumnScope(relation=sales, columns=("amount", "customer_id")),
            RelationColumnScope(relation=customers, columns=("customer_id", "name")),
        )
    )
    request = _request(scope=scope)
    joins = (
        AuthorizedJoinScope(
            left_relation=sales,
            left_column="customer_id",
            right_relation=customers,
            right_column="customer_id",
            join_type="inner",
        ),
        AuthorizedJoinScope(
            left_relation=sales,
            left_column="customer_id",
            right_relation=customers,
            right_column="customer_id",
            join_type="left",
        ),
    )
    authorization = _authorization(
        request,
        scope=AuthorizedDataScope(relations=scope.relations),
        authorized_joins=joins,
    )
    effective_scope = derive_effective_scope(request, authorization)
    compiler = SqlGlotGovernedDataCompiler(
        QueryGateway(MagicMock(), schema="analytics"),
        source_id="test-source",
        source_version="fixture-v1",
    )

    with pytest.raises(DataRequestDenied):
        compiler.compile_request(request, authorization, effective_scope)


def _candidate(
    request: DataRequest,
    authorization: VerifiedDataAuthorization,
    *,
    query_fingerprint: str = QUERY_FINGERPRINT,
    effective_scope: EffectiveDataScope | None = None,
    request_checksum: str | None = None,
    authorization_checksum: str | None = None,
) -> GovernedSQLCandidate:
    resolved_scope = effective_scope or derive_effective_scope(request, authorization)
    return GovernedSQLCandidate(
        schema_version=(
            "governed-sql-candidate-v2"
            if resolved_scope.authorized_joins
            else "governed-sql-candidate-v1"
        ),
        request_id=request.request_id,
        request_checksum=request_checksum or request.request_checksum,
        authorization_checksum=(
            authorization_checksum or authorization.authorization_checksum
        ),
        compiler_id="test-compiler",
        compiler_version="contract-test-v1",
        effective_scope=resolved_scope,
        source_snapshot=DataSourceSnapshot(
            source_id="test-source",
            source_version="snapshot-version-4",
            snapshot_id="snapshot-0004",
        ),
        query_fingerprint=query_fingerprint,
        parameter_binding_fingerprint=_parameter_binding_fingerprint(
            {"owner_user_id": request.owner_user_id}
        ),
    )


def _execution(
    candidate: GovernedSQLCandidate,
    *,
    status: str = "complete",
    source: DataSourceSnapshot | None = None,
    receipt: QueryGatewayReceiptReference | None = None,
    columns: tuple[ReturnedColumnIdentity, ...] | None = None,
    rows: tuple[FrozenRow, ...] = (FrozenRow(values=(12.5, "north")),),
    row_count: int | None = None,
    estimated_cost: float = 12.0,
) -> TrustedExecutionResult:
    actual_columns = columns
    if actual_columns is None:
        actual_columns = tuple(
            ReturnedColumnIdentity(
                relation=relation_scope.relation,
                column_name=column_name,
                output_name=column_name,
            )
            for relation_scope in candidate.effective_scope.relations
            for column_name in relation_scope.columns
        )
    return TrustedExecutionResult(
        status=status,  # type: ignore[arg-type]
        query_fingerprint=candidate.query_fingerprint,
        source_snapshot=source or candidate.source_snapshot,
        gateway_receipt=receipt or QueryGatewayReceiptReference(
            receipt_id=RECEIPT_ID,
            query_fingerprint=candidate.query_fingerprint,
            policy_version="query-gateway-test-v1",
            policy_outcome="allow",
            source_snapshot_id=candidate.source_snapshot.snapshot_id,
        ),
        returned_columns=actual_columns,
        rows=rows,
        row_count=len(rows) if row_count is None else row_count,
        estimated_cost=estimated_cost,
    )


def _frozen_dataset(
    *,
    request: DataRequest | None = None,
    rows: tuple[FrozenRow, ...] = (FrozenRow(values=(12.5, "north")),),
    scope: RequestedDataScope | None = None,
) -> FrozenDataset:
    request = request or _request(scope=scope)
    authorization = _authorization(request)
    candidate = _candidate(request, authorization)
    execution = _execution(candidate, rows=rows)
    return build_frozen_dataset(
        request=request,
        authorization=authorization,
        candidate=candidate,
        execution=execution,
    )


@pytest.mark.asyncio
async def test_missing_trusted_authority_fails_closed_without_live_execution() -> None:
    request = _request()

    with pytest.raises(DataBrokerUnavailable) as error:
        await GovernedDataBroker().request_dataset(request)

    assert str(error.value) == "governed_data_broker_unavailable"


def test_client_owner_or_scope_does_not_authorize_a_request() -> None:
    request = _request(owner="owner-a")
    mismatched = _authorization(request, owner="owner-b")

    with pytest.raises(DataRequestDenied) as error:
        derive_effective_scope(request, mismatched)

    assert str(error.value) == "governed_data_request_denied"


def test_requested_relation_or_column_outside_grant_is_denied_without_partial_scope() -> None:
    request = _request()
    partial_grant = _authorization(
        request,
        scope=AuthorizedDataScope(
            relations=(
                RelationColumnScope(
                    relation=RelationIdentity(
                        schema_name="analytics",
                        relation_name="sales",
                    ),
                    columns=("amount",),
                ),
            )
        ),
    )

    with pytest.raises(DataRequestDenied):
        derive_effective_scope(request, partial_grant)


@pytest.mark.asyncio
async def test_missing_compiler_or_executor_never_issues_a_dataset() -> None:
    request = _request()

    with pytest.raises(DataBrokerUnavailable):
        await GovernedDataBroker().request_dataset(request)


@pytest.mark.asyncio
async def test_synthetic_contract_path_requires_authority_compiler_and_complete_execution() -> None:
    data_request = _request()
    verified_authorization = _authorization(data_request)
    expected_scope = derive_effective_scope(data_request, verified_authorization)
    candidate = _candidate(
        data_request,
        verified_authorization,
        effective_scope=expected_scope,
    )
    execution = _execution(candidate)
    calls: list[str] = []

    class SyntheticAuthority:
        async def verify_request(
            self,
            request: DataRequest,
        ) -> VerifiedDataAuthorization:
            calls.append("authority")
            assert request.request_id == data_request.request_id
            return verified_authorization

    class SyntheticCompiler:
        def compile_request(
            self,
            request: DataRequest,
            authorization: VerifiedDataAuthorization,
            effective_scope: EffectiveDataScope,
        ) -> CompiledGovernedQuery:
            calls.append("compiler")
            assert request == data_request
            assert authorization == verified_authorization
            assert effective_scope == expected_scope
            return CompiledGovernedQuery(
                candidate=candidate,
                sql=(
                    "SELECT s.amount AS amount, s.region AS region "
                    "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id"
                ),
                parameters={"owner_user_id": data_request.owner_user_id},
                projections=tuple(
                    ReturnedColumnIdentity(
                        relation=relation_scope.relation,
                        column_name=column_name,
                        output_name=column_name,
                    )
                    for relation_scope in effective_scope.relations
                    for column_name in relation_scope.columns
                ),
            )

    class SyntheticExecutor:
        async def execute_candidate(
            self,
            compiled: CompiledGovernedQuery,
            budget: DataRequestBudget,
        ) -> TrustedExecutionResult:
            calls.append("executor")
            assert compiled.candidate == candidate
            assert budget == data_request.budget
            return execution

    dataset = await GovernedDataBroker(
        authority=SyntheticAuthority(),
        compiler=SyntheticCompiler(),
        executor=SyntheticExecutor(),
    ).request_dataset(data_request)

    assert calls == ["authority", "compiler", "executor"]
    assert dataset.status == "complete"


def test_compiled_sql_requires_authorized_join_key_and_rejects_extra_filters() -> None:
    requested = RequestedDataScope(
        relations=(
            RelationColumnScope(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                columns=("sale_id", "amount"),
            ),
            RelationColumnScope(
                relation=RelationIdentity(schema_name="analytics", relation_name="customers"),
                columns=("display_name",),
            ),
        )
    )
    request = _request(owner="owner-7", scope=requested)
    authorization = _authorization(
        request,
        scope=AuthorizedDataScope(
            relations=(
                RelationColumnScope(
                    relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                    columns=("sale_id", "amount", "customer_id"),
                ),
                RelationColumnScope(
                    relation=RelationIdentity(schema_name="analytics", relation_name="customers"),
                    columns=("customer_id", "display_name"),
                ),
            )
        ),
        authorized_joins=(
            AuthorizedJoinScope(
                left_relation=RelationIdentity(
                    schema_name="analytics", relation_name="sales"
                ),
                left_column="customer_id",
                right_relation=RelationIdentity(
                    schema_name="analytics", relation_name="customers"
                ),
                right_column="customer_id",
                join_type="inner",
            ),
        ),
    )
    scope = derive_effective_scope(request, authorization)
    parameters = {"owner_user_id": request.owner_user_id}
    candidate = _candidate(request, authorization, effective_scope=scope)
    projections = (
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
            column_name="sale_id",
            output_name="sale_id",
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
            column_name="amount",
            output_name="amount",
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="customers"),
            column_name="display_name",
            output_name="customer_name",
        ),
    )
    sql = (
        "WITH owner_sales AS ("
        "SELECT s.sale_id AS sale_key, s.customer_id AS customer_key, s.amount AS amount "
        "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id) "
        "SELECT os.sale_key AS sale_id, os.amount AS amount, "
        "c.display_name AS customer_name "
        "FROM owner_sales AS os "
        "JOIN analytics.customers AS c "
        "ON c.customer_id = os.customer_key "
        "WHERE c.owner_id = :owner_user_id"
    )
    compiled = CompiledGovernedQuery(
        candidate=candidate,
        sql=sql,
        parameters=parameters,
        projections=projections,
    )

    _validate_compiled_query(
        request=request,
        authorization=authorization,
        effective_scope=scope,
        compiled=compiled,
    )

    no_join_authority = _authorization(
        request,
        scope=authorization.authorized_scope,
    )
    assert no_join_authority.authorization_checksum != authorization.authorization_checksum
    no_join_scope = derive_effective_scope(request, no_join_authority)
    no_join_compiled = CompiledGovernedQuery(
        candidate=_candidate(request, no_join_authority, effective_scope=no_join_scope),
        sql=sql,
        parameters=parameters,
        projections=projections,
    )
    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=no_join_authority,
            effective_scope=no_join_scope,
            compiled=no_join_compiled,
        )

    unscoped_join = CompiledGovernedQuery(
        candidate=candidate,
        sql=sql.replace(" WHERE c.owner_id = :owner_user_id", ""),
        parameters=parameters,
        projections=projections,
    )
    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=scope,
            compiled=unscoped_join,
        )

    filtered_derived_join = CompiledGovernedQuery(
        candidate=candidate,
        sql=(
            "WITH owner_sales AS ("
            "SELECT s.sale_id AS sale_key, s.customer_id AS customer_key, "
            "s.amount AS amount FROM analytics.sales AS s "
            "WHERE s.owner_id = :owner_user_id), "
            "owner_customers AS ("
            "SELECT c.customer_id AS customer_key, c.display_name AS customer_name "
            "FROM analytics.customers AS c WHERE c.owner_id = :owner_user_id) "
            "SELECT os.sale_key AS sale_id, os.amount AS amount, "
            "oc.customer_name AS customer_name FROM owner_sales AS os "
            "JOIN owner_customers AS oc "
            "ON os.customer_key = oc.customer_key AND oc.customer_name <> 'Bob'"
        ),
        parameters=parameters,
        projections=projections,
    )
    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=scope,
            compiled=filtered_derived_join,
        )

    mismatched_join_key = CompiledGovernedQuery(
        candidate=candidate,
        sql=(
            "SELECT s.sale_id AS sale_id, s.amount AS amount, "
            "c.display_name AS customer_name "
            "FROM analytics.sales AS s "
            "JOIN analytics.customers AS c ON s.sale_id = c.customer_id "
            "WHERE s.owner_id = :owner_user_id "
            "AND c.owner_id = :owner_user_id"
        ),
        parameters=parameters,
        projections=projections,
    )
    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=scope,
            compiled=mismatched_join_key,
        )

    left_join_with_on_only_scopes = CompiledGovernedQuery(
        candidate=candidate,
        sql=(
            "SELECT s.sale_id AS sale_id, s.amount AS amount, "
            "c.display_name AS customer_name "
            "FROM analytics.sales AS s "
            "LEFT JOIN analytics.customers AS c "
            "ON c.customer_id = s.customer_id "
            "AND s.owner_id = :owner_user_id "
            "AND c.owner_id = :owner_user_id"
        ),
        parameters=parameters,
        projections=projections,
    )
    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=scope,
            compiled=left_join_with_on_only_scopes,
        )


def test_compiled_sql_accepts_complete_single_relation_cte() -> None:
    request = _request()
    authorization = _authorization(request)
    scope = derive_effective_scope(request, authorization)
    compiled = CompiledGovernedQuery(
        candidate=_candidate(request, authorization, effective_scope=scope),
        sql=(
            "WITH owner_sales AS ("
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id) "
            "SELECT owner_sales.amount AS amount, owner_sales.region AS region "
            "FROM owner_sales"
        ),
        parameters={"owner_user_id": request.owner_user_id},
        projections=(
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="amount",
                output_name="amount",
            ),
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="region",
                output_name="region",
            ),
        ),
    )

    _validate_compiled_query(
        request=request,
        authorization=authorization,
        effective_scope=scope,
        compiled=compiled,
    )


def test_left_join_owner_scope_preserves_rows_through_a_prefiltered_cte() -> None:
    request = _request(
        scope=RequestedDataScope(
            relations=(
                RelationColumnScope(
                    relation=RelationIdentity(
                        schema_name="analytics",
                        relation_name="sales",
                    ),
                    columns=("sale_id", "amount"),
                ),
                RelationColumnScope(
                    relation=RelationIdentity(
                        schema_name="analytics",
                        relation_name="customers",
                    ),
                    columns=("display_name",),
                ),
            )
        )
    )
    authorized_scope = AuthorizedDataScope(
        relations=(
            RelationColumnScope(
                relation=RelationIdentity(
                    schema_name="analytics",
                    relation_name="sales",
                ),
                columns=("sale_id", "amount", "customer_id"),
            ),
            RelationColumnScope(
                relation=RelationIdentity(
                    schema_name="analytics",
                    relation_name="customers",
                ),
                columns=("customer_id", "display_name"),
            ),
        )
    )
    join_rule = AuthorizedJoinScope(
        left_relation=RelationIdentity(
            schema_name="analytics",
            relation_name="sales",
        ),
        left_column="customer_id",
        right_relation=RelationIdentity(
            schema_name="analytics",
            relation_name="customers",
        ),
        right_column="customer_id",
        join_type="left",
    )
    authorization = _authorization(
        request,
        scope=authorized_scope,
        authorized_joins=(join_rule,),
    )
    scope = derive_effective_scope(request, authorization)
    parameters = {"owner_user_id": request.owner_user_id}
    projections = (
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
            column_name="sale_id",
            output_name="sale_id",
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
            column_name="amount",
            output_name="amount",
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="customers"),
            column_name="display_name",
            output_name="customer_name",
        ),
    )

    nullable_physical_side = CompiledGovernedQuery(
        candidate=_candidate(request, authorization, effective_scope=scope),
        sql=(
            "SELECT s.sale_id AS sale_id, s.amount AS amount, "
            "c.display_name AS customer_name "
            "FROM analytics.sales AS s "
            "LEFT JOIN analytics.customers AS c "
            "ON s.customer_id = c.customer_id "
            "WHERE s.owner_id = :owner_user_id "
            "AND c.owner_id = :owner_user_id"
        ),
        parameters=parameters,
        projections=projections,
    )
    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=scope,
            compiled=nullable_physical_side,
        )

    prefitered_right_side = CompiledGovernedQuery(
        candidate=_candidate(request, authorization, effective_scope=scope),
        sql=(
            "WITH owner_customers AS ("
            "SELECT c.customer_id AS customer_key, c.display_name AS customer_name "
            "FROM analytics.customers AS c WHERE c.owner_id = :owner_user_id) "
            "SELECT s.sale_id AS sale_id, s.amount AS amount, "
            "oc.customer_name AS customer_name FROM analytics.sales AS s "
            "LEFT JOIN owner_customers AS oc "
            "ON s.customer_id = oc.customer_key "
            "WHERE s.owner_id = :owner_user_id"
        ),
        parameters=parameters,
        projections=projections,
    )
    _validate_compiled_query(
        request=request,
        authorization=authorization,
        effective_scope=scope,
        compiled=prefitered_right_side,
    )


@pytest.mark.parametrize(
    ("sql", "parameter_owner"),
    [
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.payroll AS s WHERE s.owner_id = :owner_user_id",
            "owner-7",
        ),
        (
            "SELECT s.secret AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id",
            "owner-7",
        ),
        (
            'SELECT s."Amount" AS amount, s.region AS region '
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id",
            "owner-7",
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            'FROM analytics."Sales" AS s WHERE s.owner_id = :owner_user_id',
            "owner-7",
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id OR TRUE",
            "owner-7",
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s "
            "WHERE s.owner_id = :owner_user_id AND FALSE",
            "owner-7",
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s "
            "WHERE s.owner_id = :owner_user_id AND s.amount > 100",
            "owner-7",
        ),
        (
            "SELECT DISTINCT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id",
            "owner-7",
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id "
            "GROUP BY s.amount, s.region",
            "owner-7",
        ),
        (
            "WITH owner_sales AS ("
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id) "
            "SELECT os.amount AS amount, os.region AS region FROM owner_sales AS os "
            "WHERE os.amount > 100",
            "owner-7",
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id",
            "owner-else",
        ),
    ],
)
def test_compiled_sql_rejects_unapproved_relation_column_or_owner_scope(
    sql: str,
    parameter_owner: str,
) -> None:
    request = _request()
    authorization = _authorization(request)
    scope = derive_effective_scope(request, authorization)
    compiled = CompiledGovernedQuery(
        candidate=_candidate(request, authorization, effective_scope=scope),
        sql=sql,
        parameters={"owner_user_id": parameter_owner},
        projections=(
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="amount",
                output_name="amount",
            ),
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="region",
                output_name="region",
            ),
        ),
    )

    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=scope,
            compiled=compiled,
        )


def test_frozen_dataset_projects_only_values_and_output_column_names_to_sandbox() -> None:
    dataset = _frozen_dataset()

    projection = dataset.to_sandbox_projection()

    assert projection.column_names == ("amount", "region")
    assert projection.rows == dataset.rows
    assert "owner_user_id" not in projection.model_fields
    assert "sql" not in projection.model_fields
    assert "gateway_receipt_id" not in projection.model_fields


def test_compiler_cannot_alias_sensitive_source_column_into_sandbox_data() -> None:
    requested = _scope(columns=("api_key", "region"))
    request = _request(scope=requested)
    authorization = _authorization(
        request,
        scope=AuthorizedDataScope(relations=requested.relations),
    )
    effective_scope = derive_effective_scope(request, authorization)
    compiled = CompiledGovernedQuery(
        candidate=_candidate(request, authorization, effective_scope=effective_scope),
        sql=(
            "SELECT s.api_key AS visible_text, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id"
        ),
        parameters={"owner_user_id": request.owner_user_id},
        projections=(
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="api_key",
                output_name="visible_text",
            ),
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="region",
                output_name="region",
            ),
        ),
    )

    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=effective_scope,
            compiled=compiled,
        )


@pytest.mark.parametrize(
    "sql",
    [
        (
            "WITH limited_sales AS ("
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id LIMIT 1) "
            "SELECT limited_sales.amount AS amount, limited_sales.region AS region "
            "FROM limited_sales"
        ),
        (
            "SELECT limited_sales.amount AS amount, limited_sales.region AS region "
            "FROM (SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id "
            "FETCH FIRST 1 ROW ONLY) AS limited_sales"
        ),
        (
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id "
            "ORDER BY s.amount OFFSET 1"
        ),
        (
            "WITH offset_sales AS ("
            "SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id "
            "ORDER BY s.amount OFFSET 1) "
            "SELECT offset_sales.amount AS amount, offset_sales.region AS region "
            "FROM offset_sales"
        ),
        (
            "SELECT offset_sales.amount AS amount, offset_sales.region AS region "
            "FROM (SELECT s.amount AS amount, s.region AS region "
            "FROM analytics.sales AS s WHERE s.owner_id = :owner_user_id "
            "ORDER BY s.amount OFFSET 1) AS offset_sales"
        ),
    ],
)
def test_nested_cte_subquery_limits_and_offsets_cannot_mark_partial_data_complete(sql: str) -> None:
    request = _request()
    authorization = _authorization(request)
    effective_scope = derive_effective_scope(request, authorization)
    compiled = CompiledGovernedQuery(
        candidate=_candidate(request, authorization, effective_scope=effective_scope),
        sql=sql,
        parameters={"owner_user_id": request.owner_user_id},
        projections=(
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="amount",
                output_name="amount",
            ),
            ReturnedColumnIdentity(
                relation=RelationIdentity(schema_name="analytics", relation_name="sales"),
                column_name="region",
                output_name="region",
            ),
        ),
    )

    with pytest.raises(DataRequestDenied):
        _validate_compiled_query(
            request=request,
            authorization=authorization,
            effective_scope=effective_scope,
            compiled=compiled,
        )


@pytest.mark.asyncio
async def test_denied_authority_never_calls_compiler_or_executor() -> None:
    request = _request()
    calls: list[str] = []

    class DeniedAuthority:
        async def verify_request(
            self,
            request: DataRequest,
        ) -> None:
            del request
            calls.append("authority")
            return None

    class NeverCompiler:
        def compile_request(
            self,
            request: DataRequest,
            authorization: VerifiedDataAuthorization,
            effective_scope: EffectiveDataScope,
        ) -> CompiledGovernedQuery:
            del request, authorization, effective_scope
            calls.append("compiler")
            raise AssertionError("compiler must not run without verified authority")

    class NeverExecutor:
        async def execute_candidate(
            self,
            compiled: CompiledGovernedQuery,
            budget: DataRequestBudget,
        ) -> TrustedExecutionResult:
            del compiled, budget
            calls.append("executor")
            raise AssertionError("executor must not run without verified authority")

    with pytest.raises(DataRequestDenied):
        await GovernedDataBroker(
            authority=DeniedAuthority(),
            compiler=NeverCompiler(),
            executor=NeverExecutor(),
        ).request_dataset(request)

    assert calls == ["authority"]


@pytest.mark.parametrize("status", ["partial", "truncated", "denied", "unavailable"])
def test_incomplete_or_truncated_execution_cannot_be_frozen(status: str) -> None:
    request = _request()
    authorization = _authorization(request)
    candidate = _candidate(request, authorization)

    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, status=status),
        )


def test_missing_source_snapshot_or_version_fails_closed() -> None:
    with pytest.raises(ValidationError):
        DataSourceSnapshot(source_id="source", source_version="", snapshot_id="snapshot")

    request = _request()
    authorization = _authorization(request)
    candidate = _candidate(request, authorization)
    result_without_source = TrustedExecutionResult(
        status="complete",
        query_fingerprint=candidate.query_fingerprint,
        source_snapshot=None,
        gateway_receipt=QueryGatewayReceiptReference(
            receipt_id=RECEIPT_ID,
            query_fingerprint=candidate.query_fingerprint,
            policy_version="query-gateway-test-v1",
            policy_outcome="allow",
        ),
        returned_columns=_execution(candidate).returned_columns,
        rows=(FrozenRow(values=(12.5, "north")),),
        row_count=1,
        estimated_cost=12.0,
    )

    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=result_without_source,
        )


def test_candidate_receipt_source_and_scope_mismatch_are_rejected() -> None:
    request = _request()
    authorization = _authorization(request)
    candidate = _candidate(request, authorization)
    wrong_candidate = _candidate(
        request,
        authorization,
        authorization_checksum="c" * 64,
    )
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=wrong_candidate,
            execution=_execution(wrong_candidate),
        )

    wrong_receipt = QueryGatewayReceiptReference(
        receipt_id=RECEIPT_ID,
        query_fingerprint="d" * 64,
        policy_version="query-gateway-test-v1",
        policy_outcome="allow",
    )
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, receipt=wrong_receipt),
        )

    wrong_snapshot_receipt = QueryGatewayReceiptReference(
        receipt_id=RECEIPT_ID,
        query_fingerprint=candidate.query_fingerprint,
        policy_version="query-gateway-test-v1",
        policy_outcome="allow",
        source_snapshot_id="other-snapshot",
    )
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, receipt=wrong_snapshot_receipt),
        )

    denied_receipt = QueryGatewayReceiptReference(
        receipt_id=RECEIPT_ID,
        query_fingerprint=candidate.query_fingerprint,
        policy_version="query-gateway-test-v1",
        policy_outcome="deny",
    )
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, receipt=denied_receipt),
        )

    wrong_source = DataSourceSnapshot(
        source_id="test-source",
        source_version="different-source-version",
        snapshot_id="different-snapshot",
    )
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, source=wrong_source),
        )

    wrong_columns = (
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="other"),
            column_name="amount",
            output_name="amount",
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="other"),
            column_name="region",
            output_name="region",
        ),
    )
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, columns=wrong_columns),
        )


def test_row_byte_and_cost_budgets_are_checked_before_dataset_issue() -> None:
    request = _request(
        budget=DataRequestBudget(max_rows=1, max_bytes=10, max_cost=1.0)
    )
    authorization = _authorization(request)
    candidate = _candidate(request, authorization)

    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=request,
            authorization=authorization,
            candidate=candidate,
            execution=_execution(candidate, estimated_cost=2.0),
        )

    row_limited_request = _request(
        budget=DataRequestBudget(max_rows=1, max_bytes=50_000, max_cost=25_000.0)
    )
    row_limited_authorization = _authorization(row_limited_request)
    row_limited_candidate = _candidate(row_limited_request, row_limited_authorization)
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=row_limited_request,
            authorization=row_limited_authorization,
            candidate=row_limited_candidate,
            execution=_execution(
                row_limited_candidate,
                rows=(FrozenRow(values=(12.5, "north")), FrozenRow(values=(8.0, "south"))),
            ),
        )

    byte_limited_request = _request(
        budget=DataRequestBudget(max_rows=20, max_bytes=10, max_cost=25_000.0)
    )
    byte_limited_authorization = _authorization(byte_limited_request)
    byte_limited_candidate = _candidate(byte_limited_request, byte_limited_authorization)
    with pytest.raises(DataRequestDenied):
        build_frozen_dataset(
            request=byte_limited_request,
            authorization=byte_limited_authorization,
            candidate=byte_limited_candidate,
            execution=_execution(byte_limited_candidate),
        )


def test_frozen_checksum_binds_content_and_schema_identity() -> None:
    first = _frozen_dataset()
    changed_data = _frozen_dataset(rows=(FrozenRow(values=(13.5, "north")),))
    changed_schema = _frozen_dataset(
        scope=_scope(schema_name="analytics", relation_name="orders")
    )

    assert first.status == "complete"
    assert first.row_count == 1
    assert first.content_checksum != changed_data.content_checksum
    assert first.content_checksum != changed_schema.content_checksum
    assert first.dataset_id != changed_data.dataset_id


def test_model_authored_sql_is_rejected_from_governed_candidate_contract() -> None:
    request = _request()
    authorization = _authorization(request)
    candidate_payload: dict[str, Any] = _candidate(request, authorization).model_dump(
        mode="json"
    )
    candidate_payload["sql"] = "SELECT * FROM analytics.sales"

    with pytest.raises(ValidationError):
        GovernedSQLCandidate.model_validate(candidate_payload)


@pytest.mark.asyncio
async def test_adapter_failure_error_does_not_echo_private_values() -> None:
    request = _request(owner="private-owner-canary")
    private_payload = "credential-and-scope-canary"

    class LeakingAuthority:
        async def verify_request(self, request: DataRequest) -> VerifiedDataAuthorization:
            del request
            raise RuntimeError(private_payload)

    class UnusedCompiler:
        def compile_request(
            self,
            request: DataRequest,
            authorization: VerifiedDataAuthorization,
            effective_scope: EffectiveDataScope,
        ) -> CompiledGovernedQuery:
            del request, authorization, effective_scope
            raise AssertionError("compiler must not run after authority failure")

    class UnusedExecutor:
        async def execute_candidate(self, compiled: CompiledGovernedQuery, budget: Any) -> Any:
            del compiled, budget
            raise AssertionError("executor must not run after authority failure")

    with pytest.raises(DataBrokerUnavailable) as error:
        await GovernedDataBroker(
            authority=LeakingAuthority(),
            compiler=UnusedCompiler(),
            executor=UnusedExecutor(),
        ).request_dataset(request)

    assert private_payload not in str(error.value)
    assert request.owner_user_id not in str(error.value)


def test_data_contract_slice_does_not_promote_product_codeact_readiness() -> None:
    readiness = assess_p5_runtime_readiness(
        AgentConfig.model_validate({"service_mode": "product"})
    )

    assert readiness.production_arbitrary_code_ready is False
