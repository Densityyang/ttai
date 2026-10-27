"""Trusted Backend AuthorizationContext provider (PRODUCTION ONLY).

This is the concrete carrier behind
``typed_runtime_activation == "trusted_backend_authorization"``.  Per request it
asks the trusted tt-api for the Backend-owned, already-resolved effective
authorization snapshot and returns it VERBATIM as an AuthorizationContext:

* it never infers scope from roles, job titles or org placement;
* it never synthesizes an authorization_revision;
* any transport error, non-200 status, or malformed payload fails closed to
  None, which every caller treats exactly like a deny;
* the caller's raw Bearer token is consumed from a request-scoped ContextVar,
  so the provider stays a deployment singleton and one request can never
  leak its token into another request's load().
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import httpx

from src.core.auth.types import AuthUser
from src.core.settings import Settings, get_settings
from src.nl2sql.contracts import AuthorizationContext

_REQUEST_BEARER_TOKEN: ContextVar[str | None] = ContextVar(
    "backend_authorization_bearer_token",
    default=None,
)


def bind_request_bearer_token(token: str | None) -> None:
    """Bind the raw Bearer token of the CURRENT request task.

    Called at the HTTP boundary while the provider for this request is being
    resolved.  The value never leaves this task and is never persisted.
    """

    _REQUEST_BEARER_TOKEN.set(token)


def extract_bearer_token(authorization: str | None) -> str | None:
    """Parse an Authorization header value into a Bearer token, else None."""

    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class TrustedBackendAuthorizationProvider:
    """Fetch the trusted Backend authorization snapshot for one request.

    Mirrors TTApiAuthProvider's transport posture (short timeout, one httpx
    call per load, no caching): authorization is resolved once per run at run
    start, and a cached grant must never outlive a Backend-side revocation.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | Any | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._base_url = cfg.tt_api_base_url.rstrip("/")
        self._authorization_path = self._normalize_path(
            cfg.tt_api_agent_authorization_path
        )
        self._timeout_seconds = cfg.auth_verify_timeout_ms / 1000
        self._client = client

    async def load(self, user: AuthUser) -> AuthorizationContext | None:
        """Return the Backend-owned snapshot for this user, else None.

        The user identity is NOT used to derive any grant; it only travels to
        the Backend as the authenticated caller.  Every failure mode collapses
        to the same None.
        """

        del user  # authority comes only from the Backend response
        token = _REQUEST_BEARER_TOKEN.get()
        if not token or not self._base_url:
            return None
        try:
            if self._client is not None:
                response = await self._client.get(
                    self._authorization_path,
                    headers={"Authorization": f"Bearer {token}"},
                )
            else:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                ) as client:
                    response = await client.get(
                        self._authorization_path,
                        headers={"Authorization": f"Bearer {token}"},
                    )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            # Tolerate the un-wrapped shape, exactly like TTApiAuthProvider.
            data = payload if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        return self._to_context(data)

    @staticmethod
    def _to_context(data: dict[str, Any]) -> AuthorizationContext | None:
        """Map the Backend payload VERBATIM; any defect fails closed to None."""

        revision = data.get("authorization_revision")
        agent_enabled = data.get("agent_enabled")
        scope_level = data.get("scope_level")
        scope_ids = data.get("allowed_scope_ids")
        if not isinstance(revision, str) or not revision.strip():
            return None
        if not isinstance(agent_enabled, bool):
            return None
        if not isinstance(scope_level, str):
            return None
        if not isinstance(scope_ids, (list, tuple)):
            return None
        try:
            return AuthorizationContext(
                authorization_revision=revision,
                agent_enabled=agent_enabled,
                scope_level=scope_level,  # type: ignore[arg-type]
                allowed_scope_ids=tuple(str(item) for item in scope_ids),
            )
        except ValueError:
            return None

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return "/"
        return path if path.startswith("/") else f"/{path}"
