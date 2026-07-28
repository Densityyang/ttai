"""上下文压缩策略 -- Phase 4 Context Engineering。

策略：
1. 新旧结果分层：最近 N 轮工具结果保留完整，更早的压缩为摘要引用
2. 超长 schema 裁剪：仅保留与当前查询相关的列子集
3. 全局摘要兜底：当压缩后仍超限时触发 LLM 摘要

并发安全说明：
- 所有函数为纯函数（接收消息列表，返回新列表），无共享可变状态
- LLM 摘要调用使用独立的 LLM 实例
"""

import logging
import re

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

logger = logging.getLogger(__name__)

# ── TUNABLE: 压缩参数 ──────────────────────────────────────────────────────────

# 保留完整内容的最近工具调用轮次
RECENT_FULL_ROUNDS: int = 2  # TUNABLE: 可能需要根据上下文窗口大小调整

# 压缩后的摘要最大字符数
SUMMARY_MAX_CHARS: int = 200  # TUNABLE: 较短的摘要节省 token 但可能丢失细节

# 触发全局摘要的消息数阈值
GLOBAL_SUMMARY_MSG_THRESHOLD: int = 30  # TUNABLE: 根据模型上下文窗口调整

# Schema 裁剪时每张表保留的最大列数
SCHEMA_MAX_COLUMNS_PER_TABLE: int = 20  # TUNABLE: 太小可能遗漏关键列

# 单条消息内容的截断长度（字符数）
MAX_MESSAGE_CONTENT_CHARS: int = 3000  # TUNABLE: 防止单条消息占满上下文


def compress_messages(
    messages: list[AnyMessage],
    recent_full_rounds: int = RECENT_FULL_ROUNDS,
) -> list[AnyMessage]:
    """分层压缩消息列表。

    策略：
    - SystemMessage 始终保留完整
    - 最近 recent_full_rounds 轮的 ToolMessage 保留完整
    - 更早的 ToolMessage 压缩为摘要
    - HumanMessage 始终保留完整（用户意图不可丢失）
    - AIMessage 内容过长时截断

    Args:
        messages: 原始消息列表
        recent_full_rounds: 保留完整内容的最近轮次

    Returns:
        压缩后的消息列表（新列表，不修改原始列表）
    """
    if not messages:
        return []

    tool_indices = _find_tool_message_rounds(messages)

    recent_cutoff = len(tool_indices) - recent_full_rounds
    old_round_indices: set[int] = set()
    for i, indices in enumerate(tool_indices):
        if i < recent_cutoff:
            old_round_indices.update(indices)

    compressed: list[AnyMessage] = []
    for i, msg in enumerate(messages):
        if isinstance(msg, SystemMessage):
            compressed.append(msg)
        elif isinstance(msg, HumanMessage):
            compressed.append(msg)
        elif isinstance(msg, ToolMessage):
            if i in old_round_indices:
                compressed.append(_compress_tool_message(msg))
            else:
                content = str(msg.content) if msg.content else ""
                if len(content) > MAX_MESSAGE_CONTENT_CHARS:
                    compressed.append(_truncate_tool_message(msg))
                else:
                    compressed.append(msg)
        elif isinstance(msg, AIMessage):
            content = str(msg.content) if msg.content else ""
            if len(content) > MAX_MESSAGE_CONTENT_CHARS:
                compressed.append(_truncate_ai_message(msg))
            else:
                compressed.append(msg)
        else:
            compressed.append(msg)

    return compressed


def prune_schema_for_query(
    schema_text: str,
    question: str,
    max_columns_per_table: int = SCHEMA_MAX_COLUMNS_PER_TABLE,
) -> str:
    """根据查询裁剪 schema 信息，只保留相关列。

    策略：
    - 从问题中提取关键词
    - 对每张表的列，优先保留名称或注释匹配关键词的列
    - 不匹配的列超出限制时截断并标注"... N 列已省略"

    Args:
        schema_text: 原始 schema 描述文本
        question: 用户问题
        max_columns_per_table: 每张表保留的最大列数

    Returns:
        裁剪后的 schema 文本
    """
    keywords = _extract_keywords(question)
    if not keywords:
        return schema_text

    lines = schema_text.split("\n")
    result_lines: list[str] = []
    current_table_columns: list[str] = []
    current_table_header: str | None = None

    for line in lines:
        if line.startswith("表：") or line.startswith("\n表："):
            if current_table_header is not None:
                result_lines.extend(
                    _select_relevant_columns(
                        current_table_header, current_table_columns,
                        keywords, max_columns_per_table,
                    )
                )
            current_table_header = line
            current_table_columns = []
        elif line.strip().startswith("- ") and current_table_header is not None:
            current_table_columns.append(line)
        else:
            if current_table_header is not None:
                result_lines.extend(
                    _select_relevant_columns(
                        current_table_header, current_table_columns,
                        keywords, max_columns_per_table,
                    )
                )
                current_table_header = None
                current_table_columns = []
            result_lines.append(line)

    if current_table_header is not None:
        result_lines.extend(
            _select_relevant_columns(
                current_table_header, current_table_columns,
                keywords, max_columns_per_table,
            )
        )

    return "\n".join(result_lines)


def should_trigger_global_summary(messages: list[AnyMessage]) -> bool:
    """判断是否需要触发全局摘要。"""
    return len(messages) > GLOBAL_SUMMARY_MSG_THRESHOLD


async def generate_global_summary(
    messages: list[AnyMessage],
    question: str,
) -> str:
    """使用 LLM 生成全局对话摘要。

    仅在消息数过多、压缩后仍然超出上下文窗口时使用。

    Args:
        messages: 当前消息列表
        question: 当前用户问题（摘要时保留与此最相关的信息）

    Returns:
        摘要文本
    """
    from src.nl2sql.infra.llm.gateway import get_legacy_model

    llm = get_legacy_model()

    content_parts: list[str] = []
    for msg in messages:
        role = msg.type if hasattr(msg, "type") else "unknown"
        text = str(msg.content)[:500] if msg.content else ""
        if text:
            content_parts.append(f"[{role}] {text}")

    conversation = "\n".join(content_parts[-20:])

    prompt = (
        "请将以下对话摘要为简洁的上下文信息（不超过300字），"
        f"重点保留与当前问题相关的信息。\n\n"
        f"当前问题：{question}\n\n"
        f"对话内容：\n{conversation}\n\n"
        "摘要："
    )

    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        return str(response.content).strip()
    except Exception as e:
        logger.warning("全局摘要生成失败: %s", e)
        return f"[对话包含 {len(messages)} 条消息，摘要生成失败]"


# ── 内部辅助函数 ──────────────────────────────────────────────────────────────


def _find_tool_message_rounds(messages: list[AnyMessage]) -> list[list[int]]:
    """将 ToolMessage 按相邻分组为"轮次"。"""
    rounds: list[list[int]] = []
    current_round: list[int] = []

    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage):
            current_round.append(i)
        else:
            if current_round:
                rounds.append(current_round)
                current_round = []

    if current_round:
        rounds.append(current_round)

    return rounds


def _compress_tool_message(msg: ToolMessage) -> ToolMessage:
    """将 ToolMessage 压缩为摘要版本。"""
    content = str(msg.content) if msg.content else ""
    if not content:
        return msg

    summary = _make_summary(content)
    return ToolMessage(
        content=f"[历史结果摘要] {summary}",
        tool_call_id=msg.tool_call_id,
        name=msg.name,
    )


def _truncate_tool_message(msg: ToolMessage) -> ToolMessage:
    """截断过长的 ToolMessage。"""
    content = str(msg.content) if msg.content else ""
    truncated = content[:MAX_MESSAGE_CONTENT_CHARS] + f"\n... [已截断，原始长度 {len(content)} 字符]"
    return ToolMessage(
        content=truncated,
        tool_call_id=msg.tool_call_id,
        name=msg.name,
    )


def _truncate_ai_message(msg: AIMessage) -> AIMessage:
    """截断过长的 AIMessage（保留 tool_calls）。"""
    content = str(msg.content) if msg.content else ""
    truncated = content[:MAX_MESSAGE_CONTENT_CHARS] + "\n... [已截断]"
    return AIMessage(
        content=truncated,
        tool_calls=msg.tool_calls if hasattr(msg, "tool_calls") else [],
    )


def _make_summary(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """对文本做简单的规则摘要（无 LLM）。"""
    lines = text.strip().split("\n")
    non_empty = [line.strip() for line in lines if line.strip()]

    if not non_empty:
        return "[空结果]"

    if len(non_empty) == 1:
        line = non_empty[0]
        return line[:max_chars] + "..." if len(line) > max_chars else line

    first_line = non_empty[0][:max_chars // 2]
    last_line = non_empty[-1][:max_chars // 2]
    return f"{first_line} ... ({len(non_empty)} 行) ... {last_line}"


def _extract_keywords(question: str) -> set[str]:
    """从问题中提取关键词用于 schema 裁剪。"""
    normalized = question.lower()
    tokens = set(re.findall(r"[a-zA-Z_]{3,}", normalized))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        tokens.add(phrase)
        tokens.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    stop_words = {
        "什么", "哪些", "多少", "怎么", "如何", "为什么", "请问", "帮我",
        "查询", "查一下", "告诉", "的", "是", "了", "有", "在",
    }
    return {token for token in tokens if token not in stop_words}


def _select_relevant_columns(
    table_header: str,
    columns: list[str],
    keywords: set[str],
    max_cols: int,
) -> list[str]:
    """从表的列中选取与关键词最相关的列。"""
    if len(columns) <= max_cols:
        return [table_header] + columns

    scored: list[tuple[int, str]] = []
    for col in columns:
        col_lower = col.lower()
        score = sum(1 for kw in keywords if kw in col_lower)
        # PK 和 FK 列始终优先
        if "[PK]" in col:
            score += 10
        if "foreign_key" in col_lower or "fk" in col_lower:
            score += 5
        scored.append((score, col))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [col for _, col in scored[:max_cols]]
    omitted = len(columns) - max_cols

    result = [table_header] + selected
    if omitted > 0:
        result.append(f"  ... ({omitted} 列已省略，与当前查询关联度较低)")

    return result
