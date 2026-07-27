"""动态指标计算数据契约。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class RetrievalEvidence(BaseModel):
    """RAG 检索证据（含质量评分）。"""

    content: str
    source: Literal["qa", "semantic", "graph"] = Field(
        description="证据来源：qa 知识库 / 语义层 / 图关系"
    )
    score: float = Field(ge=0.0, le=1.0, description="LLM 评分（0-1，越高越相关）")
    metadata: dict[str, Any] = Field(default_factory=dict)


class CalcStep(BaseModel):
    """计算计划中的单个步骤。"""

    step_id: int
    description: str
    expected_output: str
    status: Literal["pending", "running", "done", "failed"] = "pending"


class DynamicCalcPlan(BaseModel):
    """动态指标计算计划（取数 + 代码计算）。"""

    intent: str = Field(description="用户意图摘要")
    data_steps: list[CalcStep] = Field(
        default_factory=list, description="SQL 取数步骤"
    )
    calc_steps: list[CalcStep] = Field(
        default_factory=list, description="代码计算步骤"
    )
    fallback_strategy: str = Field(
        default="report_partial",
        description="降级策略：report_partial / skip_calc / abort",
    )


class DynamicCalcResult(BaseModel):
    """动态指标计算结果。"""

    final_value: Any = Field(description="最终计算结果")
    intermediate_stats: dict[str, Any] = Field(
        default_factory=dict, description="中间统计量"
    )
    execution_summary: str = Field(description="执行摘要")
    steps_log: list[dict[str, Any]] = Field(
        default_factory=list, description="各步骤执行日志"
    )


class SandboxResult(BaseModel):
    """代码沙箱执行结果。"""

    success: bool
    result: Any = None
    stats: dict[str, Any] = Field(default_factory=dict)
    stdout: str = ""
    error: str | None = None
    elapsed_ms: float = 0.0
