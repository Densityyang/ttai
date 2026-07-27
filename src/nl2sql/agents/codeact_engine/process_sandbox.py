"""进程隔离沙箱 -- CodeAct 代码的安全执行环境。

关键升级：从进程内 exec() 升级为子进程隔离。
使用 multiprocessing 创建独立进程执行 AI 生成的代码，
通过 resource 模块限制 CPU 时间和内存。

该模块仅在 Linux 上完整生效；非 Linux 系统降级为线程隔离 + 超时。
"""

import ast
import asyncio
import logging
import multiprocessing as mp
import re
import time
from multiprocessing import Queue
from typing import Any

from src.nl2sql.agents.dynamic_calc.schemas import SandboxResult
from src.nl2sql.config.settings import get_agent_config

logger = logging.getLogger(__name__)

_FORBIDDEN_PATTERNS: list[str] = [
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


def _apply_resource_limits(cpu_seconds: int, memory_mb: int) -> None:
    """在子进程启动时设置资源限制（Linux only）。"""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        mem_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def _sandbox_worker(
    code: str,
    data_context_serialized: dict[str, Any],
    allowed_modules: list[str],
    cpu_seconds: int,
    memory_mb: int,
    result_queue: Queue,  # type: ignore[type-arg]
) -> None:
    """在隔离子进程中运行代码。"""
    import contextlib
    import io

    _apply_resource_limits(cpu_seconds, memory_mb)

    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        pd = None  # type: ignore[assignment]
        np = None  # type: ignore[assignment]

    data_ctx: dict[str, Any] = {}
    for key, val in data_context_serialized.items():
        if isinstance(val, dict) and val.get("__dataframe__"):
            if pd is not None:
                data_ctx[key] = pd.DataFrame(val["data"], columns=val.get("columns"))
            else:
                data_ctx[key] = val["data"]
        else:
            data_ctx[key] = val

    if pd is not None:
        data_ctx["pd"] = pd
    if np is not None:
        data_ctx["np"] = np

    allowed = set(allowed_modules)

    def restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
        module_root = name.split(".")[0]
        if module_root not in allowed:
            raise ImportError(f"禁止导入模块: {name}")
        return __import__(name, *args, **kwargs)

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
        "__import__": restricted_import,
    }

    exec_globals: dict[str, Any] = {"__builtins__": safe_builtins}
    exec_globals.update(data_ctx)
    exec_globals["result"] = None
    exec_globals["stats"] = {}

    stdout_capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_capture):
            exec(code, exec_globals)  # noqa: S102

        result_val = exec_globals.get("result")
        stats_val = exec_globals.get("stats", {})

        if pd is not None and isinstance(result_val, pd.DataFrame):
            result_val = result_val.to_dict(orient="records")
        if pd is not None and isinstance(result_val, pd.Series):
            result_val = result_val.to_dict()

        if isinstance(stats_val, dict):
            clean_stats = {}
            for k, v in stats_val.items():
                if pd is not None and isinstance(v, (pd.DataFrame, pd.Series)):
                    clean_stats[k] = str(v)
                else:
                    clean_stats[k] = v
            stats_val = clean_stats

        result_queue.put({
            "success": True,
            "result": result_val,
            "stats": stats_val,
            "stdout": stdout_capture.getvalue(),
        })
    except Exception as e:
        result_queue.put({
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "stdout": stdout_capture.getvalue(),
        })


class ProcessSandbox:
    """进程隔离 Python 代码沙箱。"""

    def __init__(self) -> None:
        config = get_agent_config()
        self._timeout = config.sandbox_timeout_seconds
        self._max_memory_mb = config.sandbox_max_memory_mb
        self._allowed_modules = list(config.sandbox_allowed_modules)

    async def execute(
        self,
        code: str,
        data_context: dict[str, Any] | None = None,
    ) -> SandboxResult:
        """在隔离进程中执行代码。

        Args:
            code: Python 代码
            data_context: 注入的数据上下文 (DataFrame 需先序列化)
        """
        start = time.perf_counter()

        violation = self._static_check(code)
        if violation:
            return SandboxResult(
                success=False,
                error=f"安全检查失败: {violation}",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

        serialized = self._serialize_context(data_context or {})

        try:
            result = await asyncio.wait_for(
                self._run_in_process(code, serialized),
                timeout=self._timeout + 5,
            )
            elapsed = (time.perf_counter() - start) * 1000
            if result["success"]:
                return SandboxResult(
                    success=True,
                    result=result.get("result"),
                    stats=result.get("stats", {}),
                    stdout=result.get("stdout", ""),
                    elapsed_ms=elapsed,
                )
            return SandboxResult(
                success=False,
                error=result.get("error", "未知错误"),
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
                error=f"沙箱执行异常: {e}",
                elapsed_ms=elapsed,
            )

    async def _run_in_process(
        self,
        code: str,
        data_context_serialized: dict[str, Any],
    ) -> dict[str, Any]:
        """在子进程中运行代码。"""
        result_queue: Queue[dict[str, Any]] = mp.Queue()

        proc = mp.Process(
            target=_sandbox_worker,
            args=(
                code,
                data_context_serialized,
                self._allowed_modules,
                self._timeout,
                self._max_memory_mb,
                result_queue,
            ),
            daemon=True,
        )
        proc.start()

        result = await asyncio.to_thread(self._wait_for_result, proc, result_queue)
        return result

    def _wait_for_result(
        self,
        proc: mp.Process,
        result_queue: Queue,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """等待子进程完成并获取结果。"""
        proc.join(timeout=self._timeout + 2)

        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
            return {"success": False, "error": f"进程执行超时（{self._timeout}秒），已终止"}

        try:
            return result_queue.get_nowait()
        except Exception:
            exit_code = proc.exitcode
            if exit_code and exit_code < 0:
                import signal
                try:
                    sig_name = signal.Signals(-exit_code).name
                except (ValueError, AttributeError):
                    sig_name = str(-exit_code)
                return {"success": False, "error": f"进程被信号终止: {sig_name}（可能内存/CPU超限）"}
            return {"success": False, "error": f"进程异常退出 (code={exit_code})"}

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

    @staticmethod
    def _serialize_context(data_context: dict[str, Any]) -> dict[str, Any]:
        """将 data_context 序列化为可跨进程传输的格式。"""
        serialized: dict[str, Any] = {}
        for key, val in data_context.items():
            try:
                import pandas as pd
                if isinstance(val, pd.DataFrame):
                    serialized[key] = {
                        "__dataframe__": True,
                        "columns": list(val.columns),
                        "data": val.values.tolist(),
                    }
                    continue
            except ImportError:
                pass
            serialized[key] = val
        return serialized
