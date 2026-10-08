"""认证相关数据模型。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OrganizationIdentity:
    """组织身份上下文。

    CONTEXT ONLY: this is the user's OWN placement, never a grant.  "user is in
    team 12" must never be read as "user may see team 12".  Authority comes only
    from the trusted Backend AuthorizationContext.

    IDs are opaque STRINGS, aligned with the authorization contract's opaque
    scope-id model.  Every level is independently optional: the real data has a
    company -> team -> employee shape with department unpopulated, so no level
    may be synthesized from another.

    PII is deliberately excluded (no id_card, phone, emergency contact or
    employee_code).
    """

    company_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    employee_id: str | None = None
    position_id: str | None = None
    company_name: str | None = None
    department_name: str | None = None
    team_name: str | None = None
    position_name: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.company_id,
                self.department_id,
                self.team_id,
                self.employee_id,
                self.position_id,
            )
        )


@dataclass(frozen=True, slots=True)
class AuthUser:
    """已认证用户上下文。"""

    user_id: int | str
    telephone: str | None
    roles: list[str]
    permissions: list[str]
    # Optional so every existing 4-argument construction site keeps working.
    organization: OrganizationIdentity | None = None
