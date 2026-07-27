"""Architecture gates for the PR06 v2 model boundary."""

from pathlib import Path


def test_no_direct_get_llm_calls_remain() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders = [
        path
        for path in (root / "src").rglob("*.py")
        if "get_llm(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_v2_runtime_uses_explicit_engine_not_supervisor() -> None:
    root = Path(__file__).resolve().parents[2]
    container = (root / "src/nl2sql/container.py").read_text(encoding="utf-8")
    v2_api = (root / "src/nl2sql/v2.py").read_text(encoding="utf-8")

    assert "create_v2_engine" in container
    assert "create_supervisor" not in container
    assert "_engine_from_request" in v2_api
