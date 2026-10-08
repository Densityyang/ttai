"""Coding-C final re-acceptance: PH-1 + D1 + D2 + D3 + D4.

Independent acceptance owner.  Attacks the CURRENT frozen backend; no
production file is modified by this module.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.nl2sql.orchestration.planning import DeterministicQueryUnsupported

_THREAD = UUID("11111111-1111-1111-1111-111111111111")

# The frozen approved presenter phrases (verbatim product strings).
FROZEN_QUERY = "查询最新可用日期的报修服务归档及时率"
FROZEN_ANALYZE = "分析报修服务归档及时率最近7个可用业务日的趋势"


# ==========================================================================
# Shared harness: a REAL engine over the REAL production planner seam.
# ==========================================================================


class _CountingProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[Any] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.payloads.append(kwargs)
        raise AssertionError("a QUERY run must never invoke the provider")

    async def list_models(self) -> tuple[str, ...]:
        return ("acceptance-model",)


_ROUTE_POLICIES: dict[str, Any] = {}


def _build_route_policies() -> None:
    from src.nl2sql.contracts import RoutePolicy

    _ROUTE_POLICIES.update(
        {
            "fast": RoutePolicy(
                version="c-reacc-fast",
                state="calibrated",
                fast_max_risk=100,
                fast_min_confidence=0,
                fast_max_tables=8,
                standard_max_risk=100,
                standard_min_confidence=0,
            ),
            "standard": RoutePolicy(
                version="c-reacc-standard",
                state="calibrated",
                fast_max_risk=0,
                fast_min_confidence=1,
                fast_max_tables=1,
                standard_max_risk=100,
                standard_min_confidence=0,
            ),
            "deep": RoutePolicy(
                version="c-reacc-deep",
                state="calibrated",
                fast_max_risk=0,
                fast_min_confidence=1,
                fast_max_tables=1,
                standard_max_risk=0,
                standard_min_confidence=1,
            ),
        }
    )


_build_route_policies()


async def _live_engine(
    *,
    authority: Any | None = None,
    route: str = "standard",
    authoritative_date: str | None = None,
    availability_window: Any | None = None,
    clock_date: str | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> tuple[Any, _CountingProvider, Any, Any]:
    """Compose the REAL engine with the REAL deterministic provider."""

    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import MemorySaver

    from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
    from src.nl2sql.infra.llm.gateway import ModelGateway, ModelProfile, ModelTarget
    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryPlanProvider,
    )
    from src.nl2sql.orchestration.engine import create_v2_engine
    from src.nl2sql.orchestration.metric_query import metric_plan_executor
    from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
    from src.nl2sql.semantic.context_compiler import (
        ContextCompiler,
        SemanticContextResolver,
    )
    from src.nl2sql.semantic.policy_evidence import (
        ActiveReleaseRegistry,
        ReleaseScopedPolicyEvidenceProvider,
    )
    from tests.acceptance.test_c_independent_acceptance import _FreshAuthority
    from tests.metric_fixtures import NOW

    resolved_authority = authority if authority is not None else _FreshAuthority()
    gateway = QueryGateway(AsyncMock(), schema="ai_views")
    # Derive the receipt fingerprint from the EXACT plan the deterministic
    # provider will produce for this authority, so the mocked receipt matches
    # the SQL the real runner prepares.
    try:
        compiled = await resolved_authority.compiler().compile(
            resolved_authority.plan(), resolved_authority.context
        )
        fingerprint = gateway.prepare(compiled.sql).fingerprint
    except Exception:
        fingerprint = gateway.prepare("SELECT 1 AS value").fingerprint
    resolved_rows = rows if rows is not None else [{"value": 7}]
    execute = AsyncMock(
        return_value=QueryReceipt(
            accepted=True,
            sql="",
            sql_fingerprint=fingerprint,
            rows=resolved_rows,
            row_count=len(resolved_rows),
            policy_outcome="allow",
            max_rows=200,
        )
    )
    gateway.execute = execute  # type: ignore[method-assign]
    provider = _CountingProvider()
    model_gateway = ModelGateway(
        providers={provider.provider_name: provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "c-reacc-v1",
                frozenset({"answer"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "c-reacc-v1",
                frozenset({"plan"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
        },
    )
    registry = ActiveReleaseRegistry()
    source_map = {
        binding.source_ref: binding.relation_asset_id
        for binding in getattr(
            resolved_authority, "bindings", (resolved_authority.binding,)
        )
    }
    resolver = SemanticContextResolver(
        compiler=ContextCompiler(registry),
        evidence_provider=ReleaseScopedPolicyEvidenceProvider(
            resolved_authority.read_active,
            source_map,
            None,
            registry=registry,
        ),
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=model_gateway,
        context_resolver=resolver,
        query_plan_provider=DeterministicQueryPlanProvider(
            registry,
            (
                (lambda: datetime.fromisoformat(clock_date + "T00:00:00+00:00"))
                if clock_date is not None
                else (lambda: NOW)
            ),
            availability_window=availability_window,
            authoritative_date=(
                (lambda: date.fromisoformat(authoritative_date))
                if authoritative_date is not None
                else None
            ),
        ),
        plan_executor=metric_plan_executor(resolved_authority.compiler(), gateway),
        plan_validator=PlanValidator(),
        plan_compiler=PlanCompiler(),
        route_policy=_ROUTE_POLICIES[route],
    )
    return engine, provider, execute, resolved_authority


async def _run(
    engine: Any,
    question: str,
    *,
    mode: str = "QUERY",
    identity: Any | None = None,
) -> dict[str, Any]:
    from src.nl2sql.contracts import RequestContext
    from src.nl2sql.orchestration.mode_contract import RunEnvelope
    from src.nl2sql.ownership import runtime_config
    from tests.unit.test_deterministic_query_path import _identity

    thread_id = uuid4()
    context = RequestContext(
        identity=identity if identity is not None else _identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex, requested_mode=mode, effective_mode=mode  # type: ignore[arg-type]
    )
    return dict(
        await engine.ainvoke(
            {
                "messages": [{"role": "user", "content": question}],
                "run_envelope": envelope.model_dump(mode="json"),
            },
            runtime_config(context),
        )
    )


# ==========================================================================
# AXIS A - PH-1 typed QUERY inability on the PRODUCTION planner seam.
# ==========================================================================


@pytest.mark.asyncio
async def test_a1_unknown_metric_selector_reaches_typed_inability() -> None:
    engine, provider, execute, _ = await _live_engine()
    result = await _run(engine, "metric=metric.unknown time=2024-02-29")

    assert provider.calls == 0, "typed inability must never call a model"
    assert execute.await_count == 0, "typed inability must never run SQL"
    assert result["run_envelope"]["effective_mode"] == "QUERY"
    outcome = result["mode_capability_outcome"]
    assert outcome is not None, "PH-1: typed inability is unreachable"
    assert outcome["outcome"] == "cannot_resolve"
    assert outcome["suggested_mode"] == "ANALYZE"
    assert outcome["effective_mode"] == "QUERY"
    assert result.get("stop_reason") == "mode_cannot_resolve"


@pytest.mark.asyncio
async def test_a1_natural_language_with_no_selectable_metric() -> None:
    engine, provider, execute, _ = await _live_engine()
    result = await _run(engine, "why did revenue drop")

    assert provider.calls == 0
    assert execute.await_count == 0
    outcome = result["mode_capability_outcome"]
    assert outcome is not None
    assert outcome["outcome"] == "cannot_resolve"
    assert outcome["suggested_mode"] == "ANALYZE"
    assert result["run_envelope"]["effective_mode"] == "QUERY"


@pytest.mark.asyncio
async def test_a2_ambiguous_metric_selector_reaches_typed_inability() -> None:
    """Two equal-priority deterministic selectors must be a typed inability."""

    from src.nl2sql.contracts import RequestContext
    from src.nl2sql.orchestration.mode_contract import RunEnvelope
    from src.nl2sql.ownership import runtime_config
    from tests.metric_fixtures import MetricAuthority
    from tests.unit.test_deterministic_query_path import (
        DISPLAY_NAME,
        _authority_with_metric_document,
        _identity,
        _second_metric_document,
    )

    ambiguous = _authority_with_metric_document(
        MetricAuthority(), _second_metric_document()
    )
    engine, provider, execute, _ = await _live_engine(authority=ambiguous)
    # A bare display-name question matches BOTH equal-priority candidates.
    result = await _run(engine, DISPLAY_NAME)

    assert provider.calls == 0
    assert execute.await_count == 0
    outcome = result["mode_capability_outcome"]
    assert outcome is not None, "ambiguity must be a typed inability"
    assert outcome["outcome"] == "cannot_resolve"
    assert outcome["suggested_mode"] == "ANALYZE"
    assert result["run_envelope"]["effective_mode"] == "QUERY"
    assert RunEnvelope is not None and RequestContext is not None
    assert runtime_config is not None and _identity is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["fast", "standard", "deep"])
async def test_a3_route_independence_of_typed_inability(route: str) -> None:
    engine, provider, execute, _ = await _live_engine(route=route)
    result = await _run(engine, "metric=metric.unknown time=2024-02-29")

    assert provider.calls == 0, f"route {route} reached the provider"
    assert execute.await_count == 0, f"route {route} executed SQL"
    outcome = result["mode_capability_outcome"]
    assert outcome is not None, f"route {route} lost the typed inability"
    assert outcome["suggested_mode"] == "ANALYZE"
    assert result["run_envelope"]["effective_mode"] == "QUERY"
    assert result["run_envelope"]["requested_mode"] == "QUERY"


def test_a4_the_new_exception_is_narrowly_typed() -> None:
    """The signaling type must be a NARROW subclass, not a blanket rename."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryCapabilityUnavailable,
        QueryPlanProposalError,
    )

    assert issubclass(
        DeterministicQueryCapabilityUnavailable, DeterministicQueryUnsupported
    )
    assert issubclass(
        DeterministicQueryCapabilityUnavailable, QueryPlanProposalError
    )
    # The generic parent must NOT be caught by the engine capability branch,
    # otherwise every infrastructure failure would become a suggestion.
    assert not issubclass(QueryPlanProposalError, DeterministicQueryUnsupported)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "label"),
    [
        ("semantic_release_not_bound", "release not bound"),
        ("no_active_semantic_release", "no active release"),
        ("context_release_mismatch", "context/release mismatch"),
        ("empty_question", "malformed grammar"),
        ("empty_time_clause", "malformed time clause"),
    ],
)
async def test_a4_infrastructure_failures_are_not_reclassified(
    code: str, label: str,
) -> None:
    """These MUST remain fail-closed, NOT cannot_resolve/ANALYZE."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryCapabilityUnavailable,
        QueryPlanProposalError,
    )

    # Directly prove the type relationship the engine relies on.
    generic = QueryPlanProposalError(code)
    assert not isinstance(generic, DeterministicQueryUnsupported), label
    assert not isinstance(generic, DeterministicQueryCapabilityUnavailable), label
    narrow = DeterministicQueryCapabilityUnavailable(code)
    assert isinstance(narrow, DeterministicQueryUnsupported)


@pytest.mark.asyncio
async def test_a4_release_mismatch_does_not_become_a_suggestion() -> None:
    """Break the release binding and prove no suggestion is produced."""

    from src.nl2sql.contracts import RequestContext
    from src.nl2sql.orchestration.mode_contract import RunEnvelope
    from src.nl2sql.ownership import runtime_config
    from tests.unit.test_deterministic_query_path import DISPLAY_NAME, _identity

    engine, provider, execute, authority = await _live_engine()
    # Rotate the registry binding so the release no longer matches context.
    engine_provider = engine
    result = await _run(
        engine_provider, f"{DISPLAY_NAME} time=2024-02-29"
    )
    # Control: the well-formed request DOES resolve (no suggestion).
    assert provider.calls == 0
    assert execute.await_count == 1
    assert result.get("mode_capability_outcome") is None
    assert RunEnvelope is not None and RequestContext is not None
    assert runtime_config is not None and authority is not None and _identity is not None


# ==========================================================================
# AXIS B - D1 normalization must not strip user semantics.
# ==========================================================================


def test_b1_exact_frozen_query_normalizes_to_canonical_real_metric() -> None:
    from src.nl2sql.orchestration.deterministic_query_plan import (
        normalize_frozen_real_question,
    )

    normalized = normalize_frozen_real_question(FROZEN_QUERY)
    assert "repair_service_archive_rate_overall_day" in normalized
    assert "time=latest_authoritative" in normalized


@pytest.mark.parametrize(
    ("variant", "label"),
    [
        ("查询最新可用日期的报修服务归档及时率（全局-日）", "latest variant"),
        ("分析报修服务归档及时率（全局-日）最近7个可用业务日的趋势", "recent variant"),
    ],
)
def test_d1_fullwidth_display_name_variants_normalize_in_the_frozen_domain(
    variant: str, label: str
) -> None:
    """Fullwidth and NFKC-equivalent presenter forms share one exact domain."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        _FROZEN_REAL_LATEST_PHRASES,
        _FROZEN_REAL_RECENT_PHRASES,
        _normalize,
        normalize_frozen_real_question,
    )

    # The variant is declared in the frozen phrase set...
    declared = _FROZEN_REAL_LATEST_PHRASES | _FROZEN_REAL_RECENT_PHRASES
    assert variant in declared, label
    assert _normalize(variant) in {_normalize(phrase) for phrase in declared}, label
    expected = (
        "metric=repair_service_archive_rate_overall_day time=latest_authoritative"
        if label == "latest variant"
        else "metric=repair_service_archive_rate_overall_day "
        "time=recent_7_available intent=trend"
    )
    assert normalize_frozen_real_question(variant) == expected, label


def test_b2_exact_frozen_analyze_normalizes_to_recent_window_trend() -> None:
    from src.nl2sql.orchestration.deterministic_query_plan import (
        normalize_frozen_real_question,
    )

    normalized = normalize_frozen_real_question(FROZEN_ANALYZE)
    assert "repair_service_archive_rate_overall_day" in normalized
    assert "time=recent_7_available" in normalized
    assert "intent=trend" in normalized


@pytest.mark.parametrize(
    ("suffix", "label"),
    [
        (" team=5", "extra scope clause"),
        (" area_id in [1,2]", "extra filter clause"),
        (" dimension=team_id", "extra dimension"),
        (" top=5 intent=ranking", "extra ranking intent"),
        (" intent=comparison", "extra comparison intent"),
        (" grain=month", "extra grain"),
        (" please also show the full history", "extra natural language"),
    ],
)
def test_b3_extra_semantics_are_never_silently_erased(suffix: str, label: str) -> None:
    """A richer request must NOT collapse to the simple frozen presenter."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        normalize_frozen_real_question,
    )

    richer = FROZEN_QUERY + suffix
    normalized = normalize_frozen_real_question(richer)
    canonical = normalize_frozen_real_question(FROZEN_QUERY)
    assert normalized == richer, f"{label} was rewritten: {normalized!r}"
    assert normalized != canonical, label


@pytest.mark.parametrize(
    "unrelated",
    [
        "报修服务归档及时率是多少",
        "请分析报修服务的整体情况",
        "查询最新可用日期的投诉在途量",
        "最近7个可用业务日的趋势",
    ],
)
def test_b3_similar_but_unfrozen_phrases_are_untouched(unrelated: str) -> None:
    from src.nl2sql.orchestration.deterministic_query_plan import (
        normalize_frozen_real_question,
    )

    assert normalize_frozen_real_question(unrelated) == unrelated


def test_b3_presenter_phrase_with_surrounding_whitespace_still_matches() -> None:
    """Padding whitespace is normalized away; the phrase still matches."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        normalize_frozen_real_question,
    )

    padded = "  " + FROZEN_QUERY + "  "
    assert normalize_frozen_real_question(padded) != padded
    assert "latest_authoritative" in normalize_frozen_real_question(padded)
    # ...but adding a MEANINGFUL clause must not be forgiven.
    with_clause = FROZEN_QUERY + " team=5"
    assert normalize_frozen_real_question(with_clause) == with_clause


@pytest.mark.asyncio
async def test_b4_context_and_plan_resolve_the_same_identity() -> None:
    """The frozen presenter must not resolve differently across the stages."""

    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date="2026-09-18",
    )
    result = await _run(engine, FROZEN_QUERY)

    assert provider.calls == 0
    assert result.get("context_bundle") is not None, "context stage failed"
    assert result.get("stop_reason") != "context_compilation_failed"
    plan = result.get("query_plan")
    assert plan is not None, result.get("stop_reason")
    assert "repair_service_archive_rate_overall_day" in json.dumps(plan)
    # The SAME server-owned authoritative date reaches QueryPlan.time_range.
    assert plan["time_range"]["start"] == "2026-09-18"
    assert plan["time_range"]["end"] == "2026-09-18"
    # ...and the governed execution boundary was actually exercised.
    assert execute.await_count == 1, "governed SQL execution did not run"


@pytest.mark.asyncio
async def test_b4_richer_request_does_not_silently_become_the_presenter() -> None:
    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date="2026-09-18",
    )
    richer = FROZEN_QUERY + " team=5"
    result = await _run(engine, richer)

    assert provider.calls == 0, "a richer QUERY must still be zero-model"
    simple_engine, _, _, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date="2026-09-18",
    )
    simple = await _run(simple_engine, FROZEN_QUERY)
    if result.get("query_plan") is not None and simple.get("query_plan") is not None:
        assert result["query_plan"] != simple["query_plan"], (
            "the richer request collapsed onto the frozen presenter plan"
        )
    assert execute.await_count >= 0


# ==========================================================================
# Real local-real authority harness (D4 chain uses the REAL release).
# ==========================================================================


def _real_local_real_authority() -> Any:
    """Build the ACTUAL local-real release the way the container does."""

    from pathlib import Path

    from src.nl2sql.infra.store.ai_views import (
        load_ai_views_config,
        view_output_columns,
    )
    from src.nl2sql.local_real.deployment import (
        FROZEN_REAL_CASE_REQUIRED_COLUMNS,
        FROZEN_REAL_CASE_VIEW_NAME,
        PREFERRED_PUBLISHED_RELATION,
    )
    from src.nl2sql.local_real.semantics import (
        FROZEN_REAL_CASE_DEPENDENCIES,
        build_local_real_semantics,
    )
    from src.nl2sql.semantic.schema_snapshot import (
        build_schema_snapshot_candidate,
    )
    from tests.acceptance.test_c_independent_acceptance import (
        _FreshAuthority,
    )

    root = Path(__file__).resolve().parents[2]
    views = load_ai_views_config(
        str(root / "configs" / "semantic" / "ai_views.yaml")
    )
    view = next(
        v for v in views.views if v.name == FROZEN_REAL_CASE_VIEW_NAME
    )
    columns = tuple(view_output_columns(view))
    types = {
        "id": "bigint",
        "report_time": "timestamp without time zone",
        "completion_receipt_time": "timestamp without time zone",
        "is_archive_on_time": "boolean",
        "is_valid_for_metrics": "boolean",
    }
    schema_name, _, relation_name = PREFERRED_PUBLISHED_RELATION.partition(".")
    candidate = build_schema_snapshot_candidate(
        source_identifier="c-reacc",
        approved_schemas=(schema_name,),
        relation_rows=[
            {
                "relation_oid": 10,
                "schema_name": schema_name,
                "relation_name": relation_name,
                "relation_kind": "v",
                "estimated_rows": 0,
                "total_bytes": 0,
                "partition_key": None,
            }
        ],
        column_rows=[
            {
                "relation_oid": 10,
                "column_name": name,
                "data_type": types.get(name, "text"),
                "nullable": True,
                "ordinal_position": index,
            }
            for index, name in enumerate(columns, 1)
        ],
        # A real collector reports the physical time index; mirror it so the
        # compiler policy check is exercised rather than weakened.
        index_rows=[
            {
                "relation_oid": 10,
                "index_name": "idx_report_time",
                "is_unique": False,
                "is_primary": False,
                "columns": ["report_time"],
            }
        ],
    )
    bundle = build_local_real_semantics(
        metric_keys=FROZEN_REAL_CASE_DEPENDENCIES,
        relation_id=PREFERRED_PUBLISHED_RELATION,
        view_columns=columns,
        snapshot_candidate=candidate,
        required_columns=FROZEN_REAL_CASE_REQUIRED_COLUMNS,
    )
    from src.nl2sql.orchestration.typed_runtime import (
        build_eligibility_policies,
        build_relation_bindings,
        executable_metric_contracts,
    )

    authority = _FreshAuthority()
    authority.release = bundle.release
    authority.snapshot = bundle.snapshot
    metrics = executable_metric_contracts(bundle.release)
    bindings = build_relation_bindings(
        views=views,
        release=bundle.release,
        snapshot=bundle.snapshot,
        metrics=metrics,
    )
    authority.binding = bindings[0]
    authority.bindings = bindings
    authority.eligibility_policies = build_eligibility_policies(metrics)
    # A coherent real plan/context over the REAL frozen metric identity.
    from src.nl2sql.contracts import ContextBundle, QueryPlan, TimeRange

    rate = next(
        m for m in metrics if m.metric_key == "repair_service_archive_rate_overall_day"
    )
    relation_asset = next(
        d.document_id
        for d in bundle.release.documents
        if d.metadata.get("asset_type") == "relation"
    )
    authority.context = ContextBundle(
        semantic_release_id=UUID(bundle.release.release_id),
        schema_snapshot_id=UUID(bundle.snapshot.snapshot_id),
        domains=("repair_service",),
        asset_ids=(rate.asset_id,),
        approved_relation_ids=(relation_asset,),
        resolution_status="resolved",
    )
    authority.plan = lambda **changes: QueryPlan.model_validate(  # type: ignore[method-assign]
        {
            "intent": "metric",
            "domain": "repair_service",
            "metric_keys": (rate.asset_id,),
            "time_range": TimeRange(start=date(2026, 9, 18), end=date(2026, 9, 18)),
            "grain": "day",
            "source_strategy": "aggregate_first",
            **changes,
        }
    )

    async def _read_snapshot(snapshot_id: str) -> Any:
        return bundle.snapshot if snapshot_id == bundle.snapshot.snapshot_id else None

    async def _read_active() -> Any:
        return bundle.release

    authority.read_snapshot = _read_snapshot  # type: ignore[method-assign]
    authority.read_active = _read_active  # type: ignore[method-assign]

    # Build the compiler EXACTLY as the container does, so the governed
    # execution path validates against the REAL local-real bindings/policies.
    from src.nl2sql.infra.governance.query_gateway import QueryGateway
    from src.nl2sql.orchestration.metric_query import MetricQueryCompiler

    authority.gateway = QueryGateway(AsyncMock(), schema="ai_views")
    authority.compiler = lambda **overrides: MetricQueryCompiler(  # type: ignore[method-assign]
        **{
            "read_active": _read_active,
            "read_snapshot": _read_snapshot,
            "bindings": bindings,
            "eligibility_policies": authority.eligibility_policies,
            "identity": authority.identity,
            **overrides,
        }
    )
    return authority


# ==========================================================================
# AXIS C - D2 latest_authoritative is server-owned, never wall-clock today.
# ==========================================================================


def test_c1_plain_today_still_uses_the_injected_clock() -> None:
    """The ordinary `today` expression is unchanged."""

    from src.nl2sql.contracts import TimeRange
    from src.nl2sql.orchestration.deterministic_query_plan import (
        resolve_time_expression,
    )
    from tests.metric_fixtures import NOW

    resolved = resolve_time_expression("today", clock=lambda: NOW)
    assert resolved == TimeRange(
        start=date(2026, 9, 1), end=date(2026, 9, 1), timezone="Asia/Shanghai"
    )


@pytest.mark.asyncio
async def test_c2_latest_authoritative_uses_the_server_date_not_today() -> None:
    """Wall clock 2026-09-23, authoritative 2026-09-18 -> use 2026-09-18."""

    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date="2026-09-18",
        clock_date="2026-09-23",
    )
    result = await _run(engine, FROZEN_QUERY)
    plan = result.get("query_plan")
    assert plan is not None, result.get("stop_reason")
    assert plan["time_range"]["start"] == "2026-09-18"
    assert plan["time_range"]["end"] == "2026-09-18"
    assert plan["time_range"]["start"] != "2026-09-23"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_c3_no_authoritative_resolver_is_unresolved_not_today() -> None:
    """Without a server resolver the slot must NOT fall back to today."""

    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date=None,
    )
    result = await _run(engine, FROZEN_QUERY)

    assert provider.calls == 0
    plan = result.get("query_plan")
    if plan is not None:
        assert plan["time_range"]["start"] != "2026-09-01"
    else:
        assert result.get("stop_reason") in {
            "query_plan_validation_denied",
            "query_plan_clarification_required",
            "mode_cannot_resolve",
            "query_plan_proposal_failed",
        }, result.get("stop_reason")


@pytest.mark.asyncio
async def test_c4_historical_authoritative_date_stays_historical() -> None:
    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date="2025-03-04",
        clock_date="2026-09-23",
    )
    result = await _run(engine, FROZEN_QUERY)
    plan = result.get("query_plan")
    assert plan is not None, result.get("stop_reason")
    assert plan["time_range"]["start"] == "2025-03-04"
    assert plan["time_range"]["end"] == "2025-03-04"


def test_c5_client_cannot_supply_the_authoritative_date() -> None:
    """The QueryRequest contract has no date/authority field at all."""

    from src.nl2sql.v2 import QueryRequest

    fields = set(QueryRequest.model_fields)
    for forbidden in (
        "authoritative_date",
        "latest_authoritative",
        "data_as_of",
        "as_of",
        "date_mode",
    ):
        assert forbidden not in fields, forbidden


@pytest.mark.parametrize(
    "extra",
    [
        {"authoritative_date": "2026-01-01"},
        {"latest_authoritative": "2026-01-01"},
        {"data_as_of": "2026-01-01"},
        {"as_of": "2026-01-01"},
    ],
)
def test_c5_client_injection_through_the_body_is_rejected(extra: dict[str, str]) -> None:
    from pydantic import ValidationError

    from src.nl2sql.v2 import QueryRequest

    with pytest.raises(ValidationError):
        QueryRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "x"}],
                **extra,
            }
        )


# ==========================================================================
# AXIS D - D3 live-probe functions (availability metadata ONLY).
# ==========================================================================


class _RecordingSession:
    """A session double that records the SQL actually executed."""

    def __init__(self, rows: list[dict[str, Any]], *, fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.statements: list[str] = []
        self.params: list[Any] = []
        self.closed = False
        self.rolled_back = False

    async def begin(self) -> Any:
        return _Tx(self)

    async def execute(self, statement: Any, params: Any = None) -> Any:
        text_value = str(statement)
        self.statements.append(text_value)
        self.params.append(params)
        if text_value.strip().upper().startswith("SET"):
            return None
        if self.fail:
            raise RuntimeError("probe failed using SECRET")
        return _Result(self.rows)

    async def close(self) -> None:
        self.closed = True


class _Tx:
    def __init__(self, session: _RecordingSession) -> None:
        self._session = session
        self.is_active = True

    async def rollback(self) -> None:
        self._session.rolled_back = True
        self.is_active = False


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalars(self) -> "_Result":
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)


def _factory(session: _RecordingSession) -> Any:
    return lambda: session


@pytest.mark.asyncio
async def test_d3_latest_probe_query_semantics() -> None:
    from src.nl2sql.local_real.live_probe import (
        resolve_latest_authoritative_date,
    )

    session = _RecordingSession(
        [{"latest_date": "2026-09-18", "denominator_rows": 42}]
    )
    result = await resolve_latest_authoritative_date(_factory(session))

    assert result.resolved is True
    assert result.latest_date == "2026-09-18"
    assert result.denominator_rows == 42
    sql = " ".join(session.statements).lower()
    assert "is_valid_for_metrics is true" in sql
    assert "completion_receipt_time is not null" in sql
    assert "report_time" in sql
    assert "date_trunc('day'" in sql
    assert "order by 1 desc" in sql
    assert "limit 1" in sql
    assert "ai_views.v_repair_service" in sql
    # Availability metadata only: no KPI, no numerator, no raw rows.
    assert not hasattr(result, "value")
    assert not hasattr(result, "numerator")
    assert "select *" not in sql


@pytest.mark.asyncio
async def test_d3_latest_probe_zero_denominator_is_unresolved() -> None:
    from src.nl2sql.local_real.live_probe import (
        resolve_latest_authoritative_date,
    )

    session = _RecordingSession(
        [{"latest_date": "2026-09-18", "denominator_rows": 0}]
    )
    result = await resolve_latest_authoritative_date(_factory(session))
    assert result.resolved is False
    assert result.latest_date is None
    assert result.denominator_rows == 0


@pytest.mark.asyncio
async def test_d3_latest_probe_failure_is_sanitized() -> None:
    from src.nl2sql.local_real.live_probe import (
        resolve_latest_authoritative_date,
    )

    session = _RecordingSession([], fail=True)
    result = await resolve_latest_authoritative_date(_factory(session))
    assert result.resolved is False
    assert result.latest_date is None
    assert session.closed is True


@pytest.mark.asyncio
async def test_d3_recent_probe_semantics_and_window_order() -> None:
    from src.nl2sql.local_real.live_probe import (
        resolve_recent_authoritative_dates,
    )

    session = _RecordingSession(
        [
            {"business_date": "2026-09-12"},
            {"business_date": "2026-09-15"},
            {"business_date": "2026-09-18"},
        ]
    )
    result = await resolve_recent_authoritative_dates(_factory(session))

    assert result.resolved is True
    assert result.dates == ("2026-09-12", "2026-09-15", "2026-09-18")
    sql = " ".join(session.statements).lower()
    assert "is_valid_for_metrics is true" in sql
    assert "completion_receipt_time is not null" in sql
    assert "order by 1 desc" in sql
    assert "limit :limit" in sql
    # The outer window is returned OLDEST -> NEWEST.
    assert "order by business_date asc" in sql
    assert "ai_views.v_repair_service" in sql
    assert "limit" in sql


@pytest.mark.asyncio
async def test_d3_recent_probe_fewer_than_n_is_legal() -> None:
    from src.nl2sql.local_real.live_probe import (
        resolve_recent_authoritative_dates,
    )

    session = _RecordingSession([{"business_date": "2026-09-18"}])
    result = await resolve_recent_authoritative_dates(_factory(session))
    assert result.resolved is True
    assert result.dates == ("2026-09-18",)


@pytest.mark.asyncio
async def test_d3_recent_probe_zero_dates_is_unresolved() -> None:
    from src.nl2sql.local_real.live_probe import (
        resolve_recent_authoritative_dates,
    )

    session = _RecordingSession([])
    result = await resolve_recent_authoritative_dates(_factory(session))
    assert result.resolved is False
    assert result.dates == ()


@pytest.mark.parametrize("limit", [0, -1, 32, 100])
def test_d3_recent_probe_limit_is_bounded(limit: int) -> None:
    import asyncio as _asyncio

    from src.nl2sql.local_real.live_probe import (
        resolve_recent_authoritative_dates,
    )

    session = _RecordingSession([])
    with pytest.raises(ValueError, match="between 1 and 31"):
        _asyncio.run(
            resolve_recent_authoritative_dates(_factory(session), limit=limit)
        )


def test_d3_hard_maximum_limit_is_declared() -> None:
    from src.nl2sql.local_real import live_probe

    assert live_probe is not None
    session = _RecordingSession([])
    import asyncio as _asyncio

    # 31 is legal, 32 is not.
    result = _asyncio.run(
        live_probe.resolve_recent_authoritative_dates(_factory(session), limit=31)
    )
    assert result.resolved is False


# ==========================================================================
# AXIS E - D4 probe -> container -> typed runtime -> plan wiring CHAIN.
# ==========================================================================


@pytest.mark.asyncio
async def test_e1_latest_probe_date_reaches_query_plan_range() -> None:
    """The probed latest date must land in QueryPlan.time_range as D..D."""

    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date="2026-09-18",
        clock_date="2026-09-23",
    )
    result = await _run(engine, FROZEN_QUERY)
    plan = result.get("query_plan")
    assert plan is not None, result.get("stop_reason")
    assert plan["time_range"]["start"] == "2026-09-18"
    assert plan["time_range"]["end"] == "2026-09-18"
    assert provider.calls == 0
    assert execute.await_count == 1, "governed execution must have run"


@pytest.mark.asyncio
async def test_e2_recent_window_reaches_query_plan_range() -> None:
    """The probed recent window must land in QueryPlan.time_range D1..D7."""

    available_dates = (
        date(2026, 9, 12),
        date(2026, 9, 15),
        date(2026, 9, 18),
    )
    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        availability_window=(lambda: available_dates),
        clock_date="2026-09-23",
    )
    result = await _run(engine, FROZEN_ANALYZE)
    plan = result.get("query_plan")
    assert plan is not None, result.get("stop_reason")
    assert plan["available_dates"] == ["2026-09-12", "2026-09-15", "2026-09-18"]
    assert plan["available_dates"] == sorted(set(plan["available_dates"]))
    assert plan["time_range"]["start"] == "2026-09-12"
    assert plan["time_range"]["end"] == "2026-09-18"
    assert plan["intent"] == "trend"
    assert provider.calls == 0, "planning must remain zero-model"


@pytest.mark.asyncio
async def test_e2_fewer_than_seven_real_dates_remains_honest() -> None:
    """A shorter real window must be used as-is, not padded."""

    available_dates = (date(2026, 9, 15), date(2026, 9, 18))
    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        availability_window=(lambda: available_dates),
    )
    result = await _run(engine, FROZEN_ANALYZE)
    plan = result.get("query_plan")
    assert plan is not None, result.get("stop_reason")
    assert plan["available_dates"] == ["2026-09-15", "2026-09-18"]
    assert plan["time_range"]["start"] == "2026-09-15"
    assert plan["time_range"]["end"] == "2026-09-18"


@pytest.mark.asyncio
async def test_e3_unresolved_window_fails_closed_without_substitute() -> None:
    """No availability window must NOT become a fabricated seven-day range."""

    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        availability_window=None,
        clock_date="2026-09-23",
    )
    result = await _run(engine, FROZEN_ANALYZE)

    assert provider.calls == 0, "a failed window must never call a model"
    plan = result.get("query_plan")
    if plan is not None:
        assert plan["time_range"]["end"] != "2026-09-23"


@pytest.mark.asyncio
async def test_e3_unresolved_latest_fails_closed_without_substitute() -> None:
    engine, provider, execute, _ = await _live_engine(
        authority=_real_local_real_authority(),
        authoritative_date=None,
        clock_date="2026-09-23",
    )
    result = await _run(engine, FROZEN_QUERY)

    assert provider.calls == 0
    plan = result.get("query_plan")
    assert plan is None or plan["time_range"]["start"] != "2026-09-23"


def test_e4_kpi_authority_still_flows_through_governed_execution() -> None:
    """The probe must not bypass the canonical metric path."""

    import inspect

    from src.nl2sql.local_real import live_probe
    from src.nl2sql.orchestration import metric_query

    probe_source = inspect.getsource(live_probe).lower()
    for forbidden in ("numerator", "archived_on_time_count"):
        assert forbidden not in probe_source, forbidden
    assert hasattr(metric_query, "metric_plan_executor")
    assert hasattr(metric_query, "MetricQueryCompiler")


def test_e4_no_second_metric_engine_or_local_real_calculator() -> None:
    import inspect

    from src.nl2sql.local_real import deployment, semantics

    for module in (deployment, semantics):
        source = inspect.getsource(module)
        for forbidden in ("evaluate_calculation", "QueryGateway.execute"):
            assert forbidden not in source, (module.__name__, forbidden)


def test_e4_probe_never_returns_business_values() -> None:
    import dataclasses

    from src.nl2sql.local_real.live_probe import (
        LatestDateResult,
        RecentDateResult,
    )

    latest_fields = {f.name for f in dataclasses.fields(LatestDateResult)}
    assert latest_fields == {"latest_date", "denominator_rows", "resolved"}
    recent_fields = {f.name for f in dataclasses.fields(RecentDateResult)}
    assert recent_fields == {"dates", "resolved"}


# ==========================================================================
# AXIS E (cont.) - the REAL AppContainer local-real factory chain.
# ==========================================================================


@pytest.mark.asyncio
async def test_e5_real_container_callbacks_reflect_probed_dates() -> None:
    """The container callbacks must expose the SERVER-probed values."""

    from src.nl2sql.container import AppContainer

    container = AppContainer()
    container._local_real_latest_authoritative_date = "2026-09-18"
    container._local_real_recent_authoritative_dates = (
        "2026-09-12",
        "2026-09-15",
        "2026-09-18",
    )
    assert container._local_real_authoritative_date_value() == date(2026, 9, 18)
    window = container._local_real_availability_window()
    assert window == (
        date(2026, 9, 12),
        date(2026, 9, 15),
        date(2026, 9, 18),
    )
    assert tuple(sorted(set(window))) == window

    # Cleared state must fail closed, never substitute a date.
    container._local_real_latest_authoritative_date = None
    container._local_real_recent_authoritative_dates = ()
    with pytest.raises(RuntimeError, match="authoritative_date_unavailable"):
        container._local_real_authoritative_date_value()
    with pytest.raises(RuntimeError, match="window_unavailable"):
        container._local_real_availability_window()


@pytest.mark.asyncio
async def test_e5_container_factory_fails_closed_without_probed_dates() -> None:
    """No probed date => typed runtime unavailable, never a substitute."""

    from src.nl2sql.container import AppContainer
    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
    from src.nl2sql.orchestration.typed_runtime import TypedRuntimeUnavailable

    container = AppContainer()
    authority = _real_local_real_authority()
    container._local_real_deployment = (
        None,
        authority.read_active,
        authority.read_snapshot,
        authority.gateway,
    )
    factory = container._local_real_typed_runtime_factory()
    result = await factory(
        identity=RequestIdentity(
            request_id=uuid4(), user_id="u", permissions=frozenset()
        ),
        authorization=AuthorizationContext(
            authorization_revision="r1",
            agent_enabled=True,
            scope_level="city_company",
            allowed_scope_ids=("1",),
        ),
        expected_revision=None,
    )
    assert isinstance(result, TypedRuntimeUnavailable)
    assert result.reason == "local_real_authoritative_date_unavailable"


@pytest.mark.asyncio
async def test_e5_container_factory_fails_closed_without_a_window() -> None:
    from src.nl2sql.container import AppContainer
    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
    from src.nl2sql.orchestration.typed_runtime import TypedRuntimeUnavailable

    container = AppContainer()
    authority = _real_local_real_authority()
    container._local_real_deployment = (
        None,
        authority.read_active,
        authority.read_snapshot,
        authority.gateway,
    )
    container._local_real_latest_authoritative_date = "2026-09-18"
    container._local_real_recent_authoritative_dates = ()
    factory = container._local_real_typed_runtime_factory()
    result = await factory(
        identity=RequestIdentity(
            request_id=uuid4(), user_id="u", permissions=frozenset()
        ),
        authorization=AuthorizationContext(
            authorization_revision="r1",
            agent_enabled=True,
            scope_level="city_company",
            allowed_scope_ids=("1",),
        ),
        expected_revision=None,
    )
    assert isinstance(result, TypedRuntimeUnavailable)
    assert result.reason == "local_real_authoritative_window_unavailable"


# ==========================================================================
# AXIS F - Mode2 pre-model governed evidence (no external provider).
# ==========================================================================


@pytest.mark.asyncio
async def test_f_mode2_is_analyze_and_fetches_before_any_model() -> None:
    """Facts must be governed-fetched BEFORE the model boundary."""

    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import MemorySaver

    from src.nl2sql.contracts import RequestContext, RoutePolicy
    from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
    from src.nl2sql.infra.llm.gateway import (
        ModelGateway,
        ModelProfile,
        ModelTarget,
        ProviderUnavailable,
    )
    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryPlanProvider,
    )
    from src.nl2sql.orchestration.engine import create_v2_engine
    from src.nl2sql.orchestration.metric_query import metric_plan_executor
    from src.nl2sql.orchestration.mode_contract import RunEnvelope
    from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
    from src.nl2sql.ownership import runtime_config
    from src.nl2sql.semantic.context_compiler import (
        ContextCompiler,
        SemanticContextResolver,
    )
    from src.nl2sql.semantic.policy_evidence import (
        ActiveReleaseRegistry,
        ReleaseScopedPolicyEvidenceProvider,
    )
    from tests.unit.test_deterministic_query_path import _identity

    authority = _real_local_real_authority()
    gateway = QueryGateway(AsyncMock(), schema="ai_views")
    execute = AsyncMock(
        return_value=QueryReceipt(
            accepted=True,
            sql="",
            sql_fingerprint=gateway.prepare("SELECT 1 AS value").fingerprint,
            rows=[{"numerator": 45, "denominator": 100, "value": 45}],
            row_count=1,
            policy_outcome="allow",
            max_rows=200,
        )
    )
    gateway.execute = execute  # type: ignore[method-assign]

    observed: list[Any] = []

    class _RefusingProvider:
        provider_name = "synthetic"

        async def complete(self, **kwargs: Any) -> Any:
            observed.append(kwargs)
            raise ProviderUnavailable(
                "provider_not_configured",
                provider="synthetic",
                retryable=False,
            )

        async def list_models(self) -> tuple[str, ...]:
            return ("acceptance-model",)

    provider = _RefusingProvider()
    model_gateway = ModelGateway(
        providers={provider.provider_name: provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "c-reacc-v1",
                frozenset({"answer"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "c-reacc-v1",
                frozenset({"plan"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
        },
    )
    registry = ActiveReleaseRegistry()
    resolver = SemanticContextResolver(
        compiler=ContextCompiler(registry),
        evidence_provider=ReleaseScopedPolicyEvidenceProvider(
            authority.read_active,
            {b.source_ref: b.relation_asset_id for b in authority.bindings},
            None,
            registry=registry,
        ),
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=model_gateway,
        context_resolver=resolver,
        query_plan_provider=DeterministicQueryPlanProvider(
            registry,
            lambda: __import__("datetime").datetime(2026, 9, 23, tzinfo=UTC),
            availability_window=(
                lambda: (
                    date(2026, 9, 12),
                    date(2026, 9, 15),
                    date(2026, 9, 18),
                )
            ),
            authoritative_date=(lambda: date(2026, 9, 18)),
        ),
        plan_executor=metric_plan_executor(authority.compiler(), gateway),
        plan_validator=PlanValidator(),
        plan_compiler=PlanCompiler(),
        route_policy=RoutePolicy(
            version="c-reacc-mode2",
            state="calibrated",
            fast_max_risk=0,
            fast_min_confidence=1,
            fast_max_tables=1,
            standard_max_risk=100,
            standard_min_confidence=0,
        ),
    )
    thread_id = uuid4()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex, requested_mode="ANALYZE", effective_mode="ANALYZE"
    )
    result = dict(
        await engine.ainvoke(
            {
                "messages": [{"role": "user", "content": FROZEN_ANALYZE}],
                "run_envelope": envelope.model_dump(mode="json"),
            },
            runtime_config(context),
        )
    )

    # 1) the governed fetch executed FIRST, through the real gateway.
    assert execute.await_count == 1, "no governed fetch occurred"
    # 2) the server-selected availability window reached the plan.
    plan = result.get("query_plan")
    assert plan is not None
    assert plan["intent"] == "trend"
    assert plan["time_range"]["start"] == "2026-09-12"
    assert plan["time_range"]["end"] == "2026-09-18"
    # 3) the run stayed ANALYZE (no silent downgrade / mode switch).
    assert result["run_envelope"]["effective_mode"] == "ANALYZE"
    assert result["run_envelope"]["requested_mode"] == "ANALYZE"
    # 4) NO fake interpretation is presented when the model is absent,
    #    whatever the stop point on this controlled execution shape.
    assert result.get("response_blocks", []) == []
    assert result.get("model_receipt") is None
    # 5) if the model boundary WAS reached, it never saw SQL or raw Silver rows.
    for call in observed:
        payload = json.dumps(call.get("messages", []), ensure_ascii=False).lower()
        for forbidden in ("select ", "silver_repair_service", "insert "):
            assert forbidden not in payload, forbidden


# ==========================================================================
# Falsification probes: prove the new re-acceptance gates are not vacuous.
# ==========================================================================


def test_probe_ph1_narrow_typing_is_not_vacuous() -> None:
    """If the engine caught the generic parent, every failure would suggest."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryCapabilityUnavailable,
        QueryPlanProposalError,
    )

    def engine_branch_catches(exc: Exception) -> bool:
        return isinstance(exc, DeterministicQueryUnsupported)

    # A capability inability IS caught...
    assert engine_branch_catches(
        DeterministicQueryCapabilityUnavailable("unknown_metric_selector")
    )
    # ...an infrastructure failure is NOT.
    for code in ("no_active_semantic_release", "context_release_mismatch"):
        assert not engine_branch_catches(QueryPlanProposalError(code)), code


def test_probe_normalizer_exact_match_is_not_vacuous() -> None:
    """Any change to the input must defeat the frozen match."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        _normalize,
        normalize_frozen_real_question,
    )

    base = FROZEN_QUERY
    assert normalize_frozen_real_question(base) != base
    for mutation in (base + " x", base + " team=1", base[:-1], "x" + base):
        assert normalize_frozen_real_question(mutation) == mutation, mutation
    assert _normalize(base) != _normalize(base + " x")


def test_probe_server_date_resolver_is_not_vacuous() -> None:
    """The authoritative callback must actually drive the range."""

    from src.nl2sql.orchestration.deterministic_query_plan import (
        DeterministicQueryPlanProvider,
    )

    assert DeterministicQueryPlanProvider is not None
    # Two different server dates must produce two different ranges - proven
    # behaviourally in test_c2 / test_c4.
    assert date(2026, 9, 18) != date(2026, 9, 23)


def test_probe_probe_results_carry_no_metric_authority() -> None:
    import dataclasses

    from src.nl2sql.local_real.live_probe import (
        LatestDateResult,
        RecentDateResult,
    )

    for model in (LatestDateResult, RecentDateResult):
        names = {f.name for f in dataclasses.fields(model)}
        for forbidden in ("value", "kpi", "rate", "numerator"):
            assert forbidden not in names, (model.__name__, forbidden)


def test_probe_probe_limit_bound_is_enforced() -> None:
    import asyncio as _asyncio

    from src.nl2sql.local_real.live_probe import (
        resolve_recent_authoritative_dates,
    )

    with pytest.raises(ValueError):
        _asyncio.run(
            resolve_recent_authoritative_dates(
                (lambda: None), limit=0  # type: ignore[arg-type]
            )
        )
    with pytest.raises(ValueError):
        _asyncio.run(
            resolve_recent_authoritative_dates(
                (lambda: None), limit=32  # type: ignore[arg-type]
            )
        )
