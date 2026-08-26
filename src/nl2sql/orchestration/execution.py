"""Bounded executor for registered typed plan steps.

The executor never receives arbitrary callables from a plan.  Implementations
for metric SQL, trusted calculations, and verification are injected once by
the application and selected only by the step discriminator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal, Protocol, cast

from pydantic import JsonValue, TypeAdapter

from src.nl2sql.agents.dynamic_calc.trusted_templates import (
    TrustedTemplateError,
    TrustedTemplateRegistry,
    trusted_template_registry,
)
from src.nl2sql.contracts import (
    ContextBundle,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStep,
    PlanStepReceipt,
    QueryPlan,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.orchestration.budget import BudgetExceeded, RouteBudgetLedger

_JSON_VALUE = TypeAdapter(JsonValue)


class PlanStepError(RuntimeError):
    def __init__(self, code: str) -> None:
        normalized = code.strip()
        if not normalized:
            raise ValueError("plan step error code must be non-empty")
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True, slots=True)
class PreparedMetricStep:
    """Ephemeral compiled query handle; payload and SQL are never checkpointed."""

    sql_fingerprint: str
    join_hops: int
    payload: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            len(self.sql_fingerprint) != 64
            or set(self.sql_fingerprint) - set("0123456789abcdef")
        ):
            raise ValueError("prepared metric SQL fingerprint must be lowercase SHA-256")
        if self.join_hops < 0:
            raise ValueError("prepared metric join hops cannot be negative")


@dataclass(frozen=True, slots=True)
class MetricStepResult:
    value: JsonValue
    receipt: ExecutionReceipt


class MetricStepRunner(Protocol):
    """PR07A boundary: deterministic compile first, governed execution second."""

    async def prepare(
        self,
        *,
        step: FetchMetricStep,
        query_plan: QueryPlan,
        context: ContextBundle,
    ) -> PreparedMetricStep: ...

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
    ) -> MetricStepResult: ...


class TrustedCalculationRunner(Protocol):
    async def execute(
        self,
        *,
        step: TrustedCalculationStep,
        inputs: dict[str, JsonValue],
    ) -> JsonValue: ...


class ResultVerifier(Protocol):
    async def verify(
        self,
        *,
        step: VerifyStep,
        inputs: tuple[JsonValue, ...],
    ) -> JsonValue: ...


class RegistryTrustedCalculationRunner:
    """Adapter for source-controlled templates; independent from CodeAct mode."""

    def __init__(
        self,
        registry: TrustedTemplateRegistry = trusted_template_registry,
    ) -> None:
        self._registry = registry

    async def execute(
        self,
        *,
        step: TrustedCalculationStep,
        inputs: dict[str, JsonValue],
    ) -> JsonValue:
        try:
            output = self._registry.execute(step.template_id, cast(dict[str, Any], inputs))
        except TrustedTemplateError as exc:
            raise PlanStepError("trusted_calculation_failed") from exc
        return _json_value(output.model_dump(mode="json"))


class TypedResultVerifier:
    """Small deterministic verifier registry used until PR07A adds domain invariants."""

    _supported = frozenset({"typed_result_present"})

    async def verify(
        self,
        *,
        step: VerifyStep,
        inputs: tuple[JsonValue, ...],
    ) -> JsonValue:
        if set(step.invariant_ids) - self._supported:
            raise PlanStepError("result_invariant_unregistered")
        if not inputs or any(value is None for value in inputs):
            raise PlanStepError("typed_result_missing")
        return _json_value(
            {
                "verified": True,
                "invariant_ids": list(step.invariant_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class PlanExecutionResult:
    """Outputs are request-local only; only ``record`` belongs in a checkpoint."""

    record: PlanExecutionRecord
    outputs: dict[str, JsonValue]


class PlanExecutor:
    def __init__(
        self,
        *,
        metric_runner: MetricStepRunner,
        trusted_calculation_runner: TrustedCalculationRunner | None = None,
        result_verifier: ResultVerifier | None = None,
    ) -> None:
        self._metric_runner = metric_runner
        self._trusted_calculation_runner = trusted_calculation_runner
        self._result_verifier = result_verifier or TypedResultVerifier()

    async def execute(
        self,
        *,
        query_plan: QueryPlan,
        context: ContextBundle,
        execution_plan: ExecutionPlan,
        budget: RouteBudgetLedger,
        deadline_ms: int,
    ) -> PlanExecutionResult:
        # Deep snapshots close mutation windows in nested JSON filter/ref values.
        query_plan = QueryPlan.model_validate_json(query_plan.model_dump_json())
        context = ContextBundle.model_validate_json(context.model_dump_json())
        execution_plan = ExecutionPlan.model_validate_json(
            execution_plan.model_dump_json()
        )
        mismatch = _execution_plan_mismatch(query_plan, context, execution_plan)
        if mismatch is not None:
            budget.halt(mismatch)
            return _failed_result(execution_plan, mismatch)
        if deadline_ms < 1:
            budget.halt("execution_deadline_unavailable")
            return _failed_result(
                execution_plan,
                "execution_deadline_unavailable",
                status="deadline_exceeded",
            )
        timeout_ms = (
            min(deadline_ms, budget.limits.deadline_ms) - budget.policy.reserve_ms
        )
        if timeout_ms < 1:
            budget.halt("deadline_reserve")
            return _failed_result(
                execution_plan,
                "deadline_reserve",
                status="deadline_exceeded",
            )

        outputs: dict[str, JsonValue] = {}
        receipts: list[PlanStepReceipt] = []
        current_step: PlanStep | None = None
        current_started = 0.0
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                for current_step in _ordered_steps(execution_plan):
                    current_started = perf_counter()
                    try:
                        output = await self._execute_step(
                            step=current_step,
                            query_plan=query_plan,
                            context=context,
                            outputs=outputs,
                            budget=budget,
                            timeout_ms=timeout_ms,
                        )
                    except asyncio.CancelledError:
                        raise
                    except BudgetExceeded as exc:
                        code = budget.stop_reason or budget.halt(str(exc))
                        receipts.append(_failed_step(current_step, current_started, code))
                        return PlanExecutionResult(
                            record=PlanExecutionRecord(
                                execution_plan_checksum=execution_plan.checksum,
                                status="failed",
                                step_receipts=tuple(receipts),
                                output_step_ids=tuple(outputs),
                                stop_reason=code,
                            ),
                            outputs=outputs,
                        )
                    except PlanStepError as exc:
                        code = budget.halt(exc.code)
                        receipts.append(_failed_step(current_step, current_started, code))
                        return PlanExecutionResult(
                            record=PlanExecutionRecord(
                                execution_plan_checksum=execution_plan.checksum,
                                status="failed",
                                step_receipts=tuple(receipts),
                                output_step_ids=tuple(outputs),
                                stop_reason=code,
                            ),
                            outputs=outputs,
                        )
                    except Exception:
                        code = budget.halt("plan_step_failed")
                        receipts.append(_failed_step(current_step, current_started, code))
                        return PlanExecutionResult(
                            record=PlanExecutionRecord(
                                execution_plan_checksum=execution_plan.checksum,
                                status="failed",
                                step_receipts=tuple(receipts),
                                output_step_ids=tuple(outputs),
                                stop_reason=code,
                            ),
                            outputs=outputs,
                        )
                    outputs[current_step.step_id] = output
                    receipts.append(
                        PlanStepReceipt(
                            step_id=current_step.step_id,
                            kind=current_step.kind,
                            status="succeeded",
                            elapsed_ms=_elapsed_ms(current_started),
                            output_digest=_json_digest(output),
                        )
                    )
        except TimeoutError:
            code = budget.halt("execution_deadline_exceeded")
            if current_step is not None and not any(
                receipt.step_id == current_step.step_id for receipt in receipts
            ):
                receipts.append(_failed_step(current_step, current_started, code))
            return PlanExecutionResult(
                record=PlanExecutionRecord(
                    execution_plan_checksum=execution_plan.checksum,
                    status="deadline_exceeded",
                    step_receipts=tuple(receipts),
                    output_step_ids=tuple(outputs),
                    stop_reason=code,
                ),
                outputs=outputs,
            )

        return PlanExecutionResult(
            record=PlanExecutionRecord(
                execution_plan_checksum=execution_plan.checksum,
                status="succeeded",
                step_receipts=tuple(receipts),
                output_step_ids=tuple(outputs),
            ),
            outputs=outputs,
        )

    async def _execute_step(
        self,
        *,
        step: PlanStep,
        query_plan: QueryPlan,
        context: ContextBundle,
        outputs: dict[str, JsonValue],
        budget: RouteBudgetLedger,
        timeout_ms: int,
    ) -> JsonValue:
        if isinstance(step, FetchMetricStep):
            prepared = await self._metric_runner.prepare(
                step=step,
                query_plan=query_plan,
                context=context,
            )
            stop_reason = budget.record_sql_candidate(prepared.sql_fingerprint)
            if stop_reason is not None:
                raise BudgetExceeded(stop_reason)
            budget.observe_join_hops(prepared.join_hops)
            budget.begin_sql_execution()
            result = await self._metric_runner.execute(prepared, timeout_ms=timeout_ms)
            if result.receipt.sql_fingerprint != prepared.sql_fingerprint:
                raise PlanStepError("execution_receipt_fingerprint_mismatch")
            if not result.receipt.policy_version.strip():
                raise PlanStepError("execution_receipt_policy_missing")
            if (
                not result.receipt.datasource.strip()
                or not result.receipt.readonly_role.strip()
            ):
                raise PlanStepError("execution_receipt_identity_missing")
            if (
                result.receipt.policy_outcome != "allow"
                or result.receipt.error_taxonomy is not None
            ):
                raise PlanStepError(
                    result.receipt.error_taxonomy or "query_gateway_policy_denied"
                )
            return _json_value(result.value)

        if isinstance(step, TrustedCalculationStep):
            if self._trusted_calculation_runner is None:
                raise PlanStepError("trusted_calculation_unavailable")
            inputs = {
                name: _resolve_ref(reference, outputs)
                for name, reference in step.input_refs.items()
            }
            output = await self._trusted_calculation_runner.execute(
                step=step,
                inputs=inputs,
            )
            return _json_value(output)

        if isinstance(step, VerifyStep):
            inputs = tuple(_resolve_ref(reference, outputs) for reference in step.input_refs)
            output = await self._result_verifier.verify(step=step, inputs=inputs)
            return _json_value(output)

        raise PlanStepError("execution_plan_step_unregistered")


def _execution_plan_mismatch(
    query_plan: QueryPlan,
    context: ContextBundle,
    execution_plan: ExecutionPlan,
) -> str | None:
    if execution_plan.query_plan_sha256 != query_plan.checksum:
        return "execution_plan_query_hash_mismatch"
    if execution_plan.semantic_release_id != context.semantic_release_id:
        return "execution_plan_semantic_release_mismatch"
    if execution_plan.schema_snapshot_id != context.schema_snapshot_id:
        return "execution_plan_schema_snapshot_mismatch"
    return None


def _ordered_steps(execution_plan: ExecutionPlan) -> tuple[PlanStep, ...]:
    by_id = {step.step_id: step for step in execution_plan.steps}
    resolved: set[str] = set()
    ordered: list[PlanStep] = []
    while len(ordered) < len(by_id):
        ready = sorted(
            step_id
            for step_id, step in by_id.items()
            if step_id not in resolved and set(step.depends_on) <= resolved
        )
        if not ready:
            raise PlanStepError("execution_plan_dependency_cycle")
        for step_id in ready:
            resolved.add(step_id)
            ordered.append(by_id[step_id])
    return tuple(ordered)


def _resolve_ref(reference: str, outputs: dict[str, JsonValue]) -> JsonValue:
    parts = reference.split(".")
    if not parts or parts[0] not in outputs:
        raise PlanStepError("execution_input_ref_missing")
    value: JsonValue = outputs[parts[0]]
    for part in parts[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise PlanStepError("execution_input_ref_missing")
    return value


def _json_value(value: object) -> JsonValue:
    try:
        normalized = cast(JsonValue, _JSON_VALUE.validate_python(value))
        payload = json.dumps(normalized, allow_nan=False, ensure_ascii=False)
        return cast(JsonValue, json.loads(payload))
    except Exception as exc:
        raise PlanStepError("plan_step_output_not_json") from exc


def _json_digest(value: JsonValue) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _failed_step(step: PlanStep, started: float, code: str) -> PlanStepReceipt:
    return PlanStepReceipt(
        step_id=step.step_id,
        kind=step.kind,
        status="failed",
        elapsed_ms=_elapsed_ms(started),
        error_code=code,
    )


def _failed_result(
    execution_plan: ExecutionPlan,
    code: str,
    *,
    status: Literal["failed", "deadline_exceeded"] = "failed",
) -> PlanExecutionResult:
    return PlanExecutionResult(
        record=PlanExecutionRecord(
            execution_plan_checksum=execution_plan.checksum,
            status=status,
            stop_reason=code,
        ),
        outputs={},
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
