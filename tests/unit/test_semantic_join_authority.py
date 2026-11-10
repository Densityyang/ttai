from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from src.nl2sql.semantic.metric_contract import (
    JoinColumnPair,
    SemanticJoinAuthority,
)


def _payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "join_id": "orders_customers",
        "physical_relationship_id": "fk_orders_customer",
        "physical_relationship_sha256": "1" * 64,
        "left_relation_id": "ai_views.orders",
        "right_relation_id": "ai_views.customers",
        "column_pairs": ({"left_column": "customer_id", "right_column": "id"},),
        "cardinality": "many_to_one",
        "join_type": "inner",
        "null_semantics": "exclude_unmatched",
        "direction": "left_to_right",
        "semantic_release_id": "release-17",
        "semantic_release_checksum": "2" * 64,
        "schema_snapshot_id": "snapshot-29",
        "schema_snapshot_checksum": "3" * 64,
        "applicable_fetch_step_ids": ("fetch_orders",),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["authority_checksum"] = hashlib.sha256(canonical).hexdigest()
    return payload


def test_semantic_join_contract_binds_exact_pair_and_release_snapshot() -> None:
    authority = SemanticJoinAuthority.model_validate(_payload())

    authority.validate_integrity()
    assert authority.column_pairs == (
        JoinColumnPair(left_column="customer_id", right_column="id"),
    )
    assert authority.cardinality == "many_to_one"
    assert authority.semantic_release_id == "release-17"
    assert authority.schema_snapshot_id == "snapshot-29"


@pytest.mark.parametrize(
    "field,value",
    [
        ("right_relation_id", "ai_views.orders"),
        ("join_type", "cross"),
        ("direction", "unknown"),
        ("applicable_fetch_step_ids", ("fetch_orders", "fetch_orders")),
    ],
)
def test_semantic_join_contract_rejects_invalid_shape(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        SemanticJoinAuthority.model_validate(payload)


def test_semantic_join_contract_rejects_stale_checksum_and_mutated_restore() -> None:
    payload = _payload()
    payload["cardinality"] = "one_to_one"
    with pytest.raises(ValidationError, match="checksum mismatch"):
        SemanticJoinAuthority.model_validate(payload)

    authority = SemanticJoinAuthority.model_validate(_payload())
    changed = authority.model_copy(update={"join_type": "left"})
    with pytest.raises(ValidationError, match="checksum mismatch"):
        changed.validate_integrity()
