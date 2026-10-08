"""Credential-shape coverage for the shared observability content policy."""

from __future__ import annotations

from typing import Any

import pytest

from src.nl2sql.observability import content_policy
from src.nl2sql.observability.content_policy import (
    REDACTED,
    contains_technical_secret,
    scan_value,
    scrub_text,
    scrub_value,
)

# Synthetic credential SHAPES, assembled at runtime.  A provider-shaped token
# stored as one literal would be caught by GitHub push protection and by the
# repository's own gitleaks rules; assembling the value keeps the production
# detection rules under test without ever storing a scannable token.
AWS_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
SLACK_TOKEN = "-".join(
    ["xoxb", "123456789012", "1234567890123", "AbCdEfGhIjKlMnOpQrStUvWx"]
)
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
LANGFUSE_SECRET_KEY = "sk-lf-" + "12345678-1234-1234-1234-123456789012"
LANGFUSE_PUBLIC_KEY = "pk-lf-" + "12345678-1234-1234-1234-123456789012"


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (AWS_ACCESS_KEY, "cloud_access_key"),
        (GITHUB_TOKEN, "vcs_access_token"),
        (SLACK_TOKEN, "messaging_token"),
        (JWT, "jwt"),
        (LANGFUSE_SECRET_KEY, "langfuse_key"),
        (LANGFUSE_PUBLIC_KEY, "langfuse_key"),
    ],
)
def test_new_credential_shapes_are_detected_and_scrubbed(value: str, category: str) -> None:
    findings = scan_value(value, secret_values=())

    assert category in {finding.category for finding in findings}
    assert contains_technical_secret(value, secret_values=()) is True

    scrubbed = scrub_text(value, secret_values=())
    assert value not in scrubbed
    assert REDACTED in scrubbed


def test_new_shapes_are_scrubbed_inside_structures() -> None:
    payload = {"note": "deploy key " + AWS_ACCESS_KEY, "route": "standard"}

    scrubbed = scrub_value(payload, secret_values=())

    assert AWS_ACCESS_KEY not in str(scrubbed)
    assert scrubbed["route"] == "standard"


def test_configured_values_still_take_precedence_over_shapes() -> None:
    findings = scan_value({"note": "key " + GITHUB_TOKEN}, secret_values=(GITHUB_TOKEN,))

    assert {finding.category for finding in findings} == {
        "configured_secret",
        "vcs_access_token",
    }


@pytest.mark.parametrize(
    "text",
    [
        "team A revenue 1200; contact 13800138000; id 110101199001011234",
        "the basic authentication flow and a basic understanding of revenue",
        "akia is not a prefix we use here, and xoxo is a greeting",
        "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "eyJhbGciOiJIUzI1NiJ9.c2hvcnQ",
        "ghp_short",
        "sk-lf-not-a-uuid",
        "AKIA1234",
    ],
)
def test_ordinary_text_is_not_a_false_positive(text: str) -> None:
    assert scan_value(text, secret_values=()) == ()
    assert scrub_text(text, secret_values=()) == text


def _nested(depth: int, leaf: Any = "leaf") -> dict[str, Any]:
    node: Any = leaf
    for _ in range(depth):
        node = {"n": node}
    return node


def test_shallow_nesting_is_untouched() -> None:
    payload = _nested(5, leaf="ordinary revenue")

    assert scrub_value(payload, secret_values=()) == payload
    assert scan_value(payload, secret_values=()) == ()


def test_deeply_nested_scrubbing_is_bounded_and_fails_closed() -> None:
    # 400 levels would exhaust the interpreter stack without the depth cap.
    result = scrub_value(_nested(400, leaf="sk-" + "x" * 40), secret_values=())

    levels = 0
    node: Any = result
    while isinstance(node, dict):
        node = node["n"]
        levels += 1

    assert levels == content_policy._MAX_NESTING_DEPTH + 1
    assert node == REDACTED


def test_deeply_nested_scanning_reports_instead_of_recursing() -> None:
    findings = scan_value(_nested(400), secret_values=())

    assert [finding.category for finding in findings] == ["nesting_depth_exceeded"]
