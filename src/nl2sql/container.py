"""Application-scoped runtime dependencies; no request path relies on module globals."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.nl2sql.artifacts.control_store import ControlArtifactRepository
    from src.nl2sql.artifacts.custom_definition_execution_service import (
        CustomDefinitionExecutionService,
    )
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.library_control_store import ControlLibraryRepository
    from src.nl2sql.artifacts.personal_conflict_product_service import (
        PersonalConflictProductService,
    )
    from src.nl2sql.artifacts.product_library_service import ProductLibraryService
    from src.nl2sql.artifacts.publication import PublicationCatalogue
    from src.nl2sql.artifacts.publication_control_store import (
        ControlPublicationCatalogue,
    )
    from src.nl2sql.artifacts.publication_service import PublicationService
    from src.nl2sql.artifacts.repository import InMemoryArtifactRepository
    from src.nl2sql.artifacts.service import CustomDefinitionService
    from src.nl2sql.orchestration.governed_calculation_inputs import (
        GovernedMetricInputFetcher,
        TypedMetricCalculationInputResolver,
    )

from src.core.auth.provider import BackendAuthorizationProvider
from src.core.settings import get_settings
from src.nl2sql.infra.llm.gateway import ModelGateway, build_model_gateway
from src.nl2sql.infra.llm.model_input_policy import ModelInputPolicyUncalibrated
from src.nl2sql.infra.memory.checkpointer import CheckpointerManager
from src.nl2sql.observability.control_audit import ControlAuditStore

logger = logging.getLogger(__name__)


class RuntimeDependencyUnavailable(RuntimeError):
    """A required runtime dependency is unavailable without exposing its secret details."""


class AppContainer:
    """Own the resources for one FastAPI application instance and its lifespan."""

    def __init__(
        self,
        *,
        governed_metric_input_fetcher: GovernedMetricInputFetcher | None = None,
        governed_metric_key_resolver: Callable[[str], bool] | None = None,
    ) -> None:
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
        # Application-scoped product services.  These are DEMO/LOCAL and
        # NON-DURABLE: they live for the process lifetime only, deliberately so
        # for Foundation B, and state is LOST on restart.  A future Control-PG
        # implementation must be swappable behind the same service interfaces.
        self._local_real_deployment: tuple[Any, Any, Any, Any] | None = None
        self._local_real_resources: tuple[Any, Any] | None = None
        self._local_real_probe_attempted = False
        self._local_real_readiness: dict[str, object] | None = None
        self._local_real_latest_authoritative_date: str | None = None
        self._local_real_recent_authoritative_dates: tuple[str, ...] = ()
        self._local_real_source_watermark: Any | None = None
        self._local_real_source_watermark_checked_at: datetime | None = None
        self._local_real_count_column = "id"
        self._definition_service: CustomDefinitionService | None = None
        self._artifact_repository: (
            InMemoryArtifactRepository | ControlArtifactRepository | None
        ) = None
        self._library_repository: (
            InMemoryLibraryRepository | ControlLibraryRepository | None
        ) = None
        self._publication_catalogue: (
            PublicationCatalogue | ControlPublicationCatalogue | None
        ) = None
        self._product_store_ready = False
        self._publication_service: PublicationService | None = None
        self._product_library_service: ProductLibraryService | None = None
        self._personal_conflict_product_service: (
            PersonalConflictProductService | None
        ) = None
        self._custom_definition_execution_service: (
            CustomDefinitionExecutionService | None
        ) = None
        self._governed_metric_input_fetcher = governed_metric_input_fetcher
        self._governed_metric_key_resolver = governed_metric_key_resolver
        self._calculation_input_resolver: (
            TypedMetricCalculationInputResolver | None
        ) = None

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

        # The product stores are DURABLE only when they are control-backed.  A
        # product deployment may not run the process-local stores, so the
        # backend comes from immutable Settings and the control-backed stores
        # are built and PINGED here, while this container owns the lifecycle.
        # A store that cannot be reached is never reported as ready.
        if self.product_store_backend == "control":
            try:
                for store in (
                    await self.artifact_repository(),
                    await self.publication_catalogue(),
                    await self.library_repository(),
                ):
                    await store.ping()
                self._product_store_ready = True
            except Exception as exc:
                self._startup_failures.add("product_store_initialization_failed")
                logger.error(
                    "product store initialization failed: %s", type(exc).__name__
                )
        else:
            self._product_store_ready = True

    @property
    def audit_available(self) -> bool:
        return self._audit_store is not None

    @property
    def checkpoint_available(self) -> bool:
        return self._checkpoint_available

    def get_backend_authorization_provider(self) -> BackendAuthorizationProvider | None:
        """Return this deployment's authorization provider, or None.

        The branch is decided ONLY by the immutable deployment setting - never by
        a user's roles, organization or job title, and never by request data.

        The demo provider is constructed INSIDE its own branch and is never
        cached on the container, so it cannot become reachable in product mode.
        There is deliberately NO fallback: a trusted deployment whose real
        provider is unavailable returns None (fail closed) and must never
        silently degrade into demo authority.
        """

        from src.core.settings import get_settings

        settings = get_settings()
        activation = settings.typed_runtime_activation

        if activation == "demo_synthetic_authorization":
            # Structurally unreachable in product mode: Settings construction
            # rejects that pair, so this branch cannot be selected there.
            from src.core.auth.demo_provider import DemoBackendAuthorizationProvider

            return DemoBackendAuthorizationProvider()

        if activation == "local_real_data_demo":
            # A DISTINCT local-real authority.  No production fallback and no
            # synthetic fallback: this profile returns ONLY this provider.
            from src.core.auth.local_real_provider import LocalRealAuthorizationProvider

            return LocalRealAuthorizationProvider()

        if activation == "trusted_backend_authorization":
            # The concrete Backend endpoint/payload/revision contract does not
            # exist yet, so this stays None and the typed runtime fails closed
            # rather than synthesizing an authorization context.
            return None

        return None

    def authority_provenance(self) -> str:
        """Which authority answered: backend | demo | unavailable.

        Derived from the SAME single predicate as provider selection, so the
        two can never disagree.  Presentation only - it grants nothing.
        """

        from src.core.settings import get_settings

        activation = get_settings().typed_runtime_activation
        if activation == "demo_synthetic_authorization":
            return "demo"
        if activation == "local_real_data_demo":
            return "local_real_demo"
        if activation == "trusted_backend_authorization":
            return "backend" if self.get_backend_authorization_provider() else "unavailable"
        return "unavailable"

    def readiness_report(self, *, model_available: bool) -> dict[str, object]:
        """Return profile-aware status without leaking endpoints, DSNs, or exception text."""

        from src.core.settings import get_settings

        settings = get_settings()
        control_required = settings.service_mode == "product"
        checkpoint_ready = self.checkpoint_available and not (
            settings.service_mode == "product" and settings.memory_backend != "postgresql"
        )
        component_states: dict[str, dict[str, object]] = {
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
            "product_store": {
                "status": "ready" if self._product_store_ready else "unavailable",
                # Only a control-backed store is REQUIRED to be reachable; the
                # process-local backend is deliberately non-durable in infra-dev.
                "required": self.product_store_backend == "control",
            },
        }
        if settings.local_real_data_demo_enabled:
            local_readiness = self._local_real_readiness
            if local_readiness is None:
                typed_runtime: dict[str, object] = {
                    "status": "unavailable",
                    "required": True,
                    "reason": "local_real_not_probed",
                }
            else:
                typed_runtime = dict(local_readiness)
                typed_runtime["required"] = True
            component_states["typed_runtime"] = typed_runtime
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
        typed_component = component_states.get("typed_runtime")
        if isinstance(typed_component, dict) and typed_component.get("status") != "ready":
            reason = typed_component.get("reason")
            if isinstance(reason, str):
                degradation_reasons.append(reason)
        return {
            "status": "ready" if not unavailable_required else "not_ready",
            "service_mode": settings.service_mode,
            "components": component_states,
            "degradation_reasons": tuple(dict.fromkeys(degradation_reasons)),
        }

    async def prepare_readiness(self) -> dict[str, object]:
        """Probe required local-real deployment once for this app lifecycle."""

        from src.core.settings import get_settings

        settings = get_settings()
        if settings.local_real_data_demo_enabled and self._local_real_readiness is None:
            deployment = await self._local_real_deployment_inputs()
            if deployment is None:
                reason = (
                    "local_real_database_not_configured"
                    if not settings.database_url
                    else "local_real_deployment_unavailable"
                )
                self._local_real_readiness = {
                    "status": "unavailable",
                    "reason": reason,
                }
            else:
                self._local_real_readiness = {"status": "ready"}
        return self.readiness_report(model_available=False)

    def custom_definition_service(self) -> CustomDefinitionService:
        """The application-scoped definition service (DEMO/local, non-durable)."""

        if self._definition_service is None:
            from src.nl2sql.artifacts.service import CustomDefinitionService

            resolver = self._governed_metric_key_resolver
            try:
                settings = get_settings()
            except Exception:
                # Direct service unit tests may construct a container without
                # bootstrapping application Settings; retain the service's
                # explicit fixture seam in that non-application context.
                settings = None
            if resolver is None and self._governed_metric_input_fetcher is not None:
                # Explicit injected fetchers are the controlled B-owned test
                # seam; bind them to the frozen governed input identity rather
                # than reviving a mixed global allowlist.
                def injected_resolver(key: str) -> bool:
                    return key == "repair_service_archive_rate_overall_day"

                resolver = injected_resolver
            elif resolver is None and settings is not None and settings.local_real_data_demo_enabled:
                from src.nl2sql.local_real.deployment import (
                    FROZEN_REAL_CASE_METRIC_KEY,
                )

                def local_resolver(key: str) -> bool:
                    return key == FROZEN_REAL_CASE_METRIC_KEY

                resolver = local_resolver

            elif resolver is None and settings is not None and settings.demo_synthetic_authorization_enabled:
                from src.nl2sql.demo.fixtures import DEMO_METRICS_BY_KEY

                def demo_resolver(key: str) -> bool:
                    return key in DEMO_METRICS_BY_KEY

                resolver = demo_resolver

            elif resolver is None and settings is not None and (
                settings.service_mode == "product"
                or settings.typed_runtime_activation
                == "trusted_backend_authorization"
            ):
                # No trusted active semantic release is available to this
                # process until the deployment injects one; fail closed.
                def deny_resolver(_key: str) -> bool:
                    return False

                resolver = deny_resolver
            self._definition_service = CustomDefinitionService(
                governed_metric_key_resolver=resolver
            )
        return self._definition_service

    @property
    def product_store_backend(self) -> str:
        """Which persistence backend the product stores use.

        MASTER_PR_PLAN_V4.md 5.4.1 puts artifacts, their hashes and their
        lifecycle in Control PostgreSQL.  Unset resolves to "control" in product
        mode and "memory" otherwise, and Settings refuses an explicit "memory"
        in product mode, so a product deployment cannot silently run the
        process-local stores that lose state on restart.
        """

        from src.core.settings import get_settings

        settings = get_settings()
        return settings.product_store_backend or (
            "control" if settings.service_mode == "product" else "memory"
        )

    @property
    def product_store_available(self) -> bool:
        return self._product_store_ready

    # Accessor convention: an accessor is async exactly when building the object
    # can await I/O (the catalogue, the library and the product services may open
    # a database).  Pure constructors -- custom_definition_service() and
    # custom_definition_execution_service() -- stay synchronous on purpose, so
    # there is no coroutine a caller could forget to await.
    async def artifact_repository(
        self,
    ) -> InMemoryArtifactRepository | ControlArtifactRepository:
        """The application-scoped artifact repository.

        Owner-scoped and fail-closed in BOTH backends; only durability differs.
        """

        if self._artifact_repository is None:
            if self.product_store_backend == "control":
                from src.core.settings import get_settings
                from src.nl2sql.artifacts.control_store import (
                    ControlArtifactRepository,
                )

                self._artifact_repository = ControlArtifactRepository(
                    get_settings().control_database_url or ""
                )
            else:
                from src.nl2sql.artifacts.repository import InMemoryArtifactRepository

                self._artifact_repository = InMemoryArtifactRepository()
        return self._artifact_repository

    async def publication_catalogue(
        self,
    ) -> PublicationCatalogue | ControlPublicationCatalogue:
        """The ONE application-scoped publication catalogue.

        Publication and Library MUST share this single instance: a publication
        made through the Definition API is immediately visible to the Library
        API within the same process.
        """

        if self._publication_catalogue is None:
            from src.nl2sql.artifacts.library import seed_catalogue_from_fixtures

            if self.product_store_backend == "control":
                from src.core.settings import get_settings
                from src.nl2sql.artifacts.publication_control_store import (
                    ControlPublicationCatalogue,
                )

                catalogue: PublicationCatalogue | ControlPublicationCatalogue = (
                    ControlPublicationCatalogue(
                        get_settings().control_database_url or ""
                    )
                )
            else:
                from src.nl2sql.artifacts.publication import PublicationCatalogue

                catalogue = PublicationCatalogue()
            # Legacy demo fixtures are ADAPTED into the shared catalogue so the
            # existing demo catalogue stays discoverable; they carry no semantic
            # package and are therefore not forkable.  Seeding is idempotent and
            # fail-closed on an existing publication in BOTH backends.
            await seed_catalogue_from_fixtures(catalogue)
            self._publication_catalogue = catalogue
        return self._publication_catalogue

    async def publication_service(self) -> PublicationService:
        """Application-scoped publication coordinator over the shared catalogue."""

        if self._publication_service is None:
            from src.nl2sql.artifacts.publication_service import PublicationService

            self._publication_service = PublicationService(
                definitions=self.custom_definition_service(),
                catalogue=await self.publication_catalogue(),
            )
        return self._publication_service

    async def product_library_service(self) -> ProductLibraryService:
        """Application-scoped product library orchestration.

        It is bound to the SAME definition service, the SAME publication
        service, the SAME catalogue and the SAME personal-state repository as
        every other accessor, so no route can observe a second catalogue.
        """

        if self._product_library_service is None:
            from src.core.settings import get_settings
            from src.nl2sql.artifacts.product_library_service import (
                CertificationAuthority,
                build_product_library_service,
            )

            settings = get_settings()
            self._product_library_service = build_product_library_service(
                catalogue=await self.publication_catalogue(),
                library=await self.library_repository(),
                definitions=self.custom_definition_service(),
                publications=await self.publication_service(),
                # The certification authority is read from immutable Settings at
                # construction time and is NEVER inferred from a role, an
                # organization or a publication ownership.
                certification_authority=CertificationAuthority(
                    service_mode=settings.service_mode,
                    typed_runtime_activation=settings.typed_runtime_activation,
                    admin_user_id=settings.local_demo_certification_admin_user_id,
                ),
            )
        return self._product_library_service

    async def library_repository(
        self,
    ) -> InMemoryLibraryRepository | ControlLibraryRepository:
        """The application-scoped library repository over the SHARED catalogue.

        Personal state only (installs, Stars, withdrawal acknowledgements); the
        catalogue stays the single authority for versions and the current
        pointer in BOTH backends.
        """

        if self._library_repository is None:
            catalogue = await self.publication_catalogue()
            if self.product_store_backend == "control":
                from src.core.settings import get_settings
                from src.nl2sql.artifacts.library_control_store import (
                    ControlLibraryRepository,
                )

                self._library_repository = ControlLibraryRepository(
                    catalogue=catalogue,
                    database_url=get_settings().control_database_url or "",
                )
            else:
                from src.nl2sql.artifacts.library import InMemoryLibraryRepository

                self._library_repository = InMemoryLibraryRepository(
                    catalogue=catalogue
                )
        return self._library_repository

    async def personal_conflict_product_service(self) -> PersonalConflictProductService:
        """Application-scoped conflict service over the existing shared stores."""

        if self._personal_conflict_product_service is None:
            from src.nl2sql.artifacts.personal_conflict_product_service import (
                PersonalConflictProductService,
            )

            self._personal_conflict_product_service = PersonalConflictProductService(
                definitions=self.custom_definition_service(),
                catalogue=await self.publication_catalogue(),
                library=await self.library_repository(),
            )
        return self._personal_conflict_product_service

    def custom_definition_execution_service(self) -> CustomDefinitionExecutionService:
        """Application-scoped Mode3 service; real resolver binding is deferred."""

        if self._custom_definition_execution_service is None:
            from src.nl2sql.artifacts.custom_definition_execution_service import (
                CustomDefinitionExecutionService,
            )

            self._custom_definition_execution_service = CustomDefinitionExecutionService(
                definitions=self.custom_definition_service(),
                input_resolver=self.calculation_input_resolver(),
            )
        return self._custom_definition_execution_service

    def calculation_input_resolver(self) -> TypedMetricCalculationInputResolver | None:
        """The one generic resolver, present only when a governed fetcher is injected."""

        if self._governed_metric_input_fetcher is None:
            if get_settings().local_real_data_demo_enabled:
                from src.nl2sql.local_real.governed_inputs import (
                    LocalRealGovernedMetricInputFetcher,
                )

                self._governed_metric_input_fetcher = (
                    LocalRealGovernedMetricInputFetcher(self)
                )
            else:
                return None
        if self._calculation_input_resolver is None:
            from src.nl2sql.orchestration.governed_calculation_inputs import (
                TypedMetricCalculationInputResolver,
            )

            self._calculation_input_resolver = TypedMetricCalculationInputResolver(
                self._governed_metric_input_fetcher
            )
        return self._calculation_input_resolver


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
                    # P2-S2/R2: build_model_gateway injects an explicitly
                    # deployment-approved model-input policy when one is
                    # supplied; otherwise the gateway runs a BOOTSTRAP policy
                    # derived from configured targets, which is never
                    # production-ready.  A product deployment asserts readiness
                    # here, so an unconfigured (or destination-empty) policy is
                    # a startup failure rather than silently approved.  Outside
                    # product mode this is a no-op and the path is unchanged.
                    self._model_gateway.require_model_input_policy_ready(
                        product_mode=get_settings().service_mode == "product"
                    )
                except (ValueError, ModelInputPolicyUncalibrated) as exc:
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

        activation = get_settings().typed_runtime_activation
        if activation == "demo_synthetic_authorization":
            # A DISTINCT demo factory: it builds a standalone synthetic runtime
            # and never touches production deployment inputs.
            return self._demo_typed_runtime_factory()
        if activation == "local_real_data_demo":
            # Local-real uses the EXISTING typed pipeline through a deployment
            # adapter; it is NOT the synthetic runtime and does NOT require
            # production Control-DB publication.
            return self._local_real_typed_runtime_factory()
        if activation == "trusted_backend_authorization":
            return self._typed_runtime_factory()
        # disabled (the default) keeps the existing v2 product path unchanged.
        # There is deliberately NO fallback between demo and production.
        return None

    def _demo_typed_runtime_factory(self) -> Any:
        """Return the DEMO factory callable.  No DB, no control store, no gateway.

        It accepts only a valid AuthorizationContext whose revision is in the
        demo-synthetic namespace and whose identity is an explicit demo fixture;
        anything else fails closed with TypedRuntimeUnavailable.
        """

        async def factory(
            *,
            identity: Any,
            authorization: Any,
            expected_revision: str | None,
        ) -> Any:
            from src.core.auth.demo_provider import DEMO_REVISION_PREFIX
            from src.nl2sql.contracts import AuthorizationContext
            from src.nl2sql.demo.runtime import build_demo_runtime
            from src.nl2sql.orchestration.typed_runtime import TypedRuntimeUnavailable

            if not isinstance(authorization, AuthorizationContext):
                return TypedRuntimeUnavailable(reason="authorization_context_missing")
            if not authorization.authorization_revision.startswith(DEMO_REVISION_PREFIX):
                # A non-demo (i.e. production-looking) revision never enters here.
                return TypedRuntimeUnavailable(reason="demo_authorization_revision_required")
            runtime = build_demo_runtime(
                identity=identity,
                authorization=authorization,
                expected_revision=expected_revision,
            )
            if runtime is None:
                return TypedRuntimeUnavailable(reason="demo_identity_not_fixture")
            return runtime

        return factory

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

    def _local_real_typed_runtime_factory(self) -> Any:
        """Factory for local-real: the EXISTING typed pipeline, real gateway.

        It does not build a second runtime or SQL executor - it supplies the
        standard (views, read_active, read_snapshot, gateway) deployment inputs
        to the SAME build_request_typed_runtime used by production.
        """

        async def factory(
            *,
            identity: Any,
            authorization: Any,
            expected_revision: str | None,
        ) -> Any:
            from src.nl2sql.contracts import AuthorizationContext
            from src.nl2sql.local_real.deployment import (
                LOCAL_REAL_BOOTSTRAP_SCAN_MAX_ROWS,
            )
            from src.nl2sql.orchestration.typed_runtime import (
                TypedRuntimeUnavailable,
                build_request_typed_runtime,
            )

            if not isinstance(authorization, AuthorizationContext):
                return TypedRuntimeUnavailable(reason="authorization_context_missing")
            deployment = await self._local_real_deployment_inputs()
            if deployment is None:
                # Fails closed: no fabricated semantics and no silent fallback.
                return TypedRuntimeUnavailable(reason="local_real_deployment_unavailable")
            if self._local_real_latest_authoritative_date is None:
                return TypedRuntimeUnavailable(
                    reason="local_real_authoritative_date_unavailable"
                )
            if not self._local_real_recent_authoritative_dates:
                return TypedRuntimeUnavailable(
                    reason="local_real_authoritative_window_unavailable"
                )
            views, read_active, read_snapshot, gateway = deployment
            return await build_request_typed_runtime(
                views=views,
                read_active=read_active,
                read_snapshot=read_snapshot,
                gateway=gateway,
                identity=identity,
                authorization=authorization,
                availability_window=self._local_real_availability_window,
                authoritative_date=self._local_real_authoritative_date_value,
                count_column=self._local_real_count_column,
                source_freshness=self._local_real_source_freshness(),
                # The published view has no index, so the deployment supplies the
                # ONE bounded bootstrap scan cap that admits the frozen case.
                bootstrap_scan_max_rows=LOCAL_REAL_BOOTSTRAP_SCAN_MAX_ROWS,
                expected_revision=expected_revision,
            )

        return factory

    def _local_real_authoritative_date_value(self) -> date:
        """Return the server-probed latest authoritative business date."""

        latest = self._local_real_latest_authoritative_date
        if latest is None:
            raise RuntimeError("local_real_authoritative_date_unavailable")
        return date.fromisoformat(latest)

    def _local_real_source_freshness(self) -> Any:
        from datetime import UTC
        from zoneinfo import ZoneInfo

        from src.nl2sql.orchestration.metric_query import SourceFreshnessRecord

        resources = self._local_real_resources
        watermark = self._local_real_source_watermark
        if watermark is None or resources is None:
            return None
        deployment = resources[1]
        if watermark.tzinfo is None or watermark.utcoffset() is None:
            watermark = watermark.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        watermark_utc = watermark.astimezone(UTC)
        watermark_label = (
            watermark_utc.strftime("%Y%m%d")
            + "t"
            + watermark_utc.strftime("%H%M%S")
            + "z"
        )
        return SourceFreshnessRecord(
            source_id="v_repair_service",
            status="fresh",
            data_as_of=watermark,
            checked_at=self._local_real_source_watermark_checked_at,
            checkpoint=f"local-real-source-watermark-{watermark_label}",
            release_id=deployment.release.release_id,
            snapshot_id=deployment.snapshot.snapshot_id,
            snapshot_checksum=deployment.snapshot.checksum,
        )

    def _local_real_availability_window(self) -> tuple[date, ...]:
        """Return the server-probed bounded recent business-date set."""

        dates = self._local_real_recent_authoritative_dates
        if not dates:
            raise RuntimeError("local_real_authoritative_window_unavailable")
        return tuple(date.fromisoformat(item) for item in dates)


    async def _local_real_deployment_inputs(self) -> tuple[Any, Any, Any, Any] | None:
        """Deployment inputs for local-real: in-memory semantics + REAL gateway.

        The semantic release and schema snapshot are built from in-memory local
        objects derived from the in-repo authoritative sources and a LIVE
        validated catalog slice, so production Control-DB publication is NOT
        required.  The QueryGateway is the REAL shared read-only gateway, so real
        SQL still flows through the existing compiler/validator/executor boundary.

        When the database is unreachable this returns None, and the factory reports
        the existing safe boundary: local_real_deployment_unavailable.
        """

        if self._local_real_deployment is not None:
            return self._local_real_deployment
        if self._local_real_probe_attempted:
            return None
        self._local_real_probe_attempted = True
        business_engine: Any | None = None
        try:
            from pathlib import Path

            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            from src.core.database import DatabasePurpose, create_runtime_async_engine
            from src.core.settings import ROOT_DIR, get_settings
            from src.nl2sql.config.settings import get_agent_config
            from src.nl2sql.infra.governance.query_gateway import QueryGateway
            from src.nl2sql.infra.store.ai_views import load_ai_views_config
            from src.nl2sql.local_real.deployment import (
                FROZEN_REAL_CASE_VIEW_NAME,
                PREFERRED_PUBLISHED_RELATION,
                build_local_real_deployment,
            )
            from src.nl2sql.local_real.live_probe import (
                probe_relation,
                resolve_latest_authoritative_date,
                resolve_recent_authoritative_dates,
                resolve_source_watermark,
            )
            from src.nl2sql.semantic.schema_snapshot import (
                OrganizationCoverageBinding,
                PostgresSchemaSnapshotCollector,
                RelationPolicy,
            )

            settings = get_settings()
            if not settings.database_url:
                return None
            agent_config = get_agent_config()
            views_path = Path(agent_config.ai_views_config_path)
            if not views_path.is_absolute():
                views_path = ROOT_DIR / views_path
            views = load_ai_views_config(str(views_path))
            business_engine = create_runtime_async_engine(
                settings.database_url,
                purpose=DatabasePurpose.BUSINESS_READ_ONLY,
                application_name="ttai-local-real-runtime",
                settings=settings,
            )
            session_factory = async_sessionmaker(
                business_engine, class_=AsyncSession, expire_on_commit=False
            )
            gateway = QueryGateway(session_factory, schema=agent_config.nl2sql_db_schema)
            # Probe the PREFERRED published view FIRST.  When it is absent this
            # deployment fails closed rather than silently switching to
            # direct-table metric SQL.
            relation_probe = await probe_relation(session_factory)
            if not relation_probe.view_ready:
                logger.error(
                    "local-real published view unavailable: %s", relation_probe.status
                )
                await business_engine.dispose()
                return None
            latest_date = await resolve_latest_authoritative_date(session_factory)
            recent_dates = await resolve_recent_authoritative_dates(session_factory)
            if not latest_date.resolved or latest_date.latest_date is None:
                logger.error("local-real authoritative date unavailable")
                await business_engine.dispose()
                return None
            if not recent_dates.resolved:
                logger.error("local-real authoritative window unavailable")
                await business_engine.dispose()
                return None
            source_watermark = await resolve_source_watermark(session_factory)
            if not source_watermark.resolved or source_watermark.data_as_of is None:
                logger.error("local-real source watermark unavailable")
                await business_engine.dispose()
                return None
            snapshot_candidate = await PostgresSchemaSnapshotCollector(
                business_engine, max_relations=1
            ).collect(
                source_identifier="local-real-business",
                approved_schemas=(views.target_schema,),
                approved_relations=(PREFERRED_PUBLISHED_RELATION,),
                # The local-real authority scope is the deployment root
                # (city_company). A relation with no declared coverage cannot be
                # safely constrained for that caller, so the frozen published
                # view MUST declare the root scope explicitly.
                policies={
                    PREFERRED_PUBLISHED_RELATION: RelationPolicy(
                        organization_coverage=(
                            OrganizationCoverageBinding(scope_level="city_company"),
                        )
                    )
                },
            )
            frozen_view = next(
                (
                    item
                    for item in views.views
                    if item.name == FROZEN_REAL_CASE_VIEW_NAME
                ),
                None,
            )
            if frozen_view is None:
                logger.error("local-real published view is not configured")
                await business_engine.dispose()
                return None
            bundle = await self._build_local_real_semantics_async(
                snapshot_candidate=snapshot_candidate,
                view=frozen_view,
            )
            deployment = build_local_real_deployment(
                views=views,
                gateway=gateway,
                release=bundle.release,
                snapshot=bundle.snapshot,
                relation_name=PREFERRED_PUBLISHED_RELATION,
                relation_probe=relation_probe,
                latest_authoritative_date=latest_date.latest_date,
                recent_authoritative_dates=recent_dates.dates,
                produced_dates={
                    "latest_authoritative": latest_date.latest_date,
                    "recent_window_start": recent_dates.dates[0],
                    "recent_window_end": recent_dates.dates[-1],
                },
                count_column="id",
            )
        except Exception as exc:
            # Fail closed and dispose a partially constructed engine.  The
            # public readiness surface receives only a stable reason; detailed
            # exception types remain in logs for operators.
            logger.error(
                "local-real deployment unavailable: %s", type(exc).__name__
            )
            if business_engine is not None:
                try:
                    await business_engine.dispose()
                except Exception:
                    logger.error("local-real engine cleanup failed")
            self._local_real_deployment = None
            self._local_real_resources = None
            self._local_real_latest_authoritative_date = None
            self._local_real_recent_authoritative_dates = ()
            self._local_real_source_watermark = None
            self._local_real_source_watermark_checked_at = None
            return None
        self._local_real_deployment = (
            deployment.views,
            deployment.read_active,
            deployment.read_snapshot,
            deployment.gateway,
        )
        self._local_real_resources = (business_engine, deployment)
        self._local_real_latest_authoritative_date = deployment.latest_authoritative_date
        self._local_real_recent_authoritative_dates = deployment.recent_authoritative_dates
        self._local_real_source_watermark = source_watermark.data_as_of
        self._local_real_source_watermark_checked_at = source_watermark.observed_at
        self._local_real_count_column = deployment.count_column
        return self._local_real_deployment

    async def _build_local_real_semantics_async(
        self, *, snapshot_candidate: Any, view: Any
    ) -> Any:
        """Build the bounded in-memory ACTIVE release bound to the LIVE snapshot.

        The authoritative sources are re-verified, the deployment binding is
        materialized for the frozen closure only, and the LIVING snapshot must
        reach VALIDATED or construction fails closed.
        """

        from src.nl2sql.infra.store.ai_views import view_output_columns
        from src.nl2sql.local_real.deployment import (
            FROZEN_REAL_CASE_DEPENDENCIES,
            FROZEN_REAL_CASE_REQUIRED_COLUMNS,
            PREFERRED_PUBLISHED_RELATION,
        )
        from src.nl2sql.local_real.semantics import build_local_real_semantics

        return build_local_real_semantics(
            metric_keys=FROZEN_REAL_CASE_DEPENDENCIES,
            relation_id=PREFERRED_PUBLISHED_RELATION,
            # The PUBLISHED view output columns are the allowed surface, exactly
            # as the typed binding model requires.
            view_columns=view_output_columns(view),
            snapshot_candidate=snapshot_candidate,
            required_columns=FROZEN_REAL_CASE_REQUIRED_COLUMNS,
        )


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
        if self._local_real_resources is not None:
            business_engine, _deployment = self._local_real_resources
            self._local_real_resources = None
            self._local_real_deployment = None
            try:
                await business_engine.dispose()
            except Exception:
                logger.error("local-real resource close failed")
        else:
            self._local_real_deployment = None
        self._local_real_probe_attempted = False
        self._local_real_readiness = None
        self._local_real_latest_authoritative_date = None
        self._local_real_recent_authoritative_dates = ()
        self._local_real_source_watermark = None
        self._local_real_source_watermark_checked_at = None
        self._local_real_count_column = "id"
        await self._checkpointer_manager.close()
        self._checkpoint_available = False
