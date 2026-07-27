"""nl2sql ??????

??????????????????? Supervisor Middleware?
????? nl2sql ? Agent ??????
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.secrets import SecretProvider


class AgentConfig(BaseSettings):
    """NL2SQL Agent ????"""

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

    @model_validator(mode="before")
    @classmethod
    def load_file_backed_values(cls, data: object) -> object:
        values = dict(data) if isinstance(data, dict) else {}
        provider = SecretProvider()
        if "EMBEDDING_API_KEY_FILE" in os.environ:
            values["embedding_api_key"] = provider.get("EMBEDDING_API_KEY")
        return values

    sql_top_k: int = Field(
        default=5,
        description="SQL ???????????",
    )
    graph_recursion_limit: int = Field(
        default=30,
        ge=1,
        description="LangGraph ????????",
    )
    graph_timeout: int = Field(
        default=300,
        ge=1,
        description="? Agent ?????????",
    )
    engine_mode: Literal["shadow", "v2"] = Field(
        default="v2",
        description="Explicit v2 engine mode; shadow never executes business SQL.",
    )
    shadow_traffic_percent: int = Field(default=5, ge=0, le=5)
    v2_request_deadline_ms: int = Field(default=30_000, ge=1, le=120_000)
    v2_token_budget: int = Field(default=8_000, ge=1)
    v2_cost_budget: float = Field(default=1.0, ge=0)
    v2_max_model_attempts: int = Field(default=2, ge=1, le=2)
    model_profile_version: str = Field(default="v1", min_length=1, max_length=64)
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    deepseek_flash_model: str = Field(default="deepseek-v4-flash", min_length=1)
    deepseek_pro_model: str = Field(default="deepseek-v4-pro", min_length=1)
    nvidia_nim_base_url: str = Field(default="https://integrate.api.nvidia.com/v1")
    nvidia_nim_model_fast: str = Field(default="", description="Approved NVIDIA small-model ID")

    embedding_api_key: SecretStr = Field(
        default=SecretStr("not-needed"),
        description="Embedding ?? API Key",
    )
    embedding_base_url: str = Field(
        default="http://127.0.0.1:1234/v1",
        description="Embedding ?? Base URL",
    )
    embedding_model: str = Field(
        default="text-embedding-mxbai-embed-large-v1",
        description="Embedding ????",
    )

    rag_qa_file_path: str = Field(
        default="configs/semantic/qa.md",
        description="RAG ????????",
    )
    rag_faiss_index_path: str = Field(
        default=".vector_store/nl2sql_qa",
        description="FAISS ????",
    )
    rag_top_k: int = Field(default=3, description="RAG ????")
    rag_relevance_threshold: float = Field(
        default=1.0, ge=0.0, description="RAG ??????distance ??????"
    )
    rag_chunk_size: int = Field(default=500, description="RAG ????????")
    rag_chunk_overlap: int = Field(default=80, description="RAG ????????")
    rag_semantic_file_path: str = Field(
        default="configs/semantic/semantic.md",
        description="Semantic RAG ????????????/?????",
    )
    rag_semantic_faiss_index_path: str = Field(
        default=".vector_store/nl2sql_semantic",
        description="Semantic RAG FAISS ????",
    )
    semantic_retriever_mode: Literal["direct", "rag"] = Field(
        default="direct",
        description="Semantic RAG ?????direct???????rag??????",
    )
    max_tool_rounds: int = Field(
        default=1,
        ge=1,
        description="Agentic RAG ????????",
    )
    enable_agentic_rag: bool = Field(
        default=True,
        description="?????? Agentic RAG ????????",
    )
    nl2sql_db_schema: str = Field(
        default="ai_views",
        description="NL2SQL ???? schema??????????",
    )
    ai_views_auto_sync: bool = Field(
        default=True,
        description="??????? YAML ?????? ai ??",
    )
    ai_views_config_path: str = Field(
        default="configs/semantic/ai_views.yaml",
        description="ai ?????????YAML?",
    )

    # --- Self-RAG / CRAG ---
    rag_grader_score_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Self-RAG ????????????Phase 3 ?? CRAG ?????",
    )
    rag_max_rewrite_rounds: int = Field(
        default=2,
        ge=0,
        description="Self-RAG / CRAG ????????",
    )
    rag_grader_model: str | None = Field(
        default=None,
        description="Self-RAG / CRAG ??????????? None ??????",
    )
    # TUNABLE: CRAG ????????????? benchmark ??????
    crag_correct_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="CRAG Correct ?????>= ????????",
    )
    crag_ambiguous_threshold: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="CRAG Ambiguous ?????>= ????????",
    )
    enable_adaptive_routing: bool = Field(
        default=True,
        description="???? Adaptive RAG ??????Fast/Standard/Deep ???",
    )

    # --- Phase 4: SQL Guard / Context ---
    # TUNABLE: EXPLAIN ???????????????????
    explain_cost_threshold: float = Field(
        default=500_000.0,
        ge=0.0,
        description="EXPLAIN ??????????????",
    )
    context_recent_full_rounds: int = Field(
        default=2,
        ge=1,
        description="?????????????????????",
    )
    context_global_summary_threshold: int = Field(
        default=30,
        ge=5,
        description="????????????",
    )

    # --- GraphRAG ---
    enable_graph_rag: bool = Field(
        default=True,
        description="???? GraphRAG ??????",
    )
    graph_rag_max_hops: int = Field(
        default=2,
        ge=1,
        description="GraphRAG ???????",
    )

    # --- Dynamic Calc (CodeAct) ---
    enable_dynamic_calc: bool = Field(
        default=False,
        description="???????????CodeAct???",
    )
    sandbox_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="???????????",
    )
    sandbox_max_memory_mb: int = Field(
        default=256,
        ge=32,
        description="?????????MB?",
    )
    sandbox_allowed_modules: list[str] = Field(
        default_factory=lambda: [
            "pandas", "numpy", "math", "statistics",
            "datetime", "decimal", "json", "re", "collections",
        ],
        description="????????? Python ?????",
    )

    # --- SQL Specialist (Phase 2) ---
    enable_hypothesis_verification: bool = Field(
        default=True,
        description="???? APEX-SQL ??????????",
    )
    enable_parallel_generation: bool = Field(
        default=True,
        description="????????? SQL ?? + ?????",
    )
    enable_experience_store: bool = Field(
        default=False,
        description="???? Memo-SQL ?????",
    )

    # --- Repair Loop ---
    sql_max_repair_rounds: int = Field(
        default=3,
        ge=1,
        description="SQL ??-??-????????",
    )
    code_max_repair_rounds: int = Field(
        default=3,
        ge=1,
        description="????-??-????????",
    )

    @field_validator("nl2sql_db_schema")
    @classmethod
    def validate_nl2sql_db_schema(cls, value: str) -> str:
        schema = value.strip()
        if not schema:
            raise ValueError("NL2SQL_DB_SCHEMA ????")
        if not schema.replace("_", "").isalnum() or not (
            schema[0].isalpha() or schema[0] == "_"
        ):
            raise ValueError(
                "NL2SQL_DB_SCHEMA ????? schema ??????/??/????"
            )
        return schema

    @field_validator("ai_views_config_path")
    @classmethod
    def validate_ai_views_config_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("AI_VIEWS_CONFIG_PATH ????")
        return path

    @model_validator(mode="after")
    def validate_codeact_profile(self) -> "AgentConfig":
        if self.service_mode == "product" and self.codeact_mode == "unsafe-dev":
            raise ValueError("CODEACT_MODE=unsafe-dev is not permitted when SERVICE_MODE=product")
        if self.service_mode == "product" and self.enable_experience_store:
            raise ValueError("ExperienceStore must be disabled when SERVICE_MODE=product")
        return self


@lru_cache
def get_agent_config() -> AgentConfig:
    """?? Agent ????(????)"""
    return AgentConfig()
