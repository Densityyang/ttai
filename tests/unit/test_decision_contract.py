"""Typed clarification / decision / resume contracts (contract-only slice)."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from typing import get_args
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import (
    ContextBundle,
    PlanValidationIssue,
    PlanValidationRecord,
    QueryPlan,
    TimeRange,
)
from src.nl2sql.orchestration import decision_contract as dc
from src.nl2sql.orchestration.decision_contract import (
    FORBIDDEN_DECISION_FIELDS,
    RESERVED_SLOT_NAMES,
    DecisionAction,
    DecisionContractError,
    DecisionKind,
    HITLDecision,
    HITLRequest,
    SlotBinding,
    clarification_request,
    resume_token,
    revalidate_decision,
    revalidate_request,
    revalidate_resume_token,
)
from src.nl2sql.semantic.calculation_contract import FORBIDDEN_AUTHORITY_FIELDS

RELEASE = UUID("11111111-1111-1111-1111-111111111111")
SNAP = UUID("22222222-2222-2222-2222-222222222222")
POLICY_CHECKSUM = "a" * 64
REQUEST_ID = "clarify-" + "a" * 32


def _context(**overrides: object) -> ContextBundle:
    base: dict[str, object] = {
        "semantic_release_id": RELEASE,
        "schema_snapshot_id": SNAP,
        "domains": ("complaint",),
        "asset_ids": ("metric.complaint_rate",),
        "resolution_status": "incomplete",
        "unresolved_slots": ("time",),
    }
    base.update(overrides)
    return ContextBundle(**base)


def _plan(**overrides: object) -> QueryPlan:
    base: dict[str, object] = {
        "intent": "metric",
        "domain": "complaint",
        "metric_keys": ("metric.complaint_rate",),
        "time_range": TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        "grain": "month",
        "source_strategy": "aggregate_first",
        "unresolved_slots": ("dimension",),
    }
    base.update(overrides)
    return QueryPlan(**base)


def _validation(
    context: ContextBundle,
    plan: QueryPlan,
    *,
    outcome: str = "clarify",
    issues: tuple[PlanValidationIssue, ...] | None = None,
    plan_sha256: str | None = None,
    context_checksum: str | None = None,
) -> PlanValidationRecord:
    if issues is None:
        issues = (
            PlanValidationIssue(
                code="query_plan_unresolved_slots",
                path="query_plan.unresolved_slots",
                safe_message="The query requires clarification before execution.",
            ),
        )
    return PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum=POLICY_CHECKSUM,
        outcome=outcome,
        query_plan_sha256=plan_sha256 or plan.checksum,
        context_checksum=context_checksum or context.checksum,
        issues=issues,
    )


def _request(**overrides: object) -> HITLRequest:
    base: dict[str, object] = {
        "request_id": REQUEST_ID,
        "decision_kind": "clarification",
        "version": 1,
        "allowed_actions": ("resolve", "choose", "reject", "cancel"),
        "plan_sha256": "b" * 64,
        "context_checksum": "c" * 64,
        "policy_version": "plan-validation.bootstrap.v1",
        "policy_checksum": POLICY_CHECKSUM,
        "issue_codes": ("query_plan_unresolved_slots",),
        "unresolved_slots": ("time",),
    }
    base.update(overrides)
    return HITLRequest(**base)


def _decision(**overrides: object) -> HITLDecision:
    base: dict[str, object] = {
        "request_id": REQUEST_ID,
        "request_version": 1,
        "action": "resolve",
        "slot_bindings": (SlotBinding(slot="time", value="2026-08"),),
        "idempotency_key": "idem-1",
    }
    base.update(overrides)
    return HITLDecision(**base)


# 1, 2 ------------------------------------------------------------------------
def test_decision_kind_vocabulary_is_exact() -> None:
    assert set(get_args(DecisionKind)) == {
        "clarification",
        "business_confirmation",
        "risk_policy_decision",
    }


def test_decision_action_vocabulary_is_exact() -> None:
    assert set(get_args(DecisionAction)) == {
        "resolve",
        "confirm",
        "modify",
        "choose",
        "reject",
        "cancel",
    }


# 3 ---------------------------------------------------------------------------
def test_action_restrictions_by_decision_kind() -> None:
    assert _request().decision_kind == "clarification"
    _request(
        decision_kind="business_confirmation",
        allowed_actions=("confirm", "modify", "reject", "cancel"),
        unresolved_slots=(),
        issue_codes=("custom_metric_plan_confirmation",),
    )
    _request(
        decision_kind="risk_policy_decision",
        allowed_actions=("confirm", "reject", "cancel"),
        unresolved_slots=(),
        issue_codes=("high_risk_execution",),
    )
    with pytest.raises(ValidationError):
        _request(
            decision_kind="risk_policy_decision",
            allowed_actions=("resolve",),
            unresolved_slots=(),
            issue_codes=("high_risk_execution",),
        )
    with pytest.raises(ValidationError):
        _request(allowed_actions=("resolve", "confirm"))


# 4 ---------------------------------------------------------------------------
def test_contracts_are_strict_frozen_and_extra_forbidden() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        _request(bogus_field=1)
    with pytest.raises(ValidationError):
        request.version = 2  # type: ignore[misc]
    decision = _decision()
    with pytest.raises(ValidationError):
        HITLDecision(
            request_id=REQUEST_ID,
            request_version=1,
            action="resolve",
            idempotency_key="idem-1",
            unknown=1,
        )
    with pytest.raises(ValidationError):
        decision.action = "confirm"  # type: ignore[misc]


# 5 ---------------------------------------------------------------------------
def test_duplicate_slot_bindings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _decision(
            slot_bindings=(
                SlotBinding(slot="time", value="2026-08"),
                SlotBinding(slot="time", value="2026-09"),
            )
        )


# 6 ---------------------------------------------------------------------------
def test_clarification_requires_unresolved_slots_and_issues() -> None:
    with pytest.raises(ValidationError):
        _request(unresolved_slots=())
    with pytest.raises(ValidationError):
        _request(issue_codes=())
    with pytest.raises(ValidationError):
        _request(
            decision_kind="business_confirmation",
            allowed_actions=("confirm", "reject"),
            unresolved_slots=("time",),
            issue_codes=("custom_metric_plan_confirmation",),
        )


# 7, 8 ------------------------------------------------------------------------
def test_resolve_requires_bindings_and_non_resolution_actions_reject_them() -> None:
    request = _request()
    missing = _decision(slot_bindings=())
    assert "decision_resolution_payload_missing" in missing.validate_against(request)
    unknown = _decision(slot_bindings=(SlotBinding(slot="dimension", value="area"),))
    assert "decision_unknown_slot_binding" in unknown.validate_against(request)
    reject = _decision(
        action="reject",
        slot_bindings=(SlotBinding(slot="time", value="2026-08"),),
    )
    assert "decision_unexpected_resolution_payload" in reject.validate_against(request)
    cancel = _decision(
        action="cancel",
        slot_bindings=(SlotBinding(slot="time", value="2026-08"),),
    )
    assert "decision_unexpected_resolution_payload" in cancel.validate_against(request)
    assert _decision().validate_against(request) == ()
    stale = _decision(request_version=2)
    assert "decision_request_version_stale" in stale.validate_against(request)


# 9, 10 -----------------------------------------------------------------------
def test_request_checksum_is_deterministic_and_identity_sensitive() -> None:
    assert _request().checksum == _request().checksum
    base = _request().checksum
    assert _request(plan_sha256="d" * 64).checksum != base
    assert _request(context_checksum="e" * 64).checksum != base
    assert _request(policy_checksum="f" * 64).checksum != base
    assert _request(policy_version="other.policy.v2").checksum != base
    assert _request(version=2).checksum != base


# 11, 12 ----------------------------------------------------------------------
def test_resume_token_binds_exact_suspended_state() -> None:
    request = _request()
    decision = _decision()
    token = resume_token(request=request, decision=decision)
    assert token.validate_against(request, decision) == ()
    assert token.checksum == resume_token(request=request, decision=decision).checksum
    wrong_plan = token.model_copy(update={"plan_sha256": "d" * 64})
    assert "resume_plan_mismatch" in wrong_plan.validate_against(request, decision)
    wrong_context = token.model_copy(update={"context_checksum": "e" * 64})
    assert "resume_context_mismatch" in wrong_context.validate_against(request, decision)
    wrong_policy = token.model_copy(update={"policy_checksum": "f" * 64})
    assert "resume_policy_checksum_mismatch" in wrong_policy.validate_against(
        request, decision
    )
    wrong_version = token.model_copy(update={"request_version": 2})
    assert "resume_request_version_mismatch" in wrong_version.validate_against(
        request, decision
    )
    wrong_decision = token.model_copy(update={"decision_checksum": "0" * 64})
    assert "resume_decision_mismatch" in wrong_decision.validate_against(
        request, decision
    )
    with pytest.raises(DecisionContractError):
        resume_token(request=request, decision=_decision(action="reject"))


# 13, 14, 15 ------------------------------------------------------------------
def test_clarification_projection_rejects_non_clarify_validation() -> None:
    context, plan = _context(), _plan()
    denied = _validation(context, plan, outcome="deny")
    with pytest.raises(DecisionContractError):
        clarification_request(validation=denied, context=context, plan=plan)


def test_clarification_projection_rejects_plan_hash_mismatch() -> None:
    context, plan = _context(), _plan()
    bad = _validation(context, plan, plan_sha256="0" * 64)
    with pytest.raises(DecisionContractError):
        clarification_request(validation=bad, context=context, plan=plan)


def test_clarification_projection_rejects_context_checksum_mismatch() -> None:
    context, plan = _context(), _plan()
    bad = _validation(context, plan, context_checksum="0" * 64)
    with pytest.raises(DecisionContractError):
        clarification_request(validation=bad, context=context, plan=plan)


# 16, 17 ----------------------------------------------------------------------
def test_projection_unions_slots_deterministically_and_keeps_issue_codes() -> None:
    context = _context(unresolved_slots=("time",))
    plan = _plan(unresolved_slots=("dimension", "time"))
    issues = (
        PlanValidationIssue(
            code="semantic_context_incomplete",
            path="context.resolution_status",
            safe_message="Semantic context is not complete enough to execute.",
        ),
        PlanValidationIssue(
            code="query_plan_unresolved_slots",
            path="query_plan.unresolved_slots",
            safe_message="The query requires clarification before execution.",
        ),
    )
    validation = _validation(context, plan, issues=issues)
    request = clarification_request(validation=validation, context=context, plan=plan)
    assert request.decision_kind == "clarification"
    assert request.unresolved_slots == ("time", "dimension")
    assert request.issue_codes == (
        "semantic_context_incomplete",
        "query_plan_unresolved_slots",
    )
    assert request.plan_sha256 == plan.checksum
    assert request.context_checksum == context.checksum
    assert request.policy_version == validation.policy_version
    assert request.policy_checksum == validation.policy_checksum
    assert request == clarification_request(
        validation=validation, context=context, plan=plan
    )


def test_projection_fails_closed_without_an_actual_slot() -> None:
    context = _context(unresolved_slots=())
    plan = _plan(unresolved_slots=())
    validation = _validation(
        context,
        plan,
        issues=(
            PlanValidationIssue(
                code="semantic_context_incomplete",
                path="context.resolution_status",
                safe_message="Semantic context is not complete enough to execute.",
            ),
        ),
    )
    with pytest.raises(DecisionContractError):
        clarification_request(validation=validation, context=context, plan=plan)


# 18, 19 ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "field",
    ["canonical", "authority", "approved", "product_mode", "capability"],
)
def test_no_authority_or_mode_fields(field: str) -> None:
    assert field in FORBIDDEN_DECISION_FIELDS
    with pytest.raises(ValidationError):
        _request(**{field: "x"})


@pytest.mark.parametrize("field", ["sql", "code", "password", "authorization"])
def test_no_sql_code_credential_or_authorization_fields(field: str) -> None:
    assert field in FORBIDDEN_DECISION_FIELDS
    with pytest.raises(ValidationError):
        _decision(**{field: "x"})


def test_slot_values_reject_sql_markers_and_unbounded_values() -> None:
    with pytest.raises(ValidationError):
        SlotBinding(slot="time", value="select 1; drop table t")
    with pytest.raises(ValidationError):
        SlotBinding(slot="time", value="x" * 1000)
    with pytest.raises(ValidationError):
        SlotBinding(slot="time", value=Decimal("1E+999999999"))
    with pytest.raises(ValidationError):
        SlotBinding(slot="time", value=10**100)
    assert SlotBinding(slot="time", value=("a", "b")).value == ("a", "b")


# 20 --------------------------------------------------------------------------
def test_contract_module_is_isolated_from_engine_and_v2() -> None:
    source = inspect.getsource(dc)
    assert "orchestration.engine" not in source
    assert "src.nl2sql.v2" not in source
    assert "import engine" not in source


def test_clarification_projection_is_pure() -> None:
    context, plan = _context(), _plan()
    validation = _validation(context, plan)
    before = (context.checksum, plan.checksum)
    clarification_request(validation=validation, context=context, plan=plan)
    assert (context.checksum, plan.checksum) == before


# --- Phase A hardening --------------------------------------------------------
def test_forbidden_decision_vocabulary_covers_shared_authority_vocabulary() -> None:
    assert FORBIDDEN_AUTHORITY_FIELDS <= FORBIDDEN_DECISION_FIELDS
    for name in (
        "required_permissions",
        "data_scope",
        "policy_override",
        "authorization_source",
        "template_authority",
        "egress_policy",
        "execution_capability",
    ):
        assert name in FORBIDDEN_DECISION_FIELDS


@pytest.mark.parametrize(
    "reserved",
    [
        "authorization",
        "authorization_revision",
        "permissions",
        "required_permissions",
        "roles",
        "scope",
        "scope_level",
        "data_scope",
        "resource_scope",
        "subject",
        "product_mode",
        "mode",
        "capability",
        "codeact",
        "canonical",
        "authority",
        "policy_override",
        "model_input_policy",
        "egress",
        "sql",
        "code",
        "credentials",
        "token",
    ],
)
def test_reserved_slot_names_are_rejected(reserved: str) -> None:
    assert reserved in RESERVED_SLOT_NAMES
    with pytest.raises(ValidationError):
        SlotBinding(slot=reserved, value="x")
    with pytest.raises(ValidationError):
        _request(unresolved_slots=(reserved,))


@pytest.mark.parametrize(
    "business", ["time", "dimension", "metric", "grain", "store", "area"]
)
def test_ordinary_business_slot_names_are_accepted(business: str) -> None:
    assert business not in RESERVED_SLOT_NAMES
    assert SlotBinding(slot=business, value="x").slot == business
    assert _request(unresolved_slots=(business,)).unresolved_slots == (business,)


def test_issue_codes_are_bounded_and_machine_safe() -> None:
    ok = _request(
        issue_codes=("query_plan_unresolved_slots", "semantic_context_incomplete")
    )
    assert ok.issue_codes == (
        "query_plan_unresolved_slots",
        "semantic_context_incomplete",
    )
    with pytest.raises(ValidationError):
        _request(issue_codes=("x" * 200,))
    with pytest.raises(ValidationError):
        _request(issue_codes=("Has Space",))
    with pytest.raises(ValidationError):
        _request(issue_codes=("ctrl" + chr(7),))


def test_safe_summary_rejects_control_and_escape_characters() -> None:
    with pytest.raises(ValidationError):
        _request(safe_summary="bad" + chr(27) + "[31m")
    with pytest.raises(ValidationError):
        _request(safe_summary="line" + chr(10))
    with pytest.raises(ValidationError):
        _request(safe_summary="del" + chr(127))


def test_safe_summary_does_not_change_request_identity_or_checksum() -> None:
    plain = _request()
    decorated = _request(safe_summary="clarification needed for the time range")
    assert decorated.request_id == plain.request_id
    assert decorated.checksum == plain.checksum
    assert _request(policy_version="other.policy.v2").checksum != plain.checksum
    assert _request(version=2).checksum != plain.checksum


def test_sql_like_literal_text_is_bounded_user_data_not_a_classifier() -> None:
    literal = "select premium customers"
    assert SlotBinding(slot="time", value=literal).value == literal
    with pytest.raises(ValidationError):
        SlotBinding(slot="time", value="select 1; drop table t")


def test_escape_hatch_objects_fail_at_the_revalidation_boundary() -> None:
    request = _request()
    forged = HITLRequest.model_construct(
        schema_version="1.0",
        request_id=request.request_id,
        decision_kind="clarification",
        version=1,
        allowed_actions=("resolve",),
        plan_sha256="not-a-hash",
        context_checksum="c" * 64,
        policy_version="p",
        policy_checksum="a" * 64,
        issue_codes=("x",),
        unresolved_slots=("time",),
    )
    assert forged.plan_sha256 == "not-a-hash"
    with pytest.raises(ValidationError):
        revalidate_request(forged)
    assert revalidate_request(request) == request

    decision = _decision()
    copied = decision.model_copy(update={"idempotency_key": ""})
    with pytest.raises(ValidationError):
        revalidate_decision(copied)
    assert revalidate_decision(decision) == decision


def test_reserved_slot_names_cover_the_control_plane_vocabulary() -> None:
    assert FORBIDDEN_DECISION_FIELDS <= RESERVED_SLOT_NAMES
    for name in (
        "capabilities",
        "egress_policy",
        "auth_epoch",
        "org",
        "org_id",
        "org_type",
        "build_privilege",
        "sandbox",
        "sql_text",
        "sql_ast",
        "script",
        "private_key",
        "bypass",
        "force",
        "escalate",
        "grant",
        "gold",
        "max_rows",
        "timeout_ms",
        "key",
        "user",
        "username",
        "data_classification",
        "target_provider",
        "alias",
        "execution_capability",
        "codeact_mode",
        "service_mode",
        "tool",
        "tools",
        "connection_string",
        "database",
        "refresh_token",
        "access_token",
        "bearer",
        "jwt",
        "cert",
        "pem",
        "formula",
        "statement",
    ):
        assert name in RESERVED_SLOT_NAMES


def test_revalidate_resume_token_rejects_a_tampered_token() -> None:
    request = _request()
    decision = _decision()
    token = resume_token(request=request, decision=decision)
    assert revalidate_resume_token(token) == token
    tampered = token.model_copy(update={"plan_sha256": "not-a-hash"})
    with pytest.raises(ValidationError):
        revalidate_resume_token(tampered)


def test_model_validate_revalidates_instances() -> None:
    request = _request()
    forged = HITLRequest.model_construct(
        schema_version="1.0",
        request_id=request.request_id,
        decision_kind="clarification",
        version=1,
        allowed_actions=("resolve",),
        plan_sha256="z",
        context_checksum="c" * 64,
        policy_version="p",
        policy_checksum="a" * 64,
        issue_codes=("x",),
        unresolved_slots=("time",),
    )
    with pytest.raises(ValidationError):
        HITLRequest.model_validate(forged)
