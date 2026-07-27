"""Tests for Phase 4 KV-Cache friendly prompt builder."""

from src.nl2sql.infra.context.prompt_builder import (
    _STATIC_PREFIX,
    build_system_prompt,
    build_tool_availability_mask,
    inject_session_summary,
)


class TestBuildSystemPrompt:
    def test_contains_static_prefix(self) -> None:
        prompt = build_system_prompt()
        assert "TT智能助手" in prompt
        assert "二分类路由规则" in prompt

    def test_static_prefix_stable(self) -> None:
        """Static prefix should be identical across calls (KV-Cache friendly)."""
        p1 = build_system_prompt(current_time="2026-01-01", user_info="user_a")
        p2 = build_system_prompt(current_time="2026-06-01", user_info="user_b")
        # Both should start with the same static prefix
        assert p1.startswith(_STATIC_PREFIX)
        assert p2.startswith(_STATIC_PREFIX)

    def test_dynamic_time_appended(self) -> None:
        prompt = build_system_prompt(current_time="2026-04-01 12:00:00")
        assert "2026-04-01 12:00:00" in prompt

    def test_dynamic_user_appended(self) -> None:
        prompt = build_system_prompt(user_info="user_id=42")
        assert "user_id=42" in prompt

    def test_extra_context_appended(self) -> None:
        prompt = build_system_prompt(extra_context="会话已进行3轮对话")
        assert "会话已进行3轮对话" in prompt

    def test_default_values(self) -> None:
        prompt = build_system_prompt()
        assert "未知用户" in prompt


class TestToolMask:
    def test_available_tools_marked(self) -> None:
        mask = build_tool_availability_mask(
            available_tools=["query_database_with_semantic_sql"],
        )
        assert "可用" in mask
        assert "未启用" in mask

    def test_all_available(self) -> None:
        mask = build_tool_availability_mask(
            available_tools=[
                "query_database_with_semantic_sql",
                "codeact_dynamic_calculation",
                "query_database",
                "generate_data_and_chart",
            ],
        )
        assert "未启用" not in mask

    def test_custom_tool_list(self) -> None:
        mask = build_tool_availability_mask(
            available_tools=["tool_a"],
            all_tools=["tool_a", "tool_b"],
        )
        assert "tool_a: 可用" in mask
        assert "tool_b: 未启用" in mask


class TestSessionSummary:
    def test_inject_summary(self) -> None:
        base = "system prompt content"
        result = inject_session_summary(base, "用户之前问了投诉数据")
        assert "用户之前问了投诉数据" in result
        assert "会话摘要" in result

    def test_empty_summary_unchanged(self) -> None:
        base = "system prompt content"
        result = inject_session_summary(base, "")
        assert result == base
