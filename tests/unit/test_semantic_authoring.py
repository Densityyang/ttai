"""Schema-v3 semantic authoring compiler and validator tests."""

from pathlib import Path

from src.nl2sql.semantic.authoring import (
    AssetStatus,
    AuthoringIR,
    JoinDefinition,
    MetricAsset,
    QAAsset,
    ViewAsset,
    compile_authoring_files,
    parse_semantic_metrics,
    validate_authoring_ir,
)

ROOT = Path(__file__).resolve().parents[2]


def _view(*, name: str = "v_orders", relation: str = "orders") -> ViewAsset:
    return ViewAsset(
        asset_id=f"view.{name}",
        name=name,
        source_relation=relation,
        source_alias="o",
        columns=("o.id", "o.created_at"),
        owner="data-platform",
        sensitivity="internal",
        freshness_sla_seconds=3600,
    )


def _metric(
    key: str,
    *,
    aliases: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    kind: str = "base",
) -> MetricAsset:
    return MetricAsset(
        asset_id=f"metric.{key}",
        metric_key=key,
        display_name=key,
        source_relation="orders",
        aliases=aliases,
        dependencies=dependencies,
        kind=kind,
        formula="{numerator} / NULLIF({denominator}, 0)" if kind == "derived" else None,
        calculation_template_id="ratio" if kind == "derived" else None,
        calculation_template_version="1.0" if kind == "derived" else None,
        decimal_scale=4 if kind == "derived" else None,
        rounding="half_up" if kind == "derived" else None,
        unit="percent" if kind == "derived" else None,
        null_strategy="return_null" if kind == "derived" else None,
        zero_strategy="return_null" if kind == "derived" else None,
        owner="business",
        sensitivity="internal",
        freshness_sla_seconds=3600,
    )


def test_current_sources_compile_to_one_report_and_retire_fictional_qas() -> None:
    result = compile_authoring_files(
        ROOT / "configs/semantic/semantic.md",
        ROOT / "configs/semantic/qa.md",
        ROOT / "configs/semantic/ai_views.yaml",
    )

    assert result.report.schema_version == 3
    assert result.report.metric_heading_count == 43
    assert result.report.qa_count == 21
    assert result.report.view_count == 9
    assert len(result.ir.metrics) > result.report.metric_heading_count
    assert [qa.status for qa in result.ir.qas[:5]] == [AssetStatus.RETIRED] * 5
    assert result.report.default_release_domain == "complaint"
    assert result.report.default_release_ready is False
    assert result.report.release_candidates == ()
    assert set(result.report.asset_statuses) == {asset.asset_id for asset in result.ir.assets}
    assert not any(issue.code == "qa_preflight_failed" for issue in result.report.issues)


def test_metric_variants_expand_into_independent_assets() -> None:
    metrics = parse_semantic_metrics(
        """## 业务域：订单（orders）
**源表**：`orders`

### 订单数
- **metric key**：`orders_count_day/month`
"""
    )

    assert [metric.metric_key for metric in metrics] == [
        "orders_count_day",
        "orders_count_month",
    ]
    assert len({metric.asset_id for metric in metrics}) == 2


def test_duplicate_metric_key_and_alias_are_reported() -> None:
    key_report = validate_authoring_ir(
        AuthoringIR(
            metrics=(
                _metric("orders_count_day", aliases=("工单数",)),
                _metric("orders_count_day", aliases=("工单数",)),
            ),
            views=(_view(),),
        )
    )
    alias_report = validate_authoring_ir(
        AuthoringIR(
            metrics=(
                _metric("orders_count_day", aliases=("shared alias",)),
                _metric("orders_rate_day", aliases=("shared alias",)),
            ),
            views=(_view(),),
        )
    )

    assert "duplicate_metric_key" in {issue.code for issue in key_report.issues}
    assert "duplicate_metric_alias" in {issue.code for issue in alias_report.issues}


def test_unknown_relation_and_column_are_fail_closed() -> None:
    metric = MetricAsset(
        asset_id="metric.orders_count_day",
        metric_key="orders_count_day",
        display_name="orders_count_day",
        source_relation="missing_relation",
        source_columns=("missing_column",),
        owner="business",
        sensitivity="internal",
        freshness_sla_seconds=3600,
    )
    report = validate_authoring_ir(
        AuthoringIR(metrics=(metric,), views=(_view(),)),
        relation_columns={"orders": {"id"}},
    )

    codes = {issue.code for issue in report.issues}
    assert "unknown_relation" in codes

    known_relation_report = validate_authoring_ir(
        AuthoringIR(
            metrics=(
                MetricAsset(
                    asset_id="metric.orders_count_day",
                    metric_key="orders_count_day",
                    display_name="orders_count_day",
                    source_relation="orders",
                    source_columns=("missing_column",),
                    owner="business",
                    sensitivity="internal",
                    freshness_sla_seconds=3600,
                ),
            ),
            views=(_view(),),
        ),
        relation_columns={"orders": {"id"}},
    )
    assert any(issue.code == "unknown_column" for issue in known_relation_report.issues)


def test_formula_dependency_cycle_is_reported() -> None:
    report = validate_authoring_ir(
        AuthoringIR(
            metrics=(
                _metric("orders_a", dependencies=("orders_b",), kind="base"),
                _metric("orders_b", dependencies=("orders_a",), kind="base"),
            ),
            views=(_view(),),
        )
    )

    assert any(issue.code == "formula_dependency_cycle" for issue in report.issues)


def test_illegal_join_is_reported() -> None:
    view = ViewAsset(
        asset_id="view.v_orders",
        name="v_orders",
        source_relation="orders",
        source_alias="o",
        columns=("o.id",),
        joins=(JoinDefinition(table="order_items", alias="item", join_type="cross"),),
        owner="data-platform",
        sensitivity="internal",
        freshness_sla_seconds=3600,
    )

    report = validate_authoring_ir(AuthoringIR(views=(view,)))

    codes = {issue.code for issue in report.issues}
    assert "illegal_join" in codes
    assert "unknown_relation" in codes


def test_missing_owner_sensitivity_and_freshness_are_reported() -> None:
    report = validate_authoring_ir(
        AuthoringIR(
            metrics=(
                MetricAsset(
                    asset_id="metric.orders_count_day",
                    metric_key="orders_count_day",
                    display_name="orders_count_day",
                    source_relation="orders",
                ),
            ),
            views=(_view(),),
        )
    )

    codes = {issue.code for issue in report.issues}
    assert {"missing_owner", "missing_sensitivity", "missing_freshness"} <= codes


def test_active_qa_passes_sqlglot_and_query_gateway_preflight() -> None:
    qa = QAAsset(
        asset_id="qa.orders-count",
        case_id="qa-orders-count",
        question="How many orders?",
        sql="SELECT COUNT(o.id) AS count FROM orders o",
        domain="orders",
        owner="business",
        sensitivity="internal",
        freshness_sla_seconds=3600,
    )
    report = validate_authoring_ir(AuthoringIR(qas=(qa,), views=(_view(),)))

    assert report.ok
    assert report.issues == ()


def test_ir_checksum_is_stable_across_asset_input_order() -> None:
    first = _metric("orders_a")
    second = _metric("orders_b")
    view = _view()
    left = AuthoringIR(metrics=(first, second), views=(view,))
    right = AuthoringIR(metrics=(second, first), views=(view,))

    assert left.checksum == right.checksum
    assert left.checksum == AuthoringIR(metrics=(first, second), views=(view,)).checksum
