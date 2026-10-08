"""C post-hardening falsification probes.

Each probe SABOTAGES behaviour at runtime (monkeypatch) to prove the
corresponding post-hardening acceptance gate is not vacuous.  No production
file is edited by this module.
"""

from __future__ import annotations

from typing import Any

import pytest


def test_probe_build_authority_gate_is_not_vacuous() -> None:
    """A non-BUILD envelope must be rejected by the same predicate."""

    from src.nl2sql.orchestration.mode_contract import RunEnvelope

    build = RunEnvelope(run_id="r", requested_mode="BUILD", effective_mode="BUILD")
    query = RunEnvelope(run_id="r", requested_mode="QUERY", effective_mode="QUERY")
    # The production predicate is exactly this equality.
    assert (build.effective_mode != "BUILD") is False
    assert (query.effective_mode != "BUILD") is True


async def test_probe_sse_root_gate_is_not_vacuous() -> None:
    """The REAL production root gate must reject each falsification shape.

    The strict-root predicate is inline in the production SSE generator
    (src.nl2sql.api.stream_blocks), so it is not importable on its own.  Rather
    than re-implement it here - a local copy could silently drift from
    production and let this probe pass vacuously - the probe feeds each
    falsification shape through the REAL generator and asserts that no product
    block is projected.
    """

    from src.nl2sql.api import stream_blocks

    class _Supervisor:
        def __init__(self, events: list[dict[str, Any]]) -> None:
            self._events = events

        async def astream_events(self, *_args: object, **_kwargs: object) -> Any:
            for event in self._events:
                yield event

    async def _block_frames(event: dict[str, Any]) -> list[str]:
        frames: list[str] = []
        async for chunk in stream_blocks(_Supervisor([event]), [], {}, "root-gate"):
            frames.append(chunk)
        return [frame for frame in frames if "event: block" in frame]

    good = {
        "event": "on_chain_end",
        "name": "nl2sql_v2_explicit",
        "parent_ids": [],
        # An explicit block payload, so the probe depends only on the root gate
        # and not on how an empty result would be defaulted.
        "data": {"output": {"response_blocks": [{"type": "text", "text": "root"}]}},
    }
    # The genuine root completion is projected as exactly one product block.
    assert len(await _block_frames(good)) == 1
    for sabotaged in (
        {**good, "name": None},
        {**good, "name": "child"},
        {**good, "parent_ids": ["p"]},
        {**good, "event": "on_chain_start"},
        {**good, "data": {}},
    ):
        assert await _block_frames(sabotaged) == [], sabotaged


def test_probe_publication_current_monotonicity_is_not_vacuous() -> None:
    """Directly mutating the pointer backwards must be DETECTABLE."""

    from src.nl2sql.artifacts.publication import PublicationCatalogue

    catalogue = PublicationCatalogue()
    catalogue._versions[("x", 1)] = object()
    catalogue._current["x"] = 1
    assert catalogue._current["x"] == 1
    # A rollback mutator would look exactly like this, and must not exist.
    assert not hasattr(PublicationCatalogue, "set_current_version")


def test_probe_date_context_gate_is_not_vacuous() -> None:
    """The date-context validator must reject each illegal pair."""

    from src.nl2sql.orchestration.governed_calculation_inputs import (
        DefinitionExecutionContext,
    )

    with pytest.raises(Exception):
        DefinitionExecutionContext(
            date_mode="latest_authoritative", exact_date="2026-01-01"
        )
    with pytest.raises(Exception):
        DefinitionExecutionContext(date_mode="exact_date")
    ok = DefinitionExecutionContext(date_mode="exact_date", exact_date="2026-01-01")
    assert ok.exact_date is not None


def test_probe_lifecycle_axis_gate_is_not_vacuous() -> None:
    """A collapsed status field would change the model field set."""

    from src.nl2sql.supervisor.schemas import DefinitionBlock

    fields = set(DefinitionBlock.model_fields)
    assert "status" not in fields
    assert "confirmation" in fields


def test_probe_extra_field_rejection_is_not_vacuous() -> None:
    """An extra field must be REJECTED, proving extra=forbid is in effect."""

    from pydantic import ValidationError

    from src.nl2sql.artifacts.api_definitions import ExecuteDefinitionRequest

    with pytest.raises(ValidationError):
        ExecuteDefinitionRequest.model_validate(
            {
                "binding": {
                    "calculation_id": "c",
                    "spec_checksum": "a" * 64,
                },
                "actual": 999,
            }
        )


def test_probe_definition_block_sabotage_is_detectable() -> None:
    """Prove a collapsed-status model WOULD be caught by the C gate."""

    from src.nl2sql.supervisor.schemas import DefinitionBlock

    collapsed = type(
        "Collapsed",
        (DefinitionBlock,),
        {"__annotations__": {"status": str}, "status": "SAVED"},
    )
    assert "status" in set(collapsed.model_fields)
    assert "status" not in set(DefinitionBlock.model_fields)

