from __future__ import annotations

import pytest

from src.nl2sql.infra.governance.query_gateway import PolicyEngine
from src.nl2sql.infra.governance.semantic_sql_validator import (
    SemanticSQLValidationError,
    SemanticSQLValidator,
)
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.semantic.execution_manifest import ExpectedExecutionManifest


def _manifest() -> ExpectedExecutionManifest:
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
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
        metric_operation=("count",),
        time_column=("ts",),
        time_bounds=(":start_at", ":end_at", "Asia/Shanghai"),
        placeholder_identities=("start_at", "end_at"),
        parameter_binding_fingerprint=canonical_json_digest(
            {"names": ("end_at", "start_at"), "values": params}
        ),
        output_aliases=("value",),
        cardinality_expectation="single_row",
    )


def test_semantic_validator_binds_relation_and_columns() -> None:
    validator = SemanticSQLValidator(policy=PolicyEngine(allowed_schema="ai_views"))
    result = validator.validate(
        "SELECT id FROM orders",
        expected_relation_ids=("ai_views.orders",),
        required_columns=("id",),
    )
    assert result.relations == ("ai_views.orders",)
    assert result.columns == ("id",)


def test_semantic_validator_rejects_unbound_relation_and_tablesample() -> None:
    validator = SemanticSQLValidator(policy=PolicyEngine(allowed_schema="ai_views"))
    with pytest.raises(SemanticSQLValidationError, match="relation_unbound"):
        validator.validate(
            "SELECT id FROM orders",
            expected_relation_ids=("ai_views.customers",),
        )
    with pytest.raises(SemanticSQLValidationError, match="security:policy_denied"):
        validator.validate(
            "SELECT id FROM orders TABLESAMPLE SYSTEM (10)",
            expected_relation_ids=("ai_views.orders",),
        )


def test_manifest_mode_excludes_cte_alias_and_requires_half_open_time() -> None:
    validator = SemanticSQLValidator()
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    valid = validator.validate(
        "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
        params,
        _manifest(),
    )
    assert valid.outcome == "allow"
    with pytest.raises(SemanticSQLValidationError, match="time_predicate_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts <= :end_at",
            params,
            _manifest(),
        )
    cte = validator.validate(
        "WITH selected AS (SELECT id FROM ai_views.orders) SELECT COUNT(*) AS value FROM selected WHERE ts >= :start_at AND ts < :end_at",
        params,
        _manifest(),
    )
    assert cte.relations == ("ai_views.orders",)


def test_manifest_mode_rejects_extra_predicates_and_parameter_substitution() -> None:
    validator = SemanticSQLValidator()
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    with pytest.raises(SemanticSQLValidationError, match="authorization_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at AND 1 = 1",
            params,
            _manifest(),
        )
    with pytest.raises(SemanticSQLValidationError, match="parameter_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :from_at AND ts < :end_at",
            {"from_at": params["start_at"], "end_at": params["end_at"]},
            _manifest(),
        )


def test_manifest_mode_rejects_literal_count_and_wrong_time_value() -> None:
    validator = SemanticSQLValidator()
    params = {
        "start_at": "2026-01-01T00:00:00+08:00",
        "end_at": "2026-01-02T00:00:00+08:00",
    }
    with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
        validator.validate(
            "SELECT 42 AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
            params,
            _manifest(),
        )
    with pytest.raises(SemanticSQLValidationError, match="parameter_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
            {**params, "end_at": "2026-01-03T00:00:00+08:00"},
            _manifest(),
        )


def test_manifest_mode_rejects_missing_authorization_and_wrong_ratio_denominator() -> None:
    validator = SemanticSQLValidator()
    params = {"start_at": "2026-01-01T00:00:00+08:00", "end_at": "2026-01-02T00:00:00+08:00"}
    auth_manifest = _manifest().model_copy(
        update={
            "authorization_predicate_fingerprint": "e" * 64,
            "authorization_predicate_fields": ("area_id",),
        }
    )
    with pytest.raises(SemanticSQLValidationError, match="authorization_mismatch"):
        validator.validate(
            "SELECT COUNT(*) AS value FROM ai_views.orders WHERE ts >= :start_at AND ts < :end_at",
            params,
            auth_manifest,
        )
    ratio_manifest = _manifest().model_copy(
        update={
            "metric_operation": ("ratio",),
            "ratio_numerator_roles": ("is_first_response_on_time",),
            "ratio_denominator_roles": ("has_valid_bandwidth",),
            "output_aliases": ("numerator", "denominator", "value", "status"),
        }
    )
    with pytest.raises(SemanticSQLValidationError, match="aggregate_mismatch"):
        validator.validate(
            "SELECT COUNT(*) FILTER (WHERE wrong_denominator IS TRUE) AS numerator, "
            "COUNT(*) FILTER (WHERE wrong_denominator IS TRUE) AS denominator, "
            "CASE WHEN denominator = 0 THEN NULL ELSE ROUND(100 * numerator / denominator, 2) END AS value, "
            "'success' AS status FROM ai_views.orders "
            "WHERE ts >= :start_at AND ts < :end_at",
            params,
            ratio_manifest,
        )
