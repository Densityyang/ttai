"""Value-level technical-secret scanner and scrubber shared by every egress sink.

P2-S2 recognises ONLY technical credential shapes and the deployment's own
configured secret VALUES.  It is deliberately NOT a business-field DLP matrix:
ordinary authorised business content -- including values that merely look like
PII -- is returned untouched.  The same primitive is reused by model input,
embedding input, audit records and checkpoint payloads.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from src.nl2sql.observability.secret_source import (
    NON_SECRET_SENTINELS,
    observability_secret_values,
)

REDACTED = "[REDACTED]"

SecretCategory = Literal[
    "configured_secret",
    "bearer_token",
    "basic_auth",
    "private_key",
    "credential_dsn",
    "credential_assignment",
    "cloud_access_key",
    "vcs_access_token",
    "messaging_token",
    "jwt",
    "langfuse_key",
    "nesting_depth_exceeded",
]

CONFIGURED_SECRET: SecretCategory = "configured_secret"
BEARER_TOKEN: SecretCategory = "bearer_token"
BASIC_AUTH: SecretCategory = "basic_auth"
PRIVATE_KEY: SecretCategory = "private_key"
CREDENTIAL_DSN: SecretCategory = "credential_dsn"
CREDENTIAL_ASSIGNMENT: SecretCategory = "credential_assignment"
CLOUD_ACCESS_KEY: SecretCategory = "cloud_access_key"
VCS_ACCESS_TOKEN: SecretCategory = "vcs_access_token"
MESSAGING_TOKEN: SecretCategory = "messaging_token"
JWT: SecretCategory = "jwt"
LANGFUSE_KEY: SecretCategory = "langfuse_key"
# Not a credential shape: a structure too deep to walk, reported instead of
# silently passing unverified content through.
NESTING_DEPTH_EXCEEDED: SecretCategory = "nesting_depth_exceeded"

# Layer 2: secret SHAPES.  Each entry is intentionally anchored to a technical
# credential form; none of them attempts to classify business data.
_SHAPE_PATTERNS: tuple[tuple[SecretCategory, re.Pattern[str]], ...] = (
    (
        PRIVATE_KEY,
        re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----"),
    ),
    (
        BEARER_TOKEN,
        re.compile(r"\bbearer\s+[A-Za-z0-9\-._~+/]{8,}=*", re.IGNORECASE),
    ),
    (
        CREDENTIAL_DSN,
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^:@\s/]+:[^@\s/]+@", re.IGNORECASE),
    ),
    (
        CREDENTIAL_ASSIGNMENT,
        re.compile(
            r"(?<![A-Za-z0-9_])"
            r"(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey)"
            r"(?![A-Za-z0-9_])[\"']?\s*[:=]\s*[\"']?[^\s\"']{1,}[\"']?",
            re.IGNORECASE,
        ),
    ),
    (
        # AWS access key IDs: the AKIA/ASIA prefix plus 16 upper-case
        # base32-ish characters.  Ordinary prose cannot produce that shape.
        CLOUD_ACCESS_KEY,
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        # GitHub personal/oauth/user/server/refresh tokens.
        VCS_ACCESS_TOKEN,
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ),
    (
        # Slack bot/app/user/refresh/workflow tokens.
        MESSAGING_TOKEN,
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ),
    (
        # A JWT is three dot-separated base64url segments.  The per-segment
        # minimum length plus the "eyJ" header prefix keeps ordinary long
        # base64 blobs out of the shape.
        JWT,
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    (
        # Langfuse public/secret API keys: "pk-lf-"/"sk-lf-" plus a UUID.
        LANGFUSE_KEY,
        re.compile(
            r"\b(?:pk|sk)-lf-[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}"
            r"-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b"
        ),
    ),
)

# A value nested deeper than this is not walked further: unbounded recursion on
# attacker-shaped input would exhaust the interpreter stack.  The cap is far
# above any legitimate observability payload.
_MAX_NESTING_DEPTH = 64

# Basic auth is VALIDATED, not length-guessed: a candidate token must be
# syntactically valid Base64 that decodes to "user:password".  This catches an
# all-letters credential token (e.g. "Basic YWFhYTpiYmJiYmJi") while ordinary
# prose after a "basic" prefix ("...basic authentication flow") is left intact,
# so the shape can never become broad business DLP.
_BASIC_AUTH_CANDIDATE = re.compile(
    r"\bbasic\s+([A-Za-z0-9+/]{4,}={0,2})", re.IGNORECASE
)


@dataclass(frozen=True)
class SecretFinding:
    """One matched secret category at one structural path; never a raw value."""

    category: SecretCategory
    path: str


def scan_value(
    value: Any, *, secret_values: Sequence[str] | None = None
) -> tuple[SecretFinding, ...]:
    """Return every technical-secret match inside an arbitrarily nested value.

    When secret_values is omitted it defaults to the bounded deployment
    configured-secret source, so a caller that does not inject its own values
    still hard-denies the deployment's own credentials, not only known shapes.
    """

    findings: list[SecretFinding] = []
    _walk(
        value,
        "$",
        _usable_secret_values(_resolved_secret_values(secret_values)),
        findings,
        0,
    )
    return tuple(findings)


def contains_technical_secret(
    value: Any, *, secret_values: Sequence[str] | None = None
) -> bool:
    """True when any string carries a configured value or a secret shape."""

    return bool(scan_value(value, secret_values=secret_values))


def scan_embedding_input(
    texts: Sequence[str], *, secret_values: Sequence[str] | None = None
) -> tuple[SecretFinding, ...]:
    """Scan the exact strings an embedding provider would receive."""

    return scan_value(list(texts), secret_values=secret_values)


def scrub_value(
    value: Any,
    *,
    secret_values: Sequence[str] | None = None,
    replacement: str = REDACTED,
) -> Any:
    """Return a structurally identical value with every technical secret replaced.

    Ordinary business content -- including PII-shaped business fields -- is
    preserved; only configured values and the recognised technical-secret shapes
    are replaced.  Both mapping values AND mapping keys are scrubbed.  A subtree
    nested deeper than _MAX_NESTING_DEPTH is replaced wholesale instead of being
    recursed into.
    """

    secrets = _usable_secret_values(_resolved_secret_values(secret_values))
    return _scrub_value(value, secrets, replacement, 0)


def _scrub_value(
    value: Any,
    secrets: tuple[str, ...],
    replacement: str,
    depth: int,
) -> Any:
    if depth > _MAX_NESTING_DEPTH:
        # Fail closed: content too deep to inspect is never emitted as-is.
        return replacement
    if isinstance(value, str):
        return _scrub_text(value, secrets, replacement)
    if isinstance(value, BaseModel):
        return _scrub_value(
            value.model_dump(mode="json"), secrets, replacement, depth + 1
        )
    if isinstance(value, Mapping):
        # Mapping KEYS are scrubbed too: a secret used as a key is still a
        # technical secret that must not leave the boundary.
        return {
            _scrub_mapping_key(key, secrets, replacement): _scrub_value(
                item, secrets, replacement, depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(
            _scrub_value(item, secrets, replacement, depth + 1) for item in value
        )
    if isinstance(value, list):
        return [_scrub_value(item, secrets, replacement, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return type(value)(
            _scrub_value(item, secrets, replacement, depth + 1) for item in value
        )
    return value


def scrub_text(
    text: str,
    *,
    secret_values: Sequence[str] | None = None,
    replacement: str = REDACTED,
) -> str:
    """Scrub a single string; the string-level half of scrub_value."""

    return _scrub_text(
        text, _usable_secret_values(_resolved_secret_values(secret_values)), replacement
    )


def _resolved_secret_values(secret_values: Sequence[str] | None) -> tuple[str, ...]:
    """Configured values default to the bounded deployment secret source."""

    if secret_values is None:
        return observability_secret_values()
    return tuple(secret_values)


def _usable_secret_values(secret_values: Sequence[str]) -> tuple[str, ...]:
    # A real configured value is a secret regardless of length (R11); only
    # empty strings and known non-secret placeholders are ignored.
    return tuple(
        value
        for value in secret_values
        if isinstance(value, str) and value and value not in NON_SECRET_SENTINELS
    )


def deployment_secret_values(
    extras: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Bounded deployment secrets unioned with caller-supplied extras (R10).

    An explicit policy may ADD secret values but must never replace or disable
    the deployment's canonical bounded source.
    """

    values = list(observability_secret_values())
    if extras:
        values.extend(extras)
    return tuple(dict.fromkeys(value for value in values if value))


def _walk(
    value: Any,
    path: str,
    secrets: tuple[str, ...],
    findings: list[SecretFinding],
    depth: int,
) -> None:
    if depth > _MAX_NESTING_DEPTH:
        # Too deep to verify: report it instead of walking on unverified.
        findings.append(SecretFinding(category=NESTING_DEPTH_EXCEEDED, path=path))
        return
    if isinstance(value, str):
        for category in _match_categories(value, secrets):
            findings.append(SecretFinding(category=category, path=path))
        return
    if isinstance(value, BaseModel):
        _walk(value.model_dump(mode="json"), path, secrets, findings, depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            # Non-str keys are scanned via their text form too, and a
            # secret-bearing key is NEVER echoed into the finding path.
            key_text = key if isinstance(key, str) else str(key)
            key_categories = _match_categories(key_text, secrets)
            child_path = (
                path + ".<redacted>" if key_categories else path + "." + key_text
            )
            for category in key_categories:
                findings.append(SecretFinding(category=category, path=child_path))
            _walk(item, child_path, secrets, findings, depth + 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _walk(item, path + "[" + str(index) + "]", secrets, findings, depth + 1)
        return


def _match_categories(text: str, secrets: tuple[str, ...]) -> tuple[SecretCategory, ...]:
    matched: list[SecretCategory] = []
    if any(secret in text for secret in secrets):
        matched.append(CONFIGURED_SECRET)
    for category, pattern in _SHAPE_PATTERNS:
        if pattern.search(text):
            matched.append(category)
    if _has_basic_credential(text):
        matched.append(BASIC_AUTH)
    return tuple(matched)


def _has_basic_credential(text: str) -> bool:
    return any(
        _is_basic_credential(match.group(1))
        for match in _BASIC_AUTH_CANDIDATE.finditer(text)
    )


def _is_basic_credential(token: str) -> bool:
    """True when a token is valid Base64 that decodes to "user:password"."""

    if len(token) % 4 != 0:
        return False
    try:
        decoded = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        return False
    user, separator, password = decoded.partition(b":")
    return bool(separator) and bool(user) and bool(password)


def _scrub_mapping_key(key: Any, secrets: tuple[str, ...], replacement: str) -> Any:
    if isinstance(key, str):
        return _scrub_text(key, secrets, replacement)
    key_text = str(key)
    scrubbed = _scrub_text(key_text, secrets, replacement)
    # Preserve the original key type only when it carries no secret.
    return scrubbed if scrubbed != key_text else key


def _scrub_text(text: str, secrets: tuple[str, ...], replacement: str) -> str:
    scrubbed = text
    for secret in secrets:
        scrubbed = scrubbed.replace(secret, replacement)
    for _, pattern in _SHAPE_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    scrubbed = _BASIC_AUTH_CANDIDATE.sub(
        lambda match: (
            replacement if _is_basic_credential(match.group(1)) else match.group(0)
        ),
        scrubbed,
    )
    return scrubbed
