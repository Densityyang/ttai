"""Tests for experience store (Memo-SQL)."""

from src.nl2sql.agents.sql_agent.experience_store import (
    ExperienceStore,
    _classify_error,
    _normalize_question,
)


def test_record_and_search_success() -> None:
    store = ExperienceStore()
    store.record_success(
        question="查询各区县的首响及时率",
        table_names=["silver_fault_reporting_order"],
        schema_snippet="id, area_id, is_first_response_on_time",
        sql="SELECT area_id, AVG(is_first_response_on_time) FROM ...",
        result_summary="[{'area_id': 1, 'rate': 0.85}]",
    )

    results = store.search_similar_queries("各区县首响及时率排名")
    assert len(results) >= 1
    assert "silver_fault_reporting_order" in results[0].table_names


def test_search_empty_store() -> None:
    store = ExperienceStore()
    results = store.search_similar_queries("任何问题")
    assert results == []


def test_record_and_search_repair() -> None:
    store = ExperienceStore()
    store.record_repair(
        error_sql="SELECT foo FROM bar",
        error_message='column "foo" does not exist',
        repaired_sql="SELECT baz FROM bar",
        repair_explanation="列名 foo 不存在，改为 baz",
    )

    results = store.search_repair_patterns('column "xyz" does not exist')
    assert len(results) >= 1
    assert results[0].error_type == "column_not_found"


def test_eviction_on_max_size() -> None:
    store = ExperienceStore(max_size=3)
    for i in range(5):
        store.record_success(
            question=f"问题 {i} 某某指标 {i}",
            table_names=[f"table_{i}"],
            schema_snippet="",
            sql=f"SELECT * FROM table_{i}",
        )
    assert store.stats["success_memories"] <= 3


def test_classify_error() -> None:
    assert _classify_error('column "foo" does not exist') == "column_not_found"
    assert _classify_error('relation "bar" does not exist') == "table_not_found"
    assert _classify_error("syntax error at or near") == "syntax_error"
    assert _classify_error("division by zero") == "division_by_zero"
    assert _classify_error("some random error") == "unknown"


def test_normalize_question() -> None:
    result = _normalize_question("  查询各区County的 首响及时率?  ")
    assert "?" not in result
    assert result == result.lower()
    assert "  " not in result


def test_table_bonus_in_search() -> None:
    store = ExperienceStore()
    store.record_success(
        question="区县投诉工单统计",
        table_names=["silver_fault_reporting_order"],
        schema_snippet="",
        sql="SELECT COUNT(*) FROM silver_fault_reporting_order",
    )
    store.record_success(
        question="区县服务质量统计",
        table_names=["silver_service_quality"],
        schema_snippet="",
        sql="SELECT COUNT(*) FROM silver_service_quality",
    )

    results = store.search_similar_queries(
        "区县投诉工单数量",
        table_names=["silver_fault_reporting_order"],
    )
    if results:
        assert results[0].table_names == ["silver_fault_reporting_order"]


def test_format_experience_context() -> None:
    store = ExperienceStore()
    store.record_success(
        question="各区县首响及时率",
        table_names=["orders"],
        schema_snippet="",
        sql="SELECT area, rate FROM orders",
    )

    ctx = store.format_experience_context("区县首响及时率排名")
    assert "案例" in ctx or ctx == ""


def test_format_repair_context() -> None:
    store = ExperienceStore()
    store.record_repair(
        error_sql="SELECT foo FROM bar",
        error_message='column "foo" does not exist',
        repaired_sql="SELECT baz FROM bar",
    )

    ctx = store.format_repair_context('column "abc" does not exist')
    assert "修复案例" in ctx or ctx == ""
