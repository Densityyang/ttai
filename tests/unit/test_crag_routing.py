"""Tests for CRAG three-tier confidence routing logic."""

from src.nl2sql.agents.sql_agent.agentic_rag import after_grade_router


def _make_state(
    graded: list[dict] | None = None,
    tier: str = "incorrect",
    rewrite_count: int = 0,
) -> dict:
    """Create a minimal AgenticRagState-like dict for router testing."""
    return {
        "messages": [],
        "tool_calls_count": 0,
        "rewrite_count": rewrite_count,
        "graded_evidences": graded or [],
        "current_query": "test",
        "confidence_tier": tier,
    }


def test_correct_tier_goes_to_finalize() -> None:
    state = _make_state(tier="correct")
    assert after_grade_router(state) == "finalize"


def test_ambiguous_tier_goes_to_refine() -> None:
    state = _make_state(tier="ambiguous")
    assert after_grade_router(state) == "refine"


def test_incorrect_tier_goes_to_rewrite() -> None:
    state = _make_state(tier="incorrect", rewrite_count=0)
    assert after_grade_router(state) == "rewrite"


def test_incorrect_tier_exceeds_rewrite_limit_goes_to_degrade() -> None:
    # Default max_rewrite_rounds is 2, so rewrite_count=2 should trigger degrade
    state = _make_state(tier="incorrect", rewrite_count=5)
    assert after_grade_router(state) == "degrade"


def test_default_tier_is_incorrect() -> None:
    """If confidence_tier is not set, default to incorrect behavior."""
    state = {
        "messages": [],
        "tool_calls_count": 0,
        "rewrite_count": 0,
        "graded_evidences": [],
        "current_query": "test",
    }
    result = after_grade_router(state)
    assert result in ("rewrite", "degrade")
