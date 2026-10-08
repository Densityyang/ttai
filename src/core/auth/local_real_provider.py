"""SERVER-OWNED local real-data authority (LOCAL DEMO ONLY).

This profile reads the REAL business database read-only, but its AUTHORITY is a
server-owned local fixture - it is never labelled backend/production/certified
production authority.  The identity is configured on the server and is NEVER
inferred from roles, organization labels, a request payload, headers or query
parameters.

It exists ONLY under typed_runtime_activation == "local_real_data_demo".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.core.auth.types import AuthUser
from src.nl2sql.contracts import AuthorizationContext

# A clearly local/demo revision namespace, distinct from both the Backend and
# the synthetic demo namespaces.
LOCAL_REAL_REVISION_PREFIX: Final[str] = "local-real-demo:"
LOCAL_REAL_PROVENANCE: Final[str] = "local_real_demo"


@dataclass(frozen=True, slots=True)
class LocalRealIdentity:
    """One configured local-real demo identity and its bounded scope."""

    user_id: str
    scope_level: str
    allowed_scope_ids: tuple[str, ...]
    revision_suffix: str = "v1"


DEFAULT_LOCAL_REAL_IDENTITIES: Final[tuple[LocalRealIdentity, ...]] = (
    LocalRealIdentity(
        user_id="local-real-demo",
        # The frozen case is the company-wide ("overall") daily rate, so its
        # bound scope is the deployment's city_company level (the only
        # organization-wide level in the frozen ScopeLevel vocabulary).
        scope_level="city_company",
        allowed_scope_ids=("local-real-demo-scope",),
    ),
)


class LocalRealAuthorizationProvider:
    """Local real-data authority: explicit fixtures only, never inferred."""

    def __init__(
        self, identities: tuple[LocalRealIdentity, ...] | None = None
    ) -> None:
        fixtures = (
            identities
            if identities is not None
            else DEFAULT_LOCAL_REAL_IDENTITIES
        )
        self._identities = {identity.user_id: identity for identity in fixtures}

    async def load(self, user: AuthUser) -> AuthorizationContext | None:
        identity = self._identities.get(str(user.user_id))
        if identity is None:
            # A real/unknown authenticated user gets NO local-real authority.
            return None
        return AuthorizationContext(
            authorization_revision=f"{LOCAL_REAL_REVISION_PREFIX}{identity.revision_suffix}",
            agent_enabled=True,
            scope_level=identity.scope_level,  # type: ignore[arg-type]
            allowed_scope_ids=identity.allowed_scope_ids,
        )


__all__ = [
    "DEFAULT_LOCAL_REAL_IDENTITIES",
    "LOCAL_REAL_PROVENANCE",
    "LOCAL_REAL_REVISION_PREFIX",
    "LocalRealAuthorizationProvider",
    "LocalRealIdentity",
]
