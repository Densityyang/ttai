"""HITL 计算计划卡片与确认后计划的数据模型。

CalcPlanCard: 展示给用户确认的结构化计划
ConfirmedCalcPlan: 用户确认后的不可变执行契约
"""

from datetime import datetime

from pydantic import BaseModel, Field


class DataSourceSpec(BaseModel):
    """取数来源规格。"""

    table: str = Field(description="源表名")
    fields: list[str] = Field(default_factory=list, description="需要的字段列表")
    join_hint: str | None = Field(default=None, description="JOIN 提示（如需多表关联）")


class FilterSpec(BaseModel):
    """数据筛选条件。"""

    field: str = Field(description="筛选字段")
    operator: str = Field(description="运算符: =, !=, >, <, >=, <=, IN, IS NULL, ...")
    value: str = Field(description="筛选值（字符串表示）")
    description: str = Field(default="", description="业务含义说明")


class ComputeStep(BaseModel):
    """有序计算步骤。"""

    step_id: int
    description: str = Field(description="步骤描述（自然语言）")
    expected_output: str = Field(description="预期输出描述")
    depends_on: list[int] = Field(default_factory=list, description="依赖的前序步骤 ID")


class DataFetchStep(BaseModel):
    """从 CalcPlanCard 派生的精确取数指令。"""

    step_id: int
    source: DataSourceSpec
    filters: list[FilterSpec] = Field(default_factory=list)
    description: str
    expected_columns: list[str] = Field(default_factory=list)


class ValidationCriteria(BaseModel):
    """结果验证标准。"""

    expected_type: str = Field(description="预期结果类型: int, float, str, DataFrame, list, ...")
    value_range: tuple[float, float] | None = Field(default=None, description="值域范围 (min, max)")
    allow_null: bool = Field(default=False, description="是否允许空值结果")
    precision: int | None = Field(default=None, description="小数精度位数")
    unit: str | None = Field(default=None, description="单位")


class CalcPlanCard(BaseModel):
    """展示给用户确认的计算计划卡片。"""

    # --- 1. 取数来源 ---
    data_sources: list[DataSourceSpec] = Field(description="表名、字段、关联关系")
    source_confidence: float = Field(ge=0.0, le=1.0, description="AI 对来源判断的置信度")

    # --- 2. 数据筛选与流转 ---
    filters: list[FilterSpec] = Field(default_factory=list, description="筛选条件")
    data_flow: str = Field(description="数据流转描述（自然语言）")

    # --- 3. 数据处理逻辑 ---
    computation_steps: list[ComputeStep] = Field(default_factory=list)
    formula_description: str = Field(description="计算公式的自然语言描述")
    intermediate_outputs: list[str] = Field(default_factory=list, description="预期中间产物名称")

    # --- 4. 输出结果 ---
    output_type: str = Field(description="数值/比率/排名/分布/...")
    output_precision: str = Field(description="精度要求描述")
    output_unit: str = Field(description="单位")
    output_format: str = Field(description="表格/单值/列表/...")

    # --- 元信息 ---
    plan_version: int = Field(default=1, description="修改轮次计数")
    ambiguity_warnings: list[str] = Field(default_factory=list, description="AI 识别出的歧义点")
    assumptions: list[str] = Field(default_factory=list, description="AI 做出的假设")

    def to_markdown(self) -> str:
        """渲染为 Markdown 文本供 chat 展示（方案 A 降级渲染）。"""
        sections = [f"## 计算计划确认 [v{self.plan_version}]\n"]

        sections.append("### 1. 取数来源")
        for ds in self.data_sources:
            sections.append(f"- 表: `{ds.table}`")
            if ds.fields:
                sections.append(f"  - 字段: {', '.join(f'`{f}`' for f in ds.fields)}")
            if ds.join_hint:
                sections.append(f"  - 关联: {ds.join_hint}")
        sections.append(f"- 置信度: {self.source_confidence:.0%}\n")

        sections.append("### 2. 数据筛选与流转")
        for f in self.filters:
            sections.append(f"- `{f.field}` {f.operator} `{f.value}` — {f.description}")
        sections.append(f"- 流转说明: {self.data_flow}\n")

        sections.append("### 3. 数据处理逻辑")
        for step in self.computation_steps:
            deps = f" (依赖步骤 {step.depends_on})" if step.depends_on else ""
            sections.append(f"- 步骤{step.step_id}: {step.description}{deps}")
            sections.append(f"  - 预期输出: {step.expected_output}")
        sections.append(f"- 公式: {self.formula_description}\n")

        sections.append("### 4. 输出结果")
        sections.append(f"- 类型: {self.output_type}")
        sections.append(f"- 精度: {self.output_precision}")
        sections.append(f"- 单位: {self.output_unit}")
        sections.append(f"- 格式: {self.output_format}\n")

        if self.assumptions:
            sections.append("### AI 假设（请确认）")
            for a in self.assumptions:
                sections.append(f"- {a}")
            sections.append("")

        if self.ambiguity_warnings:
            sections.append("### 歧义提示")
            for w in self.ambiguity_warnings:
                sections.append(f"- {w}")
            sections.append("")

        sections.append("---")
        sections.append("请回复 **确认执行** / **修改计划**（说明修改内容）/ **重新描述**")

        return "\n".join(sections)


class ConfirmedCalcPlan(BaseModel):
    """经用户确认的不可变计算计划 -- CodeAct Engine 的唯一输入。"""

    plan_card: CalcPlanCard
    confirmed_at: datetime
    confirmed_by: str = Field(default="user")
    modification_history: list[str] = Field(default_factory=list, description="修改历史摘要")
    is_locked: bool = Field(default=True)

    data_fetch_instructions: list[DataFetchStep] = Field(default_factory=list)
    compute_instructions: list[ComputeStep] = Field(default_factory=list)
    validation_criteria: ValidationCriteria = Field(
        default_factory=lambda: ValidationCriteria(expected_type="Any")
    )

    def summary(self) -> str:
        """返回计划的简短摘要，用于审计和输出。"""
        src = ", ".join(ds.table for ds in self.plan_card.data_sources)
        return (
            f"[{self.confirmed_at:%Y-%m-%d %H:%M}] "
            f"来源={src} | 步骤数={len(self.compute_instructions)} | "
            f"公式={self.plan_card.formula_description[:60]}"
        )
