"""Tests for the in-process code executor sandbox (no arbitrary execution)."""

from __future__ import annotations

import pytest

from src.nl2sql.agents.dynamic_calc import code_executor
from src.nl2sql.agents.dynamic_calc.code_executor import SandboxExecutor
from src.nl2sql.config.settings import AgentConfig


def _unsafe_dev_config() -> AgentConfig:
    return AgentConfig(
        _env_file=None,
        service_mode="infra-dev",
        enable_dynamic_calc=True,
        codeact_mode="unsafe-dev",
    )


@pytest.mark.parametrize(
    "code",
    [
        "().__class__.__bases__[0].__subclasses__()",
        "type(1).__mro__",
        "[].__class__",
        "result = (1).__class__.__base__",
        "f.__globals__",
        "obj.__getattribute__('__class__')",
        "x.__dict__",
        "print('{0.__class__}'.format(value))",
    ],
)
def test_static_check_blocks_dunder_attribute_escapes(code: str) -> None:
    violation = SandboxExecutor()._static_check(code)
    assert violation is not None
    assert "禁止" in violation


def test_ast_check_alone_rejects_any_dunder_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shape-based, so it still holds with the regex layer removed and it cannot
    # be bypassed by a dunder name nobody listed.
    monkeypatch.setattr(code_executor, "_FORBIDDEN_PATTERNS", [])
    violation = SandboxExecutor()._static_check("x = (1).__class__")
    assert violation is not None
    assert "__class__" in violation
    assert SandboxExecutor()._static_check("x = obj.__unknown_dunder__") is not None


def test_static_check_allows_legitimate_calculation_code() -> None:
    code = """
import pandas as pd
df = pd.DataFrame({'a': [1, 2, 3]})
total = float(df['a'].sum())
result = {'total': total}
stats = {'rows': len(df)}
"""
    assert SandboxExecutor()._static_check(code) is None


def test_in_process_namespace_exposes_no_type_builtin() -> None:
    # type(x).__mro__ is a standard escape step, so "type" is not injected.
    with pytest.raises(NameError):
        SandboxExecutor()._run_code("result = type(1)", {})


def test_in_process_execution_still_runs_ordinary_code() -> None:
    assert SandboxExecutor()._run_code("result = sum([1, 2, 3])", {})["result"] == 6


@pytest.mark.asyncio
async def test_in_process_executor_is_fail_closed_without_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(code_executor, "get_agent_config", _unsafe_dev_config)
    monkeypatch.delenv(code_executor._ALLOW_UNLIMITED_RESOURCES_ENV, raising=False)

    result = await SandboxExecutor().execute("result = 1")

    assert result.success is False
    assert "已拒绝执行" in (result.error or "")
    assert result.resource_limits_applied is False


@pytest.mark.asyncio
async def test_in_process_executor_marks_the_explicitly_allowed_degraded_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(code_executor, "get_agent_config", _unsafe_dev_config)
    monkeypatch.setenv(code_executor._ALLOW_UNLIMITED_RESOURCES_ENV, "1")

    result = await SandboxExecutor().execute("result = 41 + 1")

    assert result.success is True
    assert result.result == 42
    assert result.resource_limits_applied is False


def test_in_process_resource_limits_are_always_reported_as_unavailable() -> None:
    with pytest.raises(code_executor.SandboxResourceLimitError):
        code_executor._apply_resource_limits(5, 128)
