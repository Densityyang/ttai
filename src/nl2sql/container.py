"""Application-scoped runtime dependencies; no request path relies on module globals."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
                )
        return self._engine

    async def close(self) -> None:
        self._engine = None
        self._model_gateway = None
        if self._audit_store is not None:
            await self._audit_store.close()
            self._audit_store = None
        await self._checkpointer_manager.close()
        self._checkpoint_available = False
