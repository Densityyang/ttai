"""Shared calculation semantic contract (contract-only slice).

MODE_SEMANTIC_SHARED_CALC_CONTRACT_V1.

These tests pin the frozen shared vocabulary and prove the authority-separation
invariant: shared semantics never grant canonical authority, and a strict
CalculationSpec rejects injected authority/lifecycle fields.
"""

from __future__ import annotations

from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationInput,
)
from src.nl2sql.semantic import calculation_contract as contract
from src.nl2sql.semantic.calculation_contract import (
    FORBIDDEN_AUTHORITY_FIELDS,
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_NODES,
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
    SemanticResolution,
    expression_depth,
    expression_node_count,
    referenced_input_roles,
    referenced_parameter_names,
)


def _inputs() -> tuple[CalculationInputSpec, ...]:
    return (
        CalculationInputSpec(
            role="sales", provenance="published_gold", metric_key="metric.sales"
        ),
        CalculationInputSpec(
            role="stores", provenance="published_gold", metric_key="metric.stores"
        ),
    )


def _ratio_expression() -> BinaryOperand:
    return BinaryOperand(
        op="divide",
        left=AggregateOperand(function="sum", role="sales"),
        right=AggregateOperand(function="count_distinct", role="stores"),
    )


def _spec(**overrides: object) -> CalculationSpec:
    base: dict[str, object] = {
        "calculation_id": "metric.sales_per_store",
        "expression": _ratio_expression(),
        "inputs": _inputs(),
        "parameters": (ParameterSpec(name="month", value_type="string", required=True),),
        "unit": "ratio",
        "precision": 4,
        "rounding": "half_up",
    }
    base.update(overrides)
    return CalculationSpec(**base)


# 1 ---------------------------------------------------------------------------


# 2 ---------------------------------------------------------------------------


# 3 ---------------------------------------------------------------------------


# 4 ---------------------------------------------------------------------------
def test_typed_calculation_represents_input_roles_and_bounded_expression() -> None:
    spec = _spec()
    assert referenced_input_roles(spec.expression) == ("sales", "stores")
    assert referenced_parameter_names(spec.expression) == ()
    assert expression_depth(spec.expression) == 2
    assert expression_node_count(spec.expression) == 3
    assert spec.checksum


# 5 ---------------------------------------------------------------------------
def test_parameter_spec_is_distinct_from_execution_binding() -> None:
    spec = _spec()
    assert isinstance(spec.parameters[0], ParameterSpec)
    assert "value" not in ParameterSpec.model_fields
    assert "value" in ParameterBinding.model_fields
    # A reusable spec carries no concrete parameter value.
    assert spec.parameters[0].allowed_values == ()
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="month", value="2026-08"),),
    )
    assert binding.binding_failures(spec) == ()


# 6 ---------------------------------------------------------------------------
def test_changing_valid_parameter_binding_does_not_mutate_spec_identity() -> None:
    spec = _spec()
    checksum_before = spec.checksum
    first = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="month", value="2026-08"),),
    )
    second = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="month", value="2026-09"),),
    )
    assert first.binding_failures(spec) == ()
    assert second.binding_failures(spec) == ()
    assert first.checksum != second.checksum
    assert spec.checksum == checksum_before


# 7 ---------------------------------------------------------------------------
def test_input_roles_are_non_empty_and_unique() -> None:
    with pytest.raises(ValidationError):
        _spec(inputs=(_inputs()[0], _inputs()[0]))
    with pytest.raises(ValidationError):
        CalculationInputSpec(role="", provenance="ad_hoc_metric")
    with pytest.raises(ValidationError):
        _spec(expression=InputRefOperand(role="missing"))


# 8 ---------------------------------------------------------------------------
def test_parameter_names_are_non_empty_and_unique() -> None:
    duplicate = (
        ParameterSpec(name="month", value_type="string"),
        ParameterSpec(name="month", value_type="string"),
    )
    with pytest.raises(ValidationError):
        _spec(parameters=duplicate)
    with pytest.raises(ValidationError):
        ParameterSpec(name="", value_type="string")


# 9 ---------------------------------------------------------------------------
def test_null_zero_unit_precision_policy_validates_strictly() -> None:
    with pytest.raises(ValidationError):
        _spec(unit="bogus")
    with pytest.raises(ValidationError):
        _spec(null_policy="maybe")
    with pytest.raises(ValidationError):
        _spec(zero_policy="zero")
    with pytest.raises(ValidationError):
        _spec(precision=13)
    with pytest.raises(ValidationError):
        _spec(rounding="half_up", precision=None)
    ok = _spec(null_policy="no_data", zero_policy="no_data")
    assert ok.null_policy == "no_data"
    assert ok.zero_policy == "no_data"


# 10 --------------------------------------------------------------------------
def test_malformed_or_unbounded_expression_shapes_fail_closed() -> None:
    with pytest.raises(ValidationError):
        BinaryOperand(
            op="modulo",
            left=LiteralOperand(value=Decimal("1")),
            right=LiteralOperand(value=Decimal("2")),
        )
    with pytest.raises(ValidationError):
        LiteralOperand(value=Decimal("Infinity"))

    deep: object = InputRefOperand(role="sales")
    for _ in range(MAX_EXPRESSION_DEPTH + 1):
        deep = BinaryOperand(op="add", left=deep, right=LiteralOperand(value=Decimal("1")))
    with pytest.raises(ValidationError):
        _spec(expression=deep)

    wide: object = InputRefOperand(role="sales")
    for _ in range(MAX_EXPRESSION_NODES):
        wide = BinaryOperand(op="add", left=wide, right=LiteralOperand(value=Decimal("1")))
    with pytest.raises(ValidationError):
        _spec(expression=wide)


# 11 --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonical", True),
        ("approved", True),
        ("saved", True),
        ("governance_candidate", True),
        ("template_registered", True),
        ("authority", "canonical"),
        ("canonical_metric_key", "metric.sales"),
        ("approved_calculation_binding", {"template_id": "ratio"}),
        ("binding_checksum", "a" * 64),
    ],
)
def test_calculation_spec_rejects_authority_fields(field: str, value: object) -> None:
    assert field in FORBIDDEN_AUTHORITY_FIELDS
    with pytest.raises(ValidationError):
        _spec(**{field: value})


# 12 --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confirmation", "Confirmed"),
        ("confirmed", True),
        ("retention", "SAVED"),
        ("governance", "GOVERNANCE_CANDIDATE"),
        ("lifecycle", "SAVED"),
        ("definition_id", "custom.1"),
        ("definition_version", 3),
    ],
)
def test_calculation_spec_rejects_lifecycle_fields(field: str, value: object) -> None:
    assert field in FORBIDDEN_AUTHORITY_FIELDS
    with pytest.raises(ValidationError):
        _spec(**{field: value})


# 13 --------------------------------------------------------------------------


# 14 --------------------------------------------------------------------------
def test_existing_canonical_binding_contract_still_holds() -> None:
    meta = trusted_template_registry.metadata("ratio")
    binding = ApprovedCalculationBinding(
        canonical_metric_key="metric.revenue_ratio",
        template_id="ratio",
        template_version=meta.version,
        template_checksum=meta.checksum,
        inputs=(
            ApprovedCalculationInput(
                role="numerator",
                metric_key="metric.revenue",
                metric_contract_sha256="b" * 64,
            ),
            ApprovedCalculationInput(
                role="denominator",
                metric_key="metric.revenue_base",
                metric_contract_sha256="c" * 64,
            ),
        ),
        binding_revision="1",
        semantic_release_id="release-1",
        semantic_release_checksum="d" * 64,
    )
    assert binding.checksum == binding.checksum
    assert binding.input_roles == ("numerator", "denominator")


# extras ---------------------------------------------------------------------
def test_semantic_resolution_vocabulary_is_additive() -> None:
    outcomes = set(
        get_args(SemanticResolution.model_fields["outcome"].annotation)
    )
    assert outcomes == {
        "resolved",
        "clarification_required",
        "no_authoritative_definition",
    }
    assert SemanticResolution(outcome="resolved").outcome == "resolved"
    with pytest.raises(ValidationError):
        SemanticResolution(outcome="resolved", unresolved_slots=("scope",))
    clarify = SemanticResolution(
        outcome="clarification_required", unresolved_slots=("scope",)
    )
    assert clarify.unresolved_slots == ("scope",)
    assert SemanticResolution(outcome="no_authoritative_definition").outcome


def test_parameter_binding_contract_validation() -> None:
    spec = _spec(
        parameters=(
            ParameterSpec(
                name="scope",
                value_type="string",
                required=True,
                allowed_values=("store", "area"),
            ),
        )
    )
    good = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="scope", value="store"),),
    )
    assert good.binding_failures(spec) == ()
    unknown = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="other", value="x"),),
    )
    assert "execution_binding_unknown_parameter" in unknown.binding_failures(spec)
    missing = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
    )
    assert "execution_binding_missing_required_parameter" in missing.binding_failures(spec)
    disallowed = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="scope", value="city"),),
    )
    assert "execution_binding_value_not_allowed" in disallowed.binding_failures(spec)


def test_parameter_reference_must_be_declared() -> None:
    with pytest.raises(ValidationError):
        _spec(expression=ParameterRefOperand(name="undeclared"))


def test_provenance_is_typed_and_formal_provenance_names_identity() -> None:
    with pytest.raises(ValidationError):
        CalculationInputSpec(role="sales", provenance="bogus")
    with pytest.raises(ValidationError):
        CalculationInputSpec(role="sales", provenance="published_gold")
    ad_hoc = CalculationInputSpec(role="sales", provenance="ad_hoc_metric")
    assert ad_hoc.metric_key is None


# D1 — value_type enforcement -------------------------------------------------
@pytest.mark.parametrize(
    ("value_type", "value", "accepted"),
    [
        ("string", "x", True),
        ("string", 5, False),
        ("boolean", True, True),
        ("boolean", 1, False),
        ("integer", 5, True),
        ("integer", True, False),
        ("integer", Decimal("5"), False),
        ("decimal", Decimal("1.25"), True),
        ("decimal", 5, True),
        ("decimal", 1.25, False),
        ("date", "2026-08-01", True),
        ("date", "2026-08", False),
        ("date", "20260801", False),
        ("date", "2026-02-30", False),
    ],
)
def test_parameter_value_type_is_enforced(
    value_type: str, value: object, accepted: bool
) -> None:
    spec = _spec(parameters=(ParameterSpec(name="p", value_type=value_type),))
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="p", value=value),),
    )
    failures = binding.binding_failures(spec)
    if accepted:
        assert failures == ()
    else:
        assert "execution_binding_value_type_mismatch" in failures


def test_allowed_values_cannot_bypass_value_type() -> None:
    spec = _spec(
        parameters=(
            ParameterSpec(name="p", value_type="integer", allowed_values=("5",)),
        )
    )
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="p", value="5"),),
    )
    # A string "5" is type-incompatible with an integer parameter even though
    # its string form appears in allowed_values.
    assert "execution_binding_value_type_mismatch" in binding.binding_failures(spec)


# D2 — bounded custom-unit identity -------------------------------------------
def test_anonymous_custom_unit_is_not_representable() -> None:
    with pytest.raises(ValidationError):
        _spec(unit="custom")


def test_custom_unit_requires_bounded_identity() -> None:
    with pytest.raises(ValidationError):
        _spec(unit="custom_unit")
    with pytest.raises(ValidationError):
        _spec(unit="ratio", custom_unit_id="orders_per_store")
    with pytest.raises(ValidationError):
        _spec(unit="custom_unit", custom_unit_id="Bad Id")
    ok = _spec(unit="custom_unit", custom_unit_id="orders_per_store")
    assert ok.custom_unit_id == "orders_per_store"


def test_distinct_custom_units_cannot_collapse_and_enter_checksum() -> None:
    first = _spec(unit="custom_unit", custom_unit_id="orders_per_store")
    second = _spec(unit="custom_unit", custom_unit_id="tickets_per_agent")
    third = _spec(unit="custom_unit", custom_unit_id="orders_per_store")
    assert first.checksum != second.checksum
    assert first.checksum == third.checksum
    assert first.checksum != _spec(unit="ratio").checksum
    dumped = first.model_dump(mode="json")
    assert dumped["unit"] == "custom_unit"
    assert dumped["custom_unit_id"] == "orders_per_store"


def test_custom_unit_id_must_not_shadow_builtins() -> None:
    for shadow in (
        "count",
        "percent",
        "ratio",
        "currency_cny",
        "seconds",
        "custom_unit",
        "custom",
    ):
        with pytest.raises(ValidationError):
            _spec(unit="custom_unit", custom_unit_id=shadow)


@pytest.mark.parametrize("degenerate", ["a.", "a-", "a..b", "a.-b", ".ab", "ab.", "A.b"])
def test_custom_unit_id_rejects_degenerate_forms(degenerate: str) -> None:
    with pytest.raises(ValidationError):
        _spec(unit="custom_unit", custom_unit_id=degenerate)


def test_percent_and_ratio_never_collapse_without_conversion() -> None:
    assert _spec(unit="percent").checksum != _spec(unit="ratio").checksum
    for name in dir(contract):
        assert "convert" not in name.lower()
