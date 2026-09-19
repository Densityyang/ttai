"""Deterministic QUERY plan authority: grammar, time boundary and engine exits.

The doubles of ``test_plan_pipeline`` are replaced here by the real
collaborators: ``SemanticContextResolver`` + ``ContextCompiler`` +
``ActiveReleaseRegistry``, ``ReleaseScopedPolicyEvidenceProvider``,
``DeterministicQueryPlanProvider`` and ``GatewayMetricStepRunner`` through
``metric_plan_executor`` over a real ``QueryGateway`` with a mocked execute.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver

from src.nl2sql.contracts import (
    BoundFilter,
    ContextBundle,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    TimeRange,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.candidates import rowset_sha256
from src.nl2sql.orchestration.deterministic_query_plan import (
    DeterministicQueryPlanProvider,
    QueryPlanProposalError,
    UnresolvedTime,
    resolve_time_expression,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.metric_query import metric_plan_executor
from src.nl2sql.orchestration.planning import (
    PlanCompiler,
    PlanValidationError,
    PlanValidator,
)
from src.nl2sql.ownership import runtime_config
from src.nl2sql.semantic.context_compiler import ContextCompiler, SemanticContextResolver
from src.nl2sql.semantic.metric_match import detect_metric_candidates
from src.nl2sql.semantic.policy_evidence import (
    QUESTION_METRIC_UNMATCHED_FLAG,
    ActiveReleaseBoundError,
    ActiveReleaseRegistry,
    ReleaseScopedPolicyEvidenceProvider,
)
from src.nl2sql.semantic.registry import SemanticDocument
from tests.metric_fixtures import MetricAuthority, ratio_contract, seed_contract

REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")
DISPLAY_NAME = "\u6295\u8bc9\u5728\u9014\u91cf"
METRIC_ASSET = "metric.complaint_in_transit_count"
FIXED_NOW = datetime(2026, 9, 19, 16, 30, tzinfo=UTC)  # 2026-09-20 00:30 Asia/Shanghai


class _FixedClock:
    """A counting injected clock: proves the once-per-proposal sampling."""

    def __init__(self, instant: datetime = FIXED_NOW) -> None:
        self.instant = instant
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.instant


class _CountingProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
        response_schema: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        del model, messages, timeout_ms, max_output_tokens, response_schema
        self.calls += 1
        return ProviderResponse(
            content="model path",
            model="small",
            usage={},
            finish_reason="stop",
        )

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


def _model_gateway(provider: _CountingProvider) -> ModelGateway:
    return ModelGateway(
        providers={provider.provider_name: provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget(provider.provider_name, "small", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "test-v1",
                frozenset({"plan"}),
                ModelTarget(provider.provider_name, "small", "small"),
                None,
            ),
        },
    )


def _identity(
    permissions: frozenset[str] | None = None,
) -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        roles=frozenset({"analyst"}),
        permissions=(
            permissions
            if permissions is not None
            else frozenset({"metrics:read", "nl2sql:invoke"})
        ),
    )


def _live_resolver(
    authority: MetricAuthority,
    permitted: frozenset[str] | None = None,
) -> tuple[SemanticContextResolver, ActiveReleaseRegistry]:
    registry = ActiveReleaseRegistry()
    provider = ReleaseScopedPolicyEvidenceProvider(
        authority.read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        permitted,
        registry=registry,
    )
    resolver = SemanticContextResolver(
        compiler=ContextCompiler(registry),
        evidence_provider=provider,
    )
    return resolver, registry


class _LiveEngine:
    """The composed deterministic QUERY engine, its registry and observable seams."""

    def __init__(
        self,
        engine: Any,
        gateway_execute: AsyncMock,
        provider: _CountingProvider,
        clock: _FixedClock,
        authority: MetricAuthority,
    ) -> None:
        self.engine = engine
        self.gateway_execute = gateway_execute
        self.provider = provider
        self.clock = clock
        self.authority = authority


async def _engine(
    authority: MetricAuthority,
    rows: list[dict[str, Any]] | None = None,
) -> _LiveEngine:
    """Compose the real collaborators over a real QueryGateway (mocked execute)."""

    resolved_rows = rows if rows is not None else [{"value": 7}]
    gateway = QueryGateway(AsyncMock(), schema="ai_views")
    # The mocked receipt must carry the fingerprint the runner will prepare for
    # the same compiled query.  A fixture whose compile is expected to fail
    # (permission denial) never reaches the runner, so a parser-valid stub
    # fingerprint is enough there.
    try:
        compiled = await authority.compiler().compile(authority.plan(), authority.context)
        fingerprint = gateway.prepare(compiled.sql).fingerprint
    except Exception:
        fingerprint = gateway.prepare("SELECT 1 AS value").fingerprint
    receipt = QueryReceipt(
        accepted=True,
        sql="",
        sql_fingerprint=fingerprint,
        rows=resolved_rows,
        row_count=len(resolved_rows),
        policy_outcome="allow",
        max_rows=200,
    )
    execute = AsyncMock(return_value=receipt)
    gateway.execute = execute  # type: ignore[method-assign]
    resolver, registry = _live_resolver(authority)
    provider = _CountingProvider()
    clock = _FixedClock()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(provider),
        context_resolver=resolver,
        query_plan_provider=DeterministicQueryPlanProvider(registry, clock),
        plan_executor=metric_plan_executor(authority.compiler(), gateway),
    )
    return _LiveEngine(engine, execute, provider, clock, authority)


def _authority_with_metric_document(
    authority: MetricAuthority,
    document: SemanticDocument,
) -> MetricAuthority:
    """Return the SAME authority with one extra metric document in its release."""

    assert authority.release is not None
    authority.release = replace(
        authority.release,
        documents=(*authority.release.documents, document),
    )
    authority.context = authority.context.model_copy(
        update={
            "asset_ids": (*authority.context.asset_ids, document.document_id),
        },
    )
    return authority


def _second_metric_document() -> SemanticDocument:
    """A different key whose display name still exactly selects DISPLAY_NAME."""

    other = seed_contract(metric_key="complaint_backlog_count")
    assert other.display_name == DISPLAY_NAME
    existing = MetricAuthority()
    assert existing.release is not None
    template = next(
        document
        for document in existing.release.documents
        if document.document_id == METRIC_ASSET
    )
    assert other.asset_id != METRIC_ASSET
    return replace(
        template,
        document_id=other.asset_id,
        metadata={**template.metadata, "execution_contract": other.model_dump_json()},
    )


async def _run(
    live: _LiveEngine,
    question: str,
    deadline_ms: int = 4_000,
    trace_id: str = "trace-deterministic-query",
) -> dict[str, Any]:
    context = RequestContext(
        identity=_identity(),
        thread_id=THREAD_ID,
        trace_id=trace_id,
        deadline_ms=deadline_ms,
    )
    result = await live.engine.ainvoke(
        {"messages": [{"role": "user", "content": question}]},
        runtime_config(context),
    )
    return dict(result)


def _trace_names(result: dict[str, Any]) -> list[str]:
    """Ordered trace event names recorded before the terminal stop."""

    return [str(event["name"]) for event in result["trace_events"]]


# --------------------------------------------------------------------------- #
# Grammar / provider unit tests
# --------------------------------------------------------------------------- #


def test_time_resolver_is_a_pure_deterministic_component() -> None:
    clock = _FixedClock()
    first = resolve_time_expression("2024-02-29", clock=clock)
    second = resolve_time_expression("2024-02-29", clock=clock)
    assert first == second == TimeRange(start=date(2024, 2, 29), end=date(2024, 2, 29))
    assert clock.calls == 2


@pytest.mark.asyncio
async def test_provider_matches_key_and_display_name_and_reports_unknown() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        identity=_identity(),
        route_hint="standard",
    )
    context = authority.context

    by_display = await provider.propose(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        context=context,
        identity=_identity(),
    )
    by_key = await provider.propose(
        question="metric=complaint_in_transit_count time=2024-02-29",
        context=context,
        identity=_identity(),
    )
    assert by_display == by_key
    assert by_display.metric_keys == (METRIC_ASSET,)
    assert by_display.required_permissions == ("nl2sql:invoke",)
    assert by_display.source_strategy == "aggregate_first"

    with pytest.raises(QueryPlanProposalError, match="unknown_metric_selector"):
        await provider.propose(
            question="metric=metric.unknown time=2024-02-29",
            context=context,
            identity=_identity(),
        )


@pytest.mark.asyncio
async def test_provider_rejects_out_of_grammar_tokens() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )
    for question in (
        "metric=complaint_in_transit_count time=2024-02-29 bogus=1",
        "metric=complaint_in_transit_count time=2024-02-29 grain=hour",
        "metric=complaint_in_transit_count time=2024-02-29 intent=detail",
        "metric=complaint_in_transit_count time=2024-02-29 top=5",
        "metric=complaint_in_transit_count time=2024-02-29 filter.unknown=x",
        "metric=complaint_in_transit_count time=2024-02-29 intent=ranking",
        (
            "metric=complaint_in_transit_count time=2024-02-29"
            " intent=comparison dim=city_company"
        ),
    ):
        with pytest.raises(QueryPlanProposalError):
            await provider.propose(
                question=question,
                context=authority.context,
                identity=_identity(),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clause",
    [
        # BARE form (the reproduced bypass: it reached real SQL before the fix)
        "area",
        "team",
        "employee",
        # DIM form
        "dim=area",
        "dim=team",
        "dim=employee",
        # FILTER form
        "area=999",
        "team=5",
        "employee=7",
        # MULTI-VALUE form
        "area=1,2",
        "team=1,2",
        "employee=1,2",
        # IN-CLAUSE shape: whitespace tokenizes, so the bare scope token leads
        # and is refused with the seam code.
        "area in [1,2]",
        "team in [1,2]",
        "employee in [1,2]",
    ],
)
async def test_organization_scope_clauses_are_refused_until_the_seam_is_live(
    clause: str,
) -> None:
    authority = MetricAuthority(ratio_contract())
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question="metric=complaint_first_response_rate",
        identity=_identity(),
        route_hint="standard",
    )
    question = "metric=complaint_first_response_rate time=2024-02-29 intent=ranking " + clause
    with pytest.raises(QueryPlanProposalError, match="org_scope_requires_authorization_seam"):
        await provider.propose(
            question=question,
            context=authority.context,
            identity=_identity(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", ["areain[1,2]", "teamin[1,2]"])
async def test_non_standard_in_clause_shapes_also_fail_closed(clause: str) -> None:
    # The bracketed form without whitespace is not a recognized clause at all;
    # it is refused as an unknown clause, which is still fail-closed.
    authority = MetricAuthority(ratio_contract())
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question="metric=complaint_first_response_rate",
        identity=_identity(),
        route_hint="standard",
    )
    question = "metric=complaint_first_response_rate time=2024-02-29 intent=ranking " + clause
    with pytest.raises(QueryPlanProposalError, match="unknown_clause"):
        await provider.propose(
            question=question,
            context=authority.context,
            identity=_identity(),
        )


def test_forged_proposal_intent_cannot_emit_an_organization_scope() -> None:
    """Defense in depth: the CONSTRUCTION boundary is guarded too.

    The parser guard alone would leave a directly constructed ProposalIntent
    (test, future code) able to compile an organization dimension or an
    ``entity_alias`` organization predicate straight to SQL.
    """

    authority = MetricAuthority(ratio_contract())
    assert authority.release is not None
    from src.nl2sql.orchestration.deterministic_query_plan import (
        ProposalIntent,
        build_plan,
    )

    candidate = detect_metric_candidates(
        "metric=complaint_first_response_rate",
        authority.release,
        frozenset(authority.context.asset_ids),
    )[0]
    resolved = resolve_time_expression("2024-02-29", clock=_FixedClock())

    forged_dimension = ProposalIntent(
        candidate=candidate,
        time_text="2024-02-29",
        time_conflict=False,
        grain="day",
        dimension="area",
        intent="ranking",
        result_limit=5,
        filters=(),
    )
    with pytest.raises(
        QueryPlanProposalError,
        match="org_scope_requires_authorization_seam",
    ):
        build_plan(forged_dimension, resolved, authority.context, candidate.contract)

    forged_filter = ProposalIntent(
        candidate=candidate,
        time_text="2024-02-29",
        time_conflict=False,
        grain="day",
        dimension="city_company",
        intent="metric",
        result_limit=None,
        filters=(
            BoundFilter(
                field_ref="area",
                operator="eq",
                value="999",
                source="entity_alias",
            ),
        ),
    )
    with pytest.raises(
        QueryPlanProposalError,
        match="org_scope_requires_authorization_seam",
    ):
        build_plan(forged_filter, resolved, authority.context, candidate.contract)

    # The guard at the protected boundary is what stops this predicate; without
    # it the compiler would bind area_id = :filter_0 (the reproduced R12 SQL).
    from src.nl2sql.orchestration.deterministic_query_plan import (
        _require_no_organization_scope_in_intent,
    )

    with pytest.raises(QueryPlanProposalError, match="org_scope_requires_authorization_seam"):
        _require_no_organization_scope_in_intent(forged_filter)


def test_build_plan_marks_an_unsupported_dimension_as_a_clarify_slot() -> None:
    # Exercised directly, and it uses a NON-organization dimension on purpose.
    # After the organization-scope restriction the only dimension the parser
    # can emit is city_company, which every metric lists in
    # supported_dimensions, so NO parser route reaches the unsupported-dimension
    # clarify branch; this direct-construction case is what keeps that semantic
    # covered.  Passing dimension="area" here would instead hit the
    # construction-boundary organization guard (see the forged-intent test).
    authority = MetricAuthority()
    assert authority.release is not None
    from src.nl2sql.orchestration.deterministic_query_plan import (
        ProposalIntent,
        build_plan,
    )

    candidate = detect_metric_candidates(
        DISPLAY_NAME,
        authority.release,
        frozenset(authority.context.asset_ids),
    )[0]
    intent = ProposalIntent(
        candidate=candidate,
        time_text="2024-02-29",
        time_conflict=False,
        grain="day",
        dimension="region",  # unsupported by the contract, and not an org scope
        intent="ranking",
        result_limit=5,
        filters=(),
    )
    plan, unresolved = build_plan(
        intent,
        resolve_time_expression("2024-02-29", clock=_FixedClock()),
        authority.context,
        candidate.contract,
    )
    assert unresolved == ("dimension",)
    assert plan.unresolved_slots == ("dimension",)
    assert plan.intent == "ranking"
    assert plan.dimensions == ("region",)
    assert plan.result_limit == 5


@pytest.mark.asyncio
async def test_repeated_time_clauses_are_clarification_not_failure() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )

    plan = await provider.propose(
        question="metric=complaint_in_transit_count time=today time=today",
        context=authority.context,
        identity=_identity(),
    )
    assert plan.unresolved_slots == ("time",)
    assert plan.time_range == TimeRange(start=date(1970, 1, 1), end=date(1970, 1, 1))


@pytest.mark.asyncio
async def test_empty_time_clause_is_a_proposal_failure_not_clarification() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )
    with pytest.raises(QueryPlanProposalError, match="empty_time_clause"):
        await provider.propose(
            question="metric=complaint_in_transit_count time=",
            context=authority.context,
            identity=_identity(),
        )


@pytest.mark.asyncio
async def test_two_different_relative_periods_are_clarification_not_failure() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )

    plan = await provider.propose(
        question="metric=complaint_in_transit_count time=today time=this_week",
        context=authority.context,
        identity=_identity(),
    )
    assert plan.unresolved_slots == ("time",)


@pytest.mark.asyncio
async def test_clock_is_sampled_exactly_once_per_proposal() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    clock = _FixedClock()
    provider = DeterministicQueryPlanProvider(registry, clock)
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )

    plan = await provider.propose(
        question="metric=complaint_in_transit_count time=today",
        context=authority.context,
        identity=_identity(),
    )

    assert clock.calls == 1
    assert plan.time_range == TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))


@pytest.mark.asyncio
async def test_no_time_clause_samples_the_clock_zero_times() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    clock = _FixedClock()
    provider = DeterministicQueryPlanProvider(registry, clock)
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )

    plan = await provider.propose(
        question="metric=complaint_in_transit_count",
        context=authority.context,
        identity=_identity(),
    )

    assert clock.calls == 0
    assert plan.unresolved_slots == ("time",)


@pytest.mark.asyncio
async def test_matcher_reports_two_equal_length_matches_as_ambiguous() -> None:
    authority = _authority_with_metric_document(MetricAuthority(), _second_metric_document())
    assert authority.release is not None
    permitted = frozenset({METRIC_ASSET, "metric.complaint_backlog_count"})
    assert len(detect_metric_candidates(DISPLAY_NAME, authority.release, permitted)) == 2
    resolver, registry = _live_resolver(authority, permitted)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )
    with pytest.raises(QueryPlanProposalError, match="ambiguous_metric_selector"):
        await provider.propose(
            question=f"{DISPLAY_NAME} time=2024-02-29",
            context=authority.context,
            identity=_identity(),
        )


@pytest.mark.asyncio
async def test_genuinely_suspending_read_active_under_a_running_loop() -> None:
    authority = MetricAuthority()
    suspends = 0

    async def suspending_read_active() -> Any:
        nonlocal suspends
        suspends += 1
        await asyncio.sleep(0)
        return authority.release

    registry = ActiveReleaseRegistry()
    scoped = ReleaseScopedPolicyEvidenceProvider(
        suspending_read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        None,
        registry=registry,
    )
    resolver = SemanticContextResolver(
        compiler=ContextCompiler(registry),
        evidence_provider=scoped,
    )

    context = await resolver.resolve(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        identity=_identity(),
        route_hint="standard",
    )

    assert suspends == 1
    assert registry.has_bound_release
    assert authority.release is not None
    assert registry.active_release() is authority.release
    assert context.semantic_release_id == UUID(authority.release.release_id)


def test_registry_read_before_bind_fails_closed() -> None:
    registry = ActiveReleaseRegistry()
    assert registry.has_bound_release is False
    with pytest.raises(ActiveReleaseBoundError):
        registry.active_release()


@pytest.mark.asyncio
async def test_registry_rejects_rebinding_a_different_release() -> None:
    first = MetricAuthority()
    assert first.release is not None
    other_release = replace(first.release, release_id=str(UUID(int=99)))
    registry = ActiveReleaseRegistry()
    registry.bind(first.release)
    assert registry.active_release() is first.release
    with pytest.raises(ActiveReleaseBoundError):
        registry.bind(other_release)


@pytest.mark.asyncio
async def test_registry_rejects_same_id_with_a_different_version() -> None:
    first = MetricAuthority()
    assert first.release is not None
    changed = replace(first.release, version=first.release.version + 1)
    assert changed.release_id == first.release.release_id
    registry = ActiveReleaseRegistry()
    registry.bind(first.release)
    with pytest.raises(ActiveReleaseBoundError):
        registry.bind(changed)
    assert registry.active_release() is first.release


@pytest.mark.asyncio
async def test_registry_rejects_same_id_with_a_different_checksum() -> None:
    first = MetricAuthority()
    assert first.release is not None
    changed = replace(first.release, checksum="f" * 64)
    assert changed.release_id == first.release.release_id
    assert changed.checksum != first.release.checksum
    registry = ActiveReleaseRegistry()
    registry.bind(first.release)
    with pytest.raises(ActiveReleaseBoundError):
        registry.bind(changed)
    assert registry.active_release() is first.release


def test_registry_bind_of_an_identical_triple_is_idempotent() -> None:
    authority = MetricAuthority()
    assert authority.release is not None
    registry = ActiveReleaseRegistry()
    registry.bind(authority.release)
    registry.bind(authority.release)
    assert registry.active_release() is authority.release


@pytest.mark.asyncio
async def test_registry_rejects_a_same_triple_clone_object() -> None:
    authority = MetricAuthority()
    assert authority.release is not None
    # Same (release_id, version, checksum) triple, DISTINCT object instance.
    clone = replace(authority.release)
    assert clone is not authority.release
    assert (clone.release_id, clone.version, clone.checksum) == (
        authority.release.release_id,
        authority.release.version,
        authority.release.checksum,
    )
    registry = ActiveReleaseRegistry()
    registry.bind(authority.release)
    with pytest.raises(ActiveReleaseBoundError):
        registry.bind(clone)
    # The served object is immutable: the clone never replaced the first one.
    assert registry.active_release() is authority.release


@pytest.mark.asyncio
async def test_plan_is_bound_to_the_context_release_not_the_live_pointer() -> None:
    authority = MetricAuthority()
    assert authority.release is not None
    # The fixture publishes every release under one id, so forge a genuinely
    # different active release to prove the plan is bound to the context one.
    other_release = replace(authority.release, release_id=str(UUID(int=99)))
    other_context = authority.context.model_copy(
        update={"semantic_release_id": UUID(other_release.release_id)},
    )
    resolver, registry = _live_resolver(authority)
    context = await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )
    assert context.semantic_release_id == UUID(authority.release.release_id)

    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    plan = await provider.propose(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        context=context,
        identity=_identity(),
    )
    assert plan.metric_keys == (METRIC_ASSET,)
    assert "metric.complaint_backlog_count" not in plan.metric_keys

    with pytest.raises(QueryPlanProposalError, match="context_release_mismatch"):
        await provider.propose(
            question=f"{DISPLAY_NAME} time=2024-02-29",
            context=other_context,
            identity=_identity(),
        )


@pytest.mark.asyncio
async def test_release_scoped_provider_returns_only_release_document_ids() -> None:
    authority = MetricAuthority()
    assert authority.release is not None
    registry = ActiveReleaseRegistry()
    provider = ReleaseScopedPolicyEvidenceProvider(
        authority.read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        None,
        registry=registry,
    )
    scoped = await provider.retrieve_permitted(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        identity=_identity(),
    )
    release_ids = {document.document_id for document in authority.release.documents}
    assert {item.evidence_id for item in scoped.lexical} <= release_ids
    assert scoped.resolution_status == "resolved"
    metric_evidence = next(
        item for item in scoped.lexical if item.evidence_id == METRIC_ASSET
    )
    assert metric_evidence.metadata["domain"] == "complaint"
    assert metric_evidence.metadata["asset_type"] == "metric"
    relation_evidence = next(
        item
        for item in scoped.lexical
        if item.evidence_id == authority.binding.relation_asset_id
    )
    assert relation_evidence.metadata["asset_type"] == "relation"
    assert relation_evidence.metadata["relation_name"] == "ai_views.complaint_orders"


@pytest.mark.asyncio
async def test_evidence_never_lies_outside_the_permitted_allow_list() -> None:
    authority = MetricAuthority()
    permitted = frozenset({METRIC_ASSET})  # relation deliberately NOT permitted
    registry = ActiveReleaseRegistry()
    provider = ReleaseScopedPolicyEvidenceProvider(
        authority.read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        permitted,
        registry=registry,
    )
    scoped = await provider.retrieve_permitted(
        question=DISPLAY_NAME,
        identity=_identity(),
    )
    returned = {
        item.evidence_id for source in (scoped.lexical, scoped.graph) for item in source
    }
    assert returned == {METRIC_ASSET}
    assert authority.binding.relation_asset_id not in returned


@pytest.mark.asyncio
async def test_identity_without_metric_permission_gets_no_evidence() -> None:
    authority = MetricAuthority()
    registry = ActiveReleaseRegistry()
    provider = ReleaseScopedPolicyEvidenceProvider(
        authority.read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        None,
        registry=registry,
    )
    scoped = await provider.retrieve_permitted(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        identity=_identity(frozenset({"metrics:read"})),
    )
    assert scoped.resolution_status == "incomplete"
    assert scoped.unresolved_slots == ("metric",)
    assert scoped.lexical == ()
    assert METRIC_ASSET not in {item.evidence_id for item in scoped.graph}


# --------------------------------------------------------------------------- #
# Engine exit assertions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_engine_exit_ea1_zero_model_fast_path_through_gateway_runner() -> None:
    rows: list[dict[str, Any]] = [{"value": 7}]
    live = await _engine(MetricAuthority(), rows)
    result = await _run(live, f"{DISPLAY_NAME} time=2024-02-29")

    assert result["route_record"]["route"] == "fast"
    assert result["execution_record"]["status"] == "succeeded"
    assert result["budget_record"]["usage"]["model_calls"] == 0
    assert result["budget_record"]["usage"]["sql_executions"] == 1
    assert result["budget_record"]["usage"]["sql_candidates"] == 1
    assert result["model_receipt"] is None
    assert live.provider.calls == 0
    assert live.clock.calls == 1
    live.gateway_execute.assert_awaited_once()
    receipts = result["execution_record"]["step_receipts"]
    fetch_receipt = next(
        item for item in receipts if item["step_id"] == "fetch_metrics"
    )
    assert fetch_receipt["rowset_sha256"] == rowset_sha256(rows)
    assert result["query_plan"]["metric_keys"] == [METRIC_ASSET]
    assert result["query_plan"]["time_range"] == {
        "start": "2024-02-29",
        "end": "2024-02-29",
        "timezone": "Asia/Shanghai",
    }


@pytest.mark.asyncio
async def test_engine_exit_ea2_unknown_metric_fails_closed() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(live, "metric=metric.unknown time=2024-02-29")

    assert result["stop_reason"] == "query_plan_proposal_failed"
    assert _trace_names(result) == [
        "received",
        "context_compiled",
        "query_plan_proposal_failed",
    ]
    assert result["budget_record"] is None
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


@pytest.mark.asyncio
async def test_engine_exit_ea3_ambiguous_metric_fails_closed() -> None:
    authority = _authority_with_metric_document(MetricAuthority(), _second_metric_document())
    live = await _engine(authority)
    result = await _run(live, f"{DISPLAY_NAME} time=2024-02-29")

    assert result["stop_reason"] == "query_plan_proposal_failed"
    assert _trace_names(result) == [
        "received",
        "context_compiled",
        "query_plan_proposal_failed",
    ]
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


@pytest.mark.asyncio
async def test_engine_exit_ea4_missing_time_clarifies() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(live, "metric=complaint_in_transit_count")

    assert result["stop_reason"] == "query_plan_clarification_required"
    assert "time" in result["query_plan"]["unresolved_slots"]
    assert "query_plan_validated" in _trace_names(result)
    assert "typed_plan_executed" not in _trace_names(result)
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


@pytest.mark.asyncio
async def test_engine_exit_ea5_missing_permission_evidence_is_absent() -> None:
    # The identity prefilter yields NO evidence for an identity missing the
    # metric permission, so the metric cannot appear in any ContextBundle; the
    # run is terminal before execution with zero SQL and zero model calls.
    authority = MetricAuthority()
    assert authority.release is not None
    restricted = authority.metric.model_copy(
        update={"required_permissions": ("metrics:restricted",)},
    )
    authority.release = replace(
        authority.release,
        documents=tuple(
            replace(
                document,
                metadata={
                    **document.metadata,
                    "execution_contract": restricted.model_dump_json(),
                },
            )
            if document.document_id == METRIC_ASSET
            else document
            for document in authority.release.documents
        ),
    )
    live = await _engine(authority)
    result = await _run(live, f"{DISPLAY_NAME} time=2024-02-29")

    assert result["stop_reason"] == "context_compilation_failed"
    assert result["context_bundle"] is None
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


class _ModelBackedQueryPlanProvider:
    is_deterministic = False

    async def propose(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> QueryPlan:
        raise AssertionError("model-backed provider must never be proposed")


@pytest.mark.asyncio
async def test_engine_exit_ea6_model_backed_provider_is_rejected() -> None:
    authority = MetricAuthority()
    resolver, _ = _live_resolver(authority)
    provider = _CountingProvider()

    with pytest.raises(ValueError, match="deterministic and zero-model") as excinfo:
        create_v2_engine(
            checkpointer=MemorySaver(),
            model_gateway=_model_gateway(provider),
            context_resolver=resolver,
            query_plan_provider=_ModelBackedQueryPlanProvider(),
            plan_executor=metric_plan_executor(
                authority.compiler(),
                QueryGateway(AsyncMock(), schema="ai_views"),
            ),
        )

    assert "deterministic and zero-model" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clause",
    ["area", "area=999", "dim=area", "area=1,2"],
)
async def test_engine_refuses_an_organization_scope_clause_with_zero_sql_model(
    clause: str,
) -> None:
    # The RELATIONSHIP contract really supports an organization dimension (the
    # ratio fixture reproduced R12), so this tests the authorization protection,
    # not a metric that merely lacks the dimension.
    authority = MetricAuthority(ratio_contract())
    assert "area" in authority.metric.supported_dimensions
    registry = ActiveReleaseRegistry()
    scoped = ReleaseScopedPolicyEvidenceProvider(
        authority.read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        None,
        registry=registry,
    )
    compiler = authority.compiler()
    compiled = await compiler.compile(
        authority.plan(),
        authority.context,
    )
    # Positive control: the authorization-shaped predicate absent here.
    assert "filter_" not in compiled.params
    assert "area_id" not in compiled.sql
    assert scoped is not None

    live = await _engine(authority)
    question = (
        "metric=complaint_first_response_rate time=2024-02-29 intent=ranking " + clause
    )
    result = await _run(live, question)

    # R12 fail-open: the organization dimension/predicate must never be planned
    # while the authorization seam is absent, so this stops at proposal time.
    assert result["stop_reason"] == "query_plan_proposal_failed"
    assert result["query_plan"] is None
    assert result["query_plan_validation"] is None
    live.gateway_execute.assert_not_awaited()  # zero SQL executions
    assert live.gateway_execute.await_count == 0
    assert live.provider.calls == 0  # zero model calls
    assert result["budget_record"] is None


# --------------------------------------------------------------------------- #
# Time boundary tests (injected fixed clock)
# --------------------------------------------------------------------------- #


def _boundary(text: str, instant: datetime) -> TimeRange | UnresolvedTime:
    return resolve_time_expression(text, clock=lambda: instant)


def test_boundary_a_today_across_utc_shanghai_date_boundary() -> None:
    result = _boundary("today", datetime(2026, 9, 19, 16, 30, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))


def test_boundary_b_this_week_on_a_monday_is_that_day() -> None:
    monday = date(2026, 9, 21)
    assert monday.weekday() == 0
    result = _boundary("this week", datetime(2026, 9, 21, 1, 0, tzinfo=UTC))
    assert result == TimeRange(start=monday, end=monday)


def test_boundary_c_this_week_on_a_sunday_spans_monday_through_today() -> None:
    sunday = date(2026, 9, 27)
    assert sunday.weekday() == 6
    result = _boundary("this week", datetime(2026, 9, 27, 3, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 9, 21), end=sunday)


def test_boundary_d_last_week_across_a_month_boundary() -> None:
    result = _boundary("last week", datetime(2026, 5, 6, 2, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 4, 27), end=date(2026, 5, 3))


def test_boundary_e_this_month_on_the_first_day_starts_and_ends_today() -> None:
    result = _boundary("this month", datetime(2026, 9, 1, 1, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 9, 1), end=date(2026, 9, 1))


def test_boundary_f_last_month_across_a_year_boundary() -> None:
    result = _boundary("last month", datetime(2026, 1, 15, 1, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2025, 12, 1), end=date(2025, 12, 31))


def test_boundary_g_this_year_on_january_first_starts_and_ends_that_day() -> None:
    result = _boundary("this year", datetime(2026, 1, 1, 1, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 1, 1), end=date(2026, 1, 1))


def test_boundary_h_last_year_across_a_leap_year() -> None:
    result = _boundary("last year", datetime(2025, 3, 1, 1, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2024, 1, 1), end=date(2024, 12, 31))


def test_boundary_i_2024_02_29_is_valid() -> None:
    result = _boundary("2024-02-29", FIXED_NOW)
    assert result == TimeRange(start=date(2024, 2, 29), end=date(2024, 2, 29))


def test_boundary_j_2025_02_29_is_unresolved_not_normalized() -> None:
    result = _boundary("2025-02-29", FIXED_NOW)
    assert isinstance(result, UnresolvedTime)


def test_boundary_k_missing_time_is_unresolved() -> None:
    assert isinstance(resolve_time_expression("", clock=_FixedClock()), UnresolvedTime)


def test_boundary_l_vague_unsupported_expression_is_not_guessed() -> None:
    assert isinstance(_boundary("\u524d\u4e0d\u4e45", FIXED_NOW), UnresolvedTime)
    assert isinstance(_boundary("\u6700\u8fd1", FIXED_NOW), UnresolvedTime)


def test_boundary_m_real_period_does_not_substitute_max_data_date() -> None:
    result = _boundary("this month", FIXED_NOW)
    assert result == TimeRange(start=date(2026, 9, 1), end=date(2026, 9, 20))
    assert isinstance(result, TimeRange)
    assert result.start != date(2024, 1, 1)


def test_boundary_n_clock_is_sampled_exactly_once_per_resolution() -> None:
    clock = _FixedClock()
    resolve_time_expression("this month", clock=clock)
    assert clock.calls == 1


@pytest.mark.parametrize("token", ["0000", "0000-01", "2025-13", "2025-02-29"])
def test_malformed_absolute_dates_resolve_unresolved_without_raising(token: str) -> None:
    assert isinstance(_boundary(token, FIXED_NOW), UnresolvedTime)


def test_chinese_relative_expressions_are_first_class() -> None:
    instant = datetime(2026, 5, 6, 2, 0, tzinfo=UTC)  # Wednesday 2026-05-06 CST
    expected = {
        "\u4eca\u5929": TimeRange(start=date(2026, 5, 6), end=date(2026, 5, 6)),
        "\u6628\u5929": TimeRange(start=date(2026, 5, 5), end=date(2026, 5, 5)),
        "\u672c\u5468": TimeRange(start=date(2026, 5, 4), end=date(2026, 5, 6)),
        "\u4e0a\u5468": TimeRange(start=date(2026, 4, 27), end=date(2026, 5, 3)),
        "\u672c\u6708": TimeRange(start=date(2026, 5, 1), end=date(2026, 5, 6)),
        "\u4e0a\u6708": TimeRange(start=date(2026, 4, 1), end=date(2026, 4, 30)),
        "\u4eca\u5e74": TimeRange(start=date(2026, 1, 1), end=date(2026, 5, 6)),
        "\u53bb\u5e74": TimeRange(start=date(2025, 1, 1), end=date(2025, 12, 31)),
    }
    assert len(expected) == 8
    for token, expected_range in expected.items():
        assert _boundary(token, instant) == expected_range, token


def test_chinese_today_across_the_utc_shanghai_date_boundary() -> None:
    result = _boundary("\u4eca\u5929", datetime(2026, 9, 19, 16, 30, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))


@pytest.mark.asyncio
async def test_boundary_o_supported_zero_model_query_yields_model_calls_zero() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(
        live,
        "metric=complaint_in_transit_count time=2024-02-29 grain=month",
    )

    assert result["route_record"]["route"] == "fast"
    assert result["execution_record"]["status"] == "succeeded"
    assert result["budget_record"]["usage"]["model_calls"] == 0
    assert result["budget_record"]["usage"]["sql_executions"] == 1
    assert live.provider.calls == 0
    assert result["model_receipt"] is None


@pytest.mark.asyncio
async def test_boundary_p_unresolved_time_yields_zero_sql_and_zero_model() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(live, "metric=complaint_in_transit_count time=2025-02-29")

    assert result["stop_reason"] == "query_plan_clarification_required"
    assert "typed_plan_executed" not in _trace_names(result)
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


@pytest.mark.asyncio
async def test_boundary_p_repeated_time_yields_zero_sql_and_zero_model() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(
        live,
        "metric=complaint_in_transit_count time=today time=this_week",
    )

    assert result["stop_reason"] == "query_plan_clarification_required"
    assert "time" in result["query_plan"]["unresolved_slots"]
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


@pytest.mark.asyncio
async def test_boundary_p_vague_time_yields_zero_sql_and_zero_model() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(
        live,
        "metric=complaint_in_transit_count time=\u524d\u4e0d\u4e45",
    )

    assert result["stop_reason"] == "query_plan_clarification_required"
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


def test_underscore_joined_time_periods_are_the_closed_table() -> None:
    result = _boundary("this_week", datetime(2026, 9, 27, 3, 0, tzinfo=UTC))
    assert result == TimeRange(start=date(2026, 9, 21), end=date(2026, 9, 27))
    assert _boundary(
        "last_month",
        datetime(2026, 1, 15, 1, 0, tzinfo=UTC),
    ) == TimeRange(start=date(2025, 12, 1), end=date(2025, 12, 31))


def test_relative_paths_at_the_calendar_edge_resolve_unresolved() -> None:
    year_one = datetime(1, 1, 1, 12, 0, tzinfo=UTC)
    assert isinstance(_boundary("last year", year_one), UnresolvedTime)
    assert isinstance(_boundary("last month", year_one), UnresolvedTime)
    assert isinstance(_boundary("last week", year_one), UnresolvedTime)
    assert isinstance(_boundary("yesterday", year_one), UnresolvedTime)
    # In-range periods still resolve at the calendar edge.
    assert _boundary("today", year_one) == TimeRange(
        start=date(1, 1, 1),
        end=date(1, 1, 1),
    )


@pytest.mark.parametrize(
    "instant, token",
    [
        (datetime(1, 1, 1, 12, 0, tzinfo=UTC), "today"),
        (datetime(9999, 12, 31, 23, 0, tzinfo=UTC), "this year"),
        (datetime(9999, 12, 31, 23, 0, tzinfo=UTC), "last year"),
        (datetime(1, 6, 15, 12, 0, tzinfo=UTC), "this week"),
        (datetime(1, 6, 15, 12, 0, tzinfo=UTC), "this month"),
    ],
)
def test_calendar_edge_clocks_never_raise(instant: datetime, token: str) -> None:
    result = _boundary(token, instant)
    assert isinstance(result, (TimeRange, UnresolvedTime))


def test_invalid_timezone_is_unresolved_not_an_exception() -> None:
    result = resolve_time_expression(
        "today",
        clock=_FixedClock(),
        timezone="Not/AZone",
    )
    assert isinstance(result, UnresolvedTime)


@pytest.mark.asyncio
async def test_unmatched_question_surfaces_the_explicit_flag() -> None:
    authority = MetricAuthority()
    registry = ActiveReleaseRegistry()
    provider = ReleaseScopedPolicyEvidenceProvider(
        authority.read_active,
        {authority.metric.source_ref: authority.binding.relation_asset_id},
        None,
        registry=registry,
    )
    matched = await provider.retrieve_permitted(
        question=f"{DISPLAY_NAME} time=2024-02-29",
        identity=_identity(),
    )
    assert matched.resolution_status == "resolved"
    assert QUESTION_METRIC_UNMATCHED_FLAG not in matched.degradation_flags

    unmatched = await provider.retrieve_permitted(
        question="unrelated question with no metric",
        identity=_identity(),
    )
    # Breadth is unchanged; only the unmatched case now says so explicitly.
    assert unmatched.resolution_status == "resolved"
    assert QUESTION_METRIC_UNMATCHED_FLAG in unmatched.degradation_flags
    assert {item.evidence_id for item in unmatched.lexical} >= {METRIC_ASSET}


@pytest.mark.asyncio
async def test_engine_unmatched_selector_flags_the_context_breadth() -> None:
    live = await _engine(MetricAuthority())
    result = await _run(live, "metric=metric.unknown time=2024-02-29")

    assert result["stop_reason"] == "query_plan_proposal_failed"
    assert QUESTION_METRIC_UNMATCHED_FLAG in result["context_bundle"]["degradation_flags"]
    live.gateway_execute.assert_not_awaited()
    assert live.provider.calls == 0


# --------------------------------------------------------------------------- #
# Sentinel kernel boundary proof (Section 7)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unresolved_time_sentinel_is_clarified_and_never_compiles() -> None:
    authority = MetricAuthority()
    resolver, registry = _live_resolver(authority)
    provider = DeterministicQueryPlanProvider(registry, _FixedClock())
    await resolver.resolve(
        question=DISPLAY_NAME,
        identity=_identity(),
        route_hint="standard",
    )
    plan = await provider.propose(
        question="metric=complaint_in_transit_count time=2025-02-29",
        context=authority.context,
        identity=_identity(),
    )
    # The sentinel is a carrier only; it is not a business default.
    assert plan.time_range == TimeRange(start=date(1970, 1, 1), end=date(1970, 1, 1))
    assert plan.unresolved_slots == ("time",)

    validation = PlanValidator().validate_query_plan(
        plan=plan,
        context=authority.context,
        identity=_identity(),
    )
    assert validation.outcome == "clarify"

    with pytest.raises(PlanValidationError):
        PlanCompiler().compile(
            plan=plan,
            context=authority.context,
            validation=validation,
        )
