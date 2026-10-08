from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import (
    ProductMode,
    QueryPlan,
    RequestContext,
    RouteBudget,
    RoutePolicy,
    RoutingBudgetPolicy,
    TimeRange,
)
from src.nl2sql.infra.llm.gateway import ProviderResponse, ProviderUnavailable
from src.nl2sql.orchestration.engine import V2EngineState, create_v2_engine
from src.nl2sql.orchestration.metric_query import SourceFreshnessRecord
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.ownership import runtime_config
from src.nl2sql.v2 import register_v2_routes
from tests.metric_fixtures import NOW, RELEASE_ID, SNAPSHOT_ID, MetricAuthority, ratio_contract
from tests.unit.test_deterministic_query_path import (
    DISPLAY_NAME,
    _CountingProvider,
    _engine,
    _identity,
    _model_gateway,
)

_QUESTION = f"{DISPLAY_NAME} time=2024-02-29"


class _FreshMetricAuthority(MetricAuthority):
    """Controlled source evidence; no synthetic business-value semantics."""

    async def read_freshness(self, source_id: str) -> SourceFreshnessRecord:
        assert source_id == self.binding.deployment_source_id
        assert self.snapshot is not None
        return SourceFreshnessRecord(
            source_id=source_id,
            status="fresh",
            data_as_of=NOW,
            checkpoint="controlled.local-real-ready.v1",
            release_id=RELEASE_ID,
            snapshot_id=SNAPSHOT_ID,
            snapshot_checksum=self.snapshot.checksum,
        )

    def compiler(self, **overrides: Any):
        options: dict[str, Any] = {
            "read_freshness": self.read_freshness,
            "clock": lambda: NOW,
        }
        options.update(overrides)
        return super().compiler(**options)


async def _run_mode(
    live: Any, mode: ProductMode, *, question: str = _QUESTION
) -> dict[str, Any]:
    thread_id = uuid4()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex,
        requested_mode=mode,
        effective_mode=mode,
    )
    result = await live.engine.ainvoke(
        {
            "messages": [{"role": "user", "content": question}],
            "run_envelope": envelope.model_dump(mode="json"),
        },
        runtime_config(context),
    )
    return dict(result)


def _structured_response(messages: list[dict[str, Any]], *, invented: bool = False) -> str:
    assert len(messages) == 2
    source_message = messages[1]["content"]
    source_json = source_message.split("SOURCE FACTS\n", 1)[1].split(
        "\n\nREQUESTED INTERPRETATION", 1
    )[0]
    source = json.loads(source_json)
    fact = source["facts"][0]
    value = 999 if invented else fact["value"]
    return json.dumps(
        {
            "summary": {
                "text": f"The controlled value is {value}.",
                "fact_ids": [fact["fact_id"]],
            },
            "observations": [],
            "caveats": [
                {"text": "No missing date was invented.", "fact_ids": []}
            ],
        }
    )


@pytest.mark.asyncio
async def test_analyze_is_fetch_first_one_structured_call_and_query_stays_zero_model() -> None:
    live = await _engine(_FreshMetricAuthority(), rows=[{"value": 7}])
    captured: list[list[dict[str, Any]]] = []

    async def complete(**kwargs: Any) -> ProviderResponse:
        messages = kwargs["messages"]
        captured.append(messages)
        assert kwargs["response_schema"] is not None
        return ProviderResponse(
            content=_structured_response(messages),
            model="controlled-analysis",
            usage={"input_tokens": 5, "output_tokens": 5},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")

    assert len(captured) == 1
    assert live.gateway_execute.await_count == 1
    assert analyzed["run_envelope"]["effective_mode"] == "ANALYZE"
    assert [block["type"] for block in analyzed["response_blocks"]] == [
        "text",
        "provenance",
    ]
    text = analyzed["response_blocks"][0]["text"]
    assert "SOURCE FACTS" in text
    assert "MODEL INTERPRETATION" in text
    provenance = analyzed["response_blocks"][1]
    assert provenance["metric_keys"]
    assert provenance["fact_ids"]
    assert provenance["model_provider"] == "synthetic"
    assert provenance["model"] == "controlled-analysis"
    assert provenance["data_as_of"] is not None
    assert provenance["source_ids"]
    assert provenance["source_checkpoints"]
    assert provenance["authority_provenance"]["receipt_step_ids"]
    analysis_budget = analyzed["budget_record"]
    assert analysis_budget["usage"]["model_calls"] == 1
    assert analysis_budget["usage"]["sql_executions"] == 1
    assert analysis_budget["limits"]["max_model_calls"] == 1
    completed = next(
        event
        for event in analyzed["trace_events"]
        if event["name"] == "governed_analysis_completed"
    )
    assert completed["attributes"]["model_calls"] == 1
    assert completed["attributes"]["sql_executions"] == 1

    model_input = json.dumps(captured[0], ensure_ascii=False).lower()
    assert "source facts" in model_input
    assert '"rows"' not in model_input
    assert "authorization_context" not in model_input
    assert "password" not in model_input
    assert "api_key" not in model_input
    assert "select " not in model_input

    queried = await _run_mode(live, "QUERY")
    assert len(captured) == 1
    assert live.gateway_execute.await_count == 2
    assert queried["run_envelope"]["effective_mode"] == "QUERY"
    assert [block["type"] for block in queried["response_blocks"]] == [
        "text",
        "provenance",
    ]
    assert queried["response_blocks"][1]["model_provider"] is None
    assert queried["response_blocks"][1]["model"] is None


@pytest.mark.asyncio
async def test_query_and_analyze_share_business_timezone_watermark_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 00:30 UTC is still the same business date in Asia/Shanghai.  A UTC
    # .date() projection would incorrectly expose the previous day.
    monkeypatch.setattr(
        "tests.unit.test_mode2_analysis_integration.NOW",
        datetime(2026, 9, 18, 0, 30, tzinfo=UTC),
    )
    live = await _engine(_FreshMetricAuthority(), rows=[{"value": 7}])
    async def complete(**kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            content=_structured_response(kwargs["messages"]),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")
    queried = await _run_mode(live, "QUERY")
    assert analyzed["response_blocks"][1]["data_as_of_date"] == "2026-09-18"
    assert queried["response_blocks"][1]["data_as_of_date"] == "2026-09-18"
    assert queried["budget_record"]["usage"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_frozen_recent_sparse_mode2_chain_preserves_exact_period_facts() -> None:
    dates = tuple(
        date(2026, 9, day) for day in (1, 3, 5, 8, 10, 12, 18)
    )
    authority = _FreshMetricAuthority(ratio_contract())
    sparse_plan = authority.plan(
        intent="trend",
        grain="day",
        time_range=TimeRange(start=dates[0], end=dates[-1]),
        available_dates=dates,
    )

    class _StaticContext:
        async def resolve(self, **_: Any):
            return authority.context

    class _SparsePlanProvider:
        is_deterministic = True

        async def propose(self, **_: Any) -> QueryPlan:
            return sparse_plan

    rows = [
        {
            "period": datetime.combine(item, datetime.min.time()),
            "numerator": 3,
            "denominator": 4,
            "value": Decimal("75.00"),
            "status": "success",
        }
        for item in dates
    ]
    live = await _engine(
        authority,
        rows=rows,
        query_plan_provider=_SparsePlanProvider(),
        context_resolver=_StaticContext(),
        fingerprint_plan=sparse_plan,
    )
    captured: list[list[dict[str, Any]]] = []

    async def complete(**kwargs: Any) -> ProviderResponse:
        captured.append(kwargs["messages"])
        return ProviderResponse(
            content=_structured_response(kwargs["messages"]),
            model="controlled-analysis",
            usage={"input_tokens": 5, "output_tokens": 5},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    from src.nl2sql.orchestration.deterministic_query_plan import (
        _FROZEN_REAL_RECENT_PHRASES,
        normalize_frozen_real_question,
    )

    frozen_question = next(iter(_FROZEN_REAL_RECENT_PHRASES))
    assert normalize_frozen_real_question(frozen_question) == (
        "metric=repair_service_archive_rate_overall_day "
        "time=recent_7_available intent=trend"
    )
    result = await _run_mode(live, "ANALYZE", question=frozen_question)
    assert len(captured) == 1
    assert [fact["time_range"]["start"] for fact in result["grounded_answer_artifact"]["facts"]] == [
        item.isoformat() for item in dates
    ]
    public_text = result["response_blocks"][0]["text"]
    for item in dates:
        assert item.isoformat() in public_text
    provenance = result["response_blocks"][1]
    assert len(provenance["fact_ids"]) == len(dates)
    model_input = json.dumps(captured[0], ensure_ascii=False).lower()
    assert '"rows"' not in model_input
    assert "select " not in model_input
    assert "authorization_context" not in model_input
    assert "password" not in model_input
    assert "api_key" not in model_input


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_route", "policy"),
    [
        (
            "standard",
            RoutePolicy(
                version="analysis-standard-test",
                state="calibrated",
                fast_max_risk=0,
                fast_min_confidence=1,
                fast_max_tables=1,
                standard_max_risk=100,
                standard_min_confidence=0,
            ),
        ),
        (
            "deep",
            RoutePolicy(
                version="analysis-deep-test",
                state="calibrated",
                fast_max_risk=0,
                fast_min_confidence=1,
                fast_max_tables=1,
                standard_max_risk=0,
                standard_min_confidence=1,
            ),
        ),
    ],
)
async def test_analyze_fetch_first_is_independent_of_route_name(
    expected_route: str,
    policy: RoutePolicy,
) -> None:
    live = await _engine(
        MetricAuthority(), rows=[{"value": 7}], route_policy=policy
    )

    async def complete(**kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            content=_structured_response(kwargs["messages"]),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")
    assert analyzed["route_record"]["route"] == expected_route
    assert live.gateway_execute.await_count == 1
    assert [block["type"] for block in analyzed["response_blocks"]] == [
        "text",
        "provenance",
    ]
    assert analyzed["budget_record"]["usage"]["model_calls"] == 1
    assert analyzed["budget_record"]["route"] == expected_route


@pytest.mark.asyncio
async def test_no_scalar_governed_fact_stops_before_model_call() -> None:
    live = await _engine(
        MetricAuthority(),
        rows=[{"value": 7}, {"value": 8}],
    )
    analyzed = await _run_mode(live, "ANALYZE")
    assert live.provider.calls == 0
    assert analyzed["stop_reason"] == "metric_result_shape_invalid"


@pytest.mark.asyncio
async def test_analyze_without_typed_pipeline_never_falls_through_to_model() -> None:
    provider = _CountingProvider()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(provider),
    )
    context = RequestContext(
        identity=_identity(),
        thread_id=uuid4(),
        trace_id="analysis-no-typed-pipeline",
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex,
        requested_mode="ANALYZE",
        effective_mode="ANALYZE",
    )
    result = await engine.ainvoke(
        {
            "messages": [{"role": "user", "content": _QUESTION}],
            "run_envelope": envelope.model_dump(mode="json"),
        },
        runtime_config(context),
    )
    assert result["stop_reason"] == "analysis_typed_pipeline_unavailable"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_invented_numeric_claim_is_rejected_without_raw_prose_fallback() -> None:
    live = await _engine(_FreshMetricAuthority(), rows=[{"value": 7}])
    calls = 0

    async def complete(**kwargs: Any) -> ProviderResponse:
        nonlocal calls
        calls += 1
        return ProviderResponse(
            content=_structured_response(kwargs["messages"], invented=True),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")
    assert calls == 1
    assert analyzed["stop_reason"] == "analysis_interpretation_invalid"
    assert analyzed.get("response_blocks", []) == []
    assert "999" not in " ".join(
        str(getattr(message, "content", ""))
        for message in analyzed.get("messages", [])
    )
    assert analyzed["budget_record"]["usage"]["model_calls"] == 1
    assert analyzed["model_receipt"]


@pytest.mark.asyncio
async def test_invalid_structured_json_is_rejected() -> None:
    live = await _engine(MetricAuthority(), rows=[{"value": 7}])

    async def complete(**_: Any) -> ProviderResponse:
        return ProviderResponse(
            content="not-json",
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")
    assert analyzed["stop_reason"] == "analysis_interpretation_invalid"
    assert analyzed["budget_record"]["usage"]["model_calls"] == 1
    assert analyzed["model_receipt"]


@pytest.mark.asyncio
async def test_analysis_provider_failure_is_accounted_after_attempt() -> None:
    live = await _engine(MetricAuthority(), rows=[{"value": 7}])

    async def fail(**_: Any) -> ProviderResponse:
        raise ProviderUnavailable(
            "provider_request_rejected",
            provider="synthetic",
            retryable=False,
            status_code=401,
        )

    live.provider.complete = fail  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")
    budget = analyzed["budget_record"]
    assert budget["usage"]["model_calls"] == 1
    assert budget["error_counts"] == {"provider_request_rejected": 1}
    assert analyzed["stop_reason"] == "provider_request_rejected"
    assert analyzed.get("response_blocks", []) == []


@pytest.mark.asyncio
async def test_analysis_budget_denial_prevents_provider_call() -> None:
    denied_policy = RoutingBudgetPolicy(
        version="analysis-budget-denied-test",
        state="calibrated",
        routes={
            "fast": RouteBudget(
                deadline_ms=4_000,
                max_model_calls=0,
                max_sql_candidates=1,
                max_sql_executions=1,
                max_join_hops=0,
                max_repairs=0,
            ),
            "standard": RouteBudget(
                deadline_ms=10_000,
                max_model_calls=0,
                max_sql_candidates=2,
                max_sql_executions=2,
                max_join_hops=1,
                max_repairs=1,
            ),
            "deep": RouteBudget(
                deadline_ms=30_000,
                max_model_calls=0,
                max_sql_candidates=2,
                max_sql_executions=2,
                max_join_hops=2,
                max_repairs=1,
            ),
        },
        reserve_ms=800,
        max_same_sql=2,
        max_same_error=2,
        max_retryable_provider_errors=1,
    )
    live = await _engine(
        MetricAuthority(),
        rows=[{"value": 7}],
        budget_policy=denied_policy,
    )
    analyzed = await _run_mode(live, "ANALYZE")
    assert live.provider.calls == 0
    assert analyzed["stop_reason"] == "model_call_budget_exhausted"
    assert analyzed["budget_record"]["usage"]["model_calls"] == 0


def test_analysis_checkpoint_state_has_no_request_local_evidence_or_raw_payload() -> None:
    annotations = V2EngineState.__annotations__
    assert "analysis_evidence" not in annotations
    assert "analysis_model_input" not in annotations
    assert "raw_execution_rows" not in annotations
    assert "grounded_answer_artifact" in annotations
    assert "grounded_answer_text" in annotations


@pytest.mark.asyncio
async def test_controlled_local_real_mode2_preserves_safe_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = await _engine(_FreshMetricAuthority(), rows=[{"value": 7}])

    async def complete(**kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            content=_structured_response(kwargs["messages"]),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    analyzed = await _run_mode(live, "ANALYZE")

    class _FrozenEngine:
        async def ainvoke(self, *_: object, **__: object) -> dict[str, Any]:
            return analyzed

    class _LocalRealContainer:
        async def get_engine(self) -> _FrozenEngine:
            return _FrozenEngine()

        def authority_provenance(self) -> str:
            return "local_real_demo"

    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    from src.core.settings import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.state.container = _LocalRealContainer()
    register_v2_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="local-real-user",
            telephone=None,
            roles=["demo"],
            permissions=["*"],
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={
                "requested_mode": "ANALYZE",
                "messages": [{"role": "user", "content": _QUESTION}],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authority_provenance"] == "local_real_demo"
    provenance = next(block for block in body["blocks"] if block["type"] == "provenance")
    assert provenance["data_as_of"] is not None
    assert provenance["metric_keys"]
    assert provenance["source_ids"]
    assert provenance["source_checkpoints"]
    assert provenance["fact_ids"]
    assert provenance["authority_provenance"]["receipt_step_ids"]
    get_settings.cache_clear()
