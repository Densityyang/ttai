"""Typed clarification / decision / resume control-plane contracts.

TYPED_CLARIFICATION_DECISION_RESUME_CONTRACT_V1.

This module defines strict, frozen typed objects for resumable governed
decisions plus the pure producers that create them: a clarification request
from an existing clarify validation record, and business-confirmation /
risk-policy requests from a validated plan+context and explicit policy
identity.  It is CONTRACT + PURE PRODUCER ONLY: it performs no I/O, mutates no
engine graph, and wires nothing into LangGraph, /actions, checkpoints or
execution.

INVARIANTS
----------
* HITL is reason-oriented, not mode-oriented.  DecisionKind is a separate axis
  from ProductMode and never encodes it.
* A human decision can NEVER grant data/org/relation authorization, bypass
  ModelInputPolicy, authorize prohibited egress, create canonical authority,
  give QUERY CodeAct/BUILD privileges, or override a server policy ceiling.
  Authority/egress/SQL/credential fields are rejected structurally.
* A resume binds the EXACT suspended request/plan/context/policy/version.  It is
  not a fresh reinterpretation of the original natural-language request, and it
  carries no authorization escalation material.
* TRUST BOUNDARY: untrusted or checkpoint-restored payloads MUST be re-validated
  through a normal Pydantic validation boundary (model_validate /
  model_validate_json) before they affect control flow.  model_construct() and
  model_copy() can bypass extra="forbid", the forbidden-field guard and every
  field pattern, so an already-instantiated object is never trusted merely
  because its class name matches.
* SQL PRECISION: this contract provides no SQL/code field and never interprets
  slot text as SQL.  Statement delimiters are rejected as defense-in-depth, NOT
  as a SQL classifier; bounded literal text that merely resembles SQL remains
  valid user data.
* IN-PLACE CORRECTION: decision_kind="metric_plan_confirmation" is the ONE kind
  whose resolution payload is a correction of the run's own derived-calculation
  inputs.  A correction is a CHOICE among server-derived, already-authorized
  candidates carried on the request (resolution_options); it is never free text,
  never a second formula representation, never a new metric identity.  It grants
  no authority, creates no definition, changes no canonicality and reaches no
  definition lifecycle.  Any choice outside the candidate set fails closed, and
  the candidate set is re-derived from the restored plan/context at continuation
  time so a tampered checkpoint cannot widen it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Final, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.nl2sql.contracts import ContextBundle, PlanValidationRecord, QueryPlan
from src.nl2sql.semantic.calculation_contract import FORBIDDEN_AUTHORITY_FIELDS

SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
SlotName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Bounded, machine-safe issue code: no control characters, no huge user/model
# text masquerading as a code.  Existing planning codes remain valid.
IssueCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]

# --- reason vocabulary (never ProductMode) ------------------------------------
DecisionKind = Literal[
    "clarification",
    "business_confirmation",
    "risk_policy_decision",
    "metric_plan_confirmation",
]
DecisionAction = Literal["resolve", "confirm", "modify", "choose", "reject", "cancel"]

# Each request kind restricts which actions are valid; a request may narrow this
# set further but can never widen it.
_REQUEST_ACTIONS: Final[dict[DecisionKind, tuple[DecisionAction, ...]]] = {
    "clarification": ("resolve", "choose", "reject", "cancel"),
    "business_confirmation": ("confirm", "modify", "reject", "cancel"),
    "risk_policy_decision": ("confirm", "reject", "cancel"),
    # The run-scoped FORMULA-PLAN confirmation is the ONE kind whose resolution
    # payload is an IN-PLACE CORRECTION of the run's own derived-calculation
    # inputs.  It is a SEPARATE kind precisely so no frozen action set is
    # widened: the definition/material confirmations above keep their exact
    # vocabulary.  `modify` is ABSENT on purpose - a correction is expressed as
    # `resolve` ONLY, so there is exactly one way to correct and no ambiguity
    # between "record a note" and "change the inputs".  This kind reaches no
    # definition/lifecycle surface and grants no authority.
    "metric_plan_confirmation": ("confirm", "resolve", "reject", "cancel"),
}

# Bounded value limits for resolved slots (fail-closed, pre-coercion).
MAX_SLOT_STRING_LENGTH: Final[int] = 512
MAX_SLOT_ITEMS: Final[int] = 32
MAX_SLOT_INT_MAGNITUDE: Final[int] = 2**63 - 1
MAX_SLOT_DECIMAL_DIGITS: Final[int] = 64
MAX_SLOT_DECIMAL_ADJUSTED: Final[int] = 64

_SQL_MARKERS: Final[tuple[str, ...]] = (";", "--", "/*", "*/")

# Fields a decision/resume object must never carry.  extra="forbid" is the
# PRIMARY closed-schema guarantee; this explicit set is defense-in-depth that
# yields an adversarial fail-closed message.  It is a tested SUPERSET of the
# shared authority/lifecycle vocabulary frozen in calculation_contract, so the
# two lists cannot drift apart.
_DECISION_SPECIFIC_FORBIDDEN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # authority / canonical / definition lifecycle
        "approved",
        "saved",
        "governance_candidate",
        "template_registered",
        "canonical_metric_key",
        "approved_calculation_binding",
        "binding_checksum",
        "definition_id",
        "definition_version",
        "confirmation",
        "confirmed",
        "retention",
        "governance",
        "lifecycle",
        "template_id",
        "template_version",
        "template_checksum",
        "template_authority",
        "binding_revision",
        "output_metric_key",
        "semantic_release_id",
        "semantic_release_checksum",
        "calculation_id",
        "spec_checksum",
        "expression",
        "parameters",
        "inputs",
        "input_roles",
        "metric_keys",
        "metric_key",
        "source_ref",
        "relation",
        "relation_id",
        "execution_plan",
        "query_plan",
        "plan",
        "steps",
        # mode / capability
        "product_mode",
        "mode",
        "capability",
        "capabilities",
        "codeact",
        "codeact_mode",
        "build_privilege",
        "execution_capability",
        "intent",
        "route",
        "build",
        "analyze",
        "service_mode",
        "enable_dynamic_calc",
        "sandbox",
        "tool",
        "tools",
        # authorization / scope / escalation
        "authorization",
        "authorization_revision",
        "authorization_context",
        "authorization_source",
        "allowed_scope_ids",
        "scope",
        "scope_level",
        "data_scope",
        "resource_scope",
        "permissions",
        "required_permissions",
        "permission",
        "roles",
        "role",
        "agent_enabled",
        "subject",
        "identity",
        "user_id",
        "thread_id",
        "run_id",
        "tenant",
        "deployment_scope",
        "auth_epoch",
        "org",
        "org_id",
        "org_type",
        # model input / egress
        "model_input_policy",
        "model_input_policy_version",
        "model_input_policy_checksum",
        "egress",
        "egress_policy",
        "egress_outcome",
        "provider",
        "model",
        "target_provider",
        "target_model",
        "alias",
        "stage",
        "data_classification",
        "base_url",
        "endpoint",
        "url",
        "host",
        "port",
        "messages",
        "tool_schema",
        "prompt",
        "prompt_version",
        "temperature",
        # raw SQL / code / credentials
        "sql",
        "sql_text",
        "sql_ast",
        "sql_fingerprint",
        "query_sql",
        "query",
        "statement",
        "formula",
        "template",
        "code",
        "script",
        "python",
        "bash",
        "powershell",
        "shell",
        "command",
        "exec",
        "eval",
        "import",
        "dsn",
        "connection_string",
        "database",
        "password",
        "passwd",
        "secret",
        "secrets",
        "token",
        "access_token",
        "refresh_token",
        "credential",
        "credentials",
        "api_key",
        "private_key",
        "key",
        "bearer",
        "auth",
        "jwt",
        "cert",
        "pem",
        "username",
        "user",
        # free-form replacement / policy override / authority claims
        "confirmed_by",
        "confirmed_at",
        "approved_by",
        "reviewer",
        "actor",
        "replacement",
        "replace",
        "patch",
        "override",
        "policy_override",
        "bypass",
        "force",
        "escalate",
        "elevate",
        "grant",
        "gold",
        "max_rows",
        "timeout_ms",
    }
)

FORBIDDEN_DECISION_FIELDS: Final[frozenset[str]] = (
    frozenset(FORBIDDEN_AUTHORITY_FIELDS) | _DECISION_SPECIFIC_FORBIDDEN_FIELDS
)

# Control-plane / security vocabulary that must never be used as a slot name.
# Ordinary business slots (time, dimension, metric, grain, store, area) are NOT
# reserved here merely because they may later need domain validation.
RESERVED_SLOT_NAMES: Final[frozenset[str]] = FORBIDDEN_DECISION_FIELDS | frozenset(
    {
        "authorization",
        "authorization_context",
        "authorization_revision",
        "authorization_source",
        "permissions",
        "required_permissions",
        "permission",
        "roles",
        "role",
        "scope",
        "scope_level",
        "data_scope",
        "resource_scope",
        "subject",
        "identity",
        "user_id",
        "thread_id",
        "run_id",
        "tenant",
        "product_mode",
        "mode",
        "capability",
        "codeact",
        "canonical",
        "canonical_metric_key",
        "authority",
        "policy_override",
        "override",
        "model_input_policy",
        "egress",
        "provider",
        "model",
        "sql",
        "code",
        "credentials",
        "credential",
        "token",
        "secret",
        "password",
        "api_key",
        "dsn",
        "confirmed_by",
        "approved",
        "saved",
        "governance",
        "lifecycle",
        "patch",
        "replacement",
    }
)


class DecisionContractError(ValueError):
    """Raised when a decision/resume projection cannot be formed safely."""


class _StrictDecisionModel(BaseModel):
    """Unknown fields are rejected, authority fields explicitly so."""

    # revalidate_instances="always": even an already-instantiated (possibly
    # model_construct()/model_copy()) instance is revalidated when it crosses a
    # normal Pydantic validation boundary.  revalidate_request/_decision/_token
    # additionally dump-then-validate so escape-hatch objects cannot pass.
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, data: object) -> object:
        if isinstance(data, Mapping):
            injected = sorted(FORBIDDEN_DECISION_FIELDS.intersection(data))
            if injected:
                raise ValueError(
                    "decision contract must not carry authority/mode/authorization/"
                    "egress/sql/credential fields: " + ", ".join(injected)
                )
        return data


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _checksum(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _guard_slot_scalar(value: object) -> None:
    """Reject unsafe/unbounded scalar values BEFORE pydantic union coercion.

    Running pre-coercion matters: a large-exponent Decimal must never reach the
    union, which would attempt an exact-int expansion and can hang.
    """

    if isinstance(value, bool):
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("slot value must be finite")
        if len(value.as_tuple().digits) > MAX_SLOT_DECIMAL_DIGITS:
            raise ValueError("slot decimal value has too many digits")
        if value.adjusted() > MAX_SLOT_DECIMAL_ADJUSTED:
            raise ValueError("slot decimal value exponent is too large")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("slot value must be finite")
        return
    if isinstance(value, int):
        if abs(value) > MAX_SLOT_INT_MAGNITUDE:
            raise ValueError("slot integer value is out of bounds")
        return
    if isinstance(value, str):
        if len(value) > MAX_SLOT_STRING_LENGTH:
            raise ValueError("slot string value is too long")
        if any(ord(char) < 32 for char in value):
            raise ValueError("slot value must not contain control characters")
        if any(marker in value for marker in _SQL_MARKERS):
            raise ValueError("slot value must not contain statement delimiters")
        return
    raise ValueError("slot value must be a bounded JSON scalar")


_SlotScalar = str | bool | int | float | Decimal
SlotValue = _SlotScalar | tuple[_SlotScalar, ...]

# Bounded limits for the ONE kind that may carry an in-place correction.
MAX_RESOLUTION_OPTIONS: Final[int] = 16
MAX_RESOLUTION_CANDIDATES: Final[int] = 32
MAX_METRIC_ID_LENGTH: Final[int] = 256
# A metric identity a correction may SELECT.  It is a bounded identifier, never
# free text: the contract never parses it and never treats it as SQL.
AuthorizedMetricId = Annotated[str, Field(min_length=1, max_length=MAX_METRIC_ID_LENGTH)]


class SlotBinding(_StrictDecisionModel):
    """One typed resolved-slot binding.

    Bounded JSON data only.  This contract never interprets slot text as SQL and
    provides no SQL/code field; statement delimiters are rejected as
    defense-in-depth, not as a SQL classifier.
    """

    slot: SlotName
    value: SlotValue
    source: Literal["user", "entity_alias", "semantic_default"] = "user"

    @field_validator("slot")
    @classmethod
    def _slot_is_not_reserved(cls, value: str) -> str:
        if value in RESERVED_SLOT_NAMES:
            raise ValueError("slot name is reserved control-plane vocabulary")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _guard_value(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            if len(value) > MAX_SLOT_ITEMS:
                raise ValueError("slot value list is too long")
            for item in value:
                _guard_slot_scalar(item)
            return value
        _guard_slot_scalar(value)
        return value


class ResolutionOption(_StrictDecisionModel):
    """One bindable role plus the server-derived, ALREADY-AUTHORIZED choices.

    A correction is a CHOICE among these candidates - never free text, never a
    second formula representation and never a new metric identity.  The
    candidates are derived server-side from the run's OWN question-resolved plan
    intersected with the run's authorized context, so:

    * naming them reveals nothing the caller is not already authorized to see;
    * they are exactly the set the governed resolver would accept for this run,
      so a correction can never widen the accessible metric range.

    This object carries no authority, no lifecycle, no canonicality and no
    capability field; `extra="forbid"` plus the forbidden-field guard enforce it.
    """

    slot: SlotName
    candidates: tuple[AuthorizedMetricId, ...] = Field(
        min_length=1, max_length=MAX_RESOLUTION_CANDIDATES
    )

    @field_validator("slot")
    @classmethod
    def _slot_is_not_reserved(cls, value: str) -> str:
        if value in RESERVED_SLOT_NAMES:
            raise ValueError("resolution slot name is reserved control-plane vocabulary")
        return value

    @field_validator("candidates")
    @classmethod
    def _candidates_are_bounded_unique_metric_ids(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for candidate in value:
            if not candidate.strip():
                raise ValueError("resolution candidate must be non-blank")
            if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
                raise ValueError(
                    "resolution candidate must not contain control characters"
                )
            if any(marker in candidate for marker in _SQL_MARKERS):
                raise ValueError(
                    "resolution candidate must not contain statement delimiters"
                )
        if len(set(value)) != len(value):
            raise ValueError("resolution candidates must be unique")
        return value


class HITLRequest(_StrictDecisionModel):
    """A suspended decision bound to the exact validation state.

    Identity/checksum derive from the typed payload, never from model prose.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: RequestId
    decision_kind: DecisionKind
    version: int = Field(ge=1, le=1_000_000)
    allowed_actions: tuple[DecisionAction, ...] = Field(min_length=1, max_length=6)
    plan_sha256: Checksum
    context_checksum: Checksum
    policy_version: str = Field(min_length=1, max_length=128)
    policy_checksum: Checksum
    issue_codes: tuple[IssueCode, ...] = Field(default=(), max_length=32)
    unresolved_slots: tuple[SlotName, ...] = Field(default=(), max_length=16)
    # The correction surface of a metric_plan_confirmation: one bounded entry per
    # BINDABLE formula role, naming the server-derived, already-authorized
    # candidates that role may be re-bound to.  Empty for every other kind.
    resolution_options: tuple[ResolutionOption, ...] = Field(
        default=(), max_length=MAX_RESOLUTION_OPTIONS
    )
    # Display metadata only; never identity and never authority.  Excluded from
    # the request checksum by construction.
    safe_summary: str | None = Field(default=None, max_length=512)

    @field_validator("allowed_actions")
    @classmethod
    def _unique_actions(
        cls, value: tuple[DecisionAction, ...]
    ) -> tuple[DecisionAction, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed actions must be unique")
        return value

    @field_validator("issue_codes")
    @classmethod
    def _unique_issue_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("issue codes must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError("issue codes must be unique")
        return value

    @field_validator("unresolved_slots")
    @classmethod
    def _unique_slots(cls, value: tuple[SlotName, ...]) -> tuple[SlotName, ...]:
        if len(set(value)) != len(value):
            raise ValueError("unresolved slots must be unique")
        reserved = sorted(set(value) & RESERVED_SLOT_NAMES)
        if reserved:
            raise ValueError(
                "unresolved slot name is reserved control-plane vocabulary"
            )
        return value

    @field_validator("resolution_options")
    @classmethod
    def _unique_resolution_slots(
        cls, value: tuple[ResolutionOption, ...]
    ) -> tuple[ResolutionOption, ...]:
        slots = [item.slot for item in value]
        if len(set(slots)) != len(slots):
            raise ValueError("resolution options must be unique by slot")
        return value

    @field_validator("safe_summary")
    @classmethod
    def _safe_summary_has_no_control_characters(
        cls, value: str | None
    ) -> str | None:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("safe summary must not contain control characters")
        return value

    @model_validator(mode="after")
    def _validate_kind(self) -> HITLRequest:
        permitted = set(_REQUEST_ACTIONS[self.decision_kind])
        if not set(self.allowed_actions) <= permitted:
            raise ValueError("allowed actions are not permitted for this decision kind")
        if self.decision_kind == "clarification":
            if not self.unresolved_slots:
                raise ValueError("clarification request requires at least one unresolved slot")
            if not self.issue_codes:
                raise ValueError("clarification request requires at least one issue code")
        elif self.unresolved_slots:
            raise ValueError("non-clarification request must not carry unresolved slots")
        if self.decision_kind == "metric_plan_confirmation":
            # A correction needs something to choose FROM: offering `resolve`
            # without any authorized candidate would be an unbounded action.
            if "resolve" in self.allowed_actions and not self.resolution_options:
                raise ValueError(
                    "a metric plan confirmation may only offer resolve when it "
                    "carries resolution options"
                )
        elif self.resolution_options:
            # The correction surface is scoped to the ONE kind that has it, so no
            # other decision can carry - or be mistaken for - a correction.
            raise ValueError(
                "only a metric plan confirmation may carry resolution options"
            )
        return self

    @property
    def checksum(self) -> str:
        # safe_summary is display metadata only and must never change identity.
        return _checksum(self.model_dump(mode="json", exclude={"safe_summary"}))


class HITLDecision(_StrictDecisionModel):
    """A typed human decision over one suspended request."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: RequestId
    request_version: int = Field(ge=1, le=1_000_000)
    action: DecisionAction
    slot_bindings: tuple[SlotBinding, ...] = Field(default=(), max_length=16)
    idempotency_key: IdempotencyKey

    @field_validator("slot_bindings")
    @classmethod
    def _unique_slot_bindings(
        cls, value: tuple[SlotBinding, ...]
    ) -> tuple[SlotBinding, ...]:
        slots = [item.slot for item in value]
        if len(set(slots)) != len(slots):
            raise ValueError("slot bindings must be unique by slot")
        return value

    def validate_against(self, request: HITLRequest) -> tuple[str, ...]:
        """Action/kind compatibility and resolution-payload rules."""

        failures: list[str] = []
        if self.request_id != request.request_id:
            failures.append("decision_request_id_mismatch")
        if self.request_version != request.version:
            failures.append("decision_request_version_stale")
        if self.action not in request.allowed_actions:
            failures.append("decision_action_not_allowed")
        bound = {item.slot for item in self.slot_bindings}
        if self.action in ("resolve", "choose"):
            if not self.slot_bindings:
                failures.append("decision_resolution_payload_missing")
            if request.decision_kind == "metric_plan_confirmation":
                # The in-place correction is a CHOICE among server-derived,
                # already-authorized candidates.  Fail closed on:
                #   * a slot that is not one of the request's bindable roles;
                #   * a value outside that role's candidate set (this covers
                #     free text AND an unauthorized/nonexistent metric
                #     identically, so nothing is leaked about existence);
                #   * a non-string payload (never free text, never a second
                #     formula representation);
                #   * the same metric selected for two roles (the governed
                #     resolver refuses duplicates, so this fails closed earlier).
                options = {item.slot: item for item in request.resolution_options}
                if bound - set(options):
                    failures.append("decision_unknown_slot_binding")
                selected: list[str] = []
                for item in self.slot_bindings:
                    option = options.get(item.slot)
                    if option is None:
                        continue
                    if not isinstance(item.value, str) or (
                        item.value not in option.candidates
                    ):
                        failures.append("decision_binding_value_not_a_candidate")
                    else:
                        selected.append(item.value)
                if len(set(selected)) != len(selected):
                    failures.append("decision_binding_value_duplicate")
            elif bound - set(request.unresolved_slots):
                failures.append("decision_unknown_slot_binding")
            # A resolve MAY bind a proper subset of the request's unresolved
            # slots: multi-round clarification resolves the remaining slots in
            # later rounds, and the replan re-suspends for whatever is left.
        elif self.slot_bindings:
            failures.append("decision_unexpected_resolution_payload")
        return tuple(dict.fromkeys(failures))

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


class ResumeToken(_StrictDecisionModel):
    """Binds a continuation to the EXACT suspended state.

    Carries no authorization escalation material: the current auth revision is
    an engine-side revalidation concern, never user-supplied authority here.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: RequestId
    request_version: int = Field(ge=1, le=1_000_000)
    plan_sha256: Checksum
    context_checksum: Checksum
    policy_version: str = Field(min_length=1, max_length=128)
    policy_checksum: Checksum
    decision_checksum: Checksum

    def validate_against(
        self, request: HITLRequest, decision: HITLDecision
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if self.request_id != request.request_id:
            failures.append("resume_request_id_mismatch")
        if self.request_version != request.version:
            failures.append("resume_request_version_mismatch")
        if self.plan_sha256 != request.plan_sha256:
            failures.append("resume_plan_mismatch")
        if self.context_checksum != request.context_checksum:
            failures.append("resume_context_mismatch")
        if self.policy_version != request.policy_version:
            failures.append("resume_policy_version_mismatch")
        if self.policy_checksum != request.policy_checksum:
            failures.append("resume_policy_checksum_mismatch")
        if self.decision_checksum != decision.checksum:
            failures.append("resume_decision_mismatch")
        return tuple(dict.fromkeys(failures))

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))


# --- trust-boundary revalidation ---------------------------------------------

ModelT = TypeVar("ModelT", bound=BaseModel)


def _revalidate(model: type[ModelT], payload: object) -> ModelT:
    """Re-validate a payload through the normal Pydantic boundary.

    Untrusted or checkpoint-restored payloads MUST cross this boundary before
    they can affect control flow.  A checkpoint dict OR an already-instantiated
    model (possibly built with model_construct()/model_copy()) is dumped and
    re-validated, so escape-hatch objects cannot bypass extra="forbid", the
    forbidden-field guard or any field pattern.
    """

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return model.model_validate(payload)


def revalidate_request(payload: object) -> HITLRequest:
    """Re-validate a request payload through the normal Pydantic boundary."""

    return _revalidate(HITLRequest, payload)


def revalidate_decision(payload: object) -> HITLDecision:
    """Re-validate a decision payload through the normal Pydantic boundary."""

    return _revalidate(HITLDecision, payload)


def revalidate_resume_token(payload: object) -> ResumeToken:
    """Re-validate a resume-token payload through the normal Pydantic boundary.

    A ResumeToken is server-derived REPLAY EVIDENCE only, never a bearer
    capability; revalidating it keeps a tampered checkpoint record from being
    trusted.
    """

    return _revalidate(ResumeToken, payload)


# --- pure projections ---------------------------------------------------------


def clarification_request(
    *,
    validation: PlanValidationRecord,
    context: ContextBundle,
    plan: QueryPlan,
    version: int = 1,
) -> HITLRequest:
    """Pure projection: a clarify validation record -> typed HITLRequest.

    No I/O and no authority.  Fails closed when the validation is not a clarify,
    when the bound hashes do not match the supplied plan/context, or when no
    unresolved slot actually exists.  business_confirmation_request and
    risk_policy_decision_request are the sibling producers for the other kinds.
    """

    if validation.outcome != "clarify":
        raise DecisionContractError(
            "clarification request requires a clarify validation record"
        )
    if validation.query_plan_sha256 != plan.checksum:
        raise DecisionContractError("clarification request plan hash mismatch")
    if validation.context_checksum != context.checksum:
        raise DecisionContractError("clarification request context checksum mismatch")
    slots = tuple(dict.fromkeys((*context.unresolved_slots, *plan.unresolved_slots)))
    if not slots:
        raise DecisionContractError(
            "clarification request requires at least one unresolved slot"
        )
    issue_codes = tuple(dict.fromkeys(issue.code for issue in validation.issues))
    identity = {
        "decision_kind": "clarification",
        "version": version,
        "plan_sha256": validation.query_plan_sha256,
        "context_checksum": validation.context_checksum,
        "policy_version": validation.policy_version,
        "policy_checksum": validation.policy_checksum,
        "issue_codes": list(issue_codes),
        "unresolved_slots": list(slots),
    }
    return HITLRequest(
        request_id="clarify-" + _checksum(identity)[:32],
        decision_kind="clarification",
        version=version,
        allowed_actions=_REQUEST_ACTIONS["clarification"],
        plan_sha256=validation.query_plan_sha256,
        context_checksum=validation.context_checksum,
        policy_version=validation.policy_version,
        policy_checksum=validation.policy_checksum,
        issue_codes=issue_codes,
        unresolved_slots=slots,
    )


def _confirmation_request(
    *,
    decision_kind: Literal["business_confirmation", "risk_policy_decision"],
    plan: QueryPlan,
    context: ContextBundle,
    policy_version: str,
    policy_checksum: str,
    issue_codes: tuple[str, ...],
    version: int = 1,
    safe_summary: str | None = None,
) -> HITLRequest:
    """Shared fail-closed HITLRequest constructor for the two material kinds.

    Reuses HITLRequest's own kind/action/forbidden-field validation instead of
    introducing a second decision interface.
    """

    normalized_codes = tuple(dict.fromkeys(code.strip() for code in issue_codes))
    if not normalized_codes or any(not code for code in normalized_codes):
        raise DecisionContractError(
            "material decision request requires at least one non-blank issue code"
        )
    if re.fullmatch(r"^[0-9a-f]{64}$", policy_checksum) is None:
        raise DecisionContractError(
            "material decision request policy checksum is invalid"
        )
    identity = {
        "decision_kind": decision_kind,
        "version": version,
        "plan_sha256": plan.checksum,
        "context_checksum": context.checksum,
        "policy_version": policy_version,
        "policy_checksum": policy_checksum,
        "issue_codes": list(normalized_codes),
    }
    prefix = "business-" if decision_kind == "business_confirmation" else "risk-"
    return HITLRequest(
        request_id=prefix + _checksum(identity)[:32],
        decision_kind=decision_kind,
        version=version,
        allowed_actions=_REQUEST_ACTIONS[decision_kind],
        plan_sha256=plan.checksum,
        context_checksum=context.checksum,
        policy_version=policy_version,
        policy_checksum=policy_checksum,
        issue_codes=normalized_codes,
        unresolved_slots=(),
        safe_summary=safe_summary,
    )


def business_confirmation_request(
    *,
    plan: QueryPlan,
    context: ContextBundle,
    policy_version: str,
    policy_checksum: str,
    issue_codes: tuple[str, ...],
    version: int = 1,
    safe_summary: str | None = None,
) -> HITLRequest:
    """Pure producer: a material business-plan confirmation -> HITLRequest.

    This is the missing producer for decision_kind="business_confirmation".
    It is reason-oriented: the caller must supply at least one bounded issue
    code.  It carries no unresolved slots, no authority, no mode and no
    canonical/definition confirmation; it never replaces a definition
    confirmation and never grants canonical authority.
    """

    return _confirmation_request(
        decision_kind="business_confirmation",
        plan=plan,
        context=context,
        policy_version=policy_version,
        policy_checksum=policy_checksum,
        issue_codes=issue_codes,
        version=version,
        safe_summary=safe_summary,
    )


def risk_policy_decision_request(
    *,
    plan: QueryPlan,
    context: ContextBundle,
    policy_version: str,
    policy_checksum: str,
    issue_codes: tuple[str, ...],
    version: int = 1,
    safe_summary: str | None = None,
) -> HITLRequest:
    """Pure producer: a material risk/sensitivity decision -> HITLRequest.

    This is the missing producer for decision_kind="risk_policy_decision".  It
    only offers confirm/reject/cancel and never a resolution payload, so it can
    never smuggle a data/org/relation authorization through a risk acceptance.
    """

    return _confirmation_request(
        decision_kind="risk_policy_decision",
        plan=plan,
        context=context,
        policy_version=policy_version,
        policy_checksum=policy_checksum,
        issue_codes=issue_codes,
        version=version,
        safe_summary=safe_summary,
    )


def metric_plan_confirmation_request(
    *,
    plan: QueryPlan,
    context: ContextBundle,
    policy_version: str,
    policy_checksum: str,
    issue_codes: tuple[str, ...],
    resolution_options: tuple[ResolutionOption, ...] = (),
    version: int = 1,
    safe_summary: str | None = None,
) -> HITLRequest:
    """Pure producer: the run-scoped FORMULA-PLAN confirmation -> HITLRequest.

    This is the ONE decision kind that may carry an IN-PLACE CORRECTION of the
    run's own derived-calculation inputs, and it exists as a SEPARATE kind so
    that no frozen action set is widened:

    * the correction payload is a CHOICE among server-derived, already-authorized
      candidates (resolution_options), never free text;
    * it grants no authority, creates no definition, changes no canonicality and
      carries no mode/capability/lifecycle field;
    * modify is deliberately NOT offered - resolve is the single way to
      correct, so there is no second, payload-less "change" action;
    * when no role is bindable the request simply narrows to
      confirm/reject/cancel instead of offering an unbounded action.

    Every candidate set is caller-supplied and re-derived from the restored
    plan/context at continuation time, so a tampered checkpoint cannot widen it.
    """

    normalized_codes = tuple(dict.fromkeys(code.strip() for code in issue_codes))
    if not normalized_codes or any(not code for code in normalized_codes):
        raise DecisionContractError(
            "material decision request requires at least one non-blank issue code"
        )
    if re.fullmatch(r"^[0-9a-f]{64}$", policy_checksum) is None:
        raise DecisionContractError(
            "material decision request policy checksum is invalid"
        )
    options = tuple(resolution_options)
    allowed_actions: tuple[DecisionAction, ...] = (
        ("confirm", "resolve", "reject", "cancel")
        if options
        else ("confirm", "reject", "cancel")
    )
    identity = {
        "decision_kind": "metric_plan_confirmation",
        "version": version,
        "plan_sha256": plan.checksum,
        "context_checksum": context.checksum,
        "policy_version": policy_version,
        "policy_checksum": policy_checksum,
        "issue_codes": list(normalized_codes),
        "resolution_options": [item.model_dump(mode="json") for item in options],
    }
    return HITLRequest(
        request_id="metric-" + _checksum(identity)[:32],
        decision_kind="metric_plan_confirmation",
        version=version,
        allowed_actions=allowed_actions,
        plan_sha256=plan.checksum,
        context_checksum=context.checksum,
        policy_version=policy_version,
        policy_checksum=policy_checksum,
        issue_codes=normalized_codes,
        unresolved_slots=(),
        resolution_options=options,
        safe_summary=safe_summary,
    )


def resume_token(*, request: HITLRequest, decision: HITLDecision) -> ResumeToken:
    """Pure projection: a satisfied decision -> replay-bound ResumeToken."""

    failures = decision.validate_against(request)
    if failures:
        raise DecisionContractError(
            "decision does not satisfy the request: " + ",".join(failures)
        )
    return ResumeToken(
        request_id=request.request_id,
        request_version=request.version,
        plan_sha256=request.plan_sha256,
        context_checksum=request.context_checksum,
        policy_version=request.policy_version,
        policy_checksum=request.policy_checksum,
        decision_checksum=decision.checksum,
    )


__all__ = [
    "AuthorizedMetricId",
    "Checksum",
    "DecisionAction",
    "DecisionContractError",
    "DecisionKind",
    "FORBIDDEN_DECISION_FIELDS",
    "HITLDecision",
    "HITLRequest",
    "IdempotencyKey",
    "IssueCode",
    "MAX_METRIC_ID_LENGTH",
    "MAX_RESOLUTION_CANDIDATES",
    "MAX_RESOLUTION_OPTIONS",
    "MAX_SLOT_DECIMAL_ADJUSTED",
    "MAX_SLOT_DECIMAL_DIGITS",
    "MAX_SLOT_INT_MAGNITUDE",
    "MAX_SLOT_ITEMS",
    "MAX_SLOT_STRING_LENGTH",
    "RESERVED_SLOT_NAMES",
    "RequestId",
    "ResolutionOption",
    "ResumeToken",
    "SCHEMA_VERSION",
    "SlotBinding",
    "SlotName",
    "SlotValue",
    "business_confirmation_request",
    "clarification_request",
    "metric_plan_confirmation_request",
    "resume_token",
    "revalidate_decision",
    "risk_policy_decision_request",
    "revalidate_request",
    "revalidate_resume_token",
]
