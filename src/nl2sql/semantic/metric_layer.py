"""指标语义层：指标匹配与指标结果查询。"""

import re
from typing import Any

from src.nl2sql.infra.store.database import DatabaseManager

from .models import MetricMeta


class MetricSemanticLayer:
    """直接读取数据库指标元信息与结果。"""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def resolve(self, question: str) -> list[MetricMeta]:
        """解析问题，返回候选指标列表（按匹配分数降序）。"""
        normalized_question = _normalize_text(question)
        if not normalized_question:
            return []

        metrics = await self._load_metric_metadata()
        scored: list[tuple[int, MetricMeta]] = []
        for metric in metrics:
            score = _score_metric(metric, question=question, normalized_question=normalized_question)
            if score <= 0:
                continue
            scored.append((score, metric))

        scored.sort(key=lambda item: (-item[0], len(item[1].display_name), item[1].metric_code))
        return [metric for _, metric in scored]

    async def query(
        self,
        *,
        metric_code: str,
        time_grain: str,
        start_date: str,
        end_date: str,
        aggregation: str | None = None,
        target_grain: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询指标结果。"""
        normalized_aggregation = _normalize_aggregation(aggregation)
        normalized_target_grain = _normalize_grain(target_grain) if target_grain else None

        if normalized_aggregation and normalized_target_grain and normalized_target_grain != _normalize_grain(time_grain):
            aggregate_fn = "AVG" if normalized_aggregation == "avg" else "SUM"
            sql = (
                "SELECT "
                "date_trunc(:target_grain, time_value::timestamp)::date AS period, "
                f"{aggregate_fn}(value::numeric) AS value, "
                "MAX(unit) AS unit "
                "FROM v_metric_result "
                "WHERE metric_code = :metric_code "
                "AND time_grain = :time_grain "
                "AND time_value::date BETWEEN CAST(:start_date AS date) AND CAST(:end_date AS date) "
                "AND (dimension_type IS NULL OR dimension_type IN ('overall', 'all', 'total')) "
                "GROUP BY 1 "
                "ORDER BY 1"
            )
            params = {
                "metric_code": metric_code,
                "time_grain": _normalize_grain(time_grain),
                "start_date": start_date,
                "end_date": end_date,
                "target_grain": normalized_target_grain,
            }
            rows = await self._db_manager.execute_query(sql, params)
            return [
                {
                    "period": str(row.get("period", "")),
                    "value": row.get("value"),
                    "unit": row.get("unit"),
                }
                for row in rows
            ]

        sql = (
            "SELECT "
            "time_value::date AS period, "
            "value, "
            "value_type, "
            "unit "
            "FROM v_metric_result "
            "WHERE metric_code = :metric_code "
            "AND time_grain = :time_grain "
            "AND time_value::date BETWEEN CAST(:start_date AS date) AND CAST(:end_date AS date) "
            "AND (dimension_type IS NULL OR dimension_type IN ('overall', 'all', 'total')) "
            "ORDER BY period"
        )
        params = {
            "metric_code": metric_code,
            "time_grain": _normalize_grain(time_grain),
            "start_date": start_date,
            "end_date": end_date,
        }
        rows = await self._db_manager.execute_query(sql, params)

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "period": str(row.get("period", "")),
                    "value": row.get("value"),
                    "value_type": row.get("value_type"),
                    "unit": row.get("unit"),
                }
            )
        return results

    async def _load_metric_metadata(self) -> list[MetricMeta]:
        sql = (
            "SELECT "
            "metric_code, display_name, description, source_type, calculation_type, "
            "time_grain, value_type, unit, tags, rollup_strategy "
            "FROM v_metric_metadata"
        )
        rows = await self._db_manager.execute_query(sql)

        metrics: list[MetricMeta] = []
        for row in rows:
            try:
                metric = MetricMeta.from_row(row)
            except Exception:
                continue
            if not metric.metric_code or not metric.display_name:
                continue
            metrics.append(metric)
        return metrics


def _score_metric(metric: MetricMeta, *, question: str, normalized_question: str) -> int:
    score = 0
    question_lower = question.lower()

    metric_code = metric.metric_code.strip().lower()
    if metric_code and metric_code in question_lower:
        score += 100

    display_name = metric.display_name.strip()
    if display_name and _contains_text(normalized_question, _normalize_text(display_name)):
        score += 80

    for tag in metric.tags:
        normalized_tag = _normalize_text(tag)
        if not normalized_tag:
            continue
        if _contains_text(normalized_question, normalized_tag):
            score += 30

    return score


def _contains_text(haystack: str, needle: str) -> bool:
    return bool(needle) and needle in haystack


def _normalize_text(text: str) -> str:
    lowered = text.lower()
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fa5]", "", lowered)


def _normalize_grain(grain: str | None) -> str:
    if grain is None:
        return "day"

    value = grain.strip().lower()
    mapping = {
        "h": "hour",
        "hour": "hour",
        "hourly": "hour",
        "d": "day",
        "day": "day",
        "daily": "day",
        "w": "week",
        "week": "week",
        "weekly": "week",
        "m": "month",
        "month": "month",
        "monthly": "month",
        "q": "quarter",
        "quarter": "quarter",
        "quarterly": "quarter",
        "y": "year",
        "year": "year",
        "yearly": "year",
    }
    return mapping.get(value, value)


def _normalize_aggregation(aggregation: str | None) -> str | None:
    if aggregation is None:
        return None
    value = aggregation.strip().lower()
    if value in {"sum", "avg"}:
        return value
    return None
