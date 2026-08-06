"""Typed semantic release materialization contracts."""

from dataclasses import replace

import pytest

from src.nl2sql.semantic.authoring import (
    AuthoringIR,
    JoinDefinition,
    MetricAsset,
    QAAsset,
    ViewAsset,
    validate_authoring_ir,
)
from src.nl2sql.semantic.materialization import (
    PARSER_VERSION,
    materialize_authoring_ir,
    normalize_semantic_alias,
)
from src.nl2sql.semantic.registry import SemanticReleaseError


def _metric(
    key: str,
    *,
    aliases: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> MetricAsset:
    derived = bool(dependencies)
    return MetricAsset(
        asset_id=f"metric.{key}",
        metric_key=key,
        display_name="投诉量" if key == "complaint_count" else key,
        source_relation="complaints",
        aliases=aliases,
        kind="derived" if derived else "base",
        dependencies=dependencies,
        formula="{complaint_count} / NULLIF({complaint_count}, 0)" if derived else None,
        calculation_template_id="ratio" if derived else None,
        calculation_template_version="1.0" if derived else None,
        decimal_scale=4 if derived else None,
        rounding="half_up" if derived else None,
        unit="percent" if derived else None,
        null_strategy="return_null" if derived else None,
        zero_strategy="return_null" if derived else None,
        source_columns=("id",),
        domain="complaint",
        owner="complaint-analytics",
        sensitivity="internal",
        freshness_sla_seconds=3600,
        legacy_metadata_inferred=key == "complaint_count",
    )


def _authoring_ir(*, reverse_metrics: bool = False) -> AuthoringIR:
    metrics = (
        _metric("complaint_count", aliases=("投诉数量", "Complaint Count")),
        _metric("complaint_rate", dependencies=("complaint_count",)),
    )
    if reverse_metrics:
        metrics = tuple(reversed(metrics))
    return AuthoringIR(
        metrics=metrics,
        qas=(
            QAAsset(
                asset_id="qa.complaint-count",
                case_id="qa-complaint-count",
                question="How many complaints?",
                sql="SELECT COUNT(c.id) AS count FROM complaints c",
                metric_keys=("complaint_count",),
                domain="complaint",
                owner="complaint-analytics",
                sensitivity="internal",
                freshness_sla_seconds=3600,
            ),
        ),
        views=(
            ViewAsset(
                asset_id="view.complaint_detail",
                name="complaint_detail",
                source_relation="complaints",
                source_alias="c",
                columns=("c.id", "u.name"),
                joins=(
                    JoinDefinition(
                        table="users",
                        alias="u",
                        join_condition="c.user_id = u.id",
                    ),
                ),
                domain="complaint",
                owner="data-platform",
                sensitivity="internal",
                freshness_sla_seconds=3600,
            ),
        ),
    )


def _validated(ir: AuthoringIR):
    return validate_authoring_ir(
        ir,
        relation_columns={
            "complaints": {"id", "user_id"},
            "users": {"id", "name"},
        },
    )


def test_materialization_emits_typed_rows_and_exact_aliases() -> None:
    ir = _authoring_ir()
    report = _validated(ir)

    assert report.ok
    candidate = materialize_authoring_ir(ir, report)
    assets = {asset.asset_id: asset for asset in candidate.assets}
    aliases = {alias.normalized_alias: alias for alias in candidate.aliases}

    assert candidate.parser_version == PARSER_VERSION
    assert candidate.validation_report["checksum"] == ir.checksum
    assert {asset.asset_type for asset in candidate.assets} == {"metric", "qa", "view", "relation"}
    assert len([asset for asset in candidate.assets if asset.asset_type == "relation"]) == 2
    assert aliases["complaint_count"].asset_id == "metric.complaint_count"
    assert aliases["投诉量"].asset_id == "metric.complaint_count"
    assert aliases["投诉量"].language == "zh"
    assert set(assets) == {document.document_id for document in candidate.documents}
    assert {edge.edge_type for edge in candidate.edges} == {
        "approved_join",
        "lineage",
        "metric_dependency",
    }
    assert all(edge.status == "approved" for edge in candidate.edges)
    assert [issue.code for issue in candidate.validation_issues] == ["legacy_metadata_inferred"]


def test_materialized_checksum_is_stable_across_authoring_input_order() -> None:
    first = _authoring_ir()
    second = _authoring_ir(reverse_metrics=True)

    left = materialize_authoring_ir(first, _validated(first))
    right = materialize_authoring_ir(second, _validated(second))

    assert left.checksum == right.checksum
    assert left.assets == right.assets
    assert left.aliases == right.aliases
    assert left.edges == right.edges


def test_materialization_rejects_stale_validation_report() -> None:
    ir = _authoring_ir()
    stale = replace(_validated(ir), checksum="0" * 64)

    with pytest.raises(SemanticReleaseError, match="checksums differ"):
        materialize_authoring_ir(ir, stale)


def test_asset_ids_must_be_unique_across_authoring_types() -> None:
    ir = AuthoringIR(
        metrics=(_metric("complaint_count"),),
        qas=(
            QAAsset(
                asset_id="metric.complaint_count",
                case_id="duplicate-id",
                question="How many complaints?",
                sql="SELECT COUNT(c.id) AS count FROM complaints c",
                domain="complaint",
                owner="complaint-analytics",
                sensitivity="internal",
                freshness_sla_seconds=3600,
            ),
        ),
    )

    with pytest.raises(SemanticReleaseError, match="unique across all asset types"):
        materialize_authoring_ir(ir, _validated(ir))


def test_normalized_alias_must_map_to_one_asset() -> None:
    ir = AuthoringIR(
        metrics=(
            _metric("complaint_count", aliases=("Shared Alias",)),
            _metric("complaint_rate", aliases=(" shared   alias ",)),
        )
    )
    report = _validated(ir)

    with pytest.raises(SemanticReleaseError, match="maps to multiple assets"):
        materialize_authoring_ir(ir, report)


def test_alias_normalization_uses_nfkc_casefold_and_whitespace_collapse() -> None:
    assert normalize_semantic_alias("  Ｃｏｍｐｌａｉｎｔ   COUNT  ") == "complaint count"
