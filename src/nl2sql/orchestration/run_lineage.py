"""Identity-namespaced proof for the persisted current run of one thread."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, cast

from src.nl2sql.contracts import RequestContext
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.ownership import runtime_config


class RunLineageInvalid(RuntimeError):
    """The requested run is not the current owned run for this thread."""

    code = "mode_switch_lineage_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


async def current_owned_run(
    engine: Any, *, context: RequestContext
) -> RunEnvelope | None:
    """Read the current owned envelope, treating an absent thread as fresh."""
    try:
        get_state = getattr(engine, "aget_state", None)
        if not callable(get_state):
            return None
        state = await cast(Awaitable[Any], get_state(runtime_config(context)))
        values = getattr(state, "values", None)
        if not isinstance(values, Mapping):
            return None
        if not values:
            return None
        # Legacy/fake engines may expose history without the v2 run envelope;
        # there is no current mode lineage to validate in that representation.
        if values.get("run_envelope") is None:
            return None
        if values.get("run_owner_user_id") != context.identity.user_id:
            raise ValueError("persisted run owner mismatch")
        envelope = RunEnvelope.model_validate(values.get("run_envelope"))
        return envelope
    except RunLineageInvalid:
        raise
    except Exception as exc:
        raise RunLineageInvalid() from exc


async def require_current_owned_run(
    engine: Any,
    *,
    context: RequestContext,
    expected_run_id: str,
) -> RunEnvelope:
    """Return the persisted current envelope or fail without an oracle."""

    try:
        envelope = await current_owned_run(engine, context=context)
        if envelope is None:
            raise ValueError("persisted run state is unavailable")
        if envelope.run_id != expected_run_id:
            raise ValueError("persisted current run mismatch")
        return envelope
    except Exception as exc:
        raise RunLineageInvalid() from exc


__all__ = ["RunLineageInvalid", "current_owned_run", "require_current_owned_run"]
