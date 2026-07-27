"""Versioned API and agent-boundary contracts for the NL2SQL service."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    masking_applied: bool = False
    error_taxonomy: str | None = None


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
    stage: Literal["classify", "retrieve", "plan", "generate_sql", "verify", "answer"]
    alias: str
    messages: list[dict[str, Any]]
    tool_schema: dict[str, Any] | None = None
    deadline_ms: int = Field(ge=1)
    token_budget: int = Field(ge=1)
    cost_budget: float = Field(ge=0)
    data_classification: str
    plan_reason: str | None = Field(default=None, max_length=1024)


class ModelReceipt(StrictContract):
    provider: str
    resolved_model: str
    latency_ms: int = Field(ge=0)
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str
    retries: int = Field(default=0, ge=0)
    fallback_used: bool = False
    profile_version: str
    content: str = ""
    estimated_cost: float = Field(default=0, ge=0)
