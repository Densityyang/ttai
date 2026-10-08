"""Typed query-plan validation and deterministic execution-plan compilation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

from src.nl2sql.contracts import (
    AdHocCalculationStep,
    ContextBundle,
    ExecutionPlan,
    FetchMetricStep,
    PlanStep,
    PlanValidationIssue,
    PlanValidationRecord,
    QueryPlan,
    RequestIdentity,
    RouteBudget,
    RouteName,
    TrustedCalculationStep,
    VerifyStep,
    query_plan_payload,
)
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    declares_canonical_binding,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
    CalculationSpec,
    derived_output_id,
    referenced_input_roles,
)

PLAN_VALIDATION_POLICY_VERSION = "plan-validation.bootstrap.v1"
PLAN_COMPILER_POLICY_VERSION = "plan-compiler.v1"

# A binding input role is only compilable when it maps to a PlanStepId-safe,
# lowercase dependency step id.  Anything else fails closed at compile time.
_DEPENDENCY_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


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


class DeterministicQueryUnsupported(ValueError):
    """A DETERMINISTIC planner cannot serve this request without a model.

    Generic and provider-agnostic: it lives at the shared planning seam so the
    engine never depends on any concrete (e.g. demo) provider.  Any future
    deterministic provider may raise it.  It carries only a stable machine code,
    never business meaning.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
        calculation_catalog: ApprovedCalculationCatalog | None = None,
    ) -> None:
        if not policy_version.strip():
            raise ValueError("plan validation policy version must be non-empty")
        self.policy_version = policy_version
        self.approved_template_ids = approved_template_ids
        self.approved_invariant_ids = approved_invariant_ids
        # Already-resolved trusted bindings; None keeps the bounded legacy
        # approved-template-id behaviour and never invents authority.
        self._calculation_catalog = calculation_catalog
        policy_payload_fields: dict[str, object] = {
            "policy_version": policy_version,
            "approved_template_ids": sorted(approved_template_ids),
            "approved_invariant_ids": sorted(approved_invariant_ids),
        }
        if calculation_catalog is not None:
            # Two validators with different authoritative canonical calculation
            # catalogs must not share a policy identity.  When no catalog is
            # configured the legacy payload is kept byte-for-byte identical, so
            # no empty-catalog authority marker is invented.
            policy_payload_fields["calculation_catalog_checksum"] = (
                calculation_catalog.checksum
            )
        policy_payload = json.dumps(
            policy_payload_fields,
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
        plan = QueryPlan.model_validate(query_plan_payload(plan))
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
        query_plan = QueryPlan.model_validate(query_plan_payload(query_plan))
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

        ad_hoc_steps = [
            step
            for step in execution_plan.steps
            if isinstance(step, AdHocCalculationStep)
        ]
        canonical_provenance_present = any(
            isinstance(step, TrustedCalculationStep)
            and declares_canonical_binding(step)
            for step in execution_plan.steps
        ) or any(
            isinstance(step, FetchMetricStep)
            and step.calculation_input_role is not None
            for step in execution_plan.steps
        )
        if ad_hoc_steps and canonical_provenance_present:
            deny.append(
                _issue(
                    "ad_hoc_calculation_canonical_conflict",
                    "execution_plan.steps",
                    "A plan may not mix canonical and AD_HOC calculation provenance.",
                )
            )
        for code in _ad_hoc_v1_closure_failures(execution_plan):
            deny.append(
                _issue(
                    code,
                    "execution_plan.steps",
                    "The plan does not match the exact V1 AD_HOC carrier shape.",
                )
            )
        for code in _ad_hoc_closure_failures(execution_plan.steps):
            deny.append(
                _issue(
                    code,
                    "execution_plan.steps",
                    "An AD_HOC dependency fetch is not closed over exactly one owner.",
                )
            )

        if self._calculation_catalog is None:
            # Calculation provenance is trusted-binding authority evidence.  A
            # plan carrying ANY canonical provenance -- a canonical calculation
            # or a dependency fetch -- is a hard DENY without a catalog, and a
            # template-id allowlist can never stand in for it.
            if canonical_provenance_present:
                deny.append(
                    _issue(
                        "trusted_calculation_binding_authority_missing",
                        "execution_plan.steps",
                        "Canonical calculation provenance is present but no trusted "
                        "binding catalog is available.",
                    )
                )
        else:
            # Plan-level canonical authority: a catalog-bound requested output
            # must be claimed by exactly one canonical calculation, and every
            # provenance-carrying dependency fetch must have exactly one owner,
            # whether or not the plan happens to contain a calculation step.
            for code in self._calculation_catalog.validate_bound_outputs(
                query_plan=query_plan,
                execution_plan=execution_plan,
            ):
                deny.append(
                    _issue(
                        code,
                        "execution_plan.steps",
                        "A catalog-bound requested metric is not satisfied by exactly "
                        "one trusted calculation.",
                    )
                )
            for code in self._calculation_catalog.validate_dependency_provenance(
                execution_plan=execution_plan,
            ):
                deny.append(
                    _issue(
                        code,
                        "execution_plan.steps",
                        "A calculation dependency fetch is not owned by exactly one "
                        "canonical calculation.",
                    )
                )

        for step in execution_plan.steps:
            if isinstance(step, TrustedCalculationStep):
                if declares_canonical_binding(step):
                    # Canonical metric authority is the already-resolved trusted
                    # binding, NEVER a template-id allowlist.  Without a catalog
                    # the plan was already denied above.
                    if self._calculation_catalog is not None:
                        for code in self._calculation_catalog.validate_calculation(
                            step=step,
                            execution_plan=execution_plan,
                            context=context,
                        ):
                            deny.append(
                                _issue(
                                    code,
                                    f"execution_plan.steps.{step.step_id}",
                                    "The calculation step does not match its trusted binding.",
                                )
                            )
                elif step.template_id not in self.approved_template_ids:
                    approval.append(
                        _issue(
                            "trusted_calculation_approval_required",
                            f"execution_plan.steps.{step.step_id}.template_id",
                            "The requested calculation template is not approved for this plan.",
                        )
                    )
            elif isinstance(step, AdHocCalculationStep):
                for code in _ad_hoc_step_failures(
                    step, execution_plan.steps, context, query_plan
                ):
                    deny.append(
                        _issue(
                            code,
                            f"execution_plan.steps.{step.step_id}",
                            "The AD_HOC calculation step is not structurally valid.",
                        )
                    )
                if self._calculation_catalog is not None:
                    for item in step.calculation_spec.inputs:
                        if item.metric_key and (
                            self._calculation_catalog.binding_for(item.metric_key)
                            is not None
                        ):
                            deny.append(
                                _issue(
                                    "ad_hoc_calculation_input_catalog_bound",
                                    f"execution_plan.steps.{step.step_id}",
                                    "An AD_HOC input may not be a catalog-bound "
                                    "approved calculation.",
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
            execution_plan_sha256=execution_plan.checksum,
            issues=issues,
        )


class PlanCompiler:
    """Compile one validated proposal into the registered SQL-plus-verify DAG."""

    def __init__(
        self,
        *,
        policy_version: str = PLAN_COMPILER_POLICY_VERSION,
        calculation_catalog: ApprovedCalculationCatalog | None = None,
    ) -> None:
        if not policy_version.strip():
            raise ValueError("plan compiler policy version must be non-empty")
        self.policy_version = policy_version
        self._calculation_catalog = calculation_catalog

    @property
    def calculation_catalog(self) -> ApprovedCalculationCatalog | None:
        """The catalog this compiler was built with, read-only.

        A caller that must run the SAME authority check before compiling (for
        example the AD_HOC request resolver, which has to reject an input that is
        catalog-bound) would otherwise have to be handed the catalog separately
        and could silently be handed a DIFFERENT one.  Exposing the compiler's
        own catalog makes that impossible; the reference is returned as-is and
        there is deliberately no setter.
        """

        return self._calculation_catalog

    def compile(
        self,
        *,
        plan: QueryPlan,
        context: ContextBundle,
        validation: PlanValidationRecord,
    ) -> ExecutionPlan:
        plan = QueryPlan.model_validate(query_plan_payload(plan))
        context = ContextBundle.model_validate_json(context.model_dump_json())
        validation = PlanValidationRecord.model_validate_json(validation.model_dump_json())
        if validation.outcome != "allow":
            raise PlanValidationError(validation)
        if validation.query_plan_sha256 != plan.checksum:
            raise PlanCompilationError("query_plan_validation_hash_mismatch")
        if validation.context_checksum != context.checksum:
            raise PlanCompilationError("context_validation_hash_mismatch")

        catalog = self._calculation_catalog
        bound_requested = (
            tuple(
                key for key in plan.metric_keys if catalog.binding_for(key) is not None
            )
            if catalog is not None
            else ()
        )
        if bound_requested:
            # V1 supports exactly ONE catalog-bound canonical output per plan.
            # Any other shape that requests a bound metric fails closed: falling
            # back to a direct fetch would create an untrusted second authority
            # for a canonical value.
            if len(plan.metric_keys) != 1 or len(bound_requested) != 1:
                raise PlanCompilationError("approved_calculation_plan_shape_unsupported")
            binding = catalog.binding_for(plan.metric_keys[0]) if catalog is not None else None
            if binding is None:
                raise PlanCompilationError("approved_calculation_plan_shape_unsupported")
            steps = _compile_bound_calculation(plan=plan, binding=binding)
        else:
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
            steps: tuple[PlanStep, ...] = (fetch, verify)
        return ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version=self.policy_version,
            steps=steps,
        )

    def compile_ad_hoc(
        self,
        *,
        plan: QueryPlan,
        context: ContextBundle,
        validation: PlanValidationRecord,
        calculation_spec: CalculationSpec,
        execution_binding: CalculationExecutionBinding,
    ) -> ExecutionPlan:
        """Compile one PRE-RESOLVED explicit AD_HOC calculation (noncanonical).

        Separate entry point: normal compile() behavior is unchanged and AD_HOC
        is never inferred from the mere presence of arithmetic.
        """

        plan = QueryPlan.model_validate(query_plan_payload(plan))
        context = ContextBundle.model_validate_json(context.model_dump_json())
        validation = PlanValidationRecord.model_validate_json(
            validation.model_dump_json()
        )
        spec = CalculationSpec.model_validate_json(calculation_spec.model_dump_json())
        binding = CalculationExecutionBinding.model_validate_json(
            execution_binding.model_dump_json()
        )
        if validation.outcome != "allow":
            raise PlanValidationError(validation)
        if validation.query_plan_sha256 != plan.checksum:
            raise PlanCompilationError("query_plan_validation_hash_mismatch")
        if validation.context_checksum != context.checksum:
            raise PlanCompilationError("context_validation_hash_mismatch")
        if plan.intent != "metric":
            raise PlanCompilationError("ad_hoc_calculation_intent_unsupported")
        if plan.unresolved_slots or context.unresolved_slots:
            raise PlanCompilationError("ad_hoc_calculation_unresolved_slots")
        if binding.calculation_id != spec.calculation_id:
            raise PlanCompilationError("ad_hoc_calculation_binding_identity_mismatch")
        if binding.spec_checksum != spec.checksum:
            raise PlanCompilationError("ad_hoc_calculation_binding_spec_mismatch")
        if binding.binding_failures(spec):
            raise PlanCompilationError("ad_hoc_calculation_binding_invalid")
        if {item.role for item in spec.inputs} != set(
            referenced_input_roles(spec.expression)
        ):
            raise PlanCompilationError("ad_hoc_calculation_input_unused")
        resolved: dict[str, str] = {}
        for item in spec.inputs:
            if item.metric_key is None:
                raise PlanCompilationError("ad_hoc_calculation_input_unresolved")
            if item.provenance == "ad_hoc_metric":
                raise PlanCompilationError(
                    "ad_hoc_calculation_nested_input_unsupported"
                )
            resolved[item.role] = item.metric_key
        if len(set(resolved.values())) != len(resolved):
            raise PlanCompilationError("ad_hoc_calculation_input_duplicate")
        if set(resolved.values()) != set(plan.metric_keys):
            raise PlanCompilationError("ad_hoc_calculation_source_plan_mismatch")
        if set(resolved.values()) - set(context.asset_ids):
            raise PlanCompilationError("ad_hoc_calculation_context_missing")
        catalog = self._calculation_catalog
        if catalog is not None and any(
            catalog.binding_for(key) is not None for key in resolved.values()
        ):
            raise PlanCompilationError("ad_hoc_calculation_input_catalog_bound")
        steps = _compile_ad_hoc_calculation(
            spec=spec, binding=binding, resolved=resolved
        )
        return ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version=self.policy_version,
            steps=steps,
        )


def _ad_hoc_fetch_step_id(role: str) -> str:
    """Deterministic PlanStepId for one AD_HOC calculation input role."""

    normalized = role.lower()
    if not _DEPENDENCY_ROLE.fullmatch(normalized):
        raise PlanCompilationError("ad_hoc_calculation_role_unsupported")
    return f"fetch_{normalized}"


def _compile_ad_hoc_calculation(
    *,
    spec: CalculationSpec,
    binding: CalculationExecutionBinding,
    resolved: dict[str, str],
) -> tuple[PlanStep, ...]:
    """Emit the deterministic NONCANONICAL AD_HOC dependency DAG.

    Each declared input role becomes exactly one dependency fetch carrying the
    AD_HOC provenance triple; the calculation consumes only the projected scalar
    ("<fetch>.value") of those fetches.  No canonical provenance is emitted.
    """

    if len(spec.inputs) + 2 > 16:
        raise PlanCompilationError("ad_hoc_calculation_plan_shape_unsupported")
    output_id = derived_output_id(spec, binding)
    fetch_ids = {item.role: _ad_hoc_fetch_step_id(item.role) for item in spec.inputs}
    if len(set(fetch_ids.values())) != len(fetch_ids):
        raise PlanCompilationError("ad_hoc_calculation_role_unsupported")
    fetches = tuple(
        FetchMetricStep(
            step_id=fetch_ids[item.role],
            metric_keys=(resolved[item.role],),
            ad_hoc_input_role=item.role,
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=output_id,
        )
        for item in spec.inputs
    )
    calculation = AdHocCalculationStep(
        step_id="calculate_adhoc",
        calculation_spec=spec,
        execution_binding=binding,
        input_refs={
            item.role: f"{fetch_ids[item.role]}.value" for item in spec.inputs
        },
        depends_on=tuple(fetch_ids[item.role] for item in spec.inputs),
        derived_output_id=output_id,
    )
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(calculation.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(calculation.step_id,),
    )
    return (*fetches, calculation, verify)


def _dependency_fetch_step_id(role: str) -> str:
    """Deterministic PlanStepId for one trusted binding input role."""

    normalized = role.lower()
    if not _DEPENDENCY_ROLE.fullmatch(normalized):
        raise PlanCompilationError("approved_calculation_role_unsupported")
    return f"fetch_{normalized}"


def _compile_bound_calculation(
    *,
    plan: QueryPlan,
    binding: ApprovedCalculationBinding,
) -> tuple[PlanStep, ...]:
    """Emit internal dependency fetches feeding one trusted calculation.

    The requested metric is NEVER itself fetched: fetching the derived output
    would create a second, untrusted authority for the same value alongside the
    bound calculation.  Each binding input becomes exactly one dependency fetch
    whose declared provenance is the binding checksum, the input role and the
    canonical output key; the calculation consumes only the projected scalar
    ("<fetch>.value") of those fetches.
    """

    if plan.intent != "metric":
        raise PlanCompilationError("approved_calculation_intent_unsupported")
    if plan.metric_keys != (binding.canonical_metric_key,):
        raise PlanCompilationError("approved_calculation_output_not_requested")
    fetch_steps = [
        FetchMetricStep(
            step_id=_dependency_fetch_step_id(item.role),
            metric_keys=(item.metric_key,),
            calculation_input_role=item.role,
            calculation_binding_checksum=binding.checksum,
            calculation_output_metric_key=binding.canonical_metric_key,
        )
        for item in binding.inputs
    ]
    if len({step.step_id for step in fetch_steps}) != len(fetch_steps):
        raise PlanCompilationError("approved_calculation_role_unsupported")
    calculation = TrustedCalculationStep(
        step_id="calculate_metric",
        template_id=binding.template_id,
        template_version=binding.template_version,
        template_checksum=binding.template_checksum,
        binding_checksum=binding.checksum,
        output_metric_key=binding.canonical_metric_key,
        input_refs={
            item.role: f"{_dependency_fetch_step_id(item.role)}.value"
            for item in binding.inputs
        },
        depends_on=tuple(step.step_id for step in fetch_steps),
    )
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(calculation.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(calculation.step_id,),
    )
    return (*fetch_steps, calculation, verify)


def _ad_hoc_v1_closure_failures(execution_plan: ExecutionPlan) -> tuple[str, ...]:
    """Prove the EXACT V1 AD_HOC carrier shape WITHOUT trusting compiler provenance.

    A plan containing an AdHocCalculationStep must be exactly:
    N AD_HOC dependency fetches + 1 AdHocCalculationStep + 1 VerifyStep, where N is
    the number of declared calculation input roles.  Every fetch must carry full
    AD_HOC provenance and map to exactly one role; the calculation must depend on
    exactly those fetches; the single verify must observe only the calculation.
    """

    steps = execution_plan.steps
    ad_hoc_steps = [s for s in steps if isinstance(s, AdHocCalculationStep)]
    if not ad_hoc_steps:
        return ()
    failures: list[str] = []
    if len(ad_hoc_steps) != 1:
        failures.append("ad_hoc_calculation_output_duplicate")
    calculation = ad_hoc_steps[0]
    if any(isinstance(s, TrustedCalculationStep) for s in steps):
        failures.append("ad_hoc_calculation_trusted_step_forbidden")
    fetch_steps = [s for s in steps if isinstance(s, FetchMetricStep)]
    verify_steps = [s for s in steps if isinstance(s, VerifyStep)]
    if not verify_steps:
        failures.append("ad_hoc_calculation_verify_missing")
    elif len(verify_steps) > 1:
        failures.append("ad_hoc_calculation_verify_duplicate")
    permitted = {s.step_id for s in fetch_steps}
    permitted.add(calculation.step_id)
    permitted.update(s.step_id for s in verify_steps)
    if len(permitted) != len(steps):
        failures.append("ad_hoc_calculation_plan_shape_unsupported")
    roles = {item.role for item in calculation.calculation_spec.inputs}
    role_counts: dict[str, int] = {}
    for fetch in fetch_steps:
        role = fetch.ad_hoc_input_role
        if (
            role is None
            or fetch.ad_hoc_spec_checksum is None
            or fetch.ad_hoc_derived_output_id is None
        ):
            failures.append("ad_hoc_calculation_dependency_provenance_missing")
            continue
        role_counts[role] = role_counts.get(role, 0) + 1
    if any(count != 1 for count in role_counts.values()):
        failures.append("ad_hoc_calculation_dependency_role_duplicate")
    if set(role_counts) != roles or len(fetch_steps) != len(roles):
        failures.append("ad_hoc_calculation_dependency_count_mismatch")
    if set(calculation.depends_on) != {f.step_id for f in fetch_steps}:
        failures.append("ad_hoc_calculation_dependency_set_mismatch")
    if verify_steps:
        verify = verify_steps[0]
        if (
            verify.depends_on != (calculation.step_id,)
            or verify.input_refs != (calculation.step_id,)
            or verify.invariant_ids != ("typed_result_present",)
        ):
            failures.append("ad_hoc_calculation_verify_mismatch")
    return tuple(dict.fromkeys(failures))


def _ad_hoc_closure_failures(steps: tuple[PlanStep, ...]) -> tuple[str, ...]:
    failures: list[str] = []
    ad_hoc_steps = [s for s in steps if isinstance(s, AdHocCalculationStep)]
    for step in steps:
        if not isinstance(step, FetchMetricStep) or step.ad_hoc_input_role is None:
            continue
        consumers = [c for c in ad_hoc_steps if step.step_id in c.depends_on]
        if not consumers:
            failures.append("ad_hoc_calculation_dependency_rogue")
            continue
        if len(consumers) > 1:
            failures.append("ad_hoc_calculation_dependency_duplicate_consumer")
            continue
        consumer = consumers[0]
        if consumer.input_refs.get(step.ad_hoc_input_role) != f"{step.step_id}.value":
            failures.append("ad_hoc_calculation_input_ref_mismatch")
        if step.ad_hoc_spec_checksum != consumer.calculation_spec.checksum:
            failures.append("ad_hoc_calculation_dependency_spec_mismatch")
        if step.ad_hoc_derived_output_id != consumer.derived_output_id:
            failures.append("ad_hoc_calculation_dependency_output_mismatch")
    return tuple(dict.fromkeys(failures))


def _ad_hoc_step_failures(
    step: AdHocCalculationStep,
    steps: tuple[PlanStep, ...],
    context: ContextBundle,
    query_plan: QueryPlan,
) -> tuple[str, ...]:
    failures: list[str] = []
    spec = step.calculation_spec
    # Source-identity closure: the spec's resolved input metrics must be exactly
    # the governed QueryPlan source metric set.  No hidden input, no extra plan
    # metric, no unresolved or duplicate identity.
    metrics = [item.metric_key for item in spec.inputs]
    if any(metric is None for metric in metrics):
        failures.append("ad_hoc_calculation_input_unresolved")
    if any(item.provenance == "ad_hoc_metric" for item in spec.inputs):
        failures.append("ad_hoc_calculation_nested_input_unsupported")
    resolved = [metric for metric in metrics if metric is not None]
    if len(set(resolved)) != len(resolved):
        failures.append("ad_hoc_calculation_input_duplicate")
    if set(resolved) != set(query_plan.metric_keys):
        failures.append("ad_hoc_calculation_source_plan_mismatch")
    fetches_by_role: dict[str, list[FetchMetricStep]] = {}
    for candidate in steps:
        if (
            isinstance(candidate, FetchMetricStep)
            and candidate.ad_hoc_input_role is not None
            and candidate.step_id in step.depends_on
        ):
            fetches_by_role.setdefault(candidate.ad_hoc_input_role, []).append(candidate)
    declared_roles = {item.role for item in spec.inputs}
    if set(fetches_by_role) != declared_roles:
        failures.append("ad_hoc_calculation_dependency_role_missing")
    for role, matches in fetches_by_role.items():
        if len(matches) > 1:
            failures.append("ad_hoc_calculation_dependency_duplicate_consumer")
            continue
        fetch = matches[0]
        expected_metric = next(
            (item.metric_key for item in spec.inputs if item.role == role), None
        )
        if expected_metric is None or fetch.metric_keys != (expected_metric,):
            failures.append("ad_hoc_calculation_dependency_metric_mismatch")
        elif expected_metric not in context.asset_ids:
            failures.append("ad_hoc_calculation_context_missing")
    return tuple(dict.fromkeys(failures))


def _issue(code: str, path: str, safe_message: str) -> PlanValidationIssue:
    return PlanValidationIssue(code=code, path=path, safe_message=safe_message)
