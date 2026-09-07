"""Versioned API and agent-boundary contracts for the NL2SQL service."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ModelStage = Literal["classify", "retrieve", "plan", "generate_sql", "verify", "answer"]
ModelDataClassification = Literal["public", "internal", "confidential", "restricted"]
RouteName = Literal["fast", "standard", "deep"]
PolicyLifecycle = Literal["bootstrap", "calibrated", "frozen"]


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
    grain: Literal["hour", "day", "week", "month", "quarter", "year"]
    source_strategy: Literal["aggregate_first", "detail_required"]
    required_permissions: tuple[str, ...] = Field(default=(), max_length=32)
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=16)

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
        return _contract_checksum(self)


PlanStepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
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

    @field_validator("metric_keys", "depends_on")
    @classmethod
    def validate_unique_fetch_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("fetch metric values must be unique")
        return value


class TrustedCalculationStep(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["trusted_calculation"] = "trusted_calculation"
    step_id: PlanStepId
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    input_refs: dict[PlanInputName, PlanInputRef] = Field(min_length=1, max_length=32)
    depends_on: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)

    @field_validator("depends_on")
    @classmethod
    def validate_unique_calculation_dependencies(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("calculation dependencies must be unique")
        return value


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
    FetchMetricStep | TrustedCalculationStep | VerifyStep,
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
    kind: Literal["fetch_metric", "trusted_calculation", "verify"]
    status: Literal["succeeded", "failed"]
    elapsed_ms: int = Field(ge=0)
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rowset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
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
    sql: str
    fingerprint: str
    validation: tuple[str, ...] = ()
    cost: float | None = Field(default=None, ge=0)
    score: float = Field(ge=0, le=1)


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
    rowset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"


class AnswerArtifact(StrictContract):
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    data_reference: str | None = None
    confidence: float = Field(ge=0, le=1)
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

    @field_validator("usage")
    @classmethod
    def validate_usage(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or amount < 0 for key, amount in value.items()):
            raise ValueError("model usage keys must be non-empty and values non-negative")
        return value


def _contract_checksum(contract: BaseModel) -> str:
    payload = json.dumps(
        contract.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
