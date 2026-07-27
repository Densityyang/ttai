"""受限 Python 代码执行器 -- CodeAct 范式的关键组件。

通过 AST 白名单 + 受限 globals + 超时控制实现进程内沙箱，
无需 Docker 依赖，适合单机部署场景。
"""

import ast
import asyncio
import logging
import re
import time
from typing import Any

from src.nl2sql.agents.dynamic_calc.schemas import SandboxResult
from src.nl2sql.config.settings import get_agent_config

logger = logging.getLogger(__name__)

_FORBIDDEN_PATTERNS = [
    r"\bimport\s+os\b",
    r"\bimport\s+sys\b",
    r"\bimport\s+subprocess\b",
    r"\bimport\s+shutil\b",
    r"\bimport\s+socket\b",
    r"\bimport\s+http\b",
    r"\bimport\s+urllib\b",
    r"\bimport\s+requests\b",
    r"\b__import__\s*\(",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    r"\bglobals\s*\(",
    r"\blocals\s*\(",
    r"\bgetattr\s*\(",
    r"\bsetattr\s*\(",
    r"\bdelattr\s*\(",
    r"\bopen\s*\(",
    r"\bbreakpoint\s*\(",
]

_FORBIDDEN_AST_NODES = (
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Global,
    ast.Nonlocal,
)


class SandboxExecutor:
    """受限 Python 代码执行器。"""

    def __init__(self) -> None:
        config = get_agent_config()
        self._timeout = config.sandbox_timeout_seconds
        self._allowed_modules = set(config.sandbox_allowed_modules)

    async def execute(
        self,
        code: str,
        data_context: dict[str, Any] | None = None,
    ) -> SandboxResult:
        """在受限环境中执行 Python 代码。

        Args:
            code: 要执行的 Python 代码
            data_context: 注入的数据上下文（如 DataFrame 变量）
        """
        start = time.perf_counter()

        violation = self._static_check(code)
        if violation:
            return SandboxResult(
                success=False,
                error=f"安全检查失败: {violation}",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._run_code, code, data_context or {}),
                timeout=self._timeout,
            )
            elapsed = (time.perf_counter() - start) * 1000
            return SandboxResult(
                success=True,
                result=result.get("result"),
                stats=result.get("stats", {}),
                stdout=result.get("stdout", ""),
                elapsed_ms=elapsed,
            )
        except TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            return SandboxResult(
                success=False,
                error=f"代码执行超时（{self._timeout}秒）",
                elapsed_ms=elapsed,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return SandboxResult(
                success=False,
                error=str(e),
                elapsed_ms=elapsed,
            )

    def _static_check(self, code: str) -> str | None:
        """静态安全检查：正则 + AST 分析。"""
        for pattern in _FORBIDDEN_PATTERNS:
            if re.search(pattern, code):
                return f"检测到禁止的模式: {pattern}"

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"语法错误: {e}"

        for node in ast.walk(tree):
            if isinstance(node, _FORBIDDEN_AST_NODES):
                return f"禁止的语法结构: {type(node).__name__}"

            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_root = alias.name.split(".")[0]
                    if module_root not in self._allowed_modules:
                        return f"禁止导入模块: {alias.name}"

            if isinstance(node, ast.ImportFrom) and node.module:
                module_root = node.module.split(".")[0]
                if module_root not in self._allowed_modules:
                    return f"禁止导入模块: {node.module}"

        return None

    def _run_code(
        self,
        code: str,
        data_context: dict[str, Any],
    ) -> dict[str, Any]:
        """在受限 namespace 中执行代码。"""
        import contextlib
        import io

        safe_builtins = {
            "abs": abs, "all": all, "any": any, "bool": bool,
            "dict": dict, "enumerate": enumerate, "filter": filter,
            "float": float, "format": format, "frozenset": frozenset,
            "int": int, "isinstance": isinstance, "issubclass": issubclass,
            "len": len, "list": list, "map": map, "max": max,
            "min": min, "next": next, "print": print, "range": range,
            "reversed": reversed, "round": round, "set": set,
            "slice": slice, "sorted": sorted, "str": str,
            "sum": sum, "tuple": tuple, "type": type, "zip": zip,
            "True": True, "False": False, "None": None,
            "__import__": self._restricted_import,
        }

        exec_globals: dict[str, Any] = {"__builtins__": safe_builtins}
        exec_globals.update(data_context)

        exec_globals["result"] = None
        exec_globals["stats"] = {}

        stdout_capture = io.StringIO()
        with contextlib.redirect_stdout(stdout_capture):
            exec(code, exec_globals)  # noqa: S102

        return {
            "result": exec_globals.get("result"),
            "stats": exec_globals.get("stats", {}),
            "stdout": stdout_capture.getvalue(),
        }

    def _restricted_import(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """受限的 __import__ 实现。"""
        module_root = name.split(".")[0]
        if module_root not in self._allowed_modules:
            raise ImportError(f"禁止导入模块: {name}")
        return __import__(name, *args, **kwargs)
