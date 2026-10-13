from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.config.settings import AgentConfig
from src.nl2sql.infra.governance.data_broker import (
    DataBrokerUnavailable,
    GovernedDataBroker,
)
from src.nl2sql.orchestration.data_request import (
    AuthorizedDataScope,
    DataRequest,
    DataRequestBudget,
    DataRequestDenied,
    DataRequestInput,
    DataSourceSnapshot,
    EffectiveDataScope,
    FrozenDataset,
    FrozenRow,
    GovernedSQLCandidate,
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
PARAMETER_FINGERPRINT = "b" * 64


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
) -> VerifiedDataAuthorization:
    return VerifiedDataAuthorization(
        schema_version="data-authorization-v1",
        request_id=request.request_id,
        request_checksum=request.request_checksum,
        owner_user_id=owner or request.owner_user_id,
        authority_id="synthetic-test-authority",
        authorization_revision="test-auth-rev-1",
        authorized_scope=scope or AuthorizedDataScope(relations=request.requested_scope.relations),
    )


def _candidate(
    request: DataRequest,
    authorization: VerifiedDataAuthorization,
    *,
    query_fingerprint: str = QUERY_FINGERPRINT,
    effective_scope: EffectiveDataScope | None = None,
    request_checksum: str | None = None,
    authorization_checksum: str | None = None,
) -> GovernedSQLCandidate:
    return GovernedSQLCandidate(
        schema_version="governed-sql-candidate-v1",
        request_id=request.request_id,
        request_checksum=request_checksum or request.request_checksum,
        authorization_checksum=(
            authorization_checksum or authorization.authorization_checksum
        ),
        compiler_id="test-compiler",
        compiler_version="contract-test-v1",
        effective_scope=effective_scope or derive_effective_scope(request, authorization),
        source_snapshot=DataSourceSnapshot(
            source_id="test-source",
            source_version="snapshot-version-4",
            snapshot_id="snapshot-0004",
        ),
        query_fingerprint=query_fingerprint,
        parameter_binding_fingerprint=PARAMETER_FINGERPRINT,
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
    request = _request()
    authorization = _authorization(request)
    effective_scope = derive_effective_scope(request, authorization)
    candidate = _candidate(request, authorization, effective_scope=effective_scope)
    execution = _execution(candidate)
    calls: list[str] = []

    class SyntheticAuthority:
        async def verify_request(
            self,
            candidate_request: DataRequest,
        ) -> VerifiedDataAuthorization:
            calls.append("authority")
            assert candidate_request.request_id == request.request_id
            return authorization

    class SyntheticCompiler:
        def compile_request(
            self,
            candidate_request: DataRequest,
            verified: VerifiedDataAuthorization,
            scope: EffectiveDataScope,
        ) -> GovernedSQLCandidate:
            calls.append("compiler")
            assert candidate_request == request
            assert verified == authorization
            assert scope == effective_scope
            return candidate

    class SyntheticExecutor:
        async def execute_candidate(
            self,
            compiled: GovernedSQLCandidate,
            budget: DataRequestBudget,
        ) -> TrustedExecutionResult:
            calls.append("executor")
            assert compiled == candidate
            assert budget == request.budget
            return execution

    dataset = await GovernedDataBroker(
        authority=SyntheticAuthority(),
        compiler=SyntheticCompiler(),
        executor=SyntheticExecutor(),
    ).request_dataset(request)

    assert calls == ["authority", "compiler", "executor"]
    assert dataset.status == "complete"


@pytest.mark.asyncio
async def test_denied_authority_never_calls_compiler_or_executor() -> None:
    request = _request()
    calls: list[str] = []

    class DeniedAuthority:
        async def verify_request(
            self,
            candidate_request: DataRequest,
        ) -> None:
            del candidate_request
            calls.append("authority")
            return None

    class NeverCompiler:
        def compile_request(
            self,
            candidate_request: DataRequest,
            authorization: VerifiedDataAuthorization,
            scope: EffectiveDataScope,
        ) -> GovernedSQLCandidate:
            del candidate_request, authorization, scope
            calls.append("compiler")
            raise AssertionError("compiler must not run without verified authority")

    class NeverExecutor:
        async def execute_candidate(
            self,
            candidate: GovernedSQLCandidate,
            budget: DataRequestBudget,
        ) -> TrustedExecutionResult:
            del candidate, budget
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
        ),
        ReturnedColumnIdentity(
            relation=RelationIdentity(schema_name="analytics", relation_name="other"),
            column_name="region",
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
        ) -> GovernedSQLCandidate:
            del request, authorization, effective_scope
            raise AssertionError("compiler must not run after authority failure")

    class UnusedExecutor:
        async def execute_candidate(self, candidate: GovernedSQLCandidate, budget: Any) -> Any:
            del candidate, budget
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
        AgentConfig(_env_file=None, service_mode="product")
    )

    assert readiness.production_arbitrary_code_ready is False
