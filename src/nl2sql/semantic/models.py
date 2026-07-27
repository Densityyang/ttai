"""语义层核心数据模型。"""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MetricMeta:
    """指标元数据。"""

    metric_code: str
    display_name: str
    description: str | None = None
    source_type: str | None = None
    calculation_type: str | None = None
    time_grain: str = "day"
    value_type: str | None = None
    unit: str | None = None
    tags: list[str] = field(default_factory=list)
    rollup_strategy: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MetricMeta":
        """从查询结果行构建模型。"""
        tags = _parse_tags(row.get("tags"))
        metric_code = str(row.get("metric_code", "")).strip()
        display_name = str(row.get("display_name", "")).strip()
        return cls(
            metric_code=metric_code,
            display_name=display_name,
            description=_nullable_str(row.get("description")),
            source_type=_nullable_str(row.get("source_type")),
            calculation_type=_nullable_str(row.get("calculation_type")),
            time_grain=_nullable_str(row.get("time_grain")) or "day",
            value_type=_nullable_str(row.get("value_type")),
            unit=_nullable_str(row.get("unit")),
            tags=tags,
            rollup_strategy=_nullable_str(row.get("rollup_strategy")),
        )

def _nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []

    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]

    if isinstance(raw, tuple):
        return [str(item).strip() for item in raw if str(item).strip()]

    text = str(raw).strip()
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    separators = [",", "，", "|", "/", "、"]
    for separator in separators:
        if separator in text:
            return [segment.strip() for segment in text.split(separator) if segment.strip()]

    return [text]
