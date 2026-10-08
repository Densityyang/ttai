"""Pure unit tests for the P2-S1 authorization contract skeleton.

These tests define their own local test doubles and never touch Docker,
network access, tests/metric_fixtures.py, or any existing test.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from src.core.auth.provider import BackendAuthorizationProvider, load_authorization
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import (
    AUTHORIZATION_DENIED,
    AUTHORIZATION_DENIED_REASON,
    AuthorizationContext,
    AuthorizationDecision,
    evaluate_authorization,
)

USER = AuthUser(
    user_id="u-1",
    telephone=None,
    roles=["analyst"],
    permissions=["nl2sql:invoke"],
)


def _context(**overrides: object) -> AuthorizationContext:
    values: dict[str, object] = {
        "authorization_revision": "rev-1",
        "agent_enabled": True,
        "scope_level": "area",
        "allowed_scope_ids": ("area-1", "area-2"),
    }
    values.update(overrides)
    return AuthorizationContext(**values)


class _StubProvider:
    """Local test double; the central fixture module is untouched."""

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls = 0

    async def load(self, user: AuthUser) -> AuthorizationContext | None:
        del user
        self.calls += 1
        return self._result


class _RaisingProvider:
    async def load(self, user: AuthUser) -> AuthorizationContext | None:
        del user
        raise RuntimeError("backend authorization unavailable")


class _LookalikeContext:
    """Not an AuthorizationContext, but exposes lookalike attributes."""

    authorization_revision = "rev-1"
    agent_enabled = True
    scope_level = "area"
    allowed_scope_ids = ("area-1",)


# --- a. strictness ---------------------------------------------------------


def test_context_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _context(org_type="department")


def test_context_is_frozen() -> None:
    context = _context()
    with pytest.raises(ValidationError):
        context.agent_enabled = False


def test_context_rejects_list_scope_ids() -> None:
    with pytest.raises(ValidationError):
        _context(allowed_scope_ids=["area-1"])


def test_context_rejects_blank_scope_ids() -> None:
    with pytest.raises(ValidationError):
        _context(allowed_scope_ids=("area-1", " "))


def test_context_rejects_duplicate_scope_ids_within_one_level() -> None:
    # Duplicates inside one AuthorizationContext are the SAME opaque id repeated
    # within the single declared scope_level, so they are rejected.
    with pytest.raises(ValidationError):
        _context(allowed_scope_ids=("shared-id", "shared-id"))


def test_context_makes_no_global_cross_level_uniqueness_claim() -> None:
    # A context never combines multiple levels, so the contract asserts nothing
    # about the same raw string existing at a DIFFERENT scope_level; two
    # separate contexts at different levels may each carry the same id.
    area = _context(scope_level="area", allowed_scope_ids=("12",))
    team = _context(scope_level="team", allowed_scope_ids=("12",))
    assert area.allowed_scope_ids == ("12",)
    assert team.allowed_scope_ids == ("12",)
    assert area.scope_level != team.scope_level


# --- b. closed ScopeLevel vocabulary --------------------------------------


@pytest.mark.parametrize("level", ["city_company", "area", "team", "employee"])
def test_scope_level_accepts_exactly_the_closed_vocabulary(level: str) -> None:
    assert _context(scope_level=level).scope_level == level


@pytest.mark.parametrize("level", ["department", "company", "organization", "EMPLOYEE", ""])
def test_scope_level_rejects_anything_else(level: str) -> None:
    with pytest.raises(ValidationError):
        _context(scope_level=level)


# --- c. opaque authorization revision -------------------------------------


def test_authorization_revision_is_required() -> None:
    with pytest.raises(ValidationError):
        AuthorizationContext(
            agent_enabled=True,
            scope_level="area",
            allowed_scope_ids=("area-1",),
        )


@pytest.mark.parametrize("revision", ["", "   ", "\t"])
def test_authorization_revision_rejects_blank(revision: str) -> None:
    with pytest.raises(ValidationError):
        _context(authorization_revision=revision)


def test_context_schema_has_no_invented_backend_policy_fields() -> None:
    # Guards the removal against silent regression: no Backend/DB evidence
    # establishes a policy-versioning capability, so these must not reappear.
    fields = set(AuthorizationContext.model_fields)
    assert "policy_version" not in fields
    assert "policy_checksum" not in fields
    assert fields == {
        "schema_version",
        "authorization_revision",
        "agent_enabled",
        "scope_level",
        "allowed_scope_ids",
    }


# --- d. empty scope is valid but denies -----------------------------------


def test_empty_allowed_scope_ids_is_valid_but_denies() -> None:
    context = _context(allowed_scope_ids=())
    assert context.allowed_scope_ids == ()
    decision = evaluate_authorization(context, expected_revision="rev-1")
    assert decision.outcome == "deny"
    assert decision == AUTHORIZATION_DENIED


# --- d2. typed scope membership -------------------------------------------


def test_typed_membership_allows_matching_level_and_id() -> None:
    context = _context(scope_level="area", allowed_scope_ids=("12",))
    decision = evaluate_authorization(
        context,
        expected_revision="rev-1",
        requested_scope_level="area",
        requested_scope_id="12",
    )
    assert decision.outcome == "allow"


def test_cross_level_id_collision_is_denied() -> None:
    # area context allows id "12"; asking for TEAM id "12" must NOT satisfy raw
    # string membership, because ids are not assumed globally unique.
    context = _context(scope_level="area", allowed_scope_ids=("12",))
    decision = evaluate_authorization(
        context,
        expected_revision="rev-1",
        requested_scope_level="team",
        requested_scope_id="12",
    )
    assert decision == AUTHORIZATION_DENIED


def test_requested_id_without_a_level_is_denied() -> None:
    decision = evaluate_authorization(
        _context(),
        expected_revision="rev-1",
        requested_scope_id="area-1",
    )
    assert decision == AUTHORIZATION_DENIED


def test_requested_level_without_an_id_is_denied() -> None:
    decision = evaluate_authorization(
        _context(),
        expected_revision="rev-1",
        requested_scope_level="area",
    )
    assert decision == AUTHORIZATION_DENIED


# --- e. fail-closed fixtures ----------------------------------------------


async def test_unavailable_provider_denies() -> None:
    decision = await load_authorization(_StubProvider(None), USER, expected_revision="rev-1")
    assert decision == AUTHORIZATION_DENIED


async def test_raising_provider_denies_without_propagating() -> None:
    decision = await load_authorization(_RaisingProvider(), USER, expected_revision="rev-1")
    assert decision == AUTHORIZATION_DENIED


async def test_malformed_provider_payload_denies() -> None:
    malformed = {"authorization_revision": "rev-1", "agent_enabled": True}
    decision = await load_authorization(_StubProvider(malformed), USER, expected_revision="rev-1")
    assert decision == AUTHORIZATION_DENIED


def test_resumed_request_allows_on_a_matching_bound_revision() -> None:
    decision = evaluate_authorization(
        _context(authorization_revision="rev-1"),
        expected_revision="rev-1",
    )
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


def test_stale_resumed_request_denies_on_a_mismatched_bound_revision() -> None:
    decision = evaluate_authorization(
        _context(authorization_revision="rev-1"),
        expected_revision="rev-2",
    )
    assert decision == AUTHORIZATION_DENIED


def test_initial_request_allows_without_a_bound_revision() -> None:
    # First request: no revision has been bound yet, so the fresh context's own
    # authorization_revision is authoritative and absence is not a denial.
    decision = evaluate_authorization(
        _context(),
        expected_revision=None,
        requested_scope_level="area",
        requested_scope_id="area-1",
    )
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


def test_initial_request_allows_without_a_requested_scope_id() -> None:
    decision = evaluate_authorization(_context(), expected_revision=None)
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


def test_disabled_agent_denies() -> None:
    decision = evaluate_authorization(_context(agent_enabled=False), expected_revision="rev-1")
    assert decision == AUTHORIZATION_DENIED


def test_in_scope_allows_against_the_matching_revision() -> None:
    decision = evaluate_authorization(
        _context(),
        expected_revision="rev-1",
        requested_scope_level="area",
        requested_scope_id="area-1",
    )
    assert decision.outcome == "allow"
    assert decision.authorization_revision == "rev-1"


async def test_forged_client_authorization_is_ignored() -> None:
    # A forged context claiming an out-of-band revision and a wide scope never
    # matches the server-derived expected revision, so it collapses to deny.
    forged = _context(
        authorization_revision="forged",
        allowed_scope_ids=("area-9",),
    )
    decision = await load_authorization(
        _StubProvider(forged),
        USER,
        expected_revision="rev-1",
        requested_scope_level="area",
        requested_scope_id="area-9",
    )
    assert decision == AUTHORIZATION_DENIED


async def test_unavailable_and_out_of_scope_are_byte_identical() -> None:
    unavailable = await load_authorization(_StubProvider(None), USER, expected_revision="rev-1")
    out_of_scope = evaluate_authorization(
        _context(allowed_scope_ids=("area-1",)),
        expected_revision="rev-1",
        requested_scope_level="area",
        requested_scope_id="area-2",
    )
    assert unavailable.model_dump_json() == out_of_scope.model_dump_json()
    assert unavailable.reason == AUTHORIZATION_DENIED_REASON


@pytest.mark.parametrize(
    "malformed",
    [
        {"authorization_revision": "rev-1", "agent_enabled": True},
        "rev-1",
        42,
        _LookalikeContext(),
    ],
)
def test_malformed_context_denies_without_raising(malformed: object) -> None:
    decision = evaluate_authorization(malformed, expected_revision="rev-1")
    assert decision == AUTHORIZATION_DENIED
    assert decision.reason == AUTHORIZATION_DENIED_REASON


async def test_every_public_deny_serializes_byte_identically() -> None:
    denies = [
        await load_authorization(_StubProvider(None), USER, expected_revision="rev-1"),
        await load_authorization(_RaisingProvider(), USER, expected_revision="rev-1"),
        await load_authorization(
            _StubProvider({"authorization_revision": "rev-1"}),
            USER,
            expected_revision="rev-1",
        ),
        evaluate_authorization(None, expected_revision="rev-1"),
        evaluate_authorization({"a": 1}, expected_revision="rev-1"),
        evaluate_authorization(_context(agent_enabled=False), expected_revision="rev-1"),
        evaluate_authorization(_context(allowed_scope_ids=()), expected_revision="rev-1"),
        evaluate_authorization(
            _context(authorization_revision="rev-1"),
            expected_revision="rev-2",
        ),
        evaluate_authorization(
            _context(allowed_scope_ids=("area-1",)),
            expected_revision="rev-1",
            requested_scope_level="area",
            requested_scope_id="area-2",
        ),
        evaluate_authorization(_context(), expected_revision="rev-1", requested_scope_id="area-1"),
        evaluate_authorization(_context(), expected_revision="rev-1", requested_scope_level="area"),
    ]
    payloads = {decision.model_dump_json() for decision in denies}
    assert payloads == {
        '{"outcome":"deny","reason":"authorization_denied","authorization_revision":null}'
    }
    assert all(decision == AUTHORIZATION_DENIED for decision in denies)


def test_decision_rejects_partial_deny_or_allow() -> None:
    with pytest.raises(ValidationError):
        AuthorizationDecision(outcome="deny", reason="x", authorization_revision="rev-1")
    with pytest.raises(ValidationError):
        AuthorizationDecision(outcome="deny")
    with pytest.raises(ValidationError):
        AuthorizationDecision(outcome="allow")
    with pytest.raises(ValidationError):
        AuthorizationDecision(outcome="allow", reason="x", authorization_revision="rev-1")


def test_decision_rejects_non_canonical_reason() -> None:
    with pytest.raises(ValidationError):
        AuthorizationDecision(outcome="deny", reason="resource_not_found")


def test_backend_authorization_provider_declares_async_load() -> None:
    assert inspect.iscoroutinefunction(BackendAuthorizationProvider.load)
