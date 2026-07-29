from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.secrets import SecretProvider
from src.core.settings import Settings
from src.nl2sql.config.settings import AgentConfig
from src.nl2sql.infra.llm.gateway import model_gateway_available


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


def test_whitespace_only_file_is_rejected(tmp_path: Path) -> None:
    secret_file = tmp_path / "whitespace"
    secret_file.write_text("   \n", encoding="utf-8")

    with pytest.raises(ValueError, match="empty secret"):
        SecretProvider({"TOKEN_FILE": str(secret_file)}).get("TOKEN")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not authoritative on Windows")
def test_group_writable_secret_file_is_rejected(tmp_path: Path) -> None:
    secret_file = tmp_path / "writable"
    secret_file.write_text("secret", encoding="utf-8")
    secret_file.chmod(0o660)

    with pytest.raises(ValueError, match="must not be writable"):
        SecretProvider({"TOKEN_FILE": str(secret_file)}).get("TOKEN")


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


def test_cors_wildcard_with_credentials_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")

    with pytest.raises(ValueError, match="CORS wildcard"):
        Settings(_env_file=None)


def test_invalid_model_secret_file_fails_capability_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    assert model_gateway_available() is False
