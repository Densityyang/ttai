"""经验记忆库 -- Memo-SQL 风格的成功查询/错误修复对存储。

两类动态记忆：
1. 成功查询记忆：(问题模式, schema 片段, 成功 SQL) 三元组
2. 错误修复对：(错误 SQL, 错误原因, 修复后 SQL) 三元组

当前版本使用内存存储 + 基于关键词的简单匹配。
后续可升级为 PostgreSQL + pgvector 语义匹配。

并发安全说明：
- ExperienceStore 使用 threading.Lock 保护所有写操作
- 读操作（search_*）在持锁期间执行以保证一致性快照
- lru_cache 的 get_experience_store() 返回全局单例，多个异步任务共享
- 由于 asyncio 是协程级并发（单线程事件循环），实际上锁开销极小
- 但在 LangGraph 使用 ThreadPoolExecutor 的场景下，锁提供了正确性保证
"""

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

MAX_MEMORY_SIZE = 500
SIMILARITY_THRESHOLD = 0.3


@dataclass
class SuccessMemory:
    """成功查询记忆。"""

    question_pattern: str
    table_names: list[str]
    schema_snippet: str
    sql: str
    result_summary: str
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0

    @property
    def key(self) -> str:
        return hashlib.md5(
            f"{self.question_pattern}:{','.join(sorted(self.table_names))}".encode()
        ).hexdigest()[:16]


@dataclass
class ErrorRepairPair:
    """错误修复对。"""

    error_sql: str
    error_message: str
    error_type: str
    repaired_sql: str
    repair_explanation: str = ""
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0

    @property
    def key(self) -> str:
        return hashlib.md5(
            f"{self.error_type}:{self.error_message[:100]}".encode()
        ).hexdigest()[:16]


class ExperienceStore:
    """经验记忆库（线程安全）。

    所有公共方法均通过 _lock 保护，确保在多线程环境下的正确性。
    锁粒度为方法级（非字段级），因为 eviction 和 search 都涉及多步操作。
    """

    def __init__(self, max_size: int = MAX_MEMORY_SIZE) -> None:
        self._success_memories: dict[str, SuccessMemory] = {}
        self._repair_pairs: dict[str, ErrorRepairPair] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    # ── 写入 ──────────────────────────────────────────────────────────────

    def record_success(
        self,
        question: str,
        table_names: list[str],
        schema_snippet: str,
        sql: str,
        result_summary: str = "",
    ) -> None:
        """记录一次成功查询（线程安全）。"""
        mem = SuccessMemory(
            question_pattern=_normalize_question(question),
            table_names=table_names,
            schema_snippet=schema_snippet,
            sql=sql,
            result_summary=result_summary,
        )
        with self._lock:
            self._success_memories[mem.key] = mem
            self._evict_if_needed(self._success_memories)
        logger.debug("记录成功查询经验: key=%s, tables=%s", mem.key, table_names)

    def record_repair(
        self,
        error_sql: str,
        error_message: str,
        repaired_sql: str,
        repair_explanation: str = "",
    ) -> None:
        """记录一次错误修复对（线程安全）。"""
        error_type = _classify_error(error_message)
        pair = ErrorRepairPair(
            error_sql=error_sql,
            error_message=error_message,
            error_type=error_type,
            repaired_sql=repaired_sql,
            repair_explanation=repair_explanation,
        )
        with self._lock:
            self._repair_pairs[pair.key] = pair
            self._evict_if_needed(self._repair_pairs)
        logger.debug("记录修复经验: key=%s, type=%s", pair.key, error_type)

    # ── 检索 ──────────────────────────────────────────────────────────────

    def search_similar_queries(
        self,
        question: str,
        table_names: list[str] | None = None,
        top_k: int = 3,
    ) -> list[SuccessMemory]:
        """搜索相似的成功查询经验（线程安全）。

        注意：此方法会修改 hit_count，因此需要在锁内执行。
        """
        normalized = _normalize_question(question)
        query_tokens = _tokenize_for_similarity(normalized)

        with self._lock:
            if not self._success_memories:
                return []

            scored: list[tuple[float, SuccessMemory]] = []
            for mem in self._success_memories.values():
                mem_tokens = _tokenize_for_similarity(mem.question_pattern)
                if not mem_tokens:
                    continue

                token_overlap = len(query_tokens & mem_tokens) / max(len(query_tokens | mem_tokens), 1)

                table_bonus = 0.0
                if table_names:
                    common_tables = set(table_names) & set(mem.table_names)
                    if common_tables:
                        table_bonus = 0.3 * len(common_tables) / max(len(table_names), 1)

                score = token_overlap + table_bonus
                if score >= SIMILARITY_THRESHOLD:
                    scored.append((score, mem))

            scored.sort(key=lambda x: x[0], reverse=True)

            results = []
            for _, mem in scored[:top_k]:
                mem.hit_count += 1
                results.append(mem)
            return results

    def search_repair_patterns(
        self,
        error_message: str,
        top_k: int = 3,
    ) -> list[ErrorRepairPair]:
        """搜索类似的错误修复模式（线程安全）。"""
        error_type = _classify_error(error_message)
        error_tokens = _tokenize_for_similarity(_normalize_question(error_message))

        with self._lock:
            if not self._repair_pairs:
                return []

            scored: list[tuple[float, ErrorRepairPair]] = []
            for pair in self._repair_pairs.values():
                type_match = 0.5 if pair.error_type == error_type else 0.0
                pair_tokens = _tokenize_for_similarity(_normalize_question(pair.error_message))
                if pair_tokens:
                    token_overlap = len(error_tokens & pair_tokens) / max(len(error_tokens | pair_tokens), 1)
                else:
                    token_overlap = 0.0

                score = type_match + token_overlap * 0.5
                if score >= SIMILARITY_THRESHOLD:
                    scored.append((score, pair))

            scored.sort(key=lambda x: x[0], reverse=True)

            results = []
            for _, pair in scored[:top_k]:
                pair.hit_count += 1
                results.append(pair)
            return results

    def format_experience_context(
        self,
        question: str,
        table_names: list[str] | None = None,
    ) -> str:
        """将匹配到的经验格式化为可注入的上下文文本。"""
        memories = self.search_similar_queries(question, table_names)
        if not memories:
            return ""

        parts = ["【历史成功查询参考】\n"]
        for i, mem in enumerate(memories, 1):
            parts.append(
                f"案例 {i}:\n"
                f"  问题: {mem.question_pattern[:100]}\n"
                f"  表: {', '.join(mem.table_names)}\n"
                f"  SQL: {mem.sql[:300]}\n"
            )

        return "\n".join(parts)

    def format_repair_context(self, error_message: str) -> str:
        """将匹配到的修复经验格式化为可注入的上下文文本。"""
        pairs = self.search_repair_patterns(error_message)
        if not pairs:
            return ""

        parts = ["【历史修复经验参考】\n"]
        for i, pair in enumerate(pairs, 1):
            parts.append(
                f"修复案例 {i} ({pair.error_type}):\n"
                f"  错误: {pair.error_message[:100]}\n"
                f"  修复方式: {pair.repair_explanation or '直接修复'}\n"
                f"  修复后 SQL: {pair.repaired_sql[:300]}\n"
            )

        return "\n".join(parts)

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _evict_if_needed(self, store: dict[str, Any]) -> None:
        """LRU 淘汰。"""
        if len(store) <= self._max_size:
            return

        items = sorted(
            store.items(),
            key=lambda x: (getattr(x[1], "hit_count", 0), getattr(x[1], "created_at", 0)),
        )
        to_remove = len(store) - self._max_size
        for key, _ in items[:to_remove]:
            del store[key]

    @property
    def stats(self) -> dict[str, int]:
        return {
            "success_memories": len(self._success_memories),
            "repair_pairs": len(self._repair_pairs),
        }


def _normalize_question(text: str) -> str:
    """标准化问题文本用于匹配。"""

    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _tokenize_for_similarity(text: str) -> set[str]:
    """Produce stable word and CJK bigram tokens for lightweight similarity."""
    tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}", text))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return tokens


def _classify_error(error_message: str) -> str:
    """对错误信息分类。"""
    msg = error_message.lower()
    if "column" in msg and ("not found" in msg or "does not exist" in msg or "不存在" in msg):
        return "column_not_found"
    if (
        "table" in msg or "relation" in msg
    ) and ("not found" in msg or "does not exist" in msg or "不存在" in msg):
        return "table_not_found"
    if "syntax" in msg or "语法" in msg:
        return "syntax_error"
    if "type" in msg and ("mismatch" in msg or "不匹配" in msg or "cannot" in msg):
        return "type_mismatch"
    if "permission" in msg or "denied" in msg or "权限" in msg:
        return "permission_denied"
    if "timeout" in msg or "超时" in msg:
        return "timeout"
    if "division" in msg or "除零" in msg or "divide by zero" in msg:
        return "division_by_zero"
    return "unknown"


@lru_cache
def get_experience_store() -> ExperienceStore:
    """获取全局经验记忆库单例。"""
    return ExperienceStore()
