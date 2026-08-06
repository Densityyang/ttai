"""Offline SchemaSnapshot contracts and release binding invariants."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.nl2sql.semantic.registry import (
    ControlSemanticReleasePublisher,
    SemanticAssetRecord,
    SemanticDocument,
    SemanticReleaseCandidate,
    SemanticReleaseError,
)
from src.nl2sql.semantic.schema_snapshot import (
    PostgresSchemaSnapshotCollector,
    RelationPolicy,
    SchemaRequirement,
    SchemaSnapshot,
    SchemaSnapshotCandidate,
    SchemaSnapshotError,
    SchemaSnapshotState,
    bind_schema_snapshot,
    build_schema_snapshot_candidate,
    load_relation_policies,
    schema_snapshot_candidate_from_payload,
    validate_schema_snapshot,
)


def _catalog_rows() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    relation_rows = [
        {
            "relation_oid": 20,
            "schema_name": "public",
            "relation_name": "users",
            "relation_kind": "table",
            "estimated_rows": 8,
            "total_bytes": 16_384,
            "partition_key": None,
            "parent_schema_name": None,
            "parent_relation_name": None,
        },
        {
            "relation_oid": 10,
            "schema_name": "public",
            "relation_name": "complaints",
            "relation_kind": "table",
            "estimated_rows": 125,
            "total_bytes": 32_768,
            "partition_key": None,
            "parent_schema_name": None,
            "parent_relation_name": None,
        },
    ]
    column_rows = [
        {
            "relation_oid": 20,
            "column_name": "name",
            "data_type": "text",
            "nullable": False,
            "ordinal_position": 2,
        },
        {
            "relation_oid": 10,
            "column_name": "user_id",
            "data_type": "integer",
            "nullable": False,
            "ordinal_position": 2,
        },
        {
            "relation_oid": 20,
            "column_name": "id",
            "data_type": "integer",
            "nullable": False,
            "ordinal_position": 1,
        },
        {
            "relation_oid": 10,
            "column_name": "id",
            "data_type": "integer",
            "nullable": False,
            "ordinal_position": 1,
        },
    ]
    constraint_rows = [
        {
            "relation_oid": 10,
            "constraint_type": b"f",
            "constraint_name": "complaints_user_id_fkey",
            "columns": ["user_id"],
            "target_schema_name": "public",
            "target_relation_name": "users",
            "target_columns": ["id"],
        },
        {
            "relation_oid": 20,
            "constraint_type": b"p",
            "constraint_name": "users_pkey",
            "columns": ["id"],
            "target_schema_name": None,
            "target_relation_name": None,
            "target_columns": [],
        },
        {
            "relation_oid": 10,
            "constraint_type": b"p",
            "constraint_name": "complaints_pkey",
            "columns": ["id"],
            "target_schema_name": None,
            "target_relation_name": None,
            "target_columns": [],
        },
    ]
    index_rows = [
        {
            "relation_oid": 10,
            "index_name": "complaints_user_id_idx",
            "is_unique": False,
            "is_primary": False,
            "columns": ["user_id"],
            "predicate": None,
        },
        {
            "relation_oid": 20,
            "index_name": "users_pkey",
            "is_unique": True,
            "is_primary": True,
            "columns": ["id"],
            "predicate": None,
        },
        {
            "relation_oid": 10,
            "index_name": "complaints_pkey",
            "is_unique": True,
            "is_primary": True,
            "columns": ["id"],
            "predicate": None,
        },
    ]
    return relation_rows, column_rows, constraint_rows, index_rows


def _policies() -> dict[str, RelationPolicy]:
    return {
        "public.complaints": RelationPolicy(
            sensitivity="restricted",
            sensitive_columns=("user_id",),
            aggregate_coverage=("metric.complaint_count",),
            freshness_sla_seconds=3_600,
        ),
        "public.users": RelationPolicy(
            sensitivity="internal",
            sensitive_columns=("name",),
            freshness_sla_seconds=3_600,
        ),
    }


def _candidate(
    *,
    relation_rows: list[dict[str, Any]] | None = None,
    column_rows: list[dict[str, Any]] | None = None,
    policies: dict[str, RelationPolicy] | None = None,
    parser_version: str = "postgres-catalog-v1",
) -> SchemaSnapshotCandidate:
    default_relations, default_columns, constraints, indexes = _catalog_rows()
    return build_schema_snapshot_candidate(
        source_identifier="integration-business",
        approved_schemas=("public",),
        relation_rows=relation_rows if relation_rows is not None else default_relations,
        column_rows=column_rows if column_rows is not None else default_columns,
        constraint_rows=constraints,
        index_rows=indexes,
        policies=_policies() if policies is None else policies,
        parser_version=parser_version,
    )


def test_snapshot_checksums_and_payload_are_canonical() -> None:
    relation_rows, column_rows, constraints, indexes = _catalog_rows()
    first = _candidate()
    second = build_schema_snapshot_candidate(
        source_identifier="integration-business",
        approved_schemas=("public",),
        relation_rows=list(reversed(relation_rows)),
        column_rows=list(reversed(column_rows)),
        constraint_rows=list(reversed(constraints)),
        index_rows=list(reversed(indexes)),
        policies=dict(reversed(tuple(_policies().items()))),
    )

    assert first == second
    assert first.checksum == second.checksum
    assert first.schema_checksum == second.schema_checksum
    assert [relation.relation_id for relation in first.relations] == [
        "public.complaints",
        "public.users",
    ]
    payload = first.to_payload()
    assert json.loads(json.dumps(payload)) == payload
    assert schema_snapshot_candidate_from_payload(payload, checksum=first.checksum) == first


def test_structure_checksum_ignores_catalog_statistics_but_full_checksum_tracks_them() -> None:
    baseline = _candidate()
    relation_rows, _, _, _ = _catalog_rows()
    changed_stats = [
        {
            **row,
            "estimated_rows": int(row["estimated_rows"]) + 10_000,
            "total_bytes": int(row["total_bytes"]) + 1_048_576,
        }
        for row in relation_rows
    ]
    current = _candidate(relation_rows=changed_stats)

    assert current.schema_checksum == baseline.schema_checksum
    assert current.checksum != baseline.checksum
    assert validate_schema_snapshot(current, previous=baseline).ok


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("column", "relation_structure_changed"),
        ("removed", "relation_removed"),
        ("added", "relation_added"),
        ("parser", "snapshot_parser_changed"),
    ],
)
def test_unapproved_schema_drift_fails_closed(mutation: str, expected_code: str) -> None:
    baseline = _candidate()
    relation_rows, column_rows, _, _ = _catalog_rows()
    policies = _policies()
    parser_version = "postgres-catalog-v1"
    if mutation == "column":
        column_rows = [
            {**row, "data_type": "bigint"}
            if row["relation_oid"] == 10 and row["column_name"] == "user_id"
            else row
            for row in column_rows
        ]
    elif mutation == "removed":
        relation_rows = [row for row in relation_rows if row["relation_oid"] != 20]
        column_rows = [row for row in column_rows if row["relation_oid"] != 20]
        policies.pop("public.users")
    elif mutation == "added":
        relation_rows.append(
            {
                "relation_oid": 30,
                "schema_name": "public",
                "relation_name": "teams",
                "relation_kind": "table",
                "estimated_rows": 3,
                "total_bytes": 8_192,
                "partition_key": None,
                "parent_schema_name": None,
                "parent_relation_name": None,
            }
        )
        column_rows.append(
            {
                "relation_oid": 30,
                "column_name": "id",
                "data_type": "integer",
                "nullable": False,
                "ordinal_position": 1,
            }
        )
        policies["public.teams"] = RelationPolicy(
            sensitivity="internal",
            freshness_sla_seconds=3_600,
        )
    else:
        parser_version = "postgres-catalog-v2"

    current = _candidate(
        relation_rows=relation_rows,
        column_rows=column_rows,
        policies=policies,
        parser_version=parser_version,
    )
    report = validate_schema_snapshot(current, previous=baseline)

    assert not report.ok
    assert expected_code in {issue.code for issue in report.issues}


def test_required_relations_columns_and_policy_metadata_are_validated() -> None:
    candidate = _candidate()
    requirements = (
        SchemaRequirement("public.complaints", ("id", "missing_column")),
        SchemaRequirement("public.missing_relation"),
    )
    report = validate_schema_snapshot(candidate, requirements=requirements)

    assert not report.ok
    assert {issue.code for issue in report.issues} == {
        "required_column_missing",
        "required_relation_missing",
    }

    _, columns, _, _ = _catalog_rows()
    invalid_policy = _policies()
    invalid_policy["public.complaints"] = RelationPolicy(
        sensitivity="restricted",
        sensitive_columns=("deleted_column",),
        freshness_sla_seconds=0,
    )
    invalid = _candidate(column_rows=columns, policies=invalid_policy)
    invalid_report = validate_schema_snapshot(invalid)
    assert not invalid_report.ok
    assert {issue.code for issue in invalid_report.issues} == {
        "invalid_freshness_sla",
        "sensitive_column_missing",
    }

    unclassified = _candidate(policies={})
    warning_codes = {issue.code for issue in validate_schema_snapshot(unclassified).issues}
    assert warning_codes == {"freshness_unknown", "sensitivity_unclassified"}


def test_policy_and_partition_links_cannot_escape_the_collected_slice() -> None:
    relation_rows, _, _, _ = _catalog_rows()
    relation_rows[0] = {
        **relation_rows[0],
        "parent_schema_name": "private",
        "parent_relation_name": "hidden_users",
    }
    candidate = _candidate(relation_rows=relation_rows)
    assert candidate.relations[1].parent_relation_id is None

    with pytest.raises(SchemaSnapshotError, match="uncollected relation"):
        _candidate(
            policies={
                **_policies(),
                "public.secret_table": RelationPolicy(sensitivity="restricted"),
            }
        )


def test_relation_policy_file_is_typed_and_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "schema-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "public.complaints": {
                    "sensitivity": "restricted",
                    "sensitive_columns": ["user_id"],
                    "aggregate_coverage": ["metric.complaint_count"],
                    "freshness_sla_seconds": 3_600,
                }
            }
        ),
        encoding="utf-8",
    )

    policies = load_relation_policies(policy_path)
    assert policies["public.complaints"] == RelationPolicy(
        sensitivity="restricted",
        sensitive_columns=("user_id",),
        aggregate_coverage=("metric.complaint_count",),
        freshness_sla_seconds=3_600,
    )

    policy_path.write_text(
        json.dumps({"public.complaints": {"unexpected": True}}),
        encoding="utf-8",
    )
    with pytest.raises(SchemaSnapshotError, match="unknown schema snapshot policy field"):
        load_relation_policies(policy_path)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return _Rows(self._rows)


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection

    async def __aenter__(self) -> None:
        self._connection.in_transaction = True

    async def __aexit__(self, *_: object) -> None:
        self._connection.in_transaction = False


class _Connection:
    def __init__(self, relation_rows: list[dict[str, Any]]) -> None:
        self._relation_rows = relation_rows
        self.in_transaction = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def begin(self) -> _Transaction:
        return _Transaction(self)

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any] | None = None,
    ) -> _Result:
        assert self.in_transaction
        sql = str(statement)
        self.calls.append((sql, dict(parameters or {})))
        if "FROM pg_catalog.pg_class c" in sql:
            return _Result(self._relation_rows)
        return _Result([])


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self, relation_rows: list[dict[str, Any]]) -> None:
        self.connection = _Connection(relation_rows)

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)


def _many_relation_rows(count: int) -> list[dict[str, Any]]:
    return [
        {
            "relation_oid": index,
            "schema_name": "public",
            "relation_name": f"approved_{index:02d}",
            "relation_kind": "view",
            "estimated_rows": 0,
            "total_bytes": 0,
            "partition_key": None,
            "parent_schema_name": None,
            "parent_relation_name": None,
        }
        for index in range(1, count + 1)
    ]


@pytest.mark.asyncio
async def test_thirty_plus_relations_stay_inside_one_bounded_catalog_slice() -> None:
    relation_rows = _many_relation_rows(35)
    approved_relations = tuple(
        reversed(tuple(f"public.approved_{index:02d}" for index in range(1, 36)))
    )
    engine = _Engine(relation_rows)
    collector = PostgresSchemaSnapshotCollector(engine, max_relations=40)  # type: ignore[arg-type]

    candidate = await collector.collect(
        source_identifier="bounded-business",
        approved_schemas=("public",),
        approved_relations=approved_relations,
    )

    assert len(candidate.relations) == 35
    assert {relation.relation_id for relation in candidate.relations} == set(
        approved_relations
    )
    statements = engine.connection.calls
    assert len(statements) == 5
    assert statements[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    relation_parameters = statements[1][1]
    assert relation_parameters["relation_limit"] == 41
    assert relation_parameters["approved_relations"] == sorted(approved_relations)
    assert "approved_relations AS text[]" in statements[1][0]
    assert all("information_schema" not in sql for sql, _ in statements)
    assert all(
        not parameters.get("relation_oids")
        or len(parameters["relation_oids"]) == 35
        for _, parameters in statements
    )


@pytest.mark.asyncio
async def test_collector_rejects_catalog_results_above_the_configured_bound() -> None:
    engine = _Engine(_many_relation_rows(36))
    collector = PostgresSchemaSnapshotCollector(engine, max_relations=35)  # type: ignore[arg-type]

    with pytest.raises(SchemaSnapshotError, match="relation bound"):
        await collector.collect(
            source_identifier="bounded-business",
            approved_schemas=("public",),
        )


def _release_candidate() -> SemanticReleaseCandidate:
    document = SemanticDocument(
        document_id="relation.public.complaints",
        content="public.complaints",
        metadata={"domain": "complaint"},
    )
    asset = SemanticAssetRecord(
        asset_id=document.document_id,
        asset_type="relation",
        status="active",
        domain="complaint",
        owner="data-platform",
        sensitivity="internal",
        content=document.content,
        payload={"relation_name": "public.complaints"},
    )
    return SemanticReleaseCandidate(
        checksum="semantic-materialization-checksum",
        documents=(document,),
        validation_report={"ok": True},
        assets=(asset,),
        parser_version="semantic-authoring-v3",
    )


def _snapshot(state: SchemaSnapshotState = SchemaSnapshotState.VALIDATED) -> SchemaSnapshot:
    candidate = _candidate()
    report = validate_schema_snapshot(candidate)
    now = datetime.now(UTC)
    return SchemaSnapshot(
        snapshot_id="00000000-0000-0000-0000-000000000101",
        state=state,
        candidate=candidate,
        validation_report=report.to_dict(),
        created_at=now,
        validated_at=now if state is SchemaSnapshotState.VALIDATED else None,
    )


def test_validated_snapshot_binding_extends_the_semantic_release_checksum() -> None:
    candidate = _release_candidate()
    snapshot = _snapshot()

    bound = bind_schema_snapshot(candidate, snapshot)

    assert bound.schema_snapshot_id == snapshot.snapshot_id
    assert bound.schema_snapshot_checksum == snapshot.checksum
    assert bound.checksum != candidate.checksum
    assert bound.validation_report["schema_snapshot_checksum"] == snapshot.checksum
    assert bind_schema_snapshot(bound, snapshot) is bound

    with pytest.raises(SemanticReleaseError, match="validated schema snapshot"):
        bind_schema_snapshot(candidate, _snapshot(SchemaSnapshotState.REJECTED))


@pytest.mark.asyncio
async def test_typed_publisher_rejects_an_unbound_snapshot_before_database_io() -> None:
    publisher = ControlSemanticReleasePublisher(engine=object())  # type: ignore[arg-type]

    with pytest.raises(SemanticReleaseError, match="require a validated schema snapshot"):
        await publisher.publish_candidate(
            _release_candidate(),
            change_summary="must fail before database I/O",
        )


def test_snapshot_checksum_integrity_is_rechecked_before_validation() -> None:
    candidate = replace(_candidate(), checksum="0" * 64)

    with pytest.raises(SchemaSnapshotError, match="candidate checksum mismatch"):
        validate_schema_snapshot(candidate)
