"""BIRD Benchmark 评测辅助工具。

功能：
1. 从 BIRD 的 SQLite databases 中执行 gold SQL 提取 gold values
2. 结果比对（Execution Accuracy 标准）
3. 支持 BIRD 的 dev_tables.json 加载 schema

BIRD 官方评测标准：
- Execution Accuracy (EX): 候选 SQL 的执行结果与 gold SQL 执行结果
  在同一 SQLite 数据库上一致（行集合匹配，忽略顺序）
- 注：本项目基于 PostgreSQL，此工具用于离线提取 gold 参考值

并发安全：每次调用独立 SQLite 连接，无共享状态。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BirdGoldResult:
    """BIRD 单条 gold SQL 执行结果。"""

    question_id: int
    db_id: str
    question: str
    gold_sql: str
    difficulty: str
    execution_success: bool
    result_rows: list[tuple[Any, ...]]
    result_hash: str
    error: str = ""


def load_bird_dev(bird_dir: str | Path) -> list[dict[str, Any]]:
    """加载 BIRD dev.json。"""
    dev_json = Path(bird_dir) / "dev.json"
    if not dev_json.exists():
        raise FileNotFoundError(f"BIRD dev.json 不存在: {dev_json}")
    return json.loads(dev_json.read_text(encoding="utf-8"))


def execute_gold_sql(
    bird_dir: str | Path,
    question_ids: list[int] | None = None,
    max_cases: int | None = None,
) -> list[BirdGoldResult]:
    """在 SQLite 数据库上执行 BIRD gold SQL，提取参考结果。

    Args:
        bird_dir: BIRD 数据集目录（需包含 dev.json 和 dev_databases/）
        question_ids: 仅执行指定 question_id（None 表示全部）
        max_cases: 最大执行数

    Returns:
        BirdGoldResult 列表
    """
    bird_path = Path(bird_dir)
    dev_data = load_bird_dev(bird_path)

    db_base = bird_path / "dev_databases"
    if not db_base.exists():
        dev_db_zip = bird_path / "dev_databases.zip"
        if dev_db_zip.exists():
            logger.info("解压 dev_databases.zip ...")
            import zipfile
            with zipfile.ZipFile(dev_db_zip) as zf:
                zf.extractall(bird_path)
        else:
            logger.warning("dev_databases 目录不存在且无 zip: %s", db_base)
            return []

    results: list[BirdGoldResult] = []
    db_connections: dict[str, sqlite3.Connection] = {}

    try:
        for item in dev_data:
            qid = item.get("question_id", -1)

            if question_ids is not None and qid not in question_ids:
                continue
            if max_cases is not None and len(results) >= max_cases:
                break

            db_id = str(item.get("db_id", ""))
            gold_sql = str(item.get("SQL", ""))
            question = str(item.get("question", ""))
            difficulty = str(item.get("difficulty", ""))

            if not gold_sql or not db_id:
                continue

            conn = db_connections.get(db_id)
            if conn is None:
                db_path = db_base / db_id / f"{db_id}.sqlite"
                if not db_path.exists():
                    results.append(BirdGoldResult(
                        question_id=qid, db_id=db_id, question=question,
                        gold_sql=gold_sql, difficulty=difficulty,
                        execution_success=False, result_rows=[], result_hash="",
                        error=f"DB not found: {db_path}",
                    ))
                    continue
                conn = sqlite3.connect(str(db_path))
                conn.text_factory = str
                db_connections[db_id] = conn

            try:
                cursor = conn.execute(gold_sql)
                rows = cursor.fetchall()
                row_hash = _hash_result_set(rows)
                results.append(BirdGoldResult(
                    question_id=qid, db_id=db_id, question=question,
                    gold_sql=gold_sql, difficulty=difficulty,
                    execution_success=True, result_rows=rows, result_hash=row_hash,
                ))
            except Exception as e:
                results.append(BirdGoldResult(
                    question_id=qid, db_id=db_id, question=question,
                    gold_sql=gold_sql, difficulty=difficulty,
                    execution_success=False, result_rows=[], result_hash="",
                    error=str(e)[:200],
                ))

    finally:
        for conn in db_connections.values():
            conn.close()

    logger.info("BIRD gold SQL 执行完成: %d / %d 成功",
                sum(1 for r in results if r.execution_success), len(results))
    return results


def compare_results(
    gold_rows: list[tuple[Any, ...]],
    candidate_rows: list[tuple[Any, ...]],
) -> bool:
    """BIRD 式结果比对（行集合匹配，忽略顺序）。"""
    if len(gold_rows) != len(candidate_rows):
        return False

    gold_set = _normalize_rows(gold_rows)
    cand_set = _normalize_rows(candidate_rows)
    return gold_set == cand_set


def export_gold_values(
    results: list[BirdGoldResult],
    output_path: str | Path,
) -> int:
    """导出 gold values 为 JSONL 供后续评测使用。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            if not r.execution_success:
                continue
            f.write(json.dumps({
                "question_id": r.question_id,
                "db_id": r.db_id,
                "question": r.question,
                "gold_sql": r.gold_sql,
                "difficulty": r.difficulty,
                "result_hash": r.result_hash,
                "result_rows_count": len(r.result_rows),
                "first_row": list(r.result_rows[0]) if r.result_rows else [],
            }, ensure_ascii=False) + "\n")
            count += 1

    logger.info("导出 %d 条 gold values -> %s", count, path)
    return count


# ── 内部辅助 ──────────────────────────────────────────────────────────────────


def _hash_result_set(rows: list[tuple[Any, ...]]) -> str:
    """对结果集计算确定性哈希（排序后，用于快速比对）。"""
    normalized = sorted(str(row) for row in rows)
    content = "\n".join(normalized)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _normalize_rows(rows: list[tuple[Any, ...]]) -> set[tuple[str, ...]]:
    """将结果行标准化为可比较的字符串元组集合。"""
    result: set[tuple[str, ...]] = set()
    for row in rows:
        normalized_row = tuple(
            str(v).strip().lower() if v is not None else "null"
            for v in row
        )
        result.add(normalized_row)
    return result
