"""Identity-bound thread namespacing and the runtime configurable payload."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast
from urllib.parse import quote

from src.nl2sql.contracts import (
    AuthorizationContext,
    AuthorizationDecision,
    ExecutionReceipt,
    RequestContext,
    ScopeLevel,
    evaluate_authorization,
)

# The single configurable key carrying the trusted authorization context; the
# context itself carries its authorization revision, so no second copy exists.
AUTHORIZATION_CONFIG_KEY = "authorization_context"


def internal_thread_id(context: RequestContext) -> str:
    """Return a checkpointer key that cannot be reached by another user."""

    owner = quote(context.identity.user_id, safe="")
    return f"{context.deployment_scope}:{owner}:{context.thread_id}"


def runtime_config(context: RequestContext) -> dict[str, object]:
    """Build the config propagated to supervisor graphs and their tools.

    The pre-existing keys and their exact values are unchanged.  The
    authorization context (which carries its own revision) is added only when
    the request actually carries one, so an authorization-free request produces
    the exact same mapping as before this slice.
    """

    identity = context.identity
    request_payload = context.model_dump(mode="json")
    if context.authorization is None:
        # Drop only the top-level authorization key so an authorization-free
        # request_context is byte-identical to the pre-slice payload.
        # exclude_none is unusable here: it is recursive and would also strip
        # identity's pre-existing "auth_epoch": null.
        request_payload.pop("authorization", None)
    configurable: dict[str, object] = {
        "thread_id": internal_thread_id(context),
        "request_identity": identity.model_dump(mode="json"),
        "request_context": request_payload,
        "auth_user_id": identity.user_id,
        "auth_user_roles": sorted(identity.roles),
        "auth_user_permissions": sorted(identity.permissions),
    }
    authorization = context.authorization
    if authorization is not None:
        configurable[AUTHORIZATION_CONFIG_KEY] = authorization.model_dump(mode="json")
    return {"configurable": configurable}


def runtime_configurable() -> dict[str, object]:
    """Return the active child-runnable configurable mapping, or an empty one.

    Outside a LangGraph run (for example a directly invoked unit test) the
    context variable is unset and this returns an empty mapping, which callers
    must treat exactly like an absent authorization carrier.
    """

    from langchain_core.runnables.config import var_child_runnable_config

    runtime = var_child_runnable_config.get()
    raw = runtime.get("configurable", {}) if isinstance(runtime, dict) else {}
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


def authorization_context_from_config(
    configurable: Mapping[str, object],
) -> AuthorizationContext | None:
    """Read a trusted AuthorizationContext back out of a configurable mapping.

    PURE and total: an absent carrier, a wrong-typed carrier, or a malformed
    payload all collapse to None and this function never raises.  A malformed
    context is therefore treated EXACTLY like an absent one, reusing the
    slice-1 fail-closed discipline rather than inventing a second error path.
    """

    raw = configurable.get(AUTHORIZATION_CONFIG_KEY)
    if isinstance(raw, AuthorizationContext):
        return raw
    if not isinstance(raw, Mapping):
        return None
    try:
        # Revalidate through the JSON path: the carrier is a JSON projection, and
        # AuthorizationContext is strict, so a JSON array must still bind to the
        # strict tuple field exactly as it did when originally produced.
        return AuthorizationContext.model_validate_json(json.dumps(dict(raw)))
    except Exception:
        return None


def evaluate_config_authorization(
    configurable: Mapping[str, object],
    *,
    authorization_required: bool,
    expected_revision: str | None,
    requested_scope_level: ScopeLevel | None = None,
    requested_scope_id: str | None = None,
) -> AuthorizationDecision | None:
    """The single fail-closed enforcement seam for a runtime configurable.

    Returns None ONLY when authorization is not required AND no valid context is
    carried -- absent, wrong-typed or malformed, all of which the accessor
    collapses to absent -- so an authorization-free request follows today's
    code path unchanged.  Otherwise it delegates verbatim to
    evaluate_authorization: required-but-absent yields the canonical denial,
    and a valid-but-unusable supplied context (revision-mismatched,
    agent-disabled, empty or out-of-scope) yields that SAME canonical denial
    even when authorization_required is False.  No second deny shape is
    produced and the cause is never placed on a user-visible surface.
    """

    context = authorization_context_from_config(configurable)
    if context is None and not authorization_required:
        return None
    return evaluate_authorization(
        context,
        expected_revision=expected_revision,
        requested_scope_level=requested_scope_level,
        requested_scope_id=requested_scope_id,
    )


def bind_execution_receipt_authorization(
    receipt: ExecutionReceipt,
    configurable: Mapping[str, object],
) -> ExecutionReceipt:
    """Bind the carried authorization revision onto an execution receipt.

    Called where the execution receipt is constructed.  When the configurable
    carries no valid authorization context the ORIGINAL receipt is returned
    unchanged, so receipts on the existing path stay byte-identical.
    """

    context = authorization_context_from_config(configurable)
    if context is None:
        return receipt
    return receipt.model_copy(
        update={"authorization_revision": context.authorization_revision}
    )
