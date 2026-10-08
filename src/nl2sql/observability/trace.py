"""Unified request trace schema shared by runtime and benchmarks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.nl2sql.observability.content_policy import scrub_text, scrub_value

TraceStage = Literal["query", "retrieval", "candidate", "policy", "sql", "answer"]
_SENSITIVE_KEYS = frozenset({"authorization", "password", "secret", "token", "api_key", "prompt", "rows"})
_SAFE_PROMPT_METADATA = frozenset({"prompt_hash", "prompt_version"})
# Derived credential field names that stay redacted although the key is not
# an exact match.  This is an explicit allowlist, not a substring rule:
# matching substrings redacted legitimate telemetry such as
# authorization_revision, token_cost, input_tokens or secret_version.
_SENSITIVE_KEY_SUFFIXES = ("_authorization", "_password", "_secret", "_token", "_api_key")


class _TraceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TraceEvent(_TraceModel):
    trace_id: str = Field(min_length=1, max_length=256)
    stage: TraceStage
    name: str = Field(min_length=1, max_length=128)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceEnvelope(_TraceModel):
    trace_id: str = Field(min_length=1, max_length=256)
    events: list[TraceEvent] = Field(default_factory=list)

    def record(self, stage: TraceStage, name: str, **attributes: Any) -> TraceEvent:
        event = TraceEvent(
            trace_id=self.trace_id,
            stage=stage,
            name=name,
            attributes=_sanitize_attributes(attributes),
        )
        self.events.append(event)
        return event


def fingerprint(value: object) -> str:
    """Return a stable digest so audit/report data never requires raw payloads."""
    payload = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sanitize_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        normalized = key.lower().replace("-", "_")
        if normalized in _SAFE_PROMPT_METADATA and isinstance(value, str):
            safe[key] = value[:512]
        elif normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES):
            safe[key] = "[REDACTED]"
        elif isinstance(value, str):
            safe[key] = scrub_text(value)[:512]
        else:
            # Value-level scrub so a technical secret nested inside an
            # otherwise safe attribute can never reach an audit sink.
            safe[key] = scrub_value(value)
    return safe
