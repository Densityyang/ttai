"""Application-scoped runtime dependencies; no request path relies on module globals."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.settings import get_settings
from src.nl2sql.infra.llm.gateway import ModelGateway, build_model_gateway
from src.nl2sql.infra.memory.checkpointer import CheckpointerManager
from src.nl2sql.observability.control_audit import ControlAuditStore

logger = logging.getLogger(__name__)


class RuntimeDependencyUnavailable(RuntimeError):
    """A required runtime dependency is unavailable without exposing its secret details."""


class AppContainer:
    """Own the resources for one FastAPI application instance and its lifespan."""

    def __init__(self) -> None:
        self._checkpointer_manager = CheckpointerManager()
        self._engine: Any | None = None
        self._engine_lock = asyncio.Lock()
        self._model_gateway: ModelGateway | None = None
        self._audit_store: ControlAuditStore | None = None
        self._checkpoint_available = False
        self._startup_failures: set[str] = set()
        # S1c: application-scoped deployment inputs for the typed runtime
        # factory.  Only the parsed views config, the control read callables and
        # the ONE shared QueryGateway live here; the request-scoped objects
        # (registry, evidence provider, resolver, deterministic provider,
        # compiler, executor, identity, authorization) are NEVER stored here.
        self._typed_deployment: tuple[Any, Any, Any, Any] | None = None
        self._typed_resources: tuple[Any, Any, Any] | None = None

    async def start(self) -> None:
        """Initialize only state persistence; migrations and index builds are external jobs."""

        try:
            await self._checkpointer_manager.init(setup=False)
            self._checkpoint_available = True
        except Exception as exc:
            self._startup_failures.add("checkpoint_initialization_failed")
            logger.error("checkpoint initialization failed: %s", type(exc).__name__)
        from src.core.settings import get_settings

        settings = get_settings()
        if settings.control_database_url:
            try:
                self._audit_store = await ControlAuditStore.open(settings.control_database_url)
            except Exception as exc:
                self._startup_failures.add("control_audit_initialization_failed")
                logger.error("control audit initialization failed: %s", type(exc).__name__)

    @property
    def audit_available(self) -> bool:
        return self._audit_store is not None

    @property
    def checkpoint_available(self) -> bool:
        return self._checkpoint_available

    def readiness_report(self, *, model_available: bool) -> dict[str, object]:
        """Return profile-aware status without leaking endpoints, DSNs, or exception text."""

        from src.core.settings import get_settings

        settings = get_settings()
        control_required = settings.service_mode == "product"
        checkpoint_ready = self.checkpoint_available and not (
            settings.service_mode == "product" and settings.memory_backend != "postgresql"
        )
        component_states = {
            "checkpoint": {
                "status": "ready" if checkpoint_ready else "unavailable",
                "required": True,
            },
            "control_audit": {
                "status": "ready" if self.audit_available else "unavailable",
                "required": control_required,
            },
            "model": {
                "status": "ready" if model_available else "unavailable",
                "required": settings.model_required,
            },
        }
        unavailable_required = [
            name
            for name, state in component_states.items()
            if state["required"] and state["status"] != "ready"
        ]
        degradation_reasons = sorted(self._startup_failures)
        if not model_available:
            degradation_reasons.append("model_provider_unavailable")
        if not self.audit_available:
            degradation_reasons.append("control_audit_unavailable")
        if not self.checkpoint_available:
            degradation_reasons.append("checkpoint_unavailable")
        elif not checkpoint_ready:
            degradation_reasons.append("product_checkpoint_backend_not_durable")
        return {
            "status": "ready" if not unavailable_required else "not_ready",
            "service_mode": settings.service_mode,
            "components": component_states,
            "degradation_reasons": tuple(dict.fromkeys(degradation_reasons)),
        }

    async def get_engine(self) -> Any:
        if not self._checkpoint_available:
            raise RuntimeDependencyUnavailable("checkpoint persistence is unavailable")
        if self._engine is not None:
            return self._engine
        async with self._engine_lock:
            if self._engine is None:
                from src.nl2sql.orchestration.engine import create_v2_engine

                try:
                    self._model_gateway = build_model_gateway()
                except ValueError as exc:
                    raise RuntimeDependencyUnavailable(
                        "model provider configuration is unavailable"
                    ) from exc
                self._engine = create_v2_engine(
                    checkpointer=self._checkpointer_manager.checkpointer,
                    model_gateway=self._model_gateway,
                    trace_sink=self._audit_store,
                    typed_runtime_factory=self._configured_typed_runtime_factory(),
                )
        return self._engine

    def _configured_typed_runtime_factory(self) -> Any | None:
        """Return the typed factory ONLY for a deployment that activated it.

        DEPLOYMENT-LEVEL by construction: the decision is read once from the
        immutable Settings when the engine is built, so no request can enable or
        disable the typed path.  When disabled (the default) the engine is built
        WITHOUT the factory and the existing v2 product path is unchanged.  When
        enabled, EVERY request in the deployment goes through the trusted typed
        path, which fails closed on missing/malformed authorization and has no
        edge back to the legacy/no-auth execution path.
        """

        if not get_settings().typed_runtime_enabled:
            return None
        return self._typed_runtime_factory()

    def _typed_runtime_factory(self) -> Any:
        """Return the application-scoped factory CALLABLE for the engine.

        The engine stores only this callable; the factory output is built per
        request.  Without a trusted AuthorizationContext it fails closed with
        the canonical authorization_context_missing outcome and never touches
        the deployment, so no business SQL can be opened without authority.
        """

        async def factory(
            *,
            identity: Any,
            authorization: Any,
            expected_revision: str | None,
        ) -> Any:
            from src.nl2sql.contracts import AuthorizationContext
            from src.nl2sql.orchestration.typed_runtime import (
                TypedRuntimeUnavailable,
                build_request_typed_runtime,
            )

            if not isinstance(authorization, AuthorizationContext):
                return TypedRuntimeUnavailable(reason="authorization_context_missing")
            deployment = await self._typed_deployment_inputs()
            if deployment is None:
                return TypedRuntimeUnavailable(reason="typed_deployment_unavailable")
            views, read_active, read_snapshot, gateway = deployment
            return await build_request_typed_runtime(
                views=views,
                read_active=read_active,
                read_snapshot=read_snapshot,
                gateway=gateway,
                identity=identity,
                authorization=authorization,
                expected_revision=expected_revision,
            )

        return factory

    async def _typed_deployment_inputs(self) -> tuple[Any, Any, Any, Any] | None:
        """Lazily build and reuse the application-scoped deployment inputs."""

        if self._typed_deployment is not None:
            return self._typed_deployment
        try:
            from pathlib import Path

            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            from src.core.database import DatabasePurpose, create_runtime_async_engine
            from src.core.settings import ROOT_DIR, get_settings
            from src.nl2sql.config.settings import get_agent_config
            from src.nl2sql.infra.governance.query_gateway import QueryGateway
            from src.nl2sql.infra.store.ai_views import load_ai_views_config
            from src.nl2sql.semantic.registry import ControlSemanticReleasePublisher
            from src.nl2sql.semantic.schema_snapshot import ControlSchemaSnapshotStore

            settings = get_settings()
            if not settings.control_database_url or not settings.database_url:
                return None
            agent_config = get_agent_config()
            views_path = Path(agent_config.ai_views_config_path)
            if not views_path.is_absolute():
                views_path = ROOT_DIR / views_path
            views = load_ai_views_config(str(views_path))
            publisher = ControlSemanticReleasePublisher(settings.control_database_url)
            snapshots = ControlSchemaSnapshotStore(settings.control_database_url)
            business_engine = create_runtime_async_engine(
                settings.database_url,
                purpose=DatabasePurpose.BUSINESS_READ_ONLY,
                application_name="ttai-typed-runtime",
                settings=settings,
            )
            session_factory = async_sessionmaker(
                business_engine, class_=AsyncSession, expire_on_commit=False
            )
            gateway = QueryGateway(session_factory, schema=agent_config.nl2sql_db_schema)
        except Exception as exc:
            logger.error("typed runtime deployment unavailable: %s", type(exc).__name__)
            return None
        self._typed_deployment = (views, publisher.read_active, snapshots.read, gateway)
        self._typed_resources = (publisher, snapshots, business_engine)
        return self._typed_deployment

    async def close(self) -> None:
        self._engine = None
        self._model_gateway = None
        if self._audit_store is not None:
            await self._audit_store.close()
            self._audit_store = None
        if self._typed_resources is not None:
            publisher, snapshots, business_engine = self._typed_resources
            self._typed_resources = None
            self._typed_deployment = None
            for resource in (snapshots, publisher):
                try:
                    await resource.close()
                except Exception:
                    logger.error("typed resource close failed")
            try:
                await business_engine.dispose()
            except Exception:
                pass
        await self._checkpointer_manager.close()
        self._checkpoint_available = False
