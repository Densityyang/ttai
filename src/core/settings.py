"""?????? - ??????????"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.secrets import SecretProvider

ROOT_DIR = Path(__file__).resolve().parents[2]
APP_ENV = os.getenv("ENV", "dev")


class Settings(BaseSettings):
    """????"""

    model_config = SettingsConfigDict(
        env_file=(
            ROOT_DIR / ".env",
            ROOT_DIR / f".env.{APP_ENV}",
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ???
    database_url: str = Field(default="", description="????? URL?????????????")
    control_database_url: str | None = Field(default=None)
    checkpoint_database_url: str | None = Field(default=None)
    backup_retention_days: int = Field(default=7, ge=1, le=365)

    # LLM
    openai_api_key: str = Field(default="", description="OpenAI API Key")
    openai_base_url: str | None = Field(default=None, description="OpenAI Base URL")
    model_name: str = Field(default="jiutian-lan-comv3", description="????")

    # ????
    langfuse_enabled: bool = Field(default=False, description="???? Langfuse ??")
    langfuse_public_key: str | None = Field(default=None, description="Langfuse Public Key")
    langfuse_secret_key: str | None = Field(default=None, description="Langfuse Secret Key")
    langfuse_host: str | None = Field(default=None, description="Langfuse ????")
    langfuse_timeout: int = Field(default=30, description="Langfuse ???????")

    # ?????tt-api -> tt-ai?
    auth_enabled: bool = Field(default=True, description="??????")
    tt_api_base_url: str = Field(default="", description="tt-api ????")
    tt_api_auth_info_path: str = Field(
        default="/vadmin/auth/user/profile",
        description="tt-api ????????????",
    )
    auth_verify_timeout_ms: int = Field(default=1500, description="????????????")
    auth_cache_ttl_seconds: int = Field(default=60, description="???? TTL???")
    auth_required_permission_invoke: str = Field(
        default="nl2sql:invoke",
        description="invoke ????????",
    )
    auth_required_permission_stream: str = Field(
        default="nl2sql:stream",
        description="stream ????????",
    )
    api_port: int = Field(default=9001, description="API ????")
    service_mode: Literal["infra-dev", "product"] = Field(default="infra-dev")
    model_required: bool = Field(default=False)
    cors_allowed_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        description="Comma-separated browser origins allowed to call the API.",
    )
    cors_allow_credentials: bool = Field(default=True)

    # ??? RAG ???????????????????? false?
    rag_startup_sync_strict: bool = Field(
        default=True,
        description="? true ? QA/Semantic ??????????????false ???????",
    )
    # ? true ???????? QA/Semantic ?????? embedding ???? SKIP_RAG_STARTUP_SYNC=true?
    skip_rag_startup_sync: bool = Field(
        default=False,
        description="?????? FAISS ??????? .env ? SKIP_RAG_STARTUP_SYNC",
    )

    # ?????
    memory_backend: Literal["memory", "postgresql"] = Field(
        default="memory",
        description="???????memory=?????postgresql=????",
    )

    @model_validator(mode="before")
    @classmethod
    def load_file_backed_values(cls, data: object) -> object:
        values = dict(data) if isinstance(data, dict) else {}
        provider = SecretProvider()
        for env_name, field_name in {
            "DATABASE_URL": "database_url",
            "CONTROL_DATABASE_URL": "control_database_url",
            "CHECKPOINT_DATABASE_URL": "checkpoint_database_url",
            "OPENAI_API_KEY": "openai_api_key",
            "LANGFUSE_PUBLIC_KEY": "langfuse_public_key",
            "LANGFUSE_SECRET_KEY": "langfuse_secret_key",
        }.items():
            if f"{env_name}_FILE" in os.environ:
                values[field_name] = provider.get(env_name)
        return values

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        if self.auth_enabled and not self.tt_api_base_url.strip():
            raise ValueError("AUTH_ENABLED=true ????? TT_API_BASE_URL")
        return self

    @model_validator(mode="after")
    def validate_memory_backend(self) -> "Settings":
        if self.memory_backend == "postgresql" and not self.checkpoint_database_url:
            raise ValueError("MEMORY_BACKEND=postgresql ????? CHECKPOINT_DATABASE_URL")
        return self

    @model_validator(mode="after")
    def validate_cors_settings(self) -> "Settings":
        origins = self.cors_origins
        if self.cors_allow_credentials and "*" in origins:
            raise ValueError("CORS wildcard is forbidden when credentials are enabled")
        if self.service_mode == "product" and not origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must be explicit in product mode")
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """????????(????)"""
    return Settings()
