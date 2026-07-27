"""全局配置管理 - 跨应用共享的基础配置"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
APP_ENV = os.getenv("ENV", "dev")


class Settings(BaseSettings):
    """全局配置"""

    model_config = SettingsConfigDict(
        env_file=(
            ROOT_DIR / ".env",
            ROOT_DIR / f".env.{APP_ENV}",
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 数据库
    database_url: str = Field(default="", description="数据库连接 URL（日常查询，建议只读用户）")
    database_url_admin: str | None = Field(
        default=None,
        description="管理员数据库连接 URL（DDL 操作，如创建 schema/视图）",
    )

    # LLM
    openai_api_key: str = Field(default="", description="OpenAI API Key")
    openai_base_url: str | None = Field(default=None, description="OpenAI Base URL")
    model_name: str = Field(default="jiutian-lan-comv3", description="模型名称")

    # 可观测性
    langfuse_enabled: bool = Field(default=False, description="是否启用 Langfuse 监控")
    langfuse_public_key: str | None = Field(default=None, description="Langfuse Public Key")
    langfuse_secret_key: str | None = Field(default=None, description="Langfuse Secret Key")
    langfuse_host: str | None = Field(default=None, description="Langfuse 服务地址")
    langfuse_timeout: int = Field(default=30, description="Langfuse 请求超时（秒）")

    # 认证复用（tt-api -> tt-ai）
    auth_enabled: bool = Field(default=True, description="是否启用鉴权")
    tt_api_base_url: str = Field(default="", description="tt-api 基础地址")
    tt_api_auth_info_path: str = Field(
        default="/vadmin/auth/user/profile",
        description="tt-api 当前用户身份信息接口路径",
    )
    auth_verify_timeout_ms: int = Field(default=1500, description="鉴权请求超时时间（毫秒）")
    auth_cache_ttl_seconds: int = Field(default=60, description="鉴权缓存 TTL（秒）")
    auth_required_permission_invoke: str = Field(
        default="nl2sql:invoke",
        description="invoke 接口需要的权限点",
    )
    auth_required_permission_stream: str = Field(
        default="nl2sql:stream",
        description="stream 接口需要的权限点",
    )
    api_port: int = Field(default=9001, description="API 启动端口")

    # 启动时 RAG 索引同步失败是否直接退出（测试环境可设为 false）
    rag_startup_sync_strict: bool = Field(
        default=True,
        description="为 true 时 QA/Semantic 索引同步失败将导致进程退出；false 时仅告警并继续",
    )
    # 为 true 时整段跳过启动时 QA/Semantic 索引同步（无 embedding 服务时设 SKIP_RAG_STARTUP_SYNC=true）
    skip_rag_startup_sync: bool = Field(
        default=False,
        description="跳过启动阶段 FAISS 索引同步；依赖 .env 中 SKIP_RAG_STARTUP_SYNC",
    )

    # 记忆持久化
    memory_backend: Literal["memory", "postgresql"] = Field(
        default="memory",
        description="记忆后端类型：memory=开发环境，postgresql=生产环境",
    )
    memory_backend_url: str | None = Field(
        default=None,
        description="记忆后端连接 URL（memory_backend=postgresql 时必填）",
    )

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        if self.auth_enabled and not self.tt_api_base_url.strip():
            raise ValueError("AUTH_ENABLED=true 时必须配置 TT_API_BASE_URL")
        return self

    @model_validator(mode="after")
    def validate_memory_backend(self) -> "Settings":
        if self.memory_backend == "postgresql" and not self.memory_backend_url:
            raise ValueError("MEMORY_BACKEND=postgresql 时必须配置 MEMORY_BACKEND_URL")
        return self


@lru_cache
def get_settings() -> Settings:
    """获取全局配置实例(单例模式)"""
    return Settings()
