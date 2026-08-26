"""Typed query-plan validation and deterministic execution-plan compilation."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from src.nl2sql.contracts import (
    ContextBundle,
    ExecutionPlan,
    FetchMetricStep,
    PlanValidationIssue,
    PlanValidationRecord,
    QueryPlan,
    RequestIdentity,
    RouteBudget,
    RouteName,
    TrustedCalculationStep,
    VerifyStep,
)

PLAN_VALIDATION_POLICY_VERSION = "plan-validation.bootstrap.v1"
PLAN_COMPILER_POLICY_VERSION = "plan-compiler.v1"


class ContextResolver(Protocol):
    """Policy-aware Context Compiler boundary used by the request graph."""

    async def resolve(
        self,
        *,
        question: str,
        identity: RequestIdentity,
        route_hint: RouteName,
    ) -> ContextBundle: ...


class QueryPlanProvider(Protocol):
    """Produce an untrusted proposal without a model call before route selection."""

    @property
    def is_deterministic(self) -> bool: ...

    async def propose(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> QueryPlan: ...


class PlanValidationError(ValueError):
    def __init__(self, record: PlanValidationRecord) -> None:
        super().__init__(record.issues[0].code if record.issues else "plan_validation_failed")
        self.record = record


class PlanCompilationError(ValueError):
    pass


class PlanValidator:
    """Fail-closed semantic, permission, dependency, and budget validation."""

    def __init__(
        self,
        *,
        policy_version: str = PLAN_VALIDATION_POLICY_VERSION,
        approved_template_ids: frozenset[str] = frozenset(),
        approved_invariant_ids: frozenset[str] = frozenset({"typed_result_present"}),
    ) -> None:
        if not policy_version.strip():
            raise ValueError("plan validation policy version must be non-empty")
        self.policy_version = policy_version
        self.approved_template_ids = approved_template_ids
        self.approved_invariant_ids = approved_invariant_ids
        policy_payload = json.dumps(
            {
                "policy_version": policy_version,
                "approved_template_ids": sorted(approved_template_ids),
                "approved_invariant_ids": sorted(approved_invariant_ids),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self.policy_checksum = hashlib.sha256(policy_payload.encode("utf-8")).hexdigest()

    def validate_query_plan(
        self,
        *,
        plan: QueryPlan,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> PlanValidationRecord:
        plan = QueryPlan.model_validate_json(plan.model_dump_json())
        context = ContextBundle.model_validate_json(context.model_dump_json())
        identity = RequestIdentity.model_validate_json(identity.model_dump_json())
        deny: list[PlanValidationIssue] = []
        clarify: list[PlanValidationIssue] = []

        if context.resolution_status == "conflict" or context.conflict_ids:
            deny.append(
                _issue(
                    "semantic_context_conflict",
                    "context.resolution_status",
                    "Semantic context contains unresolved conflicts.",
                )
            )
        elif context.resolution_status != "resolved":
            clarify.append(
                _issue(
                    "semantic_context_incomplete",
                    "context.resolution_status",
                    "Semantic context is not complete enough to execute.",
                )
            )

        unresolved = tuple(dict.fromkeys((*context.unresolved_slots, *plan.unresolved_slots)))
        if unresolved:
            clarify.append(
                _issue(
                    "query_plan_unresolved_slots",
                    "query_plan.unresolved_slots",
                    "The query requires clarification before execution.",
                )
            )

        if plan.domain not in context.domains:
            deny.append(
                _issue(
                    "query_plan_domain_not_permitted",
                    "query_plan.domain",
                    "The requested semantic domain is not available to this request.",
                )
            )

        unknown_metrics = sorted(set(plan.metric_keys) - set(context.asset_ids))
        if unknown_metrics:
            deny.append(
                _issue(
                    "query_plan_metric_not_resolved",
                    "query_plan.metric_keys",
                    "One or more requested metrics are not present in the resolved context.",
                )
            )

        permissions = identity.permissions
        missing_permissions = (
            set()
            if "*" in permissions
            else set(plan.required_permissions) - set(permissions)
        )
        if missing_permissions:
            deny.append(
                _issue(
                    "query_plan_permission_denied",
                    "query_plan.required_permissions",
                    "The request identity does not have all permissions required by the plan.",
                )
            )

        if plan.source_strategy == "detail_required" and not context.approved_relation_ids:
            deny.append(
                _issue(
                    "query_plan_detail_source_unapproved",
                    "query_plan.source_strategy",
                    "Detail execution requires an approved relation in the active release.",
                )
            )
        if plan.intent == "detail" and plan.source_strategy != "detail_required":
            deny.append(
                _issue(
                    "query_plan_detail_strategy_mismatch",
                    "query_plan.source_strategy",
                    "A detail request must use the governed detail-source strategy.",
                )
            )

        issues = tuple(deny or clarify)
        outcome = "deny" if deny else "clarify" if clarify else "allow"
        return PlanValidationRecord(
            policy_version=self.policy_version,
            policy_checksum=self.policy_checksum,
            outcome=outcome,
            query_plan_sha256=plan.checksum,
            context_checksum=context.checksum,
            issues=issues,
        )

    def validate_execution_plan(
        self,
        *,
        execution_plan: ExecutionPlan,
        query_plan: QueryPlan,
        context: ContextBundle,
        route_budget: RouteBudget,
    ) -> PlanValidationRecord:
        execution_plan = ExecutionPlan.model_validate_json(
            execution_plan.model_dump_json()
        )
        query_plan = QueryPlan.model_validate_json(query_plan.model_dump_json())
        context = ContextBundle.model_validate_json(context.model_dump_json())
        deny: list[PlanValidationIssue] = []
        approval: list[PlanValidationIssue] = []

        if execution_plan.query_plan_sha256 != query_plan.checksum:
            deny.append(
                _issue(
                    "execution_plan_query_hash_mismatch",
                    "execution_plan.query_plan_sha256",
                    "The execution plan does not match the validated query plan.",
                )
            )
        if execution_plan.semantic_release_id != context.semantic_release_id:
            deny.append(
                _issue(
                    "execution_plan_semantic_release_mismatch",
                    "execution_plan.semantic_release_id",
                    "The execution plan semantic release is no longer active for this request.",
                )
            )
        if execution_plan.schema_snapshot_id != context.schema_snapshot_id:
            deny.append(
                _issue(
                    "execution_plan_schema_snapshot_mismatch",
                    "execution_plan.schema_snapshot_id",
                    "The execution plan schema snapshot does not match the resolved context.",
                )
            )

        fetch_steps = [
            step for step in execution_plan.steps if isinstance(step, FetchMetricStep)
        ]
        if len(fetch_steps) > route_budget.max_sql_candidates:
            deny.append(
                _issue(
                    "execution_plan_sql_candidate_budget_exceeded",
                    "execution_plan.steps",
                    "The execution plan exceeds the route SQL-candidate budget.",
                )
            )
        if len(fetch_steps) > route_budget.max_sql_executions:
            deny.append(
                _issue(
                    "execution_plan_sql_execution_budget_exceeded",
                    "execution_plan.steps",
                    "The execution plan exceeds the route SQL-execution budget.",
                )
            )
        if len(context.approved_edge_ids) > route_budget.max_join_hops:
            deny.append(
                _issue(
                    "execution_plan_join_budget_exceeded",
                    "context.approved_edge_ids",
                    "The approved join path exceeds the route budget.",
                )
            )

        for step in execution_plan.steps:
            if isinstance(step, TrustedCalculationStep):
                if step.template_id not in self.approved_template_ids:
                    approval.append(
                        _issue(
                            "trusted_calculation_approval_required",
                            f"execution_plan.steps.{step.step_id}.template_id",
                            "The requested calculation template is not approved for this plan.",
                        )
                    )
            elif isinstance(step, VerifyStep):
                unknown = set(step.invariant_ids) - self.approved_invariant_ids
                if unknown:
                    deny.append(
                        _issue(
                            "execution_plan_invariant_unregistered",
                            f"execution_plan.steps.{step.step_id}.invariant_ids",
                            "The execution plan references an unregistered result invariant.",
                        )
                    )

        issues = tuple(deny or approval)
        outcome = "deny" if deny else "approval" if approval else "allow"
        return PlanValidationRecord(
            policy_version=self.policy_version,
            policy_checksum=self.policy_checksum,
            outcome=outcome,
            query_plan_sha256=query_plan.checksum,
            context_checksum=context.checksum,
            issues=issues,
        )


class PlanCompiler:
    """Compile one validated proposal into the registered SQL-plus-verify DAG."""

    def __init__(self, *, policy_version: str = PLAN_COMPILER_POLICY_VERSION) -> None:
        if not policy_version.strip():
            raise ValueError("plan compiler policy version must be non-empty")
        self.policy_version = policy_version

    def compile(
        self,
        *,
        plan: QueryPlan,
        context: ContextBundle,
        validation: PlanValidationRecord,
    ) -> ExecutionPlan:
        plan = QueryPlan.model_validate_json(plan.model_dump_json())
        context = ContextBundle.model_validate_json(context.model_dump_json())
        validation = PlanValidationRecord.model_validate_json(validation.model_dump_json())
        if validation.outcome != "allow":
            raise PlanValidationError(validation)
        if validation.query_plan_sha256 != plan.checksum:
            raise PlanCompilationError("query_plan_validation_hash_mismatch")
        if validation.context_checksum != context.checksum:
            raise PlanCompilationError("context_validation_hash_mismatch")

        fetch = FetchMetricStep(
            step_id="fetch_metrics",
            metric_keys=plan.metric_keys,
        )
        verify = VerifyStep(
            step_id="verify_result",
            input_refs=(fetch.step_id,),
            invariant_ids=("typed_result_present",),
            depends_on=(fetch.step_id,),
        )
        return ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version=self.policy_version,
            steps=(fetch, verify),
        )


def _issue(code: str, path: str, safe_message: str) -> PlanValidationIssue:
    return PlanValidationIssue(code=code, path=path, safe_message=safe_message)
