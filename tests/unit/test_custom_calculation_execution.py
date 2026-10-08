from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from src.nl2sql.artifacts.custom_definition import (
    DefinitionVersion,
    derive_parameter_contract,
)
from src.nl2sql.contracts import TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    CustomCalculationExecutionError,
    ResolvedCalculationInput,
    execute_custom_calculation,
)
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    InputProvenance,
    InputRefOperand,
    LiteralOperand,
    ParameterBinding,
    ParameterRefOperand,
    ParameterSpec,
)

_METRIC = "repair_service_archive_rate_overall_day"


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="custom.actual_to_target_index",
        expression=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="actual"),
                right=ParameterRefOperand(name="target_percent"),
            ),
            right=LiteralOperand(value=Decimal("100")),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual", provenance="published_gold", metric_key=_METRIC
            ),
        ),
        parameters=(ParameterSpec(name="target_percent", value_type="decimal"),),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


def _binding(spec: CalculationSpec, target: int) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="target_percent", value=target),),
    )


def _input(
    value: Decimal | int | str | None,
    *,
    metric_key: str = _METRIC,
    status: Literal["resolved", "unavailable"] = "resolved",
    provenance: InputProvenance = "published_gold",
) -> ResolvedCalculationInput:
    return ResolvedCalculationInput(
        role="actual",
        metric_key=metric_key,
        status=status,
        value=value,
        unit="percent",
        data_as_of=datetime(2026, 9, 20, tzinfo=UTC),
        time_range=TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20)),
        provenance=provenance,
        source_id="gold.repair.archive",
        receipt_step_id="fetch_actual",
        fact_id="a" * 64,
    )


def test_same_definition_version_executes_with_different_parameter_values() -> None:
    spec = _spec()
    version = DefinitionVersion(
        definition_id="def_" + "1" * 32,
        version=1,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="Controlled actual to target index",
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    first_binding = _binding(spec, 20)
    second_binding = _binding(spec, 40)

    first = execute_custom_calculation(
        spec=spec, binding=first_binding, inputs=(_input(10),)
    )
    second = execute_custom_calculation(
        spec=spec, binding=second_binding, inputs=(_input(10),)
    )

    assert first.value == Decimal("50.00")
    assert second.value == Decimal("25.00")
    assert first.spec_checksum == second.spec_checksum == version.calculation.checksum
    assert first.binding_checksum != second.binding_checksum
    assert version.version == 1
    assert first.calculation_scope == "reusable_custom_definition"
    assert first.data_as_of == datetime(2026, 9, 20, tzinfo=UTC)
    assert first.time_range == TimeRange(
        start=date(2026, 9, 20), end=date(2026, 9, 20)
    )


def test_zero_actual_is_numeric_and_zero_target_is_undefined_not_no_data() -> None:
    spec = _spec()

    zero_actual = execute_custom_calculation(
        spec=spec, binding=_binding(spec, 20), inputs=(_input(0),)
    )
    assert zero_actual.value == Decimal("0.00")

    with pytest.raises(
        CustomCalculationExecutionError,
        match="calculation_undefined_division_by_zero",
    ) as raised:
        execute_custom_calculation(
            spec=spec, binding=_binding(spec, 0), inputs=(_input(10),)
        )
    assert raised.value.code != "NO_DATA"


def test_null_and_unavailable_remain_distinct_governed_failures() -> None:
    spec = _spec()

    with pytest.raises(
        CustomCalculationExecutionError, match="calculation_result_null_unsupported"
    ):
        execute_custom_calculation(
            spec=spec, binding=_binding(spec, 20), inputs=(_input(None),)
        )

    with pytest.raises(
        CustomCalculationExecutionError, match="calculation_input_unavailable"
    ):
        execute_custom_calculation(
            spec=spec,
            binding=_binding(spec, 20),
            inputs=(_input(None, status="unavailable"),),
        )


def test_missing_parameter_and_wrong_binding_fail_before_runtime() -> None:
    spec = _spec()
    missing = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
    )
    with pytest.raises(
        CustomCalculationExecutionError,
        match="execution_binding_missing_required_parameter",
    ):
        execute_custom_calculation(spec=spec, binding=missing, inputs=(_input(10),))

    mismatch = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum="0" * 64,
        parameters=(ParameterBinding(name="target_percent", value=20),),
    )
    with pytest.raises(
        CustomCalculationExecutionError, match="execution_binding_spec_mismatch"
    ):
        execute_custom_calculation(spec=spec, binding=mismatch, inputs=(_input(10),))


def test_wrong_metric_or_provenance_fails_closed() -> None:
    spec = _spec()
    with pytest.raises(
        CustomCalculationExecutionError, match="calculation_input_metric_mismatch"
    ):
        execute_custom_calculation(
            spec=spec,
            binding=_binding(spec, 20),
            inputs=(_input(10, metric_key="repair.other_metric"),),
        )
    with pytest.raises(
        CustomCalculationExecutionError, match="calculation_input_provenance_mismatch"
    ):
        execute_custom_calculation(
            spec=spec,
            binding=_binding(spec, 20),
            inputs=(_input(10, provenance="ad_hoc_metric"),),
        )


def test_arbitrary_expression_string_has_no_execution_path() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        CalculationSpec.model_validate(
            {
                "calculation_id": "custom.unsafe",
                "expression": "actual / target_percent * 100",
                "inputs": spec.inputs,
                "parameters": spec.parameters,
                "unit": "percent",
            }
        )
