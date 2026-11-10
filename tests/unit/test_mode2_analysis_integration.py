from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    AuthorizationContext,
    ProductMode,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    RouteBudget,
    RoutePolicy,
    RoutingBudgetPolicy,
    TimeRange,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.infra.llm.gateway import ProviderResponse, ProviderUnavailable
from src.nl2sql.orchestration import engine as engine_module
from src.nl2sql.orchestration.analysis_evidence import (
    AnalysisEvidenceError,
    _grounded_fact_id,
    validate_grounded_answer_checkpoint,
)
from src.nl2sql.orchestration.engine import V2EngineState, create_v2_engine
from src.nl2sql.orchestration.execution import PlanExecutionResult, PlanExecutor
from src.nl2sql.orchestration.grounding import render_grounded_answer
from src.nl2sql.orchestration.metric_query import SourceFreshnessRecord
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.orchestration.typed_runtime import (
    RequestTypedRuntime,
    TypedRuntimeUnavailable,
    build_request_typed_runtime,
)
from src.nl2sql.ownership import runtime_config
from src.nl2sql.v2 import register_v2_routes
from tests.metric_fixtures import NOW, RELEASE_ID, SNAPSHOT_ID, MetricAuthority, ratio_contract
from tests.unit import test_typed_runtime as typed_runtime_fixtures
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


async def _factory_backed_live(
    *,
    checkpointer: MemorySaver | None = None,
    deployment: Any | None = None,
    unavailable_after_first: bool = False,
) -> Any:
    """Build an Engine whose factory calls the real production runtime builder."""

    active_deployment = deployment or typed_runtime_fixtures._deployment()
    gateway = QueryGateway(None, schema="ai_views")  # type: ignore[arg-type]
    execute_calls = 0

    async def execute(sql: str, params: dict[str, object], **kwargs: object) -> QueryReceipt:
        nonlocal execute_calls
        del params, kwargs
        execute_calls += 1
        prepared = gateway.prepare(sql)
        return QueryReceipt(
            accepted=True,
            sql=sql,
            sql_fingerprint=prepared.fingerprint,
            rows=[{"value": 7}],
            row_count=1,
            estimated_cost=1.0,
            estimated_rows=1,
            policy_outcome="allow",
            policy_version=prepared.policy_version,
            max_rows=prepared.max_rows,
            result_columns=("value",),
        )

    gateway.execute = AsyncMock(side_effect=execute)  # type: ignore[method-assign]
    provider = _CountingProvider()
    factory_calls = 0
    analysis_call_count = [0]
    runtimes: list[RequestTypedRuntime] = []

    async def factory(
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
        capabilities: frozenset[str] = frozenset(),
    ) -> RequestTypedRuntime | TypedRuntimeUnavailable:
        nonlocal factory_calls
        factory_calls += 1
        if unavailable_after_first and factory_calls > 1:
            return TypedRuntimeUnavailable(reason="controlled_unavailable")
        runtime = await build_request_typed_runtime(
            views=typed_runtime_fixtures._views(),
            read_active=active_deployment.read_active,
            read_snapshot=active_deployment.read_snapshot,
            gateway=gateway,
            identity=identity,
            authorization=authorization,
            expected_revision=expected_revision,
            capabilities=capabilities,
        )
        if isinstance(runtime, RequestTypedRuntime):
            runtimes.append(runtime)
        return runtime

    engine = create_v2_engine(
        checkpointer=checkpointer or MemorySaver(),
        model_gateway=_model_gateway(provider),
        typed_runtime_factory=factory,
    )
    return SimpleNamespace(
        engine=engine,
        gateway=gateway,
        gateway_execute=gateway.execute,
        provider=provider,
        factory_calls=lambda: factory_calls,
        runtimes=runtimes,
        deployment=active_deployment,
        execute_calls=lambda: execute_calls,
        analysis_calls=lambda: analysis_call_count[0],
        analysis_call_count=analysis_call_count,
    )


async def _run_factory_analyze(live: Any) -> tuple[dict[str, Any], dict[str, object]]:
    async def complete(**kwargs: Any) -> ProviderResponse:
        live.analysis_call_count[0] += 1
        return ProviderResponse(
            content=_structured_response(kwargs["messages"]),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    thread_id = uuid4()
    authorization = typed_runtime_fixtures._authorization()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
        authorization=authorization,
    )
    config = runtime_config(context)
    result = await live.engine.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "metric=complaint_in_transit_count time=2024-02-29",
                }
            ],
            "run_envelope": RunEnvelope(
                run_id=uuid4().hex,
                requested_mode="ANALYZE",
                effective_mode="ANALYZE",
            ).model_dump(mode="json"),
        },
        config,
    )
    return dict(result), config


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
async def test_query_rebinds_fetch_unit_before_grounding_and_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common QUERY path rejects a receipt unit mutation before grounding."""

    live = await _engine(_FreshMetricAuthority(), rows=[{"value": 7}])
    original_execute = PlanExecutor.execute

    async def execute_with_mutated_unit(self: PlanExecutor, **kwargs: Any) -> PlanExecutionResult:
        result = await original_execute(self, **kwargs)
        receipts = tuple(
            receipt.model_copy(update={"unit": "percent"})
            if receipt.kind == "fetch_metric"
            else receipt
            for receipt in result.record.step_receipts
        )
        return PlanExecutionResult(
            record=result.record.model_copy(update={"step_receipts": receipts}),
            outputs=result.outputs,
            output_contracts=result.output_contracts,
            require_semantic_contract=result.require_semantic_contract,
        )

    monkeypatch.setattr(PlanExecutor, "execute", execute_with_mutated_unit)
    queried = await _run_mode(live, "QUERY")
    assert queried["stop_reason"] == "checkpoint_unit_authority_mismatch"
    assert queried.get("grounded_answer_artifact") is None
    assert queried.get("response_blocks", []) == []
    assert all(
        "capture_execution_record_sha256" not in event["attributes"]
        for event in queried.get("trace_events", [])
    )
    assert live.provider.calls == 0


@pytest.mark.asyncio
async def test_typed_execution_fails_before_sql_without_evidence_seal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = await _engine(_FreshMetricAuthority(), rows=[{"value": 7}])
    monkeypatch.setattr(
        engine_module,
        "get_settings",
        lambda: SimpleNamespace(checkpoint_evidence_seal_key=None),
    )
    result = await _run_mode(live, "QUERY")
    assert result["stop_reason"] == "checkpoint_evidence_seal_key_unavailable"
    assert live.gateway_execute.await_count == 0
    assert result.get("grounded_answer_artifact") is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "receipt_output_digest",
        "fact_output_digest",
        "receipt_and_fact_output_digest",
        "receipt_and_fact_rowset_digest",
        "fact_value_and_id",
        "answer_text_and_artifact",
        "whole_record_and_artifact",
        "receipt_and_fact_unit",
    ],
)
async def test_engine_checkpoint_restore_rejects_joint_evidence_mutations(
    mutation: str,
) -> None:
    """Real Engine restoration rejects tampering before another model call."""

    checkpointer = MemorySaver()
    live = await _engine(
        _FreshMetricAuthority(),
        rows=[{"value": 7}],
        checkpointer=checkpointer,
    )
    calls = 0

    async def complete(**kwargs: Any) -> ProviderResponse:
        nonlocal calls
        calls += 1
        return ProviderResponse(
            content=_structured_response(kwargs["messages"]),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )

    live.provider.complete = complete  # type: ignore[method-assign]
    thread_id = uuid4()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    config = runtime_config(context)
    initial = await live.engine.ainvoke(
        {
            "messages": [{"role": "user", "content": _QUESTION}],
            "run_envelope": RunEnvelope(
                run_id=uuid4().hex,
                requested_mode="ANALYZE",
                effective_mode="ANALYZE",
            ).model_dump(mode="json"),
        },
        config,
    )
    assert initial.get("stop_reason") != "checkpoint_evidence_seal_key_unavailable"
    assert calls == 1
    saved = (await live.engine.aget_state(config)).values
    record = copy.deepcopy(saved["execution_record"])
    assert record["evidence_seal"]["version"] == "v1"
    artifact = copy.deepcopy(saved["grounded_answer_artifact"])
    answer_text = str(saved["grounded_answer_text"])
    query_plan = QueryPlan.model_validate(saved["query_plan"])
    fact_index = 0
    fact = AnswerFact.model_validate(artifact["facts"][fact_index])
    receipt_index = next(
        index
        for index, receipt in enumerate(record["step_receipts"])
        if receipt["step_id"] == fact.step_id
    )
    receipt = record["step_receipts"][receipt_index]

    def update_fact(**changes: object) -> None:
        nonlocal fact, answer_text
        fact = fact.model_copy(update=changes)
        fact = fact.model_copy(update={"fact_id": _grounded_fact_id(fact)})
        artifact["facts"][fact_index] = fact.model_dump(mode="json")
        answer_text = render_grounded_answer(query_plan=query_plan, facts=(fact,))

    if mutation == "receipt_output_digest":
        receipt["output_digest"] = "0" * 64
    elif mutation == "fact_output_digest":
        update_fact(output_digest="0" * 64)
        answer_text = str(saved["grounded_answer_text"])
    elif mutation == "receipt_and_fact_output_digest":
        receipt["output_digest"] = "0" * 64
        update_fact(output_digest="0" * 64)
    elif mutation == "receipt_and_fact_rowset_digest":
        receipt["rowset_sha256"] = "0" * 64
        update_fact(rowset_sha256="0" * 64)
    elif mutation == "fact_value_and_id":
        original_text = answer_text
        update_fact(value=808)
        answer_text = original_text
    elif mutation == "answer_text_and_artifact":
        update_fact(value=809)
    elif mutation == "whole_record_and_artifact":
        receipt["output_digest"] = "0" * 64
        receipt["rowset_sha256"] = "0" * 64
        receipt["source_id"] = "tampered.source"
        update_fact(
            value=810,
            output_digest="0" * 64,
            rowset_sha256="0" * 64,
            source_id="tampered.source",
        )
    elif mutation == "receipt_and_fact_unit":
        receipt["unit"] = "percent"
        update_fact(unit="percent")

    await live.engine.aupdate_state(
        config,
        {
            "execution_record": record,
            "grounded_answer_artifact": artifact,
            "grounded_answer_text": answer_text,
        },
        as_node="execute",
    )
    restored = await live.engine.ainvoke(None, config)
    assert restored.get("stop_reason") in {
        "checkpoint_evidence_seal_invalid",
        "checkpoint_unit_authority_mismatch",
        "analysis_grounded_answer_mismatch",
    }
    assert restored.get("response_blocks", []) == []
    assert calls == 1


@pytest.mark.asyncio
async def test_factory_backed_checkpoint_restoration_rebuilds_runtime_without_execution(
) -> None:
    """Resume analysis through a fresh scope and the production runtime factory."""

    checkpointer = MemorySaver()
    live = await _factory_backed_live(checkpointer=checkpointer)
    initial, config = await _run_factory_analyze(live)
    assert initial.get("stop_reason") is None

    assert live.factory_calls() == 1
    assert live.analysis_calls() == 1
    assert len(live.runtimes) == 1
    assert isinstance(live.runtimes[0], RequestTypedRuntime)
    assert live.gateway.execute.await_count == 1

    history = [snapshot async for snapshot in live.engine.aget_state_history(config)]
    execution_checkpoint = next(
        snapshot for snapshot in history if "analysis" in snapshot.next
    )
    saved = execution_checkpoint.values
    assert saved["execution_record"] is not None
    assert saved["execution_record"]["evidence_seal"]["version"] == "v1"
    assert not any(isinstance(value, RequestTypedRuntime) for value in saved.values())
    resume_config = copy.deepcopy(config)
    resume_config["configurable"].update(
        execution_checkpoint.config["configurable"]
    )
    resume_config["configurable"]["authorization_context"] = AuthorizationContext(
        authorization_revision="rev-currently-supplied",
        agent_enabled=True,
        scope_level="city_company",
        allowed_scope_ids=("1",),
    ).model_dump(mode="json")
    restored = await live.engine.ainvoke(None, resume_config)

    assert restored.get("stop_reason") is None
    assert live.analysis_calls() == 2
    assert live.factory_calls() == 2
    assert len(live.runtimes) == 2
    assert live.runtimes[0] is not live.runtimes[1]
    assert live.runtimes[1].authorization.authorization_revision == "rev-p4-s1b"
    assert live.gateway.execute.await_count == 1
    assert live.execute_calls() == 1
    assert [block["type"] for block in restored["response_blocks"]] == [
        "text",
        "provenance",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [
        ("release", "checkpoint_runtime_release_mismatch"),
        ("snapshot", "checkpoint_runtime_snapshot_mismatch"),
        ("release_snapshot_checksum", "checkpoint_runtime_unavailable"),
    ],
)
async def test_factory_backed_checkpoint_restore_rejects_release_or_snapshot_drift(
    drift: str,
    expected_code: str,
) -> None:
    live = await _factory_backed_live()
    initial, config = await _run_factory_analyze(live)
    assert initial.get("stop_reason") is None
    saved = (await live.engine.aget_state(config)).values
    await live.engine.aupdate_state(
        config,
        {"execution_record": copy.deepcopy(saved["execution_record"])},
        as_node="execute",
    )

    deployment = live.deployment
    release = deployment.release
    snapshot = deployment.snapshot
    if drift == "release":
        deployment.release = replace(
            release,
            release_id="99999999-9999-9999-9999-999999999999",
        )
    elif drift == "snapshot":
        changed_snapshot = replace(
            snapshot,
            snapshot_id="33333333-3333-3333-3333-333333333333",
        )

        async def read_changed_snapshot(snapshot_id: str) -> Any:
            assert snapshot_id == deployment.release.schema_snapshot_id
            return changed_snapshot

        # The changed release still advertises the old snapshot id, so a
        # separate release copy binds it to the new validated snapshot.
        deployment.release = replace(
            release,
            schema_snapshot_id=changed_snapshot.snapshot_id,
            schema_snapshot_checksum=changed_snapshot.checksum,
        )
        deployment.read_snapshot = read_changed_snapshot
    else:
        deployment.release = replace(
            release,
            schema_snapshot_checksum="f" * 64,
        )

    restored = await live.engine.ainvoke(None, config)
    assert restored.get("stop_reason") == expected_code
    assert restored.get("response_blocks", []) == []
    assert live.gateway.execute.await_count == 1
    assert live.factory_calls() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("authorization_missing", "checkpoint_runtime_authorization_missing"),
        ("authorization_malformed", "checkpoint_runtime_authorization_missing"),
        ("revision_missing", "checkpoint_runtime_authorization_mismatch"),
        ("revision_mismatch", "checkpoint_runtime_authorization_mismatch"),
        ("owner_mismatch", "checkpoint_runtime_owner_mismatch"),
        ("identity_mismatch", "checkpoint_runtime_owner_mismatch"),
        ("factory_unavailable", "checkpoint_runtime_unavailable"),
    ],
)
async def test_factory_backed_checkpoint_restore_rejects_authority_drift(
    mutation: str,
    expected_code: str,
) -> None:
    live = await _factory_backed_live(unavailable_after_first=mutation == "factory_unavailable")
    initial, config = await _run_factory_analyze(live)
    assert initial.get("stop_reason") is None
    saved = (await live.engine.aget_state(config)).values
    changes: dict[str, object] = {}
    if mutation == "authorization_missing":
        changes["authorization_context"] = None
    elif mutation == "authorization_malformed":
        changes["authorization_context"] = {"authorization_revision": "bad"}
    elif mutation == "revision_missing":
        changes["authorization_revision"] = None
    elif mutation == "revision_mismatch":
        changes["authorization_revision"] = "revision-other"
    elif mutation == "owner_mismatch":
        changes["run_owner_user_id"] = "another-user"
    await live.engine.aupdate_state(
        config,
        {
            **changes,
            "execution_record": copy.deepcopy(saved["execution_record"]),
        },
        as_node="execute",
    )

    resume_config = config
    if mutation == "identity_mismatch":
        resume_config = copy.deepcopy(config)
        resume_config["configurable"]["request_identity"]["user_id"] = "foreign-user"
    restored = await live.engine.ainvoke(None, resume_config)

    assert restored.get("stop_reason") == expected_code
    assert restored.get("response_blocks", []) == []
    assert live.gateway.execute.await_count == 1
    assert live.analysis_calls() == 1
    if mutation in {"owner_mismatch", "identity_mismatch"}:
        assert live.factory_calls() == 1
    elif mutation == "factory_unavailable":
        assert live.factory_calls() == 2
    else:
        assert live.factory_calls() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "receipt_output_digest",
        "fact_output_digest",
        "receipt_and_fact_output_digest",
        "receipt_and_fact_rowset_digest",
        "fact_value_and_id",
        "answer_text_and_artifact",
        "whole_record_and_artifact",
        "receipt_and_fact_unit",
    ],
)
async def test_factory_backed_checkpoint_restore_rejects_joint_evidence_mutations(
    mutation: str,
) -> None:
    live = await _factory_backed_live()
    initial, config = await _run_factory_analyze(live)
    assert initial.get("stop_reason") is None
    saved = (await live.engine.aget_state(config)).values
    record = copy.deepcopy(saved["execution_record"])
    artifact = copy.deepcopy(saved["grounded_answer_artifact"])
    answer_text = str(saved["grounded_answer_text"])
    query_plan = QueryPlan.model_validate(saved["query_plan"])
    fact_index = 0
    fact = AnswerFact.model_validate(artifact["facts"][fact_index])
    receipt_index = next(
        index
        for index, receipt in enumerate(record["step_receipts"])
        if receipt["step_id"] == fact.step_id
    )
    receipt = record["step_receipts"][receipt_index]

    def update_fact(**changes: object) -> None:
        nonlocal fact, answer_text
        fact = fact.model_copy(update=changes)
        fact = fact.model_copy(update={"fact_id": _grounded_fact_id(fact)})
        artifact["facts"][fact_index] = fact.model_dump(mode="json")
        answer_text = render_grounded_answer(query_plan=query_plan, facts=(fact,))

    if mutation == "receipt_output_digest":
        receipt["output_digest"] = "0" * 64
    elif mutation == "fact_output_digest":
        update_fact(output_digest="0" * 64)
        answer_text = str(saved["grounded_answer_text"])
    elif mutation == "receipt_and_fact_output_digest":
        receipt["output_digest"] = "0" * 64
        update_fact(output_digest="0" * 64)
    elif mutation == "receipt_and_fact_rowset_digest":
        receipt["rowset_sha256"] = "0" * 64
        update_fact(rowset_sha256="0" * 64)
    elif mutation == "fact_value_and_id":
        original_text = answer_text
        update_fact(value=808)
        answer_text = original_text
    elif mutation == "answer_text_and_artifact":
        update_fact(value=809)
    elif mutation == "whole_record_and_artifact":
        receipt["output_digest"] = "0" * 64
        receipt["rowset_sha256"] = "0" * 64
        receipt["source_id"] = "tampered.source"
        update_fact(
            value=810,
            output_digest="0" * 64,
            rowset_sha256="0" * 64,
            source_id="tampered.source",
        )
    elif mutation == "receipt_and_fact_unit":
        receipt["unit"] = "percent"
        update_fact(unit="percent")

    await live.engine.aupdate_state(
        config,
        {
            "execution_record": record,
            "grounded_answer_artifact": artifact,
            "grounded_answer_text": answer_text,
        },
        as_node="execute",
    )
    restored = await live.engine.ainvoke(None, config)

    assert restored.get("stop_reason") in {
        "checkpoint_evidence_seal_invalid",
        "checkpoint_unit_authority_mismatch",
        "analysis_grounded_answer_mismatch",
    }
    assert restored.get("response_blocks", []) == []
    assert live.gateway.execute.await_count == 1
    assert live.execute_calls() == 1
    assert live.analysis_calls() == 1
    assert live.factory_calls() == 2

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


def test_analyze_checkpoint_answer_text_mismatch_fails_closed() -> None:
    authority = MetricAuthority()
    query_plan = authority.plan()
    with pytest.raises(AnalysisEvidenceError, match="analysis_grounded_answer_mismatch"):
        validate_grounded_answer_checkpoint(
            query_plan, AnswerArtifact(), "tampered checkpoint text"
        )


def test_analyze_checkpoint_artifact_tamper_fails_and_unchanged_artifact_passes() -> None:
    query_plan = MetricAuthority().plan()
    fact = AnswerFact(
        fact_id="a" * 64,
        step_id="fetch_archive_rate",
        metric_key="controlled.metric",
        status="grounded",
        value=7,
    )
    artifact = AnswerArtifact(facts=(fact,))
    valid = validate_grounded_answer_checkpoint(
        query_plan, artifact, "controlled.metric: 7"
    )
    assert valid.facts == (fact,)
    tampered = artifact.model_copy(
        update={"facts": (fact.model_copy(update={"value": 8}),)}
    )
    with pytest.raises(AnalysisEvidenceError, match="analysis_grounded_answer_mismatch"):
        validate_grounded_answer_checkpoint(query_plan, tampered, valid.answer_text)


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
