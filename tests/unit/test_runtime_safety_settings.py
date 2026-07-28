"""Runtime safety defaults required by the PR00 delivery baseline."""

import pytest
from pydantic import ValidationError

from src.nl2sql.config.settings import AgentConfig


def test_dynamic_calculation_is_disabled_by_default() -> None:
    config = AgentConfig(_env_file=None)

    assert config.enable_dynamic_calc is False
    assert config.codeact_mode == "disabled"
    assert config.enable_experience_store is False


def test_product_profile_rejects_unsafe_development_codeact() -> None:
    with pytest.raises(ValidationError, match="unsafe-dev"):
        AgentConfig(
            _env_file=None,
            service_mode="product",
            codeact_mode="unsafe-dev",
        )


def test_infra_dev_profile_can_explicitly_enable_unsafe_development_codeact() -> None:
    config = AgentConfig(
        _env_file=None,
        service_mode="infra-dev",
        enable_dynamic_calc=True,
        codeact_mode="unsafe-dev",
    )

    assert config.codeact_mode == "unsafe-dev"
    assert config.codeact_capability() == (True, "unsafe development calculation mode is enabled")


def test_trusted_template_mode_is_explicitly_available_and_default_has_reason() -> None:
    trusted = AgentConfig(
        _env_file=None,
        enable_dynamic_calc=True,
        codeact_mode="trusted-template",
    )

    assert trusted.codeact_capability() == (True, None)
    assert AgentConfig(_env_file=None).codeact_capability() == (False, "dynamic calculation is disabled")


def test_product_profile_rejects_memory_experience_store() -> None:
    with pytest.raises(ValueError, match="ExperienceStore"):
        AgentConfig(service_mode="product", enable_experience_store=True)
