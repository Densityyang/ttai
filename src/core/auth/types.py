"""认证相关数据模型。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthUser:
    """已认证用户上下文。"""

    user_id: int | str
    telephone: str | None
    roles: list[str]
    permissions: list[str]
