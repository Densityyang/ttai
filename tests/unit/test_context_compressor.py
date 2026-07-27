"""Tests for Phase 4 context compression."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.nl2sql.infra.context.compressor import (
    _extract_keywords,
    _make_summary,
    compress_messages,
    prune_schema_for_query,
    should_trigger_global_summary,
)


def _make_tool_msg(content: str, call_id: str = "c1") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, name="test_tool")


class TestCompressMessages:
    def test_empty_messages(self) -> None:
        assert compress_messages([]) == []

    def test_system_message_preserved(self) -> None:
        msgs = [SystemMessage(content="system prompt")]
        result = compress_messages(msgs)
        assert len(result) == 1
        assert result[0].content == "system prompt"

    def test_human_message_preserved(self) -> None:
        msgs = [HumanMessage(content="user question")]
        result = compress_messages(msgs)
        assert len(result) == 1
        assert result[0].content == "user question"

    def test_old_tool_messages_compressed(self) -> None:
        msgs = [
            HumanMessage(content="q1"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
            _make_tool_msg("old result " * 100, "c1"),
            HumanMessage(content="q2"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c2"}]),
            _make_tool_msg("old result 2 " * 100, "c2"),
            HumanMessage(content="q3"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c3"}]),
            _make_tool_msg("recent result " * 50, "c3"),
            HumanMessage(content="q4"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c4"}]),
            _make_tool_msg("latest result", "c4"),
        ]
        result = compress_messages(msgs, recent_full_rounds=2)

        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        compressed_count = sum(1 for m in tool_msgs if "摘要" in str(m.content))
        assert compressed_count >= 1

    def test_recent_messages_kept_full(self) -> None:
        msgs = [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
            _make_tool_msg("result data", "c1"),
        ]
        result = compress_messages(msgs, recent_full_rounds=2)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert "result data" in str(tool_msgs[0].content)


class TestSchemaRuning:
    def test_short_schema_unchanged(self) -> None:
        schema = "表：orders\n  - id: INT [PK]\n  - name: VARCHAR"
        result = prune_schema_for_query(schema, "查询订单")
        assert "id" in result
        assert "name" in result

    def test_prunes_irrelevant_columns(self) -> None:
        columns = "\n".join([f"  - col_{i}: INT" for i in range(30)])
        schema = f"表：orders\n  - order_id: INT [PK]\n  - 投诉类型: VARCHAR -- 投诉分类\n{columns}"
        result = prune_schema_for_query(schema, "投诉类型统计", max_columns_per_table=5)
        assert "投诉类型" in result
        assert "已省略" in result

    def test_pk_always_preserved(self) -> None:
        columns = "\n".join([f"  - col_{i}: INT" for i in range(30)])
        schema = f"表：orders\n  - id: INT [PK]\n{columns}"
        result = prune_schema_for_query(schema, "随便问", max_columns_per_table=3)
        assert "[PK]" in result


class TestGlobalSummary:
    def test_below_threshold(self) -> None:
        msgs = [HumanMessage(content="q")] * 10
        assert should_trigger_global_summary(msgs) is False

    def test_above_threshold(self) -> None:
        msgs = [HumanMessage(content="q")] * 50
        assert should_trigger_global_summary(msgs) is True


class TestHelpers:
    def test_make_summary_short(self) -> None:
        assert _make_summary("hello") == "hello"

    def test_make_summary_multiline(self) -> None:
        text = "line1\nline2\nline3\nline4"
        result = _make_summary(text)
        assert "4 行" in result

    def test_make_summary_empty(self) -> None:
        assert _make_summary("") == "[空结果]"

    def test_extract_keywords(self) -> None:
        kws = _extract_keywords("各区县投诉工单的及时率统计")
        assert "区县" in kws or "投诉" in kws or "工单" in kws
        assert "查询" not in kws
