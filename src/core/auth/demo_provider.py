"""EXPLICIT demo-only synthetic authorization provider.

This provider exists so the product flow can be exercised END TO END without a
production Backend.  It is deliberately narrow:

* it authorizes ONLY identities present in an explicit demo fixture;
* an unknown user resolves to None (no wildcard, no fallback);
* it NEVER infers authority from a role name, a job title or org membership;
* it claims no production permission and reads no production database;
* its revision lives in a clearly identifiable DEMO namespace, so a synthetic
  grant can never be mistaken for a Backend-issued one.

Selection is structurally guarded elsewhere: the demo value is rejected at
Settings construction whenever service_mode == "product", and the container
only ever builds this provider inside that activation branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.core.auth.types import AuthUser
from src.nl2sql.contracts import AuthorizationContext, ScopeLevel

# Every demo revision is namespaced, so a synthetic authority can never be
# confused with a trusted Backend revision.
DEMO_REVISION_PREFIX: Final[str] = "demo-synthetic:"


@dataclass(frozen=True, slots=True)
class DemoIdentity:
    """One explicit demo fixture identity and its bounded synthetic authority."""

    user_id: str
    scope_level: ScopeLevel
    allowed_scope_ids: tuple[str, ...]
    agent_enabled: bool = True
    # The fixture revision suffix; the full revision is namespaced on read.
    revision_suffix: str = "v1"


DEFAULT_DEMO_IDENTITIES: Final[tuple[DemoIdentity, ...]] = (
    DemoIdentity(
        user_id="demo-analyst",
        scope_level="team",
        allowed_scope_ids=("demo-team-1",),
    ),
    DemoIdentity(
        user_id="demo-manager",
        scope_level="area",
        allowed_scope_ids=("demo-area-1", "demo-area-2"),
    ),
)


class DemoBackendAuthorizationProvider:
    """Frozen-demo implementation of the BackendAuthorizationProvider Protocol.

    It satisfies the SAME Protocol as the real provider so callers cannot tell
    them apart structurally - the distinguishing signal is the namespaced
    revision and the deployment provenance, never a type check at call time.
    """

    def __init__(self, identities: tuple[DemoIdentity, ...] | None = None) -> None:
        fixtures = identities if identities is not None else DEFAULT_DEMO_IDENTITIES
        self._identities = {identity.user_id: identity for identity in fixtures}

    async def load(self, user: AuthUser) -> AuthorizationContext | None:
        """Return synthetic authority ONLY for an explicitly listed demo user."""

        identity = self._identities.get(str(user.user_id))
        if identity is None:
            # An unknown (i.e. real) authenticated user gets NO demo authority.
            return None
        return AuthorizationContext(
            authorization_revision=f"{DEMO_REVISION_PREFIX}{identity.revision_suffix}",
            agent_enabled=identity.agent_enabled,
            scope_level=identity.scope_level,
            allowed_scope_ids=identity.allowed_scope_ids,
        )
