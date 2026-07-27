"""Identity-bound thread namespacing used by all stateful v2 endpoints."""

from __future__ import annotations

from urllib.parse import quote

from src.nl2sql.contracts import RequestContext


def internal_thread_id(context: RequestContext) -> str:
    """Return a checkpointer key that cannot be reached by another user."""

    owner = quote(context.identity.user_id, safe="")
    return f"{context.deployment_scope}:{owner}:{context.thread_id}"


def runtime_config(context: RequestContext) -> dict[str, object]:
    """Build the config propagated to supervisor graphs and their tools."""

    identity = context.identity
    return {
        "configurable": {
            "thread_id": internal_thread_id(context),
            "request_identity": identity.model_dump(mode="json"),
            "request_context": context.model_dump(mode="json"),
            "auth_user_id": identity.user_id,
            "auth_user_roles": sorted(identity.roles),
            "auth_user_permissions": sorted(identity.permissions),
        }
    }
