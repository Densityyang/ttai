"""Tests for hypothesis verifier (static logic only)."""

from src.nl2sql.agents.sql_agent.hypothesis_verifier import (
    ColumnProfile,
    HypothesisReport,
    TableProfile,
    extract_candidates_from_rag,
)


def test_extract_candidates_from_rag() -> None:
    tables = ["silver_fault_reporting_order", "dim_area"]
    hints = (
        "使用 silver_fault_reporting_order.area_id 和 "
        "silver_fault_reporting_order.is_first_response_on_time 做统计，"
        "关联 dim_area.area_name"
    )

    result_tables, result_columns = extract_candidates_from_rag(tables, hints)

    assert "silver_fault_reporting_order" in result_tables
    assert "dim_area" in result_tables
    assert "area_id" in result_columns.get("silver_fault_reporting_order", [])
    assert "is_first_response_on_time" in result_columns.get("silver_fault_reporting_order", [])
    assert "area_name" in result_columns.get("dim_area", [])


def test_extract_candidates_empty() -> None:
    tables, columns = extract_candidates_from_rag([], "")
    assert tables == []
    assert columns == {}


def test_hypothesis_report_to_context_str() -> None:
    report = HypothesisReport(
        tables=[
            TableProfile(
                table="orders",
                exists=True,
                row_count=1000,
                columns=[
                    ColumnProfile(
                        table="orders",
                        column="id",
                        exists=True,
                        distinct_values=["1", "2", "3"],
                        data_type="int",
                    ),
                    ColumnProfile(
                        table="orders",
                        column="foo",
                        exists=False,
                    ),
                ],
            ),
            TableProfile(table="nonexistent", exists=False),
        ],
        warnings=["表 nonexistent 不存在"],
        verified=True,
    )

    text = report.to_context_str()
    assert "orders" in text
    assert "1000" in text
    assert "id" in text
    assert "nonexistent" in text
    assert "不存在" in text


def test_empty_report_context_str() -> None:
    report = HypothesisReport(tables=[], verified=False)
    assert report.to_context_str() == ""
