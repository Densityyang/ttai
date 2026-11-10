"""Bounded executor for registered typed plan steps.

The executor never receives arbitrary callables from a plan.  Implementations
for metric SQL, trusted calculations, and verification are injected once by
the application and selected only by the step discriminator.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Mapping
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
    AdHocCalculationStep,
    ContextBundle,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStep,
    PlanStepReceipt,
    PlanValidationRecord,
    QueryPlan,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.orchestration.approved_compute import ApprovedCalculationCatalog
from src.nl2sql.orchestration.budget import BudgetExceeded, RouteBudgetLedger
from src.nl2sql.orchestration.evidence_digest import (
    canonical_json_digest,
    canonical_rowset_digest,
)
from src.nl2sql.orchestration.verification_policy import verify_execution_plan_policy
from src.nl2sql.semantic.calculation_contract import CalculationSpec
from src.nl2sql.semantic.calculation_runtime import (
    CalculationRuntimeError,
    evaluate_calculation,
)

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
    # Request-local mode: True when this fetch serves one trusted calculation
    # input role and must project a typed scalar instead of a rowset.
    dependency_fetch: bool = False
    # Optional request-local semantic evidence.  It is ephemeral and never
    # serialized into the public response or checkpoint.
    semantic_manifest: object | None = field(default=None, repr=False, compare=False)
    semantic_record: object | None = field(default=None, repr=False, compare=False)
    semantic_step: object | None = field(default=None, repr=False, compare=False)
    semantic_execution_plan: object | None = field(default=None, repr=False, compare=False)
    semantic_root_query_plan: object | None = field(default=None, repr=False, compare=False)

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
    # Request-local validated manifest for grounding; never checkpointed.
    semantic_manifest: object | None = None
    # Request-local allow record paired with the manifest. Checkpoint receipt
    # fields are evidence only; grounding rebinds them to this server-side pair.
    semantic_validation_record: object | None = None
    # Request-local CandidateVerifier result. It is paired with the exact
    # Gateway receipt and consumed by grounding; it is never checkpointed.
    candidate_receipt: object | None = None


@dataclass(frozen=True, slots=True)
class RequestOutputContract:
    """Trusted, request-local semantic authority for one fetch output."""

    manifest: object
    semantic_validation_record: object
    # Gateway fingerprint uses a distinct canonicalizer from the manifest's
    # semantic fingerprint. Keep the request-local gateway value for an exact
    # checkpoint-receipt comparison instead of comparing unlike hashes.
    gateway_parameter_binding_fingerprint: str | None
    gateway_execution_receipt: ExecutionReceipt | None = None
    candidate_receipt: object | None = None


class MetricStepRunner(Protocol):
    """PR07A boundary: deterministic compile first, governed execution second."""

    async def prepare(
        self,
        *,
        step: FetchMetricStep,
        query_plan: QueryPlan,
        context: ContextBundle,
        execution_plan: ExecutionPlan | None = None,
    ) -> PreparedMetricStep: ...

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
        trace_id: str = "",
    ) -> MetricStepResult: ...


class TrustedCalculationRunner(Protocol):
    async def execute(
        self,
        *,
        step: TrustedCalculationStep,
        inputs: dict[str, JsonValue],
    ) -> JsonValue: ...


class AdHocCalculationRunner(Protocol):
    """NONCANONICAL AD_HOC execution seam.

    Receives ONLY the validated step and resolved JSON-safe scalar inputs: no
    raw SQL, rowset, authorization object or catalog authority object.  V1 has
    no production runner, so an AdHocCalculationStep fails closed by default.
    """

    async def execute(
        self,
        *,
        step: AdHocCalculationStep,
        inputs: dict[str, JsonValue],
    ) -> JsonValue: ...


class RuntimeCalculationRunner:
    """The ONE reusable adapter from a validated step to the shared evaluator.

    It performs NO arithmetic of its own and holds NO authority, catalog,
    gateway or context state: it projects the step into the pure typed evaluator
    and
    maps its stable error codes onto PlanStepError.  Future AD_HOC, Custom
    Definition and Published wrappers all reuse this single implementation, so
    arithmetic semantics exist in exactly one place.
    """

    async def execute(
        self,
        *,
        step: AdHocCalculationStep,
        inputs: dict[str, JsonValue],
    ) -> JsonValue:
        # The step contract already proved binding<->spec identity, checksum
        # agreement and that input_refs are exactly the referenced roles.
        try:
            result = evaluate_calculation(
                step.calculation_spec,
                inputs=cast("dict[str, object]", dict(inputs)),
                binding=step.execution_binding,
            )
        except CalculationRuntimeError as exc:
            # 1:1 mapping: never swallow, never substitute a default value.
            raise PlanStepError(exc.code) from exc
        # Deterministic, lossless wire form for an exact decimal.
        return str(result.value)


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
    """Deterministic verifier for the typed execution result boundary.

    ``typed_result_present`` is intentionally a small invariant, but presence
    is not satisfied by an arbitrary nested object or an unbounded rowset.  The
    verifier validates the JSON shape that later grounding is allowed to read;
    domain-specific cardinality remains bound to the execution manifest and is
    checked by the metric runner/grounding seam.
    """

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
        for value in inputs:
            if isinstance(value, Mapping):
                mapping = cast("Mapping[str, JsonValue]", value)
                if not mapping:
                    raise PlanStepError("typed_result_shape_invalid")
                if "rows" in mapping:
                    rows = mapping.get("rows")
                    no_data = mapping.get("no_data")
                    if not isinstance(rows, list) or any(
                        not isinstance(row, Mapping) for row in rows
                    ) or not isinstance(no_data, bool):
                        raise PlanStepError("typed_result_shape_invalid")
                    if (no_data and rows) or (not no_data and not rows):
                        raise PlanStepError("typed_result_shape_invalid")
                    for row in rows:
                        row_mapping = cast("Mapping[str, JsonValue]", row)
                        if any(
                            not _valid_typed_result_scalar(item)
                            for item in row_mapping.values()
                        ):
                            raise PlanStepError("typed_result_shape_invalid")
                elif any(not _valid_typed_result_scalar(item) for item in mapping.values()):
                    raise PlanStepError("typed_result_shape_invalid")
            elif not _valid_typed_result_scalar(value):
                raise PlanStepError("typed_result_shape_invalid")
        return _json_value(
            {
                "verified": True,
                "invariant_ids": list(step.invariant_ids),
            }
        )


def _valid_typed_result_scalar(value: JsonValue) -> bool:
    if value is None or isinstance(value, (dict, list)):
        return False
    if type(value) is float:
        return math.isfinite(value)
    return True


@dataclass(frozen=True, slots=True)
class PlanExecutionResult:
    """Outputs are request-local only; only ``record`` belongs in a checkpoint."""

    record: PlanExecutionRecord
    outputs: dict[str, JsonValue]
    output_contracts: dict[str, RequestOutputContract] = field(default_factory=dict)
    require_semantic_contract: bool = False


def validate_execution_record_against_plan(
    execution_plan: ExecutionPlan,
    execution_record: PlanExecutionRecord,
) -> None:
    """Validate checkpoint-safe receipts against the actual execution plan.

    This is the single plan-bound receipt gate.  Failed and deadline-exceeded
    records may be partial, but every receipt they do carry must still belong
    to this plan and have the plan-declared step kind.  A successful record is
    complete and must include a successful terminal verification receipt.
    """

    if execution_record.execution_plan_checksum != execution_plan.checksum:
        raise PlanStepError("execution_record_plan_checksum_mismatch")

    plan_steps = {step.step_id: step for step in execution_plan.steps}
    receipts = tuple(execution_record.step_receipts)
    receipt_ids = tuple(receipt.step_id for receipt in receipts)
    if len(receipt_ids) != len(set(receipt_ids)):
        raise PlanStepError("execution_record_step_coverage_mismatch")

    for receipt in receipts:
        step = plan_steps.get(receipt.step_id)
        if step is None:
            raise PlanStepError("execution_record_unknown_step")
        if receipt.kind != step.kind:
            raise PlanStepError("execution_record_step_kind_mismatch")

    if execution_record.status != "succeeded":
        return

    policy_failures = verify_execution_plan_policy(execution_plan)
    if policy_failures:
        raise PlanStepError(policy_failures[0])

    verify_steps = [step for step in execution_plan.steps if isinstance(step, VerifyStep)]
    if len(verify_steps) != 1:
        raise PlanStepError("execution_record_verify_missing")
    verify_receipt = next(
        (receipt for receipt in receipts if receipt.step_id == verify_steps[0].step_id),
        None,
    )
    if verify_receipt is None or verify_receipt.status != "succeeded":
        raise PlanStepError("execution_record_verify_missing")

    required_ids = set(plan_steps)
    if set(receipt_ids) != required_ids or any(
        receipt.status != "succeeded" for receipt in receipts
    ):
        raise PlanStepError("execution_record_step_coverage_mismatch")


class PlanExecutor:
    def __init__(
        self,
        *,
        metric_runner: MetricStepRunner,
        trusted_calculation_runner: TrustedCalculationRunner | None = None,
        ad_hoc_calculation_runner: AdHocCalculationRunner | None = None,
        result_verifier: ResultVerifier | None = None,
        calculation_catalog: ApprovedCalculationCatalog | None = None,
        require_semantic_contract: bool = False,
        allow_legacy_unitless_grounding: bool = False,
    ) -> None:
        self._metric_runner = metric_runner
        self._trusted_calculation_runner = trusted_calculation_runner
        # V1 default is None: an AD_HOC calculation step fails closed until a
        # production arithmetic runner is separately authorized.
        self._ad_hoc_calculation_runner = ad_hoc_calculation_runner
        self._result_verifier = result_verifier or TypedResultVerifier()
        self._require_semantic_contract = require_semantic_contract
        self._allow_legacy_unitless_grounding = allow_legacy_unitless_grounding
        if calculation_catalog is not None:
            self._calculation_catalog = calculation_catalog

    async def execute(
        self,
        *,
        query_plan: QueryPlan,
        context: ContextBundle,
        execution_plan: ExecutionPlan,
        validation: PlanValidationRecord,
        expected_policy_version: str,
        expected_policy_checksum: str,
        budget: RouteBudgetLedger,
        deadline_ms: int,
        trace_id: str = "",
    ) -> PlanExecutionResult:
        # Deep snapshots close mutation windows in nested JSON filter/ref values.
        from src.nl2sql.contracts import query_plan_payload

        query_plan = QueryPlan.model_validate(query_plan_payload(query_plan))
        context = ContextBundle.model_validate_json(context.model_dump_json())
        execution_plan = ExecutionPlan.model_validate_json(
            execution_plan.model_dump_json()
        )
        validation = PlanValidationRecord.model_validate_json(
            validation.model_dump_json()
        )
        mismatch = _execution_plan_mismatch(query_plan, context, execution_plan)
        if mismatch is None:
            # Trust boundary: prove the supplied allow record names THIS exact
            # content AND was produced by the expected server-side validator
            # policy before any step runs; no skip flag exists.
            mismatch = _execution_validation_mismatch(
                validation,
                query_plan,
                context,
                execution_plan,
                expected_policy_version=expected_policy_version,
                expected_policy_checksum=expected_policy_checksum,
            )
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
        gateway_receipts: dict[str, ExecutionReceipt] = {}
        output_contracts: dict[str, RequestOutputContract] = {}
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
                            execution_plan=execution_plan,
                            budget=budget,
                            timeout_ms=timeout_ms,
                            trace_id=trace_id,
                            gateway_receipts=gateway_receipts,
                            output_contracts=output_contracts,
                        )
                    except asyncio.CancelledError:
                        raise
                    except BudgetExceeded as exc:
                        code = budget.stop_reason or budget.halt(str(exc))
                        receipts.append(_failed_step(current_step, current_started, code))
                        return _validated_result(
                            execution_plan=execution_plan,
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
                        return _validated_result(
                            execution_plan=execution_plan,
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
                        return _validated_result(
                            execution_plan=execution_plan,
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
                    gateway_receipt = gateway_receipts.get(current_step.step_id)
                    if isinstance(current_step, TrustedCalculationStep):
                        calculation: dict[str, Any] = {
                            "template_id": current_step.template_id,
                            "template_version": current_step.template_version,
                            "binding_checksum": current_step.binding_checksum,
                            "output_metric_key": current_step.output_metric_key,
                        }
                    elif isinstance(current_step, AdHocCalculationStep):
                        calculation = {
                            "calculation_spec_checksum": (
                                current_step.calculation_spec.checksum
                            ),
                            "execution_binding_checksum": (
                                current_step.execution_binding.checksum
                            ),
                            "derived_output_id": current_step.derived_output_id,
                            "calculation_scope": "ad_hoc_noncanonical",
                        }
                    else:
                        calculation = {}
                    trusted_unit: str | None = None
                    if isinstance(current_step, TrustedCalculationStep):
                        calculation_catalog = getattr(self, "_calculation_catalog", None)
                        if current_step.output_metric_key is not None:
                            if calculation_catalog is None:
                                raise PlanStepError(
                                    "trusted_calculation_unit_authority_missing"
                                )
                            binding = calculation_catalog.binding_for(
                                current_step.output_metric_key
                            )
                            if (
                                binding is None
                                or (
                                    binding.unit is None
                                    and not self._allow_legacy_unitless_grounding
                                )
                                or binding.canonical_metric_key
                                != current_step.output_metric_key
                                or binding.checksum != current_step.binding_checksum
                            ):
                                raise PlanStepError("trusted_calculation_unit_missing")
                            trusted_unit = binding.unit
                        elif any(
                            item is not None
                            for item in (
                                current_step.template_version,
                                current_step.template_checksum,
                                current_step.binding_checksum,
                                current_step.output_metric_key,
                            )
                        ):
                            raise PlanStepError("trusted_calculation_unit_authority_missing")
                    receipts.append(
                        PlanStepReceipt(
                            step_id=current_step.step_id,
                            kind=current_step.kind,
                            status="succeeded",
                            elapsed_ms=_elapsed_ms(current_started),
                            output_digest=_json_digest(output),
                            **calculation,
                            rowset_sha256=(
                                gateway_receipt.rowset_sha256
                                if gateway_receipt is not None
                                and gateway_receipt.rowset_sha256 is not None
                                else _output_rowset_digest(output)
                            ),
                            unit=(
                                trusted_unit
                                if isinstance(current_step, TrustedCalculationStep)
                                else (
                                    _calculation_spec_unit(
                                        current_step.calculation_spec
                                    )
                                    if isinstance(current_step, AdHocCalculationStep)
                                    else (
                                        gateway_receipt.unit
                                        if gateway_receipt is not None
                                        else None
                                    )
                                )
                            ),
                            execution_id=gateway_receipt.execution_id if gateway_receipt else None,
                            trace_id=gateway_receipt.trace_id if gateway_receipt else None,
                            sql_fingerprint=gateway_receipt.sql_fingerprint if gateway_receipt else None,
                            source_version=gateway_receipt.source_version if gateway_receipt else None,
                            source_snapshot_id=(
                                gateway_receipt.source_snapshot_id
                                if gateway_receipt
                                else None
                            ),
                            source_snapshot_fingerprint=(
                                gateway_receipt.source_snapshot_fingerprint
                                if gateway_receipt
                                else None
                            ),
                            result_columns=gateway_receipt.result_columns if gateway_receipt else (),
                            parameter_binding_fingerprint=(
                                gateway_receipt.parameter_binding_fingerprint
                                if gateway_receipt
                                else None
                            ),
                            data_as_of=gateway_receipt.data_as_of if gateway_receipt else None,
                            freshness_status=gateway_receipt.freshness_status if gateway_receipt else "unknown",
                            source_kind=gateway_receipt.source_kind if gateway_receipt else None,
                            source_id=gateway_receipt.source_id if gateway_receipt else None,
                            selection_reason=gateway_receipt.selection_reason if gateway_receipt else None,
                            source_degradation=gateway_receipt.source_degradation if gateway_receipt else (),
                            source_checkpoint=gateway_receipt.source_checkpoint if gateway_receipt else None,
                            semantic_signature=gateway_receipt.semantic_signature if gateway_receipt else None,
                            semantic_validation_outcome=(
                                gateway_receipt.semantic_validation_outcome
                                if gateway_receipt
                                else None
                            ),
                            semantic_validator_version=(
                                gateway_receipt.semantic_validator_version
                                if gateway_receipt
                                else None
                            ),
                            semantic_manifest_checksum=(
                                gateway_receipt.semantic_manifest_checksum
                                if gateway_receipt
                                else None
                            ),
                            semantic_denial_codes=(
                                gateway_receipt.semantic_denial_codes
                                if gateway_receipt
                                else ()
                            ),
                            semantic_validation_identity=(
                                gateway_receipt.semantic_validation_identity
                                if gateway_receipt
                                else None
                            ),
                        )
                    )
        except TimeoutError:
            code = budget.halt("execution_deadline_exceeded")
            if current_step is not None and not any(
                receipt.step_id == current_step.step_id for receipt in receipts
            ):
                receipts.append(_failed_step(current_step, current_started, code))
            return _validated_result(
                execution_plan=execution_plan,
                record=PlanExecutionRecord(
                    execution_plan_checksum=execution_plan.checksum,
                    status="deadline_exceeded",
                    step_receipts=tuple(receipts),
                    output_step_ids=tuple(outputs),
                    stop_reason=code,
                ),
                outputs=outputs,
            )

        snapshot_fingerprints = {
            receipt.source_snapshot_fingerprint
            for receipt in gateway_receipts.values()
            if receipt.source_snapshot_fingerprint is not None
        }
        if len(snapshot_fingerprints) > 1:
            code = budget.halt("execution_plan_mixed_source_snapshots")
            return _validated_result(
                execution_plan=execution_plan,
                record=PlanExecutionRecord(
                    execution_plan_checksum=execution_plan.checksum,
                    status="failed",
                    step_receipts=tuple(receipts),
                    output_step_ids=tuple(outputs),
                    stop_reason=code,
                ),
                outputs=outputs,
            )

        return _validated_result(
            execution_plan=execution_plan,
            record=PlanExecutionRecord(
                execution_plan_checksum=execution_plan.checksum,
                status="succeeded",
                step_receipts=tuple(receipts),
                output_step_ids=tuple(outputs),
            ),
            outputs=outputs,
            output_contracts=output_contracts,
            require_semantic_contract=self._require_semantic_contract,
        )

    async def _execute_step(
        self,
        *,
        step: PlanStep,
        query_plan: QueryPlan,
        context: ContextBundle,
        outputs: dict[str, JsonValue],
        execution_plan: ExecutionPlan,
        budget: RouteBudgetLedger,
        timeout_ms: int,
        trace_id: str,
        gateway_receipts: dict[str, ExecutionReceipt],
        output_contracts: dict[str, RequestOutputContract],
    ) -> JsonValue:
        if isinstance(step, FetchMetricStep):
            prepare_kwargs: dict[str, Any] = {
                "step": step,
                "query_plan": query_plan,
                "context": context,
            }
            if _supports_keyword(self._metric_runner.prepare, "execution_plan"):
                prepare_kwargs["execution_plan"] = execution_plan
            prepared = await self._metric_runner.prepare(**prepare_kwargs)
            stop_reason = budget.record_sql_candidate(prepared.sql_fingerprint)
            if stop_reason is not None:
                raise BudgetExceeded(stop_reason)
            budget.observe_join_hops(prepared.join_hops)
            budget.begin_sql_execution()
            if trace_id and _supports_trace_id(self._metric_runner.execute):
                result = await self._metric_runner.execute(
                    prepared, timeout_ms=timeout_ms, trace_id=trace_id
                )
            else:
                result = await self._metric_runner.execute(
                    prepared, timeout_ms=timeout_ms
                )
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
            gateway_receipts[step.step_id] = result.receipt
            if result.semantic_manifest is not None and result.semantic_validation_record is not None:
                output_contracts[step.step_id] = RequestOutputContract(
                    manifest=result.semantic_manifest,
                    semantic_validation_record=result.semantic_validation_record,
                    gateway_parameter_binding_fingerprint=(
                        result.receipt.parameter_binding_fingerprint
                    ),
                    gateway_execution_receipt=result.receipt,
                    candidate_receipt=result.candidate_receipt,
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

        if isinstance(step, AdHocCalculationStep):
            if self._ad_hoc_calculation_runner is None:
                raise PlanStepError("ad_hoc_calculation_unavailable")
            inputs = {
                name: _resolve_ref(reference, outputs)
                for name, reference in step.input_refs.items()
            }
            if any(isinstance(value, (dict, list)) for value in inputs.values()):
                # Shape enforcement only: the runner contract accepts resolved
                # JSON scalars, never a rowset or nested structure.
                raise PlanStepError("ad_hoc_calculation_scalar_input_required")
            output = await self._ad_hoc_calculation_runner.execute(
                step=step,
                inputs=inputs,
            )
            return _json_value(output)

        if isinstance(step, VerifyStep):
            inputs = tuple(_resolve_ref(reference, outputs) for reference in step.input_refs)
            output = await self._result_verifier.verify(step=step, inputs=inputs)
            return _json_value(output)

        raise PlanStepError("execution_plan_step_unregistered")


def _supports_trace_id(callable_obj: object) -> bool:
    """Preserve compatibility with injected runners from older contracts."""

    try:
        parameters = inspect.signature(cast(Any, callable_obj)).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "trace_id"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _supports_keyword(callable_obj: object, name: str) -> bool:
    try:
        parameters = inspect.signature(cast(Any, callable_obj)).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _execution_validation_mismatch(
    validation: PlanValidationRecord,
    query_plan: QueryPlan,
    context: ContextBundle,
    execution_plan: ExecutionPlan,
    *,
    expected_policy_version: str,
    expected_policy_checksum: str,
) -> str | None:
    """Prove the record names THIS content under the EXPECTED policy.

    expected_policy_version/expected_policy_checksum are server-side inputs
    and are NEVER derived from the validation record itself: a well-formed
    forged record cannot self-authorize.
    """

    if validation.outcome != "allow":
        return "execution_plan_validation_denied"
    if validation.query_plan_sha256 != query_plan.checksum:
        return "execution_plan_query_hash_mismatch"
    if validation.context_checksum != context.checksum:
        return "execution_plan_context_hash_mismatch"
    if validation.execution_plan_sha256 != execution_plan.checksum:
        return "execution_plan_checksum_mismatch"
    if validation.policy_version != expected_policy_version:
        return "execution_plan_policy_version_mismatch"
    if validation.policy_checksum != expected_policy_checksum:
        return "execution_plan_policy_checksum_mismatch"
    return None


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
    return canonical_json_digest(value)


def _output_rowset_digest(value: JsonValue) -> str | None:
    if not isinstance(value, dict) or "rows" not in value:
        return None
    rows = value.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    try:
        return canonical_rowset_digest(
            cast("list[dict[str, Any]]", rows)
        )
    except (TypeError, ValueError):
        return None


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
    return _validated_result(
        execution_plan=execution_plan,
        record=PlanExecutionRecord(
            execution_plan_checksum=execution_plan.checksum,
            status=status,
            stop_reason=code,
        ),
        outputs={},
    )


def _validated_result(
    *,
    execution_plan: ExecutionPlan,
    record: PlanExecutionRecord,
    outputs: dict[str, JsonValue],
    output_contracts: dict[str, RequestOutputContract] | None = None,
    require_semantic_contract: bool = False,
) -> PlanExecutionResult:
    validate_execution_record_against_plan(execution_plan, record)
    return PlanExecutionResult(
        record=record,
        outputs=outputs,
        output_contracts=dict(output_contracts or {}),
        require_semantic_contract=require_semantic_contract,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _calculation_spec_unit(spec: CalculationSpec) -> str:
    """Resolve the exact unit ID declared by the run-scoped calculation spec."""

    if spec.unit == "custom_unit":
        if spec.custom_unit_id is None:
            raise PlanStepError("ad_hoc_calculation_unit_missing")
        return spec.custom_unit_id
    return spec.unit
