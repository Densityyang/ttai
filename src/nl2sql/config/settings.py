"""nl2sql 应用特定配置

记忆压缩、调用限制等横切关注点已迁移至 Supervisor Middleware，
此处仅保留 nl2sql 子 Agent 的业务配置。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """NL2SQL Agent 专属配置"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    service_mode: Literal["infra-dev", "product"] = Field(
        default="infra-dev",
        description="Service operating profile. Product mode enforces fail-closed CodeAct settings.",
    )
    codeact_mode: Literal["disabled", "trusted-template", "unsafe-dev"] = Field(
        default="disabled",
        description="Dynamic calculation execution mode. unsafe-dev is never permitted in product mode.",
    )

    sql_top_k: int = Field(
        default=5,
        description="SQL 查询默认返回的最大行数",
    )
    graph_recursion_limit: int = Field(
        default=30,
        ge=1,
        description="LangGraph 运行最大递归步数",
    )
    graph_timeout: int = Field(
        default=300,
        ge=1,
        description="子 Agent 调用超时时间（秒）",
    )

    embedding_api_key: SecretStr = Field(
        default=SecretStr("not-needed"),
        description="Embedding 服务 API Key",
    )
    embedding_base_url: str = Field(
        default="http://127.0.0.1:1234/v1",
        description="Embedding 服务 Base URL",
    )
    embedding_model: str = Field(
        default="text-embedding-mxbai-embed-large-v1",
        description="Embedding 模型名称",
    )

    rag_qa_file_path: str = Field(
        default="configs/semantic/qa.md",
        description="RAG 问答样本文件路径",
    )
    rag_faiss_index_path: str = Field(
        default=".vector_store/nl2sql_qa",
        description="FAISS 索引目录",
    )
    rag_top_k: int = Field(default=3, description="RAG 检索条数")
    rag_relevance_threshold: float = Field(
        default=1.0, ge=0.0, description="RAG 相关性阈值（distance 越小越相关）"
    )
    rag_chunk_size: int = Field(default=500, description="RAG 分片大小（字符）")
    rag_chunk_overlap: int = Field(default=80, description="RAG 分片重叠（字符）")
    rag_semantic_file_path: str = Field(
        default="configs/semantic/semantic.md",
        description="Semantic RAG 语义层文件路径（业务术语/指标定义）",
    )
    rag_semantic_faiss_index_path: str = Field(
        default=".vector_store/nl2sql_semantic",
        description="Semantic RAG FAISS 索引目录",
    )
    semantic_retriever_mode: Literal["direct", "rag"] = Field(
        default="direct",
        description="Semantic RAG 检索模式：direct直接读取全文，rag基于向量索引",
    )
    max_tool_rounds: int = Field(
        default=1,
        ge=1,
        description="Agentic RAG 工具调用最大轮数",
    )
    enable_agentic_rag: bool = Field(
        default=True,
        description="是否启用前置 Agentic RAG 节点进行语义探索",
    )
    nl2sql_db_schema: str = Field(
        default="ai_views",
        description="NL2SQL 查询默认 schema（用于隔离可见视图）",
    )
    ai_views_auto_sync: bool = Field(
        default=True,
        description="启动时是否根据 YAML 配置自动同步 ai 视图",
    )
    ai_views_config_path: str = Field(
        default="configs/semantic/ai_views.yaml",
        description="ai 视图配置文件路径（YAML）",
    )

    # --- Self-RAG / CRAG ---
    rag_grader_score_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Self-RAG 证据评分阈值（旧版兼容，Phase 3 使用 CRAG 三级阈值）",
    )
    rag_max_rewrite_rounds: int = Field(
        default=2,
        ge=0,
        description="Self-RAG / CRAG 查询重写最大轮次",
    )
    rag_grader_model: str | None = Field(
        default=None,
        description="Self-RAG / CRAG 评分使用的模型名称，为 None 时复用主模型",
    )
    # TUNABLE: CRAG 三级置信阈值，可能需要根据 benchmark 测试结果调整
    crag_correct_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="CRAG Correct 置信阈值：>= 此值直接使用证据",
    )
    crag_ambiguous_threshold: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="CRAG Ambiguous 置信阈值：>= 此值触发知识精炼",
    )
    enable_adaptive_routing: bool = Field(
        default=True,
        description="是否启用 Adaptive RAG 自适应路由（Fast/Standard/Deep 三路）",
    )

    # --- Phase 4: SQL Guard / Context ---
    # TUNABLE: EXPLAIN 成本阈值，可能需要根据实际数据规模调整
    explain_cost_threshold: float = Field(
        default=500_000.0,
        ge=0.0,
        description="EXPLAIN 查询成本上限，超过则拒绝执行",
    )
    context_recent_full_rounds: int = Field(
        default=2,
        ge=1,
        description="上下文压缩时保留完整内容的最近工具调用轮次",
    )
    context_global_summary_threshold: int = Field(
        default=30,
        ge=5,
        description="触发全局摘要的消息数阈值",
    )

    # --- GraphRAG ---
    enable_graph_rag: bool = Field(
        default=True,
        description="是否启用 GraphRAG 关系增强检索",
    )
    graph_rag_max_hops: int = Field(
        default=2,
        ge=1,
        description="GraphRAG 关系图最大跳数",
    )

    # --- Dynamic Calc (CodeAct) ---
    enable_dynamic_calc: bool = Field(
        default=False,
        description="是否启用动态指标计算（CodeAct）链路",
    )
    sandbox_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="代码沙箱执行超时（秒）",
    )
    sandbox_max_memory_mb: int = Field(
        default=256,
        ge=32,
        description="代码沙箱最大内存（MB）",
    )
    sandbox_allowed_modules: list[str] = Field(
        default_factory=lambda: [
            "pandas", "numpy", "math", "statistics",
            "datetime", "decimal", "json", "re", "collections",
        ],
        description="代码沙箱允许导入的 Python 模块白名单",
    )

    # --- SQL Specialist (Phase 2) ---
    enable_hypothesis_verification: bool = Field(
        default=True,
        description="是否启用 APEX-SQL 假设验证（数据画像）",
    )
    enable_parallel_generation: bool = Field(
        default=True,
        description="是否启用多策略并行 SQL 生成 + 锦标赛选优",
    )
    enable_experience_store: bool = Field(
        default=True,
        description="是否启用 Memo-SQL 经验记忆库",
    )

    # --- Repair Loop ---
    sql_max_repair_rounds: int = Field(
        default=3,
        ge=1,
        description="SQL 生成-执行-修复循环最大轮次",
    )
    code_max_repair_rounds: int = Field(
        default=3,
        ge=1,
        description="代码生成-执行-修复循环最大轮次",
    )

    @field_validator("nl2sql_db_schema")
    @classmethod
    def validate_nl2sql_db_schema(cls, value: str) -> str:
        schema = value.strip()
        if not schema:
            raise ValueError("NL2SQL_DB_SCHEMA 不能为空")
        if not schema.replace("_", "").isalnum() or not (
            schema[0].isalpha() or schema[0] == "_"
        ):
            raise ValueError(
                "NL2SQL_DB_SCHEMA 必须是合法 schema 标识符（字母/数字/下划线）"
            )
        return schema

    @field_validator("ai_views_config_path")
    @classmethod
    def validate_ai_views_config_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("AI_VIEWS_CONFIG_PATH 不能为空")
        return path

    @model_validator(mode="after")
    def validate_codeact_profile(self) -> "AgentConfig":
        if self.service_mode == "product" and self.codeact_mode == "unsafe-dev":
            raise ValueError("CODEACT_MODE=unsafe-dev is not permitted when SERVICE_MODE=product")
        return self


@lru_cache
def get_agent_config() -> AgentConfig:
    """获取 Agent 配置实例(单例模式)"""
    return AgentConfig()
