"""B-RT subgate: demo runtime, factory selection and capability wire."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import FetchMetricStep, RequestIdentity
from src.nl2sql.demo.fixtures import DEMO_DATASOURCE, DEMO_READONLY_ROLE
from src.nl2sql.demo.runtime import (
    DemoUnsupportedRequest,
    build_demo_runtime,
    is_demo_identity,
)
from src.nl2sql.orchestration.execution import RuntimeCalculationRunner
from src.nl2sql.orchestration.typed_runtime import TypedRuntimeBundle
from src.nl2sql.v2 import (
    BLOCK_MANIFEST,
    UNSUPPORTED_BLOCK_FALLBACK,
    register_v2_routes,
)


class _Auth:
    authorization_revision = "demo-synthetic:v1"


def _identity(user_id: str = "demo-analyst") -> RequestIdentity:
    return RequestIdentity(request_id=uuid4(), user_id=user_id)


def test_demo_runtime_satisfies_the_shared_bundle_contract() -> None:
    runtime = build_demo_runtime(identity=_identity(), authorization=_Auth())
    assert runtime is not None
    assert isinstance(runtime, TypedRuntimeBundle)


def test_foreign_identity_never_builds_a_demo_runtime() -> None:
    for user_id in ("real-alice", "admin", "demo-analyst "):
        assert (
            build_demo_runtime(identity=_identity(user_id), authorization=_Auth())
            is None
        )


def test_demo_identity_test_is_explicit() -> None:
    assert is_demo_identity(_identity("demo-analyst")) is True
    assert is_demo_identity(_identity("someone-else")) is False


@pytest.mark.asyncio
async def test_known_metric_resolves_deterministically() -> None:
    identity = _identity()
    runtime = build_demo_runtime(identity=identity, authorization=_Auth())
    assert runtime is not None
    context = await runtime.context_resolver.resolve(
        question="revenue", identity=identity, route_hint="fast"
    )
    plan = await runtime.query_plan_provider.propose(
        question="revenue", context=context, identity=identity
    )
    assert plan.metric_keys == ("demo.revenue",)
    assert runtime.query_plan_provider.is_deterministic is True


@pytest.mark.asyncio
async def test_unsupported_term_raises_without_a_model() -> None:
    identity = _identity()
    runtime = build_demo_runtime(identity=identity, authorization=_Auth())
    assert runtime is not None
    context = await runtime.context_resolver.resolve(
        question="revenue", identity=identity, route_hint="fast"
    )
    with pytest.raises(DemoUnsupportedRequest):
        await runtime.query_plan_provider.propose(
            question="why did revenue drop", context=context, identity=identity
        )


@pytest.mark.asyncio
async def test_identity_bound_resolver_refuses_a_foreign_caller() -> None:
    runtime = build_demo_runtime(identity=_identity(), authorization=_Auth())
    assert runtime is not None
    with pytest.raises(ValueError, match="request_identity_mismatch"):
        await runtime.context_resolver.resolve(
            question="revenue", identity=_identity("real-alice"), route_hint="fast"
        )


@pytest.mark.asyncio
async def test_demo_runner_returns_demo_provenance_only() -> None:
    identity = _identity()
    runtime = build_demo_runtime(identity=identity, authorization=_Auth())
    assert runtime is not None
    context = await runtime.context_resolver.resolve(
        question="revenue", identity=identity, route_hint="fast"
    )
    plan = await runtime.query_plan_provider.propose(
        question="revenue", context=context, identity=identity
    )
    step = FetchMetricStep(step_id="fetch_revenue", metric_keys=plan.metric_keys)
    runner = runtime.plan_executor._metric_runner
    prepared = await runner.prepare(step=step, query_plan=plan, context=context)
    result = await runner.execute(prepared, timeout_ms=1000)
    assert result.receipt.datasource == DEMO_DATASOURCE
    assert result.receipt.readonly_role == DEMO_READONLY_ROLE
    assert result.receipt.authorization_revision == "demo-synthetic:v1"
    assert result.receipt.policy_outcome == "allow"
    for forbidden in ("postgres", "business_reader", "business_postgres"):
        assert forbidden not in result.receipt.datasource
        assert forbidden not in result.receipt.readonly_role


def test_query_demo_runtime_has_no_ad_hoc_runner() -> None:
    runtime = build_demo_runtime(identity=_identity(), authorization=_Auth())
    assert runtime is not None
    assert runtime.plan_executor._ad_hoc_calculation_runner is None


def test_analyze_demo_runtime_may_carry_the_shared_ad_hoc_runner() -> None:
    runtime = build_demo_runtime(
        identity=_identity(),
        authorization=_Auth(),
        ad_hoc_calculation_runner=RuntimeCalculationRunner(),
    )
    assert runtime is not None
    assert runtime.plan_executor._ad_hoc_calculation_runner is not None


def test_capabilities_response_populates_the_block_manifest() -> None:
    """GAP 2: the wire must carry the manifest, not Pydantic defaults."""

    app = FastAPI()

    class _Container:
        checkpoint_available = False

        async def get_engine(self) -> Any:
            raise AssertionError("no engine needed")

    app.state.container = _Container()

    async def _identity_dep() -> AuthUser:
        return AuthUser(user_id="u", telephone=None, roles=[], permissions=["*"])

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity_dep

    with TestClient(app) as client:
        response = client.get("/api/v2/nl2sql/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["supported_block_types"] == list(BLOCK_MANIFEST)
    assert body["unsupported_block_fallback"] == dict(UNSUPPORTED_BLOCK_FALLBACK)
    assert "mode_suggestion" in body["supported_block_types"]
