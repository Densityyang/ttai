"""Demo request-scoped typed runtime: synthetic, deterministic, no database.

Satisfies the same TypedRuntimeBundle contract the engine consumes, so the demo
exercises the REAL engine/planning/execution/grounding path without a remote
PostgreSQL, a control store, a semantic publisher or a QueryGateway.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from src.nl2sql.contracts import (
    AuthorizationDecision,
    ContextBundle,
    ExecutionReceipt,
    FetchMetricStep,
    QueryPlan,
    RequestIdentity,
    TimeRange,
)
from src.nl2sql.demo.fixtures import (
    DEMO_AMBIGUOUS_ASSET_ID,
    DEMO_AMBIGUOUS_TERM,
    DEMO_DATASOURCE,
    DEMO_METRICS_BY_KEY,
    DEMO_READONLY_ROLE,
    DEMO_RETRIEVAL_TERMS,
    DEMO_SOURCE_ID,
    DEMO_UNSUPPORTED_ASSET_ID,
    DEMO_UNSUPPORTED_TERMS,
)
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
)
from src.nl2sql.orchestration.planning import DeterministicQueryUnsupported

# Fixed demo release/snapshot identities (the contract types them as UUIDs).
# They are constant so demo runs are reproducible, and they are NOT any
# production release/snapshot identity.
DEMO_RELEASE_ID = UUID("00000000-0000-4000-8000-00000000d001")
DEMO_SNAPSHOT_ID = UUID("00000000-0000-4000-8000-00000000d002")
DEMO_AS_OF_DATE = date(2026, 1, 1)


def _demo_hash(label: str) -> str:
    """A deterministic, contract-shaped 64-hex fingerprint."""

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def is_demo_identity(identity: RequestIdentity) -> bool:
    """True only for an explicit demo fixture identity."""

    from src.core.auth.demo_provider import DEFAULT_DEMO_IDENTITIES

    return str(identity.user_id) in {item.user_id for item in DEFAULT_DEMO_IDENTITIES}


class DemoContextResolver:
    """Bounded deterministic resolver over the demo vocabulary.  No model."""

    def __init__(self, identity: RequestIdentity) -> None:
        self._identity = identity

    async def resolve(
        self,
        *,
        question: str,
        identity: RequestIdentity,
        route_hint: str,
    ) -> ContextBundle:
        if identity != self._identity:
            raise ValueError("request_identity_mismatch")
        text = (question or "").strip().lower()
        metric_key = DEMO_RETRIEVAL_TERMS.get(text)
        if metric_key is not None:
            return ContextBundle(
                semantic_release_id=DEMO_RELEASE_ID,
                schema_snapshot_id=DEMO_SNAPSHOT_ID,
                domains=("demo",),
                asset_ids=(metric_key,),
                resolution_status="resolved",
            )
        # Every ContextBundle must stay contract-valid (asset_ids >= 1), so an
        # unresolved request carries an explicit DEMO-ONLY synthetic candidate
        # id.  It is not executable and grants no authority; it exists solely so
        # the contract holds and the planner can emit its typed signal.
        if DEMO_AMBIGUOUS_TERM in text:
            return ContextBundle(
                semantic_release_id=DEMO_RELEASE_ID,
                schema_snapshot_id=DEMO_SNAPSHOT_ID,
                domains=("demo",),
                asset_ids=(DEMO_AMBIGUOUS_ASSET_ID,),
                resolution_status="ambiguous",
                unresolved_slots=("metric",),
            )
        return ContextBundle(
            semantic_release_id=DEMO_RELEASE_ID,
            schema_snapshot_id=DEMO_SNAPSHOT_ID,
            domains=("demo",),
            asset_ids=(DEMO_UNSUPPORTED_ASSET_ID,),
            resolution_status="incomplete",
            unresolved_slots=("metric",),
        )


class DemoQueryPlanProvider:
    """Deterministic bounded planner over the demo vocabulary.  No model."""

    def __init__(self, identity: RequestIdentity) -> None:
        self._identity = identity

    @property
    def is_deterministic(self) -> bool:
        return True

    async def propose(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> QueryPlan:
        if identity != self._identity:
            raise ValueError("request_identity_mismatch")
        text = (question or "").strip().lower()
        metric_key = DEMO_RETRIEVAL_TERMS.get(text)
        if metric_key is None:
            if any(term in text for term in DEMO_UNSUPPORTED_TERMS):
                # Deliberately unservable deterministically: QUERY must reach
                # the typed cannot_resolve outcome, never a model.
                raise DemoUnsupportedRequest("demo_request_unsupported")
            raise DemoUnsupportedRequest("demo_request_unresolved")
        return QueryPlan(
            intent="metric",
            domain="demo",
            metric_keys=(metric_key,),
            time_range=TimeRange(start=DEMO_AS_OF_DATE, end=DEMO_AS_OF_DATE),
            grain="day",
            source_strategy="aggregate_first",
            required_permissions=(),
            unresolved_slots=(),
        )


# The demo provider raises the SHARED deterministic-planning signal, so the
# engine never imports anything from this demo package.
DemoUnsupportedRequest = DeterministicQueryUnsupported


class DemoMetricStepRunner:
    """Synthetic metric runner: fixture lookup only, never SQL."""

    def __init__(self, authorization_revision: str) -> None:
        self._authorization_revision = authorization_revision

    async def prepare(
        self,
        *,
        step: FetchMetricStep,
        query_plan: QueryPlan,
        context: ContextBundle,
    ) -> PreparedMetricStep:
        del query_plan, context
        for metric_key in step.metric_keys:
            if metric_key not in DEMO_METRICS_BY_KEY:
                raise ValueError("demo_metric_unknown")
        return PreparedMetricStep(
            sql_fingerprint=_demo_hash(f"demo-sql:{step.step_id}"),
            join_hops=0,
            # The fetched step travels inside the ephemeral payload: the demo
            # needs no SQL, so the "compiled handle" is just the request itself.
            payload=(step.step_id, tuple(step.metric_keys)),
        )

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
    ) -> MetricStepResult:
        del timeout_ms
        step_id, metric_keys = prepared.payload  # type: ignore[misc]
        values: dict[str, Any] = {}
        for metric_key in metric_keys:
            fixture = DEMO_METRICS_BY_KEY.get(metric_key)
            if fixture is None:
                raise ValueError("demo_metric_unknown")
            values[metric_key] = float(fixture.value)
        return MetricStepResult(
            value=values,
            receipt=ExecutionReceipt(
                # EXPLICIT demo provenance: never a production identity.
                datasource=DEMO_DATASOURCE,
                readonly_role=DEMO_READONLY_ROLE,
                elapsed_ms=0,
                row_count=1,
                policy_version="demo.policy.v1",
                policy_outcome="allow",
                authorization_revision=self._authorization_revision,
                sql_fingerprint=prepared.sql_fingerprint,
                source_id=DEMO_SOURCE_ID,
                source_kind="approved_aggregate",
                selection_reason="fresh_approved_aggregate",
                freshness_status="fresh",
            ),
        )


@dataclass(frozen=True, slots=True)
class DemoRequestTypedRuntime:
    """Demo implementation of the TypedRuntimeBundle contract."""

    context_resolver: DemoContextResolver
    query_plan_provider: DemoQueryPlanProvider
    plan_executor: PlanExecutor
    identity: RequestIdentity
    authorization: Any
    authorization_revision: str

    def authorization_decision(self) -> AuthorizationDecision:
        """The explicit ALLOW proven by the bound demo context."""

        return AuthorizationDecision(
            outcome="allow",
            authorization_revision=self.authorization_revision,
        )


def build_demo_runtime(
    *,
    identity: RequestIdentity,
    authorization: Any,
    expected_revision: str | None = None,
    ad_hoc_calculation_runner: Any | None = None,
) -> DemoRequestTypedRuntime | None:
    """Build ONE request-scoped demo runtime, or None for a foreign identity.

    Performs NO database connection, NO control-store read, NO release read and
    NO production deployment input access.
    """

    if not is_demo_identity(identity):
        return None
    revision = str(authorization.authorization_revision)
    if expected_revision is not None and revision != expected_revision:
        return None
    runner = DemoMetricStepRunner(revision)
    return DemoRequestTypedRuntime(
        context_resolver=DemoContextResolver(identity),
        query_plan_provider=DemoQueryPlanProvider(identity),
        # QUERY never receives an AD_HOC runner; ANALYZE/BUILD may.
        plan_executor=PlanExecutor(
            metric_runner=runner,
            ad_hoc_calculation_runner=ad_hoc_calculation_runner,
        ),
        identity=identity,
        authorization=authorization,
        authorization_revision=revision,
    )
