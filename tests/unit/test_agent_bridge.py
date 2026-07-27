"""Tests for Phase 5 agent_bridge module."""

from benchmarks.agent_bridge import _check_rejection, _extract_text, _extract_value


class TestCheckRejection:
    def test_rejection_detected(self) -> None:
        assert _check_rejection("很抱歉，无法执行此操作")
        assert _check_rejection("该请求被拒绝")
        assert _check_rejection("禁止执行删除操作")
        assert _check_rejection("违反安全策略")

    def test_normal_answer(self) -> None:
        assert not _check_rejection("查询结果：共有 42 条工单")
        assert not _check_rejection("投诉工单总数为 1234")

    def test_empty(self) -> None:
        assert not _check_rejection("")


class TestExtractValue:
    def test_single_number(self) -> None:
        assert _extract_value("工单总数为 42") == 42

    def test_decimal(self) -> None:
        val = _extract_value("及时率为 95.5%")
        assert val == 95.5

    def test_comma_number(self) -> None:
        assert _extract_value("共 1,234 条") == 1234

    def test_no_number(self) -> None:
        assert _extract_value("没有找到相关数据") == "没有找到相关数据"

    def test_empty(self) -> None:
        assert _extract_value("") is None

    def test_multiple_numbers_takes_last(self) -> None:
        val = _extract_value("从 100 条记录中筛选出 42 条")
        assert val == 42


class TestExtractText:
    def test_string(self) -> None:
        assert _extract_text("hello") == "hello"

    def test_list_format(self) -> None:
        content = [{"text": "part1"}, {"text": "part2"}]
        assert "part1" in _extract_text(content)

    def test_none(self) -> None:
        assert _extract_text(None) == ""

    def test_empty_string(self) -> None:
        assert _extract_text("") == ""
