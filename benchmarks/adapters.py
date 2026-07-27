"""公共 NL2SQL 基准数据集适配器 -- Phase 5。

将 BIRD / Spider 等公共数据集转换为统一的 BenchmarkCase 格式，
以便复用同一套评测 runner 和指标计算。

适配的数据集：
- BIRD (dev set): https://bird-bench.github.io/
- Spider 1.0 (dev set): https://yale-lily.github.io/spider
- 企业自建集: benchmarks/datasets/enterprise/
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkCase:
    """统一评测样本格式。"""

    case_id: str
    source: str  # bird / spider / enterprise / synthetic / adversarial
    layer: str  # L1 / L2 / L3 / L4
    domain: str  # 业务域 / 数据库名
    question: str
    gold_sql: str = ""
    gold_value: Any = None
    expected_mode: str = "sql_only"  # sql_only / sql_plus_code / reject
    difficulty: str = "medium"  # simple / medium / challenging / extra
    db_id: str = ""
    tolerance: float = 0.0
    tags: list[str] = field(default_factory=list)
    is_adversarial: bool = False
    should_reject: bool = False


def load_bird_cases(
    bird_dir: str | Path,
    max_cases: int | None = None,
) -> list[BenchmarkCase]:
    """加载 BIRD dev set 并转为 BenchmarkCase。

    BIRD dev.json 格式:
    [
        {
            "question_id": 0,
            "db_id": "california_schools",
            "question": "...",
            "SQL": "SELECT ...",
            "difficulty": "simple" / "moderate" / "challenging"
        }
    ]
    """
    bird_path = Path(bird_dir)
    dev_json = bird_path / "dev.json"

    if not dev_json.exists():
        logger.warning("BIRD dev.json 不存在: %s", dev_json)
        return []

    data = json.loads(dev_json.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        logger.warning("BIRD dev.json 格式异常")
        return []

    cases: list[BenchmarkCase] = []
    for item in data:
        if max_cases and len(cases) >= max_cases:
            break

        difficulty = str(item.get("difficulty", "moderate")).lower()
        layer = _bird_difficulty_to_layer(difficulty)

        evidence = str(item.get("evidence", "")).strip()
        tags = [f"bird_{difficulty}"]
        if evidence:
            tags.append("has_evidence")

        cases.append(BenchmarkCase(
            case_id=f"bird-{item.get('question_id', len(cases))}",
            source="bird",
            layer=layer,
            domain=str(item.get("db_id", "unknown")),
            question=str(item.get("question", "")),
            gold_sql=str(item.get("SQL", "")),
            expected_mode="sql_only",
            difficulty=difficulty,
            db_id=str(item.get("db_id", "")),
            tags=tags,
        ))

    logger.info("加载 BIRD dev set: %d cases", len(cases))
    return cases


def load_spider_cases(
    spider_dir: str | Path,
    max_cases: int | None = None,
) -> list[BenchmarkCase]:
    """加载 Spider dev set 并转为 BenchmarkCase。

    Spider dev.json 格式:
    [
        {
            "db_id": "concert_singer",
            "query": "SELECT ...",
            "query_toks": [...],
            "question": "How many singers...",
            "question_toks": [...]
        }
    ]
    """
    spider_path = Path(spider_dir)
    # 尝试不同文件名
    for name in ["dev.json", "spider_dev.json"]:
        dev_json = spider_path / name
        if dev_json.exists():
            break
    else:
        logger.warning("Spider dev.json 不存在: %s", spider_path)
        return []

    data = json.loads(dev_json.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        logger.warning("Spider dev.json 格式异常")
        return []

    cases: list[BenchmarkCase] = []
    for i, item in enumerate(data):
        if max_cases and len(cases) >= max_cases:
            break

        sql = str(item.get("query", ""))
        difficulty = _spider_sql_to_difficulty(sql)
        layer = _spider_difficulty_to_layer(difficulty)

        cases.append(BenchmarkCase(
            case_id=f"spider-{i}",
            source="spider",
            layer=layer,
            domain=str(item.get("db_id", "unknown")),
            question=str(item.get("question", "")),
            gold_sql=sql,
            expected_mode="sql_only",
            difficulty=difficulty,
            db_id=str(item.get("db_id", "")),
            tags=[f"spider_{difficulty}"],
        ))

    logger.info("加载 Spider dev set: %d cases", len(cases))
    return cases


def load_enterprise_cases(
    enterprise_dir: str | Path,
) -> list[BenchmarkCase]:
    """加载企业自建评测集。

    文件格式: JSONL，每行一个 case:
    {
        "case_id": "...",
        "layer": "L1",
        "domain": "complaint",
        "question": "...",
        "gold_sql": "...",
        "gold_value": ...,
        "expected_mode": "sql_only",
        "difficulty": "medium",
        "tolerance": 0.01,
        "tags": [...],
        "is_adversarial": false,
        "should_reject": false
    }
    """
    enterprise_path = Path(enterprise_dir)
    cases: list[BenchmarkCase] = []

    for jsonl_file in sorted(enterprise_path.glob("*.jsonl")):
        for line in jsonl_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                cases.append(BenchmarkCase(
                    case_id=str(item.get("case_id", f"enterprise-{len(cases)}")),
                    source="enterprise",
                    layer=str(item.get("layer", "L1")),
                    domain=str(item.get("domain", "general")),
                    question=str(item.get("question", "")),
                    gold_sql=str(item.get("gold_sql", "")),
                    gold_value=item.get("gold_value"),
                    expected_mode=str(item.get("expected_mode", "sql_only")),
                    difficulty=str(item.get("difficulty", "medium")),
                    tolerance=float(item.get("tolerance", 0)),
                    tags=item.get("tags", []),
                    is_adversarial=bool(item.get("is_adversarial", False)),
                    should_reject=bool(item.get("should_reject", False)),
                ))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("解析企业 case 失败 (%s): %s", jsonl_file.name, e)

    # 也加载 JSON 格式
    for json_file in sorted(enterprise_path.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            items = data.get("items", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for item in items:
                    cases.append(BenchmarkCase(
                        case_id=str(item.get("case_id", f"enterprise-{len(cases)}")),
                        source="enterprise",
                        layer=str(item.get("layer", "L1")),
                        domain=str(item.get("domain", "general")),
                        question=str(item.get("question", "")),
                        gold_sql=str(item.get("gold_sql", "")),
                        gold_value=item.get("gold_value"),
                        expected_mode=str(item.get("expected_mode", "sql_only")),
                        difficulty=str(item.get("difficulty", "medium")),
                        tolerance=float(item.get("tolerance", 0)),
                        tags=item.get("tags", []),
                        is_adversarial=bool(item.get("is_adversarial", False)),
                        should_reject=bool(item.get("should_reject", False)),
                    ))
        except Exception as e:
            logger.warning("加载企业 JSON 失败 (%s): %s", json_file.name, e)

    logger.info("加载企业评测集: %d cases", len(cases))
    return cases


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _bird_difficulty_to_layer(difficulty: str) -> str:
    return {"simple": "L1", "moderate": "L2", "challenging": "L2"}.get(difficulty, "L2")


def _spider_difficulty_to_layer(difficulty: str) -> str:
    return {"simple": "L1", "medium": "L1", "hard": "L2", "extra": "L2"}.get(difficulty, "L1")


def _spider_sql_to_difficulty(sql: str) -> str:
    """根据 SQL 复杂度粗略估计难度。"""
    sql_upper = sql.upper()
    complexity = 0
    if "JOIN" in sql_upper:
        complexity += 1
    if "GROUP BY" in sql_upper:
        complexity += 1
    if "HAVING" in sql_upper:
        complexity += 1
    if "INTERSECT" in sql_upper or "UNION" in sql_upper or "EXCEPT" in sql_upper:
        complexity += 2
    if sql_upper.count("SELECT") > 1:
        complexity += 1

    if complexity == 0:
        return "simple"
    if complexity <= 2:
        return "medium"
    if complexity <= 3:
        return "hard"
    return "extra"
