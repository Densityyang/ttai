"""Versioned API and agent-boundary contracts for the NL2SQL service."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
    derived_output_id,
    referenced_input_roles,
)

ModelStage = Literal["classify", "retrieve", "plan", "generate_sql", "verify", "answer"]
ModelDataClassification = Literal["public", "internal", "confidential", "restricted"]
RouteName = Literal["fast", "standard", "deep"]
PolicyLifecycle = Literal["bootstrap", "calibrated", "frozen"]
SourceDegradation = Literal[
    "aggregate_stale", "aggregate_unknown", "metric_permission_denied",
    "metric_relation_unapproved", "metric_aggregate_coverage_unapproved",
    "metric_aggregate_sensitivity_denied", "metric_column_unapproved",
    "metric_column_type_mismatch", "metric_scan_rows_exceeded", "metric_time_index_required",
    "metric_time_range_too_large", "metric_grain_or_dimension_unsupported",
    "metric_dimension_combination_unsupported", "metric_aggregate_sla_missing",
    "metric_freshness_evidence_invalid", "metric_freshness_authority_mismatch",
]
# Deterministic evidence/verification indication band.  No producer exists for
# the typed execution path yet, so every carrier defaults to absent rather than
# fabricating a band.
ConfidenceBand = Literal["low", "medium", "high"]
# Final Agent-facing scope vocabulary owned by Backend/DB; tt-ai only consumes it.
ScopeLevel = Literal["city_company", "area", "team", "employee"]
# Shared product-mode vocabulary.  MODE is decided by THIS RUN's user intent and
# is independent of definition lifecycle and metric authority: arithmetic,
# comparison, ranking, trend, execution count, Save and SAVED identity never
# determine it.  It is deliberately NOT the same axis as QueryPlan.intent
# (metric/trend/comparison/ranking/detail), which only describes execution
# shape.  P4-Q's observed-mode vocabulary reuses this type rather than keeping a
# second duplicate mode literal.
ProductMode = Literal["QUERY", "ANALYZE", "BUILD"]


class StrictContract(BaseModel):
    """Base model that rejects unversioned or misspelled client fields."""

    model_config = ConfigDict(extra="forbid")


class RequestIdentity(StrictContract):
    request_id: UUID
    user_id: str = Field(min_length=1, max_length=256)
    roles: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    auth_epoch: int | None = Field(default=None, ge=0)


class RequestContext(StrictContract):
    identity: RequestIdentity
    deployment_scope: Literal["default"] = "default"
    thread_id: UUID
    trace_id: str = Field(min_length=1, max_length=256)
    deadline_ms: int = Field(default=30_000, ge=1, le=120_000)
    # Optional carrier; absence must keep the pre-existing runtime-configurable
    # path unchanged.  When evaluated through the authorization enforcement
    # seam, a supplied, parseable context is always evaluated: a
    # present-but-unusable one (stale revision, disabled, empty or out-of-scope)
    # denies regardless of any requirement flag.  Only when the carrier is
    # absent -- or malformed, which the accessor collapses to absent -- does the
    # requirement flag choose between a fail-closed deny (required) and the
    # existing path (not required).  No active runtime enforcement is claimed
    # here: production does not yet populate this carrier.
    authorization: AuthorizationContext | None = None


class AuthorizationContext(StrictContract):
    """Backend-owned effective authorization snapshot for one Agent request.

    The business fields -- authorization_revision, agent_enabled, scope_level
    and allowed_scope_ids -- are derived server-side from Backend/DB truth;
    tt-ai only consumes the final Agent-facing vocabulary and never
    reinterprets a legacy organization type.  authorization_revision is an
    opaque non-blank token.  schema_version is NOT Backend/DB-derived: it is
    tt-ai contract metadata that versions this Agent-boundary shape itself.

    scope_level is consumed verbatim: tt-ai owes no hierarchy, ancestor,
    sibling or employee-scope derivation, and no user-to-role resolution --
    Backend owns all of it.

    allowed_scope_ids is trusted Backend effective-authorization material for
    exactly ONE declared scope_level.  Membership is tested against exactly the
    IDs supplied: nothing is expanded and nothing is inferred (no area-to-teams,
    no team-to-employees, no siblings, no ancestors, no legacy org_type
    mapping).  The IDs are opaque strings, so the same raw string may also
    legitimately exist at a DIFFERENT scope_level; this contract makes no claim
    of global cross-level uniqueness.  An empty tuple means no effective scope
    at all (deny-by-absence) and never widens access.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    authorization_revision: str = Field(min_length=1, max_length=256)
    agent_enabled: bool
    scope_level: ScopeLevel
    allowed_scope_ids: tuple[str, ...] = ()

    @field_validator("authorization_revision")
    @classmethod
    def validate_authorization_revision(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("authorization revision must be non-blank")
        return value

    @field_validator("allowed_scope_ids")
    @classmethod
    def validate_allowed_scope_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require unique, non-blank IDs within this single declared scope.

        Uniqueness is required WITHIN one declared scope_level.  No claim is
        made that the same raw string cannot also exist at a different
        scope_level; a context never combines multiple levels.
        """

        if any(not item.strip() for item in value):
            raise ValueError("allowed scope identifiers must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("allowed scope identifiers must be unique within a scope")
        return value

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)


# RequestContext is declared before AuthorizationContext, so its forward
# reference is bound here, once AuthorizationContext exists in this namespace.
RequestContext.model_rebuild()


# The one and only PUBLIC deny reason.  Internal audit and metrics MAY record a
# precise cause (provider-unavailable, malformed-backend-response,
# agent-disabled, stale-revision, scope-denied); the user-visible decision must
# not distinguish them.
AUTHORIZATION_DENIED_REASON: Literal["authorization_denied"] = "authorization_denied"


class AuthorizationDecision(StrictContract):
    """Fail-closed authorization outcome taken against one opaque revision.

    A deny never carries a revision and its reason is pinned to the single
    canonical literal, so every PUBLIC deny serializes identically: an
    unauthorized-but-existing resource and a non-existent one are
    indistinguishable to the caller (no existence oracle).  Internal audit and
    metrics MAY distinguish causes; that detail does not belong in this
    user-visible field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["allow", "deny"]
    reason: Literal["authorization_denied"] | None = None
    authorization_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )

    @model_validator(mode="after")
    def validate_decision(self) -> AuthorizationDecision:
        if self.outcome == "deny":
            if self.reason is None:
                raise ValueError("deny decision requires a reason")
            if self.authorization_revision is not None:
                raise ValueError("deny decision must not carry a revision")
        elif self.reason is not None:
            raise ValueError("allow decision cannot carry a reason")
        elif self.authorization_revision is None:
            raise ValueError("allow decision requires an authorization revision")
        return self


AUTHORIZATION_DENIED = AuthorizationDecision(
    outcome="deny",
    reason=AUTHORIZATION_DENIED_REASON,
)


def evaluate_authorization(
    context: AuthorizationContext | None,
    *,
    expected_revision: str | None,
    requested_scope_level: ScopeLevel | None = None,
    requested_scope_id: str | None = None,
) -> AuthorizationDecision:
    """Decide one request against the trusted Backend authorization snapshot.

    context must be server-derived.  Any value that is not an
    AuthorizationContext -- None, a malformed payload, or a lookalike object --
    is treated as absent.  The function never raises.

    Two revision cases are distinct:

    * INITIAL REQUEST -- expected_revision is None because no revision has been
      bound yet.  Absence of a prior revision is not a denial: the fresh
      context's authorization_revision is authoritative, so evaluate
      agent_enabled, non-empty allowed_scope_ids and requested-id membership
      normally and allow if they pass.  The returned revision is then
      propagated into plan, receipt and checkpoint artifacts.
    * RESUME / CONTINUATION / HITL -- a previously bound revision IS supplied
      as expected_revision for RUN-BOUND CONSISTENCY.  Authorization is resolved
      once at run start and BOUND to that run; the supplied context must be the
      SAME snapshot originally bound to the run.  A mismatch is a run-binding
      failure.  This is NOT a fresh Backend fetch and must NEVER trigger a
      revocation or a live revalidation of an in-flight run.

    Callers resolve authorization once at run start and BIND that snapshot to the
    run; expected_revision is then the run-bound revision restored from run state
    (None only when no revision was bound).  Passing the context's own revision
    merely to manufacture a fresh check is tautological and is not a substitute
    for run binding.

    Scope membership is TYPED.  A requested_scope_id is evaluated only when the
    matching requested_scope_level is supplied and equals context.scope_level;
    supplying exactly one of the two is a deny.  Raw ids are opaque and are not
    assumed globally unique across levels, so an id alone can never prove
    membership: asking for TEAM id "12" must not match an area context whose
    allowed ids happen to contain "12".  This primitive infers no ancestors,
    descendants, siblings, area-to-team or team-to-employee relation and no
    legacy org_type mapping.  A higher- or lower-level business query is
    authorised later through the trusted Backend effective scope plus
    RelationCoverage and typed relation binding -- never by guessing here.

    Every failure -- absent, malformed, agent-disabled, empty scope,
    stale-revision, an unpaired or cross-level scope request, or a requested id
    outside the trusted set -- returns the same canonical AUTHORIZATION_DENIED,
    so the PUBLIC decision is indistinguishable to the caller.  Internal audit
    and metrics MAY record the precise cause.
    """

    if not isinstance(context, AuthorizationContext) or not context.agent_enabled:
        return AUTHORIZATION_DENIED
    if not context.allowed_scope_ids:
        return AUTHORIZATION_DENIED
    if expected_revision is not None and context.authorization_revision != expected_revision:
        return AUTHORIZATION_DENIED
    if (requested_scope_level is None) != (requested_scope_id is None):
        return AUTHORIZATION_DENIED
    if requested_scope_level is not None and requested_scope_id is not None:
        if requested_scope_level != context.scope_level:
            return AUTHORIZATION_DENIED
        if requested_scope_id not in context.allowed_scope_ids:
            return AUTHORIZATION_DENIED
    return AuthorizationDecision(
        outcome="allow",
        authorization_revision=context.authorization_revision,
    )


class PolicyDecision(StrictContract):
    outcome: Literal["allow", "deny", "approval"]
    max_rows: int | None = Field(default=None, ge=1)
    timeout_ms: int | None = Field(default=None, ge=1)
    data_scope: tuple[str, ...] = ()
    reason: str | None = None


class TimeRange(StrictContract):
    """Inclusive business dates; compilers use [start midnight, end + 1 day)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_order(self) -> TimeRange:
        if self.end < self.start:
            raise ValueError("time range end cannot be before start")
        return self


class BoundFilter(StrictContract):
    """A typed filter bound to a semantic field, never an SQL fragment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_ref: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$",
    )
    operator: Literal[
        "eq",
        "ne",
        "in",
        "between",
        "gt",
        "gte",
        "lt",
        "lte",
        "is_null",
        "is_not_null",
    ]
    value: JsonValue | None = None
    source: Literal["user", "entity_alias", "semantic_default"]

    @model_validator(mode="after")
    def validate_operator_value(self) -> BoundFilter:
        if self.value is not None:
            try:
                json.dumps(self.value, allow_nan=False, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("filter value must be finite JSON") from exc
        if self.operator in {"is_null", "is_not_null"}:
            if self.value is not None:
                raise ValueError(f"{self.operator} filter must not carry a value")
            return self
        if self.value is None:
            raise ValueError(f"{self.operator} filter requires a value")
        if self.operator == "in" and (
            not isinstance(self.value, list) or not self.value
        ):
            raise ValueError("in filter requires a non-empty JSON array")
        if self.operator == "between" and (
            not isinstance(self.value, list) or len(self.value) != 2
        ):
            raise ValueError("between filter requires a two-item JSON array")
        return self


class ContextBundle(StrictContract):
    """Policy-filtered, release-bound semantic context safe for checkpointing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic_release_id: UUID
    schema_snapshot_id: UUID
    domains: tuple[str, ...] = Field(min_length=1, max_length=8)
    asset_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    approved_relation_ids: tuple[str, ...] = Field(default=(), max_length=8)
    approved_edge_ids: tuple[str, ...] = Field(default=(), max_length=16)
    resolution_status: Literal["resolved", "ambiguous", "incomplete", "conflict"]
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=16)
    conflict_ids: tuple[str, ...] = Field(default=(), max_length=16)
    degradation_flags: tuple[str, ...] = Field(default=(), max_length=16)
    token_cost: int = Field(default=0, ge=0, le=10_000)
    evidence_count: int = Field(default=0, ge=0, le=20)

    @field_validator(
        "domains",
        "asset_ids",
        "approved_relation_ids",
        "approved_edge_ids",
        "unresolved_slots",
        "conflict_ids",
        "degradation_flags",
    )
    @classmethod
    def validate_unique_non_empty_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("context identifiers must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("context identifiers must be unique")
        return value

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)


class QueryPlan(StrictContract):
    """Untrusted declarative proposal; it cannot execute without validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["3.0"] = "3.0"
    intent: Literal["metric", "trend", "comparison", "ranking", "detail"]
    domain: str = Field(min_length=1, max_length=128)
    metric_keys: tuple[str, ...] = Field(min_length=1, max_length=16)
    dimensions: tuple[str, ...] = Field(default=(), max_length=16)
    filters: tuple[BoundFilter, ...] = Field(default=(), max_length=32)
    time_range: TimeRange
    # Server-owned sparse business-day availability for bounded recent windows.
    # Empty means an ordinary contiguous time range; non-empty values are an
    # exact typed execution set and are never client authority.
    available_dates: tuple[date, ...] = Field(
        default=(), max_length=31, exclude=True
    )
    grain: Literal["hour", "day", "week", "month", "quarter", "year"]
    source_strategy: Literal["aggregate_first", "detail_required"]
    # Ranking defaults to ten; other intents must leave this unset.
    result_limit: int | None = Field(default=None, strict=True, ge=1, le=100)
    required_permissions: tuple[str, ...] = Field(default=(), max_length=32)
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_result_limit(self) -> QueryPlan:
        if self.intent != "ranking" and self.result_limit is not None:
            raise ValueError("result_limit is only supported for ranking")
        return self

    @model_validator(mode="after")
    def validate_available_dates(self) -> QueryPlan:
        if self.available_dates:
            if self.intent != "trend":
                raise ValueError("available_dates is only supported for trends")
            if len(set(self.available_dates)) != len(self.available_dates):
                raise ValueError("available_dates must be unique")
            if tuple(sorted(self.available_dates)) != self.available_dates:
                raise ValueError("available_dates must be ordered")
            if self.available_dates[0] < self.time_range.start:
                raise ValueError("available_dates start is outside time_range")
            if self.available_dates[-1] > self.time_range.end:
                raise ValueError("available_dates end is outside time_range")
        return self

    @property
    def ranking_limit(self) -> int:
        return self.result_limit if self.result_limit is not None else 10

    @field_validator(
        "metric_keys",
        "dimensions",
        "required_permissions",
        "unresolved_slots",
    )
    @classmethod
    def validate_unique_plan_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("query plan identifiers must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("query plan identifiers must be unique")
        return value

    @property
    def checksum(self) -> str:
        payload = self.model_dump(mode="json")
        if self.available_dates:
            payload["available_dates"] = [item.isoformat() for item in self.available_dates]
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def query_plan_payload(plan: QueryPlan) -> dict[str, JsonValue]:
    """Serialize a plan while retaining the server-owned sparse date set."""

    payload = plan.model_dump(mode="json")
    if plan.available_dates:
        payload["available_dates"] = [item.isoformat() for item in plan.available_dates]
    return payload


PlanStepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
# Bounded NONCANONICAL derived-output identity.  It identifies CalculationSpec
# + CalculationExecutionBinding CONTENT only: never a metric_key, never a
# canonical/SAVED identity, and never a global execution/result id (two runs of
# the same spec+binding may share it over different data).  AnswerFact.fact_id
# remains the concrete grounded-fact identity.
DerivedOutputId = Annotated[str, Field(pattern=r"^adhoc_[0-9a-f]{32}$")]
PlanInputName = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"),
]
PlanInputRef = Annotated[
    str,
    Field(
        max_length=256,
        pattern=(
            r"^[a-z][a-z0-9_-]{0,63}"
            r"(?:\.(?:[A-Za-z_][A-Za-z0-9_-]{0,63}|[0-9]+))*$"
        ),
    ),
]


class FetchMetricStep(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fetch_metric"] = "fetch_metric"
    step_id: PlanStepId
    metric_keys: tuple[str, ...] = Field(min_length=1, max_length=16)
    depends_on: tuple[PlanStepId, ...] = Field(default=(), max_length=16)
    # Compiler-produced, trusted-binding-derived provenance marking this step as
    # an INTERNAL dependency fetch feeding one trusted calculation input role.
    # It never fetches the requested output metric and is never model-authored.
    # All three fields are present together or absent together.
    calculation_input_role: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
    )
    calculation_binding_checksum: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    calculation_output_metric_key: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    # Compiler-produced NONCANONICAL AD_HOC dependency provenance marking this
    # fetch as an INTERNAL AD_HOC calculation input.  It is mutually exclusive
    # with the canonical calculation_input_role group above and never carries a
    # canonical binding checksum.  All three fields are present together or
    # absent together.
    ad_hoc_input_role: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
    )
    ad_hoc_spec_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ad_hoc_derived_output_id: DerivedOutputId | None = None

    @field_validator("metric_keys", "depends_on")
    @classmethod
    def validate_unique_fetch_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("fetch metric values must be unique")
        return value

    @model_validator(mode="after")
    def validate_dependency_fetch_provenance(self) -> FetchMetricStep:
        canonical = (
            self.calculation_input_role,
            self.calculation_binding_checksum,
            self.calculation_output_metric_key,
        )
        ad_hoc = (
            self.ad_hoc_input_role,
            self.ad_hoc_spec_checksum,
            self.ad_hoc_derived_output_id,
        )
        canonical_declared = sum(1 for item in canonical if item is not None)
        ad_hoc_declared = sum(1 for item in ad_hoc if item is not None)
        if canonical_declared not in (0, len(canonical)):
            raise ValueError("dependency fetch provenance must be all-or-none")
        if ad_hoc_declared not in (0, len(ad_hoc)):
            raise ValueError("ad hoc dependency fetch provenance must be all-or-none")
        if canonical_declared and ad_hoc_declared:
            raise ValueError(
                "canonical and ad hoc dependency provenance are mutually exclusive"
            )
        if (canonical_declared or ad_hoc_declared) and len(self.metric_keys) != 1:
            raise ValueError("dependency fetch must project exactly one metric key")
        return self


class TrustedCalculationStep(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["trusted_calculation"] = "trusted_calculation"
    step_id: PlanStepId
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    input_refs: dict[PlanInputName, PlanInputRef] = Field(min_length=1, max_length=32)
    depends_on: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)
    # Compiler-produced, trusted-binding-derived provenance.  Never model-authored.
    template_version: str | None = Field(default=None, min_length=1, max_length=64)
    template_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    binding_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_metric_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("depends_on")
    @classmethod
    def validate_unique_calculation_dependencies(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("calculation dependencies must be unique")
        return value

    @model_validator(mode="after")
    def validate_binding_provenance(self) -> TrustedCalculationStep:
        """Canonical binding provenance is all-or-none.

        All absent is the legacy/unbound representation.  All present is a
        canonical trusted-binding calculation whose ONLY authority is the
        resolved ApprovedCalculationCatalog binding, never the template
        allowlist.  A partial set could be confused with either, so it is
        rejected at contract construction.
        """

        provenance = (
            self.template_version,
            self.template_checksum,
            self.binding_checksum,
            self.output_metric_key,
        )
        declared = sum(1 for item in provenance if item is not None)
        if declared not in (0, len(provenance)):
            raise ValueError("calculation binding provenance must be all-or-none")
        return self


class AdHocCalculationStep(StrictContract):
    """One NONCANONICAL AD_HOC calculation over already-resolved inputs.

    Structurally distinct from TrustedCalculationStep: it carries NO template
    approval, NO canonical output_metric_key and NO binding checksum authority.
    Shared calculation SEMANTICS are reused; authority stays outside.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ad_hoc_calculation"] = "ad_hoc_calculation"
    step_id: PlanStepId
    calculation_spec: CalculationSpec
    execution_binding: CalculationExecutionBinding
    input_refs: dict[PlanInputName, PlanInputRef] = Field(min_length=1, max_length=32)
    depends_on: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)
    derived_output_id: DerivedOutputId

    @field_validator("depends_on")
    @classmethod
    def validate_unique_ad_hoc_dependencies(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("ad hoc calculation dependencies must be unique")
        return value

    @model_validator(mode="after")
    def validate_ad_hoc_semantics(self) -> AdHocCalculationStep:
        spec = self.calculation_spec
        binding = self.execution_binding
        if binding.calculation_id != spec.calculation_id:
            raise ValueError("ad hoc execution binding spec identity mismatch")
        if binding.spec_checksum != spec.checksum:
            raise ValueError("ad hoc execution binding spec checksum mismatch")
        if binding.binding_failures(spec):
            raise ValueError("ad hoc execution binding is not contract-valid")
        roles = set(referenced_input_roles(spec.expression))
        if set(self.input_refs) != roles:
            raise ValueError(
                "ad hoc input refs must match the expression input roles exactly"
            )
        if self.derived_output_id != derived_output_id(spec, binding):
            raise ValueError(
                "ad hoc derived output id must derive from the spec and binding"
            )
        return self


class VerifyStep(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["verify"] = "verify"
    step_id: PlanStepId
    input_refs: tuple[PlanInputRef, ...] = Field(min_length=1, max_length=32)
    invariant_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    depends_on: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)

    @field_validator("input_refs", "invariant_ids", "depends_on")
    @classmethod
    def validate_unique_verify_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("verify values must be unique")
        return value


PlanStep = Annotated[
    FetchMetricStep | TrustedCalculationStep | AdHocCalculationStep | VerifyStep,
    Field(discriminator="kind"),
]


class ExecutionPlan(StrictContract):
    """Registered typed DAG compiled from one validated QueryPlan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_release_id: UUID
    schema_snapshot_id: UUID
    policy_version: str = Field(min_length=1, max_length=128)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_typed_dag(self) -> ExecutionPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("execution plan step ids must be unique")
        known = set(step_ids)
        dependencies: dict[str, set[str]] = {}
        for step in self.steps:
            current = set(step.depends_on)
            if step.step_id in current:
                raise ValueError("execution plan step cannot depend on itself")
            unknown = current - known
            if unknown:
                raise ValueError(f"execution plan dependency is unknown: {sorted(unknown)[0]}")
            dependencies[step.step_id] = current
            refs: tuple[str, ...]
            if isinstance(step, TrustedCalculationStep):
                refs = tuple(step.input_refs.values())
            elif isinstance(step, AdHocCalculationStep):
                refs = tuple(step.input_refs.values())
            elif isinstance(step, VerifyStep):
                refs = step.input_refs
            else:
                refs = ()
            for ref in refs:
                root = ref.split(".", 1)[0]
                if root not in known:
                    raise ValueError(f"execution plan input ref is unknown: {root}")
                if root not in current:
                    raise ValueError(
                        f"execution plan input ref must be declared as a dependency: {root}"
                    )

        resolved: set[str] = set()
        pending = dict(dependencies)
        while pending:
            ready = sorted(step_id for step_id, deps in pending.items() if deps <= resolved)
            if not ready:
                raise ValueError("execution plan dependencies contain a cycle")
            for step_id in ready:
                resolved.add(step_id)
                pending.pop(step_id)
        return self

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)


class PlanValidationIssue(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128)
    path: str = Field(default="", max_length=256)
    safe_message: str = Field(min_length=1, max_length=512)


class PlanValidationRecord(StrictContract):
    """Replayable result of validating a query or compiled execution plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = Field(min_length=1, max_length=128)
    policy_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["allow", "deny", "clarify", "approval"]
    query_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Exact ExecutionPlan content identity when (and only when) this record is
    # the result of validating a compiled execution plan.  Never overloads
    # query_plan_sha256: the two hashes name different artifacts.
    execution_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    issues: tuple[PlanValidationIssue, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_outcome_issues(self) -> PlanValidationRecord:
        if self.outcome == "allow" and self.issues:
            raise ValueError("allow validation record cannot contain issues")
        if self.outcome != "allow" and not self.issues:
            raise ValueError("non-allow validation record must contain issues")
        return self


class PlanStepReceipt(StrictContract):
    """Secret-free execution metadata; row values and SQL are never checkpointed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: PlanStepId
    kind: Literal[
        "fetch_metric", "trusted_calculation", "ad_hoc_calculation", "verify"
    ]
    status: Literal["succeeded", "failed"]
    elapsed_ms: int = Field(ge=0)
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rowset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    source_kind: Literal["approved_aggregate", "approved_detail"] | None = None
    source_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    selection_reason: Literal["fresh_approved_aggregate", "approved_detail_fallback", "approved_detail"] | None = None
    source_degradation: tuple[SourceDegradation, ...] = ()
    source_checkpoint: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    semantic_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Calculation provenance (secret-free; no raw SQL, rowsets or values).
    template_id: str | None = Field(default=None, min_length=1, max_length=128)
    template_version: str | None = Field(default=None, min_length=1, max_length=64)
    binding_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_metric_key: str | None = Field(default=None, min_length=1, max_length=256)
    # NONCANONICAL AD_HOC provenance (never mixed with the canonical fields above).
    calculation_spec_checksum: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    execution_binding_checksum: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    derived_output_id: DerivedOutputId | None = None
    calculation_scope: Literal["ad_hoc_noncanonical"] | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_step_status(self) -> PlanStepReceipt:
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful step cannot contain an error code")
        if self.status == "succeeded" and self.output_digest is None:
            raise ValueError("successful step requires an output digest")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed step requires an error code")
        if self.status == "failed" and self.output_digest is not None:
            raise ValueError("failed step cannot contain an output digest")
        ad_hoc = (
            self.calculation_spec_checksum,
            self.execution_binding_checksum,
            self.derived_output_id,
            self.calculation_scope,
        )
        ad_hoc_declared = sum(1 for item in ad_hoc if item is not None)
        if self.kind == "ad_hoc_calculation":
            if self.status == "succeeded":
                if ad_hoc_declared != len(ad_hoc):
                    raise ValueError(
                        "ad hoc receipt requires full noncanonical provenance"
                    )
                if self.calculation_scope != "ad_hoc_noncanonical":
                    raise ValueError(
                        "ad hoc receipt requires ad_hoc_noncanonical scope"
                    )
            elif ad_hoc_declared:
                # A failed AD_HOC receipt carries ONLY a stable error code.
                raise ValueError(
                    "failed ad hoc receipt must not carry provenance"
                )
            if (
                self.output_metric_key is not None
                or self.binding_checksum is not None
                or self.template_id is not None
                or self.template_version is not None
            ):
                raise ValueError("ad hoc receipt must not carry canonical provenance")
        elif ad_hoc_declared:
            raise ValueError("non-ad-hoc receipt must not carry ad hoc provenance")
        return self


class PlanExecutionRecord(StrictContract):
    """Checkpoint-safe summary emitted by the typed PlanExecutor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["succeeded", "failed", "deadline_exceeded"]
    step_receipts: tuple[PlanStepReceipt, ...] = Field(default=(), max_length=16)
    output_step_ids: tuple[PlanStepId, ...] = Field(default=(), max_length=16)
    stop_reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_execution_status(self) -> PlanExecutionRecord:
        if self.status == "succeeded" and self.stop_reason is not None:
            raise ValueError("successful execution cannot contain a stop reason")
        if self.status != "succeeded" and self.stop_reason is None:
            raise ValueError("failed execution requires a stop reason")
        receipt_ids = tuple(receipt.step_id for receipt in self.step_receipts)
        if len(set(receipt_ids)) != len(receipt_ids):
            raise ValueError("execution step receipts must be unique")
        successful_ids = tuple(
            receipt.step_id
            for receipt in self.step_receipts
            if receipt.status == "succeeded"
        )
        if self.output_step_ids != successful_ids:
            raise ValueError("execution outputs must match successful step receipts")
        if self.status == "succeeded" and (
            not self.step_receipts
            or any(receipt.status != "succeeded" for receipt in self.step_receipts)
        ):
            raise ValueError("successful execution requires only successful step receipts")
        return self


class RoutePolicy(StrictContract):
    """Versioned routing thresholds that can be replayed with a request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=128)
    state: PolicyLifecycle
    fast_max_risk: int = Field(ge=0, le=100)
    fast_min_confidence: float = Field(ge=0, le=1)
    fast_max_tables: int = Field(ge=1)
    standard_max_risk: int = Field(ge=0, le=100)
    standard_min_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> RoutePolicy:
        if self.fast_max_risk > self.standard_max_risk:
            raise ValueError("fast risk threshold cannot exceed standard")
        if self.fast_min_confidence < self.standard_min_confidence:
            raise ValueError("fast confidence threshold cannot be lower than standard")
        return self

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)


class RouteBudget(StrictContract):
    """Hard resource limits for one route under a versioned policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deadline_ms: int = Field(ge=1, le=120_000)
    max_model_calls: int = Field(ge=0)
    max_sql_candidates: int = Field(ge=1)
    max_sql_executions: int = Field(ge=1)
    max_join_hops: int = Field(ge=0)
    max_repairs: int = Field(ge=0)


class RoutingBudgetPolicy(StrictContract):
    """Replayable route budgets; bootstrap values must be calibrated before release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=128)
    state: PolicyLifecycle
    routes: dict[RouteName, RouteBudget]
    reserve_ms: int = Field(ge=1)
    max_same_sql: int = Field(ge=1)
    max_same_error: int = Field(ge=1)
    max_retryable_provider_errors: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_complete_route_policy(self) -> RoutingBudgetPolicy:
        expected = {"fast", "standard", "deep"}
        if set(self.routes) != expected:
            raise ValueError("routing budget policy must define fast, standard, and deep")
        if self.reserve_ms >= min(route.deadline_ms for route in self.routes.values()):
            raise ValueError("response reserve must be lower than every route deadline")
        return self

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)


class RouteBudgetUsage(StrictContract):
    model_calls: int = Field(default=0, ge=0)
    sql_candidates: int = Field(default=0, ge=0)
    sql_executions: int = Field(default=0, ge=0)
    join_hops: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    retryable_provider_errors: int = Field(default=0, ge=0)


class RouteBudgetRecord(StrictContract):
    """Secret-free checkpoint record for route call accounting and loop breakers."""

    policy_version: str = Field(min_length=1, max_length=128)
    policy_state: PolicyLifecycle
    policy_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    route: RouteName
    limits: RouteBudget
    usage: RouteBudgetUsage = Field(default_factory=RouteBudgetUsage)
    sql_fingerprint_counts: dict[str, int] = Field(default_factory=dict)
    error_counts: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("sql_fingerprint_counts", "error_counts")
    @classmethod
    def validate_counter_map(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or count < 1 for key, count in value.items()):
            raise ValueError("counter keys must be non-empty and counts positive")
        return value


class QueryCandidate(StrictContract):
    sql: str = Field(repr=False, exclude=True)
    fingerprint: str
    validation: tuple[str, ...] = ()
    cost: float | None = Field(default=None, ge=0)
    semantic_signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionReceipt(StrictContract):
    datasource: str
    readonly_role: str
    elapsed_ms: int = Field(ge=0)
    row_count: int = Field(ge=0)
    plan_cost: float | None = Field(default=None, ge=0)
    estimated_rows: int | None = Field(default=None, ge=0)
    masking_applied: bool = False
    masked_columns: tuple[str, ...] = ()
    error_taxonomy: str | None = None
    sql_fingerprint: str = ""
    policy_version: str = ""
    policy_outcome: Literal["allow", "deny"] = "deny"
    # Frozen meaning: this execution was bound to an authorization decision that
    # ALLOWED under this revision.  It is NOT evidence that some parseable
    # context was merely present, and a deny must never stamp it.
    authorization_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    rowset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    source_kind: Literal["approved_aggregate", "approved_detail"] | None = None
    source_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    selection_reason: Literal["fresh_approved_aggregate", "approved_detail_fallback", "approved_detail"] | None = None
    source_degradation: tuple[SourceDegradation, ...] = ()
    source_checkpoint: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    semantic_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AnswerFact(StrictContract):
    """One grounded value projected from request-local execution evidence.

    Only fields proven by a successful step receipt or the request-local step
    output are populated.  Descriptive fields (unit, time range, dimension,
    quality, freshness explanation, confidence) default to absent: no scope,
    unit, time, dimension, category, quality or provenance plumbing is added in
    this slice, and nothing may be fabricated to satisfy the schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    step_id: PlanStepId
    # Exactly one result identity: a canonical metric_key OR a NONCANONICAL
    # derived_output_id, never both and never neither.
    metric_key: str | None = Field(default=None, min_length=1, max_length=256)
    derived_output_id: DerivedOutputId | None = None
    calculation_scope: Literal["ad_hoc_noncanonical"] | None = None
    status: Literal["grounded", "unavailable"] = "grounded"
    value: JsonValue = None
    rowset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    semantic_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    source_kind: Literal["approved_aggregate", "approved_detail"] | None = None
    selection_reason: Literal[
        "fresh_approved_aggregate", "approved_detail_fallback", "approved_detail"
    ] | None = None
    source_degradation: tuple[SourceDegradation, ...] = ()
    # Descriptive fields: evidence absent -> absent, never synthesised.
    unit: str | None = None
    time_range: TimeRange | None = None
    dimension: str | None = None
    quality: str | None = None
    freshness_explanation: str | None = None
    confidence_band: ConfidenceBand | None = None

    @model_validator(mode="after")
    def validate_unavailable_carries_no_value(self) -> AnswerFact:
        if self.status == "unavailable" and self.value is not None:
            raise ValueError("unavailable answer fact cannot carry a value")
        has_metric = self.metric_key is not None
        has_derived = self.derived_output_id is not None
        if has_metric == has_derived:
            raise ValueError("answer fact requires exactly one result identity")
        if has_derived != (self.calculation_scope == "ad_hoc_noncanonical"):
            raise ValueError(
                "derived output identity requires ad_hoc_noncanonical scope"
            )
        return self


class AnswerArtifact(StrictContract):
    """Request-local grounded projection; only its rendered text is persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[AnswerFact, ...] = ()
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    data_reference: str | None = None
    confidence_band: ConfidenceBand | None = None
    citations: tuple[str, ...] = ()
    degradation_flags: tuple[str, ...] = ()


class ErrorEnvelope(StrictContract):
    code: str
    retryable: bool = False
    stage: str
    safe_message: str
    trace_id: str


class ModelRequest(StrictContract):
    stage: ModelStage
    alias: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$",
    )
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    tool_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for a requested structured model output.",
    )
    deadline_ms: int = Field(ge=1, le=120_000)
    token_budget: int = Field(ge=1, le=1_000_000)
    cost_budget: float = Field(ge=0)
    data_classification: ModelDataClassification
    prompt_version: str = Field(min_length=1, max_length=128)
    plan_reason: str | None = Field(default=None, max_length=1024)
    # Optional run-bound authorization snapshot identity carried for provenance.
    # It is NOT re-evaluated here and is never authoritative on the model path.
    authorization_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )


class ModelFailure(StrictContract):
    """Safe, typed model failure details suitable for API and trace metadata."""

    code: str = Field(min_length=1, max_length=128)
    retryable: bool = False
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    status_code: int | None = Field(default=None, ge=100, le=599)
    attempted_providers: tuple[str, ...] = ()
    causes: tuple[str, ...] = ()


class ModelReceipt(StrictContract):
    alias: str = Field(min_length=1, max_length=128)
    stage: ModelStage
    provider: str = Field(min_length=1, max_length=128)
    resolved_model: str = Field(min_length=1, max_length=256)
    latency_ms: int = Field(ge=0)
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str = Field(min_length=1, max_length=128)
    retries: int = Field(default=0, ge=0)
    fallback_used: bool = False
    profile_version: str = Field(min_length=1, max_length=128)
    profile_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(min_length=1, max_length=128)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_checksum: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    content: str = ""
    estimated_cost: float = Field(default=0, ge=0)
    # P2-S2 egress evidence: the exact policy version/checksum that authorised
    # the resolved target, the outcome actually taken, any secret categories the
    # gate matched, and a stable digest of the request payload it evaluated.
    model_input_policy_version: str = Field(min_length=1, max_length=128)
    model_input_policy_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    egress_outcome: Literal["allow", "deny"] = "allow"
    matched_categories: tuple[str, ...] = Field(default=(), max_length=16)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("usage")
    @classmethod
    def validate_usage(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or amount < 0 for key, amount in value.items()):
            raise ValueError("model usage keys must be non-empty and values non-negative")
        return value


class ModelInputDecision(StrictContract):
    """One resolved model target's pre-egress ModelInputPolicy outcome.

    The decision is TARGET-SCOPED: the same request evaluates independently for
    the primary and for the fallback, and a permitted primary never implies a
    permitted fallback.  A deny carries matched secret categories but never a
    raw value; an allow never carries a category.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["allow", "deny"]
    reason: str | None = Field(default=None, min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_state: PolicyLifecycle
    target_provider: str = Field(min_length=1, max_length=128)
    target_model: str = Field(min_length=1, max_length=256)
    matched_categories: tuple[str, ...] = Field(default=(), max_length=16)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_decision(self) -> ModelInputDecision:
        if self.outcome == "deny":
            if self.reason is None:
                raise ValueError("deny model input decision requires a reason")
        elif self.reason is not None:
            raise ValueError("allow model input decision cannot carry a reason")
        elif self.matched_categories:
            raise ValueError("allow model input decision cannot match categories")
        return self


def _contract_checksum(contract: BaseModel) -> str:
    payload = json.dumps(
        contract.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
