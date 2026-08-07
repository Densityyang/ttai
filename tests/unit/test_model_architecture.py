"""Architecture gates for the PR06 v2 model boundary."""

import ast
from pathlib import Path

_DIRECT_MODEL_SYMBOLS = frozenset(
    {"Anthropic", "AsyncAnthropic", "AsyncOpenAI", "ChatAnthropic", "ChatOpenAI", "OpenAI"}
)
_DIRECT_MODEL_MODULES = frozenset({"anthropic", "langchain_anthropic", "openai"})
_COMPATIBILITY_ALLOWLIST = frozenset({"src/nl2sql/infra/llm/factory.py"})


def test_no_direct_get_llm_calls_remain() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders = [
        path
        for path in (root / "src").rglob("*.py")
        if "get_llm(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_external_model_clients_are_confined_to_the_gateway_compatibility_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in _COMPATIBILITY_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".", 1)[0] in _DIRECT_MODEL_MODULES for alias in node.names):
                    offenders.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".", 1)[0]
                imported = {alias.name for alias in node.names}
                if module in _DIRECT_MODEL_MODULES or imported & _DIRECT_MODEL_SYMBOLS:
                    offenders.append(f"{relative}:{node.lineno}")
                elif node.module == "langchain_openai" and "ChatOpenAI" in imported:
                    offenders.append(f"{relative}:{node.lineno}")

    assert offenders == []


def test_legacy_provider_factory_is_only_loaded_by_the_guarded_gateway_bridge() -> None:
    root = Path(__file__).resolve().parents[2]
    factory_module = "src.nl2sql.infra.llm.factory"
    importers: list[str] = []
    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == factory_module:
                importers.append(relative)
            elif isinstance(node, ast.Import) and any(
                alias.name == factory_module for alias in node.names
            ):
                importers.append(relative)

    assert importers == ["src/nl2sql/infra/llm/gateway.py"]


def test_raw_chat_completion_http_calls_are_gateway_only() -> None:
    root = Path(__file__).resolve().parents[2]
    gateway = "src/nl2sql/infra/llm/gateway.py"
    offenders = [
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*.py")
        if path.relative_to(root).as_posix() != gateway
        and "/chat/completions" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_v2_runtime_uses_explicit_engine_not_supervisor() -> None:
    root = Path(__file__).resolve().parents[2]
    container = (root / "src/nl2sql/container.py").read_text(encoding="utf-8")
    v2_api = (root / "src/nl2sql/v2.py").read_text(encoding="utf-8")
    legacy_registry = (root / "src/nl2sql/infra/runtime/registry.py").read_text(
        encoding="utf-8"
    )

    assert "create_v2_engine" in container
    assert "create_supervisor" not in container
    assert "_engine_from_request" in v2_api
    assert "get_supervisor" not in v2_api
    assert "get_legacy_model" not in legacy_registry
