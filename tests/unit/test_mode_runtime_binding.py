"""B2: ProductMode bound to the real runtime; QUERY is HARD zero-model.

R1 falsified an earlier version of this guard: the run envelope was injected as
graph input but not declared in engine state, so LangGraph silently dropped it
and the mode check was dead code.  These tests pin the fix.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.nl2sql.contracts import (
    ProductMode,
    RequestContext,
    RequestIdentity,
    RouteName,
)
from src.nl2sql.orchestration import engine as engine_module
from src.nl2sql.orchestration.engine import V2EngineState
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.v2 import QueryRequest, QueryResponse, _run_envelope


def test_run_envelope_is_declared_in_engine_state() -> None:
    """The mode axis must survive LangGraph input filtering."""

    assert "run_envelope" in V2EngineState.__annotations__
    assert "continuation_ready" in V2EngineState.__annotations__


def test_auto_resolves_to_analyze_server_side() -> None:
    context = RequestContext(
        identity=RequestIdentity(request_id=uuid4(), user_id="u"),
        thread_id=uuid4(),
        trace_id="t",
    )

    class _Body:
        requested_mode = "auto"
        switched_from_run_id = None

    envelope = _run_envelope(_Body(), context)  # type: ignore[arg-type]
    assert envelope.effective_mode == "ANALYZE"
    assert envelope.requested_mode == "auto"
    assert envelope.run_id


def test_effective_mode_cannot_be_forged_by_the_client() -> None:
    """QueryRequest has no effective_mode field at all."""

    assert "effective_mode" not in QueryRequest.model_fields
    assert "run_id" not in QueryRequest.model_fields
    assert "capabilities" not in QueryRequest.model_fields
    with pytest.raises(Exception):
        QueryRequest.model_validate(
            {"messages": [{"role": "user", "content": "x"}], "effective_mode": "BUILD"}
        )


def test_one_immutable_mode_per_run() -> None:
    envelope = RunEnvelope(run_id="r1", requested_mode="auto", effective_mode="ANALYZE")
    with pytest.raises(Exception):
        envelope.effective_mode = "BUILD"  # type: ignore[misc]


def test_route_axis_does_not_determine_product_mode() -> None:
    assert set(RouteName.__args__).isdisjoint(set(ProductMode.__args__))


def test_query_never_routes_to_the_model_edge() -> None:
    """The graph-routing guard must send QUERY to END, never to a model."""

    import inspect

    source = inspect.getsource(engine_module)
    marker = "if mode == " + chr(34) + "QUERY" + chr(34) + ":"
    assert marker in source
    start = source.index(marker)
    branch = source[
        start : source.index("# ANALYZE is governed-fetch-first", start)
    ]
    # inside the QUERY branch there must be no path to the model edge
    assert "return " + chr(34) + "model" + chr(34) not in branch
    assert "return " + chr(34) + "compile" + chr(34) in branch
    assert "return " + chr(34) + "mode_capability" + chr(34) in branch


def test_unknown_effective_mode_fails_closed() -> None:
    import inspect

    source = inspect.getsource(engine_module)
    guard = "if mode not in (None, " + chr(34) + "ANALYZE" + chr(34) + ", " + chr(34) + "BUILD" + chr(34) + "):"
    assert guard in source


def test_query_response_reports_server_owned_run_identity() -> None:
    fields = QueryResponse.model_fields
    for name in (
        "run_id",
        "requested_mode",
        "effective_mode",
        "switched_from_run_id",
        "authority_provenance",
    ):
        assert name in fields


def test_mode_switch_starts_a_new_run_on_the_same_thread() -> None:
    """A switch carries lineage but never the prior run authorization."""

    switched = RunEnvelope(
        run_id="r2",
        requested_mode="ANALYZE",
        effective_mode="ANALYZE",
        switched_from_run_id="r1",
    )
    assert switched.switched_from_run_id == "r1"
    assert switched.run_id != switched.switched_from_run_id
    # the envelope carries NO authorization field to migrate
    assert "authorization" not in RunEnvelope.model_fields
    assert "permissions" not in RunEnvelope.model_fields
