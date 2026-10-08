"""Pure deterministic typed calculation evaluator (migration foundation).

This module migrates the OLD calculation-algorithm responsibility into the typed
architecture.  It is PURE: no I/O, no SQL, no database, no catalog, no authority
and no routing.  It never decides product policy.

Migration classification (behaviour/test-family level, see MASTER_PR_PLAN_V4.md
and the legacy Gold reference implementation):

* typed deterministic expression evaluation  -> ADAPT_MIGRATE
  (the legacy responsibility of gold/metric_system/engine/expression.py, but over
  the bounded typed Expression tree instead of generated SQL text)
* divide-by-zero -> calculation undefined    -> INTENTIONAL_REWRITE
  (legacy: engine/expression.py injects NULLIF(denominator, 0) -> NULL ->
  engine/status.py maps None -> NO_DATA.  V4 forbids that collapse.)
* zero as a real value                       -> DIRECT_MIGRATE
* explicit Decimal arithmetic                -> ADAPT_MIGRATE
  (legacy relied on PostgreSQL NUMERIC; V4 computes in-process)
* AggregateOperand runtime semantics         -> NEW_BUILD (deliberately absent)

Frozen rules honoured here: numeric zero is the real value 0 and is never
no-data; no-data is a separate state; divide-by-zero is mathematical undefined/
calculation error and is NOT no-data; unit metadata never scales arithmetic.
"""

from __future__ import annotations

from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    localcontext,
)
from typing import Any, Final

from src.nl2sql.semantic.calculation_contract import (
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_NODES,
    AggregateOperand,
    AllOperand,
    AnyOperand,
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationSpec,
    CaseOperand,
    CoalesceOperand,
    CompareOperand,
    Expression,
    InputRefOperand,
    LiteralOperand,
    NullLiteralOperand,
    NullTestOperand,
    ParameterRefOperand,
    RoundingPolicy,
    RoundOperand,
    expression_depth,
    expression_node_count,
)

__all__ = [
    "CalculationRuntimeError",
    "EvaluationResult",
    "evaluate_calculation",
    "evaluate_expression",
    "EXACT_CONTEXT_PRECISION",
]

# Working precision for exact intermediate arithmetic.  Deliberately generous:
# it is an implementation detail, never a business precision, and it never
# substitutes for CalculationSpec.precision.
EXACT_CONTEXT_PRECISION: Final[int] = 64

_ROUNDING_MODES: Final[dict[str, str]] = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "half_down": ROUND_HALF_DOWN,
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
}


class CalculationRuntimeError(RuntimeError):
    """Stable, typed, secret-free calculation failure.

    The code is the contract; nothing about the inputs is echoed, so the error
    can cross a checkpoint boundary without leaking business values.
    """

    def __init__(self, code: str) -> None:
        normalized = code.strip()
        if not normalized:
            raise ValueError("calculation error code must be non-empty")
        super().__init__(normalized)
        self.code = normalized


class EvaluationResult:
    """The exact deterministic value plus the precision actually applied.

    VALUE and STATUS stay separate (legacy models.py/status.py separation): this
    object describes a SUCCESSFUL evaluation only.  Undefined/unsupported
    calculations raise instead of returning a sentinel, because numeric
    sentinels must never stand in for status.
    """

    __slots__ = ("value", "precision")

    def __init__(self, value: Decimal, precision: int | None) -> None:
        self.value = value
        self.precision = precision

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EvaluationResult):
            return NotImplemented
        return (self.value, self.precision) == (other.value, other.precision)

    def __repr__(self) -> str:  # pragma: no cover - debug affordance
        return f"EvaluationResult(value={self.value!r}, precision={self.precision!r})"


def _coerce_numeric(value: Any, *, code: str) -> Decimal:
    """Coerce a resolved scalar to an exact finite Decimal.

    Plain ints and decimals are exact.  Strings are accepted only when they are
    an exact decimal representation, because the shipped dependency-fetch wire
    contract already delivers 2-place decimal STRINGS (see metric_query).  A
    binary float is never the business source of truth, so it is rejected.
    """

    if isinstance(value, bool):
        # bool is an int subclass; it is never a business number.
        raise CalculationRuntimeError(code)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CalculationRuntimeError(code)
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise CalculationRuntimeError(code)
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise CalculationRuntimeError(code) from exc
        if not parsed.is_finite():
            raise CalculationRuntimeError(code)
        return parsed
    raise CalculationRuntimeError(code)


def _decimal_error_code(exc: DecimalException) -> str:
    """One stable, secret-free code per Decimal arithmetic trap family."""

    if isinstance(exc, DivisionByZero):
        return "calculation_undefined_division_by_zero"
    if isinstance(exc, InvalidOperation):
        return "calculation_undefined"
    return "calculation_numeric_out_of_range"


def _require_bounded(expression: Expression) -> None:
    """Enforce the SAME bounded shape the CalculationSpec contract enforces.

    A raw Expression handed straight to a public entry point must not bypass the
    contract depth/node limits and reach unbounded recursion.
    """

    try:
        depth = expression_depth(expression)
        if depth > MAX_EXPRESSION_DEPTH:
            raise CalculationRuntimeError("calculation_expression_too_deep")
        if expression_node_count(expression) > MAX_EXPRESSION_NODES:
            raise CalculationRuntimeError("calculation_expression_too_large")
    except CalculationRuntimeError:
        raise
    except (ValueError, RecursionError) as exc:
        # The shared walkers raise ValueError past the node budget; a raw caller
        # must still see one stable typed failure.
        raise CalculationRuntimeError("calculation_expression_too_large") from exc


def _apply_precision(value: Decimal, precision: int, rounding: RoundingPolicy) -> Decimal:
    """Apply the DECLARED precision (decimal places) and rounding mode once."""

    quantum = Decimal(1).scaleb(-precision)
    return value.quantize(quantum, rounding=_ROUNDING_MODES[rounding])


class _NullOperand(Exception):
    """Internal three-valued marker: this sub-expression evaluated to NULL.

    A NULL operand is NOT no-data and NOT an error; it is the absence of a
    value.  It only becomes an outcome at the expression boundary: an explicit
    COALESCE can substitute it, an explicit CASE can branch on it, and an
    unhandled NULL fails closed.
    """


_TRUE = Decimal(1)
_FALSE = Decimal(0)

# SQL-like three-valued CONDITION state.  This is internal only: UNKNOWN never
# becomes a business status, a no_data, a calculation error or a numeric zero
# outside condition context.
_UNKNOWN: Final[int] = -1
_CONDITION_TRUE: Final[int] = 1
_CONDITION_FALSE: Final[int] = 0


def _condition(
    expression: Expression,
    *,
    inputs: dict[str, object],
    parameters: dict[str, object],
) -> int:
    """Evaluate one authored CONDITION as TRUE / FALSE / UNKNOWN."""

    if isinstance(expression, NullTestOperand):
        is_null = _is_null(expression.operand, inputs=inputs, parameters=parameters)
        matched = is_null if expression.operator == "is_null" else not is_null
        return _CONDITION_TRUE if matched else _CONDITION_FALSE

    if isinstance(expression, CompareOperand):
        try:
            left = _evaluate(expression.left, inputs=inputs, parameters=parameters)
            right = _evaluate(expression.right, inputs=inputs, parameters=parameters)
        except _NullOperand:
            # SQL: comparing with NULL yields UNKNOWN, not FALSE and not an error.
            return _UNKNOWN
        matched = left > right if expression.operator == "gt" else left <= right
        return _CONDITION_TRUE if matched else _CONDITION_FALSE

    if isinstance(expression, AllOperand):
        # EVERY condition is evaluated before the result is decided, so a
        # failing condition is never hidden by an earlier short-circuit.  The
        # outcome must not depend on author-written condition order.
        states = [
            _condition(child, inputs=inputs, parameters=parameters)
            for child in expression.conditions
        ]
        if any(state == _CONDITION_FALSE for state in states):
            return _CONDITION_FALSE
        return _UNKNOWN if _UNKNOWN in states else _CONDITION_TRUE

    if isinstance(expression, AnyOperand):
        states = [
            _condition(child, inputs=inputs, parameters=parameters)
            for child in expression.conditions
        ]
        if any(state == _CONDITION_TRUE for state in states):
            return _CONDITION_TRUE
        return _UNKNOWN if _UNKNOWN in states else _CONDITION_FALSE

    raise CalculationRuntimeError("calculation_condition_unsupported")


def _is_null(
    expression: Expression,
    *,
    inputs: dict[str, object],
    parameters: dict[str, object],
) -> bool:
    try:
        _evaluate(expression, inputs=inputs, parameters=parameters)
    except _NullOperand:
        return True
    return False


def _evaluate(
    expression: Expression,
    *,
    inputs: dict[str, object],
    parameters: dict[str, object],
) -> Decimal:
    if isinstance(expression, LiteralOperand):
        return expression.value

    if isinstance(expression, NullLiteralOperand):
        # An explicitly AUTHORED NULL result.  It is a value-state, never 0,
        # never no_data, never false and never a failure.  Existing expression
        # semantics then apply (COALESCE may substitute it; an unhandled NULL
        # fails closed at the boundary).
        raise _NullOperand

    if isinstance(expression, InputRefOperand):
        if expression.role not in inputs:
            raise CalculationRuntimeError("calculation_input_missing")
        value = inputs[expression.role]
        if value is None:
            # An absent value is NULL.  No global NULL policy is invented: it
            # only becomes meaningful through an explicit COALESCE/CASE, and
            # otherwise fails closed at the boundary.
            raise _NullOperand
        return _coerce_numeric(value, code="calculation_input_not_numeric")

    if isinstance(expression, ParameterRefOperand):
        if expression.name not in parameters:
            raise CalculationRuntimeError("calculation_parameter_missing")
        value = parameters[expression.name]
        if value is None:
            raise _NullOperand
        return _coerce_numeric(value, code="calculation_parameter_not_numeric")

    if isinstance(expression, AggregateOperand):
        # No runtime aggregate semantics exist in V1: the seam supplies exactly
        # one already-aggregated scalar per role, so the function is a
        # declaration, not a runtime instruction.  Inventing row-level
        # aggregation here would re-enter the database.
        raise CalculationRuntimeError("calculation_aggregate_unsupported")

    if isinstance(expression, RoundOperand):
        # Explicit ROUND authored in the expression.  This is NOT the spec-level
        # precision: it is part of the business formula and is applied here.
        inner = _evaluate(expression.operand, inputs=inputs, parameters=parameters)
        return _apply_precision(inner, expression.digits, "half_up")

    if isinstance(expression, CoalesceOperand):
        # Explicit NULL substitution.  The default is an author-stated literal,
        # never a runtime/channel default.
        try:
            return _evaluate(expression.operand, inputs=inputs, parameters=parameters)
        except _NullOperand:
            return expression.default.value

    if isinstance(expression, (NullTestOperand, CompareOperand, AllOperand, AnyOperand)):
        # A bare condition used as a VALUE is only defined when it is TRUE or
        # FALSE; UNKNOWN has no value-level meaning and fails closed.
        state = _condition(expression, inputs=inputs, parameters=parameters)
        if state == _UNKNOWN:
            raise _NullOperand
        return _TRUE if state == _CONDITION_TRUE else _FALSE

    if isinstance(expression, CaseOperand):
        for branch in expression.whens:
            # ONLY a TRUE condition selects a branch; FALSE and UNKNOWN both
            # fall through, exactly as SQL CASE does.
            if (
                _condition(branch.condition, inputs=inputs, parameters=parameters)
                == _CONDITION_TRUE
            ):
                return _evaluate(branch.then, inputs=inputs, parameters=parameters)
        if expression.otherwise is None:
            # An absent ELSE yields NULL, exactly as the legacy CASE does.
            raise _NullOperand
        return _evaluate(expression.otherwise, inputs=inputs, parameters=parameters)

    if isinstance(expression, BinaryOperand):
        left = _evaluate(expression.left, inputs=inputs, parameters=parameters)
        right = _evaluate(expression.right, inputs=inputs, parameters=parameters)
        if expression.op == "add":
            return left + right
        if expression.op == "subtract":
            return left - right
        if expression.op == "multiply":
            return left * right
        if expression.op == "divide":
            if right == 0:
                # INTENTIONAL_REWRITE: never NULL, never no_data, never a
                # sentinel.  Numeric zero is a real value, so an exact-zero
                # denominator is a mathematically undefined calculation.
                raise CalculationRuntimeError(
                    "calculation_undefined_division_by_zero"
                )
            return left / right
        raise CalculationRuntimeError("calculation_operator_unsupported")

    raise CalculationRuntimeError("calculation_expression_unsupported")


def evaluate_expression(
    expression: Expression,
    *,
    inputs: dict[str, object] | None = None,
    parameters: dict[str, object] | None = None,
) -> Decimal:
    """Evaluate one typed expression tree exactly, without spec-level policy.

    The public entry points enforce the SAME bounded shape the contract does, so
    a raw unbounded tree cannot reach the evaluator by this route.
    """

    _require_bounded(expression)
    with localcontext() as context:
        context.prec = EXACT_CONTEXT_PRECISION
        try:
            return _evaluate(
                expression,
                inputs=dict(inputs or {}),
                parameters=dict(parameters or {}),
            )
        except _NullOperand as exc:
            # An unhandled NULL reaching the boundary has no frozen business
            # meaning.  Fail closed; never coerce to 0 and never emit no_data.
            raise CalculationRuntimeError("calculation_result_null_unsupported") from exc
        except DecimalException as exc:
            # Every Decimal arithmetic trap (division by zero, invalid operation,
            # overflow, underflow, subnormal, clamp) becomes one stable typed
            # failure.  No raw decimal exception may escape this boundary.
            raise CalculationRuntimeError(_decimal_error_code(exc)) from exc
        except RecursionError as exc:  # pragma: no cover - bounded above
            raise CalculationRuntimeError("calculation_expression_too_deep") from exc


def evaluate_calculation(
    spec: CalculationSpec,
    *,
    inputs: dict[str, object] | None = None,
    binding: CalculationExecutionBinding | None = None,
) -> EvaluationResult:
    """Evaluate one CalculationSpec under its DECLARED precision/rounding.

    Precondition (caller-owned): a binding, when supplied, must already belong to
    this spec.  Binding identity is NOT re-proved here.
    """

    parameters: dict[str, object] = {}
    if binding is not None:
        parameters = {item.name: item.value for item in binding.parameters}

    _require_bounded(spec.expression)
    with localcontext() as context:
        context.prec = EXACT_CONTEXT_PRECISION
        try:
            exact = _evaluate(spec.expression, inputs=dict(inputs or {}), parameters=parameters)
            if spec.precision is None:
                # No implicit precision and no implicit ROUND(..., 2): preserve
                # the exact decimal semantic value.
                return EvaluationResult(exact, None)
            rounding: RoundingPolicy = spec.rounding or "half_up"
            # The declared quantization is INSIDE the trap boundary: it is the
            # same class of Decimal operation as the arithmetic above.
            return EvaluationResult(
                _apply_precision(exact, spec.precision, rounding), spec.precision
            )
        except _NullOperand as exc:
            # An unhandled NULL reaching the boundary has no frozen business
            # meaning.  Fail closed; never coerce to 0 and never emit no_data.
            raise CalculationRuntimeError("calculation_result_null_unsupported") from exc
        except DecimalException as exc:
            raise CalculationRuntimeError(_decimal_error_code(exc)) from exc
        except RecursionError as exc:  # pragma: no cover - bounded above
            raise CalculationRuntimeError("calculation_expression_too_deep") from exc
