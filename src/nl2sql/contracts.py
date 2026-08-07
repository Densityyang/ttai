"""Versioned API and agent-boundary contracts for the NL2SQL service."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ContextBundle(StrictContract):
    semantic_version: str
    schema_slice: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    token_cost: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)


class QueryPlan(StrictContract):
    intent: str
    metric: str | None = None
    dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    grain: str | None = None
    risk: Literal["low", "medium", "high"] = "low"


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
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
