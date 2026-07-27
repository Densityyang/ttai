"""自适应 RAG 路由器 -- 基于查询复杂度自动选择检索策略。

三路策略：
- Fast Path:     简单单实体查询 → 向量 RAG 单次检索（延迟优先）
- Standard Path: 标准语义查询 → Self-RAG 评分+重写（当前流程）
- Deep Path:     多实体/多表/跨域 → GraphRAG 多跳 + Self-RAG + 经验匹配

并发安全说明：
- 本模块所有函数均为纯函数或只读访问配置，无共享可变状态
- 复杂度检测基于正则匹配 + 词计数，无 IO 操作，天然并发安全
- 每次调用返回独立的 RoutingDecision 实例，不跨请求共享
"""

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

# 路由策略类型
RoutePath = Literal["fast", "standard", "deep"]


@dataclass(frozen=True)
class ComplexitySignals:
    """查询复杂度信号（只读，并发安全）。"""

    entity_count: int
    has_comparison: bool
    has_temporal_range: bool
    has_computation_keywords: bool
    has_multi_table_hint: bool
    predicate_count: int
    question_length: int


@dataclass(frozen=True)
class RoutingDecision:
    """路由决策结果（不可变，并发安全）。"""

    path: RoutePath
    signals: ComplexitySignals
    reason: str
    experience_boost: bool = False


# ── 复杂度关键词（TUNABLE: 可能需要根据实际业务语料调整关键词列表） ──

_COMPUTATION_KEYWORDS: set[str] = {
    "比率", "比例", "同比", "环比", "加权", "排名", "自定义",
    "增长率", "下降率", "百分比", "均值", "方差", "中位数",
    "趋势", "拟合", "相关性", "占比", "累计", "滚动",
}

_COMPARISON_KEYWORDS: set[str] = {
    "对比", "比较", "vs", "差异", "高于", "低于", "超过",
    "不如", "优于", "劣于", "相比", "与...相比",
}

_TEMPORAL_KEYWORDS: set[str] = {
    "去年", "今年", "上月", "本月", "上周", "本周",
    "最近", "近", "年", "季度", "月份", "日",
    "同期", "历史", "截至", "从...到",
}

_MULTI_TABLE_KEYWORDS: set[str] = {
    "关联", "join", "连接", "合并", "跨表", "多表",
    "关系", "维度", "主表", "子表", "外键",
}

# ── TUNABLE: 路由阈值（可能需要根据测试结果调整） ──

# Fast Path 上限: entity_count <= 此值 且无复杂信号
_FAST_MAX_ENTITIES: int = 1  # TUNABLE: 简单查询最大实体数

# Deep Path 下限: 满足以下任一条件即进入 Deep
_DEEP_MIN_ENTITIES: int = 4  # TUNABLE: 多实体阈值
_DEEP_MIN_PREDICATES: int = 3  # TUNABLE: 多谓词阈值
_DEEP_QUESTION_LENGTH: int = 80  # TUNABLE: 长问题阈值（字符）


def detect_complexity(question: str) -> ComplexitySignals:
    """分析查询的复杂度信号。

    纯函数，无副作用，并发安全。

    Args:
        question: 用户原始问题

    Returns:
        ComplexitySignals 不可变结构
    """
    q_lower = question.lower()
    entity_count = _count_entities(q_lower)
    has_comparison = any(keyword in q_lower for keyword in _COMPARISON_KEYWORDS)
    has_temporal = any(keyword in q_lower for keyword in _TEMPORAL_KEYWORDS)
    has_computation = any(keyword in q_lower for keyword in _COMPUTATION_KEYWORDS)
    has_multi_table = any(keyword in q_lower for keyword in _MULTI_TABLE_KEYWORDS)
    predicate_count = _count_predicates(q_lower)

    return ComplexitySignals(
        entity_count=entity_count,
        has_comparison=has_comparison,
        has_temporal_range=has_temporal,
        has_computation_keywords=has_computation,
        has_multi_table_hint=has_multi_table,
        predicate_count=predicate_count,
        question_length=len(question),
    )


def route(question: str, has_experience_match: bool = False) -> RoutingDecision:
    """根据查询复杂度决定 RAG 路由策略。

    纯函数，并发安全。不持有任何可变引用。

    Args:
        question: 用户原始问题
        has_experience_match: 经验记忆库中是否有匹配项（如果有，可提升路径）

    Returns:
        RoutingDecision 不可变决策
    """
    signals = detect_complexity(question)

    # ── Deep Path 条件判断 ──
    deep_reasons: list[str] = []

    if signals.entity_count >= _DEEP_MIN_ENTITIES:
        deep_reasons.append(f"实体数 {signals.entity_count} >= {_DEEP_MIN_ENTITIES}")
    if signals.has_multi_table_hint:
        deep_reasons.append("检测到多表关联信号")
    if signals.predicate_count >= _DEEP_MIN_PREDICATES:
        deep_reasons.append(f"谓词数 {signals.predicate_count} >= {_DEEP_MIN_PREDICATES}")
    if signals.has_comparison and signals.has_temporal_range:
        deep_reasons.append("同时包含对比和时间范围")
    if signals.question_length >= _DEEP_QUESTION_LENGTH and signals.has_computation_keywords:
        deep_reasons.append("长问题 + 计算关键词")

    if deep_reasons:
        return RoutingDecision(
            path="deep",
            signals=signals,
            reason="Deep Path: " + "; ".join(deep_reasons),
            experience_boost=has_experience_match,
        )

    # ── Fast Path 条件判断 ──
    is_simple = (
        signals.entity_count <= _FAST_MAX_ENTITIES
        and not signals.has_comparison
        and not signals.has_computation_keywords
        and not signals.has_multi_table_hint
        and signals.predicate_count <= 1
        and signals.question_length < 40  # TUNABLE: 短问题阈值
    )

    if is_simple:
        return RoutingDecision(
            path="fast",
            signals=signals,
            reason="Fast Path: 简单单实体查询",
            experience_boost=has_experience_match,
        )

    # ── Standard Path（默认） ──
    return RoutingDecision(
        path="standard",
        signals=signals,
        reason="Standard Path: 标准语义查询",
        experience_boost=has_experience_match,
    )


def _count_entities(text: str) -> int:
    """粗略计算问题中的实体数量。

    TUNABLE: 实体计数策略可能需要根据实际业务词库优化。
    当前基于中文名词短语和英文单词的简单计数。
    """
    cn_nouns = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    en_words = re.findall(r"[a-zA-Z_]{3,}", text)

    stop_words = {"什么", "哪些", "多少", "怎么", "如何", "为什么", "请问", "帮我", "查询", "统计", "分析"}
    cn_entities = [w for w in cn_nouns if w not in stop_words]

    return min(len(cn_entities) + len(en_words), 10)


def _count_predicates(text: str) -> int:
    """粗略计算问题中的筛选条件/谓词数量。"""
    patterns = [
        r"等于|大于|小于|不等于|>=|<=|>|<|=",
        r"包含|不包含|属于|不属于",
        r"之间|范围|区间",
        r"并且|而且|同时|以及|且",
        r"或者|或",
    ]
    count = 0
    for p in patterns:
        count += len(re.findall(p, text))
    return count
