from __future__ import annotations

from pathlib import Path

import pytest

from src.core.secrets import SecretProvider
from src.core.settings import Settings
from src.nl2sql.config.settings import AgentConfig


def test_file_value_overrides_environment_value(tmp_path: Path) -> None:
    secret_file = tmp_path / "api-key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    provider = SecretProvider({"OPENAI_API_KEY": "env-secret", "OPENAI_API_KEY_FILE": str(secret_file)})

    assert provider.get("OPENAI_API_KEY") == "file-secret"


def test_file_value_must_reference_nonempty_regular_file(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty"
    empty_file.write_text("\n", encoding="utf-8")
    provider = SecretProvider({"TOKEN_FILE": str(empty_file)})

    with pytest.raises(ValueError, match="empty secret"):
        provider.get("TOKEN")


def test_default_is_used_when_no_environment_value_exists() -> None:
    assert SecretProvider({}).get("UNSET", "fallback") == "fallback"


def test_settings_support_file_backed_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "openai-key"
    secret_file.write_text("file-backed-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret_file))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")

    settings = Settings(_env_file=None)
    assert settings.openai_api_key == "file-backed-key"


def test_agent_config_supports_file_backed_embedding_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "embedding-key"
    secret_file.write_text("embedding-file-key\n", encoding="utf-8")
    monkeypatch.setenv("EMBEDDING_API_KEY_FILE", str(secret_file))
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)

    config = AgentConfig(_env_file=None)
    assert config.embedding_api_key.get_secret_value() == "embedding-file-key"
