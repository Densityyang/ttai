"""Offline, bounded PostgreSQL schema snapshots for semantic release validation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.database import DatabasePurpose, create_runtime_async_engine
from src.core.settings import get_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.semantic.registry import SemanticReleaseCandidate, SemanticReleaseError

SCHEMA_SNAPSHOT_PARSER_VERSION = "postgres-catalog-v1"
DEFAULT_MAX_RELATIONS = 64
ABSOLUTE_MAX_RELATIONS = 256

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_POLICY_FIELDS = {
    "aggregate_coverage",
    "freshness_sla_seconds",
    "sensitive_columns",
    "sensitivity",
}


class SchemaSnapshotState(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"
    RETIRED = "retired"


class SnapshotIssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ColumnSnapshot:
    name: str
    data_type: str
    nullable: bool
    ordinal_position: int


@dataclass(frozen=True, slots=True)
class ForeignKeySnapshot:
    name: str
    columns: tuple[str, ...]
    target_relation_id: str
    target_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IndexSnapshot:
    name: str
    columns: tuple[str, ...]
    unique: bool
    primary: bool
    predicate: str | None = None


@dataclass(frozen=True, slots=True)
class RelationPolicy:
    sensitivity: str = "unclassified"
    sensitive_columns: tuple[str, ...] = ()
    aggregate_coverage: tuple[str, ...] = ()
    freshness_sla_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class RelationSnapshot:
    relation_id: str
    schema_name: str
    relation_name: str
    relation_kind: str
    columns: tuple[ColumnSnapshot, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeySnapshot, ...]
    indexes: tuple[IndexSnapshot, ...]
    partition_key: str | None
    parent_relation_id: str | None
    estimated_rows: int
    total_bytes: int
    sensitivity: str
    sensitive_columns: tuple[str, ...]
    aggregate_coverage: tuple[str, ...]
    freshness_sla_seconds: int | None


@dataclass(frozen=True, slots=True)
class SchemaRequirement:
    relation_id: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SchemaSnapshotIssue:
    code: str
    severity: SnapshotIssueSeverity
    message: str
    relation_id: str | None = None
    path: str = ""

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "relation_id": self.relation_id,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class SchemaSnapshotValidationReport:
    candidate_checksum: str
    schema_checksum: str
    relation_count: int
    issues: tuple[SchemaSnapshotIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(issue.severity is SnapshotIssueSeverity.ERROR for issue in self.issues)

    @property
    def error_count(self) -> int:
        return sum(issue.severity is SnapshotIssueSeverity.ERROR for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity is SnapshotIssueSeverity.WARNING for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_checksum": self.candidate_checksum,
            "error_count": self.error_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "ok": self.ok,
            "relation_count": self.relation_count,
            "schema_checksum": self.schema_checksum,
            "warning_count": self.warning_count,
        }


@dataclass(frozen=True, slots=True)
class SchemaSnapshotCandidate:
    source_identifier: str
    approved_schemas: tuple[str, ...]
    relations: tuple[RelationSnapshot, ...]
    checksum: str
    schema_checksum: str
    parser_version: str = SCHEMA_SNAPSHOT_PARSER_VERSION

    def to_payload(self) -> dict[str, Any]:
        return _candidate_payload(self)

    def relation_columns(self) -> dict[str, frozenset[str]]:
        """Expose qualified names plus unambiguous short names for authoring validation."""
        short_name_counts: dict[str, int] = defaultdict(int)
        for relation in self.relations:
            short_name_counts[relation.relation_name.casefold()] += 1
        result: dict[str, frozenset[str]] = {}
        for relation in self.relations:
            columns = frozenset(column.name for column in relation.columns)
            result[relation.relation_id] = columns
            if short_name_counts[relation.relation_name.casefold()] == 1:
                result[relation.relation_name] = columns
        return result


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    snapshot_id: str
    state: SchemaSnapshotState
    candidate: SchemaSnapshotCandidate
    validation_report: dict[str, Any]
    created_at: datetime
    validated_at: datetime | None

    @property
    def checksum(self) -> str:
        return self.candidate.checksum


class SchemaSnapshotError(RuntimeError):
    pass


_RELATION_QUERY = """
SELECT
  c.oid::bigint AS relation_oid,
  n.nspname AS schema_name,
  c.relname AS relation_name,
  CASE c.relkind
    WHEN 'r' THEN 'table'
    WHEN 'p' THEN 'partitioned_table'
    WHEN 'v' THEN 'view'
    WHEN 'm' THEN 'materialized_view'
    WHEN 'f' THEN 'foreign_table'
    ELSE c.relkind::text
  END AS relation_kind,
  GREATEST(c.reltuples, 0)::bigint AS estimated_rows,
  CASE WHEN c.relkind IN ('r', 'p', 'm')
       THEN pg_catalog.pg_total_relation_size(c.oid)::bigint ELSE 0::bigint END AS total_bytes,
  CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END AS partition_key,
  parent_namespace.nspname AS parent_schema_name,
  parent_relation.relname AS parent_relation_name
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN LATERAL (
  SELECT inheritance.inhparent
  FROM pg_catalog.pg_inherits inheritance
  WHERE inheritance.inhrelid = c.oid
  ORDER BY inheritance.inhseqno
  LIMIT 1
) parent ON true
LEFT JOIN pg_catalog.pg_class parent_relation ON parent_relation.oid = parent.inhparent
LEFT JOIN pg_catalog.pg_namespace parent_namespace
  ON parent_namespace.oid = parent_relation.relnamespace
WHERE n.nspname = ANY(CAST(:approved_schemas AS text[]))
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
{relation_filter}
ORDER BY n.nspname, c.relname
LIMIT :relation_limit
"""

_COLUMN_QUERY = """
SELECT
  attribute.attrelid::bigint AS relation_oid,
  attribute.attname AS column_name,
  pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
  NOT attribute.attnotnull AS nullable,
  attribute.attnum AS ordinal_position
FROM pg_catalog.pg_attribute attribute
WHERE attribute.attrelid::bigint = ANY(CAST(:relation_oids AS bigint[]))
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
ORDER BY attribute.attrelid, attribute.attnum
"""

_CONSTRAINT_QUERY = """
SELECT
  constraint_row.conrelid::bigint AS relation_oid,
  constraint_row.contype AS constraint_type,
  constraint_row.conname AS constraint_name,
  ARRAY(
    SELECT attribute.attname
    FROM pg_catalog.unnest(constraint_row.conkey) WITH ORDINALITY keys(attnum, position)
    JOIN pg_catalog.pg_attribute attribute
      ON attribute.attrelid = constraint_row.conrelid
     AND attribute.attnum = keys.attnum
    ORDER BY keys.position
  ) AS columns,
  target_namespace.nspname AS target_schema_name,
  target_relation.relname AS target_relation_name,
  ARRAY(
    SELECT attribute.attname
    FROM pg_catalog.unnest(constraint_row.confkey) WITH ORDINALITY keys(attnum, position)
    JOIN pg_catalog.pg_attribute attribute
      ON attribute.attrelid = constraint_row.confrelid
     AND attribute.attnum = keys.attnum
    ORDER BY keys.position
  ) AS target_columns
FROM pg_catalog.pg_constraint constraint_row
LEFT JOIN pg_catalog.pg_class target_relation
  ON target_relation.oid = constraint_row.confrelid
LEFT JOIN pg_catalog.pg_namespace target_namespace
  ON target_namespace.oid = target_relation.relnamespace
WHERE constraint_row.conrelid::bigint = ANY(CAST(:relation_oids AS bigint[]))
  AND constraint_row.contype IN ('p', 'f')
ORDER BY constraint_row.conrelid, constraint_row.contype, constraint_row.conname
"""

_INDEX_QUERY = """
SELECT
  index_row.indrelid::bigint AS relation_oid,
  index_relation.relname AS index_name,
  index_row.indisunique AS is_unique,
  index_row.indisprimary AS is_primary,
  ARRAY(
    SELECT pg_catalog.pg_get_indexdef(index_row.indexrelid, position, true)
    FROM pg_catalog.generate_series(1, index_row.indnkeyatts) position
    ORDER BY position
  ) AS columns,
  pg_catalog.pg_get_expr(index_row.indpred, index_row.indrelid) AS predicate
FROM pg_catalog.pg_index index_row
JOIN pg_catalog.pg_class index_relation ON index_relation.oid = index_row.indexrelid
WHERE index_row.indrelid::bigint = ANY(CAST(:relation_oids AS bigint[]))
ORDER BY index_row.indrelid, index_relation.relname
"""


class PostgresSchemaSnapshotCollector:
    """Collect one approved catalog slice without scanning business rows."""

    def __init__(self, engine: AsyncEngine, *, max_relations: int = DEFAULT_MAX_RELATIONS) -> None:
        if not 1 <= max_relations <= ABSOLUTE_MAX_RELATIONS:
            raise ValueError(
                f"max_relations must be between 1 and {ABSOLUTE_MAX_RELATIONS}"
            )
        self._engine = engine
        self._max_relations = max_relations

    async def collect(
        self,
        *,
        source_identifier: str,
        approved_schemas: Iterable[str],
        approved_relations: Iterable[str] = (),
        policies: Mapping[str, RelationPolicy] | None = None,
    ) -> SchemaSnapshotCandidate:
        source = source_identifier.strip()
        if not source:
            raise SchemaSnapshotError("schema snapshot source_identifier must not be empty")
        schemas = _normalize_schemas(approved_schemas)
        selected_relations = _normalize_relation_refs(approved_relations, schemas)
        if len(selected_relations) > self._max_relations:
            raise SchemaSnapshotError("approved relation count exceeds the snapshot bound")
        normalized_policies = {
            _normalize_relation_ref(relation_id, schemas): policy
            for relation_id, policy in (policies or {}).items()
        }
        if any(
            not isinstance(policy, RelationPolicy)
            for policy in normalized_policies.values()
        ):
            raise SchemaSnapshotError("schema snapshot policies must use RelationPolicy values")
        relation_filter = ""
        parameters: dict[str, Any] = {
            "approved_schemas": list(schemas),
            "relation_limit": self._max_relations + 1,
        }
        if selected_relations:
            relation_filter = (
                "  AND (n.nspname || '.' || c.relname) "
                "= ANY(CAST(:approved_relations AS text[]))"
            )
            parameters["approved_relations"] = list(selected_relations)

        async with self._engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                relation_rows = tuple(
                    dict(row)
                    for row in (
                        await connection.execute(
                            text(_RELATION_QUERY.format(relation_filter=relation_filter)),
                            parameters,
                        )
                    ).mappings().all()
                )
                if len(relation_rows) > self._max_relations:
                    raise SchemaSnapshotError("catalog slice exceeds the snapshot relation bound")
                relation_oids = [int(row["relation_oid"]) for row in relation_rows]
                if relation_oids:
                    catalog_parameters = {"relation_oids": relation_oids}
                    column_rows = tuple(
                        dict(row)
                        for row in (
                            await connection.execute(text(_COLUMN_QUERY), catalog_parameters)
                        ).mappings().all()
                    )
                    constraint_rows = tuple(
                        dict(row)
                        for row in (
                            await connection.execute(
                                text(_CONSTRAINT_QUERY), catalog_parameters
                            )
                        ).mappings().all()
                    )
                    index_rows = tuple(
                        dict(row)
                        for row in (
                            await connection.execute(text(_INDEX_QUERY), catalog_parameters)
                        ).mappings().all()
                    )
                else:
                    column_rows = ()
                    constraint_rows = ()
                    index_rows = ()

        return build_schema_snapshot_candidate(
            source_identifier=source,
            approved_schemas=schemas,
            relation_rows=relation_rows,
            column_rows=column_rows,
            constraint_rows=constraint_rows,
            index_rows=index_rows,
            policies=normalized_policies,
        )


def build_schema_snapshot_candidate(
    *,
    source_identifier: str,
    approved_schemas: Iterable[str],
    relation_rows: Sequence[Mapping[str, Any]],
    column_rows: Sequence[Mapping[str, Any]] = (),
    constraint_rows: Sequence[Mapping[str, Any]] = (),
    index_rows: Sequence[Mapping[str, Any]] = (),
    policies: Mapping[str, RelationPolicy] | None = None,
    parser_version: str = SCHEMA_SNAPSHOT_PARSER_VERSION,
) -> SchemaSnapshotCandidate:
    source = source_identifier.strip()
    if not source:
        raise SchemaSnapshotError("schema snapshot source_identifier must not be empty")
    if not parser_version.strip():
        raise SchemaSnapshotError("schema snapshot parser_version must not be empty")
    schemas = _normalize_schemas(approved_schemas)
    policy_map = {
        _normalize_relation_ref(relation_id, schemas): policy
        for relation_id, policy in (policies or {}).items()
    }
    if any(not isinstance(policy, RelationPolicy) for policy in policy_map.values()):
        raise SchemaSnapshotError("schema snapshot policies must use RelationPolicy values")

    relation_identity_by_oid: dict[int, str] = {}
    raw_relations: dict[int, Mapping[str, Any]] = {}
    for row in relation_rows:
        relation_oid = int(row["relation_oid"])
        schema_name = str(row["schema_name"])
        relation_name = str(row["relation_name"])
        relation_id = _relation_id(schema_name, relation_name)
        if schema_name not in schemas:
            raise SchemaSnapshotError(f"catalog returned an unapproved schema: {relation_id}")
        if relation_oid in raw_relations or relation_id in relation_identity_by_oid.values():
            raise SchemaSnapshotError(f"duplicate catalog relation: {relation_id}")
        relation_identity_by_oid[relation_oid] = relation_id
        raw_relations[relation_oid] = row

    columns_by_oid: dict[int, list[ColumnSnapshot]] = defaultdict(list)
    for row in column_rows:
        relation_oid = int(row["relation_oid"])
        if relation_oid not in raw_relations:
            continue
        columns_by_oid[relation_oid].append(
            ColumnSnapshot(
                name=str(row["column_name"]),
                data_type=str(row["data_type"]),
                nullable=bool(row["nullable"]),
                ordinal_position=int(row["ordinal_position"]),
            )
        )

    primary_keys_by_oid: dict[int, tuple[str, ...]] = {}
    foreign_keys_by_oid: dict[int, list[ForeignKeySnapshot]] = defaultdict(list)
    selected_relation_ids = set(relation_identity_by_oid.values())
    unknown_policy_relations = sorted(policy_map.keys() - selected_relation_ids)
    if unknown_policy_relations:
        raise SchemaSnapshotError(
            "schema snapshot policy references an uncollected relation: "
            f"{unknown_policy_relations[0]}"
        )
    for row in constraint_rows:
        relation_oid = int(row["relation_oid"])
        if relation_oid not in raw_relations:
            continue
        constraint_type = _text_value(row["constraint_type"])
        columns = _text_tuple(row.get("columns"))
        if constraint_type == "p":
            if relation_oid in primary_keys_by_oid:
                raise SchemaSnapshotError(
                    f"relation has multiple primary key rows: {relation_identity_by_oid[relation_oid]}"
                )
            primary_keys_by_oid[relation_oid] = columns
        elif constraint_type == "f":
            target_schema = str(row.get("target_schema_name") or "")
            target_relation = str(row.get("target_relation_name") or "")
            if not target_schema or not target_relation:
                continue
            possible_target_id = f"{target_schema}.{target_relation}"
            if possible_target_id not in selected_relation_ids:
                continue
            target_relation_id = _relation_id(target_schema, target_relation)
            foreign_keys_by_oid[relation_oid].append(
                ForeignKeySnapshot(
                    name=str(row["constraint_name"]),
                    columns=columns,
                    target_relation_id=target_relation_id,
                    target_columns=_text_tuple(row.get("target_columns")),
                )
            )

    indexes_by_oid: dict[int, list[IndexSnapshot]] = defaultdict(list)
    for row in index_rows:
        relation_oid = int(row["relation_oid"])
        if relation_oid not in raw_relations:
            continue
        predicate = str(row.get("predicate") or "").strip() or None
        indexes_by_oid[relation_oid].append(
            IndexSnapshot(
                name=str(row["index_name"]),
                columns=_text_tuple(row.get("columns")),
                unique=bool(row["is_unique"]),
                primary=bool(row["is_primary"]),
                predicate=predicate,
            )
        )

    relations: list[RelationSnapshot] = []
    for relation_oid, row in raw_relations.items():
        relation_id = relation_identity_by_oid[relation_oid]
        policy = policy_map.get(relation_id, RelationPolicy())
        parent_schema = str(row.get("parent_schema_name") or "")
        parent_relation = str(row.get("parent_relation_name") or "")
        parent_relation_id = None
        if parent_schema and parent_relation:
            possible_parent_id = f"{parent_schema}.{parent_relation}"
            if possible_parent_id in selected_relation_ids:
                parent_relation_id = _relation_id(parent_schema, parent_relation)
        relations.append(
            RelationSnapshot(
                relation_id=relation_id,
                schema_name=str(row["schema_name"]),
                relation_name=str(row["relation_name"]),
                relation_kind=str(row["relation_kind"]),
                columns=tuple(
                    sorted(
                        columns_by_oid[relation_oid],
                        key=lambda column: (column.ordinal_position, column.name),
                    )
                ),
                primary_key=primary_keys_by_oid.get(relation_oid, ()),
                foreign_keys=tuple(
                    sorted(
                        foreign_keys_by_oid[relation_oid],
                        key=lambda foreign_key: foreign_key.name,
                    )
                ),
                indexes=tuple(
                    sorted(indexes_by_oid[relation_oid], key=lambda index: index.name)
                ),
                partition_key=str(row.get("partition_key") or "").strip() or None,
                parent_relation_id=parent_relation_id,
                estimated_rows=max(int(row.get("estimated_rows") or 0), 0),
                total_bytes=max(int(row.get("total_bytes") or 0), 0),
                sensitivity=policy.sensitivity.strip() or "unclassified",
                sensitive_columns=tuple(sorted(set(policy.sensitive_columns))),
                aggregate_coverage=tuple(sorted(set(policy.aggregate_coverage))),
                freshness_sla_seconds=policy.freshness_sla_seconds,
            )
        )

    ordered_relations = tuple(sorted(relations, key=lambda relation: relation.relation_id))
    schema_checksum = _sha256(
        {
            "approved_schemas": list(schemas),
            "parser_version": parser_version,
            "relations": [_relation_structure_payload(relation) for relation in ordered_relations],
            "source_identifier": source,
        }
    )
    candidate_without_checksum = SchemaSnapshotCandidate(
        source_identifier=source,
        approved_schemas=schemas,
        relations=ordered_relations,
        checksum="",
        schema_checksum=schema_checksum,
        parser_version=parser_version,
    )
    checksum = _sha256(_candidate_payload(candidate_without_checksum))
    return replace(candidate_without_checksum, checksum=checksum)


def load_relation_policies(path: Path) -> dict[str, RelationPolicy]:
    """Load explicit relation policy metadata without accepting unknown fields."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchemaSnapshotError(f"cannot load schema snapshot policy file: {path}") from exc
    if not isinstance(loaded, dict):
        raise SchemaSnapshotError("schema snapshot policy file must contain a JSON object")

    policies: dict[str, RelationPolicy] = {}
    for relation_id, raw_policy in loaded.items():
        if not isinstance(relation_id, str) or not relation_id.strip():
            raise SchemaSnapshotError("schema snapshot policy relation ids must be strings")
        if not isinstance(raw_policy, dict):
            raise SchemaSnapshotError(
                f"schema snapshot policy must be an object: {relation_id}"
            )
        unknown_fields = sorted(set(raw_policy) - _POLICY_FIELDS)
        if unknown_fields:
            raise SchemaSnapshotError(
                f"unknown schema snapshot policy field for {relation_id}: "
                f"{unknown_fields[0]}"
            )
        sensitivity = raw_policy.get("sensitivity", "unclassified")
        if not isinstance(sensitivity, str):
            raise SchemaSnapshotError(
                f"schema snapshot sensitivity must be a string: {relation_id}"
            )
        freshness = raw_policy.get("freshness_sla_seconds")
        if freshness is not None and (
            isinstance(freshness, bool) or not isinstance(freshness, int)
        ):
            raise SchemaSnapshotError(
                f"schema snapshot freshness SLA must be an integer: {relation_id}"
            )
        policies[relation_id] = RelationPolicy(
            sensitivity=sensitivity,
            sensitive_columns=_policy_string_tuple(
                raw_policy.get("sensitive_columns"),
                relation_id=relation_id,
                field="sensitive_columns",
            ),
            aggregate_coverage=_policy_string_tuple(
                raw_policy.get("aggregate_coverage"),
                relation_id=relation_id,
                field="aggregate_coverage",
            ),
            freshness_sla_seconds=freshness,
        )
    return policies


def validate_schema_snapshot(
    candidate: SchemaSnapshotCandidate,
    *,
    requirements: Iterable[SchemaRequirement] = (),
    previous: SchemaSnapshotCandidate | None = None,
) -> SchemaSnapshotValidationReport:
    """Validate required relations and fail closed on unapproved structural drift."""
    _validate_candidate_checksums(candidate)
    issues: list[SchemaSnapshotIssue] = []
    relations = {relation.relation_id: relation for relation in candidate.relations}
    if not relations:
        issues.append(
            SchemaSnapshotIssue(
                code="empty_schema_snapshot",
                severity=SnapshotIssueSeverity.ERROR,
                message="approved schema snapshot contains no relations",
            )
        )
    if len(relations) > ABSOLUTE_MAX_RELATIONS:
        issues.append(
            SchemaSnapshotIssue(
                code="relation_bound_exceeded",
                severity=SnapshotIssueSeverity.ERROR,
                message=f"schema snapshot exceeds {ABSOLUTE_MAX_RELATIONS} relations",
            )
        )

    normalized_requirements: dict[str, set[str]] = defaultdict(set)
    for requirement in requirements:
        relation_id = _normalize_relation_ref(
            requirement.relation_id,
            candidate.approved_schemas,
        )
        normalized_requirements[relation_id].update(
            column.strip() for column in requirement.columns if column.strip()
        )
    for relation_id, required_columns in sorted(normalized_requirements.items()):
        relation = relations.get(relation_id)
        if relation is None:
            issues.append(
                SchemaSnapshotIssue(
                    code="required_relation_missing",
                    severity=SnapshotIssueSeverity.ERROR,
                    message=f"required relation is missing from PostgreSQL: {relation_id}",
                    relation_id=relation_id,
                )
            )
            continue
        available_columns = {column.name for column in relation.columns}
        for column in sorted(required_columns - available_columns):
            issues.append(
                SchemaSnapshotIssue(
                    code="required_column_missing",
                    severity=SnapshotIssueSeverity.ERROR,
                    message=f"required column is missing from PostgreSQL: {relation_id}.{column}",
                    relation_id=relation_id,
                    path=f"columns.{column}",
                )
            )

    for relation in candidate.relations:
        column_names = {column.name for column in relation.columns}
        if not relation.columns:
            issues.append(
                SchemaSnapshotIssue(
                    code="relation_has_no_columns",
                    severity=SnapshotIssueSeverity.WARNING,
                    message="approved relation has no visible columns",
                    relation_id=relation.relation_id,
                    path="columns",
                )
            )
        if relation.sensitivity == "unclassified":
            issues.append(
                SchemaSnapshotIssue(
                    code="sensitivity_unclassified",
                    severity=SnapshotIssueSeverity.WARNING,
                    message="approved relation does not have a sensitivity classification",
                    relation_id=relation.relation_id,
                    path="sensitivity",
                )
            )
        if relation.freshness_sla_seconds is None:
            issues.append(
                SchemaSnapshotIssue(
                    code="freshness_unknown",
                    severity=SnapshotIssueSeverity.WARNING,
                    message="approved relation does not have a freshness SLA",
                    relation_id=relation.relation_id,
                    path="freshness_sla_seconds",
                )
            )
        elif relation.freshness_sla_seconds <= 0:
            issues.append(
                SchemaSnapshotIssue(
                    code="invalid_freshness_sla",
                    severity=SnapshotIssueSeverity.ERROR,
                    message="freshness SLA must be positive",
                    relation_id=relation.relation_id,
                    path="freshness_sla_seconds",
                )
            )
        for column in sorted(set(relation.sensitive_columns) - column_names):
            issues.append(
                SchemaSnapshotIssue(
                    code="sensitive_column_missing",
                    severity=SnapshotIssueSeverity.ERROR,
                    message=f"classified sensitive column does not exist: {column}",
                    relation_id=relation.relation_id,
                    path=f"sensitive_columns.{column}",
                )
            )

    if previous is not None:
        issues.extend(_schema_drift_issues(previous, candidate))

    ordered_issues = tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.severity.value,
                issue.relation_id or "",
                issue.code,
                issue.path,
            ),
        )
    )
    return SchemaSnapshotValidationReport(
        candidate_checksum=candidate.checksum,
        schema_checksum=candidate.schema_checksum,
        relation_count=len(candidate.relations),
        issues=ordered_issues,
    )


def _schema_drift_issues(
    previous: SchemaSnapshotCandidate,
    candidate: SchemaSnapshotCandidate,
) -> list[SchemaSnapshotIssue]:
    issues: list[SchemaSnapshotIssue] = []
    if previous.source_identifier != candidate.source_identifier:
        issues.append(
            SchemaSnapshotIssue(
                code="snapshot_source_changed",
                severity=SnapshotIssueSeverity.ERROR,
                message="schema snapshot source identifier changed",
                path="source_identifier",
            )
        )
    if previous.parser_version != candidate.parser_version:
        issues.append(
            SchemaSnapshotIssue(
                code="snapshot_parser_changed",
                severity=SnapshotIssueSeverity.ERROR,
                message="schema snapshot parser version changed and requires explicit approval",
                path="parser_version",
            )
        )
    if previous.approved_schemas != candidate.approved_schemas:
        issues.append(
            SchemaSnapshotIssue(
                code="approved_schema_drift",
                severity=SnapshotIssueSeverity.ERROR,
                message="approved schema set changed and requires explicit approval",
                path="approved_schemas",
            )
        )

    previous_relations = {
        relation.relation_id: _relation_structure_payload(relation)
        for relation in previous.relations
    }
    candidate_relations = {
        relation.relation_id: _relation_structure_payload(relation)
        for relation in candidate.relations
    }
    for relation_id in sorted(previous_relations.keys() - candidate_relations.keys()):
        issues.append(
            SchemaSnapshotIssue(
                code="relation_removed",
                severity=SnapshotIssueSeverity.ERROR,
                message="approved relation was removed from the schema snapshot",
                relation_id=relation_id,
            )
        )
    for relation_id in sorted(candidate_relations.keys() - previous_relations.keys()):
        issues.append(
            SchemaSnapshotIssue(
                code="relation_added",
                severity=SnapshotIssueSeverity.ERROR,
                message="new relation requires explicit schema approval",
                relation_id=relation_id,
            )
        )
    for relation_id in sorted(previous_relations.keys() & candidate_relations.keys()):
        if previous_relations[relation_id] != candidate_relations[relation_id]:
            issues.append(
                SchemaSnapshotIssue(
                    code="relation_structure_changed",
                    severity=SnapshotIssueSeverity.ERROR,
                    message="approved relation structure changed",
                    relation_id=relation_id,
                )
            )
    return issues


class ControlSchemaSnapshotStore:
    """Persist snapshot candidates and their validation state in control PostgreSQL."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        self._owns_engine = engine is None
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_runtime_async_engine(
                database_url or "",
                purpose=DatabasePurpose.CONTROL_APP,
                application_name="ttai-schema-snapshot-store",
                settings=get_settings(),
            )

    async def close(self) -> None:
        if self._owns_engine:
            await self._engine.dispose()

    async def publish(
        self,
        candidate: SchemaSnapshotCandidate,
        report: SchemaSnapshotValidationReport,
    ) -> SchemaSnapshot:
        _validate_candidate_checksums(candidate)
        _validate_snapshot_report_alignment(candidate, report)
        snapshot_id = str(uuid4())
        desired_state = (
            SchemaSnapshotState.VALIDATED if report.ok else SchemaSnapshotState.REJECTED
        )
        payload = candidate.to_payload()
        report_payload = report.to_dict()
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO schema_snapshots (
                      snapshot_id, checksum, state, source_identifier, parser_version,
                      approved_schemas, relation_count, payload, validation_report
                    ) VALUES (
                      CAST(:snapshot_id AS uuid), :checksum, 'candidate', :source_identifier,
                      :parser_version, :approved_schemas, :relation_count,
                      CAST(:payload AS jsonb), NULL
                    )
                    ON CONFLICT (checksum) DO NOTHING
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "checksum": candidate.checksum,
                    "source_identifier": candidate.source_identifier,
                    "parser_version": candidate.parser_version,
                    "approved_schemas": list(candidate.approved_schemas),
                    "relation_count": len(candidate.relations),
                    "payload": json.dumps(payload),
                },
            )
            existing = (
                await connection.execute(
                    text(
                        """
                        SELECT snapshot_id::text, checksum, state, source_identifier,
                               parser_version, approved_schemas, relation_count, payload,
                               validation_report, created_at, validated_at
                        FROM schema_snapshots
                        WHERE checksum = :checksum
                        FOR UPDATE
                        """
                    ),
                    {"checksum": candidate.checksum},
                )
            ).mappings().one()
            _validate_persisted_candidate(
                cast(Mapping[str, Any], existing), candidate, payload
            )
            current_state = SchemaSnapshotState(str(existing["state"]))
            if current_state is SchemaSnapshotState.RETIRED:
                raise SchemaSnapshotError("retired schema snapshots cannot be republished")
            if current_state is SchemaSnapshotState.VALIDATED and not report.ok:
                raise SchemaSnapshotError("validated schema snapshots cannot be demoted")
            if current_state is SchemaSnapshotState.VALIDATED:
                return _snapshot_from_row(cast(Mapping[str, Any], existing))

            persisted = (
                await connection.execute(
                    text(
                        """
                        UPDATE schema_snapshots
                        SET state = :state,
                            validation_report = CAST(:validation_report AS jsonb),
                            validated_at = CASE WHEN :state = 'validated' THEN now() ELSE NULL END
                        WHERE snapshot_id = CAST(:snapshot_id AS uuid)
                        RETURNING snapshot_id::text, checksum, state, source_identifier,
                                  parser_version, approved_schemas, relation_count, payload,
                                  validation_report, created_at, validated_at
                        """
                    ),
                    {
                        "snapshot_id": existing["snapshot_id"],
                        "state": desired_state.value,
                        "validation_report": json.dumps(report_payload),
                    },
                )
            ).mappings().one()
        return _snapshot_from_row(cast(Mapping[str, Any], persisted))

    async def read(self, snapshot_id: str) -> SchemaSnapshot | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT snapshot_id::text, checksum, state, source_identifier,
                               parser_version, approved_schemas, relation_count, payload,
                               validation_report, created_at, validated_at
                        FROM schema_snapshots
                        WHERE snapshot_id = CAST(:snapshot_id AS uuid)
                        """
                    ),
                    {"snapshot_id": snapshot_id},
                )
            ).mappings().one_or_none()
        return (
            _snapshot_from_row(cast(Mapping[str, Any], row))
            if row is not None
            else None
        )

    async def read_active(self) -> SchemaSnapshot | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT s.snapshot_id::text, s.checksum, s.state, s.source_identifier,
                               s.parser_version, s.approved_schemas, s.relation_count,
                               s.payload, s.validation_report, s.created_at, s.validated_at
                        FROM semantic_release_pointers p
                        JOIN semantic_releases r
                          ON r.release_id = p.release_id AND r.state = 'active'
                        JOIN schema_snapshots s ON s.snapshot_id = r.schema_snapshot_id
                        WHERE p.pointer_name = 'active'
                        """
                    )
                )
            ).mappings().one_or_none()
        return (
            _snapshot_from_row(cast(Mapping[str, Any], row))
            if row is not None
            else None
        )


def bind_schema_snapshot(
    release_candidate: SemanticReleaseCandidate,
    snapshot: SchemaSnapshot,
) -> SemanticReleaseCandidate:
    """Bind one validated snapshot and extend the release checksum contract."""
    if snapshot.state is not SchemaSnapshotState.VALIDATED:
        raise SemanticReleaseError("semantic release requires a validated schema snapshot")
    try:
        _validate_candidate_checksums(snapshot.candidate)
    except SchemaSnapshotError as exc:
        raise SemanticReleaseError("semantic release schema snapshot is corrupt") from exc
    if (
        snapshot.validation_report.get("ok") is not True
        or snapshot.validation_report.get("candidate_checksum") != snapshot.checksum
        or snapshot.validation_report.get("schema_checksum")
        != snapshot.candidate.schema_checksum
    ):
        raise SemanticReleaseError(
            "semantic release schema snapshot validation report is invalid"
        )
    if release_candidate.schema_snapshot_id is not None:
        if (
            release_candidate.schema_snapshot_id == snapshot.snapshot_id
            and release_candidate.schema_snapshot_checksum == snapshot.checksum
        ):
            return release_candidate
        raise SemanticReleaseError("semantic release is already bound to another schema snapshot")
    validation_report = dict(release_candidate.validation_report)
    validation_report["schema_snapshot_id"] = snapshot.snapshot_id
    validation_report["schema_snapshot_checksum"] = snapshot.checksum
    checksum = _sha256(
        {
            "semantic_materialization_checksum": release_candidate.checksum,
            "schema_snapshot_checksum": snapshot.checksum,
        }
    )
    return replace(
        release_candidate,
        checksum=checksum,
        validation_report=validation_report,
        schema_snapshot_id=snapshot.snapshot_id,
        schema_snapshot_checksum=snapshot.checksum,
    )


def schema_snapshot_candidate_from_payload(
    payload: Mapping[str, Any],
    *,
    checksum: str,
) -> SchemaSnapshotCandidate:
    relations = tuple(
        _relation_from_payload(relation)
        for relation in payload.get("relations", ())
        if isinstance(relation, Mapping)
    )
    candidate = SchemaSnapshotCandidate(
        source_identifier=str(payload.get("source_identifier") or ""),
        approved_schemas=tuple(str(item) for item in payload.get("approved_schemas", ())),
        relations=relations,
        checksum=checksum,
        schema_checksum=str(payload.get("schema_checksum") or ""),
        parser_version=str(payload.get("parser_version") or ""),
    )
    canonical_payload = candidate.to_payload()
    if dict(payload) != canonical_payload:
        raise SchemaSnapshotError("persisted schema snapshot payload is not canonical")
    if _sha256(canonical_payload) != checksum:
        raise SchemaSnapshotError("persisted schema snapshot checksum mismatch")
    expected_schema_checksum = _sha256(
        {
            "approved_schemas": list(candidate.approved_schemas),
            "parser_version": candidate.parser_version,
            "relations": [
                _relation_structure_payload(relation) for relation in candidate.relations
            ],
            "source_identifier": candidate.source_identifier,
        }
    )
    if expected_schema_checksum != candidate.schema_checksum:
        raise SchemaSnapshotError("persisted schema structure checksum mismatch")
    return candidate


async def run_snapshotter(
    *,
    source_identifier: str,
    approved_schemas: Iterable[str],
    approved_relations: Iterable[str] = (),
    policies: Mapping[str, RelationPolicy] | None = None,
    baseline_snapshot_id: str | None = None,
    max_relations: int = DEFAULT_MAX_RELATIONS,
) -> SchemaSnapshot:
    """Run the one-shot collector; this function is never called by API lifespan."""
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for the schema snapshotter")
    if not settings.control_database_url:
        raise RuntimeError("CONTROL_DATABASE_URL is required for the schema snapshotter")
    business_engine = create_runtime_async_engine(
        _async_database_url(settings.database_url),
        purpose=DatabasePurpose.BUSINESS_READ_ONLY,
        application_name="ttai-schema-snapshot-collector",
        settings=settings,
    )
    store = ControlSchemaSnapshotStore(_async_database_url(settings.control_database_url))
    try:
        collector = PostgresSchemaSnapshotCollector(
            business_engine,
            max_relations=max_relations,
        )
        selected_relations = tuple(approved_relations)
        candidate = await collector.collect(
            source_identifier=source_identifier,
            approved_schemas=approved_schemas,
            approved_relations=selected_relations,
            policies=policies,
        )
        baseline = (
            await store.read(baseline_snapshot_id)
            if baseline_snapshot_id is not None
            else await store.read_active()
        )
        requirements = tuple(
            SchemaRequirement(relation_id=relation_id)
            for relation_id in selected_relations
        )
        report = validate_schema_snapshot(
            candidate,
            requirements=requirements,
            previous=baseline.candidate if baseline is not None else None,
        )
        return await store.publish(candidate, report)
    finally:
        await business_engine.dispose()
        await store.close()


def _validate_snapshot_report_alignment(
    candidate: SchemaSnapshotCandidate,
    report: SchemaSnapshotValidationReport,
) -> None:
    if report.candidate_checksum != candidate.checksum:
        raise SchemaSnapshotError("schema snapshot validation report is stale")
    if report.schema_checksum != candidate.schema_checksum:
        raise SchemaSnapshotError("schema snapshot validation report structure is stale")
    if report.relation_count != len(candidate.relations):
        raise SchemaSnapshotError("schema snapshot validation report relation count differs")


def _validate_candidate_checksums(candidate: SchemaSnapshotCandidate) -> None:
    expected_schema_checksum = _sha256(
        {
            "approved_schemas": list(candidate.approved_schemas),
            "parser_version": candidate.parser_version,
            "relations": [
                _relation_structure_payload(relation) for relation in candidate.relations
            ],
            "source_identifier": candidate.source_identifier,
        }
    )
    if expected_schema_checksum != candidate.schema_checksum:
        raise SchemaSnapshotError("schema snapshot structure checksum mismatch")
    if _sha256(candidate.to_payload()) != candidate.checksum:
        raise SchemaSnapshotError("schema snapshot candidate checksum mismatch")


def _validate_persisted_candidate(
    row: Mapping[str, Any],
    candidate: SchemaSnapshotCandidate,
    payload: dict[str, Any],
) -> None:
    if str(row["source_identifier"]) != candidate.source_identifier:
        raise SchemaSnapshotError("schema snapshot checksum collides across sources")
    if str(row["parser_version"]) != candidate.parser_version:
        raise SchemaSnapshotError("schema snapshot checksum collides across parser versions")
    if tuple(row["approved_schemas"] or ()) != candidate.approved_schemas:
        raise SchemaSnapshotError("schema snapshot approved schemas differ")
    if int(row["relation_count"]) != len(candidate.relations):
        raise SchemaSnapshotError("schema snapshot relation count differs")
    if dict(row["payload"] or {}) != payload:
        raise SchemaSnapshotError("schema snapshot payload differs for the same checksum")


def _snapshot_from_row(row: Mapping[str, Any]) -> SchemaSnapshot:
    payload = dict(row["payload"] or {})
    candidate = schema_snapshot_candidate_from_payload(payload, checksum=str(row["checksum"]))
    _validate_persisted_candidate(row, candidate, payload)
    snapshot = SchemaSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        state=SchemaSnapshotState(str(row["state"])),
        candidate=candidate,
        validation_report=dict(row["validation_report"] or {}),
        created_at=row["created_at"],
        validated_at=row["validated_at"],
    )
    report = snapshot.validation_report
    if snapshot.state is SchemaSnapshotState.CANDIDATE:
        if report or snapshot.validated_at is not None:
            raise SchemaSnapshotError("candidate schema snapshot has invalid lifecycle metadata")
        return snapshot
    if (
        report.get("candidate_checksum") != snapshot.checksum
        or report.get("schema_checksum") != candidate.schema_checksum
        or int(report.get("relation_count", -1)) != len(candidate.relations)
    ):
        raise SchemaSnapshotError("persisted schema snapshot validation report is stale")
    if snapshot.state is SchemaSnapshotState.REJECTED:
        if report.get("ok") is not False or snapshot.validated_at is not None:
            raise SchemaSnapshotError("rejected schema snapshot has invalid lifecycle metadata")
    elif report.get("ok") is not True or snapshot.validated_at is None:
        raise SchemaSnapshotError("validated schema snapshot has invalid lifecycle metadata")
    return snapshot


def _candidate_payload(candidate: SchemaSnapshotCandidate) -> dict[str, Any]:
    return {
        "approved_schemas": list(candidate.approved_schemas),
        "parser_version": candidate.parser_version,
        "relations": [_relation_payload(relation) for relation in candidate.relations],
        "schema_checksum": candidate.schema_checksum,
        "source_identifier": candidate.source_identifier,
    }


def _relation_payload(relation: RelationSnapshot) -> dict[str, Any]:
    return {
        "aggregate_coverage": list(relation.aggregate_coverage),
        "columns": [asdict(column) for column in relation.columns],
        "estimated_rows": relation.estimated_rows,
        "foreign_keys": [
            {
                "columns": list(foreign_key.columns),
                "name": foreign_key.name,
                "target_columns": list(foreign_key.target_columns),
                "target_relation_id": foreign_key.target_relation_id,
            }
            for foreign_key in relation.foreign_keys
        ],
        "freshness_sla_seconds": relation.freshness_sla_seconds,
        "indexes": [
            {
                "columns": list(index.columns),
                "name": index.name,
                "predicate": index.predicate,
                "primary": index.primary,
                "unique": index.unique,
            }
            for index in relation.indexes
        ],
        "parent_relation_id": relation.parent_relation_id,
        "partition_key": relation.partition_key,
        "primary_key": list(relation.primary_key),
        "relation_id": relation.relation_id,
        "relation_kind": relation.relation_kind,
        "relation_name": relation.relation_name,
        "schema_name": relation.schema_name,
        "sensitive_columns": list(relation.sensitive_columns),
        "sensitivity": relation.sensitivity,
        "total_bytes": relation.total_bytes,
    }


def _relation_structure_payload(relation: RelationSnapshot) -> dict[str, Any]:
    return {
        "columns": [asdict(column) for column in relation.columns],
        "foreign_keys": [asdict(foreign_key) for foreign_key in relation.foreign_keys],
        "indexes": [asdict(index) for index in relation.indexes],
        "parent_relation_id": relation.parent_relation_id,
        "partition_key": relation.partition_key,
        "primary_key": list(relation.primary_key),
        "relation_id": relation.relation_id,
        "relation_kind": relation.relation_kind,
    }


def _relation_from_payload(payload: Mapping[str, Any]) -> RelationSnapshot:
    return RelationSnapshot(
        relation_id=str(payload["relation_id"]),
        schema_name=str(payload["schema_name"]),
        relation_name=str(payload["relation_name"]),
        relation_kind=str(payload["relation_kind"]),
        columns=tuple(
            ColumnSnapshot(
                name=str(column["name"]),
                data_type=str(column["data_type"]),
                nullable=bool(column["nullable"]),
                ordinal_position=int(column["ordinal_position"]),
            )
            for column in payload.get("columns", ())
        ),
        primary_key=tuple(str(item) for item in payload.get("primary_key", ())),
        foreign_keys=tuple(
            ForeignKeySnapshot(
                name=str(foreign_key["name"]),
                columns=tuple(str(item) for item in foreign_key.get("columns", ())),
                target_relation_id=str(foreign_key["target_relation_id"]),
                target_columns=tuple(
                    str(item) for item in foreign_key.get("target_columns", ())
                ),
            )
            for foreign_key in payload.get("foreign_keys", ())
        ),
        indexes=tuple(
            IndexSnapshot(
                name=str(index["name"]),
                columns=tuple(str(item) for item in index.get("columns", ())),
                unique=bool(index["unique"]),
                primary=bool(index["primary"]),
                predicate=str(index.get("predicate") or "").strip() or None,
            )
            for index in payload.get("indexes", ())
        ),
        partition_key=str(payload.get("partition_key") or "").strip() or None,
        parent_relation_id=(
            str(payload["parent_relation_id"])
            if payload.get("parent_relation_id") is not None
            else None
        ),
        estimated_rows=int(payload.get("estimated_rows") or 0),
        total_bytes=int(payload.get("total_bytes") or 0),
        sensitivity=str(payload.get("sensitivity") or "unclassified"),
        sensitive_columns=tuple(
            str(item) for item in payload.get("sensitive_columns", ())
        ),
        aggregate_coverage=tuple(
            str(item) for item in payload.get("aggregate_coverage", ())
        ),
        freshness_sla_seconds=(
            int(payload["freshness_sla_seconds"])
            if payload.get("freshness_sla_seconds") is not None
            else None
        ),
    )


def _normalize_schemas(values: Iterable[str]) -> tuple[str, ...]:
    schemas = tuple(sorted({value.strip() for value in values if value.strip()}))
    if not schemas:
        raise SchemaSnapshotError("at least one approved schema is required")
    for schema in schemas:
        _validate_identifier(schema, field="schema")
    return schemas


def _normalize_relation_refs(
    values: Iterable[str],
    approved_schemas: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _normalize_relation_ref(value, approved_schemas)
                for value in values
                if value.strip()
            }
        )
    )


def _normalize_relation_ref(value: str, approved_schemas: Sequence[str]) -> str:
    parts = [part.strip() for part in value.split(".")]
    if len(parts) == 1:
        if len(approved_schemas) != 1:
            raise SchemaSnapshotError(
                f"unqualified relation is ambiguous across approved schemas: {value}"
            )
        schema_name, relation_name = approved_schemas[0], parts[0]
    elif len(parts) == 2:
        schema_name, relation_name = parts
    else:
        raise SchemaSnapshotError(f"invalid approved relation: {value}")
    relation_id = _relation_id(schema_name, relation_name)
    if schema_name not in approved_schemas:
        raise SchemaSnapshotError(f"relation uses an unapproved schema: {relation_id}")
    return relation_id


def _relation_id(schema_name: str, relation_name: str) -> str:
    _validate_identifier(schema_name, field="schema")
    _validate_identifier(relation_name, field="relation")
    return f"{schema_name}.{relation_name}"


def _validate_identifier(value: str, *, field: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise SchemaSnapshotError(f"invalid PostgreSQL {field} identifier: {value}")


def _text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _text_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _policy_string_tuple(
    value: Any,
    *,
    relation_id: str,
    field: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SchemaSnapshotError(
            f"schema snapshot policy {field} must be a string array: {relation_id}"
        )
    return tuple(item.strip() for item in value)


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _async_database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    return value


def _split_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect and validate one bounded PostgreSQL schema snapshot"
    )
    parser.add_argument(
        "--source-identifier",
        default=os.getenv("SCHEMA_SNAPSHOT_SOURCE_IDENTIFIER", ""),
        help="non-secret stable identifier for the business database source",
    )
    parser.add_argument("--approved-schema", action="append", default=None)
    parser.add_argument("--approved-relation", action="append", default=None)
    parser.add_argument(
        "--baseline-snapshot-id",
        default=os.getenv("SCHEMA_SNAPSHOT_BASELINE_ID") or None,
    )
    parser.add_argument(
        "--policy-file",
        type=Path,
        default=(
            Path(os.environ["SCHEMA_SNAPSHOT_POLICY_FILE"])
            if os.getenv("SCHEMA_SNAPSHOT_POLICY_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--max-relations",
        type=int,
        default=int(
            os.getenv("SCHEMA_SNAPSHOT_MAX_RELATIONS", str(DEFAULT_MAX_RELATIONS))
        ),
    )
    args = parser.parse_args()

    source_identifier = args.source_identifier.strip()
    if not source_identifier:
        parser.error("--source-identifier or SCHEMA_SNAPSHOT_SOURCE_IDENTIFIER is required")
    approved_schemas = tuple(args.approved_schema or ()) or _split_env(
        "SCHEMA_SNAPSHOT_APPROVED_SCHEMAS"
    )
    if not approved_schemas:
        approved_schemas = (get_agent_config().nl2sql_db_schema,)
    approved_relations = tuple(args.approved_relation or ()) or _split_env(
        "SCHEMA_SNAPSHOT_APPROVED_RELATIONS"
    )
    policies = load_relation_policies(args.policy_file) if args.policy_file else None
    snapshot = asyncio.run(
        run_snapshotter(
            source_identifier=source_identifier,
            approved_schemas=approved_schemas,
            approved_relations=approved_relations,
            policies=policies,
            baseline_snapshot_id=args.baseline_snapshot_id,
            max_relations=args.max_relations,
        )
    )
    print(
        f"schema snapshot {snapshot.snapshot_id} state={snapshot.state.value} "
        f"checksum={snapshot.checksum} relations={len(snapshot.candidate.relations)}"
    )
    if snapshot.state is not SchemaSnapshotState.VALIDATED:
        raise SystemExit(2)


__all__ = [
    "ABSOLUTE_MAX_RELATIONS",
    "DEFAULT_MAX_RELATIONS",
    "SCHEMA_SNAPSHOT_PARSER_VERSION",
    "ColumnSnapshot",
    "ControlSchemaSnapshotStore",
    "ForeignKeySnapshot",
    "IndexSnapshot",
    "PostgresSchemaSnapshotCollector",
    "RelationPolicy",
    "RelationSnapshot",
    "SchemaRequirement",
    "SchemaSnapshot",
    "SchemaSnapshotCandidate",
    "SchemaSnapshotError",
    "SchemaSnapshotIssue",
    "SchemaSnapshotState",
    "SchemaSnapshotValidationReport",
    "SnapshotIssueSeverity",
    "bind_schema_snapshot",
    "build_schema_snapshot_candidate",
    "load_relation_policies",
    "run_snapshotter",
    "schema_snapshot_candidate_from_payload",
    "validate_schema_snapshot",
]


if __name__ == "__main__":
    main()
