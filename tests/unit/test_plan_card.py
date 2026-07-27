"""Tests for CalcPlanCard and ConfirmedCalcPlan data models."""

from datetime import datetime, timezone

from src.nl2sql.agents.codeact_engine.plan_card import (
    CalcPlanCard,
    ComputeStep,
    ConfirmedCalcPlan,
    DataSourceSpec,
    FilterSpec,
    ValidationCriteria,
)


def test_calc_plan_card_creation() -> None:
    card = CalcPlanCard(
        data_sources=[
            DataSourceSpec(
                table="silver_fault_reporting_order",
                fields=["id", "area_id", "is_first_response_on_time"],
                join_hint=None,
            )
        ],
        source_confidence=0.92,
        filters=[
            FilterSpec(
                field="has_valid_bandwidth",
                operator="=",
                value="true",
                description="仅统计带宽有效工单",
            )
        ],
        data_flow="从投诉工单表中按区县分组统计首响及时率",
        computation_steps=[
            ComputeStep(
                step_id=1,
                description="按 area_id 分组统计首响及时工单数",
                expected_output="DataFrame with area_id, on_time_count",
            ),
            ComputeStep(
                step_id=2,
                description="计算各区县首响及时率",
                expected_output="DataFrame with area_id, rate",
            ),
        ],
        formula_description="首响及时率 = 首响及时工单数 / 已归档工单总数 * 100",
        intermediate_outputs=["on_time_count", "total_count"],
        output_type="比率排名表",
        output_precision="保留2位小数",
        output_unit="%",
        output_format="表格",
        ambiguity_warnings=["未指定时间范围，默认最近30天"],
        assumptions=["首响及时 = is_first_response_on_time 字段"],
    )

    assert card.source_confidence == 0.92
    assert len(card.data_sources) == 1
    assert len(card.computation_steps) == 2
    assert card.plan_version == 1


def test_confirmed_calc_plan_is_locked() -> None:
    card = CalcPlanCard(
        data_sources=[],
        source_confidence=0.5,
        filters=[],
        data_flow="test",
        computation_steps=[],
        formula_description="test",
        intermediate_outputs=[],
        output_type="数值",
        output_precision="整数",
        output_unit="个",
        output_format="单值",
    )

    confirmed = ConfirmedCalcPlan(
        plan_card=card,
        confirmed_at=datetime.now(tz=timezone.utc),
        confirmed_by="test_user",
        modification_history=[],
        validation_criteria=ValidationCriteria(
            expected_type="float",
            value_range=None,
            allow_null=False,
        ),
    )

    assert confirmed.is_locked is True
    assert confirmed.confirmed_by == "test_user"


def test_plan_card_version_increment() -> None:
    card = CalcPlanCard(
        data_sources=[],
        source_confidence=0.5,
        filters=[],
        data_flow="v1",
        computation_steps=[],
        formula_description="v1",
        intermediate_outputs=[],
        output_type="数值",
        output_precision="整数",
        output_unit="个",
        output_format="单值",
    )
    assert card.plan_version == 1

    revised = card.model_copy(update={"plan_version": 2, "data_flow": "v2"})
    assert revised.plan_version == 2
    assert revised.data_flow == "v2"
