"""Tests for parallel generator tournament logic (no LLM calls)."""

from src.nl2sql.agents.sql_agent.parallel_generator import (
    SQLCandidate,
    _hash_result,
    _tournament_select,
)


def test_tournament_all_agree() -> None:
    same_hash = _hash_result("result_data")
    candidates = [
        SQLCandidate(
            strategy="a", sql="SELECT 1", execution_success=True,
            execution_result="result_data", result_hash=same_hash,
        ),
        SQLCandidate(
            strategy="b", sql="SELECT 1", execution_success=True,
            execution_result="result_data", result_hash=same_hash,
        ),
    ]
    result = _tournament_select(candidates)
    assert result.consensus is True
    assert result.winner is not None


def test_tournament_majority_wins() -> None:
    hash_a = _hash_result("data_a")
    hash_b = _hash_result("data_b")
    candidates = [
        SQLCandidate(
            strategy="a", sql="SELECT 1", execution_success=True,
            execution_result="data_a", result_hash=hash_a,
        ),
        SQLCandidate(
            strategy="b", sql="SELECT 2", execution_success=True,
            execution_result="data_a", result_hash=hash_a,
        ),
        SQLCandidate(
            strategy="c", sql="SELECT 3", execution_success=True,
            execution_result="data_b", result_hash=hash_b,
        ),
    ]
    result = _tournament_select(candidates)
    assert result.winner is not None
    assert result.winner.result_hash == hash_a
    assert result.consensus is True


def test_tournament_all_fail() -> None:
    candidates = [
        SQLCandidate(strategy="a", sql="SELECT 1", execution_success=False, error="err"),
        SQLCandidate(strategy="b", sql="SELECT 2", execution_success=False, error="err"),
    ]
    result = _tournament_select(candidates)
    assert result.winner is not None
    assert result.consensus is False


def test_tournament_single_success() -> None:
    candidates = [
        SQLCandidate(
            strategy="a", sql="SELECT 1", execution_success=True,
            execution_result="ok", result_hash=_hash_result("ok"),
        ),
        SQLCandidate(strategy="b", sql="SELECT 2", execution_success=False, error="err"),
    ]
    result = _tournament_select(candidates)
    assert result.winner is not None
    assert result.winner.strategy == "a"
    assert result.consensus is True


def test_hash_result_deterministic() -> None:
    h1 = _hash_result("  some DATA  ")
    h2 = _hash_result("  some DATA  ")
    assert h1 == h2


def test_hash_result_case_insensitive() -> None:
    h1 = _hash_result("Hello World")
    h2 = _hash_result("hello world")
    assert h1 == h2
