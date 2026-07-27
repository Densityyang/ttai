"""Tests for process sandbox (static checks only, no subprocess execution in CI)."""

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
