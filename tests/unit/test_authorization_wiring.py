"""P2-S1 slice 2A: authorization across the typed runtime boundary.

Injected/test composition only: the configurable mapping is built through
runtime_config, read back through the pure accessor, and evaluated with the
slice-1 evaluate_authorization rule.  No Backend, no network, no Docker, and
no existing test or fixture is touched.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import cast
from uuid import UUID

import pytest

from src.nl2sql.contracts import (
    AUTHORIZATION_DENIED,
    AuthorizationContext,
    ExecutionReceipt,
    RequestContext,
    RequestIdentity,
)
from src.nl2sql.ownership import (
    AUTHORIZATION_CONFIG_KEY,
    authorization_context_from_config,
    bind_execution_receipt_authorization,
    evaluate_config_authorization,
    runtime_config,
    runtime_configurable,
)

THREAD_ID = UUID("11111111-1111-1111-1111-111111111111")
_CANONICAL_DENY = (
    '{"outcome":"deny","reason":"authorization_denied","authorization_revision":null}'
)


def _authorization(
    revision: str = "rev-1",
    *,
    agent_enabled: bool = True,
) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=agent_enabled,
        scope_level="area",
        allowed_scope_ids=("area-1", "area-2"),
    )


def _context(authorization: AuthorizationContext | None = None) -> RequestContext:
    return RequestContext(
        identity=RequestIdentity(
            request_id=UUID("22222222-2222-2222-2222-222222222222"),
            user_id="alice",
            roles=frozenset({"analyst"}),
            permissions=frozenset({"nl2sql:invoke"}),
        ),
        thread_id=THREAD_ID,
        trace_id="trace-1",
        authorization=authorization,
    )


def _configurable(
    authorization: AuthorizationContext | None = None,
) -> dict[str, object]:
    config = runtime_config(_context(authorization))
    return cast(dict[str, object], config["configurable"])


def _receipt() -> ExecutionReceipt:
    return ExecutionReceipt(
        datasource="synthetic",
        readonly_role="fixture_reader",
        elapsed_ms=1,
        row_count=1,
        policy_version="synthetic.v1",
        policy_outcome="allow",
    )


class _ExplodingMapping(Mapping[str, object]):
    """A malformed carrier that raises on every structural access."""

    def __getitem__(self, key: str) -> object:
        raise RuntimeError("malformed carrier")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("malformed carrier")

    def __len__(self) -> int:
        return 1


# --- propagation -----------------------------------------------------------


def test_runtime_config_carries_the_context_when_present() -> None:
    configurable = _configurable(_authorization("rev-7"))
    assert configurable[AUTHORIZATION_CONFIG_KEY] == _authorization("rev-7").model_dump(
        mode="json"
    )
    # The context carries its own revision; no second, drift-prone copy exists.
    assert "authorization_revision" not in configurable


def test_runtime_config_omits_authorization_keys_when_absent() -> None:
    configurable = _configurable()
    assert set(configurable) == {
        "thread_id",
        "request_identity",
        "request_context",
        "auth_user_id",
        "auth_user_roles",
        "auth_user_permissions",
    }


def test_absent_authorization_request_context_dump_is_pre_slice_identical() -> None:
    # Structural pin: the pre-existing request_context payload must not gain an
    # "authorization" key on the authorization-free path, and the nested
    # pre-existing "auth_epoch": null must survive (exclude_none would strip it).
    configurable = _configurable()
    payload = cast(dict[str, object], configurable["request_context"])
    assert "authorization" not in payload
    assert set(payload) == {
        "deadline_ms",
        "deployment_scope",
        "identity",
        "thread_id",
        "trace_id",
    }
    identity = cast(dict[str, object], payload["identity"])
    assert identity["auth_epoch"] is None


def test_present_authorization_request_context_dump_carries_and_round_trips() -> None:
    configurable = _configurable(_authorization("rev-5"))
    payload = cast(dict[str, object], configurable["request_context"])
    assert "authorization" in payload
    assert payload["authorization"] == _authorization("rev-5").model_dump(mode="json")


# --- accessor --------------------------------------------------------------


def test_accessor_round_trips_a_carried_context() -> None:
    recovered = authorization_context_from_config(_configurable(_authorization("rev-7")))
    assert recovered == _authorization("rev-7")


def test_accessor_accepts_an_already_typed_context() -> None:
    context = _authorization("rev-3")
    assert (
        authorization_context_from_config({AUTHORIZATION_CONFIG_KEY: context}) is context
    )


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "rev-1",
        42,
        ["area-1"],
        {},
        {"authorization_revision": "rev-1", "agent_enabled": True},
        {
            "schema_version": "9.9",
            "authorization_revision": "rev-1",
            "agent_enabled": True,
            "scope_level": "area",
            "allowed_scope_ids": ["area-1"],
        },
        object(),
        _ExplodingMapping(),
    ],
)
def test_accessor_returns_none_for_absent_and_malformed_without_raising(
    malformed: object,
) -> None:
    assert authorization_context_from_config({}) is None
    assert authorization_context_from_config({AUTHORIZATION_CONFIG_KEY: malformed}) is None


# --- enforcement seam ------------------------------------------------------


def test_initial_request_with_a_valid_context_may_allow() -> None:
    decision = evaluate_config_authorization(
        _configurable(_authorization()),
        authorization_required=True,
        expected_revision=None,
        requested_scope_level="area",
        requested_scope_id="area-1",
    )
    assert decision is not None
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


def test_resume_with_a_matching_bound_revision_allows() -> None:
    decision = evaluate_config_authorization(
        _configurable(_authorization("rev-1")),
        authorization_required=True,
        expected_revision="rev-1",
    )
    assert decision is not None
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


def test_resume_with_a_stale_bound_revision_denies_canonically() -> None:
    decision = evaluate_config_authorization(
        _configurable(_authorization("rev-2")),
        authorization_required=True,
        expected_revision="rev-1",
    )
    assert decision == AUTHORIZATION_DENIED
    assert decision is not None
    assert decision.model_dump_json() == _CANONICAL_DENY


def test_required_but_absent_authorization_denies_canonically() -> None:
    decision = evaluate_config_authorization(
        {},
        authorization_required=True,
        expected_revision="rev-1",
    )
    assert decision == AUTHORIZATION_DENIED
    assert decision is not None
    assert decision.model_dump_json() == _CANONICAL_DENY


def test_present_stale_context_denies_even_when_authorization_is_not_required() -> None:
    # The requirement flag does NOT gate a supplied-but-unusable context.
    decision = evaluate_config_authorization(
        _configurable(_authorization("rev-2")),
        authorization_required=False,
        expected_revision="rev-1",
    )
    assert decision == AUTHORIZATION_DENIED
    assert decision is not None
    assert decision.model_dump_json() == _CANONICAL_DENY


def test_present_disabled_context_denies_even_when_authorization_is_not_required() -> None:
    decision = evaluate_config_authorization(
        _configurable(_authorization(agent_enabled=False)),
        authorization_required=False,
        expected_revision="rev-1",
    )
    assert decision == AUTHORIZATION_DENIED
    assert decision is not None


def test_present_but_malformed_context_is_absent_when_not_required() -> None:
    # Malformed collapses to absent, so the requirement flag DOES govern it.
    assert (
        evaluate_config_authorization(
            {AUTHORIZATION_CONFIG_KEY: "malformed"},
            authorization_required=False,
            expected_revision="rev-1",
        )
        is None
    )
    assert (
        evaluate_config_authorization(
            {},
            authorization_required=False,
            expected_revision="rev-1",
        )
        is None
    )


def test_required_with_a_valid_matching_context_allows() -> None:
    decision = evaluate_config_authorization(
        _configurable(_authorization("rev-1")),
        authorization_required=True,
        expected_revision="rev-1",
    )
    assert decision is not None
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


def test_absent_authorization_leaves_the_existing_path_unchanged() -> None:
    configurable = _configurable()
    assert AUTHORIZATION_CONFIG_KEY not in configurable
    assert (
        evaluate_config_authorization(
            configurable,
            authorization_required=False,
            expected_revision="rev-1",
        )
        is None
    )


def test_every_enforcement_failure_uses_the_one_canonical_deny_shape() -> None:
    failures: list[dict[str, object]] = [
        {},
        {AUTHORIZATION_CONFIG_KEY: "garbage"},
        _configurable(_authorization("rev-2")),
        _configurable(_authorization(agent_enabled=False)),
    ]
    payloads: set[str] = set()
    for configurable in failures:
        decision = evaluate_config_authorization(
            configurable,
            authorization_required=True,
            expected_revision="rev-1",
        )
        assert decision is not None
        payloads.add(decision.model_dump_json())
    out_of_scope = evaluate_config_authorization(
        _configurable(_authorization("rev-1")),
        authorization_required=True,
        expected_revision="rev-1",
        requested_scope_level="team",
        requested_scope_id="area-1",
    )
    assert out_of_scope is not None
    payloads.add(out_of_scope.model_dump_json())
    assert payloads == {_CANONICAL_DENY}


# --- execution receipt binding ---------------------------------------------


def test_revision_reaches_the_execution_receipt() -> None:
    receipt = _receipt()
    bound = bind_execution_receipt_authorization(
        receipt,
        _configurable(_authorization("rev-9")),
    )
    assert bound.authorization_revision == "rev-9"
    assert receipt.authorization_revision is None
    assert bound is not receipt


def test_receipt_is_unchanged_when_no_authorization_is_carried() -> None:
    receipt = _receipt()
    assert bind_execution_receipt_authorization(receipt, {}) is receipt
    assert receipt.authorization_revision is None


def test_receipt_binding_uses_the_active_runtime_config() -> None:
    from langchain_core.runnables.config import var_child_runnable_config

    config = runtime_config(_context(_authorization("rev-11")))
    token = var_child_runnable_config.set(config)
    try:
        bound = bind_execution_receipt_authorization(_receipt(), runtime_configurable())
    finally:
        var_child_runnable_config.reset(token)
    assert bound.authorization_revision == "rev-11"
