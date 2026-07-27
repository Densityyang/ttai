"""Tests for Phase 5 BIRD evaluation helpers."""

from benchmarks.bird_eval import _hash_result_set, _normalize_rows, compare_results


class TestHashResultSet:
    def test_deterministic(self) -> None:
        rows = [(1, "a"), (2, "b")]
        assert _hash_result_set(rows) == _hash_result_set(rows)

    def test_order_insensitive(self) -> None:
        rows_a = [(1, "a"), (2, "b")]
        rows_b = [(2, "b"), (1, "a")]
        assert _hash_result_set(rows_a) == _hash_result_set(rows_b)

    def test_different_content(self) -> None:
        rows_a = [(1, "a")]
        rows_b = [(1, "b")]
        assert _hash_result_set(rows_a) != _hash_result_set(rows_b)


class TestNormalizeRows:
    def test_basic(self) -> None:
        rows = [(1, "Hello", None)]
        result = _normalize_rows(rows)
        assert ("1", "hello", "null") in result

    def test_whitespace(self) -> None:
        rows = [(" A ", 2)]
        result = _normalize_rows(rows)
        assert ("a", "2") in result


class TestCompareResults:
    def test_equal_same_order(self) -> None:
        gold = [(1, "a"), (2, "b")]
        cand = [(1, "a"), (2, "b")]
        assert compare_results(gold, cand)

    def test_equal_different_order(self) -> None:
        gold = [(1, "a"), (2, "b")]
        cand = [(2, "b"), (1, "a")]
        assert compare_results(gold, cand)

    def test_different_content(self) -> None:
        gold = [(1, "a")]
        cand = [(1, "b")]
        assert not compare_results(gold, cand)

    def test_different_length(self) -> None:
        gold = [(1,), (2,)]
        cand = [(1,)]
        assert not compare_results(gold, cand)

    def test_empty(self) -> None:
        assert compare_results([], [])

    def test_case_insensitive(self) -> None:
        gold = [("Hello",)]
        cand = [("hello",)]
        assert compare_results(gold, cand)

    def test_null_handling(self) -> None:
        gold = [(None, 1)]
        cand = [(None, 1)]
        assert compare_results(gold, cand)
