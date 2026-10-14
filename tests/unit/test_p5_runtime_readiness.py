from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.nl2sql.config.settings import AgentConfig
from src.nl2sql.infra.runtime import registry
from src.nl2sql.orchestration.p5_runtime_readiness import (
    P5EvidenceStatus,
    P5ExecutionMode,
    P5ReadinessReason,
    assess_p5_runtime_readiness,
)


def test_default_profile_is_disabled_and_external_evidence_fails_closed() -> None:
    readiness = assess_p5_runtime_readiness(AgentConfig(_env_file=None))

    assert readiness.execution_mode is P5ExecutionMode.DISABLED
    assert readiness.os_isolation_evidence is P5EvidenceStatus.NOT_VERIFIED
    assert readiness.governed_broker_evidence is P5EvidenceStatus.NOT_VERIFIED
    assert readiness.production_arbitrary_code_ready is False
    assert P5ReadinessReason.DYNAMIC_CALCULATION_DISABLED in readiness.reason_codes


def test_product_profile_still_rejects_unsafe_dev() -> None:
    with pytest.raises(ValueError, match="unsafe-dev"):
        AgentConfig(
            _env_file=None,
            service_mode="product",
            enable_dynamic_calc=True,
            codeact_mode="unsafe-dev",
        )


def test_infra_dev_unsafe_mode_remains_development_only() -> None:
    config = AgentConfig(
        _env_file=None,
        service_mode="infra-dev",
        enable_dynamic_calc=True,
        codeact_mode="unsafe-dev",
    )

    readiness = assess_p5_runtime_readiness(config)

    assert readiness.execution_mode is P5ExecutionMode.INFRA_DEV_UNSAFE
    assert readiness.unsafe_dev_permitted is True
    assert readiness.trusted_template_permitted is False
    assert readiness.production_arbitrary_code_ready is False
    assert (
        P5ReadinessReason.UNSAFE_DEV_RESTRICTED_TO_INFRA_DEV
        in readiness.reason_codes
    )


def test_trusted_template_permission_does_not_imply_arbitrary_code_readiness() -> None:
    config = AgentConfig(
        _env_file=None,
        enable_dynamic_calc=True,
        codeact_mode="trusted-template",
    )

    readiness = assess_p5_runtime_readiness(config)

    assert readiness.execution_mode is P5ExecutionMode.TRUSTED_TEMPLATE
    assert readiness.trusted_template_permitted is True
    assert readiness.unsafe_dev_permitted is False
    assert readiness.production_arbitrary_code_ready is False


def test_missing_isolation_and_governed_broker_evidence_are_not_ready() -> None:
    readiness = assess_p5_runtime_readiness(
        AgentConfig(
            _env_file=None,
            enable_dynamic_calc=True,
            codeact_mode="trusted-template",
        )
    )

    assert readiness.os_isolation_evidence is P5EvidenceStatus.NOT_VERIFIED
    assert readiness.governed_broker_evidence is P5EvidenceStatus.NOT_VERIFIED
    assert readiness.production_arbitrary_code_ready is False
    assert P5ReadinessReason.OS_ISOLATION_NOT_VERIFIED in readiness.reason_codes
    assert P5ReadinessReason.GOVERNED_BROKER_NOT_VERIFIED in readiness.reason_codes


def test_unlimited_resource_override_does_not_establish_readiness(monkeypatch) -> None:
    monkeypatch.setenv("TTAI_SANDBOX_ALLOW_UNLIMITED_RESOURCES", "true")
    config = AgentConfig(
        _env_file=None,
        service_mode="infra-dev",
        enable_dynamic_calc=True,
        codeact_mode="unsafe-dev",
    )

    readiness = assess_p5_runtime_readiness(config)

    assert readiness.os_isolation_evidence is P5EvidenceStatus.NOT_VERIFIED
    assert readiness.production_arbitrary_code_ready is False


def test_infra_dev_permission_does_not_attest_os_isolation() -> None:
    config = AgentConfig(
        _env_file=None,
        service_mode="infra-dev",
        enable_dynamic_calc=True,
        codeact_mode="unsafe-dev",
    )

    readiness = assess_p5_runtime_readiness(config)

    assert readiness.execution_mode is P5ExecutionMode.INFRA_DEV_UNSAFE
    assert readiness.os_isolation_evidence is P5EvidenceStatus.NOT_VERIFIED
    assert readiness.production_arbitrary_code_ready is False


def test_this_slice_exposes_no_isolation_or_broker_attestation_state() -> None:
    assert tuple(P5EvidenceStatus) == (P5EvidenceStatus.NOT_VERIFIED,)


def test_readiness_reasons_never_expose_configured_paths_or_credentials() -> None:
    credential = "readiness-secret-canary"
    path = "readiness-private-path-canary"
    config = AgentConfig(
        _env_file=None,
        embedding_api_key=credential,
        ai_views_config_path=path,
    )

    readiness = assess_p5_runtime_readiness(config)

    rendered = repr(readiness)
    assert credential not in rendered
    assert path not in rendered
    assert all(isinstance(reason, P5ReadinessReason) for reason in readiness.reason_codes)


@pytest.mark.asyncio
async def test_disabled_warmup_does_not_construct_calculation_graphs(monkeypatch) -> None:
    config = AgentConfig(_env_file=None, enable_graph_rag=False)
    monkeypatch.setattr(registry, "get_agent_config", lambda: config)
    semantic = AsyncMock()
    dynamic_calc = AsyncMock()
    codeact = AsyncMock()
    monkeypatch.setattr(registry, "get_or_create_semantic_sql_graph", semantic)
    monkeypatch.setattr(registry, "get_or_create_dynamic_calc_graph", dynamic_calc)
    monkeypatch.setattr(registry, "get_or_create_codeact_graph", codeact)

    await registry.warmup_runtime()

    semantic.assert_awaited_once_with()
    dynamic_calc.assert_not_awaited()
    codeact.assert_not_awaited()


@pytest.mark.asyncio
async def test_trusted_template_warmup_keeps_existing_template_path(monkeypatch) -> None:
    config = AgentConfig(
        _env_file=None,
        enable_dynamic_calc=True,
        enable_graph_rag=False,
        codeact_mode="trusted-template",
    )
    monkeypatch.setattr(registry, "get_agent_config", lambda: config)
    semantic = AsyncMock()
    dynamic_calc = AsyncMock()
    codeact = AsyncMock()
    monkeypatch.setattr(registry, "get_or_create_semantic_sql_graph", semantic)
    monkeypatch.setattr(registry, "get_or_create_dynamic_calc_graph", dynamic_calc)
    monkeypatch.setattr(registry, "get_or_create_codeact_graph", codeact)

    await registry.warmup_runtime()

    semantic.assert_awaited_once_with()
    dynamic_calc.assert_awaited_once_with()
    codeact.assert_not_awaited()


@pytest.mark.asyncio
async def test_infra_dev_unsafe_warmup_keeps_existing_codeact_path(monkeypatch) -> None:
    config = AgentConfig(
        _env_file=None,
        service_mode="infra-dev",
        enable_dynamic_calc=True,
        enable_graph_rag=False,
        codeact_mode="unsafe-dev",
    )
    monkeypatch.setattr(registry, "get_agent_config", lambda: config)
    semantic = AsyncMock()
    dynamic_calc = AsyncMock()
    codeact = AsyncMock()
    monkeypatch.setattr(registry, "get_or_create_semantic_sql_graph", semantic)
    monkeypatch.setattr(registry, "get_or_create_dynamic_calc_graph", dynamic_calc)
    monkeypatch.setattr(registry, "get_or_create_codeact_graph", codeact)

    await registry.warmup_runtime()

    semantic.assert_awaited_once_with()
    dynamic_calc.assert_not_awaited()
    codeact.assert_awaited_once_with()
