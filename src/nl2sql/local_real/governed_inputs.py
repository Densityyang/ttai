"""Local-real governed input adapter for reusable Definition execution.

This adapter deliberately reuses the same request-scoped typed runtime path as
Mode1 QUERY.  It does not contain SQL, a KPI formula, or a second evaluator.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from src.core.auth.types import AuthUser
from src.core.settings import get_settings
from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
    GovernedMetricInputResolutionError,
)
from src.nl2sql.orchestration.grounding import ground_execution_answer
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.calculation_contract import InputProvenance


class LocalRealGovernedMetricInputFetcher:
    """Resolve one frozen local-real metric through the shared typed runtime."""

    def __init__(self, container: object) -> None:
        self._container = container

    async def fetch_metric_input(
        self,
        *,
        owner_user_id: str,
        role: str,
        metric_key: str,
        required_provenance: InputProvenance,
        execution_context: DefinitionExecutionContext,
    ) -> ResolvedCalculationInput:
        settings = get_settings()
        if owner_user_id != settings.local_real_demo_user_id:
            raise GovernedMetricInputResolutionError(
                "local_real_governed_identity_mismatch"
            )
        if required_provenance != "published_gold":
            raise GovernedMetricInputResolutionError(
                "local_real_governed_provenance_unsupported"
            )
        from src.nl2sql.local_real.deployment import FROZEN_REAL_CASE_METRIC_KEY

        if metric_key != FROZEN_REAL_CASE_METRIC_KEY:
            raise GovernedMetricInputResolutionError(
                "local_real_governed_metric_unsupported"
            )

        from src.core.auth.provider import resolve_authorization_context

        provider = self._container.get_backend_authorization_provider()  # type: ignore[attr-defined]
        auth_user = AuthUser(
            user_id=owner_user_id,
            telephone=None,
            roles=["local-real-demo"],
            permissions=["nl2sql:invoke", "metrics:read"],
        )
        authorization = await resolve_authorization_context(provider, auth_user)
        if not isinstance(authorization, AuthorizationContext):
            raise GovernedMetricInputResolutionError(
                "local_real_authorization_unavailable"
            )
        identity = RequestIdentity(
            request_id=uuid4(),
            user_id=owner_user_id,
            roles=frozenset(auth_user.roles),
            permissions=frozenset(auth_user.permissions),
        )
        factory = self._container._local_real_typed_runtime_factory()  # type: ignore[attr-defined]
        runtime = await factory(
            identity=identity,
            authorization=authorization,
            expected_revision=authorization.authorization_revision,
        )
        if not hasattr(runtime, "context_resolver"):
            raise GovernedMetricInputResolutionError(
                "local_real_typed_runtime_unavailable"
            )

        if execution_context.date_mode == "exact_date":
            if execution_context.exact_date is None:
                raise GovernedMetricInputResolutionError(
                    "local_real_governed_exact_date_missing"
                )
            time_clause = execution_context.exact_date.isoformat()
        else:
            time_clause = "latest_authoritative"
        question = f"metric={metric_key} time={time_clause}"
        context = await runtime.context_resolver.resolve(
            question=question,
            identity=identity,
            route_hint="standard",
        )
        plan = await runtime.query_plan_provider.propose(
            question=question,
            context=context,
            identity=identity,
        )
        validator = PlanValidator()
        validation = validator.validate_query_plan(
            plan=plan, context=context, identity=identity
        )
        if validation.outcome != "allow":
            raise GovernedMetricInputResolutionError(
                "local_real_governed_plan_unavailable"
            )
        execution_plan = PlanCompiler().compile(
            plan=plan, context=context, validation=validation
        )
        budget = RouteBudgetLedger(route="standard")
        execution_validation = validator.validate_execution_plan(
            execution_plan=execution_plan,
            query_plan=plan,
            context=context,
            route_budget=budget.limits,
        )
        if execution_validation.outcome != "allow":
            raise GovernedMetricInputResolutionError(
                "local_real_governed_execution_unavailable"
            )
        execution = await runtime.plan_executor.execute(
            query_plan=plan,
            context=context,
            execution_plan=execution_plan,
            validation=execution_validation,
            expected_policy_version=validator.policy_version,
            expected_policy_checksum=validator.policy_checksum,
            budget=budget,
            deadline_ms=30_000,
        )
        grounded = ground_execution_answer(
            query_plan=plan,
            execution_plan=execution_plan,
            record=execution.record,
            outputs=execution.outputs,
        )
        fact = next(
            (
                item
                for item in grounded.facts
                if item.metric_key == metric_key and item.status == "grounded"
            ),
            None,
        )
        if fact is None or fact.value is None:
            raise GovernedMetricInputResolutionError(
                "local_real_governed_metric_unavailable"
            )
        raw_value = fact.value
        if raw_value is None or type(raw_value) is int or type(raw_value) is str:
            resolved_value = raw_value
        elif type(raw_value) is float:
            resolved_value = Decimal(str(raw_value))
        else:
            raise GovernedMetricInputResolutionError(
                "local_real_governed_metric_value_invalid"
            )
        return ResolvedCalculationInput(
            role=role,
            metric_key=metric_key,
            value=resolved_value,
            # The eligibility guard above is intentionally frozen to the one
            # governed ratio KPI, whose unit is canonically percent.  Raw
            # count dependencies never reach this reusable-input seam.
            unit="percent",
            data_as_of=fact.data_as_of,
            time_range=fact.time_range or plan.time_range,
            provenance="published_gold",
            source_id=fact.source_id,
            receipt_step_id=fact.step_id,
            fact_id=fact.fact_id,
        )


__all__ = ["LocalRealGovernedMetricInputFetcher"]
