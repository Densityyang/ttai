"""Guards for the repository secret-scanning rules (.gitleaks.toml)."""

from __future__ import annotations

import math
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

_CONFIG_PATH = Path(__file__).resolve().parents[2] / ".gitleaks.toml"
CANARY = "TT_AI_SECRET_CANARY_" + "A" * 32


def _config() -> dict[str, Any]:
    with _CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in _config()["rules"]}


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    return -sum(
        (value.count(char) / len(value)) * math.log2(value.count(char) / len(value))
        for char in set(value)
    )


def _matches(rule: dict[str, Any], text: str) -> bool:
    """Mirror gitleaks evaluation: regex -> secretGroup -> entropy -> allowlist."""

    for match in re.finditer(rule["regex"], text):
        secret = match.group(rule.get("secretGroup", 0)) or match.group(0)
        threshold = rule.get("entropy")
        if threshold is not None and _shannon_entropy(secret) <= threshold:
            continue
        allowlisted = rule.get("allowlist", {}).get("regexes", [])
        if any(re.search(pattern, secret) for pattern in allowlisted):
            continue
        return True
    return False


def test_default_rules_are_still_extended_and_the_canary_survives() -> None:
    assert _config()["extend"]["useDefault"] is True
    assert _matches(_rules()["tt-ai-secret-canary"], CANARY) is True


def test_the_requested_rules_are_registered() -> None:
    assert {
        "tt-ai-postgres-dsn-credentials",
        "tt-ai-langfuse-key",
        "tt-ai-jwt",
    } <= set(_rules())


@pytest.mark.parametrize(
    "rule_id",
    ["tt-ai-postgres-dsn-credentials", "tt-ai-langfuse-key", "tt-ai-jwt"],
)
def test_new_rules_do_not_match_the_canary(rule_id: str) -> None:
    assert _matches(_rules()[rule_id], CANARY) is False


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:password@db:5432/app",
        "postgresql+asyncpg://business_reader:test@db/app",
        "postgresql://checkpoint_app:secret@db/checkpoint",
        "postgresql://c:{control_password}@db:5432/x",
        "postgresql://user:***@db/app",
    ],
)
def test_postgres_rule_ignores_placeholders(dsn: str) -> None:
    assert _matches(_rules()["tt-ai-postgres-dsn-credentials"], dsn) is False


def test_postgres_rule_still_catches_a_real_password() -> None:
    dsn = "postgresql://svc:9fK2mQ7pXv4Lw8Za@db:5432/app"

    assert _matches(_rules()["tt-ai-postgres-dsn-credentials"], dsn) is True


@pytest.mark.parametrize(
    "text",
    [
        "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=",
        "eyJhbGciOiJIUzI1NiJ9.c2hvcnQ",
    ],
)
def test_jwt_rule_ignores_ordinary_base64(text: str) -> None:
    assert _matches(_rules()["tt-ai-jwt"], text) is False


def test_jwt_rule_catches_a_real_token() -> None:
    token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )

    assert _matches(_rules()["tt-ai-jwt"], token) is True


def test_langfuse_rule_catches_both_key_kinds() -> None:
    rule = _rules()["tt-ai-langfuse-key"]
    # Assembled so the repository text holds no provider-scannable token.
    key_suffix = "12345678-1234-1234-1234-123456789012"

    assert _matches(rule, "sk-lf-" + key_suffix) is True
    assert _matches(rule, "pk-lf-" + key_suffix) is True
    assert _matches(rule, "sk-lf-not-a-uuid") is False
