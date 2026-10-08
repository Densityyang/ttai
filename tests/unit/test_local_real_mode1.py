"""Focused tests for the local real-data Mode1 vertical (non-live).

No test here touches a real database: the live tunnel is a separate, explicitly
gated lane.  These tests prove the foundation is complete and fail-closed.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any

import pytest

from src.nl2sql.infra.store.ai_views import load_ai_views_config, view_output_columns
from src.nl2sql.local_real.deployment import (
    FROZEN_REAL_CASE_DEPENDENCIES,
    FROZEN_REAL_CASE_METRIC_KEY,
    FROZEN_REAL_CASE_REQUIRED_COLUMNS,
    FROZEN_REAL_CASE_VIEW_NAME,
    PREFERRED_PUBLISHED_RELATION,
    RELATION_STATUS_PUBLISHED_VIEW_READY,
    RELATION_STATUS_SOURCE_MISSING,
    RELATION_STATUS_VIEW_MISSING_SOURCE_PRESENT,
    build_frozen_case_closure,
    build_local_real_deployment,
    missing_required_columns,
)
from src.nl2sql.local_real.semantics import (
    LOCAL_REAL_DEMO_PROVENANCE,
    LocalRealSemanticError,
    assert_release_is_bounded,
    build_bounded_release_candidate,
    build_local_real_semantics,
    executable_metric_keys,
    load_frozen_case_contracts,
    verify_authoritative_source_fingerprint,
)
from src.nl2sql.semantic.registry import SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import (
    SchemaSnapshotState,
    build_schema_snapshot_candidate,
)

ROOT = Path(__file__).resolve().parents[2]
AI_VIEWS_PATH = ROOT / "configs" / "semantic" / "ai_views.yaml"

# Live types for the columns the closure depends on; everything else is text.
_TYPES = {
    "id": "bigint",
    "report_time": "timestamp without time zone",
    "completion_receipt_time": "timestamp without time zone",
    "is_archive_on_time": "boolean",
    "is_valid_for_metrics": "boolean",
}


def _views() -> Any:
    return load_ai_views_config(str(AI_VIEWS_PATH))


def _view() -> Any:
    return next(v for v in _views().views if v.name == FROZEN_REAL_CASE_VIEW_NAME)


def _columns() -> tuple[str, ...]:
    return tuple(view_output_columns(_view()))


def _candidate(
    columns: tuple[str, ...] | None = None,
    *,
    relation_id: str = PREFERRED_PUBLISHED_RELATION,
) -> Any:
    names = columns if columns is not None else _columns()
    schema_name, _, relation_name = relation_id.partition(".")
    relation_rows = [
        {
            "relation_oid": 10,
            "schema_name": schema_name,
            "relation_name": relation_name,
            "relation_kind": "v",
            "estimated_rows": 0,
            "total_bytes": 0,
            "partition_key": None,
        }
    ]
    column_rows = [
        {
            "relation_oid": 10,
            "column_name": name,
            "data_type": _TYPES.get(name, "text"),
            "nullable": True,
            "ordinal_position": index,
        }
        for index, name in enumerate(names, 1)
    ]
    return build_schema_snapshot_candidate(
        source_identifier="local-real-test",
        approved_schemas=(schema_name,),
        relation_rows=relation_rows,
        column_rows=column_rows,
    )


def _bundle(**overrides: Any) -> Any:
    params: dict[str, Any] = {
        "metric_keys": FROZEN_REAL_CASE_DEPENDENCIES,
        "relation_id": PREFERRED_PUBLISHED_RELATION,
        "view_columns": _columns(),
        "snapshot_candidate": _candidate(),
        "required_columns": FROZEN_REAL_CASE_REQUIRED_COLUMNS,
    }
    params.update(overrides)
    return build_local_real_semantics(**params)


# --- authoritative source + frozen closure ------------------------------------


def test_authoritative_sources_are_verified_before_any_release() -> None:
    fingerprint = verify_authoritative_source_fingerprint()
    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")


def test_frozen_case_closure_is_exact_and_rejects_unrelated_metrics() -> None:
    assert build_frozen_case_closure() == FROZEN_REAL_CASE_DEPENDENCIES
    assert FROZEN_REAL_CASE_METRIC_KEY in FROZEN_REAL_CASE_DEPENDENCIES
    for key in FROZEN_REAL_CASE_DEPENDENCIES:
        assert build_frozen_case_closure((key,)) == (key,)
    with pytest.raises(ValueError, match="unsupported real-data metric"):
        build_frozen_case_closure(("complaint_in_transit_count",))


def test_contract_binding_covers_exactly_the_frozen_closure() -> None:
    catalog = load_frozen_case_contracts()
    keys = {metric.metric_key for metric in catalog.metrics}
    assert keys == set(FROZEN_REAL_CASE_DEPENDENCIES)
    # The binding must not silently become ACTIVE without deployment activation.
    assert all(metric.release_status == "pending_source" for metric in catalog.metrics)


def test_binding_carries_canonical_eligibility_semantics() -> None:
    catalog = load_frozen_case_contracts()
    by_key = {metric.metric_key: metric for metric in catalog.metrics}
    ratio = by_key["repair_service_archive_rate_overall_day"]
    assert ratio.operation == "ratio"
    assert ratio.business_time_column == "report_time"
    assert ratio.ratio is not None
    assert ratio.ratio.zero_denominator_policy == "calculation_error"
    numerator = by_key["repair_service_archive_on_time_count_overall_day"]
    assert numerator.business_time_column == "report_time"
    assert {p.field for p in numerator.predicates} == {
        "is_archive_on_time",
        "completion_receipt_time",
    }
    denominator = by_key["repair_service_calc_total_count_overall_day"]
    assert {p.field for p in denominator.predicates} == {"completion_receipt_time"}
    # every contract binds the published view, never the physical table
    assert {metric.source_ref for metric in catalog.metrics} == {
        FROZEN_REAL_CASE_VIEW_NAME
    }


def test_bounded_candidate_rejects_an_unrelated_activation_request() -> None:
    with pytest.raises(LocalRealSemanticError, match="missing"):
        build_bounded_release_candidate(
            catalog=load_frozen_case_contracts(),
            relation_id=PREFERRED_PUBLISHED_RELATION,
            view_columns=_columns(),
            metric_keys=(*FROZEN_REAL_CASE_DEPENDENCIES, "complaint_in_transit_count"),
        )


# --- only the frozen closure is executable ------------------------------------


def test_only_the_frozen_closure_becomes_executable() -> None:
    bundle = _bundle()
    assert executable_metric_keys(bundle.release) == tuple(
        sorted(FROZEN_REAL_CASE_DEPENDENCIES)
    )
    assert_release_is_bounded(bundle.release, metric_keys=FROZEN_REAL_CASE_DEPENDENCIES)


def test_bounded_check_rejects_an_unrelated_executable_metric() -> None:
    bundle = _bundle()
    with pytest.raises(LocalRealSemanticError, match="unrelated executable metric"):
        assert_release_is_bounded(bundle.release, metric_keys=(FROZEN_REAL_CASE_METRIC_KEY,))


# --- release / snapshot binding -----------------------------------------------


def test_in_memory_release_is_active_and_bound_to_the_validated_snapshot() -> None:
    bundle = _bundle()
    assert bundle.release.state is SemanticReleaseState.ACTIVE
    assert bundle.snapshot.state is SchemaSnapshotState.VALIDATED
    assert bundle.release.schema_snapshot_id == bundle.snapshot.snapshot_id
    assert bundle.release.schema_snapshot_checksum == bundle.snapshot.checksum
    assert bundle.release.checksum
    assert bundle.provenance == LOCAL_REAL_DEMO_PROVENANCE
    # the projection must never claim production governance authority
    assert bundle.provenance != "backend"


def test_release_documents_include_the_published_relation_asset() -> None:
    bundle = _bundle()
    relations = [
        d for d in bundle.release.documents if d.metadata.get("asset_type") == "relation"
    ]
    assert len(relations) == 1
    assert relations[0].metadata.get("status") == "active"


def test_snapshot_read_is_exact_id_only() -> None:
    bundle = _bundle()
    deployment = build_local_real_deployment(
        views=_views(),
        gateway=object(),
        release=bundle.release,
        snapshot=bundle.snapshot,
        relation_name=PREFERRED_PUBLISHED_RELATION,
    )
    import asyncio

    assert asyncio.run(deployment.read_snapshot(bundle.snapshot.snapshot_id)) is bundle.snapshot
    assert asyncio.run(deployment.read_snapshot("00000000-0000-0000-0000-000000000000")) is None
    assert asyncio.run(deployment.read_active()) is bundle.release


def test_deployment_requires_the_real_gateway() -> None:
    bundle = _bundle()
    gateway = object()
    deployment = build_local_real_deployment(
        views=_views(),
        gateway=gateway,
        release=bundle.release,
        snapshot=bundle.snapshot,
        relation_name=PREFERRED_PUBLISHED_RELATION,
    )
    # the real gateway must actually be CARRIED, not validated and dropped
    assert deployment.gateway is gateway
    with pytest.raises(ValueError, match="local-real deployment missing"):
        build_local_real_deployment(
            views=_views(),
            gateway=None,
            release=bundle.release,
            snapshot=bundle.snapshot,
            relation_name=PREFERRED_PUBLISHED_RELATION,
        )


# --- fail-closed schema behaviour ---------------------------------------------


def test_missing_required_column_fails_closed() -> None:
    reduced = tuple(c for c in _columns() if c != "is_archive_on_time")
    with pytest.raises(LocalRealSemanticError):
        _bundle(view_columns=reduced, snapshot_candidate=_candidate(reduced))


def test_missing_report_time_fails_closed() -> None:
    reduced = tuple(c for c in _columns() if c != "report_time")
    with pytest.raises(LocalRealSemanticError):
        _bundle(view_columns=reduced, snapshot_candidate=_candidate(reduced))


def test_absent_required_relation_fails_closed() -> None:
    wrong = _candidate(relation_id="ai_views.v_not_repair_service")
    with pytest.raises(LocalRealSemanticError):
        _bundle(snapshot_candidate=wrong)


def test_required_column_detection_is_exact() -> None:
    assert missing_required_columns(FROZEN_REAL_CASE_REQUIRED_COLUMNS) == ()
    assert missing_required_columns(("id", "report_time")) == (
        "completion_receipt_time",
        "is_archive_on_time",
        "is_valid_for_metrics",
    )
    # case-insensitive against a live catalog
    assert missing_required_columns(name.upper() for name in FROZEN_REAL_CASE_REQUIRED_COLUMNS) == ()


# --- published view contract --------------------------------------------------


def test_repair_service_view_is_configured_and_publishes_the_closure_columns() -> None:
    view = _view()
    assert view.source_table == "silver_repair_service"
    published = set(view_output_columns(view))
    for column in FROZEN_REAL_CASE_REQUIRED_COLUMNS:
        assert column in published, column
    # the service reads the view schema, never the physical table
    assert _views().target_schema == "ai_views"


def test_relation_status_vocabulary_is_stable() -> None:
    assert RELATION_STATUS_PUBLISHED_VIEW_READY == "PUBLISHED_VIEW_READY"
    assert (
        RELATION_STATUS_VIEW_MISSING_SOURCE_PRESENT
        == "PUBLISHED_VIEW_MISSING_SOURCE_PRESENT"
    )
    assert RELATION_STATUS_SOURCE_MISSING == "SOURCE_MISSING"
    assert PREFERRED_PUBLISHED_RELATION == "ai_views.v_repair_service"


# --- launch profile -----------------------------------------------------------


def test_launch_profile_exists_and_contains_no_secrets() -> None:
    path = ROOT / "deploy" / "local-real-demo.env.example"
    text = path.read_text(encoding="utf-8")
    assert "TYPED_RUNTIME_ACTIVATION=local_real_data_demo" in text
    assert "SERVICE_MODE=infra-dev" in text
    assert "DATABASE_URL_FILE=secrets/database/business_ro_database_url" in text
    assert "MODEL_REQUIRED=false" in text
    # no literal credential may ever appear in a tracked example
    lowered = text.lower()
    for forbidden in ("postgresql://", "postgres://", "password=", "ssh_password", "api_key="):
        assert forbidden not in lowered, forbidden
    assert "DATABASE_URL=" not in text.replace("DATABASE_URL_FILE=", "")


def test_launch_profile_activates_a_supported_settings_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.settings import Settings

    for key, value in {
        "SERVICE_MODE": "infra-dev",
        "AUTH_ENABLED": "false",
        "MEMORY_BACKEND": "memory",
        "TYPED_RUNTIME_ACTIVATION": "local_real_data_demo",
        "MODEL_REQUIRED": "false",
        "LOCAL_REAL_DEMO_USER_ID": "local-real-demo",
        "LOCAL_DEMO_CERTIFICATION_ADMIN_USER_ID": "local-demo-cert-admin",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.typed_runtime_activation == "local_real_data_demo"
    assert settings.service_mode == "infra-dev"
    assert settings.local_real_data_demo_enabled is True


def test_product_mode_rejects_the_local_real_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.settings import Settings

    monkeypatch.setenv("SERVICE_MODE", "product")
    monkeypatch.setenv("TYPED_RUNTIME_ACTIVATION", "local_real_data_demo")
    with pytest.raises(Exception):
        Settings(_env_file=None)  # type: ignore[call-arg]


# --- synthetic isolation ------------------------------------------------------


def test_local_real_module_never_imports_the_synthetic_demo_runtime() -> None:
    for name in ("semantics", "deployment", "live_probe"):
        source = (
            ROOT / "src" / "nl2sql" / "local_real" / f"{name}.py"
        ).read_text(encoding="utf-8")
        assert "demo.runtime" not in source
        assert "DemoQueryPlanProvider" not in source
        assert "build_demo_runtime" not in source


def test_no_not_implemented_placeholder_remains_in_the_local_real_path() -> None:
    for name in ("semantics", "deployment", "live_probe"):
        source = (
            ROOT / "src" / "nl2sql" / "local_real" / f"{name}.py"
        ).read_text(encoding="utf-8")
        assert "NotImplementedError" not in source
    container = (ROOT / "src" / "nl2sql" / "container.py").read_text(encoding="utf-8")
    assert "_build_local_real_semantics" not in container or "NotImplementedError" not in container


# --- zero-model property ------------------------------------------------------


def test_typed_runtime_construction_makes_no_model_call() -> None:
    """Mode1 QUERY is deterministic; constructing the runtime needs no model."""

    os.environ.setdefault("MODEL_REQUIRED", "false")
    bundle = _bundle()
    assert bundle.release.state is SemanticReleaseState.ACTIVE
    assert bundle.release.state is SemanticReleaseState.ACTIVE
    assert bundle.release.documents
    # the closure is executable without any model gateway being constructed
    assert executable_metric_keys(bundle.release) == tuple(
        sorted(FROZEN_REAL_CASE_DEPENDENCIES)
    )


def test_latest_date_result_shape_carries_no_business_rows() -> None:
    """The availability probe returns a date and a count, never row data."""

    import dataclasses

    from src.nl2sql.local_real.live_probe import (
        PROBE_LOCK_TIMEOUT_MS,
        PROBE_STATEMENT_TIMEOUT_MS,
        LatestDateResult,
    )

    assert PROBE_STATEMENT_TIMEOUT_MS > 0
    assert PROBE_LOCK_TIMEOUT_MS > 0
    unresolved = LatestDateResult(latest_date=None, denominator_rows=0, resolved=False)
    assert unresolved.resolved is False
    assert unresolved.latest_date is None
    solved = LatestDateResult(latest_date='2025-01-02', denominator_rows=7, resolved=True)
    assert solved.latest_date == '2025-01-02'
    # The count must be the DENOMINATOR row count, never the row count of the
    # grouped candidate subquery (which would always be exactly 1).
    assert solved.denominator_rows == 7
    fields = {field.name for field in dataclasses.fields(solved)}
    assert fields == {'latest_date', 'denominator_rows', 'resolved'}


def test_recent_date_result_is_bounded_and_carries_no_business_rows() -> None:
    import dataclasses

    from src.nl2sql.local_real.live_probe import RecentDateResult

    result = RecentDateResult(
        dates=('2025-01-02', '2025-01-03', '2025-01-06'), resolved=True
    )
    assert result.resolved is True
    assert result.dates == ('2025-01-02', '2025-01-03', '2025-01-06')
    fields = {field.name for field in dataclasses.fields(result)}
    assert fields == {'dates', 'resolved'}


@pytest.mark.asyncio
async def test_latest_probe_runs_the_bounded_eligibility_query(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from src.nl2sql.local_real import live_probe

    executed: list[tuple[str, object]] = []

    class _Result:
        def mappings(self) -> Any:
            return self

        def one_or_none(self) -> dict[str, object]:
            return {"latest_date": "2025-01-06", "denominator_rows": 4}

    class _Session:
        async def execute(self, statement: object, params: object = None) -> _Result:
            executed.append((str(statement), params))
            return _Result()

    @asynccontextmanager
    async def _bounded(_factory: object):
        yield _Session()

    monkeypatch.setattr(live_probe, "bounded_read_only_session", _bounded)
    result = await live_probe.resolve_latest_authoritative_date(
        _factory(object())
    )
    assert result == live_probe.LatestDateResult(
        latest_date="2025-01-06", denominator_rows=4, resolved=True
    )
    query = executed[0][0].lower()
    assert "ai_views.v_repair_service" in query
    assert "is_valid_for_metrics is true" in query
    assert "completion_receipt_time is not null" in query
    assert "report_time is not null" in query
    assert "report_time" in query
    assert "group by" in query
    assert "order by 1 desc" in query
    assert "limit 1" in query
    assert 'having count("id") > 0' in query


@pytest.mark.asyncio
async def test_latest_probe_skips_newest_zero_count_group() -> None:
    """The SQL-level HAVING keeps a NULL-id newest date from shadowing D-1."""

    from src.nl2sql.local_real import live_probe

    source = live_probe.resolve_latest_authoritative_date
    assert source is not None
    statement_source = inspect.getsource(source).lower()
    assert 'having count("id") > 0' in statement_source or "having count" in statement_source


@pytest.mark.asyncio
async def test_recent_probe_selects_bounded_dates_and_fails_safely(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from src.nl2sql.local_real import live_probe

    class _Result:
        def mappings(self) -> Any:
            return self

        def all(self) -> list[dict[str, str]]:
            return [
                {"business_date": "2025-01-02"},
                {"business_date": "2025-01-03"},
                {"business_date": "2025-01-06"},
            ]

    captured: list[object] = []

    class _Session:
        async def execute(self, statement: object, params: object = None) -> _Result:
            captured.extend((str(statement), params))
            return _Result()

    @asynccontextmanager
    async def _bounded(_factory: object):
        yield _Session()

    monkeypatch.setattr(live_probe, "bounded_read_only_session", _bounded)
    result = await live_probe.resolve_recent_authoritative_dates(
        _factory(object()), limit=7
    )
    assert result.dates == ("2025-01-02", "2025-01-03", "2025-01-06")
    assert result.resolved is True
    assert captured[1] == {"limit": 7}
    assert "report_time is not null" in str(captured[0]).lower()
    with pytest.raises(ValueError, match="recent date limit"):
        await live_probe.resolve_recent_authoritative_dates(
            _factory(object()), limit=32
        )

    class _FailingSession:
        async def execute(self, statement: object, params: object = None) -> Any:
            raise RuntimeError("secret database detail")

    @asynccontextmanager
    async def _failing(_factory: object):
        yield _FailingSession()

    monkeypatch.setattr(live_probe, "bounded_read_only_session", _failing)
    failed = await live_probe.resolve_recent_authoritative_dates(_factory(object()))
    assert failed == live_probe.RecentDateResult(dates=(), resolved=False)


@pytest.mark.asyncio
async def test_source_watermark_is_observed_timestamp_and_fails_closed(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    from src.nl2sql.local_real import live_probe

    class _Result:
        def mappings(self) -> Any:
            return self

        def one_or_none(self) -> dict[str, object]:
            return {
                "source_watermark": datetime(2026, 9, 18, 17, 42, tzinfo=UTC)
            }

    class _Session:
        async def execute(self, statement: object, params: object = None) -> _Result:
            del params
            self.statement = str(statement)
            return _Result()

    session = _Session()

    @asynccontextmanager
    async def _bounded(_factory: object):
        yield session

    monkeypatch.setattr(live_probe, "bounded_read_only_session", _bounded)
    result = await live_probe.resolve_source_watermark(_factory(object()))
    assert result.resolved is True
    assert result.data_as_of == datetime(2026, 9, 18, 17, 42, tzinfo=UTC)
    assert "max(report_time)" in session.statement.lower()
    assert "report_time is not null" in session.statement.lower()
    assert "id is not null" in session.statement.lower()

    class _FailingSession:
        async def execute(self, statement: object, params: object = None) -> Any:
            del statement, params
            raise RuntimeError("sensitive database detail")

    @asynccontextmanager
    async def _failing(_factory: object):
        yield _FailingSession()

    monkeypatch.setattr(live_probe, "bounded_read_only_session", _failing)
    assert await live_probe.resolve_source_watermark(_factory(object())) == (
        live_probe.SourceWatermarkResult(data_as_of=None, resolved=False)
    )


@pytest.mark.asyncio
async def test_local_real_factory_injects_server_availability_and_fails_closed(
    monkeypatch,
) -> None:
    from datetime import date
    from uuid import uuid4

    from src.nl2sql.container import AppContainer
    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
    from src.nl2sql.orchestration import typed_runtime

    container = AppContainer()
    container._local_real_deployment = ("views", "active", "snapshot", "gateway")
    container._local_real_latest_authoritative_date = "2025-01-06"
    container._local_real_recent_authoritative_dates = (
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
    )
    captured: dict[str, object] = {}

    async def _build(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(typed_runtime, "build_request_typed_runtime", _build)
    factory = container._local_real_typed_runtime_factory()
    identity = RequestIdentity(request_id=uuid4(), user_id="local-real-demo")
    authorization = AuthorizationContext(
        authorization_revision="local-real-demo:v1",
        agent_enabled=True,
        scope_level="city_company",
        allowed_scope_ids=("local-real-demo-scope",),
    )
    result = await factory(
        identity=identity, authorization=authorization, expected_revision=None
    )
    assert result is not None
    assert captured["authoritative_date"]() == date(2025, 1, 6)  # type: ignore[operator]
    window = captured["availability_window"]()  # type: ignore[operator]
    assert window == (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))

    container._local_real_latest_authoritative_date = None
    unavailable = await factory(
        identity=identity, authorization=authorization, expected_revision=None
    )
    assert getattr(unavailable, "reason", None) == "local_real_authoritative_date_unavailable"

    container._local_real_latest_authoritative_date = "2025-01-06"
    container._local_real_recent_authoritative_dates = ()
    unavailable_window = await factory(
        identity=identity, authorization=authorization, expected_revision=None
    )
    assert getattr(unavailable_window, "reason", None) == (
        "local_real_authoritative_window_unavailable"
    )


def test_local_real_freshness_uses_observed_watermark_not_business_midnight() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.nl2sql.container import AppContainer

    bundle = _bundle()
    observed = datetime(2026, 9, 18, 17, 42, tzinfo=UTC)
    observed_at = datetime(2026, 9, 18, 17, 43, tzinfo=UTC)
    container = AppContainer()
    container._local_real_resources = (
        object(),
        SimpleNamespace(release=bundle.release, snapshot=bundle.snapshot),
    )
    container._local_real_latest_authoritative_date = "2026-09-18"
    container._local_real_source_watermark = observed
    container._local_real_source_watermark_checked_at = observed_at
    freshness = container._local_real_source_freshness()
    assert freshness.data_as_of == observed
    assert freshness.checked_at == observed_at
    assert freshness.data_as_of != datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
    assert "20260918t174200z" in freshness.checkpoint


# --- standard typed runtime consumption ---------------------------------------


def test_standard_typed_runtime_consumes_the_local_real_deployment() -> None:
    """The SAME build_request_typed_runtime used by production accepts it."""

    import asyncio

    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
    from src.nl2sql.orchestration.typed_runtime import (
        TypedRuntimeUnavailable,
        build_request_typed_runtime,
    )

    bundle = _bundle()
    deployment = build_local_real_deployment(
        views=_views(),
        gateway=object(),
        release=bundle.release,
        snapshot=bundle.snapshot,
        relation_name=PREFERRED_PUBLISHED_RELATION,
    )
    identity = RequestIdentity(
        request_id=__import__('uuid').UUID(int=1),
        user_id='local-real-demo',
        permissions=frozenset({'nl2sql:invoke'}),
    )
    authorization = AuthorizationContext(
        authorization_revision='local-real-demo:1',
        agent_enabled=True,
        scope_level='city_company',
        allowed_scope_ids=('1',),
    )
    runtime = asyncio.run(
        build_request_typed_runtime(
            views=deployment.views,
            read_active=deployment.read_active,
            read_snapshot=deployment.read_snapshot,
            gateway=deployment.gateway,
            identity=identity,
            authorization=authorization,
        )
    )
    assert not isinstance(runtime, TypedRuntimeUnavailable), runtime
    assert runtime.context_resolver is not None
    assert runtime.query_plan_provider is not None
    assert runtime.plan_executor is not None


def test_standard_typed_runtime_fails_closed_without_authority() -> None:
    import asyncio

    from src.nl2sql.contracts import RequestIdentity
    from src.nl2sql.orchestration.typed_runtime import (
        TypedRuntimeUnavailable,
        build_request_typed_runtime,
    )

    bundle = _bundle()
    deployment = build_local_real_deployment(
        views=_views(),
        gateway=object(),
        release=bundle.release,
        snapshot=bundle.snapshot,
        relation_name=PREFERRED_PUBLISHED_RELATION,
    )
    identity = RequestIdentity(
        request_id=__import__('uuid').UUID(int=2),
        user_id='local-real-demo',
        permissions=frozenset({'nl2sql:invoke'}),
    )
    runtime = asyncio.run(
        build_request_typed_runtime(
            views=deployment.views,
            read_active=deployment.read_active,
            read_snapshot=deployment.read_snapshot,
            gateway=deployment.gateway,
            identity=identity,
            authorization=None,
        )
    )
    assert isinstance(runtime, TypedRuntimeUnavailable)
    assert runtime.reason == 'authorization_context_missing'


def test_standard_typed_runtime_rejects_a_wrong_snapshot_id() -> None:
    """A wrong snapshot id must fail closed, not resolve a foreign snapshot."""

    import asyncio

    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
    from src.nl2sql.orchestration.typed_runtime import (
        TypedRuntimeUnavailable,
        build_request_typed_runtime,
    )

    bundle = _bundle()
    deployment = build_local_real_deployment(
        views=_views(),
        gateway=object(),
        release=bundle.release,
        snapshot=bundle.snapshot,
        relation_name=PREFERRED_PUBLISHED_RELATION,
    )
    identity = RequestIdentity(
        request_id=__import__('uuid').UUID(int=3),
        user_id='local-real-demo',
        permissions=frozenset({'nl2sql:invoke'}),
    )
    runtime = asyncio.run(
        build_request_typed_runtime(
            views=deployment.views,
            read_active=deployment.read_active,
            read_snapshot=lambda snapshot_id: _none(),
            gateway=deployment.gateway,
            identity=identity,
            authorization=AuthorizationContext(
                authorization_revision='local-real-demo:1',
                agent_enabled=True,
                scope_level='city_company',
                allowed_scope_ids=('1',),
            ),
        )
    )
    assert isinstance(runtime, TypedRuntimeUnavailable)
    assert runtime.reason == 'schema_snapshot_unavailable'


async def _none() -> None:
    return None


# --- microfix: probe transaction boundary, count and relation allowlist --------


def test_probe_relation_allowlist_rejects_arbitrary_relations() -> None:
    """No request/model/user relation name may reach the probe SQL."""

    from src.nl2sql.local_real.live_probe import (
        ALLOWED_PROBE_RELATIONS,
        LocalRealProbeError,
        _require_allowed_relation,
    )

    assert ALLOWED_PROBE_RELATIONS == frozenset(
        {'ai_views.v_repair_service', 'public.silver_repair_service'}
    )
    assert _require_allowed_relation('ai_views.v_repair_service') == (
        'ai_views.v_repair_service'
    )
    for rejected in (
        'public.silver_repair_service; DROP TABLE t',
        'ai_views.v_repair_service_evil',
        'public.users',
        'silver_repair_service',
        '',
    ):
        with pytest.raises(LocalRealProbeError):
            _require_allowed_relation(rejected)


def _factory(session: Any) -> Any:
    """A minimal session-factory double for the probe boundary tests."""

    return lambda: session


def test_bounded_read_only_session_sets_timeouts_and_closes() -> None:
    """The probe transaction is explicit, read-only, bounded and always closed."""

    import asyncio

    from src.nl2sql.local_real import live_probe

    executed: list[str] = []
    closed: list[bool] = []
    rolled_back: list[bool] = []

    class _Tx:
        is_active = True

        async def rollback(self) -> None:
            rolled_back.append(True)
            self.is_active = False

    class _Session:
        def __init__(self) -> None:
            self.tx = _Tx()

        async def begin(self) -> Any:
            return self.tx

        async def execute(self, statement: Any) -> Any:
            executed.append(str(statement))
            return None

        async def close(self) -> None:
            closed.append(True)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            await self.close()

    session = _Session()

    async def _run() -> None:
        async with live_probe.bounded_read_only_session(_factory(session)) as yielded:
            assert yielded is session

    asyncio.run(_run())
    joined = ' | '.join(executed).upper()
    assert 'SET TRANSACTION READ ONLY' in joined
    assert 'STATEMENT_TIMEOUT' in joined
    assert 'LOCK_TIMEOUT' in joined
    assert rolled_back == [True]
    assert closed == [True]


def test_bounded_read_only_session_closes_even_on_failure() -> None:
    import asyncio

    from src.nl2sql.local_real import live_probe

    closed: list[bool] = []
    rolled_back: list[bool] = []

    class _Tx:
        is_active = True

        async def rollback(self) -> None:
            rolled_back.append(True)

    class _Session:
        def __init__(self) -> None:
            self.tx = _Tx()

        async def begin(self) -> Any:
            return self.tx

        async def execute(self, statement: Any) -> Any:
            return None

        async def close(self) -> None:
            closed.append(True)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            await self.close()

    async def _run() -> None:
        with pytest.raises(RuntimeError):
            async with live_probe.bounded_read_only_session(_factory(_Session())):
                raise RuntimeError('probe failure')

    asyncio.run(_run())
    assert rolled_back == [True]
    assert closed == [True]


# --- microfix: canonical binding machine-check -------------------------------


def test_binding_matches_the_verified_canonical_source() -> None:
    """The transcribed binding must MACHINE-agree with the canonical source."""

    from src.nl2sql.local_real.semantics import assert_binding_matches_canonical

    assert_binding_matches_canonical(catalog=load_frozen_case_contracts())


def test_binding_mismatch_fails_closed_with_a_stable_reason() -> None:
    from src.nl2sql.local_real.semantics import assert_binding_matches_canonical
    from src.nl2sql.semantic.metric_contract import MetricCatalog

    catalog = load_frozen_case_contracts()
    kpi = next(
        m for m in catalog.metrics if m.metric_key == FROZEN_REAL_CASE_METRIC_KEY
    )
    broken = kpi.model_dump(mode='json')
    broken['business_time_column'] = 'completion_receipt_time'
    tampered = MetricCatalog(
        metrics=tuple(
            type(m).model_validate(
                broken if m.metric_key == kpi.metric_key else m.model_dump(mode='json')
            )
            for m in catalog.metrics
        )
    )
    with pytest.raises(LocalRealSemanticError) as raised:
        assert_binding_matches_canonical(catalog=tampered)
    assert 'local_real_binding_canonical_mismatch' in str(raised.value)


def test_binding_cannot_invent_identities_or_sources() -> None:
    from src.nl2sql.local_real.semantics import assert_binding_matches_canonical
    from src.nl2sql.semantic.metric_contract import MetricCatalog

    catalog = load_frozen_case_contracts()
    # dropping a frozen identity must fail
    with pytest.raises(LocalRealSemanticError) as raised:
        assert_binding_matches_canonical(
            catalog=MetricCatalog(metrics=catalog.metrics[:2])
        )
    assert 'local_real_binding_canonical_mismatch' in str(raised.value)
    # retargeting the published view must fail
    retargeted = MetricCatalog(
        metrics=tuple(
            type(m).model_validate(
                {**m.model_dump(mode='json'), 'source_ref': 'v_installation_work_order'}
            )
            for m in catalog.metrics
        )
    )
    with pytest.raises(LocalRealSemanticError) as raised2:
        assert_binding_matches_canonical(catalog=retargeted)
    assert 'local_real_binding_canonical_mismatch' in str(raised2.value)


def test_exact_executable_closure_proves_equality_not_only_absence() -> None:
    """A MISSING frozen metric is as much a defect as a stray one."""

    bundle = _bundle()
    assert_release_is_bounded(bundle.release, metric_keys=FROZEN_REAL_CASE_DEPENDENCIES)
    with pytest.raises(LocalRealSemanticError, match='missing a frozen executable metric'):
        assert_release_is_bounded(
            bundle.release,
            metric_keys=(*FROZEN_REAL_CASE_DEPENDENCIES, 'complaint_in_transit_count'),
        )


def test_real_canonical_source_passes_the_raw_source_table_check() -> None:
    """The genuine verified canonical source must satisfy the closure."""

    from src.nl2sql.local_real.semantics import (
        RAW_FROZEN_KEYS,
        assert_binding_matches_canonical,
        load_canonical_gold_metrics,
    )

    canonical = load_canonical_gold_metrics()
    assert RAW_FROZEN_KEYS == (
        'repair_service_archive_on_time_count_overall_day',
        'repair_service_calc_total_count_overall_day',
    )
    for key in RAW_FROZEN_KEYS:
        assert canonical[key]['source_type'] == 'raw', key
        assert canonical[key]['source_table'] == 'silver_repair_service', key
        assert canonical[key]['aggregation']['operation'] == 'count', key
        assert canonical[key]['aggregation']['column'] == 'id', key
    # The derived KPI declares NO source_table and must not be required to.
    assert 'source_table' not in canonical[FROZEN_REAL_CASE_METRIC_KEY]
    assert_binding_matches_canonical(catalog=load_frozen_case_contracts())


def test_denominator_canonical_source_table_tamper_fails_closed() -> None:
    """Changing ONLY the denominator canonical source must be detected."""

    from src.nl2sql.local_real.semantics import (
        DENOMINATOR_KEY,
        assert_binding_matches_canonical,
        load_canonical_gold_metrics,
    )

    canonical = load_canonical_gold_metrics()
    assert canonical[DENOMINATOR_KEY]['source_table'] == 'silver_repair_service'
    # Inject the drift WITHOUT touching the canonical YAML.
    tampered = dict(canonical)
    tampered[DENOMINATOR_KEY] = {
        **canonical[DENOMINATOR_KEY],
        'source_table': 'silver_single_faulty_order',
    }
    with pytest.raises(LocalRealSemanticError) as raised:
        assert_binding_matches_canonical(
            catalog=load_frozen_case_contracts(),
            canonical=tampered,
        )
    assert 'source_table' in str(raised.value)
    # The numerator is still bound, so the failure is specifically the denominator.
    assert DENOMINATOR_KEY in str(raised.value)


def test_numerator_canonical_source_table_tamper_still_fails_closed() -> None:
    from src.nl2sql.local_real.semantics import (
        NUMERATOR_KEY,
        assert_binding_matches_canonical,
        load_canonical_gold_metrics,
    )

    canonical = load_canonical_gold_metrics()
    tampered = dict(canonical)
    tampered[NUMERATOR_KEY] = {
        **canonical[NUMERATOR_KEY],
        'source_table': 'silver_single_faulty_order',
    }
    with pytest.raises(LocalRealSemanticError) as raised:
        assert_binding_matches_canonical(
            catalog=load_frozen_case_contracts(),
            canonical=tampered,
        )
    assert 'source_table' in str(raised.value)
    assert NUMERATOR_KEY in str(raised.value)


def test_raw_source_type_drift_fails_closed() -> None:
    from src.nl2sql.local_real.semantics import (
        DENOMINATOR_KEY,
        assert_binding_matches_canonical,
        load_canonical_gold_metrics,
    )

    canonical = load_canonical_gold_metrics()
    tampered = dict(canonical)
    tampered[DENOMINATOR_KEY] = {
        **canonical[DENOMINATOR_KEY],
        'source_type': 'derived',
    }
    with pytest.raises(LocalRealSemanticError) as raised:
        assert_binding_matches_canonical(
            catalog=load_frozen_case_contracts(),
            canonical=tampered,
        )
    assert 'source_type' in str(raised.value)
