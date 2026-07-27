"""Tests for adaptive RAG router -- complexity detection and routing decisions."""

from src.nl2sql.agents.sql_agent.adaptive_router import (
    RoutingDecision,
    detect_complexity,
    route,
)


def test_simple_query_goes_fast() -> None:
    decision = route("查询总投诉量")
    assert decision.path == "fast"
    assert "Fast" in decision.reason


def test_standard_query() -> None:
    decision = route("查询各区县上月的投诉工单总数")
    assert decision.path in ("standard", "fast")


def test_complex_query_goes_deep() -> None:
    decision = route(
        "对比去年和今年各区县的首响及时率，并关联维度表统计环比变化趋势，"
        "同时计算加权平均值和排名"
    )
    assert decision.path == "deep"
    assert "Deep" in decision.reason


def test_multi_table_hint_goes_deep() -> None:
    decision = route("关联投诉工单表和区县维度表，跨表统计各区县的平均处理时长")
    assert decision.path == "deep"


def test_computation_keywords_detected() -> None:
    signals = detect_complexity("计算各区县首响及时率的同比增长率")
    assert signals.has_computation_keywords is True


def test_comparison_keywords_detected() -> None:
    signals = detect_complexity("对比A区和B区的投诉量差异")
    assert signals.has_comparison is True


def test_temporal_keywords_detected() -> None:
    signals = detect_complexity("查询去年第四季度的投诉数据")
    assert signals.has_temporal_range is True


def test_experience_boost_field() -> None:
    decision = route("查询投诉量", has_experience_match=True)
    assert decision.experience_boost is True


def test_routing_is_deterministic() -> None:
    """Same input should always produce same routing."""
    d1 = route("各区县首响及时率排名")
    d2 = route("各区县首响及时率排名")
    assert d1.path == d2.path
    assert d1.reason == d2.reason


def test_routing_decision_is_frozen() -> None:
    decision = route("查询投诉量")
    assert isinstance(decision, RoutingDecision)
    # frozen dataclass should raise on assignment
    try:
        decision.path = "deep"  # type: ignore[misc]
        raised = False
    except (AttributeError, TypeError):
        raised = True
    assert raised, "RoutingDecision should be immutable"


def test_empty_question() -> None:
    decision = route("")
    # Should not crash, should return some valid path
    assert decision.path in ("fast", "standard", "deep")
