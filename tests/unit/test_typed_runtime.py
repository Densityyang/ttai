"""P4-S1b: typed deployment adapter and request-scoped typed runtime factory."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.infra.store.ai_views import (
    AIViewsConfig,
    load_ai_views_config,
    view_output_columns,
)
from src.nl2sql.orchestration.execution import PlanExecutor
from src.nl2sql.orchestration.metric_query import EligibilityPolicy, RelationBinding
from src.nl2sql.orchestration.typed_runtime import (
    RequestTypedRuntime,
    TypedDeploymentError,
    TypedRuntimeUnavailable,
    build_eligibility_policies,
    build_relation_binding,
    build_relation_bindings,
    build_request_typed_runtime,
    executable_metric_contracts,
    resolve_eligibility_policy,
)
from src.nl2sql.semantic.authoring import validate_authoring_ir
from src.nl2sql.semantic.context_compiler import ContextCompiler, SemanticContextResolver
from src.nl2sql.semantic.materialization import materialize_authoring_ir
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    MetricContract,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.registry import SemanticRelease, SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import (
    ColumnSnapshot,
    IndexSnapshot,
    OrganizationCoverageBinding,
    RelationSnapshot,
    SchemaSnapshot,
    SchemaSnapshotCandidate,
    SchemaSnapshotState,
)

ROOT = Path(__file__).resolve().parents[2]
AI_VIEWS_PATH = ROOT / "configs" / "semantic" / "ai_views.yaml"
COMPLAINT_PATH = ROOT / "config" / "metrics" / "complaint.yaml"
VIEW_NAME = "v_fault_reporting_order"
QUALIFIED = "ai_views.v_fault_reporting_order"
RELEASE_ID = "11111111-1111-1111-1111-111111111111"
RELEASE_ID_B = "99999999-9999-9999-9999-999999999999"
SNAPSHOT_ID = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 9, 1, tzinfo=UTC)
QUESTION = "metric=complaint_in_transit_count time=2024-02-29"

# Only the non-text columns need explicit snapshot types; every other published
# output column is a text-family column for this synthetic snapshot.
_COLUMN_TYPES = {
    "acceptance_time": "timestamp with time zone",
    "completion_time": "timestamp with time zone",
    "is_valid_for_metrics": "boolean",
    "has_valid_bandwidth": "boolean",
    "is_first_response_on_time": "boolean",
    "area_id": "text",
    "team_id": "integer",
}


def _views() -> AIViewsConfig:
    return load_ai_views_config(str(AI_VIEWS_PATH))


def _view(config: AIViewsConfig):
    return next(view for view in config.views if view.name == VIEW_NAME)


def _configured_metrics() -> tuple[MetricContract, ...]:
    catalog = load_metric_catalog(COMPLAINT_PATH.read_text(encoding="utf-8"))
    return catalog.metrics


def _activate(metric: MetricContract, **changes: object) -> MetricContract:
    payload = metric.model_dump(mode="json")
    payload.update(
        owner="synthetic-owner", release_status="active", freshness_sla_seconds=86_400
    )
    payload.update(changes)
    return MetricContract.model_validate(payload)


def _active_metrics() -> tuple[MetricContract, ...]:
    return tuple(_activate(metric) for metric in _configured_metrics())


def _relation(
    *,
    drop: str | None = None,
    retype: tuple[str, str] | None = None,
) -> RelationSnapshot:
    view = _view(_views())
    names = list(view_output_columns(view))
    if drop is not None:
        names.remove(drop)
    types = {name: _COLUMN_TYPES.get(name, "text") for name in names}
    if retype is not None:
        types[retype[0]] = retype[1]
    return RelationSnapshot(
        relation_id=QUALIFIED,
        schema_name="ai_views",
        relation_name=VIEW_NAME,
        relation_kind="view",
        columns=tuple(
            ColumnSnapshot(name, types[name], True, index)
            for index, name in enumerate(names, 1)
        ),
        primary_key=(),
        foreign_keys=(),
        indexes=(IndexSnapshot("fro_acceptance_time", ("acceptance_time",), False, False),),
        partition_key=None,
        parent_relation_id=None,
        estimated_rows=1000,
        total_bytes=8192,
        sensitivity="internal",
        sensitive_columns=(),
        aggregate_coverage=(),
        freshness_sla_seconds=None,
        organization_coverage=(
            OrganizationCoverageBinding("city_company"),
            OrganizationCoverageBinding("area", "area_id", "text"),
            OrganizationCoverageBinding("team", "team_id", "integer"),
        ),
    )


def _snapshot(relation: RelationSnapshot | None = None) -> SchemaSnapshot:
    relations = () if relation is None else (relation,)
    candidate = SchemaSnapshotCandidate(
        "p4-s1b-fixture", ("ai_views",), relations, "a" * 64, "b" * 64
    )
    return SchemaSnapshot(
        SNAPSHOT_ID,
        SchemaSnapshotState.VALIDATED,
        candidate,
        {"ok": True},
        NOW,
        NOW,
    )


def _release(
    metrics: tuple[MetricContract, ...],
    *,
    release_id: str = RELEASE_ID,
    drop_relation: bool = False,
) -> SemanticRelease:
    views = _views()
    ir = metric_catalog_ir(
        MetricCatalog(metrics=metrics),
        relations={metric.source_ref: QUALIFIED for metric in metrics},
    )
    report = validate_authoring_ir(
        ir, relation_columns={QUALIFIED: view_output_columns(_view(views))}
    )
    assert report.ok, report.to_dict()
    candidate = materialize_authoring_ir(ir, report)
    documents = candidate.documents
    if drop_relation:
        documents = tuple(
            document
            for document in documents
            if document.metadata.get("asset_type") != "relation"
        )
    return SemanticRelease(
        release_id=release_id,
        version=1,
        checksum=candidate.checksum,
        state=SemanticReleaseState.ACTIVE,
        documents=documents,
        validation_report=report.to_dict(),
        change_summary="p4-s1b synthetic fixture",
        previous_release_id=None,
        created_at=NOW,
        schema_snapshot_id=SNAPSHOT_ID,
        schema_snapshot_checksum="a" * 64,
    )


def _identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=UUID(RELEASE_ID),
        user_id="synthetic-reader",
        permissions=frozenset({"nl2sql:invoke"}),
    )


def _authorization(
    *,
    agent_enabled: bool = True,
    scope_level: str = "city_company",
    allowed_scope_ids: tuple[str, ...] = ("1",),
) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision="rev-p4-s1b",
        agent_enabled=agent_enabled,
        scope_level=scope_level,  # type: ignore[arg-type]
        allowed_scope_ids=allowed_scope_ids,
    )


class _Deployment:
    """A flippable deployment double: one active release plus one snapshot."""

    def __init__(self, release: SemanticRelease, snapshot: SchemaSnapshot) -> None:
        self.release = release
        self.snapshot = snapshot
        self.active_calls = 0
        self.snapshot_calls = 0

    async def read_active(self) -> SemanticRelease | None:
        self.active_calls += 1
        return self.release

    async def read_snapshot(self, snapshot_id: str) -> SchemaSnapshot | None:
        self.snapshot_calls += 1
        assert snapshot_id == SNAPSHOT_ID
        return self.snapshot


def _deployment() -> _Deployment:
    return _Deployment(_release(_active_metrics()), _snapshot(_relation()))


def _gateway() -> QueryGateway:
    return QueryGateway(AsyncMock(), schema="ai_views")


_UNSET: object = object()


async def _factory(
    *,
    deployment: _Deployment | None = None,
    gateway: QueryGateway | None = None,
    authorization: AuthorizationContext | None | object = _UNSET,
    views: AIViewsConfig | None = None,
) -> RequestTypedRuntime | TypedRuntimeUnavailable:
    resolved = deployment or _deployment()
    resolved_authorization = (
        _authorization() if authorization is _UNSET else authorization
    )
    return await build_request_typed_runtime(
        views=views or _views(),
        read_active=resolved.read_active,
        read_snapshot=resolved.read_snapshot,
        gateway=gateway or _gateway(),
        identity=_identity(),
        authorization=resolved_authorization,  # type: ignore[arg-type]
    )


async def _resolve_propose_compile(runtime: RequestTypedRuntime):
    context = await runtime.context_resolver.resolve(
        question=QUESTION, identity=_identity(), route_hint="standard"
    )
    plan = await runtime.query_plan_provider.propose(
        question=QUESTION, context=context, identity=_identity()
    )
    compiled = await runtime.metric_query_compiler.compile(plan, context)
    return context, plan, compiled


# --------------------------------------------------------------------------- #
# 1-4: the real published config and the V1 source_ref realignment
# --------------------------------------------------------------------------- #


def test_real_published_config_loads() -> None:
    config = _views()
    assert config.target_schema == "ai_views"
    assert VIEW_NAME in {view.name for view in config.views}


def test_fault_reporting_view_resolves_from_published_config() -> None:
    metric = _active_metrics()[0]
    binding = build_relation_binding(
        views=_views(),
        release=_release((metric,)),
        snapshot=_snapshot(_relation()),
        metric=metric,
    )
    assert binding.source_ref == VIEW_NAME
    assert binding.schema_name == "ai_views"
    assert binding.relation_name == VIEW_NAME
    assert binding.relation_id == QUALIFIED
    assert binding.timestamp_kind == "timestamptz"
    assert binding.allowed_columns == view_output_columns(_view(_views()))


def test_configured_metric_source_ref_maps_to_legacy_view() -> None:
    config = _views()
    names = {view.name for view in config.views}
    for metric in _configured_metrics():
        assert metric.source_ref == VIEW_NAME
        # source_ref matches EXACTLY ONE published view by name, and the
        # qualified relation is target_schema + "." + view.name.
        assert sum(view.name == metric.source_ref for view in config.views) == 1
        assert f"{config.target_schema}.{metric.source_ref}" == QUALIFIED
    assert names >= {VIEW_NAME}
    # The mapping resolves once the metric is executable; it is deliberately NOT
    # equated with "configured", because the two product metrics are
    # pending_source and their release relation asset is retired.
    activated = _active_metrics()
    release = _release(activated)
    snapshot = _snapshot(_relation())
    for metric in activated:
        binding = build_relation_binding(
            views=config, release=release, snapshot=snapshot, metric=metric
        )
        assert binding.relation_id == QUALIFIED
    pending_release = _release(_configured_metrics())
    with pytest.raises(TypedDeploymentError, match="release_relation_asset_missing"):
        build_relation_binding(
            views=config,
            release=pending_release,
            snapshot=snapshot,
            metric=_configured_metrics()[0],
        )


def test_required_metric_columns_are_present() -> None:
    metric = _active_metrics()[0]
    binding = build_relation_binding(
        views=_views(),
        release=_release((metric,)),
        snapshot=_snapshot(_relation()),
        metric=metric,
    )
    required = {
        "acceptance_time",
        "completion_time",
        "is_first_response_on_time",
        "has_valid_bandwidth",
        "is_valid_for_metrics",
    }
    assert required <= set(binding.allowed_columns)


# --------------------------------------------------------------------------- #
# 5-10: fail-closed deployment adapter
# --------------------------------------------------------------------------- #


def test_missing_view_fails_closed() -> None:
    metric = _active_metrics()[0]
    views = _views().model_copy(
        update={"views": [view for view in _views().views if view.name != VIEW_NAME]}
    )
    with pytest.raises(TypedDeploymentError, match="published_view_missing"):
        build_relation_binding(
            views=views,
            release=_release((metric,)),
            snapshot=_snapshot(_relation()),
            metric=metric,
        )


def test_duplicate_or_malformed_view_fails_closed(tmp_path: Path) -> None:
    metric = _active_metrics()[0]
    view = _view(_views())
    duplicated = _views().model_copy(update={"views": [view, view]})
    with pytest.raises(TypedDeploymentError, match="published_view_ambiguous"):
        build_relation_binding(
            views=duplicated,
            release=_release((metric,)),
            snapshot=_snapshot(_relation()),
            metric=metric,
        )
    with pytest.raises(ValidationError):
        AIViewsConfig.model_validate(
            {"schema": "ai_views", "views": [{"name": "v", "source_table": "t", "columns": []}]}
        )
    malformed = tmp_path / "ai_views_malformed.yaml"
    malformed.write_text("schema: ai_views\nviews: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_ai_views_config(str(malformed))


def test_missing_release_relation_asset_fails_closed() -> None:
    metric = _active_metrics()[0]
    with pytest.raises(TypedDeploymentError, match="release_relation_asset_missing"):
        build_relation_binding(
            views=_views(),
            release=_release((metric,), drop_relation=True),
            snapshot=_snapshot(_relation()),
            metric=metric,
        )


def test_missing_snapshot_relation_fails_closed() -> None:
    metric = _active_metrics()[0]
    with pytest.raises(TypedDeploymentError, match="snapshot_relation_missing"):
        build_relation_binding(
            views=_views(),
            release=_release((metric,)),
            snapshot=_snapshot(None),
            metric=metric,
        )


def test_view_column_absent_from_snapshot_fails_closed() -> None:
    metric = _active_metrics()[0]
    with pytest.raises(TypedDeploymentError, match="published_view_column_missing"):
        build_relation_binding(
            views=_views(),
            release=_release((metric,)),
            snapshot=_snapshot(_relation(drop="area_name")),
            metric=metric,
        )


def test_invalid_business_time_type_fails_closed() -> None:
    metric = _active_metrics()[0]
    with pytest.raises(TypedDeploymentError, match="business_time_type_unsupported"):
        build_relation_binding(
            views=_views(),
            release=_release((metric,)),
            snapshot=_snapshot(_relation(retype=("acceptance_time", "date"))),
            metric=metric,
        )


# --------------------------------------------------------------------------- #
# 11-14: bounded V1 eligibility, binding permissions and approver semantics
# --------------------------------------------------------------------------- #


def test_unknown_eligibility_policy_fails_closed() -> None:
    with pytest.raises(TypedDeploymentError, match="unknown_eligibility_policy"):
        resolve_eligibility_policy("metric_unknown_policy_v9")
    with pytest.raises(TypedDeploymentError):
        build_eligibility_policies(
            (_activate(_configured_metrics()[0], eligibility_policy_id="unknown_policy_v9"),)
        )


@pytest.mark.asyncio
async def test_unknown_eligibility_policy_makes_the_factory_unavailable() -> None:
    metric = _activate(
        _configured_metrics()[0], eligibility_policy_id="unknown_policy_v9"
    )
    deployment = _Deployment(_release((metric,)), _snapshot(_relation()))
    result = await _factory(deployment=deployment)
    assert isinstance(result, TypedRuntimeUnavailable)
    assert result.reason == "unknown_eligibility_policy"


def test_known_eligibility_policy_yields_empty_predicates() -> None:
    policy = resolve_eligibility_policy("work_order_metric_eligibility_v1")
    assert isinstance(policy, EligibilityPolicy)
    assert policy.policy_id == "work_order_metric_eligibility_v1"
    assert policy.predicates == ()
    resolved = build_eligibility_policies(_active_metrics())
    assert len(resolved) == 1
    assert resolved[0].predicates == ()


def test_relation_binding_has_no_extra_permission_requirement() -> None:
    metric = _active_metrics()[0]
    binding = build_relation_binding(
        views=_views(),
        release=_release((metric,)),
        snapshot=_snapshot(_relation()),
        metric=metric,
    )
    assert binding.required_permissions == ()
    assert RelationBinding.model_fields["required_permissions"].default == ()
    assert metric.required_permissions == ("nl2sql:invoke",)


def test_active_metric_requires_owner_not_approver() -> None:
    seed = _configured_metrics()[0]
    payload = seed.model_dump(mode="json")
    payload.update(release_status="active", owner="owner-a", approver=None)
    active = MetricContract.model_validate(payload)
    assert active.approver is None and active.owner == "owner-a"
    with pytest.raises(ValidationError, match="active metric requires owner"):
        MetricContract.model_validate({**payload, "owner": None})
    with pytest.raises(ValidationError, match="active metric requires owner"):
        MetricContract.model_validate({**payload, "owner": "   "})


# --------------------------------------------------------------------------- #
# 15-18: the request-scoped factory and request coherence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_factory_refuses_missing_authorization_context() -> None:
    deployment = _deployment()
    with patch(
        "src.nl2sql.orchestration.typed_runtime.MetricQueryCompiler"
    ) as compiler_cls:
        missing = await _factory(deployment=deployment, authorization=None)
        disabled = await _factory(
            deployment=deployment, authorization=_authorization(agent_enabled=False)
        )
        empty_scope = await _factory(
            deployment=deployment, authorization=_authorization(allowed_scope_ids=())
        )
    assert isinstance(missing, TypedRuntimeUnavailable)
    assert missing.reason == "authorization_context_missing"
    assert isinstance(disabled, TypedRuntimeUnavailable)
    assert disabled.reason == "authorization_denied"
    assert isinstance(empty_scope, TypedRuntimeUnavailable)
    assert empty_scope.reason == "authorization_denied"
    # Fail closed BEFORE any release read and BEFORE the compiler is built, so
    # the authorization=None compatibility seam can never be activated here.
    assert deployment.active_calls == 0
    compiler_cls.assert_not_called()


@pytest.mark.asyncio
async def test_factory_fails_closed_for_the_pending_product_config() -> None:
    deployment = _Deployment(
        _release(_configured_metrics()), _snapshot(_relation())
    )
    result = await _factory(deployment=deployment)
    assert isinstance(result, TypedRuntimeUnavailable)
    assert result.reason == "no_executable_metric_binding"
    assert executable_metric_contracts(deployment.release) == ()


@pytest.mark.asyncio
async def test_factory_with_valid_authorization_builds_one_coherent_set() -> None:
    runtime = await _factory()
    assert isinstance(runtime, RequestTypedRuntime)
    assert isinstance(runtime.context_resolver, SemanticContextResolver)
    assert isinstance(runtime.context_compiler, ContextCompiler)
    assert isinstance(runtime.query_plan_provider.is_deterministic, bool)
    assert runtime.query_plan_provider.is_deterministic is True
    assert isinstance(runtime.plan_executor, PlanExecutor)
    assert len(runtime.relation_bindings) == 1
    assert runtime.relation_bindings[0].relation_id == QUALIFIED
    assert runtime.relation_bindings[0].required_permissions == ()

    context, plan, compiled = await _resolve_propose_compile(runtime)
    assert str(context.semantic_release_id) == RELEASE_ID
    assert runtime.active_release_registry.active_release() is runtime.release
    assert plan.metric_keys == ("metric.complaint_in_transit_count",)
    assert compiled.release_id == RELEASE_ID
    assert compiled.snapshot_id == SNAPSHOT_ID
    assert '"v_fault_reporting_order"' in compiled.sql
    assert "is_valid_for_metrics" in compiled.sql


@pytest.mark.asyncio
async def test_bound_release_and_snapshot_cannot_rotate_inside_the_request() -> None:
    metrics = _active_metrics()
    release_a = _release(metrics)
    release_b = _release(metrics, release_id=RELEASE_ID_B)
    deployment = _Deployment(release_a, _snapshot(_relation()))
    runtime = await _factory(deployment=deployment)
    assert isinstance(runtime, RequestTypedRuntime)
    assert runtime.release is release_a
    assert deployment.active_calls == 1
    assert deployment.snapshot_calls == 1

    # Flip the deployment pointer AFTER the request runtime was constructed.
    deployment.release = release_b

    context, _, compiled = await _resolve_propose_compile(runtime)
    assert str(context.semantic_release_id) == RELEASE_ID
    assert compiled.release_id == RELEASE_ID
    assert runtime.active_release_registry.active_release() is release_a
    # The request never re-read the deployment pointer: exactly one active read.
    assert deployment.active_calls == 1
    assert deployment.snapshot_calls == 1


@pytest.mark.asyncio
async def test_one_shared_query_gateway_is_reused() -> None:
    gateway = _gateway()
    deployment = _deployment()
    first = await _factory(deployment=deployment, gateway=gateway)
    second = await _factory(deployment=deployment, gateway=gateway)
    assert isinstance(first, RequestTypedRuntime) and isinstance(second, RequestTypedRuntime)
    assert first.gateway is gateway
    assert second.gateway is gateway
    assert first.gateway is second.gateway
    # Request-scoped executors stay fresh even though the gateway is shared.
    assert first.plan_executor is not second.plan_executor
    assert deployment.active_calls == 2  # exactly one read per request


# --------------------------------------------------------------------------- #
# 19: no DDL / publishing / second-IAM surface in the typed runtime
# --------------------------------------------------------------------------- #


def test_runtime_module_has_no_ddl_or_publishing_dependencies() -> None:
    source = (
        ROOT / "src" / "nl2sql" / "orchestration" / "typed_runtime.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "sync_ai_views_from_yaml",
        "CREATE OR REPLACE VIEW",
        "CREATE VIEW",
        "GRANT",
        "ALTER DEFAULT PRIVILEGES",
        "engine.execute",
        "QueryGateway(",
        "BackendAuthorizationProvider",
        "ApprovalRecord",
        "approval_attestation",
    )
    for token in forbidden:
        assert token not in source, token
    # The read-only loader is the only ai_views entry point reused, and the
    # publishing sync function keeps ZERO callers anywhere in src.
    callers = [
        path.name
        for path in (ROOT / "src").rglob("*.py")
        if path.name != "ai_views.py"
        and "sync_ai_views_from_yaml" in path.read_text(encoding="utf-8")
    ]
    assert callers == []


def test_read_only_loader_parses_yaml_without_a_database() -> None:
    # load_ai_views_config takes only a path: it cannot open a connection or run
    # view DDL, and the adapter/factory tests above never touched PostgreSQL.
    config = load_ai_views_config(str(AI_VIEWS_PATH))
    assert config.target_schema == "ai_views"
    assert view_output_columns(_view(config)) == view_output_columns(_view(_views()))
    bound = build_relation_bindings(
        views=config,
        release=_release(_active_metrics()),
        snapshot=_snapshot(_relation()),
        metrics=_active_metrics(),
    )
    assert len(bound) == 1
