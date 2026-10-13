"""Immutable contracts for a future owner-governed data broker.

These contracts describe authorization and execution evidence; they do not
provide an authority source, compile SQL, access a database, or activate P5.
Checksums are deterministic pseudonyms and must not be treated as anonymity.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

MAX_REQUEST_ROWS = 200
MAX_REQUEST_BYTES = 1_000_000
MAX_REQUEST_COST = 500_000.0

_IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER = Annotated[
    str,
    StringConstraints(min_length=1, max_length=63, pattern=_IDENTIFIER_PATTERN),
]
_OPAQUE_TOKEN = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^\S(?:.*\S)?$",
    ),
]
_SHA256 = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]
_OWNER_ID = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^\S(?:.*\S)?$",
    ),
]
DataCell = StrictStr | StrictInt | StrictFloat | StrictBool | None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


class ImmutableDataContract(BaseModel):
    """Strict, immutable base for nested data-request values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class RelationIdentity(ImmutableDataContract):
    schema_name: _IDENTIFIER
    relation_name: _IDENTIFIER


class RelationColumnScope(ImmutableDataContract):
    relation: RelationIdentity
    columns: tuple[_IDENTIFIER, ...]

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("data scope columns are invalid")
        return tuple(sorted(value))


class _DataScope(ImmutableDataContract):
    relations: tuple[RelationColumnScope, ...]

    @field_validator("relations")
    @classmethod
    def validate_relations(
        cls,
        value: tuple[RelationColumnScope, ...],
    ) -> tuple[RelationColumnScope, ...]:
        keys = [
            (item.relation.schema_name, item.relation.relation_name)
            for item in value
        ]
        if not value or len(keys) != len(set(keys)):
            raise ValueError("data scope relations are invalid")
        return tuple(
            sorted(value, key=lambda item: (item.relation.schema_name, item.relation.relation_name))
        )


class RequestedDataScope(_DataScope):
    """Relations and columns requested by the run; this is not permission."""


class AuthorizedDataScope(_DataScope):
    """Relations and columns returned by a trusted server authority."""


class EffectiveDataScope(_DataScope):
    """Requested scope after complete authorization; partial grants are denied."""


class DataRequestBudget(ImmutableDataContract):
    max_rows: int = Field(ge=1, le=MAX_REQUEST_ROWS)
    max_bytes: int = Field(ge=2, le=MAX_REQUEST_BYTES)
    max_cost: float = Field(ge=0, le=MAX_REQUEST_COST)


class DataRequestInput(ImmutableDataContract):
    """Untrusted request intent; deliberately has no owner or authorization field."""

    schema_version: Literal["data-request-v1"] = "data-request-v1"
    request_id: UUID
    requested_scope: RequestedDataScope
    budget: DataRequestBudget


class DataRequestValidationError(ValueError):
    """Safe validation failure that does not expose input values."""

    def __init__(self) -> None:
        super().__init__("data_request_invalid")


def parse_data_request_input(payload: object) -> DataRequestInput:
    """Parse untrusted request intent with a stable, value-free error."""

    try:
        return DataRequestInput.model_validate(payload)
    except ValidationError:
        raise DataRequestValidationError() from None


class DataRequest(ImmutableDataContract):
    """Server-bound request identity and checksum; owner is never client intent."""

    schema_version: Literal["data-request-v1"]
    request_id: UUID
    owner_user_id: _OWNER_ID
    requested_scope: RequestedDataScope
    budget: DataRequestBudget

    @computed_field(return_type=str)
    @property
    def request_checksum(self) -> str:
        return _sha256(
            {
                "schema_version": self.schema_version,
                "request_id": str(self.request_id),
                "owner_user_id": self.owner_user_id,
                "requested_scope": self.requested_scope.model_dump(mode="json"),
                "budget": self.budget.model_dump(mode="json"),
            }
        )


def bind_data_request_owner(
    request_input: DataRequestInput,
    *,
    owner_user_id: str,
) -> DataRequest:
    """Bind identity from the authenticated server context, never client payload."""

    try:
        return DataRequest(
            schema_version=request_input.schema_version,
            request_id=request_input.request_id,
            owner_user_id=owner_user_id,
            requested_scope=request_input.requested_scope,
            budget=request_input.budget,
        )
    except ValidationError:
        raise DataRequestValidationError() from None


class OwnerRowScope(ImmutableDataContract):
    """Trusted owner column for one relation; its value comes from request binding."""

    relation: RelationIdentity
    owner_column: _IDENTIFIER


class VerifiedDataAuthorization(ImmutableDataContract):
    """Typed, owner-bound authority output; no production producer is wired."""

    schema_version: Literal["data-authorization-v1"]
    request_id: UUID
    request_checksum: _SHA256
    owner_user_id: _OWNER_ID
    authority_id: _OPAQUE_TOKEN
    authorization_revision: _OPAQUE_TOKEN
    authorized_scope: AuthorizedDataScope
    owner_row_scopes: tuple[OwnerRowScope, ...]

    @field_validator("owner_row_scopes")
    @classmethod
    def validate_owner_row_scopes(
        cls,
        value: tuple[OwnerRowScope, ...],
    ) -> tuple[OwnerRowScope, ...]:
        keys = [
            (item.relation.schema_name, item.relation.relation_name)
            for item in value
        ]
        if not value or len(keys) != len(set(keys)):
            raise ValueError("owner row scopes are invalid")
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.relation.schema_name,
                    item.relation.relation_name,
                ),
            )
        )

    @computed_field(return_type=str)
    @property
    def authorization_checksum(self) -> str:
        return _sha256(
            {
                "schema_version": self.schema_version,
                "request_id": str(self.request_id),
                "request_checksum": self.request_checksum,
                "owner_user_id": self.owner_user_id,
                "authority_id": self.authority_id,
                "authorization_revision": self.authorization_revision,
                "authorized_scope": self.authorized_scope.model_dump(mode="json"),
                "owner_row_scopes": [
                    item.model_dump(mode="json") for item in self.owner_row_scopes
                ],
            }
        )


class DataRequestDenied(ValueError):
    """Safe authorization/evidence refusal with no private scope details."""

    def __init__(self) -> None:
        super().__init__("governed_data_request_denied")


def derive_effective_scope(
    request: DataRequest,
    authorization: VerifiedDataAuthorization,
) -> EffectiveDataScope:
    """Require a complete owner/request-bound grant for every requested column."""

    if (
        authorization.request_id != request.request_id
        or authorization.request_checksum != request.request_checksum
        or authorization.owner_user_id != request.owner_user_id
    ):
        raise DataRequestDenied()

    authorized = {
        (item.relation.schema_name, item.relation.relation_name): set(item.columns)
        for item in authorization.authorized_scope.relations
    }
    owner_scopes = {
        (item.relation.schema_name, item.relation.relation_name)
        for item in authorization.owner_row_scopes
    }
    if owner_scopes != set(authorized):
        raise DataRequestDenied()
    for requested in request.requested_scope.relations:
        key = (requested.relation.schema_name, requested.relation.relation_name)
        if not set(requested.columns).issubset(authorized.get(key, set())):
            raise DataRequestDenied()

    return EffectiveDataScope(relations=request.requested_scope.relations)


class DataSourceSnapshot(ImmutableDataContract):
    """Trusted source identity plus its observed database snapshot when available."""

    source_id: _OPAQUE_TOKEN
    source_version: _OPAQUE_TOKEN
    # A compiler may bind the expected source/version before execution. The
    # QueryGateway supplies the actual transaction snapshot after it connects.
    snapshot_id: _OPAQUE_TOKEN | None = None


class GovernedSQLCandidate(ImmutableDataContract):
    """Compiler evidence only; intentionally contains no SQL text or bind values."""

    schema_version: Literal["governed-sql-candidate-v1"]
    request_id: UUID
    request_checksum: _SHA256
    authorization_checksum: _SHA256
    compiler_id: _OPAQUE_TOKEN
    compiler_version: _OPAQUE_TOKEN
    effective_scope: EffectiveDataScope
    source_snapshot: DataSourceSnapshot
    query_fingerprint: _SHA256
    parameter_binding_fingerprint: _SHA256


class ReturnedColumnIdentity(ImmutableDataContract):
    relation: RelationIdentity
    column_name: _IDENTIFIER
    output_name: _IDENTIFIER


class FrozenRow(ImmutableDataContract):
    values: tuple[DataCell, ...]


class QueryGatewayReceiptReference(ImmutableDataContract):
    """Safe receipt identity; no SQL, credentials, or raw audit payload."""

    receipt_id: UUID
    query_fingerprint: _SHA256
    policy_version: _OPAQUE_TOKEN
    policy_outcome: Literal["allow", "deny"]
    source_snapshot_id: _OPAQUE_TOKEN | None = None


class TrustedExecutionResult(ImmutableDataContract):
    """Result envelope an eventual trusted QueryGateway adapter must produce."""

    status: Literal["complete", "partial", "truncated", "denied", "unavailable"]
    query_fingerprint: _SHA256
    source_snapshot: DataSourceSnapshot | None
    gateway_receipt: QueryGatewayReceiptReference | None
    returned_columns: tuple[ReturnedColumnIdentity, ...]
    rows: tuple[FrozenRow, ...]
    row_count: int = Field(ge=0)
    estimated_cost: float = Field(ge=0, le=MAX_REQUEST_COST)

    @field_validator("estimated_cost")
    @classmethod
    def validate_finite_cost(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("execution cost is invalid")
        return value


class FrozenDataset(ImmutableDataContract):
    """Complete immutable rowset plus the provenance required for future replay."""

    schema_version: Literal["frozen-dataset-v1"]
    request_id: UUID
    request_checksum: _SHA256
    owner_user_id: _OWNER_ID
    authorization_checksum: _SHA256
    authority_id: _OPAQUE_TOKEN
    authorization_revision: _OPAQUE_TOKEN
    authorized_scope: AuthorizedDataScope
    owner_row_scopes: tuple[OwnerRowScope, ...]
    effective_scope: EffectiveDataScope
    source_snapshot: DataSourceSnapshot
    query_fingerprint: _SHA256
    gateway_receipt_id: UUID
    gateway_receipt_query_fingerprint: _SHA256
    gateway_receipt_snapshot_id: _OPAQUE_TOKEN
    gateway_policy_version: _OPAQUE_TOKEN
    gateway_policy_outcome: Literal["allow"]
    budget: DataRequestBudget
    estimated_cost: float = Field(ge=0, le=MAX_REQUEST_COST)
    returned_columns: tuple[ReturnedColumnIdentity, ...]
    rows: tuple[FrozenRow, ...]

    @model_validator(mode="after")
    def validate_complete_rows(self) -> FrozenDataset:
        try:
            bound_request = DataRequest(
                schema_version="data-request-v1",
                request_id=self.request_id,
                owner_user_id=self.owner_user_id,
                requested_scope=RequestedDataScope(
                    relations=self.effective_scope.relations,
                ),
                budget=self.budget,
            )
            bound_authorization = VerifiedDataAuthorization(
                schema_version="data-authorization-v1",
                request_id=self.request_id,
                request_checksum=self.request_checksum,
                owner_user_id=self.owner_user_id,
                authority_id=self.authority_id,
                authorization_revision=self.authorization_revision,
                authorized_scope=self.authorized_scope,
                owner_row_scopes=self.owner_row_scopes,
            )
            derived_scope = derive_effective_scope(bound_request, bound_authorization)
        except ValidationError:
            raise ValueError("frozen dataset identity is invalid") from None
        except DataRequestDenied:
            raise ValueError("frozen dataset identity is invalid") from None
        if (
            bound_request.request_checksum != self.request_checksum
            or bound_authorization.authorization_checksum != self.authorization_checksum
            or derived_scope != self.effective_scope
            or self.gateway_receipt_query_fingerprint != self.query_fingerprint
            or self.gateway_receipt_snapshot_id != self.source_snapshot.snapshot_id
            or self.estimated_cost > self.budget.max_cost
            or self.source_snapshot.snapshot_id is None
        ):
            raise ValueError("frozen dataset identity is invalid")

        authorized = {
            (item.relation.schema_name, item.relation.relation_name): set(item.columns)
            for item in self.authorized_scope.relations
        }
        if any(
            not set(item.columns).issubset(
                authorized.get((item.relation.schema_name, item.relation.relation_name), set())
            )
            for item in self.effective_scope.relations
        ):
            raise ValueError("frozen dataset scope is invalid")

        column_keys = [
            (
                column.relation.schema_name,
                column.relation.relation_name,
                column.column_name,
            )
            for column in self.returned_columns
        ]
        output_names = [column.output_name for column in self.returned_columns]
        expected = {
            (item.relation.schema_name, item.relation.relation_name, name)
            for item in self.effective_scope.relations
            for name in item.columns
        }
        if (
            not column_keys
            or len(column_keys) != len(set(column_keys))
            or len(output_names) != len(set(output_names))
            or set(column_keys) != expected
        ):
            raise ValueError("frozen dataset columns are invalid")
        if len(self.rows) > self.budget.max_rows:
            raise ValueError("frozen dataset exceeds row budget")
        if any(len(row.values) != len(column_keys) for row in self.rows):
            raise ValueError("frozen dataset row shape is invalid")
        if self.result_byte_count > self.budget.max_bytes:
            raise ValueError("frozen dataset exceeds byte budget")
        return self

    @computed_field(return_type=int)
    @property
    def row_count(self) -> int:
        return len(self.rows)

    @computed_field(return_type=int)
    @property
    def result_byte_count(self) -> int:
        return len(
            _canonical_json(
                {
                    "source_snapshot": self.source_snapshot.model_dump(mode="json"),
                    "returned_columns": [
                        item.model_dump(mode="json") for item in self.returned_columns
                    ],
                    "rows": [list(row.values) for row in self.rows],
                }
            )
        )

    @computed_field(return_type=str)
    @property
    def content_checksum(self) -> str:
        return _sha256(
            {
                "schema_version": self.schema_version,
                "source_snapshot": self.source_snapshot.model_dump(mode="json"),
                "returned_columns": [
                    item.model_dump(mode="json") for item in self.returned_columns
                ],
                "rows": [list(row.values) for row in self.rows],
            }
        )

    @computed_field(return_type=str)
    @property
    def dataset_id(self) -> str:
        return _sha256(
            {
                "schema_version": self.schema_version,
                "request_checksum": self.request_checksum,
                "authorization_checksum": self.authorization_checksum,
                "query_fingerprint": self.query_fingerprint,
                "content_checksum": self.content_checksum,
            }
        )

    @computed_field(return_type=Literal["complete"])
    @property
    def status(self) -> Literal["complete"]:
        return "complete"

    def to_sandbox_projection(self) -> SandboxDatasetProjection:
        """Return only column labels and scalar rows to an isolated sandbox."""

        return SandboxDatasetProjection(
            column_names=tuple(column.output_name for column in self.returned_columns),
            rows=self.rows,
        )


class SandboxDatasetProjection(ImmutableDataContract):
    """Data-only value object; contains no owner, SQL, receipt, or DB capability."""

    column_names: tuple[_IDENTIFIER, ...]
    rows: tuple[FrozenRow, ...]

    @model_validator(mode="after")
    def validate_projection_shape(self) -> SandboxDatasetProjection:
        if (
            not self.column_names
            or len(self.column_names) != len(set(self.column_names))
            or any(len(row.values) != len(self.column_names) for row in self.rows)
        ):
            raise ValueError("sandbox dataset projection is invalid")
        return self


def build_frozen_dataset(
    *,
    request: DataRequest,
    authorization: VerifiedDataAuthorization,
    candidate: GovernedSQLCandidate,
    execution: TrustedExecutionResult,
) -> FrozenDataset:
    """Issue a dataset only when all typed authority and execution evidence agrees."""

    effective_scope = derive_effective_scope(request, authorization)
    expected_source = candidate.source_snapshot
    if (
        candidate.request_id != request.request_id
        or candidate.request_checksum != request.request_checksum
        or candidate.authorization_checksum != authorization.authorization_checksum
        or candidate.effective_scope != effective_scope
        or execution.status != "complete"
        or execution.query_fingerprint != candidate.query_fingerprint
        or execution.row_count != len(execution.rows)
        or execution.row_count > request.budget.max_rows
        or execution.estimated_cost > request.budget.max_cost
    ):
        raise DataRequestDenied()
    actual_source = execution.source_snapshot
    gateway_receipt = execution.gateway_receipt
    if (
        actual_source is None
        or actual_source.snapshot_id is None
        or actual_source.source_id != expected_source.source_id
        or actual_source.source_version != expected_source.source_version
        or (
            expected_source.snapshot_id is not None
            and actual_source.snapshot_id != expected_source.snapshot_id
        )
        or gateway_receipt is None
        or gateway_receipt.query_fingerprint != candidate.query_fingerprint
        or gateway_receipt.policy_outcome != "allow"
        or gateway_receipt.source_snapshot_id is None
        or gateway_receipt.source_snapshot_id != actual_source.snapshot_id
    ):
        raise DataRequestDenied()

    try:
        dataset = FrozenDataset(
            schema_version="frozen-dataset-v1",
            request_id=request.request_id,
            request_checksum=request.request_checksum,
            owner_user_id=request.owner_user_id,
            authorization_checksum=authorization.authorization_checksum,
            authority_id=authorization.authority_id,
            authorization_revision=authorization.authorization_revision,
            authorized_scope=authorization.authorized_scope,
            owner_row_scopes=authorization.owner_row_scopes,
            effective_scope=effective_scope,
            source_snapshot=actual_source,
            query_fingerprint=candidate.query_fingerprint,
            gateway_receipt_id=gateway_receipt.receipt_id,
            gateway_receipt_query_fingerprint=gateway_receipt.query_fingerprint,
            gateway_receipt_snapshot_id=gateway_receipt.source_snapshot_id,
            gateway_policy_version=gateway_receipt.policy_version,
            gateway_policy_outcome=gateway_receipt.policy_outcome,
            budget=request.budget,
            estimated_cost=execution.estimated_cost,
            returned_columns=execution.returned_columns,
            rows=execution.rows,
        )
    except ValidationError:
        raise DataRequestDenied() from None

    if dataset.result_byte_count > request.budget.max_bytes:
        raise DataRequestDenied()
    return dataset
