"""Shared test fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never hit real services."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("AI_VIEWS_AUTO_SYNC", "false")


@pytest.fixture()
def mock_llm() -> MagicMock:
    """Return a mock LLM that can be used in place of a real model."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content="mock response"))
    llm.with_structured_output = MagicMock(return_value=llm)
    return llm
