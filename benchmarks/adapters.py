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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from benchmarks.registry import (
    CLARIFICATION_TAGS,
    SECURITY_TAGS,
    CaseOracle,
    EvalCase,
    ExpectedOutcome,
    derive_case_revision,
    derive_expected_outcome,
    sha256_hex,
)

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
    expected_outcome: str = ""  # P9A pre-labelled 6.6.1 expected outcome


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


def load_synthetic_cases(
    directory: str | Path,
    *,
    source: str = "synthetic",
    max_cases: int | None = None,
) -> list[BenchmarkCase]:
    """Load a self-contained JSONL case set whose source label is explicit.

    Used by the P9A synthetic typed set: it is harness material and must never be
    confused with a public dataset, so the caller names its source.
    """
    directory_path = Path(directory)
    cases: list[BenchmarkCase] = []
    for jsonl_file in sorted(directory_path.glob("*.jsonl")):
        for line in jsonl_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if max_cases is not None and len(cases) >= max_cases:
                break
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("解析 %s case 失败 (%s): %s", source, jsonl_file.name, exc)
                continue
            cases.append(BenchmarkCase(
                case_id=str(item.get("case_id", f"{source}-{len(cases)}")),
                source=source,
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
                expected_outcome=str(item.get("expected_outcome", "")),
            ))
    logger.info("加载 %s 评测集: %d cases", source, len(cases))
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
                    expected_outcome=str(item.get("expected_outcome", "")),
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
                        expected_outcome=str(item.get("expected_outcome", "")),
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


# ── P9A BenchmarkCase -> EvalCase 转换 ──────────────────────────────────────


_BUILD_TAGS = frozenset({"build", "plan_confirmation", "confirmed_plan", "materialize"})
_ANALYZE_TAGS = frozenset(
    {
        "codeact",
        "dynamic_metric",
        "hitl",
        "statistics",
        "regression",
        "prediction",
        "anomaly_detection",
        "time_series",
    }
)
_COMPUTE_TAGS = frozenset(
    {
        "dynamic_metric",
        "codeact",
        "statistics",
        "weighted_avg",
        "ratio",
        "ranking",
        "yoy",
        "mom",
        "moving_average",
        "regression",
        "prediction",
        "anomaly_detection",
        "pareto",
        "abc_analysis",
        "funnel",
        "cv",
    }
)
_VALID_EXPECTED_OUTCOMES = frozenset(
    {
        "CORRECT_ANSWER",
        "CORRECT_CLARIFICATION",
        "CORRECT_HITL",
        "CORRECT_RESULT_UNAVAILABLE",
        "CORRECT_REJECTION",
    }
)


def resolve_expected_outcome(case: BenchmarkCase) -> ExpectedOutcome:
    """Prefer an explicit 6.6.1 label, else derive it from legacy labels."""
    if case.expected_outcome in _VALID_EXPECTED_OUTCOMES:
        return cast(ExpectedOutcome, case.expected_outcome)
    return derive_expected_outcome(
        expected_mode=case.expected_mode,
        should_reject=case.should_reject,
        is_adversarial=case.is_adversarial,
        tags=case.tags,
    )


def derive_mode(case: BenchmarkCase) -> str:
    tags = set(case.tags)
    if tags & _BUILD_TAGS:
        return "BUILD"
    if case.expected_mode == "sql_plus_code" or tags & _ANALYZE_TAGS:
        return "ANALYZE"
    return "QUERY"


def derive_capability(case: BenchmarkCase, expected_outcome: str) -> str:
    tags = set(case.tags)
    if expected_outcome == "CORRECT_REJECTION" or tags & SECURITY_TAGS:
        return "safety"
    if expected_outcome in ("CORRECT_CLARIFICATION", "CORRECT_HITL") or "hitl" in tags:
        return "clarification"
    if expected_outcome == "CORRECT_RESULT_UNAVAILABLE":
        return "availability"
    if case.expected_mode == "sql_plus_code" or tags & _COMPUTE_TAGS:
        return "compute"
    return "fetch"


def derive_risk(case: BenchmarkCase, expected_outcome: str) -> str:
    tags = set(case.tags)
    if case.is_adversarial or case.should_reject or tags & SECURITY_TAGS:
        return "high"
    if expected_outcome in ("CORRECT_CLARIFICATION", "CORRECT_HITL"):
        return "medium"
    if tags & CLARIFICATION_TAGS:
        return "medium"
    return "low"


def build_case_oracle(case: BenchmarkCase, expected_outcome: str) -> CaseOracle | None:
    """Build the independent oracle a case needs to be adjudicable at all.

    An answer case with neither a gold value nor a reference SQL gets NO
    oracle: it must evaluate to UNKNOWN rather than a silent PASS.
    """
    revision = f"{case.case_id}-oracle-r1"
    if expected_outcome == "CORRECT_ANSWER":
        if case.gold_value is not None:
            return CaseOracle(
                oracle_kind="approved_fact",
                oracle_revision=revision,
                expected_value=case.gold_value,
                tolerance=case.tolerance,
            )
        if case.gold_sql.strip():
            return CaseOracle(
                oracle_kind="independent_reference_sql",
                oracle_revision=revision,
                reference_sql_fingerprint=sha256_hex(case.gold_sql.strip()),
                tolerance=case.tolerance,
            )
        return None
    return CaseOracle(oracle_kind="reviewed_label", oracle_revision=revision)


def benchmark_case_to_eval_case(case: BenchmarkCase) -> EvalCase:
    """Convert one legacy BenchmarkCase without breaking its consumers."""
    expected = resolve_expected_outcome(case)
    tags = tuple(str(tag) for tag in case.tags)
    revision = derive_case_revision(
        {
            "case_id": case.case_id,
            "source": case.source,
            "layer": case.layer,
            "domain": case.domain,
            "question": case.question,
            "gold_sql": case.gold_sql,
            "gold_value": case.gold_value,
            "expected_mode": case.expected_mode,
            "difficulty": case.difficulty,
            "tags": list(tags),
            "is_adversarial": case.is_adversarial,
            "should_reject": case.should_reject,
            "expected_outcome": expected,
        }
    )
    return EvalCase(
        case_id=case.case_id,
        revision=revision,
        source=case.source,
        layer=case.layer,
        domain=case.domain,
        question=case.question,
        mode=cast(Any, derive_mode(case)),
        capability=derive_capability(case, expected),
        risk=cast(Any, derive_risk(case, expected)),
        tags=tags,
        expected_outcome=expected,
        oracle=build_case_oracle(case, expected),
        legacy_expected_mode=case.expected_mode,
        difficulty=case.difficulty,
        tolerance=case.tolerance,
    )


def to_eval_cases(cases: Sequence[BenchmarkCase]) -> tuple[EvalCase, ...]:
    return tuple(benchmark_case_to_eval_case(case) for case in cases)


def eval_case_to_benchmark_case(case: EvalCase) -> BenchmarkCase:
    """Reconstruct an executable case from a registry case.

    Selection operates on EvalCase; execution still speaks BenchmarkCase.  The
    oracle value (not just its digest) and the reviewed labels travel across, so
    nothing the evaluator needs is lost.  A reference-SQL-only oracle has no
    value to carry; it keeps its digest on the EvalCase, which is the object the
    evaluator actually adjudicates.
    """
    oracle = case.oracle
    return BenchmarkCase(
        case_id=case.case_id,
        source=case.source,
        layer=case.layer,
        domain=case.domain,
        question=case.question,
        gold_sql="",
        gold_value=oracle.expected_value if oracle is not None else None,
        expected_mode=case.legacy_expected_mode,
        difficulty=case.difficulty,
        tolerance=oracle.tolerance if oracle is not None else case.tolerance,
        tags=list(case.tags),
        is_adversarial=bool(set(case.tags) & SECURITY_TAGS),
        should_reject=case.expected_outcome == "CORRECT_REJECTION",
        expected_outcome=case.expected_outcome,
    )


def load_enterprise_eval_cases(enterprise_dir: str | Path) -> tuple[EvalCase, ...]:
    return to_eval_cases(load_enterprise_cases(enterprise_dir))


def load_bird_eval_cases(
    bird_dir: str | Path,
    max_cases: int | None = None,
) -> tuple[EvalCase, ...]:
    return to_eval_cases(load_bird_cases(bird_dir, max_cases=max_cases))


def load_spider_eval_cases(
    spider_dir: str | Path,
    max_cases: int | None = None,
) -> tuple[EvalCase, ...]:
    return to_eval_cases(load_spider_cases(spider_dir, max_cases=max_cases))
