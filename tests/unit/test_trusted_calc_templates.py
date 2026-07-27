from __future__ import annotations

import pytest

from src.nl2sql.agents.codeact_engine import process_sandbox
from src.nl2sql.agents.dynamic_calc import code_executor
from src.nl2sql.agents.dynamic_calc import graph as dynamic_graph
from src.nl2sql.agents.dynamic_calc.schemas import DynamicCalcPlan
from src.nl2sql.agents.dynamic_calc.trusted_templates import (
    TrustedTemplateError,
    trusted_template_registry,
)
from src.nl2sql.config.settings import AgentConfig


def test_trusted_templates_have_typed_contracts_and_reject_unknown_calls() -> None:
    assert trusted_template_registry.execute("sum_values", {"values": [1, 2.5]}).model_dump() == {
        "total": 3.5
    }
    assert trusted_template_registry.execute("ratio", {"numerator": 3, "denominator": 2}).model_dump() == {
        "ratio": 1.5
    }

    with pytest.raises(TrustedTemplateError, match="unapproved"):
        trusted_template_registry.execute("python_eval", {"code": "result = 1"})
    with pytest.raises(TrustedTemplateError, match="invalid input"):
        trusted_template_registry.execute("mean_values", {"values": []})


@pytest.mark.asyncio
async def test_trusted_template_graph_never_executes_model_generated_code(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentConfig(
        _env_file=None,
        enable_dynamic_calc=True,
        codeact_mode="trusted-template",
    )
    monkeypatch.setattr(dynamic_graph, "get_agent_config", lambda: config)
    state = {
        "plan": DynamicCalcPlan(
            intent="total",
            trusted_template_id="sum_values",
            trusted_template_inputs={"values": [1, 4]},
        ),
        "generated_code": "raise RuntimeError('must not run')",
    }

    result = await dynamic_graph.code_exec_node(state)  # type: ignore[arg-type]

    assert result["sandbox_result"].success is True
    assert result["sandbox_result"].result == {"total": 5.0}
    assert result["sandbox_result"].stats == {"template_id": "sum_values"}
    assert dynamic_graph.after_plan_router(state) == "code_exec"  # type: ignore[arg-type]
    assert dynamic_graph.after_code_exec_router({"sandbox_result": None}) == "fallback"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_arbitrary_executors_fail_closed_outside_unsafe_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentConfig(_env_file=None, enable_dynamic_calc=True, codeact_mode="trusted-template")
    monkeypatch.setattr(code_executor, "get_agent_config", lambda: config)
    monkeypatch.setattr(process_sandbox, "get_agent_config", lambda: config)

    in_process = await code_executor.SandboxExecutor().execute("result = 1")
    process_isolated = await process_sandbox.ProcessSandbox().execute("result = 1")

    assert in_process.success is False
    assert process_isolated.success is False
    assert "disabled outside unsafe-dev" in (in_process.error or "")
    assert "disabled outside unsafe-dev" in (process_isolated.error or "")
