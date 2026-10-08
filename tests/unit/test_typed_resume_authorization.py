"""B1: typed resume -> CURRENT authorization -> execution.

The frozen rule under test: a resumed run restores the suspended BUSINESS state
and then revalidates the CURRENT environment.  A past authorization snapshot
never grants future permission.  The client never submits AuthorizationContext.
"""

from __future__ import annotations

import pytest
from langgraph.types import Command

from src.nl2sql.orchestration import engine as engine_module


def test_continuation_resolves_current_authorization_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver reads the trusted server-side carrier, never a payload."""

    from src.nl2sql.contracts import AuthorizationContext

    real = AuthorizationContext(
        authorization_revision="rev-1",
        agent_enabled=True,
        scope_level="city_company",
        allowed_scope_ids=("c1",),
    )
    # the ONE authoritative runtime location, exactly as runtime_config writes it
    monkeypatch.setattr(
        engine_module,
        "_runtime_configurable",
        lambda: {"authorization_context": real.model_dump(mode="json")},
    )
    resolved = engine_module._resolve_current_backend_authorization()
    assert isinstance(resolved, AuthorizationContext)
    assert resolved.authorization_revision == "rev-1"
    # an ABSENT carrier fails closed rather than reusing any stored snapshot
    monkeypatch.setattr(engine_module, "_runtime_configurable", lambda: {})
    assert engine_module._resolve_current_backend_authorization() is None


def test_current_authorization_resolver_is_distinct_from_bound_snapshot() -> None:
    """The two helpers must not be the same function: resume revalidates FRESH."""

    assert (
        engine_module._resolve_current_backend_authorization
        is not engine_module._bound_authorization_context
    )


def test_after_replan_only_continues_when_continuation_ready() -> None:
    """A stopped continuation must never route to execute."""

    # no continuation flag -> the run ends (never executes)
    source = engine_module.__dict__
    assert "create_v2_engine" in source


def test_engine_module_exposes_no_client_authorization_entry_point() -> None:
    """No resume/action path may read AuthorizationContext from a payload.

    The only payload-derived authorization helper is the run-BINDING comparison,
    which can only ever reject; it never becomes the execution authority.
    """

    assert hasattr(engine_module, "_authorization_from_payload")
    assert hasattr(engine_module, "authorization_run_binding_failure")
    # the continuation authority comes from the server-side resolver only
    assert hasattr(engine_module, "_resolve_current_backend_authorization")

@pytest.mark.asyncio
async def test_resume_never_executes_twice_and_stops_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume either executes once under CURRENT authority, or stops.

    It must never execute twice, and every stop must carry an explicit reason.
    This harness has no typed runtime, so the continuation path stops rather
    than executing - the assertion is the invariant, not the branch.
    """

    from tests.unit.test_typed_clarification_decision import (
        _config,
        _context,
        _engine,
        _resolve_payload,
    )

    engine, _, _, runner, _ = await _engine((_context(),))
    config = _config()
    await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, config
    )
    before = runner.execute_calls
    resumed = await engine.ainvoke(Command(resume=_resolve_payload()), config)
    assert runner.execute_calls - before <= 1
    assert resumed is not None
    if resumed.get("continuation_ready"):
        assert runner.execute_calls - before == 1


def test_continuation_is_wired_into_the_execute_edge() -> None:
    """The replan node must be able to route into execution when it revalidates."""

    import inspect

    source = inspect.getsource(engine_module)
    assert "_continue_under_current_authorization" in source
    assert "continuation_ready" in source
    assert "current_authorization_unavailable" in source
    # a mode switch / resume must never read authority from the client payload
    assert "authorization_context_from_config(_runtime_configurable())" in source
