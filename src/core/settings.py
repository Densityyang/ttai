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
    database_pool_size: int = Field(default=3, ge=1, le=50)
    database_max_overflow: int = Field(default=2, ge=0, le=50)
    database_pool_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    database_pool_recycle_seconds: int = Field(default=900, ge=30, le=86_400)
    database_connect_timeout_seconds: int = Field(default=3, ge=1, le=60)
    database_statement_timeout_ms: int = Field(default=30_000, ge=100, le=900_000)
    database_lock_timeout_ms: int = Field(default=3_000, ge=100, le=60_000)
    database_idle_transaction_timeout_ms: int = Field(
        default=30_000,
        ge=1_000,
        le=900_000,
    )
    query_gateway_sql_active_concurrency: int = Field(
        default=4,
        ge=1,
        le=4,
        description="Per-API bootstrap ceiling for active business SQL queries.",
    )
    query_gateway_sql_wait_queue_size: int = Field(
        default=8,
        ge=0,
        le=1_024,
        description="Per-API bounded waiting queue for business SQL capacity.",
    )
    query_gateway_sql_wait_timeout_seconds: float = Field(
        default=3.0,
        gt=0,
        le=60,
        description="Maximum wait for QueryGateway SQL capacity.",
    )

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
    # Deployment-level typed-runtime activation.  "disabled" (the default) keeps
    # the EXISTING v2 product path and leaves the typed runtime DORMANT.  The
    # ONLY enabling value is "trusted_backend_authorization", which requires the
    # trusted Backend Agent AuthorizationContext carrier to be configured.  There
    # is deliberately no generic boolean "security off" switch and no
    # per-request bypass: the decision is taken once, deployment-wide, when the
    # engine is built.
    typed_runtime_activation: Literal[
        "disabled",
        "trusted_backend_authorization",
        "demo_synthetic_authorization",
        "local_real_data_demo",
    ] = Field(
        default="disabled",
        description=(
            "Deployment-level typed-runtime activation.  "
            "trusted_backend_authorization uses the real Backend carrier.  "
            "demo_synthetic_authorization issues SYNTHETIC demo authority and is "
            "legal ONLY when service_mode is infra-dev.  "
            "local_real_data_demo reads the REAL business database read-only under "
            "a server-owned local authority, and is legal ONLY when service_mode "
            "is infra-dev."
        ),
    )
    api_port: int = Field(default=9001, description="API ????")
    service_mode: Literal["infra-dev", "product"] = Field(default="infra-dev")
    # SERVER-OWNED demo identity, used ONLY when auth is disabled AND demo
    # activation is on.  It is never taken from a request body/query/header, so
    # a client can never select its own demo identity.
    local_real_demo_user_id: str = Field(
        default="local-real-demo",
        min_length=1,
        max_length=128,
        description=(
            "Server-owned identity used when AUTH_ENABLED=false and "
            "TYPED_RUNTIME_ACTIVATION=local_real_data_demo."
        ),
    )
    local_demo_certification_admin_user_id: str = Field(
        default="local-real-demo",
        min_length=1,
        max_length=128,
        description=(
            "INDEPENDENT server-owned authority for LOCAL-DEMO certification. "
            "Authority comes from this setting, never from publication ownership."
        ),
    )
    demo_synthetic_user_id: str = Field(
        default="demo-analyst",
        min_length=1,
        max_length=128,
        description=(
            "Synthetic identity used when AUTH_ENABLED=false and "
            "TYPED_RUNTIME_ACTIVATION=demo_synthetic_authorization."
        ),
    )
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
        if self.service_mode == "product" and not self.auth_enabled:
            raise ValueError("AUTH_ENABLED=false is not permitted in product mode")
        if self.auth_enabled and not self.tt_api_base_url.strip():
            raise ValueError("AUTH_ENABLED=true ????? TT_API_BASE_URL")
        return self

    @model_validator(mode="after")
    def validate_memory_backend(self) -> "Settings":
        if self.memory_backend == "postgresql" and not self.checkpoint_database_url:
            raise ValueError("MEMORY_BACKEND=postgresql ????? CHECKPOINT_DATABASE_URL")
        return self

    @model_validator(mode="after")
    def validate_product_database_roles(self) -> "Settings":
        if self.service_mode != "product":
            return self

        from src.core.database import DatabasePurpose, validate_application_database_url

        required_urls = {
            DatabasePurpose.BUSINESS_READ_ONLY: self.database_url,
            DatabasePurpose.CONTROL_APP: self.control_database_url,
            DatabasePurpose.CHECKPOINT_APP: self.checkpoint_database_url,
        }
        for purpose, database_url in required_urls.items():
            if database_url:
                validate_application_database_url(database_url, purpose)
        return self

    @model_validator(mode="after")
    def validate_demo_activation_is_never_product(self) -> "Settings":
        """Demo synthetic authority must be structurally impossible in product.

        Evaluated at Settings CONSTRUCTION, so the forbidden pair can never be
        minted - not even transiently - and cache clearing cannot skip it.
        """

        if self.service_mode == "product" and self.typed_runtime_activation in (
            "demo_synthetic_authorization",
            "local_real_data_demo",
        ):
            raise ValueError(
                "typed_runtime_activation="
                f"{self.typed_runtime_activation} is not permitted when "
                "service_mode=product: demo/local authority must never be "
                "reachable in a product deployment"
            )
        if (
            self.service_mode != "product"
            and self.typed_runtime_activation == "demo_synthetic_authorization"
            and not self.auth_enabled
        ):
            # The configured demo identity must be an EXPLICIT demo fixture, so a
            # typo fails at startup instead of silently yielding no authority.
            from src.core.auth.demo_provider import DEFAULT_DEMO_IDENTITIES

            known = {item.user_id for item in DEFAULT_DEMO_IDENTITIES}
            if self.demo_synthetic_user_id not in known:
                raise ValueError(
                    "demo_synthetic_user_id must name an explicit demo fixture "
                    f"identity (known: {sorted(known)})"
                )
        if (
            self.service_mode != "product"
            and self.typed_runtime_activation == "local_real_data_demo"
            and not self.auth_enabled
        ):
            from src.core.auth.local_real_provider import (
                DEFAULT_LOCAL_REAL_IDENTITIES,
            )

            local_known = {item.user_id for item in DEFAULT_LOCAL_REAL_IDENTITIES}
            if self.local_real_demo_user_id not in local_known:
                raise ValueError(
                    "local_real_demo_user_id must name an explicit local-real "
                    f"fixture identity (known: {sorted(local_known)})"
                )
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

    @property
    def typed_runtime_enabled(self) -> bool:
        """True for a deployment that enabled a typed path.

        Both enabling values turn the typed runtime ON, but their authority
        construction is COMPLETELY DISTINCT: trusted_backend_authorization reads
        the real Backend carrier, while demo_synthetic_authorization issues
        synthetic demo authority (legal only outside product mode).
        """

        return self.typed_runtime_activation in (
            "trusted_backend_authorization",
            "demo_synthetic_authorization",
            "local_real_data_demo",
        )

    @property
    def demo_synthetic_authorization_enabled(self) -> bool:
        """True only for the EXPLICIT synthetic demo activation.

        LOCAL-REAL activation is deliberately NOT included: the synthetic demo
        must remain completely DB-free, and local-real is a distinct profile.
        """

        return self.typed_runtime_activation == "demo_synthetic_authorization"

    @property
    def local_real_data_demo_enabled(self) -> bool:
        """True only for the EXPLICIT local real-data profile.  Never implied."""

        return self.typed_runtime_activation == "local_real_data_demo"


@lru_cache
def get_settings() -> Settings:
    """????????(????)"""
    return Settings()
