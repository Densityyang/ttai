"""Request-scoped typed runtime composed from the published legacy AI-view deployment.

Scope and non-goals
-------------------
This module is PREPARATION/COMPOSITION only.  It is NOT wired into AppContainer
or the v2 production request path, and it activates no typed path.  Production
activation stays a later slice because a production AuthorizationContext is
still absent (the Backend source remains BLOCKED_BACKEND).  The factory is a PURE
constructor: it takes (identity, authorization) parameters and returns a
request-scoped component set.  Nothing here may be cached in the engine, and no
lifetime/lifespan hook may hold this factory's output, because
MetricQueryCompiler stores request identity and authorization and must be
created fresh per request.

Deployment model (O1)
---------------------
The FIRST VERSION reuses the legacy deployment model already in this repo:
configs/semantic/ai_views.yaml is the definition/configuration evidence and
src/nl2sql/infra/store/ai_views.py parses it.  The actually published database
relation is runtime truth.  The typed runtime only READS and VALIDATES that
contract; it never publishes.  It imports the CONFIG parsing entry points only
and never the publishing sync entry point or any DDL/DB helper: no view DDL and
no privilege statement is executed here.

V1 mapping
----------
source_ref IS the published AI-view name (views[].name) and the qualified
relation is AIViewsConfig.target_schema + "." + view.name.  A release relation
asset id is taken from the ACTIVE release (exactly one active relation asset for
the qualified relation); it is never invented and never silently re-hashed.

Executable metrics (explicit)
-----------------------------
The factory builds bindings for every metric document in the active release that
is ACTIVE: document asset_type == "metric", document status == "active", a
parseable execution contract whose release_status == "active" and
assistant_enabled.  Metrics that share a source_ref share ONE binding, because
the compiler keys bindings by deployment source id.  Merely being CONFIGURED is not enough: both product
complaint metrics are pending_source today and therefore materialize as RETIRED
assets, so the factory fails closed with no_executable_metric_binding for the
current product config.  That is intentional and is not the same as the
section-4 source_ref realignment, which resolves the configured metric
vocabulary against the published view.

Eligibility (V1)
----------------
work_order_metric_eligibility_v1 maps to NO additional predicates because
MetricQueryCompiler already includes the base is_valid_for_metrics is_true
predicate.  An unknown policy id FAILS CLOSED.  There is no policy table, policy
release, policy approval, policy attestation or policy checksum product.

Request coherence
-----------------
The active release is read ONCE, at factory construction.  A REQUEST-LOCAL bound
read callable returns that SAME release object to BOTH the evidence provider and
the compiler, and the SAME release-bound schema snapshot to the compiler, so a
pointer rotation during one request can never yield context from release A plus
compiler bindings from release B.  Approved relation ids come from the release
relation DOCUMENTS: the frozen provider sets the evidence relation_id to the
relation document_id and the resolver builds approved_relation_ids from that
evidence.  The release-bound snapshot separately supplies the physical relation
structure and organization coverage the binding is validated against.  The
registry rejects a same-id different-version rebind, but
MetricQueryCompiler.compile itself compares only release_id; the bound callable,
NOT that comparison, is what keeps one request coherent.

Authorization (O2)
------------------
The production-oriented factory REQUIRES a trusted AuthorizationContext.  It
NEVER constructs the compiler with authorization=None: that path silently
disables organization coverage (metric_query.py:337 validates coverage only when
authorization is present, and metric_query.py:344-347 sets
authorized_coverage = None), which is the deliberately preserved pre-2B
compatibility behaviour and the opposite of a production request.  A missing or
denied authority returns TypedRuntimeUnavailable.  Agent enforces only the
trusted Backend result and derives no hierarchy, role or organization scope.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from src.nl2sql.contracts import (
    AuthorizationContext,
    RequestIdentity,
    evaluate_authorization,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.infra.store.ai_views import (
    AIViewsConfig,
    ViewDefinition,
    view_output_columns,
)
from src.nl2sql.orchestration.deterministic_query_plan import (
    DeterministicQueryPlanProvider,
)
from src.nl2sql.orchestration.execution import PlanExecutor
from src.nl2sql.orchestration.metric_query import (
    EligibilityPolicy,
    MetricQueryCompiler,
    RelationBinding,
    metric_plan_executor,
)
from src.nl2sql.semantic.context_compiler import (
    ContextCompiler,
    SemanticContextResolver,
)
from src.nl2sql.semantic.metric_contract import MetricContract, Predicate
from src.nl2sql.semantic.policy_evidence import (
    ActiveReleaseRegistry,
    ReleaseScopedPolicyEvidenceProvider,
)
from src.nl2sql.semantic.registry import (
    SemanticDocument,
    SemanticRelease,
    SemanticReleaseState,
)
from src.nl2sql.semantic.schema_snapshot import (
    RelationSnapshot,
    SchemaSnapshot,
    SchemaSnapshotState,
)

ReadActive = Callable[[], Awaitable[SemanticRelease | None]]
ReadSnapshot = Callable[[str], Awaitable[SchemaSnapshot | None]]

# The bounded V1 eligibility resolver.  The known catalog policy maps to NO
# extra predicates; an id outside this closed table fails closed.  There is no
# policy store and no auto-accept of arbitrary ids.
V1_ELIGIBILITY_POLICIES: Mapping[str, tuple[Predicate, ...]] = {
    "work_order_metric_eligibility_v1": (),
}

# Typed-runtime-unavailable marker returned by the fail-closed factory.
TYPED_RUNTIME_UNAVAILABLE = "typed_runtime_unavailable"

TimestampKind = Literal["timestamp", "timestamptz"]

# Physical snapshot type -> RelationBinding.timestamp_kind.  Only timestamp and
# timestamptz are accepted; anything else fails closed.
_TIMESTAMP_KINDS: Mapping[str, TimestampKind] = {
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamptz": "timestamptz",
    "timestamp with time zone": "timestamptz",
}

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class TypedDeploymentError(RuntimeError):
    """A fail-closed typed-deployment construction failure.

    code is a stable machine-readable reason.  The request factory converts it
    into TypedRuntimeUnavailable(reason=code); direct adapter callers see the
    exception.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class TypedRuntimeUnavailable:
    """The typed runtime could not be constructed; reason is fail-closed."""

    reason: str


@dataclass(frozen=True, slots=True)
class RequestTypedRuntime:
    """One coherent request-scoped typed component set over one pinned release.

    Every component below was created together inside the factory.  Only
    deployment/application-scoped inputs (the parsed AIViewsConfig, the control
    read callables and the single shared QueryGateway) are reused across
    requests.
    """

    release: SemanticRelease
    snapshot: SchemaSnapshot
    relation_bindings: tuple[RelationBinding, ...]
    eligibility_policies: tuple[EligibilityPolicy, ...]
    active_release_registry: ActiveReleaseRegistry
    evidence_provider: ReleaseScopedPolicyEvidenceProvider
    context_compiler: ContextCompiler
    context_resolver: SemanticContextResolver
    query_plan_provider: DeterministicQueryPlanProvider
    metric_query_compiler: MetricQueryCompiler
    plan_executor: PlanExecutor
    gateway: QueryGateway


def resolve_eligibility_policy(policy_id: str) -> EligibilityPolicy:
    """Resolve one catalog eligibility_policy_id under bounded V1 rules.

    The known policy yields EligibilityPolicy(..., predicates=()); an unknown id
    raises TypedDeploymentError with code unknown_eligibility_policy.  No policy
    is auto-accepted.
    """

    predicates = V1_ELIGIBILITY_POLICIES.get(policy_id)
    if predicates is None:
        raise TypedDeploymentError("unknown_eligibility_policy", policy_id)
    return EligibilityPolicy(policy_id=policy_id, predicates=predicates)


def build_eligibility_policies(
    metrics: Iterable[MetricContract],
) -> tuple[EligibilityPolicy, ...]:
    """Resolve the distinct eligibility policies referenced by metrics."""

    resolved: dict[str, EligibilityPolicy] = {}
    for metric in metrics:
        policy = resolve_eligibility_policy(metric.eligibility_policy_id)
        resolved.setdefault(policy.policy_id, policy)
    return tuple(resolved[key] for key in sorted(resolved))


def executable_metric_contracts(release: SemanticRelease) -> tuple[MetricContract, ...]:
    """Return the ACTIVE, executable metric contracts of one release.

    Selection is explicit: only release metric documents that are themselves
    active and whose execution contract is active and assistant-enabled are
    returned.  A configured-but-pending metric is NOT executable, so it produces
    no binding.
    """

    contracts: list[MetricContract] = []
    for document in release.documents:
        contract = _metric_contract(document)
        if contract is None:
            continue
        if contract.release_status != "active" or not contract.assistant_enabled:
            continue
        contracts.append(contract)
    return tuple(sorted(contracts, key=lambda item: item.asset_id))


def build_relation_bindings(
    *,
    views: AIViewsConfig,
    release: SemanticRelease,
    snapshot: SchemaSnapshot,
    metrics: Iterable[MetricContract],
) -> tuple[RelationBinding, ...]:
    """Build ONE RelationBinding per DISTINCT published source_ref.

    Metrics that share a source_ref share one typed relation binding (the
    compiler keys bindings by deployment source id); every metric in the group
    is still validated against the published view and bound snapshot.
    """

    grouped: dict[str, list[MetricContract]] = {}
    for metric in metrics:
        grouped.setdefault(metric.source_ref, []).append(metric)
    return tuple(
        _build_binding(
            views=views,
            release=release,
            snapshot=snapshot,
            source_ref=source_ref,
            metrics=tuple(grouped[source_ref]),
        )
        for source_ref in sorted(grouped)
    )


def build_relation_binding(
    *,
    views: AIViewsConfig,
    release: SemanticRelease,
    snapshot: SchemaSnapshot,
    metric: MetricContract,
) -> RelationBinding:
    """Build the one typed execution binding for a single metric's source_ref."""

    return _build_binding(
        views=views,
        release=release,
        snapshot=snapshot,
        source_ref=metric.source_ref,
        metrics=(metric,),
    )


def _build_binding(
    *,
    views: AIViewsConfig,
    release: SemanticRelease,
    snapshot: SchemaSnapshot,
    source_ref: str,
    metrics: tuple[MetricContract, ...],
) -> RelationBinding:
    """Build one typed execution binding from published deployment evidence.

    The fail-closed steps mirror the slice rules:

    A. source_ref matches EXACTLY ONE published AI view by view.name;
    B. the qualified relation is target_schema.view.name;
    C. the active release contains EXACTLY ONE active relation asset for it;
    D. the release-bound snapshot contains EXACTLY ONE physical relation;
    E. allowed columns are the PUBLISHED view output columns, each validated to
       exist in the bound snapshot;
    F. every column EACH grouped metric requires is present in the published view;
    G. timestamp_kind is derived from the ACTUAL snapshot type of
       business_time_column and only timestamp/timestamptz are accepted.
    """

    view = _view_for_source_ref(views, source_ref)
    qualified = f"{views.target_schema}.{view.name}"
    relation_asset_id = _release_relation_asset_id(release, qualified)
    relation = _snapshot_relation(snapshot, qualified)
    allowed_columns = view_output_columns(view)
    if not allowed_columns:
        raise TypedDeploymentError("published_view_columns_missing", qualified)
    if len(set(allowed_columns)) != len(allowed_columns):
        raise TypedDeploymentError("published_view_columns_ambiguous", qualified)
    invalid_columns = sorted(
        item for item in allowed_columns if not _IDENTIFIER_RE.fullmatch(item)
    )
    if invalid_columns:
        raise TypedDeploymentError("published_view_column_invalid", invalid_columns[0])
    snapshot_columns = {column.name: column.data_type.lower() for column in relation.columns}
    missing = sorted(item for item in allowed_columns if item not in snapshot_columns)
    if missing:
        raise TypedDeploymentError("published_view_column_missing", f"{qualified}.{missing[0]}")

    published = set(allowed_columns)
    time_kinds: set[TimestampKind] = set()
    for metric in metrics:
        required = {
            "is_valid_for_metrics",
            metric.business_time_column,
            *(item.field for item in metric.formula_predicates),
            *(item.field for item in metric.filters),
        }
        for coverage in relation.organization_coverage:
            if coverage.field is not None:
                required.add(coverage.field)
        not_published = sorted(item for item in required if item not in published)
        if not_published:
            raise TypedDeploymentError(
                "metric_column_not_published", f"{qualified}.{not_published[0]}"
            )
        time_type = snapshot_columns.get(metric.business_time_column)
        if time_type is None or time_type not in _TIMESTAMP_KINDS:
            raise TypedDeploymentError(
                "business_time_type_unsupported",
                f"{qualified}.{metric.business_time_column}",
            )
        time_kinds.add(_TIMESTAMP_KINDS[time_type])
    if len(time_kinds) != 1:
        # One binding carries one timestamp_kind; disagreeing metrics fail closed.
        raise TypedDeploymentError("business_time_type_ambiguous", qualified)
    try:
        return RelationBinding(
            source_ref=source_ref,
            relation_asset_id=relation_asset_id,
            schema_name=views.target_schema,
            relation_name=view.name,
            allowed_columns=allowed_columns,
            # V1 has no extra relation-level permission layer; the metric-level
            # Backend entry permission stays authoritative.
            required_permissions=(),
            approved=True,
            timestamp_kind=next(iter(time_kinds)),
        )
    except ValidationError as exc:
        raise TypedDeploymentError("relation_binding_invalid", source_ref) from exc


async def build_request_typed_runtime(
    *,
    views: AIViewsConfig,
    read_active: ReadActive,
    read_snapshot: ReadSnapshot,
    gateway: QueryGateway,
    identity: RequestIdentity,
    authorization: AuthorizationContext | None,
    clock: Callable[[], datetime] | None = None,
) -> RequestTypedRuntime | TypedRuntimeUnavailable:
    """Compose ONE request-scoped typed component set, or fail closed.

    Deployment/application-scoped inputs are reused; the request-scoped objects
    are created fresh together.  The active release is read exactly once here and
    pinned behind a request-local read callable shared by the evidence provider
    and the compiler, together with the release-bound snapshot.
    """

    # REQUIRED trusted Backend authority.  Never substitute a wildcard scope, old
    # DataScope, role guess or full access, and never fall through to the
    # authorization=None compatibility seam (see the module docstring).
    if not isinstance(authorization, AuthorizationContext):
        return TypedRuntimeUnavailable(reason="authorization_context_missing")
    if evaluate_authorization(authorization, expected_revision=None).outcome != "allow":
        return TypedRuntimeUnavailable(reason="authorization_denied")

    # ONE read of the active pointer for the whole request.
    release = await read_active()
    if release is None or release.state is not SemanticReleaseState.ACTIVE:
        return TypedRuntimeUnavailable(reason="no_active_semantic_release")
    if release.schema_snapshot_id is None:
        return TypedRuntimeUnavailable(reason="schema_snapshot_unbound")
    snapshot = await read_snapshot(release.schema_snapshot_id)
    if (
        snapshot is None
        or snapshot.state is not SchemaSnapshotState.VALIDATED
        or snapshot.snapshot_id != release.schema_snapshot_id
        or release.schema_snapshot_checksum != snapshot.checksum
    ):
        return TypedRuntimeUnavailable(reason="schema_snapshot_unavailable")

    # Request-local bound reads: they return the SAME pinned objects for the
    # whole request, so a control-pointer flip cannot mix releases.
    async def bound_read_active() -> SemanticRelease | None:
        return release

    async def bound_read_snapshot(snapshot_id: str) -> SchemaSnapshot | None:
        if snapshot_id != snapshot.snapshot_id:
            return None
        return snapshot

    metrics = executable_metric_contracts(release)
    try:
        bindings = build_relation_bindings(
            views=views, release=release, snapshot=snapshot, metrics=metrics
        )
        policies = build_eligibility_policies(metrics)
    except TypedDeploymentError as exc:
        return TypedRuntimeUnavailable(reason=exc.code)
    if not bindings:
        return TypedRuntimeUnavailable(reason="no_executable_metric_binding")

    request_clock = clock or (lambda: datetime.now(UTC))
    registry = ActiveReleaseRegistry()
    evidence_provider = ReleaseScopedPolicyEvidenceProvider(
        bound_read_active,
        {binding.source_ref: binding.relation_asset_id for binding in bindings},
        None,
        registry=registry,
    )
    context_compiler = ContextCompiler(registry)
    context_resolver = SemanticContextResolver(
        compiler=context_compiler,
        evidence_provider=evidence_provider,
    )
    query_plan_provider = DeterministicQueryPlanProvider(registry, request_clock)
    compiler = MetricQueryCompiler(
        read_active=bound_read_active,
        read_snapshot=bound_read_snapshot,
        bindings=bindings,
        eligibility_policies=policies,
        identity=identity,
        authorization=authorization,
        clock=request_clock,
    )
    return RequestTypedRuntime(
        release=release,
        snapshot=snapshot,
        relation_bindings=bindings,
        eligibility_policies=policies,
        active_release_registry=registry,
        evidence_provider=evidence_provider,
        context_compiler=context_compiler,
        context_resolver=context_resolver,
        query_plan_provider=query_plan_provider,
        metric_query_compiler=compiler,
        plan_executor=metric_plan_executor(compiler, gateway),
        gateway=gateway,
    )


def _metric_contract(document: SemanticDocument) -> MetricContract | None:
    if document.metadata.get("asset_type") != "metric":
        return None
    if document.metadata.get("status") != "active":
        return None
    raw = document.metadata.get("execution_contract", "")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        contract = MetricContract.model_validate_json(raw)
    except ValueError:
        return None
    if contract.asset_id != document.document_id:
        return None
    return contract


def _view_for_source_ref(views: AIViewsConfig, source_ref: str) -> ViewDefinition:
    matches = [view for view in views.views if view.name == source_ref]
    if not matches:
        raise TypedDeploymentError("published_view_missing", source_ref)
    if len(matches) > 1:
        raise TypedDeploymentError("published_view_ambiguous", source_ref)
    return matches[0]


def _release_relation_asset_id(release: SemanticRelease, qualified_relation: str) -> str:
    matches = [
        document
        for document in release.documents
        if document.metadata.get("asset_type") == "relation"
        and document.metadata.get("status") == "active"
        and _published_relation_name(document) == qualified_relation
    ]
    if not matches:
        raise TypedDeploymentError("release_relation_asset_missing", qualified_relation)
    if len(matches) > 1:
        raise TypedDeploymentError("release_relation_asset_ambiguous", qualified_relation)
    return matches[0].document_id


def _published_relation_name(document: SemanticDocument) -> str:
    name = document.metadata.get("relation_name", "").strip()
    if name:
        return name
    content = document.content.strip()
    if content.startswith("relation "):
        return content[len("relation ") :].strip()
    return ""


def _snapshot_relation(snapshot: SchemaSnapshot, qualified_relation: str) -> RelationSnapshot:
    matches = [
        relation
        for relation in snapshot.candidate.relations
        if relation.relation_id == qualified_relation
    ]
    if not matches:
        raise TypedDeploymentError("snapshot_relation_missing", qualified_relation)
    if len(matches) > 1:
        raise TypedDeploymentError("snapshot_relation_ambiguous", qualified_relation)
    return matches[0]


__all__ = [
    "TYPED_RUNTIME_UNAVAILABLE",
    "V1_ELIGIBILITY_POLICIES",
    "RequestTypedRuntime",
    "TypedDeploymentError",
    "TypedRuntimeUnavailable",
    "build_eligibility_policies",
    "build_relation_binding",
    "build_relation_bindings",
    "build_request_typed_runtime",
    "executable_metric_contracts",
    "resolve_eligibility_policy",
]
