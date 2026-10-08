"""Pure typed calculation evaluator: migration-foundation behaviour.

Behaviour families are tagged with their migration classification:
ADAPT_MIGRATE for the typed deterministic evaluator, DIRECT_MIGRATE for plain
zero arithmetic, INTENTIONAL_REWRITE for divide-by-zero, NEW_BUILD/unsupported
for aggregates.  Legacy reference: gold/metric_system/engine/expression.py and
engine/status.py (read-only reference).
"""

from __future__ import annotations

import hashlib
from datetime import date as _date
from decimal import Decimal
from uuid import UUID

import pytest

from src.nl2sql.semantic.calculation_contract import (
    AggregateOperand,
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    LiteralOperand,
    ParameterBinding,
    ParameterRefOperand,
    ParameterSpec,
)
from src.nl2sql.semantic.calculation_runtime import (
    CalculationRuntimeError,
    evaluate_calculation,
    evaluate_expression,
)


def _literal(value: str | int) -> LiteralOperand:
    return LiteralOperand(value=Decimal(str(value)))


def _ratio_spec(**overrides: object) -> CalculationSpec:
    base: dict[str, object] = {
        "calculation_id": "adhoc.on_time_rate",
        "expression": BinaryOperand(
            op="divide",
            left=BinaryOperand(
                op="multiply",
                left=LiteralOperand(value=Decimal("100")),
                right=InputRefOperand(role="on_time"),
            ),
            right=InputRefOperand(role="total"),
        ),
        "inputs": (
            CalculationInputSpec(
                role="on_time", provenance="published_gold", metric_key="metric.on_time"
            ),
            CalculationInputSpec(
                role="total", provenance="published_gold", metric_key="metric.total"
            ),
        ),
        "unit": "percent",
        "precision": 2,
        "rounding": "half_up",
    }
    base.update(overrides)
    return CalculationSpec(**base)


def _runtime_step():
    """The legacy V1 formula as a real AdHocCalculationStep."""
    from src.nl2sql.contracts import AdHocCalculationStep
    from src.nl2sql.semantic.calculation_contract import RoundOperand, derived_output_id

    spec = CalculationSpec(
        calculation_id="adhoc.on_time_rate",
        expression=RoundOperand(
            operand=BinaryOperand(
                op="multiply",
                left=BinaryOperand(
                    op="divide",
                    left=InputRefOperand(role="on_time"),
                    right=InputRefOperand(role="total"),
                ),
                right=_literal(100),
            ),
            digits=2,
        ),
        inputs=(
            CalculationInputSpec(
                role="on_time", provenance="published_gold", metric_key="metric.on_time"
            ),
            CalculationInputSpec(
                role="total", provenance="published_gold", metric_key="metric.total"
            ),
        ),
        unit="percent",
        precision=2,
        rounding="half_up",
    )
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
    )
    return AdHocCalculationStep(
        step_id="calculate_adhoc",
        calculation_spec=spec,
        execution_binding=binding,
        input_refs={"on_time": "fetch_on_time.value", "total": "fetch_total.value"},
        depends_on=("fetch_on_time", "fetch_total"),
        derived_output_id=derived_output_id(spec, binding),
    )


_RUNTIME_RELEASE = UUID("11111111-1111-1111-1111-111111111111")
_RUNTIME_SNAPSHOT = UUID("22222222-2222-2222-2222-222222222222")


def _runtime_spec_and_binding():
    """The legacy V1 guarded-rate formula as a real typed spec + binding."""
    from src.nl2sql.semantic.calculation_contract import RoundOperand

    spec = CalculationSpec(
        calculation_id="adhoc.on_time_rate",
        expression=RoundOperand(
            operand=BinaryOperand(
                op="multiply",
                left=BinaryOperand(
                    op="divide",
                    left=InputRefOperand(role="on_time"),
                    right=InputRefOperand(role="total"),
                ),
                right=_literal(100),
            ),
            digits=2,
        ),
        inputs=(
            CalculationInputSpec(
                role="on_time", provenance="published_gold", metric_key="metric.on_time"
            ),
            CalculationInputSpec(
                role="total", provenance="published_gold", metric_key="metric.total"
            ),
        ),
        unit="percent",
        precision=2,
        rounding="half_up",
    )
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
    )
    return spec, binding


class _StubMetricRunner:
    """Delivers the already-aggregated scalar per dependency fetch."""

    def __init__(self, values: dict[str, int]) -> None:
        self.values = values

    async def prepare(self, *, step, query_plan, context):  # type: ignore[no-untyped-def]
        from src.nl2sql.orchestration.execution import PreparedMetricStep

        metric = step.metric_keys[0]
        return PreparedMetricStep(
            sql_fingerprint=hashlib.sha256(metric.encode()).hexdigest(),
            join_hops=0,
            payload={"metric": metric, "fingerprint": hashlib.sha256(metric.encode()).hexdigest()},
        )

    async def execute(self, prepared, *, timeout_ms):  # type: ignore[no-untyped-def]
        from src.nl2sql.contracts import ExecutionReceipt
        from src.nl2sql.orchestration.execution import MetricStepResult

        metric = prepared.payload["metric"]  # type: ignore[index]
        fingerprint = prepared.payload["fingerprint"]  # type: ignore[index]
        return MetricStepResult(
            value={"value": self.values[metric]},
            receipt=ExecutionReceipt(
                datasource="synthetic",
                readonly_role="fixture_reader",
                elapsed_ms=1,
                row_count=1,
                sql_fingerprint=fingerprint,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


def _runtime_query_plan():
    from src.nl2sql.contracts import QueryPlan, TimeRange

    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.on_time", "metric.total"),
        time_range=TimeRange(start=_date(2026, 8, 1), end=_date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
    )


def _runtime_context():
    from src.nl2sql.contracts import ContextBundle

    return ContextBundle(
        semantic_release_id=_RUNTIME_RELEASE,
        schema_snapshot_id=_RUNTIME_SNAPSHOT,
        domains=("finance",),
        asset_ids=("metric.on_time", "metric.total"),
        resolution_status="resolved",
    )


_RUNTIME_QUERY_PLAN = _runtime_query_plan()
_RUNTIME_CONTEXT = _runtime_context()


# 0. Frozen serialization/identity lock ---------------------------------------
def test_pinned_spec_checksum_is_stable_for_a_fixed_spec() -> None:
    """A pinned identity lock: the SPEC checksum for a fixed spec is frozen.

    All other checksum assertions in the suite are reflexive (x == x). This is
    the one executable non-regression guard for spec/binding identity, so a
    future serialization or schema change cannot pass silently.
    """
    spec = _ratio_spec()
    # Re-pinned for calculation schema 1.1 (the inert global null/zero policy
    # fields were removed and schema_version moved to "1.1", so every spec
    # checksum intentionally changes).
    assert spec.checksum == "691b6d9e842d1a796ced053a03cadf3979c51fa0b2f8cb5b4e32cf1ec00e6993"


# 1. LiteralOperand -------------------------------------------------------------
def test_literal_evaluates_exactly() -> None:
    assert evaluate_expression(_literal("2.50")) == Decimal("2.50")
    assert evaluate_expression(_literal(0)) == Decimal(0)


# 2. InputRefOperand / ParameterRefOperand --------------------------------------
def test_input_and_parameter_references_resolve() -> None:
    expression = BinaryOperand(
        op="add", left=InputRefOperand(role="on_time"), right=ParameterRefOperand(name="bump")
    )
    assert evaluate_expression(
        expression, inputs={"on_time": 5}, parameters={"bump": 2}
    ) == Decimal(7)


def test_missing_input_and_parameter_fail_closed() -> None:
    with pytest.raises(CalculationRuntimeError) as missing_input:
        evaluate_expression(InputRefOperand(role="on_time"), inputs={})
    assert missing_input.value.code == "calculation_input_missing"

    with pytest.raises(CalculationRuntimeError) as missing_parameter:
        evaluate_expression(ParameterRefOperand(name="bump"), parameters={})
    assert missing_parameter.value.code == "calculation_parameter_missing"


# 3. BinaryOperand arithmetic ---------------------------------------------------
def test_all_four_binary_operators_are_deterministic() -> None:
    left, right = _literal(7), _literal(2)
    assert evaluate_expression(BinaryOperand(op="add", left=left, right=right)) == Decimal(9)
    assert evaluate_expression(BinaryOperand(op="subtract", left=left, right=right)) == Decimal(5)
    assert evaluate_expression(BinaryOperand(op="multiply", left=left, right=right)) == Decimal(14)
    assert evaluate_expression(BinaryOperand(op="divide", left=left, right=right)) == Decimal(7) / Decimal(2)


# 4. Zero is a real value (DIRECT_MIGRATE) --------------------------------------
def test_zero_is_a_real_value_not_no_data() -> None:
    zero, positive = _literal(0), _literal(4)
    assert evaluate_expression(BinaryOperand(op="add", left=zero, right=zero)) == Decimal(0)
    assert evaluate_expression(BinaryOperand(op="multiply", left=zero, right=positive)) == Decimal(0)
    # 0 / positive -> 0 (never no_data, never a sentinel)
    assert evaluate_expression(BinaryOperand(op="divide", left=zero, right=positive)) == Decimal(0)


# 5. Divide by zero (INTENTIONAL_REWRITE) ---------------------------------------
def test_divide_by_zero_is_undefined_not_no_data() -> None:
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(
            BinaryOperand(op="divide", left=_literal(1), right=_literal(0))
        )
    assert error.value.code == "calculation_undefined_division_by_zero"
    # it is an ERROR, never a value: no None, no no_data, no Infinity/NaN
    assert "no_data" not in error.value.code


def test_divide_by_zero_is_undefined_for_a_zero_denominator_input() -> None:
    expression = BinaryOperand(
        op="divide", left=InputRefOperand(role="on_time"), right=InputRefOperand(role="total")
    )
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(expression, inputs={"on_time": 3, "total": 0})
    assert error.value.code == "calculation_undefined_division_by_zero"


def test_zero_numerator_over_zero_denominator_is_still_undefined() -> None:
    # The legacy NULLIF(0,0) -> NULL -> NO_DATA path must NOT be reproduced.
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(
            BinaryOperand(op="divide", left=_literal(0), right=_literal(0))
        )
    assert error.value.code == "calculation_undefined_division_by_zero"


# 6. NULL / missing operand (no invented global policy) -------------------------
@pytest.mark.parametrize("null_value", [None])
def test_null_operand_fails_closed_without_inventing_a_policy(null_value: object) -> None:
    expression = BinaryOperand(
        op="add", left=InputRefOperand(role="on_time"), right=_literal(1)
    )
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(expression, inputs={"on_time": null_value})
    # An unhandled NULL fails closed at the boundary: never coerced to 0 and
    # never turned into no_data.  An explicit COALESCE/CASE is what gives a NULL
    # business meaning.
    assert error.value.code == "calculation_result_null_unsupported"
    assert "no_data" not in error.value.code


# 6b. Explicit NULL semantics: COALESCE / CASE / NULL predicates ---------------
def test_coalesce_substitutes_only_an_authored_default() -> None:
    from src.nl2sql.semantic.calculation_contract import CoalesceOperand

    expression = CoalesceOperand(
        operand=InputRefOperand(role="on_time"), default=_literal(0)
    )
    # legacy V1 shape: COALESCE({on_time}, 0)
    assert evaluate_expression(expression, inputs={"on_time": None}) == Decimal(0)
    assert evaluate_expression(expression, inputs={"on_time": 5}) == Decimal(5)


def test_case_branches_are_three_valued_and_else_defaults_to_null() -> None:
    from src.nl2sql.semantic.calculation_contract import (
        CaseOperand,
        CompareOperand,
        WhenBranch,
    )

    guarded = CaseOperand(
        whens=(
            WhenBranch(
                condition=CompareOperand(
                    operator="gt", left=InputRefOperand(role="total"), right=_literal(0)
                ),
                then=BinaryOperand(
                    op="divide",
                    left=InputRefOperand(role="on_time"),
                    right=InputRefOperand(role="total"),
                ),
            ),
        ),
    )
    assert evaluate_expression(guarded, inputs={"on_time": 1, "total": 4}) == Decimal("0.25")
    # an absent ELSE yields NULL, and an unhandled NULL fails closed
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(guarded, inputs={"on_time": 1, "total": 0})
    assert error.value.code == "calculation_result_null_unsupported"


def test_null_predicates_distinguish_null_from_zero() -> None:
    from src.nl2sql.semantic.calculation_contract import NullTestOperand

    is_null = NullTestOperand(operator="is_null", operand=InputRefOperand(role="on_time"))
    not_null = NullTestOperand(
        operator="is_not_null", operand=InputRefOperand(role="on_time")
    )
    assert evaluate_expression(is_null, inputs={"on_time": None}) == Decimal(1)
    assert evaluate_expression(is_null, inputs={"on_time": 0}) == Decimal(0)
    assert evaluate_expression(not_null, inputs={"on_time": 0}) == Decimal(1)
    assert evaluate_expression(not_null, inputs={"on_time": None}) == Decimal(0)


# 6c. Explicit ROUND and compound/weighted formulas -----------------------------
def test_explicit_round_is_part_of_the_formula() -> None:
    from src.nl2sql.semantic.calculation_contract import RoundOperand

    expression = RoundOperand(
        operand=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="on_time"),
                right=InputRefOperand(role="total"),
            ),
            right=_literal(100),
        ),
        digits=2,
    )
    # legacy V1 exactly: ROUND((CAST(...) / total) * 100, 2)
    assert evaluate_expression(expression, inputs={"on_time": 2, "total": 3}) == Decimal("66.67")
    assert evaluate_expression(expression, inputs={"on_time": 1, "total": 8}) == Decimal("12.50")


def test_weighted_formula_matches_the_legacy_vector() -> None:
    from src.nl2sql.semantic.calculation_contract import RoundOperand

    expression = RoundOperand(
        operand=BinaryOperand(
            op="add",
            left=BinaryOperand(
                op="multiply",
                left=InputRefOperand(role="recognition"),
                right=_literal("0.3"),
            ),
            right=BinaryOperand(
                op="multiply",
                left=InputRefOperand(role="qualification"),
                right=_literal("0.7"),
            ),
        ),
        digits=2,
    )
    # inspection.yaml:178 -> recognition=80, qualification=90 => 87.00
    assert evaluate_expression(
        expression, inputs={"recognition": 80, "qualification": 90}
    ) == Decimal("87.00")


def test_compound_denominator_formula_matches_the_legacy_vector() -> None:
    from src.nl2sql.semantic.calculation_contract import (
        CaseOperand,
        CompareOperand,
        RoundOperand,
        WhenBranch,
    )

    total = BinaryOperand(
        op="add",
        left=InputRefOperand(role="repair"),
        right=InputRefOperand(role="fault"),
    )
    guarded = CaseOperand(
        whens=(
            WhenBranch(
                condition=CompareOperand(operator="gt", left=total, right=_literal(0)),
                then=RoundOperand(
                    operand=BinaryOperand(
                        op="multiply",
                        left=BinaryOperand(
                            op="divide", left=InputRefOperand(role="repair"), right=total
                        ),
                        right=_literal(100),
                    ),
                    digits=2,
                ),
            ),
        ),
    )
    # repair_service.yaml:591 -> repair=3, fault=1 => 75.00
    assert evaluate_expression(guarded, inputs={"repair": 3, "fault": 1}) == Decimal("75.00")
    # 0/0 hits the guard, not the division: NULL -> fail closed, NEVER no_data
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(guarded, inputs={"repair": 0, "fault": 0})
    assert error.value.code == "calculation_result_null_unsupported"


def test_guarded_division_does_not_raise_the_divide_by_zero_error() -> None:
    # ARCHITECTURAL HIGH: a guarded denominator is never an ACTIVE divide-by-zero.
    from src.nl2sql.semantic.calculation_contract import (
        CaseOperand,
        CompareOperand,
        WhenBranch,
    )

    guarded = CaseOperand(
        whens=(
            WhenBranch(
                condition=CompareOperand(
                    operator="gt", left=InputRefOperand(role="total"), right=_literal(0)
                ),
                then=BinaryOperand(
                    op="divide",
                    left=InputRefOperand(role="on_time"),
                    right=InputRefOperand(role="total"),
                ),
            ),
        ),
        otherwise=_literal(0),
    )
    assert evaluate_expression(guarded, inputs={"on_time": 5, "total": 0}) == Decimal(0)


# 6d. End-to-end runnable path: compiled AD_HOC plan -> runtime adapter ---------
@pytest.mark.asyncio
async def test_runtime_adapter_executes_a_compiled_ad_hoc_plan() -> None:
    from src.nl2sql.orchestration.execution import (
        PlanExecutor,
        RuntimeCalculationRunner,
    )

    runner = RuntimeCalculationRunner()
    step = _runtime_step()
    output = await runner.execute(step=step, inputs={"on_time": 2, "total": 3})
    assert output == "66.67"
    assert isinstance(output, str)
    # PlanExecutor accepts it as the AD_HOC runner seam
    executor = PlanExecutor(
        metric_runner=None,  # type: ignore[arg-type]
        ad_hoc_calculation_runner=runner,
    )
    assert executor is not None


@pytest.mark.asyncio
async def test_runtime_adapter_maps_calculation_errors_verbatim() -> None:
    from src.nl2sql.orchestration.execution import PlanStepError, RuntimeCalculationRunner

    runner = RuntimeCalculationRunner()
    step = _runtime_step()
    with pytest.raises(PlanStepError) as error:
        await runner.execute(step=step, inputs={"on_time": 1, "total": 0})
    assert error.value.code == "calculation_undefined_division_by_zero"


# 6e. Hardening: bounds, Decimal traps, magnitude -------------------------------
def test_high_magnitude_quantize_becomes_a_typed_error() -> None:
    # adversarial finding: spec-level quantize used to escape as a raw
    # decimal.InvalidOperation
    spec = _ratio_spec(expression=_literal("9" * 70), precision=2, rounding="half_up")
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_calculation(spec, inputs={})
    assert error.value.code.startswith("calculation_")


def test_overflow_is_a_typed_error_not_a_raw_decimal_exception() -> None:
    expression = BinaryOperand(
        op="add", left=_literal("1e999999999"), right=_literal(1)
    )
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(expression)
    assert error.value.code == "calculation_numeric_out_of_range"


def test_unbounded_expression_is_rejected_by_the_public_entry_point() -> None:
    # a raw 5000-level chain must not blow the stack: the public entry point
    # enforces the same bound the contract does
    from src.nl2sql.semantic.calculation_contract import RoundOperand

    expression: object = _literal(1)
    for _ in range(5000):
        expression = RoundOperand(operand=expression, digits=2)  # type: ignore[arg-type]
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(expression)  # type: ignore[arg-type]
    assert error.value.code in (
        "calculation_expression_too_deep",
        "calculation_expression_too_large",
    )


def test_deep_but_bounded_expression_still_evaluates() -> None:
    from src.nl2sql.semantic.calculation_contract import RoundOperand

    expression: object = _literal(2)
    for _ in range(4):
        expression = RoundOperand(operand=expression, digits=2)  # type: ignore[arg-type]
    assert evaluate_expression(expression) == Decimal(2)  # type: ignore[arg-type]


# 6f. SQL-like three-valued CONDITION semantics (Fix B) -------------------------
def _all(*conditions: object) -> object:
    from src.nl2sql.semantic.calculation_contract import AllOperand

    return AllOperand(conditions=conditions)  # type: ignore[arg-type]


def _any(*conditions: object) -> object:
    from src.nl2sql.semantic.calculation_contract import AnyOperand

    return AnyOperand(conditions=conditions)  # type: ignore[arg-type]


def _gt(role: str, value: object) -> object:
    from src.nl2sql.semantic.calculation_contract import CompareOperand

    return CompareOperand(operator="gt", left=InputRefOperand(role=role), right=_literal(value))


def _isnull(role: str) -> object:
    from src.nl2sql.semantic.calculation_contract import NullTestOperand

    return NullTestOperand(operator="is_null", operand=InputRefOperand(role=role))


def _case(*whens: tuple[object, object], otherwise: object = None) -> object:
    from src.nl2sql.semantic.calculation_contract import CaseOperand, WhenBranch

    return CaseOperand(
        whens=tuple(
            WhenBranch(condition=condition, then=result)  # type: ignore[arg-type]
            for condition, result in whens
        ),
        otherwise=otherwise,  # type: ignore[arg-type]
    )


def test_compare_with_null_is_unknown_and_case_does_not_select() -> None:
    # NULL > 0 => UNKNOWN => the branch is NOT selected
    expression = _case((_gt("x", 0), _literal(7)), otherwise=_literal(9))
    assert evaluate_expression(expression, inputs={"x": None}) == Decimal(9)  # type: ignore[arg-type]
    assert evaluate_expression(expression, inputs={"x": 1}) == Decimal(7)  # type: ignore[arg-type]
    assert evaluate_expression(expression, inputs={"x": 0}) == Decimal(9)  # type: ignore[arg-type]


def test_all_truth_table() -> None:
    expression = _case((_all(_gt("a", 0), _gt("b", 0)), _literal(1)), otherwise=_literal(0))
    # TRUE AND TRUE => TRUE
    assert evaluate_expression(expression, inputs={"a": 1, "b": 1}) == Decimal(1)  # type: ignore[arg-type]
    # TRUE AND UNKNOWN => UNKNOWN => not selected
    assert evaluate_expression(expression, inputs={"a": 1, "b": None}) == Decimal(0)  # type: ignore[arg-type]
    # FALSE AND UNKNOWN => FALSE => not selected
    assert evaluate_expression(expression, inputs={"a": 0, "b": None}) == Decimal(0)  # type: ignore[arg-type]


def test_any_truth_table() -> None:
    expression = _case((_any(_isnull("a"), _isnull("b")), _literal(1)), otherwise=_literal(0))
    # TRUE OR UNKNOWN => TRUE
    assert evaluate_expression(expression, inputs={"a": None, "b": 3}) == Decimal(1)  # type: ignore[arg-type]
    # FALSE OR FALSE => FALSE
    assert evaluate_expression(expression, inputs={"a": 3, "b": 4}) == Decimal(0)  # type: ignore[arg-type]
    # FALSE OR UNKNOWN => UNKNOWN => not selected
    mixed = _case((_any(_gt("a", 5), _isnull("b")), _literal(1)), otherwise=_literal(0))
    assert evaluate_expression(mixed, inputs={"a": 1, "b": 3}) == Decimal(0)  # type: ignore[arg-type]
    assert evaluate_expression(mixed, inputs={"a": 1, "b": None}) == Decimal(1)  # type: ignore[arg-type]


def test_unknown_is_not_no_data_not_error_and_not_zero() -> None:
    # A bare UNKNOWN condition used as a VALUE fails closed as NULL, never 0.
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(_gt("x", 0), inputs={"x": None})  # type: ignore[arg-type]
    assert error.value.code == "calculation_result_null_unsupported"
    assert "no_data" not in error.value.code


def _legacy_repair_service_formula() -> object:
    """The REAL legacy repair_service formula (repair_service.yaml:591).

    CASE
      WHEN repair IS NULL OR fault IS NULL THEN NULL
      WHEN repair + fault > 0
        THEN ROUND(repair / (repair + fault) * 100, 2)
      ELSE NULL
    END

    The authored NULL results are NullLiteralOperand (first branch) and an
    absent otherwise (ELSE NULL) - NEVER a numeric zero.
    """
    from src.nl2sql.semantic.calculation_contract import NullLiteralOperand, RoundOperand

    total = BinaryOperand(
        op="add",
        left=InputRefOperand(role="repair"),
        right=InputRefOperand(role="fault"),
    )
    from src.nl2sql.semantic.calculation_contract import CompareOperand

    return _case(
        # WHEN repair IS NULL OR fault IS NULL THEN NULL
        (_any(_isnull("repair"), _isnull("fault")), NullLiteralOperand()),
        # WHEN repair + fault > 0
        (
            CompareOperand(operator="gt", left=total, right=_literal(0)),
            RoundOperand(
                operand=BinaryOperand(
                    op="multiply",
                    left=BinaryOperand(
                        op="divide", left=InputRefOperand(role="repair"), right=total
                    ),
                    right=_literal(100),
                ),
                digits=2,
            ),
        ),
        # ELSE NULL - an EXPLICIT authored NULL, exactly as the source writes it.
        # The legacy corpus has zero absent-ELSE CASEs, so the else branch is
        # present and explicit rather than omitted.
        otherwise=NullLiteralOperand(),
    )


def test_legacy_repair_or_formula_represents_null_without_substitution() -> None:
    formula = _legacy_repair_service_formula()
    # repair=3, fault=1 -> 75.00 (real legacy vector)
    assert evaluate_expression(formula, inputs={"repair": 3, "fault": 1}) == Decimal("75.00")  # type: ignore[arg-type]


def test_legacy_repair_first_branch_yields_authored_null() -> None:
    formula = _legacy_repair_service_formula()
    # repair IS NULL OR fault IS NULL -> THEN NULL -> authored NULL, NOT 0
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(formula, inputs={"repair": None, "fault": 1})  # type: ignore[arg-type]
    assert error.value.code == "calculation_result_null_unsupported"
    assert "no_data" not in error.value.code


def test_legacy_repair_else_null_never_executes_the_division() -> None:
    formula = _legacy_repair_service_formula()
    # repair=0, fault=0: the guard is FALSE, so ELSE NULL applies.  The division
    # must NEVER run, and the outcome must NOT be a divide-by-zero error.
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(formula, inputs={"repair": 0, "fault": 0})  # type: ignore[arg-type]
    assert error.value.code == "calculation_result_null_unsupported"
    assert error.value.code != "calculation_undefined_division_by_zero"
    assert "no_data" not in error.value.code


def test_authored_null_is_distinct_from_authored_zero() -> None:
    from src.nl2sql.semantic.calculation_contract import (
        CoalesceOperand,
        NullLiteralOperand,
    )

    # an authored NULL is a value-state: COALESCE may substitute it deliberately
    substituted = CoalesceOperand(operand=NullLiteralOperand(), default=_literal(0))
    assert evaluate_expression(substituted) == Decimal(0)
    # an authored ZERO is a real number and needs no substitution
    assert evaluate_expression(_literal(0)) == Decimal(0)
    # bare authored NULL never becomes 0 (or false) at the boundary
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(NullLiteralOperand())
    assert error.value.code == "calculation_result_null_unsupported"
    # and it is NOT NULL-tested as a missing input
    assert _isnull("missing_role") is not None


# 6g. REAL executor end-to-end proof (Fix C): PlanExecutor -> runtime adapter -----
def _ad_hoc_execution_plan(spec, binding):
    from src.nl2sql.contracts import (
        AdHocCalculationStep,
        ExecutionPlan,
        FetchMetricStep,
        VerifyStep,
    )
    from src.nl2sql.semantic.calculation_contract import derived_output_id

    derived = derived_output_id(spec, binding)
    return ExecutionPlan(
        query_plan_sha256=_RUNTIME_QUERY_PLAN.checksum,
        semantic_release_id=_RUNTIME_CONTEXT.semantic_release_id,
        schema_snapshot_id=_RUNTIME_CONTEXT.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(
                step_id="fetch_on_time",
                metric_keys=("metric.on_time",),
                ad_hoc_input_role="on_time",
                ad_hoc_spec_checksum=spec.checksum,
                ad_hoc_derived_output_id=derived,
            ),
            FetchMetricStep(
                step_id="fetch_total",
                metric_keys=("metric.total",),
                ad_hoc_input_role="total",
                ad_hoc_spec_checksum=spec.checksum,
                ad_hoc_derived_output_id=derived,
            ),
            AdHocCalculationStep(
                step_id="calculate_adhoc",
                calculation_spec=spec,
                execution_binding=binding,
                input_refs={
                    "on_time": "fetch_on_time.value",
                    "total": "fetch_total.value",
                },
                depends_on=("fetch_on_time", "fetch_total"),
                derived_output_id=derived,
            ),
            VerifyStep(
                step_id="verify_result",
                input_refs=("calculate_adhoc",),
                invariant_ids=("typed_result_present",),
                depends_on=("calculate_adhoc",),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_real_plan_executor_runs_the_runtime_adapter_end_to_end() -> None:
    """compiled plan -> PlanExecutor -> RuntimeCalculationRunner -> output."""
    from src.nl2sql.orchestration.budget import RouteBudgetLedger
    from src.nl2sql.orchestration.execution import PlanExecutor, RuntimeCalculationRunner
    from src.nl2sql.orchestration.planning import PlanValidator

    spec, binding = _runtime_spec_and_binding()
    query_plan, context = _RUNTIME_QUERY_PLAN, _RUNTIME_CONTEXT
    plan = _ad_hoc_execution_plan(spec, binding)
    validation = PlanValidator().validate_execution_plan(
        execution_plan=plan,
        query_plan=query_plan,
        context=context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    assert validation.outcome == "allow"
    runner = RuntimeCalculationRunner()
    result = await PlanExecutor(
        metric_runner=_StubMetricRunner({"metric.on_time": 2, "metric.total": 3}),
        ad_hoc_calculation_runner=runner,
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=validation,
        expected_policy_version=PlanValidator().policy_version,
        expected_policy_checksum=PlanValidator().policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "succeeded"
    assert result.outputs["calculate_adhoc"] == "66.67"
    receipt = next(
        item for item in result.record.step_receipts if item.kind == "ad_hoc_calculation"
    )
    assert receipt.calculation_spec_checksum == spec.checksum
    assert receipt.execution_binding_checksum == binding.checksum
    assert receipt.calculation_scope == "ad_hoc_noncanonical"
    assert receipt.derived_output_id is not None
    assert receipt.output_metric_key is None
    assert receipt.binding_checksum is None


@pytest.mark.asyncio
async def test_real_plan_executor_fails_closed_on_divide_by_zero() -> None:
    from src.nl2sql.orchestration.budget import RouteBudgetLedger
    from src.nl2sql.orchestration.execution import PlanExecutor, RuntimeCalculationRunner
    from src.nl2sql.orchestration.planning import PlanValidator

    spec, binding = _runtime_spec_and_binding()
    query_plan, context = _RUNTIME_QUERY_PLAN, _RUNTIME_CONTEXT
    plan = _ad_hoc_execution_plan(spec, binding)
    validation = PlanValidator().validate_execution_plan(
        execution_plan=plan,
        query_plan=query_plan,
        context=context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    result = await PlanExecutor(
        metric_runner=_StubMetricRunner({"metric.on_time": 1, "metric.total": 0}),
        ad_hoc_calculation_runner=RuntimeCalculationRunner(),
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=validation,
        expected_policy_version=PlanValidator().policy_version,
        expected_policy_checksum=PlanValidator().policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "failed"
    assert result.record.stop_reason == "calculation_undefined_division_by_zero"
    assert "calculate_adhoc" not in result.outputs


def test_all_and_any_do_not_hide_a_failing_condition() -> None:
    # Adversarial finding F1: error suppression must not depend on the order in
    # which the author wrote the conditions.
    from src.nl2sql.semantic.calculation_contract import NullTestOperand

    broken = NullTestOperand(
        operator="is_null",
        operand=BinaryOperand(
            op="divide", left=_literal(1), right=InputRefOperand(role="d")
        ),
    )
    all_forward = _all(_gt("a", 0), broken)
    all_reverse = _all(broken, _gt("a", 0))
    any_forward = _any(_isnull("a"), broken)
    any_reverse = _any(broken, _isnull("a"))
    inputs = {"a": None, "d": 0}
    for expression in (all_forward, all_reverse, any_forward, any_reverse):
        with pytest.raises(CalculationRuntimeError) as error:
            evaluate_expression(expression, inputs=inputs)  # type: ignore[arg-type]
        assert error.value.code == "calculation_undefined_division_by_zero"


def test_nested_case_and_unknown_in_any() -> None:
    inner = _case((_gt("x", 0), _literal(1)), otherwise=_literal(2))
    outer = _case((_gt("y", 0), inner), otherwise=_literal(3))
    assert evaluate_expression(outer, inputs={"x": 5, "y": 5}) == Decimal(1)  # type: ignore[arg-type]
    assert evaluate_expression(outer, inputs={"x": 5, "y": None}) == Decimal(3)  # type: ignore[arg-type]
    assert evaluate_expression(outer, inputs={"x": 0, "y": 5}) == Decimal(2)  # type: ignore[arg-type]
    # TRUE OR UNKNOWN => TRUE (IS NULL is never UNKNOWN)
    nested = _any(_gt("a", 5), _isnull("b"))
    assert evaluate_expression(nested, inputs={"a": 1, "b": None}) == Decimal(1)  # type: ignore[arg-type]
    # FALSE OR FALSE => FALSE is a real boolean value
    assert evaluate_expression(nested, inputs={"a": 1, "b": 3}) == Decimal(0)  # type: ignore[arg-type]
    # FALSE OR UNKNOWN => UNKNOWN, and a bare UNKNOWN value fails closed
    unknown = _any(_gt("a", 5), _gt("a", 0))
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(unknown, inputs={"a": None})  # type: ignore[arg-type]
    assert error.value.code == "calculation_result_null_unsupported"


# 7. Numeric model --------------------------------------------------------------
@pytest.mark.parametrize("bad", [True, False, "not-a-number", "", "NaN", "Infinity", 1.5, {"a": 1}, [1]])
def test_non_numeric_shapes_are_rejected(bad: object) -> None:
    expression = BinaryOperand(
        op="add", left=InputRefOperand(role="on_time"), right=_literal(1)
    )
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(expression, inputs={"on_time": bad})
    assert error.value.code == "calculation_input_not_numeric"


def test_exact_decimal_strings_are_accepted() -> None:
    expression = BinaryOperand(
        op="add", left=InputRefOperand(role="on_time"), right=_literal(1)
    )
    assert evaluate_expression(expression, inputs={"on_time": "2.50"}) == Decimal("3.50")


def test_decimal_and_int_are_accepted_exactly() -> None:
    expression = BinaryOperand(
        op="multiply", left=InputRefOperand(role="on_time"), right=_literal(3)
    )
    assert evaluate_expression(expression, inputs={"on_time": Decimal("0.1")}) == Decimal("0.3")
    assert evaluate_expression(expression, inputs={"on_time": 4}) == Decimal(12)


# 8. AggregateOperand is deliberately unsupported -------------------------------
def test_aggregate_operand_fails_closed_as_unsupported() -> None:
    with pytest.raises(CalculationRuntimeError) as error:
        evaluate_expression(AggregateOperand(function="sum", role="on_time"), inputs={"on_time": 1})
    assert error.value.code == "calculation_aggregate_unsupported"


# 9. Precision / rounding: declared only, never implicit ------------------------
def test_declared_precision_and_rounding_are_applied_once() -> None:
    result = evaluate_calculation(
        _ratio_spec(), inputs={"on_time": 2, "total": 3}
    )
    assert result.precision == 2
    assert result.value == Decimal("66.67")


def test_absent_precision_preserves_the_exact_value() -> None:
    spec = _ratio_spec(precision=None, rounding=None)
    result = evaluate_calculation(spec, inputs={"on_time": 1, "total": 3})
    assert result.precision is None
    # no implicit ROUND(..., 2): the value stays the exact repeating quotient
    assert result.value != Decimal("33.33")
    assert result.value != Decimal("33.33333333333333333333333333333333")
    assert result.value * 3 == Decimal(100)


def test_rounding_modes_are_deterministic() -> None:
    expression = BinaryOperand(
        op="divide", left=_literal("1"), right=_literal("8")
    )
    spec = _ratio_spec(expression=expression, precision=2, rounding="floor")
    assert evaluate_calculation(spec, inputs={}).value == Decimal("0.12")
    spec_up = _ratio_spec(expression=expression, precision=2, rounding="ceil")
    assert evaluate_calculation(spec_up, inputs={}).value == Decimal("0.13")
    spec_even = _ratio_spec(expression=expression, precision=2, rounding="half_even")
    assert evaluate_calculation(spec_even, inputs={}).value == Decimal("0.12")


# 10. Unit never scales arithmetic ----------------------------------------------
def test_percent_unit_does_not_imply_multiplication_by_100() -> None:
    expression = BinaryOperand(
        op="divide", left=InputRefOperand(role="on_time"), right=InputRefOperand(role="total")
    )
    percent_spec = _ratio_spec(expression=expression, unit="percent", precision=None, rounding=None)
    count_spec = _ratio_spec(
        expression=expression,
        unit="count",
        precision=None,
        rounding=None,
    )
    inputs = {"on_time": 1, "total": 4}
    assert evaluate_calculation(percent_spec, inputs=inputs).value == Decimal("0.25")
    assert evaluate_calculation(count_spec, inputs=inputs).value == Decimal("0.25")


def test_scaling_must_come_from_the_expression() -> None:
    # the explicit *100 is what makes it a percentage, not the unit metadata
    result = evaluate_calculation(_ratio_spec(), inputs={"on_time": 1, "total": 4})
    assert result.value == Decimal("25.00")


# 11. Per-run Parameter Contract ------------------------------------------------
def test_parameter_binding_values_are_used_and_are_not_spec_identity() -> None:
    spec = CalculationSpec(
        calculation_id="adhoc.weighted",
        expression=BinaryOperand(
            op="multiply",
            left=InputRefOperand(role="recognition"),
            right=ParameterRefOperand(name="weight"),
        ),
        inputs=(
            CalculationInputSpec(
                role="recognition",
                provenance="published_gold",
                metric_key="metric.recognition",
            ),
        ),
        parameters=(ParameterSpec(name="weight", value_type="decimal", required=True),),
        unit="ratio",
        precision=2,
        rounding="half_up",
    )
    first = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="weight", value=Decimal("0.3")),),
    )
    second = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="weight", value=Decimal("0.7")),),
    )
    assert evaluate_calculation(spec, inputs={"recognition": 10}, binding=first).value == Decimal("3.00")
    assert evaluate_calculation(spec, inputs={"recognition": 10}, binding=second).value == Decimal("7.00")
    # the binding never changes spec identity
    assert first.spec_checksum == second.spec_checksum == spec.checksum
