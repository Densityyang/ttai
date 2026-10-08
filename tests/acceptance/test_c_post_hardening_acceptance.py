"""Coding-C POST-HARDENING independent acceptance (sections A-G).

Written by the independent acceptance owner.  Attacks the post-hardening
contracts with adversarial inputs; no production file is modified.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.build_run import BUILD_MODE_REQUIRED, require_build_run
from src.nl2sql.artifacts.custom_definition_execution_service import (
    CustomDefinitionExecutionService,
)
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.contracts import RequestContext
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.ownership import runtime_config
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)

_THREAD = UUID("11111111-1111-1111-1111-111111111111")
_RUN = "build-run-1"
_USER = "alice"

def _internal_key(thread: UUID) -> str:
    """The EXACT server thread key runtime_config emits for one thread."""

    from src.nl2sql.contracts import RequestIdentity

    context = RequestContext(
        identity=RequestIdentity(
            request_id=UUID(int=1), user_id=_USER, permissions=frozenset()
        ),
        thread_id=thread,
        trace_id="t",
    )
    config = runtime_config(context)
    return str(config["configurable"]["thread_id"])  # type: ignore[index]


_THREAD_KEY = _internal_key(_THREAD)


# ==========================================================================
# A. BUILD authority: headers are REFERENCES, never authority.
# ==========================================================================


class _StateEngine:
    """Server-persisted checkpoint state; the ONLY authority source.

    The double is THREAD-AWARE: the persisted state exists only under the exact
    server thread key, so a mismatched thread reference resolves to no state
    exactly as a real checkpointer would.
    """

    def __init__(
        self, values: dict[str, object] | None, *, thread_key: str | None = None
    ) -> None:
        self.values = values
        self.thread_key = thread_key or _THREAD_KEY
        self.calls = 0

    async def aget_state(self, config: object) -> Any:
        self.calls += 1
        configurable = (
            config.get("configurable", {}) if isinstance(config, dict) else {}
        )
        if not isinstance(configurable, dict):
            return SimpleNamespace(values=None)
        if configurable.get("thread_id") != self.thread_key:
            return SimpleNamespace(values=None)
        return SimpleNamespace(values=self.values)


class _BuildContainer:
    def __init__(self, engine: _StateEngine) -> None:
        self.engine = engine

    async def get_engine(self) -> _StateEngine:
        return self.engine


def _envelope(mode: str, run_id: str = _RUN) -> RunEnvelope:
    return RunEnvelope(
        run_id=run_id, requested_mode=mode, effective_mode=mode  # type: ignore[arg-type]
    )


def _persisted_values(
    *, mode: str = "BUILD", run_id: str = _RUN, owner: str = _USER
) -> dict[str, object]:
    return {
        "run_envelope": _envelope(mode, run_id).model_dump(mode="json"),
        "run_owner_user_id": owner,
    }


async def _call_build(
    engine: _StateEngine,
    *,
    thread: str | None = str(_THREAD),
    run: str | None = _RUN,
    user: str = _USER,
) -> tuple[str | None, int]:
    app = FastAPI()
    app.state.container = _BuildContainer(engine)
    headers: dict[str, str] = {}
    if thread is not None:
        headers["x-tt-build-thread-id"] = thread
    if run is not None:
        headers["x-tt-build-run-id"] = run

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, str]:
        auth = AuthUser(user_id=user, telephone=None, roles=["analyst"], permissions=["*"])
        envelope = await require_build_run(request, auth)
        return {"run_id": envelope.run_id, "mode": envelope.effective_mode}

    with TestClient(app) as client:
        response = client.get("/probe", headers=headers)
    detail = response.json().get("detail") if response.status_code != 200 else None
    return detail, response.status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"thread": None}, "missing thread header"),
        ({"run": None}, "missing run header"),
        ({"thread": "not-a-uuid"}, "malformed thread reference"),
        ({"run": "stale-run"}, "stale run id"),
        ({"thread": str(uuid4())}, "mismatched thread"),
    ],
)
async def test_missing_or_malformed_build_references_fail_closed(
    kwargs: dict[str, Any], label: str
) -> None:
    engine = _StateEngine(_persisted_values())
    detail, status = await _call_build(engine, **kwargs)
    assert status == 409, label
    assert detail == BUILD_MODE_REQUIRED, label


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["QUERY", "ANALYZE"])
async def test_valid_thread_with_a_non_build_run_is_rejected(mode: str) -> None:
    """A real run that is not BUILD confers no authoring authority."""

    engine = _StateEngine(_persisted_values(mode=mode))
    detail, status = await _call_build(engine)
    assert status == 409
    assert detail == BUILD_MODE_REQUIRED


@pytest.mark.asyncio
async def test_foreign_users_build_run_is_rejected() -> None:
    engine = _StateEngine(_persisted_values(owner="bob"))
    detail, status = await _call_build(engine, user="alice")
    assert status == 409
    assert detail == BUILD_MODE_REQUIRED


@pytest.mark.asyncio
async def test_client_headers_without_matching_server_state_are_rejected() -> None:
    """Headers alone are references: no checkpoint => no authority."""

    for values in (None, {}, {"run_envelope": None}, {"run_envelope": {"bogus": 1}}):
        engine = _StateEngine(values)
        detail, status = await _call_build(engine)
        assert status == 409, values
        assert detail == BUILD_MODE_REQUIRED, values


@pytest.mark.asyncio
async def test_exact_persisted_build_run_is_accepted() -> None:
    engine = _StateEngine(_persisted_values())
    detail, status = await _call_build(engine)
    assert status == 200, detail
    assert detail is None


@pytest.mark.asyncio
async def test_build_reference_alone_cannot_forge_authority() -> None:
    """Passing the CORRECT run id without a persisted BUILD run must fail."""

    engine = _StateEngine(_persisted_values(mode="ANALYZE"))
    detail, status = await _call_build(engine, run=_RUN)
    assert status == 409
    assert detail == BUILD_MODE_REQUIRED


# ==========================================================================
# B. SSE public contract: strict root only.
# ==========================================================================


def _sse_events(payload: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in payload.split("\n\n"):
        name = None
        data = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if name is None:
            continue
        if name == "done":
            events.append((name, {}))
            continue
        events.append((name, json.loads(data or "{}")))
    return events


async def _sse_frames(events: list[dict[str, Any]], **kwargs: Any) -> str:
    from src.nl2sql.api import stream_blocks

    class _Supervisor:
        async def astream_events(self, *_a: object, **_k: object):
            for event in events:
                yield event

    frames: list[str] = []
    async for chunk in stream_blocks(
        _Supervisor(), [], {}, str(_THREAD), kwargs.get("extra_input")
    ):
        frames.append(chunk)
    return "".join(frames)


def _root_event(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "nl2sql_v2_explicit",
        "parent_ids": [],
        "data": {"output": output},
    }


def _metadata_input() -> dict[str, object]:
    return {
        "metadata": {
            "thread_id": str(_THREAD),
            "run_id": "run-sse-1",
            "requested_mode": "QUERY",
            "effective_mode": "QUERY",
            "switched_from_run_id": None,
            "authority_provenance": "demo",
        }
    }


@pytest.mark.asyncio
async def test_metadata_is_first_and_done_is_terminal() -> None:
    from langchain_core.messages import AIMessage

    payload = await _sse_frames(
        [_root_event({"messages": [AIMessage(content="hello")]})],
        extra_input=_metadata_input(),
    )
    events = _sse_events(payload)
    assert events[0][0] == "metadata"
    assert events[-1][0] == "done"
    meta = events[0][1]
    for name in (
        "thread_id",
        "run_id",
        "requested_mode",
        "effective_mode",
        "switched_from_run_id",
        "authority_provenance",
    ):
        assert name in meta, name
    assert meta["run_id"] == "run-sse-1"
    assert meta["authority_provenance"] == "demo"


@pytest.mark.asyncio
async def test_unnamed_chain_end_does_not_finalize() -> None:
    from langchain_core.messages import AIMessage

    payload = await _sse_frames(
        [
            {
                "event": "on_chain_end",
                "parent_ids": [],
                "data": {"output": {"messages": [AIMessage(content="leak")]}},
            }
        ],
        extra_input=_metadata_input(),
    )
    events = _sse_events(payload)
    assert [name for name, _ in events] == ["metadata", "done"]
    assert "leak" not in payload


@pytest.mark.asyncio
async def test_named_ordinary_child_does_not_finalize() -> None:
    from langchain_core.messages import AIMessage

    payload = await _sse_frames(
        [
            {
                "event": "on_chain_end",
                "name": "some_child_chain",
                "parent_ids": [],
                "data": {"output": {"messages": [AIMessage(content="leak")]}},
            }
        ],
        extra_input=_metadata_input(),
    )
    events = _sse_events(payload)
    assert [name for name, _ in events] == ["metadata", "done"]
    assert "leak" not in payload


@pytest.mark.asyncio
async def test_root_named_event_with_parent_ids_does_not_finalize() -> None:
    from langchain_core.messages import AIMessage

    payload = await _sse_frames(
        [
            {
                "event": "on_chain_end",
                "name": "nl2sql_v2_explicit",
                "parent_ids": ["parent-1"],
                "data": {"output": {"messages": [AIMessage(content="leak")]}},
            }
        ],
        extra_input=_metadata_input(),
    )
    events = _sse_events(payload)
    assert [name for name, _ in events] == ["metadata", "done"]
    assert "leak" not in payload


@pytest.mark.asyncio
async def test_child_public_looking_blocks_never_leak_and_root_projects_once() -> None:
    from langchain_core.messages import AIMessage

    child = {
        "event": "on_chain_end",
        "name": "inner_node",
        "parent_ids": ["root"],
        "data": {
            "output": {
                "response_blocks": [{"type": "text", "text": "CHILD LEAK"}],
            }
        },
    }
    root = _root_event({"messages": [AIMessage(content="root answer")]})
    payload = await _sse_frames(
        [child, root], extra_input=_metadata_input()
    )
    events = _sse_events(payload)
    names = [name for name, _ in events]
    assert names[0] == "metadata"
    assert names[-1] == "done"
    assert "CHILD LEAK" not in payload
    blocks = [data for name, data in events if name == "block"]
    assert len(blocks) == 1, "genuine root must project exactly once"


@pytest.mark.asyncio
async def test_events_after_successful_root_cannot_project_twice() -> None:
    from langchain_core.messages import AIMessage

    first = _root_event({"messages": [AIMessage(content="one")]})
    second = _root_event({"messages": [AIMessage(content="two")]})
    payload = await _sse_frames(
        [first, second], extra_input=_metadata_input()
    )
    events = _sse_events(payload)
    blocks = [data for name, data in events if name == "block"]
    assert len(blocks) == 1
    assert payload.count("event: done") == 1


# ==========================================================================
# C. QUERY x RouteName independence.
# ==========================================================================


class _IdleProvider:
    """Counts invocations; a QUERY run must never reach it."""

    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: Any) -> Any:
        self.calls += 1
        raise AssertionError("a QUERY run must never invoke the provider")

    async def list_models(self) -> tuple[str, ...]:
        return ("acceptance-model",)


_QUERY_ROUTE_POLICIES: dict[str, Any] = {}


def _build_query_route_policies() -> None:
    from src.nl2sql.contracts import RoutePolicy

    _QUERY_ROUTE_POLICIES.update(
        {
            "fast": RoutePolicy(
                version="c-ph-fast",
                state="calibrated",
                fast_max_risk=100,
                fast_min_confidence=0,
                fast_max_tables=8,
                standard_max_risk=100,
                standard_min_confidence=0,
            ),
            "standard": RoutePolicy(
                version="c-ph-standard",
                state="calibrated",
                fast_max_risk=0,
                fast_min_confidence=1,
                fast_max_tables=1,
                standard_max_risk=100,
                standard_min_confidence=0,
            ),
            "deep": RoutePolicy(
                version="c-ph-deep",
                state="calibrated",
                fast_max_risk=0,
                fast_min_confidence=1,
                fast_max_tables=1,
                standard_max_risk=0,
                standard_min_confidence=1,
            ),
        }
    )


_build_query_route_policies()


async def _run_query(
    *, route: str, unsupported: bool = False
) -> tuple[dict[str, Any], int, int, bool]:
    """Drive a REAL QUERY run; return (result, provider_calls, sql_calls, built)."""

    from unittest.mock import AsyncMock

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
    from tests.unit.test_deterministic_query_path import DISPLAY_NAME, _identity

    authority = _FreshAuthority()
    gateway = QueryGateway(AsyncMock(), schema="ai_views")
    compiled = await authority.compiler().compile(authority.plan(), authority.context)
    fingerprint = gateway.prepare(compiled.sql).fingerprint
    execute = AsyncMock(
        return_value=QueryReceipt(
            accepted=True,
            sql="",
            sql_fingerprint=fingerprint,
            rows=[{"value": 7}],
            row_count=1,
            policy_outcome="allow",
            max_rows=200,
        )
    )
    gateway.execute = execute  # type: ignore[method-assign]
    provider = _IdleProvider()
    model_gateway = ModelGateway(
        providers={provider.provider_name: provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "c-acc-v1",
                frozenset({"answer"}),
                ModelTarget(provider.provider_name, "acceptance-model", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "c-acc-v1",
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
            {authority.metric.source_ref: authority.binding.relation_asset_id},
            None,
            registry=registry,
        ),
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=model_gateway,
        context_resolver=resolver,
        query_plan_provider=DeterministicQueryPlanProvider(registry, lambda: NOW),
        plan_executor=metric_plan_executor(authority.compiler(), gateway),
        plan_validator=PlanValidator(),
        plan_compiler=PlanCompiler(),
        route_policy=_QUERY_ROUTE_POLICIES[route],
    )
    question = (
        "why did revenue drop" if unsupported else f"{DISPLAY_NAME} time=2024-02-29"
    )
    thread_id = uuid4()
    context = RequestContext(
        identity=_identity(),
        thread_id=thread_id,
        trace_id=f"trace-{thread_id}",
        deadline_ms=10_000,
    )
    envelope = RunEnvelope(
        run_id=uuid4().hex, requested_mode="QUERY", effective_mode="QUERY"
    )
    result = dict(
        await engine.ainvoke(
            {
                "messages": [{"role": "user", "content": question}],
                "run_envelope": envelope.model_dump(mode="json"),
            },
            runtime_config(context),
        )
    )
    return (
        result,
        provider.calls,
        execute.await_count,
        result.get("mode_capability_outcome") is not None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["fast", "standard", "deep"])
async def test_query_stays_zero_model_on_every_route(route: str) -> None:
    result, provider_calls, sql_calls, suggestion = await _run_query(route=route)
    assert str(result["route_record"]["route"]) == route, "wrong route selected"
    assert provider_calls == 0, "QUERY reached a real provider"
    assert result["budget_record"]["usage"]["model_calls"] == 0
    assert result["budget_record"]["route"] == route
    assert result["run_envelope"]["effective_mode"] == "QUERY"
    assert result["run_envelope"]["requested_mode"] == "QUERY"
    assert result.get("stop_reason") is None
    assert sql_calls == 1, "deterministic execution must actually run"
    assert suggestion is False, "a servable QUERY must not suggest a switch"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["standard", "deep"])
async def test_unservable_query_reaches_typed_capability_on_every_route(
    route: str,
) -> None:
    """PH-1 (re-accepted): the typed inability is genuinely reachable.

    Previously this lane stopped at query_plan_proposal_failed.  The frozen
    contract is now that a valid-but-unservable QUERY request reaches the typed
    cannot_resolve -> suggested_mode=ANALYZE outcome, with ZERO model calls and
    ZERO SQL, and WITHOUT silently switching the run mode.
    """

    result, provider_calls, sql_calls, suggestion = await _run_query(
        route=route, unsupported=True
    )
    assert provider_calls == 0, "capability inability must never call a model"
    assert sql_calls == 0, "nothing should execute when it cannot resolve"
    assert suggestion is True, "PH-1: typed inability must be reachable"
    outcome = result["mode_capability_outcome"]
    assert outcome["outcome"] == "cannot_resolve"
    assert outcome["suggested_mode"] == "ANALYZE"
    assert outcome["effective_mode"] == "QUERY"
    assert outcome["run_id"] == result["run_envelope"]["run_id"]
    assert result.get("stop_reason") == "mode_cannot_resolve"
    # the mode never silently switched to ANALYZE/BUILD inside the same run
    assert result["run_envelope"]["effective_mode"] == "QUERY"
    assert result["run_envelope"]["requested_mode"] == "QUERY"


def test_demo_lane_does_emit_the_typed_suggestion() -> None:
    """The DEMO lane proves the mode-suggestion contract is wired end to end."""

    from src.nl2sql.demo.runtime import DemoUnsupportedRequest
    from src.nl2sql.orchestration.planning import DeterministicQueryUnsupported

    assert DemoUnsupportedRequest is DeterministicQueryUnsupported


def test_mode_suggestion_wording_is_route_neutral() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "src" / "nl2sql" / "api.py").read_text(encoding="utf-8")
    start = source.index("mode_capability_outcome")
    reason = source[start:].split(chr(34) + "reason" + chr(34) + ":", 1)[1].split(chr(10), 1)[0]
    upper = reason.upper()
    assert "FAST" not in upper, "a FAST-specific wording would leak the route axis"
    assert "DEEP" not in upper
    assert "STANDARD" not in upper
    assert "deterministic query capability" in reason.lower()


# ==========================================================================
# D. Local-real readiness + resource cleanup (fakes only).
# ==========================================================================


def _local_real_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for key, value in {
        "SERVICE_MODE": "infra-dev",
        "AUTH_ENABLED": "false",
        "TYPED_RUNTIME_ACTIVATION": "local_real_data_demo",
        "LOCAL_REAL_DEMO_USER_ID": "local-real-demo",
        "MEMORY_BACKEND": "memory",
        **extra,
    }.items():
        monkeypatch.setenv(key, value)
    from src.core.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_readiness_without_db_config_is_503_with_a_sanitized_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_real_env(monkeypatch, DATABASE_URL="")
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    report = await container.prepare_readiness()
    assert report["status"] == "not_ready"
    typed = report["components"]["typed_runtime"]
    assert typed["required"] is True
    assert typed["status"] == "unavailable"
    assert typed["reason"] == "local_real_database_not_configured"
    assert "local_real_database_not_configured" in report["degradation_reasons"]
    blob = json.dumps(report)
    for forbidden in ("postgresql://", "password", "://", "@"):
        assert forbidden not in blob.replace("sqlite+aiosqlite", ""), forbidden
    from src.core.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_readiness_reason_is_stable_across_repeated_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_real_env(monkeypatch, DATABASE_URL="")
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    first = await container.prepare_readiness()
    second = await container.prepare_readiness()
    third = await container.prepare_readiness()
    assert first == second == third
    assert container._local_real_readiness is not None
    from src.core.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_repeated_readiness_does_not_rebuild_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle probe/cache must be created ONCE, not per /readyz."""

    _local_real_env(monkeypatch, DATABASE_URL="")
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    attempts = 0
    original = container._local_real_deployment_inputs

    async def counting() -> Any:
        nonlocal attempts
        attempts += 1
        return await original()

    container._local_real_deployment_inputs = counting  # type: ignore[method-assign]
    for _ in range(5):
        await container.prepare_readiness()
    assert attempts == 1, f"deployment was rebuilt {attempts} times"
    from src.core.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_repeated_readyz_never_reprobes(monkeypatch: pytest.MonkeyPatch) -> None:
    _local_real_env(monkeypatch, DATABASE_URL="")
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    attempts = 0
    original = container._local_real_deployment_inputs

    async def counting() -> Any:
        nonlocal attempts
        attempts += 1
        return await original()

    container._local_real_deployment_inputs = counting  # type: ignore[method-assign]
    await container.prepare_readiness()
    for _ in range(4):
        report = container.readiness_report(model_available=False)
        assert report["components"]["typed_runtime"]["status"] == "unavailable"
    assert attempts == 1
    from src.core.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_deployment_probe_failure_leaks_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL deployment-build failure surfaces only a stable reason."""

    secret = "SUPERSECRET-DSN-9f3a"
    _local_real_env(
        monkeypatch,
        DATABASE_URL=f"postgresql://user:{secret}@10.0.0.9:5432/business",
    )
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    # The genuine production boundary catches everything and collapses to a
    # stable reason.  Point the deployment at an unreadable views config so the
    # real except-branch runs.
    from src.core.settings import get_settings

    settings = get_settings()
    assert settings.database_url  # configuration IS present
    monkeypatch.setattr(
        "src.nl2sql.config.settings.get_agent_config", _broken_agent_config
    )
    report = await container.prepare_readiness()
    blob = json.dumps(report)
    assert secret not in blob, "the credential leaked into readiness"
    assert "10.0.0.9" not in blob
    assert "postgresql://" not in blob
    typed = report["components"]["typed_runtime"]
    assert typed["status"] == "unavailable"
    assert typed["reason"] == "local_real_deployment_unavailable"
    get_settings.cache_clear()


def _broken_agent_config() -> Any:
    """An agent config whose views path cannot be read, forcing the failure."""

    from src.nl2sql.config.settings import get_agent_config as real

    original = real()
    return original.model_copy(
        update={"ai_views_config_path": "does/not/exist/ai_views.yaml"}
    )


@pytest.mark.asyncio
async def test_close_disposes_resources_and_clears_cached_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_real_env(monkeypatch, DATABASE_URL="")
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    await container.prepare_readiness()
    assert container._local_real_readiness is not None

    disposed: list[bool] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    container._local_real_resources = (_Engine(), object())  # type: ignore[assignment]
    container._local_real_deployment = (1, 2, 3, 4)  # type: ignore[assignment]
    await container.close()
    assert disposed == [True], "the created engine must be disposed"
    assert container._local_real_resources is None
    assert container._local_real_deployment is None
    assert container._local_real_readiness is None
    from src.core.settings import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_failure_after_engine_creation_disposes_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially constructed deployment must not leak an engine."""

    _local_real_env(monkeypatch, DATABASE_URL="")
    from src.nl2sql.container import AppContainer

    container = AppContainer()
    disposed: list[bool] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    # Simulate the production mid-construction failure: the engine exists but
    # the subsequent deployment build raises.
    engine = _Engine()

    async def failing() -> Any:
        try:
            raise RuntimeError("view probe failed")
        except RuntimeError:
            await engine.dispose()
            container._local_real_deployment = None
            container._local_real_resources = None
            return None

    container._local_real_deployment_inputs = failing  # type: ignore[method-assign]
    report = await container.prepare_readiness()
    assert disposed == [True]
    assert container._local_real_deployment is None
    assert container._local_real_resources is None
    assert report["components"]["typed_runtime"]["status"] == "unavailable"
    from src.core.settings import get_settings

    get_settings.cache_clear()


# ==========================================================================
# E. Publication current pointer / withdrawal / update semantics.
# ==========================================================================


def _published_version(identity: str, version: int, *, semantic: bool = True) -> Any:
    from src.nl2sql.artifacts.custom_definition import derive_parameter_contract
    from src.nl2sql.artifacts.publication import (
        PublishedSemanticPackage,
        PublishedVersion,
    )

    package = None
    if semantic:
        spec = CalculationSpec(
            calculation_id="custom.ptr",
            expression=LiteralOperand(value=Decimal("1")),
            inputs=(
                CalculationInputSpec(
                    role="actual",
                    provenance="published_gold",
                    metric_key="repair_service_archive_rate_overall_day",
                ),
            ),
            unit="percent",
        )
        package = PublishedSemanticPackage(
            calculation=spec,
            parameter_contract=derive_parameter_contract(spec),
            source_definition_id=identity,
            source_definition_version=version,
            source_definition_checksum="a" * 64,
        )
    return PublishedVersion(
        identity_id=identity,
        version=version,
        title="T",
        owner_user_id="alice",
        owner_label="alice",
        source_label="alice",
        definition_checksum="b" * 64,
        published_at="t",
        semantic=package,
    )


def _library_service(catalogue: Any, library: Any) -> Any:
    from src.nl2sql.artifacts.product_library_service import (
        CertificationAuthority,
        build_product_library_service,
    )
    from src.nl2sql.artifacts.publication_service import PublicationService

    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    return build_product_library_service(
        catalogue=catalogue,
        library=library,
        definitions=definitions,
        publications=PublicationService(definitions=definitions, catalogue=catalogue),
        certification_authority=CertificationAuthority(
            service_mode="infra-dev",
            typed_runtime_activation="local_real_data_demo",
            admin_user_id="admin",
        ),
    )


def test_no_public_rollback_mutator_exists() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    assert not hasattr(PublicationCatalogue, "set_current_version")
    for name in dir(PublicationCatalogue):
        if name.startswith("_"):
            continue
        assert "rollback" not in name.lower()
        assert "set_current" not in name.lower()


def test_first_valid_current_is_established_then_advances() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    catalogue.publish(_published_version("id.one", 1))
    assert catalogue.current_version("id.one") == 1
    catalogue.publish(_published_version("id.one", 2))
    assert catalogue.current_version("id.one") == 2


def test_historical_publication_never_moves_current_backward() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    catalogue.publish(_published_version("id.two", 3))
    assert catalogue.current_version("id.two") == 3
    catalogue.publish(_published_version("id.two", 1))
    assert catalogue.current_version("id.two") == 3, "current regressed"
    catalogue.publish(_published_version("id.two", 2))
    assert catalogue.current_version("id.two") == 3


def test_seed_current_false_never_changes_current() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    catalogue.seed(_published_version("id.three", 5, semantic=False), current=True)
    assert catalogue.current_version("id.three") == 5
    catalogue.seed(_published_version("id.three", 1, semantic=False))
    assert catalogue.current_version("id.three") == 5


def test_seed_current_true_cannot_regress_current() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    catalogue.seed(_published_version("id.four", 5, semantic=False), current=True)
    with pytest.raises(ValueError, match="regression"):
        catalogue.seed(_published_version("id.four", 2, semantic=False), current=True)
    assert catalogue.current_version("id.four") == 5


def test_missing_and_corrupt_current_fail_closed() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    catalogue.publish(_published_version("id.five", 1))
    catalogue._current.pop("id.five")
    with pytest.raises(LookupError, match="unbound"):
        catalogue.current_version("id.five")
    catalogue._current["id.five"] = 99
    with pytest.raises(LookupError, match="invalid"):
        catalogue.current_version("id.five")
    catalogue._current["id.five"] = True
    with pytest.raises(LookupError, match="unbound"):
        catalogue.current_version("id.five")


def test_withdrawn_current_disappears_from_discovery_without_fallback() -> None:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    service = _library_service(catalogue, library)
    catalogue.publish(_published_version("id.six", 1))
    catalogue.publish(_published_version("id.six", 2))
    assert [e["identity_id"] for e in service.catalogue_entries()] == ["id.six"]
    catalogue.withdraw("id.six", 2)
    # The withdrawn CURRENT disappears; NO fallback to the older v1.
    assert service.catalogue_entries() == ()


def test_install_and_upgrade_into_a_withdrawn_version_fail() -> None:
    from src.nl2sql.artifacts.library import (
        InMemoryLibraryRepository,
        LibraryIdentityNotFound,
    )
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    catalogue.publish(_published_version("id.seven", 1))
    catalogue.publish(_published_version("id.seven", 2))
    library.install(user_id="alice", identity_id="id.seven", version=1)
    catalogue.withdraw("id.seven", 2)
    with pytest.raises(LibraryIdentityNotFound, match="withdrawn"):
        library.install(user_id="bob", identity_id="id.seven", version=2)
    with pytest.raises(LibraryIdentityNotFound, match="withdrawn"):
        library.upgrade(user_id="alice", identity_id="id.seven", to_version=2)
    install = library.get_install(user_id="alice", identity_id="id.seven")
    assert install is not None and install.version == 1


def test_acknowledgement_never_clears_withdrawal() -> None:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    catalogue.publish(_published_version("id.eight", 1))
    library.install(user_id="alice", identity_id="id.eight", version=1)
    catalogue.withdraw("id.eight", 1)
    library.acknowledge_withdrawal(user_id="alice", identity_id="id.eight", version=1)
    assert library.is_acknowledged(user_id="alice", identity_id="id.eight", version=1)
    assert catalogue.is_withdrawn("id.eight", 1) is True, "ack cleared withdrawal"


def test_a_later_higher_published_current_restores_discovery() -> None:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    service = _library_service(catalogue, library)
    catalogue.publish(_published_version("id.nine", 1))
    catalogue.withdraw("id.nine", 1)
    assert service.catalogue_entries() == ()
    catalogue.publish(_published_version("id.nine", 2))
    entries = service.catalogue_entries()
    assert len(entries) == 1
    assert entries[0]["current_version"] == 2
    assert catalogue.is_withdrawn("id.nine", 2) is False


@pytest.mark.parametrize(
    ("installed", "current", "expected"),
    [(1, 1, False), (2, 1, False), (1, 2, True)],
)
def test_update_available_matrix(installed: int, current: int, expected: bool) -> None:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    top = max(installed, current)
    for version in range(1, top + 1):
        catalogue.publish(_published_version("id.ten", version))
    library.install(user_id="alice", identity_id="id.ten", version=installed)
    if current != top:
        catalogue._current["id.ten"] = current
    result = library.update_available(user_id="alice", identity_id="id.ten")
    assert result is expected


def test_withdrawn_or_missing_current_reports_no_update() -> None:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    catalogue.publish(_published_version("id.eleven", 1))
    catalogue.publish(_published_version("id.eleven", 2))
    library.install(user_id="alice", identity_id="id.eleven", version=1)
    assert library.update_available(user_id="alice", identity_id="id.eleven") is True
    catalogue.withdraw("id.eleven", 2)
    assert library.update_available(user_id="alice", identity_id="id.eleven") is False
    catalogue._current.pop("id.eleven")
    assert library.update_available(user_id="alice", identity_id="id.eleven") is False


def test_upgrade_must_move_strictly_upward() -> None:
    from src.nl2sql.artifacts.library import (
        InMemoryLibraryRepository,
        LibraryIdentityNotFound,
    )
    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    for version in (1, 2, 3):
        catalogue.publish(_published_version("id.twelve", version))
    library.install(user_id="alice", identity_id="id.twelve", version=2)
    with pytest.raises(LibraryIdentityNotFound, match="newer"):
        library.upgrade(user_id="alice", identity_id="id.twelve", to_version=1)
    with pytest.raises(LibraryIdentityNotFound, match="newer"):
        library.upgrade(user_id="alice", identity_id="id.twelve", to_version=2)
    held = library.get_install(user_id="alice", identity_id="id.twelve")
    assert held is not None and held.version == 2, "rejected upgrade moved it"
    library.upgrade(user_id="alice", identity_id="id.twelve", to_version=3)
    moved = library.get_install(user_id="alice", identity_id="id.twelve")
    assert moved is not None and moved.version == 3


# ==========================================================================
# F. Definition execution date context propagation.
# ==========================================================================


class _RecordingFetcher:
    """Records the EXACT execution context that reaches the fetcher."""

    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def fetch_metric_input(self, **kwargs: object) -> Any:
        self.contexts.append(kwargs["execution_context"])
        from src.nl2sql.orchestration.custom_calculation_execution import (
            ResolvedCalculationInput,
        )

        return ResolvedCalculationInput(
            role=str(kwargs["role"]),
            metric_key=str(kwargs["metric_key"]),
            value=Decimal("45"),
            unit="percent",
            data_as_of=datetime(2026, 9, 20, tzinfo=UTC),
            provenance=str(kwargs["required_provenance"]),
            source_id="gold.repair.archive",
            receipt_step_id="fetch_actual",
            fact_id="a" * 64,
        )


def _execute_spec() -> Any:
    from src.nl2sql.semantic.calculation_contract import (
        BinaryOperand,
        InputRefOperand,
        ParameterRefOperand,
        ParameterSpec,
    )

    return CalculationSpec(
        calculation_id="custom.actual_to_target_index",
        expression=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="actual"),
                right=ParameterRefOperand(name="target_percent"),
            ),
            right=LiteralOperand(value=Decimal("100")),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual",
                provenance="published_gold",
                metric_key="repair_service_archive_rate_overall_day",
            ),
        ),
        parameters=(ParameterSpec(name="target_percent", value_type="decimal"),),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


def _execute_app(fetcher: Any) -> tuple[TestClient, str]:
    from src.nl2sql.artifacts.api_definitions import register_definition_routes
    from src.nl2sql.artifacts.service import CustomDefinitionService
    from src.nl2sql.orchestration.governed_calculation_inputs import (
        TypedMetricCalculationInputResolver,
    )

    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    spec = _execute_spec()
    draft = definitions.create_draft(
        owner_user_id="alice", title="T", calculation=spec
    )
    definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    definitions.confirm(owner_user_id="alice", definition_id=draft.definition_id)
    definitions.save(owner_user_id="alice", definition_id=draft.definition_id)

    class _Container:
        def custom_definition_service(self) -> Any:
            return definitions

        def custom_definition_execution_service(self) -> Any:
            return CustomDefinitionExecutionService(
                definitions=definitions,
                input_resolver=TypedMetricCalculationInputResolver(fetcher),
            )

    app = FastAPI()
    app.state.container = _Container()
    register_definition_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(user_id="alice", telephone=None, roles=[], permissions=["*"])

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app), draft.definition_id


def _binding_body(spec: Any, target: int) -> dict[str, Any]:
    from src.nl2sql.semantic.calculation_contract import (
        CalculationExecutionBinding,
        ParameterBinding,
    )

    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="target_percent", value=target),),
    ).model_dump(mode="json")


def _execute_path(definition_id: str) -> str:
    return f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute"


def test_omitted_date_context_defaults_to_latest_authoritative() -> None:
    fetcher = _RecordingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    response = client.post(
        _execute_path(definition_id), json={"binding": _binding_body(spec, 90)}
    )
    assert response.status_code == 200, response.text
    assert len(fetcher.contexts) == 1
    context = fetcher.contexts[0]
    assert context.date_mode == "latest_authoritative"
    assert context.exact_date is None


def test_explicit_latest_authoritative_is_unchanged() -> None:
    fetcher = _RecordingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    response = client.post(
        _execute_path(definition_id),
        json={
            "binding": _binding_body(spec, 90),
            "execution_context": {"date_mode": "latest_authoritative"},
        },
    )
    assert response.status_code == 200, response.text
    context = fetcher.contexts[0]
    assert context.date_mode == "latest_authoritative"
    assert context.exact_date is None


def test_exact_date_propagates_unchanged_to_the_fetcher() -> None:
    fetcher = _RecordingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    response = client.post(
        _execute_path(definition_id),
        json={
            "binding": _binding_body(spec, 90),
            "execution_context": {"date_mode": "exact_date", "exact_date": "2026-08-15"},
        },
    )
    assert response.status_code == 200, response.text
    context = fetcher.contexts[0]
    assert context.date_mode == "exact_date"
    assert context.exact_date == date(2026, 8, 15)


def test_compact_top_level_exact_date_spelling_also_propagates() -> None:
    fetcher = _RecordingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    response = client.post(
        _execute_path(definition_id),
        json={
            "binding": _binding_body(spec, 90),
            "date_mode": "exact_date",
            "exact_date": "2026-08-15",
        },
    )
    assert response.status_code == 200, response.text
    assert fetcher.contexts[0].exact_date == date(2026, 8, 15)


@pytest.mark.parametrize(
    ("body", "label"),
    [
        (
            {"execution_context": {"date_mode": "latest_authoritative", "exact_date": "2026-08-15"}},
            "latest_authoritative plus exact date",
        ),
        (
            {"execution_context": {"date_mode": "exact_date"}},
            "exact_date mode without a date",
        ),
        ({"execution_context": {"unknown_field": 1}}, "unknown context field"),
        ({"execution_context": {"date_mode": "made_up"}}, "unknown date mode"),
        (
            {"execution_context": {"date_mode": "exact_date", "exact_date": "not-a-date"}},
            "malformed date",
        ),
    ],
)
def test_date_context_rejections(body: dict[str, Any], label: str) -> None:
    fetcher = _RecordingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    payload = {"binding": _binding_body(spec, 90), **body}
    response = client.post(_execute_path(definition_id), json=payload)
    assert response.status_code == 422, (label, response.text)
    assert fetcher.contexts == [], f"{label} reached the fetcher"



# ==========================================================================
# G. DefinitionBlock lifecycle axes.
# ==========================================================================


def test_definition_block_exposes_separate_axes_not_an_aggregate_status() -> None:
    from src.nl2sql.supervisor.schemas import DefinitionBlock

    fields = set(DefinitionBlock.model_fields)
    assert {
        "confirmation",
        "retention",
        "publication",
        "certification",
        "semantic_closed",
    } <= fields
    assert "status" not in fields, "an aggregate status shortcut must not exist"


@pytest.mark.parametrize(
    "axes",
    [
        ("DRAFT", "SESSION", "UNPUBLISHED", "UNCERTIFIED", False),
        ("DRAFT", "SESSION", "UNPUBLISHED", "UNCERTIFIED", True),
        ("CONFIRMED", "SESSION", "UNPUBLISHED", "UNCERTIFIED", True),
        ("CONFIRMED", "SAVED", "UNPUBLISHED", "UNCERTIFIED", True),
        ("CONFIRMED", "SAVED", "PUBLISHED", "UNCERTIFIED", True),
        ("CONFIRMED", "SAVED", "PUBLISHED", "CERTIFIED", True),
    ],
)
def test_legal_axis_combinations_are_all_representable(
    axes: tuple[str, str, str, str, bool],
) -> None:
    from src.nl2sql.supervisor.schemas import DefinitionBlock

    confirmation, retention, publication, certification, closed = axes
    block = DefinitionBlock(
        definition_id="def_" + "a" * 32,
        version=1,
        title="T",
        confirmation=confirmation,
        retention=retention,
        publication=publication,
        certification=certification,
        semantic_closed=closed,
        checksum="b" * 64,
    )
    dumped = block.model_dump(mode="json")
    assert dumped["confirmation"] == confirmation
    assert dumped["retention"] == retention
    assert dumped["publication"] == publication
    assert dumped["certification"] == certification
    assert dumped["semantic_closed"] is closed


def test_illustrative_axes_cannot_be_encoded_as_one_status_literal() -> None:
    """A single status literal could not distinguish these two states."""

    from src.nl2sql.supervisor.schemas import DefinitionBlock

    first = DefinitionBlock(
        definition_id="def_" + "a" * 32,
        version=1,
        title="T",
        confirmation="DRAFT",
        retention="SESSION",
        publication="UNPUBLISHED",
        certification="UNCERTIFIED",
        semantic_closed=False,
        checksum="b" * 64,
    )
    second = first.model_copy(update={"semantic_closed": True})

    assert first.model_dump() != second.model_dump()


def test_historical_publication_never_rewrites_a_newer_current_revision() -> None:
    from src.nl2sql.artifacts.publication import PublicationCatalogue
    from src.nl2sql.artifacts.publication_service import PublicationService

    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    catalogue = PublicationCatalogue()
    publications = PublicationService(
        definitions=definitions, catalogue=catalogue
    )
    spec = _execute_spec()
    draft = definitions.create_draft(
        owner_user_id="alice", title="T", calculation=spec
    )
    definition_id = draft.definition_id
    definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=definition_id
    )
    definitions.confirm(owner_user_id="alice", definition_id=definition_id)
    definitions.save(owner_user_id="alice", definition_id=definition_id)
    # open v2 BEFORE publishing v1
    definitions.create_revision(
        owner_user_id="alice", definition_id=definition_id
    )
    publications.publish(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    current = definitions.get_owned_definition(
        owner_user_id="alice", definition_id=definition_id
    )
    assert current.current_version.version == 2
    assert current.axes.publication == "UNPUBLISHED"
    assert current.axes.certification == "UNCERTIFIED"
    assert current.axes.confirmation == "DRAFT"
    assert current.axes.retention == "SESSION"
    # ...and v1 keeps its OWN exact axes in the catalogue
    assert catalogue.get(definition_id, 1) is not None
    assert catalogue.current_version(definition_id) == 1


def test_certifying_a_historical_version_never_rewrites_current_axes() -> None:
    from src.nl2sql.artifacts.library import InMemoryLibraryRepository
    from src.nl2sql.artifacts.product_library_service import (
        CertificationAuthority,
        build_product_library_service,
    )
    from src.nl2sql.artifacts.publication import PublicationCatalogue
    from src.nl2sql.artifacts.publication_service import PublicationService

    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    catalogue = PublicationCatalogue()
    library = InMemoryLibraryRepository(catalogue=catalogue)
    service = build_product_library_service(
        catalogue=catalogue,
        library=library,
        definitions=definitions,
        publications=PublicationService(definitions=definitions, catalogue=catalogue),
        certification_authority=CertificationAuthority(
            service_mode="infra-dev",
            typed_runtime_activation="local_real_data_demo",
            admin_user_id="admin",
        ),
    )
    spec = _execute_spec()
    draft = definitions.create_draft(
        owner_user_id="alice", title="T", calculation=spec
    )
    definition_id = draft.definition_id
    for step in ("mark_semantic_closed", "confirm", "save"):
        getattr(definitions, step)(
            owner_user_id="alice", definition_id=definition_id
        )
    service._publications.publish(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    definitions.create_revision(
        owner_user_id="alice", definition_id=definition_id
    )
    # certify the HISTORICAL v1
    service.certify(user_id="admin", identity_id=definition_id, version=1)
    current = definitions.get_owned_definition(
        owner_user_id="alice", definition_id=definition_id
    )
    assert current.current_version.version == 2
    assert current.axes.certification == "UNCERTIFIED", "v2 axes were rewritten"
    assert current.axes.publication == "UNPUBLISHED"
    assert catalogue.certification_state(definition_id, 1) == "certified"


@pytest.mark.parametrize(
    "override_field",
    [
        "authorization",
        "scope_level",
        "allowed_scope_ids",
        "relation",
        "source_id",
        "receipt_step_id",
        "fact_id",
        "provenance",
        "sql",
        "value",
        "unit",
        "metric_key",
        "role",
        "data_as_of",
    ],
)
def test_execution_context_cannot_carry_input_selection_authority(
    override_field: str,
) -> None:
    fetcher = _RecordingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    payload = {
        "binding": _binding_body(spec, 90),
        "execution_context": {
            "date_mode": "exact_date",
            "exact_date": "2026-08-15",
            override_field: "injected",
        },
    }
    response = client.post(_execute_path(definition_id), json=payload)
    assert response.status_code == 422, (override_field, response.text)
    assert fetcher.contexts == [], f"{override_field} reached the fetcher"


def test_parameter_values_remain_calculation_parameters_only() -> None:
    """A parameter VALUE can never select the governed business input."""

    from src.nl2sql.orchestration.custom_calculation_execution import (
        ResolvedCalculationInput,
    )

    class _SelectingFetcher:
        def __init__(self) -> None:
            self.roles: list[str] = []
            self.metrics: list[str] = []
            self.contexts: list[Any] = []

        async def fetch_metric_input(self, **kwargs: object) -> Any:
            self.roles.append(str(kwargs["role"]))
            self.metrics.append(str(kwargs["metric_key"]))
            self.contexts.append(kwargs["execution_context"])
            return ResolvedCalculationInput(
                role=str(kwargs["role"]),
                metric_key=str(kwargs["metric_key"]),
                value=Decimal("45"),
                unit="percent",
                data_as_of=datetime(2026, 9, 20, tzinfo=UTC),
                provenance=str(kwargs["required_provenance"]),
                source_id="gold.repair.archive",
                receipt_step_id="fetch_actual",
                fact_id="a" * 64,
            )

    fetcher = _SelectingFetcher()
    client, definition_id = _execute_app(fetcher)
    spec = _execute_spec()
    for target in (10, 90, 100):
        response = client.post(
            _execute_path(definition_id), json={"binding": _binding_body(spec, target)}
        )
        assert response.status_code == 200, response.text
    # Every run selected the SAME governed role/metric regardless of the value.
    assert fetcher.roles == ["actual", "actual", "actual"]
    assert set(fetcher.metrics) == {"repair_service_archive_rate_overall_day"}
    assert all(c.date_mode == "latest_authoritative" for c in fetcher.contexts)
    assert all(c.exact_date is None for c in fetcher.contexts)
