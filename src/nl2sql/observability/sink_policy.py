"""Approved observability sink registry and metadata/content envelope.

Langfuse is a deployment-approved observability sink.  It receives SAFE
metadata by default; raw prompt/answer/business content is emitted only when an
explicit deployment-level raw-content observability policy is enabled.  This is
a two-channel envelope, not a business DLP matrix.

R3: the technical-secret HARD DENY is applied at THIS egress boundary.  Even
when raw business content is explicitly enabled, a technical credential shape
or a configured system secret value is scrubbed from every field that actually
leaves, including metadata.  Ordinary authorized business content is preserved.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.nl2sql.observability.content_policy import scrub_value
from src.nl2sql.observability.secret_source import observability_secret_values

LANGFUSE_SINK = "langfuse"
RAW_CONTENT_ENV = "TTAI_OBSERVABILITY_RAW_CONTENT"
_APPROVED_SINKS = frozenset({LANGFUSE_SINK})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class SinkEnvelope:
    """A sink record split into always-safe metadata and policy-gated content."""

    sink: str
    metadata: dict[str, Any] = field(default_factory=dict)
    content: dict[str, Any] = field(default_factory=dict)
    raw_content_included: bool = False
    # The deployment's own configured secret VALUES, held only for substring
    # scrubbing.  Excluded from repr and equality so a fingerprint/log never
    # exposes them and replayable comparisons stay stable.
    secret_values: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def as_record(self) -> dict[str, Any]:
        record = dict(self.metadata)
        if self.raw_content_included:
            record.update(self.content)
        # R3 fail-closed: scrub technical secrets from EVERY exported field.
        # An ordinary authorized business payload is returned untouched.
        return scrub_value(record, secret_values=self.secret_values)


def approved_sinks() -> frozenset[str]:
    """The bounded registry of deployment-approved observability sinks."""

    return _APPROVED_SINKS


def sink_is_approved(sink: str) -> bool:
    return sink in _APPROVED_SINKS


def raw_content_observability_enabled() -> bool:
    """Read the explicit deployment-level raw-content observability policy."""

    return os.environ.get(RAW_CONTENT_ENV, "").strip().lower() in _TRUTHY


def build_sink_envelope(
    sink: str,
    *,
    metadata: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
    raw_content_enabled: bool | None = None,
    secret_values: Sequence[str] | None = None,
) -> SinkEnvelope:
    """Build a sink record; content is included only when policy allows it.

    An unapproved sink receives an empty envelope: its metadata and content are
    both dropped rather than silently emitted to an ungoverned destination.

    secret_values defaults to the deployment's bounded configured-secret source
    (src.nl2sql.observability.secret_source).  Tests and callers may inject an
    explicit bounded sequence instead.
    """

    if not sink_is_approved(sink):
        return SinkEnvelope(sink=sink, raw_content_included=False)
    enabled = (
        raw_content_observability_enabled()
        if raw_content_enabled is None
        else raw_content_enabled
    )
    resolved_secrets = (
        observability_secret_values() if secret_values is None else tuple(secret_values)
    )
    return SinkEnvelope(
        sink=sink,
        metadata=dict(metadata or {}),
        content=dict(content or {}),
        raw_content_included=enabled,
        secret_values=resolved_secrets,
    )
