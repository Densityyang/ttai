"""Tests for plan_ingestion module."""

from datetime import datetime, timezone

import pytest

from src.nl2sql.agents.codeact_engine.plan_card import (
    CalcPlanCard,
    ComputeStep,
    ConfirmedCalcPlan,
    DataFetchStep,
    DataSourceSpec,
    FilterSpec,
    ValidationCriteria,
)
from src.nl2sql.agents.codeact_engine.plan_ingestion import (
    IngestedPlan,
    PlanIngestionError,
    ingest,
)


def _make_confirmed_plan(**overrides) -> ConfirmedCalcPlan:
    card = CalcPlanCard(
        data_sources=[DataSourceSpec(table="test_table", fields=["id", "value"])],
        source_confidence=0.9,
        filters=[FilterSpec(field="status", operator="=", value="active", description="")],
        data_flow="从 test_table 取数",
        computation_steps=[
            ComputeStep(step_id=1, description="求和", expected_output="sum_value"),
        ],
        formula_description="sum(value)",
        intermediate_outputs=["sum_value"],
        output_type="数值",
        output_precision="整数",
        output_unit="个",
        output_format="单值",
    )
    payload = {
        "plan_card": card,
        "confirmed_at": datetime.now(tz=timezone.utc),
        "confirmed_by": "test",
        "is_locked": True,
        "data_fetch_instructions": [
            DataFetchStep(
                step_id=1,
                source=DataSourceSpec(table="test_table", fields=["id", "value"]),
                filters=[],
                description="取数",
                expected_columns=["id", "value"],
            ),
        ],
        "compute_instructions": [
            ComputeStep(step_id=1, description="求和", expected_output="sum_value"),
        ],
        "validation_criteria": ValidationCriteria(expected_type="int"),
    }
    payload.update(overrides)
    return ConfirmedCalcPlan(**payload)


def test_ingest_success() -> None:
    plan = _make_confirmed_plan()
    result = ingest(plan)
    assert isinstance(result, IngestedPlan)
    assert len(result.fetch_steps) == 1
    assert len(result.compute_steps) == 1
    assert result.formula == "sum(value)"


def test_ingest_unlocked_plan_raises() -> None:
    plan = _make_confirmed_plan(is_locked=False)
    with pytest.raises(PlanIngestionError, match="未锁定"):
        ingest(plan)


def test_ingest_empty_plan_raises() -> None:
    plan = _make_confirmed_plan(
        data_fetch_instructions=[],
        compute_instructions=[],
    )
    with pytest.raises(PlanIngestionError, match="既无取数步骤也无计算步骤"):
        ingest(plan)


def test_ingest_missing_table_raises() -> None:
    plan = _make_confirmed_plan(
        data_fetch_instructions=[
            DataFetchStep(
                step_id=1,
                source=DataSourceSpec(table="", fields=[]),
                description="bad",
            ),
        ],
    )
    with pytest.raises(PlanIngestionError, match="缺少表名"):
        ingest(plan)


def test_ingest_circular_dependency_raises() -> None:
    plan = _make_confirmed_plan(
        compute_instructions=[
            ComputeStep(step_id=1, description="step1", expected_output="a", depends_on=[2]),
            ComputeStep(step_id=2, description="step2", expected_output="b", depends_on=[1]),
        ],
    )
    with pytest.raises(PlanIngestionError, match="循环依赖"):
        ingest(plan)
