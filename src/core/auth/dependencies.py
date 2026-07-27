"""FastAPI 鉴权依赖。"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Depends, HTTPException
from starlette.requests import Request

from src.core.auth.provider import AuthError, TTApiAuthProvider
from src.core.auth.types import AuthUser
from src.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _get_trace_id(request: Request) -> str:
    return (
        request.headers.get("x-trace-id")
        or request.headers.get("x-request-id")
        or request.headers.get("traceparent")
        or "-"
    )


def _has_permission(user_permissions: list[str], required_permission: str) -> bool:
    if not required_permission:
        return True

    if required_permission in user_permissions:
        return True

    # tt-api 常见超级权限表达，表示全量权限。
    return "*" in user_permissions or "*.*.*" in user_permissions


def _to_http_exception(error: AuthError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@lru_cache
def get_auth_provider() -> TTApiAuthProvider:
    return TTApiAuthProvider()


async def require_user(
    request: Request,
    provider: TTApiAuthProvider = Depends(get_auth_provider),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    if not settings.auth_enabled:
        return AuthUser(
            user_id="auth_disabled",
            telephone=None,
            roles=["system"],
            permissions=["*"],
        )

    started_at = time.perf_counter()
    trace_id = _get_trace_id(request)
    try:
        user = await provider.authenticate_request(request)
        logger.info(
            "auth verify success trace_id=%s user_id=%s method=%s path=%s permission_count=%d",
            trace_id,
            user.user_id,
            request.method,
            request.url.path,
            len(user.permissions),
        )
        return user
    except AuthError as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.warning(
            "auth verify failed trace_id=%s status_code=%s error_code=%s method=%s path=%s elapsed_ms=%.2f",
            trace_id,
            exc.status_code,
            exc.code,
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise _to_http_exception(exc) from exc


def require_permission(permission: str) -> Callable[..., Awaitable[AuthUser]]:
    async def _dependency(
        request: Request,
        user: AuthUser = Depends(require_user),
    ) -> AuthUser:
        if _has_permission(user.permissions, permission):
            return user

        trace_id = _get_trace_id(request)
        logger.warning(
            "auth permission denied trace_id=%s user_id=%s required_permission=%s method=%s path=%s",
            trace_id,
            user.user_id,
            permission,
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTH_FORBIDDEN", "message": "无权限访问"},
        )

    return _dependency


async def require_nl2sql_permission(
    request: Request,
    user: AuthUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    path = request.url.path
    path_permission_map = {
        "/invoke": settings.auth_required_permission_invoke,
        "/stream": settings.auth_required_permission_stream,
        "/stream_events": settings.auth_required_permission_stream,
    }
    permission = next(
        (
            required
            for suffix, required in path_permission_map.items()
            if path.endswith(suffix)
        ),
        "",
    )
    trace_id = _get_trace_id(request)

    if _has_permission(user.permissions, permission):
        request.state.auth_user = user
        logger.info(
            "auth permission granted trace_id=%s user_id=%s required_permission=%s method=%s path=%s permission_count=%d",
            trace_id,
            user.user_id,
            permission,
            request.method,
            request.url.path,
            len(user.permissions),
        )
        return user

    logger.warning(
        "auth permission denied trace_id=%s user_id=%s required_permission=%s method=%s path=%s permission_count=%d",
        trace_id,
        user.user_id,
        permission,
        request.method,
        request.url.path,
        len(user.permissions),
    )
    raise HTTPException(
        status_code=403,
        detail={"code": "AUTH_FORBIDDEN", "message": "无权限访问"},
    )
