"""tt-api 认证信息 Provider。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.requests import Request

from src.core.auth.types import AuthUser
from src.core.settings import Settings, get_settings


@dataclass(slots=True)
class AuthError(Exception):
    """统一鉴权异常。"""

    status_code: int
    code: str
    message: str


class AuthUnauthorizedError(AuthError):
    """401 未登录/凭证无效。"""

    def __init__(self, message: str = "未登录或凭证无效") -> None:
        super().__init__(status_code=401, code="AUTH_UNAUTHORIZED", message=message)


class AuthForbiddenError(AuthError):
    """403 已登录但无权限。"""

    def __init__(self, message: str = "无权限访问") -> None:
        super().__init__(status_code=403, code="AUTH_FORBIDDEN", message=message)


class AuthUpstreamUnavailableError(AuthError):
    """503 鉴权上游不可用。"""

    def __init__(self, message: str = "鉴权上游不可用") -> None:
        super().__init__(
            status_code=503,
            code="AUTH_UPSTREAM_UNAVAILABLE",
            message=message,
        )


@dataclass(slots=True)
class AuthMetrics:
    """鉴权链路指标。"""

    verify_total: int = 0
    verify_fail_total: int = 0
    upstream_timeout_total: int = 0
    cache_hit_total: int = 0
    verify_latency_ms_total: float = 0.0
    verify_latency_count: int = 0


class TTApiAuthProvider:
    """通过 tt-api 当前用户接口完成 token 验证并返回用户上下文。"""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | Any | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._base_url = cfg.tt_api_base_url.rstrip("/")
        self._auth_info_path = self._normalize_path(cfg.tt_api_auth_info_path)
        self._timeout_seconds = cfg.auth_verify_timeout_ms / 1000
        self._cache_ttl_seconds = cfg.auth_cache_ttl_seconds
        self._client = client
        self._time_fn = time_fn or time.monotonic
        self._cache: dict[str, tuple[float, AuthUser]] = {}
        self._metrics = AuthMetrics()

    async def authenticate_request(self, request: Request) -> AuthUser:
        """从 Request 中读取 Authorization 并完成鉴权。"""
        return await self.authenticate_authorization(request.headers.get("Authorization"))

    async def authenticate_authorization(self, authorization: str | None) -> AuthUser:
        """使用 Authorization Header 完成鉴权。"""
        token = self._extract_bearer_token(authorization)
        return await self.verify_token(token)

    async def verify_token(self, token: str) -> AuthUser:
        """校验 token 并返回用户信息。"""
        start = self._time_fn()
        self._metrics.verify_total += 1
        token_hash = self._token_hash(token)
        try:
            cached = self._cache_get(token_hash)
            if cached is not None:
                self._metrics.cache_hit_total += 1
                return cached

            response = await self._request_auth_info(token)
            user = self._map_response_to_user(response)
            self._cache_set(token_hash, user)
            return user
        except AuthError:
            self._metrics.verify_fail_total += 1
            raise
        finally:
            self._metrics.verify_latency_ms_total += (self._time_fn() - start) * 1000
            self._metrics.verify_latency_count += 1

    async def _request_auth_info(self, token: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}

        try:
            if self._client is not None:
                response = await self._client.get(self._auth_info_path, headers=headers)
            else:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                ) as client:
                    response = await client.get(self._auth_info_path, headers=headers)
        except httpx.TimeoutException as exc:
            self._metrics.upstream_timeout_total += 1
            raise AuthUpstreamUnavailableError("鉴权上游请求超时") from exc
        except httpx.HTTPError as exc:
            raise AuthUpstreamUnavailableError("鉴权上游请求失败") from exc

        return response

    def _map_response_to_user(self, response: httpx.Response | Any) -> AuthUser:
        self._raise_for_status(response.status_code)

        payload = self._parse_json(response)
        raw_data = self._extract_data(payload)
        user_id = self._extract_user_id(raw_data)
        telephone = self._extract_telephone(raw_data)
        roles = self._normalize_str_list(raw_data.get("roles"))
        permissions = self._normalize_str_list(
            self._pick_first(raw_data, ["permissions", "perms", "scopes"]),
        )

        return AuthUser(
            user_id=user_id,
            telephone=telephone,
            roles=roles,
            permissions=permissions,
        )

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code == 401:
            raise AuthUnauthorizedError()
        if status_code == 403:
            raise AuthForbiddenError()
        if status_code >= 500:
            raise AuthUpstreamUnavailableError()
        if status_code >= 400:
            raise AuthUnauthorizedError()

    @staticmethod
    def _parse_json(response: httpx.Response | Any) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise AuthUpstreamUnavailableError("鉴权上游返回非 JSON") from exc

    @staticmethod
    def _extract_data(payload: Any) -> dict[str, Any]:
        raw_data = payload.get("data") if isinstance(payload, dict) else payload
        if raw_data is None:
            raw_data = payload
        if not isinstance(raw_data, dict):
            raise AuthUpstreamUnavailableError("鉴权上游返回格式错误")
        return raw_data

    def _extract_user_id(self, raw_data: dict[str, Any]) -> int | str:
        user_id = self._pick_first(
            raw_data,
            ["user_id", "id", "userId", "uid", "admin_id"],
        )
        if user_id is None:
            raise AuthUpstreamUnavailableError("鉴权上游缺少用户标识")
        return user_id

    def _extract_telephone(self, raw_data: dict[str, Any]) -> str | None:
        telephone_raw = self._pick_first(raw_data, ["telephone", "phone", "mobile"])
        return str(telephone_raw) if telephone_raw is not None else None

    def _cache_get(self, token_hash: str) -> AuthUser | None:
        entry = self._cache.get(token_hash)
        if entry is None:
            return None

        expire_at, user = entry
        if self._time_fn() >= expire_at:
            self._cache.pop(token_hash, None)
            return None

        return user

    def _cache_set(self, token_hash: str, user: AuthUser) -> None:
        expire_at = self._time_fn() + self._cache_ttl_seconds
        self._cache[token_hash] = (expire_at, user)

    def get_metrics_snapshot(self) -> dict[str, float | int]:
        verify_total = self._metrics.verify_total
        latency_count = self._metrics.verify_latency_count
        average_latency = (
            self._metrics.verify_latency_ms_total / latency_count
            if latency_count > 0
            else 0.0
        )
        cache_hit_ratio = (
            self._metrics.cache_hit_total / verify_total if verify_total > 0 else 0.0
        )
        return {
            "auth_verify_latency_ms": average_latency,
            "auth_verify_fail_total": self._metrics.verify_fail_total,
            "auth_upstream_timeout_total": self._metrics.upstream_timeout_total,
            "auth_cache_hit_ratio": cache_hit_ratio,
        }

    @staticmethod
    def _extract_bearer_token(authorization: str | None) -> str:
        if authorization is None:
            raise AuthUnauthorizedError("缺少 Authorization 请求头")

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthUnauthorizedError("Authorization 格式必须为 Bearer <token>")

        return token.strip()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return "/"
        return path if path.startswith("/") else f"/{path}"

    @staticmethod
    def _pick_first(data: dict[str, Any], keys: list[str]) -> Any | None:
        for key in keys:
            if key in data:
                return data[key]
        return None

    @classmethod
    def _normalize_str_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            return []

        result = [cls._normalize_role_or_permission_item(item) for item in value]
        return [text for text in result if text]

    @staticmethod
    def _normalize_role_or_permission_item(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return None

        for key in ("code", "role_code", "name", "value", "permission"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None
