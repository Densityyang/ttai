"""Unified request trace schema shared by runtime and benchmarks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TraceStage = Literal["query", "retrieval", "candidate", "policy", "sql", "answer"]
_SENSITIVE_KEYS = frozenset({"authorization", "password", "secret", "token", "api_key", "prompt", "rows"})


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
        if normalized in _SENSITIVE_KEYS or any(part in normalized for part in _SENSITIVE_KEYS):
            safe[key] = "[REDACTED]"
        elif isinstance(value, str):
            safe[key] = value[:512]
        else:
            safe[key] = value
    return safe
