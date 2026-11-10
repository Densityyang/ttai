from __future__ import annotations

import pytest

from src.nl2sql.infra.governance.semantic_sql_validator import (
    SemanticSQLValidationError,
    SemanticSQLValidator,
)
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.semantic.execution_manifest import (
    ExpectedExecutionManifest,
    OutputFieldAuthority,
    RelationColumnAuthority,
)


def _manifest() -> ExpectedExecutionManifest:
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    return ExpectedExecutionManifest(
        query_plan_sha256="a" * 64,
        execution_plan_sha256="b" * 64,
        semantic_release_id="release",
        schema_snapshot_id="snapshot",
        policy_version="policy-v1",
        metric_contract_sha256=("c" * 64,),
        relation_binding_sha256=("d" * 64,),
        expected_sql_fingerprints=(),
        expected_parameter_binding_fingerprints=(),
        relation_identity=("ai_views.orders",),
        relation_column_authority=(
            RelationColumnAuthority(
                relation_id="ai_views.orders",
                columns=("ts", "id", "region_id", "area_id", "value"),
            ),
        ),
        metric_operation=("count",),
        time_column=("ts",),
        time_bounds=(":start_at", ":end_at", "Asia/Shanghai"),
        expected_predicates=(
            '{"field":"ts","operator":"gte","parameter":"start_at","value":"2026-01-01T00:00:00+08:00"}',
            '{"field":"ts","operator":"lt","parameter":"end_at","value":"2026-01-02T00:00:00+08:00"}',
        ),
        predicate_authority=(
            '{"field":"ts","operator":"gte","parameter":"start_at","role":"business_time","value":"2026-01-01T00:00:00+08:00"}',
            '{"field":"ts","operator":"lt","parameter":"end_at","role":"business_time","value":"2026-01-02T00:00:00+08:00"}',
        ),
        placeholder_identities=("start_at", "end_at"),
        parameter_binding_fingerprint=canonical_json_digest(
            {"names": ("end_at", "start_at"), "values": params}
        ),
        output_aliases=("value",),
        output_logical_types=("integer",),
        cardinality_expectation="single_row",
    )


_PARAMS = {
    "start_at": "2026-01-01T00:00:00+08:00",
    "end_at": "2026-01-02T00:00:00+08:00",
}


def _validate(
    sql: str, manifest: ExpectedExecutionManifest | None = None
) -> None:
    SemanticSQLValidator().validate(sql, _PARAMS, manifest or _manifest())


def test_primary_aggregation_scope_is_accepted() -> None:
    _validate(
        "SELECT COUNT(*) AS value FROM ai_views.orders "
        "WHERE ts >= :start_at AND ts < :end_at"
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "HAVING COUNT(*) > 0",
        "LIMIT 1",
        "LIMIT 5",
        "OFFSET 0",
    ],
)
def test_single_row_aggregate_rejects_shape_clauses(suffix: str) -> None:
    sql = (
        "SELECT COUNT(*) AS value FROM ai_views.orders "
        "WHERE ts >= :start_at AND ts < :end_at "
        f"{suffix}"
    )
    with pytest.raises(SemanticSQLValidationError, match="cardinality_mismatch"):
        _validate(sql)


def test_single_row_aggregate_does_not_need_limit_when_filters_may_match_nothing() -> None:
    # A no-GROUP-BY aggregate returns one row even when its WHERE clause
    # matches no source records; LIMIT is not part of the cardinality proof.
    manifest = _manifest().model_copy(
        update={
            "expected_predicates": (
                '{"field":"ts","operator":"gte","parameter":"start_at","value":"2026-01-01T00:00:00+08:00"}',
                '{"field":"ts","operator":"lt","parameter":"end_at","value":"2026-01-02T00:00:00+08:00"}',
                '{"field":"id","operator":"eq","value":999999}',
            ),
            "predicate_authority": (
                '{"field":"ts","operator":"gte","parameter":"start_at","role":"business_time","value":"2026-01-01T00:00:00+08:00"}',
                '{"field":"ts","operator":"lt","parameter":"end_at","role":"business_time","value":"2026-01-02T00:00:00+08:00"}',
                '{"field":"id","operator":"eq","role":"request_filter","value":999999}',
            ),
        }
    )
    _validate(
        "SELECT COUNT(*) AS value FROM ai_views.orders "
        "WHERE ts >= :start_at AND ts < :end_at AND id = 999999",
        manifest,
    )


def test_bounded_query_rejects_unmanifested_grouping() -> None:
    manifest = _manifest().model_copy(update={"cardinality_expectation": "bounded"})
    with pytest.raises(SemanticSQLValidationError, match="grouping_mismatch"):
        _validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at GROUP BY id",
            manifest,
        )


def test_manifested_trend_grouping_binds_exact_grain_and_time_column() -> None:
    manifest = _manifest().model_copy(
        update={
            "cardinality_expectation": "bounded",
            "output_aliases": ("period", "value"),
            "output_logical_types": ("date", "integer"),
            "grain": ("month",),
                "output_field_authority": (
                    OutputFieldAuthority(
                        alias="period",
                        relation_id="ai_views.orders",
                        field="ts",
                        kind="time_bucket",
                        logical_type="date",
                        grain="month",
                        timezone="Asia/Shanghai",
                        timestamp_kind="timestamp",
                    ),
                ),
        }
    )
    _validate(
        "SELECT DATE_TRUNC('month', ts) AS period, COUNT(*) AS value "
        "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at "
        "GROUP BY DATE_TRUNC('month', ts)",
        manifest,
    )
    for group in (
        "DATE_TRUNC('month', ts), id",
        "DATE_TRUNC('day', ts)",
    ):
        with pytest.raises(SemanticSQLValidationError, match="grouping_mismatch"):
            _validate(
                "SELECT DATE_TRUNC('month', ts) AS period, COUNT(*) AS value "
                "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at "
                f"GROUP BY {group}",
                manifest,
            )


def test_manifested_text_dimension_accepts_compiler_collation_only() -> None:
    manifest = _manifest().model_copy(
        update={
            "cardinality_expectation": "bounded",
            "grouping_dimensions": ("region_id",),
            "output_aliases": ("dimension_id", "value"),
            "output_logical_types": ("text", "integer"),
                "output_field_authority": (
                    OutputFieldAuthority(
                        alias="dimension_id",
                        relation_id="ai_views.orders",
                        field="region_id",
                        kind="dimension",
                        logical_type="text",
                        collation="C",
                    ),
                ),
        }
    )
    _validate(
        'SELECT region_id COLLATE "C" AS dimension_id, COUNT(*) AS value '
        "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at "
        'GROUP BY region_id COLLATE "C"',
        manifest,
    )
    with pytest.raises(SemanticSQLValidationError, match="grouping_mismatch"):
        _validate(
            'SELECT region_id COLLATE "POSIX" AS dimension_id, COUNT(*) AS value '
            "FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at "
            'GROUP BY region_id COLLATE "POSIX"',
            manifest,
        )


@pytest.mark.parametrize(
    "sql",
    [
        (
            "WITH selected AS (SELECT id, ts FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at) "
            "SELECT COUNT(*) AS value FROM selected"
        ),
        (
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at "
            "AND EXISTS (SELECT 1 FROM ai_views.orders nested WHERE nested.id = orders.id)"
        ),
        (
            "SELECT COUNT(*) AS value FROM (SELECT * FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at) nested"
        ),
        (
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at "
            "UNION ALL SELECT COUNT(*) AS value FROM ai_views.orders"
        ),
        (
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at "
            "AND id IN (SELECT id FROM ai_views.orders)"
        ),
        (
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at "
            "INTERSECT SELECT COUNT(*) AS value FROM ai_views.orders"
        ),
        (
            "SELECT COUNT(*) AS value FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at "
            "EXCEPT SELECT COUNT(*) AS value FROM ai_views.orders"
        ),
    ],
)
def test_nested_or_set_scopes_cannot_authorize_primary_predicates(sql: str) -> None:
    with pytest.raises(SemanticSQLValidationError, match="scope"):
        _validate(sql)


@pytest.mark.parametrize(
    "manifest",
    [
        _manifest(),
        _manifest().model_copy(
            update={"join_identities": ("forged:orders->orders",)}
        ),
    ],
)
def test_join_predicates_cannot_supply_unproven_join_authority(
    manifest: ExpectedExecutionManifest,
) -> None:
    sql = (
        "SELECT COUNT(*) AS value FROM ai_views.orders AS base "
        "JOIN ai_views.orders AS related ON base.id = related.id "
        "WHERE base.ts >= :start_at AND base.ts < :end_at "
        "AND related.region_id = 'authorized-looking'"
    )
    with pytest.raises(SemanticSQLValidationError, match="join_cardinality_mismatch"):
        _validate(sql, manifest)
