"""Iteration 3 R4/R5: observer/Langfuse egress policy and callback gating."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

from src.core import observer
from src.nl2sql.infra.observer import langfuse as langfuse_module
from src.nl2sql.observability import sink_policy

RAW_ENV = sink_policy.RAW_CONTENT_ENV
CONFIGURED_SECRET = "fixture-fixture-fixture"


def _settings(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        langfuse_enabled=enabled,
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_host="http://localhost",
        langfuse_timeout=30,
    )


def _span(attributes: dict[str, Any]) -> tuple[OtelSpanIdentifier, OtelSpanData]:
    trace_id, span_id = "a" * 32, "b" * 16
    return (
        OtelSpanIdentifier(trace_id=trace_id, span_id=span_id),
        OtelSpanData(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            name="generation",
            instrumentation_scope_name="langfuse",
            instrumentation_scope_version=None,
            attributes=attributes,
            resource_attributes={},
        ),
    )


# --------------------------------------------------------------------------- #
# R4: base_config metadata must not bypass the observability envelope
# --------------------------------------------------------------------------- #


def test_base_config_metadata_is_dropped_when_raw_content_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RAW_ENV, raising=False)
    monkeypatch.setattr(observer, "get_langfuse_handler", lambda: None)
    base: dict[str, Any] = {
        "metadata": {
            "note": "team A revenue 1200",
            "credential": "postgresql://svc:hunter2@db/app",
        }
    }

    config = observer.create_monitored_config(session_id="session-1", base_config=base)

    metadata = config["metadata"]
    assert metadata == {"langfuse_session_id": "session-1"}
    assert "note" not in metadata
    assert "credential" not in metadata
    assert "hunter2" not in str(metadata)
    assert "team A revenue" not in str(metadata)


def test_base_config_metadata_is_raw_gated_but_secret_scrubbed_when_raw_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RAW_ENV, "true")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", CONFIGURED_SECRET)
    monkeypatch.setattr(observer, "get_langfuse_handler", lambda: None)
    base: dict[str, Any] = {
        "metadata": {
            "note": "team A revenue 1200",
            "credential": "uses " + CONFIGURED_SECRET,
            "dsn": "postgresql://svc:hunter2@db/app",
        }
    }

    config = observer.create_monitored_config(session_id="session-1", base_config=base)

    metadata = config["metadata"]
    assert metadata["note"] == "team A revenue 1200"
    assert metadata["langfuse_session_id"] == "session-1"
    assert CONFIGURED_SECRET not in str(metadata)
    assert "hunter2" not in str(metadata)


def test_base_and_call_time_metadata_are_merged_through_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RAW_ENV, raising=False)
    monkeypatch.setattr(observer, "get_langfuse_handler", lambda: None)

    config = observer.create_monitored_config(
        session_id="session-1",
        base_config={"metadata": {"base_note": "raw base"}},
        metadata={"call_note": "raw call"},
    )

    assert config["metadata"] == {"langfuse_session_id": "session-1"}


# --------------------------------------------------------------------------- #
# R5: Langfuse callback raw-input/output closure
# --------------------------------------------------------------------------- #


def test_callback_is_attached_when_enabled_even_with_raw_content_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R5: the raw-content control lives at the EXPORT MASK (which deletes the
    # raw input/output/status_message attributes), not at attachment, so safe
    # operational telemetry is preserved by default.
    monkeypatch.delenv(RAW_ENV, raising=False)
    handler = object()
    monkeypatch.setattr(langfuse_module, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: object())
    monkeypatch.setattr(
        langfuse_module, "_new_callback_handler", lambda public_key: handler
    )

    assert langfuse_module.get_langfuse_handler() is handler


def test_callback_is_attached_when_raw_content_is_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RAW_ENV, "true")
    handler = object()
    monkeypatch.setattr(langfuse_module, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: object())
    monkeypatch.setattr(
        langfuse_module, "_new_callback_handler", lambda public_key: handler
    )

    assert langfuse_module.get_langfuse_handler() is handler


def test_disabled_langfuse_keeps_returning_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RAW_ENV, "true")
    monkeypatch.setattr(langfuse_module, "get_settings", lambda: _settings(False))

    assert langfuse_module.get_langfuse_handler() is None


def test_langfuse_client_uses_the_supported_export_stage_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def auth_check(self) -> bool:
            return True

    def _fake_langfuse(**kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(langfuse_module, "Langfuse", _fake_langfuse)
    monkeypatch.setattr(langfuse_module, "get_settings", lambda: _settings(True))
    langfuse_module._init_langfuse_client.cache_clear()
    try:
        client = langfuse_module._init_langfuse_client()
    finally:
        langfuse_module._init_langfuse_client.cache_clear()

    assert client is not None
    assert captured["mask_otel_spans"] is langfuse_module.mask_langfuse_spans


def test_pinned_langfuse_really_exposes_the_mask_otel_spans_parameter() -> None:
    # Non-tautological: inspect the REAL installed class, not a fake.
    parameters = inspect.signature(langfuse_module.Langfuse.__init__).parameters
    assert "mask_otel_spans" in parameters


def test_mask_removes_raw_prompt_and_answer_attributes_when_raw_is_off() -> None:
    identifier, span = _span(
        {
            "langfuse.observation.input": "what is revenue?",
            "langfuse.observation.output": "revenue is 1200",
            "langfuse.observation.metadata.route": "standard",
        }
    )

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        secret_values=(),
        raw_content_enabled=False,
    )

    assert result is not None
    patch = result.span_patches[identifier]
    assert set(patch.delete_attributes) == {
        "langfuse.observation.input",
        "langfuse.observation.output",
    }
    # Safe operational metadata survives the raw-content closure.
    assert "langfuse.observation.metadata.route" not in patch.set_attributes


def test_mask_keeps_safe_operational_telemetry_while_deleting_raw_content() -> None:
    identifier, span = _span(
        {
            "langfuse.observation.input": "what is revenue?",
            "langfuse.observation.output": "revenue is 1200",
            "langfuse.observation.model.name": "deepseek-v4-flash",
            "langfuse.observation.usage_details": '{"input_tokens": 10}',
        }
    )

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        secret_values=(),
        raw_content_enabled=False,
    )

    assert result is not None
    patch = result.span_patches[identifier]
    assert set(patch.delete_attributes) == {
        "langfuse.observation.input",
        "langfuse.observation.output",
    }
    # Model + usage are operational telemetry, not raw content: kept.
    assert "langfuse.observation.model.name" not in patch.set_attributes
    assert "langfuse.observation.usage_details" not in patch.set_attributes


def test_mask_preserves_ordinary_raw_business_content_when_raw_is_on() -> None:
    identifier, span = _span({"langfuse.observation.input": "what is revenue?"})

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        secret_values=(),
        raw_content_enabled=True,
    )

    # Nothing removed and nothing scrubbed: raw business input passes unchanged.
    assert result is None


@pytest.mark.parametrize("raw_enabled", [False, True])
def test_mask_never_exports_technical_secrets_under_either_toggle(
    raw_enabled: bool,
) -> None:
    identifier, span = _span(
        {
            "langfuse.observation.input": "password = 'hunter2'",
            "langfuse.observation.metadata.note": "uses " + CONFIGURED_SECRET,
            "langfuse.observation.metadata.route": "standard",
        }
    )

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        secret_values=(CONFIGURED_SECRET,),
        raw_content_enabled=raw_enabled,
    )

    assert result is not None
    patch = result.span_patches[identifier]
    assert CONFIGURED_SECRET not in str(patch.set_attributes)
    assert "hunter2" not in str(patch.set_attributes)
    assert patch.set_attributes["langfuse.observation.metadata.note"] == "uses [REDACTED]"
    assert "langfuse.observation.metadata.route" not in patch.set_attributes


@pytest.mark.parametrize("raw_enabled", [False, True])
def test_mask_scrubs_configured_secret_from_sequence_attributes(
    raw_enabled: bool,
) -> None:
    identifier, span = _span(
        {"langfuse.trace.tags": ["ordinary", "uses " + CONFIGURED_SECRET]}
    )

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        secret_values=(CONFIGURED_SECRET,),
        raw_content_enabled=raw_enabled,
    )

    assert result is not None
    patch = result.span_patches[identifier]
    assert CONFIGURED_SECRET not in str(patch.set_attributes)


# --------------------------------------------------------------------------- #
# R3: the audit-trail Langfuse egress boundary is enveloped too
# --------------------------------------------------------------------------- #


class _FakeTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(
        self, *, name: str, metadata: dict[str, Any], start_time: float | None = None
    ) -> None:
        del start_time
        self.events.append((name, metadata))


class _FakeLangfuseClient:
    """Stands in for the SDK trace/event shape used by AuditTrail.flush."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.trace_sink = _FakeTrace()

    def trace(self, **kwargs: Any) -> _FakeTrace:
        self.calls.append(kwargs)
        return self.trace_sink


def test_audit_trail_flush_drops_raw_input_and_event_payload_when_raw_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.nl2sql.infra.observer import audit_trail as audit_module

    monkeypatch.delenv(RAW_ENV, raising=False)
    fake = _FakeLangfuseClient()
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: fake)

    trail = audit_module.create_audit_trail(
        thread_id="t1", question="what is revenue?"
    )
    trail.record_sql_generation(
        "SELECT password = 'hunter2' FROM t", strategy="standard"
    )
    trail.flush()

    assert len(fake.calls) == 1
    trace_call = fake.calls[0]
    assert trace_call["input"] is None
    assert trace_call["metadata"]["total_events"] == 1
    assert "hunter2" not in str(trace_call)
    name, metadata = fake.trace_sink.events[0]
    assert name == "sql_generation.generated"
    assert metadata == {"stage": "sql_generation", "event_type": "generated"}


def test_audit_trail_flush_emits_raw_business_but_scrubs_secrets_when_raw_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.nl2sql.infra.observer import audit_trail as audit_module

    monkeypatch.setenv(RAW_ENV, "true")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", CONFIGURED_SECRET)
    fake = _FakeLangfuseClient()
    monkeypatch.setattr(langfuse_module, "_init_langfuse_client", lambda: fake)

    trail = audit_module.create_audit_trail(
        thread_id="t1", question="what is revenue?"
    )
    trail.record_sql_generation("SELECT revenue FROM t", strategy="standard")
    trail.flush()

    trace_call = fake.calls[0]
    assert trace_call["input"] == "what is revenue?"
    _, metadata = fake.trace_sink.events[0]
    assert metadata["sql"] == "SELECT revenue FROM t"
    assert metadata["strategy"] == "standard"


# --------------------------------------------------------------------------- #
# R6: the bounded configured-secret source covers EMBEDDING_API_KEY
# --------------------------------------------------------------------------- #

EMBEDDING_SECRET = "embed-embed-embed"


def test_default_secret_source_scrubs_a_bare_embedding_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R6 A: default source (no injected secret_values), bare value, raw ON.
    monkeypatch.setenv("EMBEDDING_API_KEY", EMBEDDING_SECRET)
    monkeypatch.delenv("EMBEDDING_API_KEY_FILE", raising=False)

    envelope = sink_policy.build_sink_envelope(
        sink_policy.LANGFUSE_SINK,
        metadata={"note": "value " + EMBEDDING_SECRET},
        content={"prompt": EMBEDDING_SECRET},
        raw_content_enabled=True,
    )

    record = str(envelope.as_record())
    assert EMBEDDING_SECRET not in record
    assert record.count("[REDACTED]") == 2


def test_default_secret_source_reads_the_embedding_key_file_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # R6 B: production/release uses EMBEDDING_API_KEY_FILE.
    from pathlib import Path

    secret_file = Path(str(tmp_path)) / "embedding_api_key"
    secret_file.write_text(EMBEDDING_SECRET, encoding="utf-8")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY_FILE", str(secret_file))

    envelope = sink_policy.build_sink_envelope(
        sink_policy.LANGFUSE_SINK,
        content={"prompt": "value " + EMBEDDING_SECRET},
        raw_content_enabled=True,
    )

    assert EMBEDDING_SECRET not in str(envelope.as_record())


def test_span_mask_default_source_scrubs_a_bare_embedding_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R6 C: mask_langfuse_spans with the DEFAULT secret resolution.
    monkeypatch.setenv("EMBEDDING_API_KEY", EMBEDDING_SECRET)
    monkeypatch.delenv("EMBEDDING_API_KEY_FILE", raising=False)
    identifier, span = _span(
        {"langfuse.observation.metadata.note": "value " + EMBEDDING_SECRET}
    )

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        raw_content_enabled=True,
    )

    assert result is not None
    assert EMBEDDING_SECRET not in str(
        result.span_patches[identifier].set_attributes
    )


def test_default_secret_source_scrubs_migrator_dsn_passwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R6b: the release image's migrate/ops services hold these DSNs too.
    monkeypatch.setenv(
        "CONTROL_MIGRATOR_DATABASE_URL",
        "postgresql://ctrl_mig:migrator-migrator-migrator@db:5432/x",
    )
    monkeypatch.setenv(
        "CHECKPOINT_MIGRATOR_DATABASE_URL",
        "postgresql://ckpt_mig:chkpt-chkpt-chkpt@db:5432/x",
    )

    envelope = sink_policy.build_sink_envelope(
        sink_policy.LANGFUSE_SINK,
        content={"note": "password is migrator-migrator-migrator and chkpt-chkpt-chkpt"},
        raw_content_enabled=True,
    )

    record = str(envelope.as_record())
    assert "migrator-migrator-migrator" not in record
    assert "chkpt-chkpt-chkpt" not in record


def test_settings_env_file_channel_is_scrubbed_without_os_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # R9: a credential configured through the application's supported pydantic
    # env_file channel (not os.environ, not *_FILE) must still be scrubbed.
    import src.core.settings as core_settings
    import src.nl2sql.config.settings as agent_settings
    from src.core.settings import Settings
    from src.nl2sql.config.settings import AgentConfig

    openai_secret = "openai-openai-openai"
    embedding_secret = "embeddingembedding"
    db_password = "dbpass-dbpass-dbpass"
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        f"OPENAI_API_KEY={openai_secret}\n"
        f"EMBEDDING_API_KEY={embedding_secret}\n"
        f"DATABASE_URL=postgresql://user:{db_password}@db:5432/app\n",
        encoding="utf-8",
    )
    for name in ("OPENAI_API_KEY", "EMBEDDING_API_KEY", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=env_file, auth_enabled=False)  # type: ignore[call-arg]
    agent = AgentConfig(_env_file=env_file)  # type: ignore[call-arg]
    monkeypatch.setattr(core_settings, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: agent)

    envelope = sink_policy.build_sink_envelope(
        sink_policy.LANGFUSE_SINK,
        metadata={"note": f"{openai_secret} {embedding_secret}"},
        content={"prompt": db_password},
        raw_content_enabled=True,
    )

    record = str(envelope.as_record())
    assert openai_secret not in record
    assert embedding_secret not in record
    assert db_password not in record
    # R9 E: the secret-holding fields never render their values.
    assert "secret_values" not in repr(envelope)
    isolated = sink_policy.SinkEnvelope(
        sink=sink_policy.LANGFUSE_SINK, secret_values=(openai_secret,)
    )
    assert openai_secret not in repr(isolated)

    from src.nl2sql.infra.llm.model_input_policy import calibrated_model_input_policy
    from src.nl2sql.infra.llm.profiles import ModelTarget

    policy = calibrated_model_input_policy(
        (ModelTarget("deepseek", "deepseek-v4-flash", "small"),),
        secret_values=(openai_secret,),
    )
    assert openai_secret not in repr(policy)
    assert openai_secret not in policy.checksum


def test_default_scrub_covers_configured_values_on_direct_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # I5b: every consumer of the shared primitive hard-denies the deployment's
    # own configured values, not only known secret shapes.
    from src.nl2sql.observability.content_policy import (
        contains_technical_secret,
        scan_value,
        scrub_text,
        scrub_value,
    )
    from src.nl2sql.observability.trace import TraceEnvelope

    monkeypatch.setenv("EMBEDDING_API_KEY", EMBEDDING_SECRET)
    payload = "the key is " + EMBEDDING_SECRET

    assert EMBEDDING_SECRET not in scrub_text(payload)
    assert EMBEDDING_SECRET not in str(scrub_value({"note": payload}))
    assert [finding.category for finding in scan_value({"note": payload})] == [
        "configured_secret"
    ]
    assert contains_technical_secret(payload) is True

    event = TraceEnvelope(trace_id="t1").record("answer", "observed", note=payload)
    assert EMBEDDING_SECRET not in str(event.attributes["note"])


def test_non_str_mapping_key_secret_is_scrubbed() -> None:
    from src.nl2sql.observability.content_policy import scan_value, scrub_value

    payload = {("a", EMBEDDING_SECRET): "v"}

    assert EMBEDDING_SECRET not in str(
        scrub_value(payload, secret_values=(EMBEDDING_SECRET,))
    )
    findings = scan_value(payload, secret_values=(EMBEDDING_SECRET,))
    assert [finding.category for finding in findings] == ["configured_secret"]
    assert EMBEDDING_SECRET not in findings[0].path


def test_settings_channel_covers_control_checkpoint_and_langfuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.core.settings as core_settings
    from src.core.settings import Settings
    from src.nl2sql.observability.secret_source import observability_secret_values

    langfuse_secret = "langfuse-langfuse-langfuse"
    control_password = "control-control-control"
    checkpoint_password = "chkpt-chkpt-chkpt"
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        f"LANGFUSE_SECRET_KEY={langfuse_secret}\n"
        f"CONTROL_DATABASE_URL=postgresql://c:{control_password}@db:5432/x\n"
        f"CHECKPOINT_DATABASE_URL=postgresql://k:{checkpoint_password}@db:5432/x\n",
        encoding="utf-8",
    )
    for name in (
        "LANGFUSE_SECRET_KEY",
        "CONTROL_DATABASE_URL",
        "CHECKPOINT_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=env_file, auth_enabled=False)  # type: ignore[call-arg]
    monkeypatch.setattr(core_settings, "get_settings", lambda: settings)

    values = observability_secret_values()
    assert langfuse_secret in values
    assert control_password in values
    assert checkpoint_password in values


def test_ordinary_embedding_prose_is_unchanged() -> None:
    # R6 D: no broad business DLP.
    prose = "the embedding model maps team A revenue concepts to vectors"
    envelope = sink_policy.build_sink_envelope(
        sink_policy.LANGFUSE_SINK,
        content={"note": prose},
        raw_content_enabled=True,
    )

    assert envelope.as_record()["note"] == prose


# --------------------------------------------------------------------------- #
# I5: a technical secret in a Mapping KEY is still denied/scrubbed
# --------------------------------------------------------------------------- #


def test_secret_used_as_a_mapping_key_is_scrubbed_and_detected() -> None:
    from src.nl2sql.observability.content_policy import scan_value, scrub_value

    payload = {EMBEDDING_SECRET: "note", "route": "standard"}

    assert scrub_value(payload, secret_values=(EMBEDDING_SECRET,)) == {
        "[REDACTED]": "note",
        "route": "standard",
    }
    categories = [
        finding.category
        for finding in scan_value(payload, secret_values=(EMBEDDING_SECRET,))
    ]
    assert categories == ["configured_secret"]


def test_nested_mapping_key_secret_is_scrubbed() -> None:
    from src.nl2sql.observability.content_policy import scrub_value

    assert scrub_value({"outer": {EMBEDDING_SECRET: "v"}}, secret_values=(EMBEDDING_SECRET,)) == {
        "outer": {"[REDACTED]": "v"}
    }


def test_benign_mapping_keys_are_not_scrubbed() -> None:
    from src.nl2sql.observability.content_policy import scrub_value

    assert scrub_value(
        {"password": "x", "route": "standard"}, secret_values=(EMBEDDING_SECRET,)
    ) == {"password": "x", "route": "standard"}


def test_mapping_key_secret_denies_the_model_input_policy() -> None:
    from src.nl2sql.contracts import ModelRequest
    from src.nl2sql.infra.llm.model_input_policy import calibrated_model_input_policy
    from src.nl2sql.infra.llm.profiles import ModelTarget

    target = ModelTarget("deepseek", "deepseek-v4-flash", "small")
    policy = calibrated_model_input_policy((target,), secret_values=(EMBEDDING_SECRET,))
    request = ModelRequest(
        alias="fast.default",
        stage="answer",
        messages=[{"role": "user", "content": "ok", EMBEDDING_SECRET: "leak"}],
        deadline_ms=1_000,
        token_budget=100,
        cost_budget=1,
        data_classification="internal",
        prompt_version="test-prompt-v1",
    )

    decision = policy.evaluate(target, request)

    assert decision.outcome == "deny"
    assert decision.matched_categories == ("configured_secret",)


def test_base_config_metadata_key_position_is_governed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_API_KEY", EMBEDDING_SECRET)
    monkeypatch.setenv(RAW_ENV, "true")
    monkeypatch.setattr(observer, "get_langfuse_handler", lambda: None)

    config = observer.create_monitored_config(
        session_id="s1", base_config={"metadata": {EMBEDDING_SECRET: "note"}}
    )

    assert EMBEDDING_SECRET not in str(config["metadata"])


def test_mask_recurses_into_nested_structured_attributes() -> None:
    identifier, span = _span(
        {"langfuse.trace.tags": [{"deep": EMBEDDING_SECRET}, "ok"]}
    )

    result = langfuse_module.mask_langfuse_spans(
        params=MaskOtelSpansParams(spans={identifier: span}),
        secret_values=(EMBEDDING_SECRET,),
        raw_content_enabled=True,
    )

    assert result is not None
    assert EMBEDDING_SECRET not in str(
        result.span_patches[identifier].set_attributes
    )
