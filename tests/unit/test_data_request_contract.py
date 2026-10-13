from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.orchestration.data_request import (
    AuthorizedDataScope,
    DataRequestBudget,
    DataRequestInput,
    DataRequestValidationError,
    RelationColumnScope,
    RelationIdentity,
    RequestedDataScope,
    bind_data_request_owner,
    parse_data_request_input,
)

REQUEST_ID = UUID("00000000-0000-4000-8000-000000000001")


def _relation_scope(
    *,
    schema_name: str = "analytics",
    relation_name: str = "sales",
    columns: tuple[str, ...] = ("amount", "region"),
) -> RelationColumnScope:
    return RelationColumnScope(
        relation=RelationIdentity(
            schema_name=schema_name,
            relation_name=relation_name,
        ),
        columns=columns,
    )


def _request_input(
    *,
    scope: RequestedDataScope | None = None,
    budget: DataRequestBudget | None = None,
) -> DataRequestInput:
    return DataRequestInput(
        request_id=REQUEST_ID,
        requested_scope=scope or RequestedDataScope(relations=(_relation_scope(),)),
        budget=budget or DataRequestBudget(max_rows=20, max_bytes=50_000, max_cost=25_000.0),
    )


def test_request_contract_is_immutable_and_owner_is_server_bound() -> None:
    request_input = _request_input()
    request = bind_data_request_owner(request_input, owner_user_id="server-owner-7")

    assert request.owner_user_id == "server-owner-7"
    assert request.schema_version == "data-request-v1"
    assert len(request.request_checksum) == 64
    with pytest.raises(ValidationError):
        request.owner_user_id = "different-owner"
    with pytest.raises(ValidationError):
        request.requested_scope.relations[0].columns = ("secret",)


def test_missing_owner_identity_is_rejected_without_echoing_input() -> None:
    for owner_user_id in ("", "   "):
        with pytest.raises(DataRequestValidationError) as error:
            bind_data_request_owner(_request_input(), owner_user_id=owner_user_id)

        assert str(error.value) == "data_request_invalid"


def test_untrusted_payload_cannot_supply_owner_or_claimed_authorization() -> None:
    payload = _request_input().model_dump(mode="json")
    payload["owner_user_id"] = "client-selected-owner"
    payload["approved"] = True
    payload["authorized_scope"] = {"relations": []}

    with pytest.raises(DataRequestValidationError) as error:
        parse_data_request_input(payload)

    assert str(error.value) == "data_request_invalid"
    assert "client-selected-owner" not in str(error.value)


@pytest.mark.parametrize(
    ("schema_name", "relation_name"),
    [
        ("bad.schema", "sales"),
        ("analytics", "sales;drop"),
    ],
)
def test_invalid_relation_identifiers_are_rejected(
    schema_name: str,
    relation_name: str,
) -> None:
    with pytest.raises(ValidationError):
        RelationIdentity(schema_name=schema_name, relation_name=relation_name)


def test_invalid_column_identifier_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RelationColumnScope(
            relation=RelationIdentity(
                schema_name="analytics",
                relation_name="sales",
            ),
            columns=("amount;drop",),
        )


def test_duplicate_or_ambiguous_relation_and_column_scope_is_rejected() -> None:
    relation = _relation_scope()
    with pytest.raises(ValidationError):
        RequestedDataScope(relations=(relation, relation))
    with pytest.raises(ValidationError):
        AuthorizedDataScope(
            relations=(
                _relation_scope(columns=("amount", "amount")),
            )
        )


@pytest.mark.parametrize(
    "budget_values",
    [
        {"max_rows": 0, "max_bytes": 100, "max_cost": 1.0},
        {"max_rows": 201, "max_bytes": 100, "max_cost": 1.0},
        {"max_rows": 10, "max_bytes": 1, "max_cost": 1.0},
        {"max_rows": 10, "max_bytes": 1_000_001, "max_cost": 1.0},
        {"max_rows": 10, "max_bytes": 100, "max_cost": 500_000.1},
        {"max_rows": 10, "max_bytes": 100, "max_cost": float("inf")},
    ],
)
def test_request_budgets_are_positive_finite_and_bounded(
    budget_values: dict[str, int | float],
) -> None:
    with pytest.raises(ValidationError):
        DataRequestBudget(**budget_values)


def test_request_checksum_changes_with_owner_scope_and_budget() -> None:
    base_input = _request_input()
    base = bind_data_request_owner(base_input, owner_user_id="owner-a")
    other_owner = bind_data_request_owner(base_input, owner_user_id="owner-b")
    other_scope = bind_data_request_owner(
        _request_input(
            scope=RequestedDataScope(
                relations=(_relation_scope(columns=("amount",)),)
            )
        ),
        owner_user_id="owner-a",
    )
    other_budget = bind_data_request_owner(
        _request_input(
            budget=DataRequestBudget(max_rows=19, max_bytes=50_000, max_cost=25_000.0)
        ),
        owner_user_id="owner-a",
    )

    assert len({base.request_checksum, other_owner.request_checksum,
                other_scope.request_checksum, other_budget.request_checksum}) == 4


def test_scope_contract_has_stable_order_and_rejects_empty_scope() -> None:
    first = _relation_scope(schema_name="zeta", relation_name="facts", columns=("z", "a"))
    second = _relation_scope(schema_name="alpha", relation_name="facts", columns=("b",))
    scope = RequestedDataScope(relations=(first, second))

    assert scope.relations[0].relation.schema_name == "alpha"
    assert scope.relations[1].columns == ("a", "z")
    with pytest.raises(ValidationError):
        RequestedDataScope(relations=())
