"""Server-owned BUILD run capability validation for semantic mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status

from src.core.auth.types import AuthUser
from src.nl2sql.contracts import RequestContext, RequestIdentity
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.ownership import runtime_config

BUILD_MODE_REQUIRED = "build_mode_required"


def _build_mode_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=BUILD_MODE_REQUIRED,
    )


async def require_build_run(request: Request, auth_user: AuthUser) -> RunEnvelope:
    """Require the exact current server-persisted BUILD run for this user.

    The two headers are opaque references.  They grant no authority without a
    current checkpoint containing a matching RunEnvelope and server-derived
    owner binding.
    """

    thread_header = request.headers.get("x-tt-build-thread-id")
    run_header = request.headers.get("x-tt-build-run-id")
    if not thread_header or not run_header:
        raise _build_mode_required()
    try:
        thread_id = UUID(thread_header)
    except ValueError as exc:
        raise _build_mode_required() from exc

    container = getattr(request.app.state, "container", None)
    get_engine = getattr(container, "get_engine", None)
    if not callable(get_engine):
        raise _build_mode_required()
    try:
        engine = await cast(Callable[[], Awaitable[Any]], get_engine)()
        identity = RequestIdentity(
            request_id=UUID(request.headers.get("x-request-id", ""))
            if request.headers.get("x-request-id")
            else uuid4(),
            user_id=str(auth_user.user_id),
            roles=frozenset(auth_user.roles),
            permissions=frozenset(auth_user.permissions),
        )
        context = RequestContext(
            identity=identity,
            thread_id=thread_id,
            trace_id=request.headers.get("x-trace-id") or str(identity.request_id),
        )
        snapshot = await engine.aget_state(runtime_config(context))
    except Exception as exc:
        raise _build_mode_required() from exc

    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping) or not values:
        raise _build_mode_required()
    try:
        envelope = RunEnvelope.model_validate(values.get("run_envelope"))
    except Exception as exc:
        raise _build_mode_required() from exc
    if envelope.effective_mode != "BUILD" or envelope.run_id != run_header:
        raise _build_mode_required()
    if values.get("run_owner_user_id") != str(auth_user.user_id):
        raise _build_mode_required()
    return envelope


__all__ = ["BUILD_MODE_REQUIRED", "require_build_run"]
