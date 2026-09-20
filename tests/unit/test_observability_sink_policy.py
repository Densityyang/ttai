"""P2-S2 approved-sink registry and metadata/content envelope."""

from __future__ import annotations

from typing import Any

import pytest

from src.nl2sql.infra.observer import langfuse as langfuse_module
from src.nl2sql.observability import sink_policy
from src.nl2sql.observability.sink_policy import (
    LANGFUSE_SINK,
    SinkEnvelope,
    approved_sinks,
    build_sink_envelope,
    sink_is_approved,
)


class _FakeTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, *, name: str, metadata: dict[str, Any]) -> None:
        self.events.append((name, metadata))


class _FakeClient:
    def __init__(self) -> None:
        self.trace_sink = _FakeTrace()

    def trace(self, *, id: str) -> _FakeTrace:
        del id
        return self.trace_sink


def test_langfuse_is_the_only_approved_sink() -> None:
    assert approved_sinks() == frozenset({LANGFUSE_SINK})
    assert sink_is_approved(LANGFUSE_SINK) is True
    assert sink_is_approved("random-webhook") is False


def test_envelope_drops_content_by_default_and_keeps_metadata() -> None:
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        metadata={"route": "standard", "evidence_count": 2},
        content={"question": "raw business prompt"},
        raw_content_enabled=False,
    )

    assert envelope.raw_content_included is False
    record = envelope.as_record()
    assert record == {"route": "standard", "evidence_count": 2}
    assert "question" not in record


def test_envelope_includes_content_only_under_explicit_raw_policy() -> None:
    enabled = build_sink_envelope(
        LANGFUSE_SINK,
        metadata={"route": "standard"},
        content={"question": "raw business prompt"},
        raw_content_enabled=True,
    )
    assert enabled.raw_content_included is True
    assert enabled.as_record()["question"] == "raw business prompt"


def test_raw_content_environment_policy_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sink_policy.RAW_CONTENT_ENV, raising=False)
    assert sink_policy.raw_content_observability_enabled() is False

    monkeypatch.setenv(sink_policy.RAW_CONTENT_ENV, "true")
    assert sink_policy.raw_content_observability_enabled() is True

    monkeypatch.setenv(sink_policy.RAW_CONTENT_ENV, "off")
    assert sink_policy.raw_content_observability_enabled() is False


def test_unapproved_sink_receives_an_empty_envelope() -> None:
    envelope = build_sink_envelope(
        "random-webhook",
        metadata={"route": "standard"},
        content={"question": "raw business prompt"},
        raw_content_enabled=True,
    )

    assert envelope == SinkEnvelope(sink="random-webhook")
    assert envelope.as_record() == {}


def test_langfuse_routing_helper_is_safe_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: fake)
    monkeypatch.delenv(sink_policy.RAW_CONTENT_ENV, raising=False)

    langfuse_module.trace_routing_decision(
        "trace-1", "raw business question", "standard", "raw reason"
    )

    assert len(fake.trace_sink.events) == 1
    name, metadata = fake.trace_sink.events[0]
    assert name == "routing_decision"
    assert metadata["route"] == "standard"
    assert "question" not in metadata
    assert "reason" not in metadata


def test_langfuse_routing_helper_emits_raw_content_under_explicit_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: fake)
    monkeypatch.setenv(sink_policy.RAW_CONTENT_ENV, "true")

    langfuse_module.trace_routing_decision(
        "trace-1", "raw business question", "standard", "raw reason"
    )

    _, metadata = fake.trace_sink.events[0]
    assert metadata["question"] == "raw business question"
    assert metadata["reason"] == "raw reason"


def test_langfuse_code_execution_separates_safe_metadata_from_raw_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: fake)
    monkeypatch.delenv(sink_policy.RAW_CONTENT_ENV, raising=False)

    langfuse_module.trace_code_execution(
        "trace-1", "print('raw code')", {"success": True, "result": "raw result"}, 12.5
    )

    _, metadata = fake.trace_sink.events[0]
    assert metadata["success"] is True
    assert metadata["elapsed_ms"] == 12.5
    assert "code" not in metadata
    assert "result_preview" not in metadata


# --------------------------------------------------------------------------- #
# R3: raw observability is never a technical-secret bypass
# --------------------------------------------------------------------------- #

CONFIGURED_SECRET = "fixture-fixture-fixture"


def test_raw_content_scrubs_technical_credential_assignment() -> None:
    # R3 A: raw ON still hard-denies a technical credential assignment.
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        metadata={"route": "standard"},
        content={"note": "password = 'hunter2'"},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    record = envelope.as_record()
    assert envelope.raw_content_included is True
    assert record["route"] == "standard"
    assert "hunter2" not in str(record)
    assert record["note"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("content", "marker"),
    [
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("-----BEGIN RSA PRIVATE KEY-----", "RSA PRIVATE KEY"),
        ("postgresql://svc:hunter2@db:5432/app", "hunter2"),
    ],
)
def test_raw_content_scrubs_credential_shapes(content: str, marker: str) -> None:
    # R3 B: raw ON still hard-denies the credential SHAPES.
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"payload": content},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    assert marker not in str(envelope.as_record())


@pytest.mark.parametrize(
    "content",
    [
        '{"apiKey": "apikey-apikey-apikey"}',
        '"password": "hunter2"',
        '{"token":"abc123xyz"}',
        '{"secret": "topsecretvalue"}',
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
    ],
)
def test_raw_content_scrubs_quoted_and_basic_credential_shapes(content: str) -> None:
    # R3 B (quoted-key + Basic forms): a JSON-quoted credential key such as
    # "apiKey": "..." must not slip past the assignment shape, and Basic auth is
    # itself a technical credential.
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"payload": content},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    record = str(envelope.as_record())
    for secret in (
        "hunter2",
        "apikey-apikey-apikey",
        "abc123xyz",
        "topsecretvalue",
        "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
    ):
        assert secret not in record


def test_ordinary_prose_that_merely_contains_basic_is_not_redacted() -> None:
    # No broad business DLP: ordinary prose containing "basic" stays intact.
    prose = "the basic authentication flow and a basic understanding of revenue"
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"note": prose},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    assert envelope.as_record()["note"] == prose


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Basic YWFhYTpiYmJiYmJi",
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        '{"Authorization": "Basic YWFhYTpiYmJiYmJi"}',
        "Basic YWFhYTpiYmJiYmJi",
        "Authorization: Basic YTpi",
        "Proxy-Authorization: Basic YTpi",
    ],
)
def test_basic_auth_credentials_including_letters_only_are_scrubbed(
    content: str,
) -> None:
    # R7 A/B: a valid Basic credential can encode to letters only, and a padded
    # credential must remain detected.
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"payload": content},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    record = str(envelope.as_record())
    assert "YWFhYTpiYmJiYmJi" not in record
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in record
    # The short credential "a:b" (base64 YTpi) must also be gone.
    assert "YTpi" not in record
    assert "[REDACTED]" in record


@pytest.mark.parametrize(
    "content",
    [
        "authorization: basic rules",
        "Authorization: Basic authentication",
        "the basic authentication flow and basic revenue concepts",
        "Authorization: Basic not_base64",
        "Authorization: Basic YWJjZA==",
    ],
)
def test_basic_shaped_non_credentials_are_not_redacted(content: str) -> None:
    # R8 E/F/H: header-prefixed prose and non-decodable/no-separator tokens must
    # not be classified as a technical secret (validation, not length).
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"payload": content},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    assert envelope.as_record()["payload"] == content


def test_basic_auth_prose_and_short_header_chatter_are_not_redacted() -> None:
    # R7 C: no broad DLP.  A long ordinary word after a "basic" prefix must NOT
    # be treated as a Base64 credential (validated, not length-guessed).
    prose = "the basic authentication flow and basic revenue concepts"
    metadata = {
        "note": prose,
        "hint": "authorization: basic rules",
        "long_word": "see the authorization: basic authentication flow",
        "understanding": "authorization: basic understanding of revenue",
    }
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        metadata=metadata,
        content={"question": prose},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    record = envelope.as_record()
    assert record["note"] == prose
    assert record["hint"] == "authorization: basic rules"
    assert record["long_word"] == "see the authorization: basic authentication flow"
    assert record["understanding"] == "authorization: basic understanding of revenue"
    assert record["question"] == prose


def test_raw_content_scrubs_an_injected_configured_secret_in_content_and_metadata() -> None:
    # R3 C: a configured secret VALUE is scrubbed from every exported field.
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        metadata={"route": "standard", "note": "uses " + CONFIGURED_SECRET},
        content={"prompt": "ordinary business question"},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    record = envelope.as_record()
    assert CONFIGURED_SECRET not in str(record)
    assert record["prompt"] == "ordinary business question"
    assert record["route"] == "standard"


def test_raw_content_preserves_ordinary_business_and_pii_shaped_values() -> None:
    # R3 D: no broad business DLP; ordinary authorized content stays usable.
    business = "team A revenue 1200; contact 13800138000; id 110101199001011234"
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"note": business},
        raw_content_enabled=True,
        secret_values=(CONFIGURED_SECRET,),
    )

    assert envelope.as_record()["note"] == business


def test_safe_metadata_default_is_unchanged_when_raw_is_off() -> None:
    # R3 E: the safe-metadata-only default remains intact.
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        metadata={"route": "standard"},
        content={"question": "raw business prompt"},
        raw_content_enabled=False,
        secret_values=(CONFIGURED_SECRET,),
    )

    assert envelope.raw_content_included is False
    assert envelope.as_record() == {"route": "standard"}


def test_metadata_channel_is_scrubbed_even_when_raw_content_is_off() -> None:
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        metadata={"route": "standard", "db": "postgresql://svc:hunter2@db/app"},
        raw_content_enabled=False,
        secret_values=(CONFIGURED_SECRET,),
    )

    record = envelope.as_record()
    assert record["route"] == "standard"
    assert "hunter2" not in str(record)


def test_default_secret_source_scrubs_a_configured_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bounded deployment secret source is wired in when none is injected.
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", CONFIGURED_SECRET)
    envelope = build_sink_envelope(
        LANGFUSE_SINK,
        content={"note": "value " + CONFIGURED_SECRET},
        raw_content_enabled=True,
    )

    assert CONFIGURED_SECRET not in str(envelope.as_record())
