"""Tests for process sandbox (static checks only, no subprocess execution in CI)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from src.nl2sql.agents.codeact_engine import process_sandbox
from src.nl2sql.agents.codeact_engine.process_sandbox import ProcessSandbox


def test_static_check_blocks_os_import() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("import os\nos.system('rm -rf /')")
    assert violation is not None
    assert "禁止" in violation


def test_static_check_blocks_eval() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("x = eval('1+1')")
    assert violation is not None


def test_static_check_blocks_open() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("f = open('/etc/passwd')")
    assert violation is not None


def test_static_check_blocks_class_def() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("class Foo:\n    pass")
    assert violation is not None
    assert "ClassDef" in violation


def test_static_check_allows_safe_code() -> None:
    sandbox = ProcessSandbox()
    code = """
import pandas as pd
import numpy as np
df = pd.DataFrame({'a': [1, 2, 3]})
result = df['a'].sum()
stats = {'total': result}
"""
    violation = sandbox._static_check(code)
    assert violation is None


def test_static_check_blocks_subprocess() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("import subprocess\nsubprocess.run(['ls'])")
    assert violation is not None


def test_static_check_blocks_dunder_import() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("m = __import__('os')")
    assert violation is not None


def test_static_check_blocks_disallowed_module() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("import requests\nrequests.get('http://evil.com')")
    assert violation is not None


def test_static_check_syntax_error() -> None:
    sandbox = ProcessSandbox()
    violation = sandbox._static_check("def foo(\n")
    assert violation is not None
    assert "语法错误" in violation


# --------------------------------------------------------------------------- #
# Object-graph escapes: dunder attribute chains must be rejected
# --------------------------------------------------------------------------- #


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
        "cls.__init_subclass__",
        "o.__reduce_ex__(2)",
        "f'{value.__class__}'",
        "print('{0.__class__}'.format(value))",
    ],
)
def test_static_check_blocks_dunder_attribute_escapes(code: str) -> None:
    violation = ProcessSandbox()._static_check(code)
    assert violation is not None
    assert "禁止" in violation


def test_ast_check_alone_rejects_any_dunder_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    # The AST rule is shape-based, so it still holds with the regex layer removed
    # and it cannot be bypassed by a dunder name nobody listed.
    monkeypatch.setattr(process_sandbox, "_FORBIDDEN_PATTERNS", [])
    violation = ProcessSandbox()._static_check("x = (1).__class__")
    assert violation is not None
    assert "__class__" in violation
    assert ProcessSandbox()._static_check("x = obj.__unknown_dunder__") is not None


def test_dunder_attribute_pattern_matches_the_shape_not_a_name_list() -> None:
    assert process_sandbox._DUNDER_ATTRIBUTE.match("__class__")
    assert process_sandbox._DUNDER_ATTRIBUTE.match("__anything_at_all__")
    assert not process_sandbox._DUNDER_ATTRIBUTE.match("_private")
    assert not process_sandbox._DUNDER_ATTRIBUTE.match("sum")


def test_static_check_allows_legitimate_calculation_code() -> None:
    code = """
import pandas as pd
df = pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})
total = float(df['a'].sum())
mean = float(df['b'].mean())
result = {'total': total, 'mean': mean}
stats = {'rows': len(df)}
"""
    assert ProcessSandbox()._static_check(code) is None


# --------------------------------------------------------------------------- #
# Resource limits are fail-closed, never a silent pass
# --------------------------------------------------------------------------- #


class _RecordingQueue:
    """Stand-in for the multiprocessing queue used by the sandbox worker."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put(self, item: dict[str, Any]) -> None:
        self.items.append(item)


def _unavailable_limits(cpu_seconds: int, memory_mb: int) -> None:
    raise process_sandbox.SandboxResourceLimitError("resource limits unavailable")


def _deny_setrlimit(*args: Any) -> None:
    del args
    raise OSError("operation not permitted")


def test_missing_resource_module_is_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # The resource module is forced to be unimportable without touching real
    # rlimits of the test process.
    monkeypatch.setitem(sys.modules, "resource", None)
    with pytest.raises(process_sandbox.SandboxResourceLimitError):
        process_sandbox._apply_resource_limits(5, 128)


def test_kernel_refusal_to_set_limits_is_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_resource = types.SimpleNamespace(
        RLIMIT_CPU=0,
        RLIMIT_AS=1,
        RLIMIT_NPROC=2,
        setrlimit=_deny_setrlimit,
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    with pytest.raises(process_sandbox.SandboxResourceLimitError):
        process_sandbox._apply_resource_limits(5, 128)


def test_worker_refuses_execution_without_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_sandbox, "_apply_resource_limits", _unavailable_limits)
    monkeypatch.delenv(process_sandbox._ALLOW_UNLIMITED_RESOURCES_ENV, raising=False)
    queue = _RecordingQueue()

    process_sandbox._sandbox_worker("result = 1", {}, [], 5, 128, queue)

    payload = queue.items[0]
    assert payload["success"] is False
    assert "已拒绝执行" in payload["error"]
    assert payload["resource_limits_applied"] is False


def test_worker_marks_a_degraded_run_when_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_sandbox, "_apply_resource_limits", _unavailable_limits)
    monkeypatch.setenv(process_sandbox._ALLOW_UNLIMITED_RESOURCES_ENV, "1")
    queue = _RecordingQueue()

    process_sandbox._sandbox_worker("result = 41 + 1", {}, [], 5, 128, queue)

    payload = queue.items[0]
    assert payload["success"] is True
    assert payload["result"] == 42
    assert payload["resource_limits_applied"] is False


def test_worker_namespace_exposes_no_type_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_sandbox, "_apply_resource_limits", _unavailable_limits)
    monkeypatch.setenv(process_sandbox._ALLOW_UNLIMITED_RESOURCES_ENV, "1")
    queue = _RecordingQueue()

    process_sandbox._sandbox_worker("result = type(1)", {}, [], 5, 128, queue)

    payload = queue.items[0]
    assert payload["success"] is False
    assert "NameError" in payload["error"]


def test_unlimited_resources_require_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(process_sandbox._ALLOW_UNLIMITED_RESOURCES_ENV, raising=False)
    assert process_sandbox._unlimited_resources_allowed() is False

    monkeypatch.setenv(process_sandbox._ALLOW_UNLIMITED_RESOURCES_ENV, "on")
    assert process_sandbox._unlimited_resources_allowed() is True

    monkeypatch.setenv(process_sandbox._ALLOW_UNLIMITED_RESOURCES_ENV, "no")
    assert process_sandbox._unlimited_resources_allowed() is False
