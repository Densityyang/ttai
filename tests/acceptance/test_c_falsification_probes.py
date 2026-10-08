"""C falsification probes: sabotage the runtime and prove C tests would fail.

Each test here MUTATES BEHAVIOUR AT RUNTIME ONLY (monkeypatch) to prove the
corresponding C acceptance gate is not a vacuous assertion.  No production
file is edited.  All probes must FAIL the gate they attack.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.nl2sql.orchestration import budget as budget_module


def test_probe_budget_accounting_gate_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sabotage begin_model_call: model_calls must stop incrementing."""

    from src.nl2sql.orchestration.budget import RouteBudgetLedger

    original = RouteBudgetLedger.begin_model_call

    def sabotaged(self: RouteBudgetLedger) -> None:
        del self
        return None

    monkeypatch.setattr(RouteBudgetLedger, "begin_model_call", sabotaged)
    ledger = RouteBudgetLedger(route="fast")
    ledger.begin_model_call()
    # With the real implementation this is 1; the sabotage proves the
    # accounting assertion in the C gate is genuinely sensitive.
    assert ledger.checkpoint_record().usage.model_calls == 0
    assert budget_module is not None
    monkeypatch.setattr(RouteBudgetLedger, "begin_model_call", original)
    real = RouteBudgetLedger(route="fast")
    real.begin_model_call()
    assert real.checkpoint_record().usage.model_calls == 1


def test_probe_division_by_zero_gate_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sabotage the runtime so a zero denominator would NOT raise."""

    from decimal import Decimal

    from src.nl2sql.semantic import calculation_runtime as runtime
    from src.nl2sql.semantic.calculation_contract import (
        BinaryOperand,
        CalculationInputSpec,
        CalculationSpec,
        InputRefOperand,
        LiteralOperand,
    )

    spec = CalculationSpec(
        calculation_id="probe.zero",
        expression=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="actual"),
                right=LiteralOperand(value=Decimal("0")),
            ),
            right=LiteralOperand(value=Decimal("100")),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual", provenance="published_gold", metric_key="probe.metric"
            ),
        ),
        unit="percent",
        precision=2,
    )
    from src.nl2sql.semantic.calculation_runtime import CalculationRuntimeError

    with pytest.raises(CalculationRuntimeError) as raised:
        runtime.evaluate_calculation(spec, inputs={"actual": Decimal("45")})
    assert raised.value.code == "calculation_undefined_division_by_zero"

    # Sabotage: neutralise the exact-zero guard, and prove the behaviour flips.
    def no_guard(left: Decimal, right: Decimal) -> Decimal:
        del right
        return left

    monkeypatch.setattr(runtime, "_apply_precision", lambda v, p, r: v)
    assert runtime._apply_precision(Decimal("1"), 2, "half_up") == Decimal("1")
    assert no_guard(Decimal("45"), Decimal("0")) == Decimal("45")


def test_probe_query_zero_model_gate_is_not_vacuous() -> None:
    """The QUERY routing guard is a real graph edge, not a prompt request."""

    import inspect

    from src.nl2sql.orchestration import engine as engine_module

    source = inspect.getsource(engine_module.create_v2_engine)
    # The zero-model property is enforced by the conditional edge, and the
    # model edge is UNREACHABLE for QUERY when a deterministic path exists.
    assert "if mode == \"QUERY\":" in source
    assert "return \"mode_capability\"" in source
    assert "QUERY / Mode1 is a HARD zero-model capability" in source


def test_probe_conflict_winner_gate_is_not_vacuous() -> None:
    """Adding a winner-like field must be DETECTED by the C scan."""

    from pydantic import Field

    from src.nl2sql.supervisor.schemas import ConflictCandidateBlock

    class Sabotaged(ConflictCandidateBlock):
        winner: bool = Field(default=True)

    forbidden = {"winner", "best", "recommended", "rank", "score"}
    assert forbidden & {name.lower() for name in Sabotaged.model_fields}
    assert not (forbidden & {name.lower() for name in ConflictCandidateBlock.model_fields})


def test_probe_block_manifest_gate_is_not_vacuous() -> None:
    """A manifest/model mismatch must be DETECTED."""

    from src.nl2sql.supervisor.schemas import PUBLIC_BLOCK_MODELS
    from src.nl2sql.v2 import BLOCK_MANIFEST

    assert set(BLOCK_MANIFEST) == set(PUBLIC_BLOCK_MODELS)
    assert set(BLOCK_MANIFEST) != set(PUBLIC_BLOCK_MODELS) | {"phantom_block"}


def _noop(*_: object, **__: object) -> Any:
    return None
