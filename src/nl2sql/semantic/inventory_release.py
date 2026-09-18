"""P1 slice 4: identity-layer assessment, exact resolution, and a real subset candidate.

This module is the only bridge between the frozen metric inventory
(src.nl2sql.semantic.metric_inventory) and the existing V3 authoring,
materialization, and registry release mechanism.  It deliberately stays at the
catalog/identity layer:

* it never invents an executable binding (source relation, formula, template,
  precision, rounding, null/zero strategy, owner, or sensitivity);
* it never activates a release;
* it never treats an unknown, ambiguous, or retired identity as resolved;
* it never fabricates a SemanticReleaseCandidate.checksum.

The authoritative 280+14 inventory cannot be materialized by one
materialize_authoring_ir call: the frozen materializer requires globally unique
normalized display aliases, and the Gold source declares three display labels
shared by two distinct canonical identities each.  Rather than rename a label,
edit a frozen module, or synthesize a merged candidate, the bridge reports those
collisions as first-class findings and returns the unmodified output of a single
materialize_authoring_ir call over the deterministic alias-unique subset (288
identities after excluding both members of every collision).

The subset candidate is still not an executable release: the inventory record
type intentionally drops expression/source_table/aggregation/filters, so
authoring.validate_authoring_ir reports every missing binding as an explicit
error and ValidationReport.ok stays False.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.nl2sql.semantic.authoring import (
    AssetStatus,
    AuthoringIR,
    AuthoringIssue,
    IssueSeverity,
    MetricAsset,
    ValidationReport,
    validate_authoring_ir,
)
from src.nl2sql.semantic.materialization import (
    materialize_authoring_ir,
    normalize_semantic_alias,
)
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.metric_inventory import (
    MetricInventoryRecord,
    MetricInventorySnapshot,
    SourceProvenance,
)
from src.nl2sql.semantic.planner_metric_projection import (
    CurrentStateSnapshot,
    LifecycleSnapshot,
    LifecycleValue,
)
from src.nl2sql.semantic.registry import (
    SemanticRegistry,
    SemanticRelease,
    SemanticReleaseCandidate,
    SemanticReleaseState,
)

INVENTORY_RELEASE_SCHEMA_VERSION = 1

_IDENTITY_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

_DERIVED_BINDINGS: tuple[tuple[str, str], ...] = (
    ("formula", "missing_formula"),
    ("calculation_template_id", "missing_template"),
    ("calculation_template_version", "missing_template_version"),
    ("decimal_scale", "missing_precision"),
    ("rounding", "missing_rounding"),
    ("null_strategy", "missing_null_strategy"),
    ("zero_strategy", "missing_zero_strategy"),
)
_ACTIVATION_BINDINGS: tuple[tuple[str, str], ...] = (
    ("owner", "missing_owner"),
    ("sensitivity", "missing_sensitivity"),
    ("freshness_sla_seconds", "missing_freshness"),
)


class InventoryReleaseError(ValueError):
    """Raised when an inventory input cannot form a fail-closed assessment."""


class MetricNamespace(StrEnum):
    CANONICAL = "canonical_gold"
    LEGACY = "legacy_semantic"


class ResolutionCode(StrEnum):
    INVALID = "invalid_metric_key"
    UNKNOWN = "unknown_metric_key"
    AMBIGUOUS = "ambiguous_metric_key"
    RETIRED = "retired_metric_key"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResolvedMetricIdentity(_StrictFrozenModel):
    """One exact, namespace-qualified metric identity."""

    metric_key: str = Field(min_length=1, pattern=_IDENTITY_PATTERN)
    namespace: Literal["canonical_gold", "legacy_semantic"]
    metric_type: Literal["raw", "derived", "external", "legacy_only"]
    legacy_only: bool
    display_name: str = Field(min_length=1)
    domain: str | None = None
    provenance: SourceProvenance
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    lifecycle: LifecycleValue | None = None
    lifecycle_state: Literal["CURRENT_STATE", "UNSPECIFIED"] = "UNSPECIFIED"

    @property
    def is_canonical(self) -> bool:
        return self.namespace == "canonical_gold"


class MetricResolutionFailure(_StrictFrozenModel):
    """Structured, deterministic reason an identity did not resolve."""

    code: ResolutionCode
    metric_key: str
    message: str = Field(min_length=1)
    canonical_match: bool
    legacy_match: bool
    lifecycle: LifecycleValue | None = None


class MetricResolutionError(LookupError):
    """Raised when exact resolution fails closed."""

    def __init__(self, failure: MetricResolutionFailure) -> None:
        super().__init__(failure.message)
        self._failure = failure

    @property
    def failure(self) -> MetricResolutionFailure:
        return self._failure


class InventoryBindingGap(_StrictFrozenModel):
    """One authoring binding the frozen inventory cannot supply."""

    metric_key: str = Field(min_length=1, pattern=_IDENTITY_PATTERN)
    namespace: Literal["canonical_gold", "legacy_semantic"]
    field: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class AliasCollisionMember(_StrictFrozenModel):
    """One identity participating in a normalized display-label collision."""

    metric_key: str = Field(min_length=1, pattern=_IDENTITY_PATTERN)
    namespace: Literal["canonical_gold", "legacy_semantic"]
    source_file: str = Field(min_length=1)


class InventoryAliasCollision(_StrictFrozenModel):
    """One shared display label that a product decision must disambiguate."""

    normalized_label: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    members: tuple[AliasCollisionMember, ...]
    decision_required: Literal[
        "source_display_name_disambiguation_or_reviewed_materializer_change"
    ] = "source_display_name_disambiguation_or_reviewed_materializer_change"
    message: str = Field(min_length=1)


class ExcludedMetricIdentity(_StrictFrozenModel):
    """One identity excluded from the alias-unique materialized subset."""

    metric_key: str = Field(min_length=1, pattern=_IDENTITY_PATTERN)
    namespace: Literal["canonical_gold", "legacy_semantic"]
    display_name: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    reason: Literal["display_alias_collision"] = "display_alias_collision"
    collision_label: str = Field(min_length=1)
    peer_metric_keys: tuple[str, ...]


class PrunedDependencyEdge(_StrictFrozenModel):
    """One inventory dependency dropped only because its target was excluded."""

    source_metric_key: str = Field(min_length=1, pattern=_IDENTITY_PATTERN)
    label: str = Field(min_length=1)
    target_metric_key: str = Field(min_length=1, pattern=_IDENTITY_PATTERN)
    reason: Literal["target_excluded_by_alias_collision"] = (
        "target_excluded_by_alias_collision"
    )


@dataclass(frozen=True, slots=True)
class MetricIdentityIndex:
    """Exact two-namespace identity index with explicit lifecycle state."""

    canonical_records: Mapping[str, MetricInventoryRecord]
    legacy_records: Mapping[str, MetricInventoryRecord]
    lifecycle_by_key: Mapping[str, LifecycleValue]
    lifecycle_state_by_key: Mapping[str, Literal["CURRENT_STATE", "UNSPECIFIED"]]

    @classmethod
    def from_namespaces(
        cls,
        canonical: Sequence[MetricInventoryRecord],
        legacy: Sequence[MetricInventoryRecord],
        *,
        lifecycle: LifecycleSnapshot | None = None,
        current_state: CurrentStateSnapshot | None = None,
    ) -> MetricIdentityIndex:
        canonical_map = {record.metric_key: record for record in canonical}
        legacy_map = {record.metric_key: record for record in legacy}
        known = frozenset((*canonical_map, *legacy_map))
        lifecycle_map, state_map, _ = _lifecycle_maps(lifecycle, current_state, known)
        return cls(canonical_map, legacy_map, lifecycle_map, state_map)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: MetricInventorySnapshot,
        *,
        lifecycle: LifecycleSnapshot | None = None,
        current_state: CurrentStateSnapshot | None = None,
    ) -> MetricIdentityIndex:
        if not isinstance(snapshot, MetricInventorySnapshot):
            raise TypeError("exact identity resolution requires MetricInventorySnapshot")
        return cls.from_namespaces(
            snapshot.canonical,
            snapshot.legacy,
            lifecycle=lifecycle,
            current_state=current_state,
        )

    @property
    def metric_keys(self) -> tuple[str, ...]:
        return tuple(sorted((*self.canonical_records, *self.legacy_records)))

    def resolve(self, metric_key: object) -> ResolvedMetricIdentity:
        """Return the exact identity or raise a structured resolution failure."""

        outcome = self.try_resolve(metric_key)
        if isinstance(outcome, MetricResolutionFailure):
            raise MetricResolutionError(outcome)
        return outcome

    def try_resolve(
        self, metric_key: object
    ) -> ResolvedMetricIdentity | MetricResolutionFailure:
        if not isinstance(metric_key, str) or not metric_key.strip():
            return MetricResolutionFailure(
                code=ResolutionCode.INVALID,
                metric_key=str(metric_key),
                message="metric key must be a nonempty exact identity string",
                canonical_match=False,
                legacy_match=False,
            )
        # Exact identity only: no strip, case-fold, prefix, or alias matching.
        key = metric_key
        canonical = self.canonical_records.get(key)
        legacy = self.legacy_records.get(key)
        if canonical is not None and legacy is not None:
            return MetricResolutionFailure(
                code=ResolutionCode.AMBIGUOUS,
                metric_key=key,
                message=(
                    "metric key resolves in both namespaces; refusing an implicit namespace "
                    f"choice: {key}"
                ),
                canonical_match=True,
                legacy_match=True,
                lifecycle=self.lifecycle_by_key.get(key),
            )
        record = canonical if canonical is not None else legacy
        if record is None:
            return MetricResolutionFailure(
                code=ResolutionCode.UNKNOWN,
                metric_key=key,
                message=f"metric key is not an exact inventory identity: {key}",
                canonical_match=False,
                legacy_match=False,
            )
        lifecycle = self.lifecycle_by_key.get(key)
        if lifecycle == "retired":
            return MetricResolutionFailure(
                code=ResolutionCode.RETIRED,
                metric_key=key,
                message=f"metric identity is explicitly retired and cannot resolve: {key}",
                canonical_match=canonical is not None,
                legacy_match=legacy is not None,
                lifecycle=lifecycle,
            )
        return ResolvedMetricIdentity(
            metric_key=key,
            namespace=record.provenance.source_kind,
            metric_type=record.metric_type,
            legacy_only=record.metric_type == "legacy_only",
            display_name=record.display_name,
            domain=record.domain,
            provenance=record.provenance,
            definition_sha256=record.definition_sha256,
            lifecycle=lifecycle,
            lifecycle_state=self.lifecycle_state_by_key.get(key, "UNSPECIFIED"),
        )


@dataclass(frozen=True, slots=True)
class InventoryReleaseCandidate:
    """Typed assessment plus one real, untouched materializer output.

    candidate is exactly materialize_authoring_ir(ir, report) for the disclosed
    ir/report pair.  It is never assembled, merged, or re-checksummed by the
    bridge, so release_checksum is always a genuine materializer checksum.
    """

    schema_version: int
    inventory_fingerprint: str
    lifecycle_fingerprint: str | None
    ir: AuthoringIR
    report: ValidationReport
    candidate: SemanticReleaseCandidate
    index: MetricIdentityIndex
    materialized_metric_keys: tuple[str, ...]
    materializable: bool
    blocked_reason: str | None
    excluded_identities: tuple[ExcludedMetricIdentity, ...]
    alias_collisions: tuple[InventoryAliasCollision, ...]
    pruned_dependency_edges: tuple[PrunedDependencyEdge, ...]
    binding_gaps: tuple[InventoryBindingGap, ...]

    @property
    def authoring_checksum(self) -> str:
        return self.ir.checksum

    @property
    def release_checksum(self) -> str:
        """The real materialize_authoring_ir checksum for this candidate."""

        return self.candidate.checksum

    @property
    def is_activatable(self) -> bool:
        """Always reported from the real validation report; never forced true."""

        return self.report.ok

    @property
    def metric_keys(self) -> tuple[str, ...]:
        """Every inventory identity, including the excluded subset."""

        return self.index.metric_keys

    def resolve(self, metric_key: object) -> ResolvedMetricIdentity:
        return self.index.resolve(metric_key)

    def create_draft(
        self, registry: SemanticRegistry, *, change_summary: str
    ) -> SemanticRelease:
        """Create the DRAFT release through the existing registry boundary."""

        release = registry.create_draft(
            self.candidate.documents, change_summary=change_summary
        )
        if release.state is not SemanticReleaseState.DRAFT:
            raise InventoryReleaseError("inventory candidate did not create a draft release")
        return release

    def submit(
        self, registry: SemanticRegistry, *, change_summary: str
    ) -> SemanticRelease:
        """Feed this candidate through the existing publish path.

        A candidate whose report is not ok always raises SemanticReleaseError,
        and the registry only moves its active pointer inside activate.  Callers
        must treat an unexpected return value as a contract violation.
        """

        return registry.publish(
            self.candidate.documents,
            change_summary=change_summary,
            validator=lambda _: self.candidate.validation_report,
        )


class MetricContractMatch(_StrictFrozenModel):
    """One old-contract metric key compared against the inventory."""

    metric_key: str
    code: Literal["in_inventory", "not_in_inventory"]
    namespace: Literal["canonical_gold", "legacy_semantic"] | None = None
    legacy_only: bool | None = None
    message: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class MetricContractBridge:
    """Legacy complaint contract compiled through the existing authoring path."""

    catalog: MetricCatalog
    ir: AuthoringIR
    report: ValidationReport
    candidate: SemanticReleaseCandidate
    matches: tuple[MetricContractMatch, ...]

    @property
    def in_inventory(self) -> tuple[str, ...]:
        return tuple(item.metric_key for item in self.matches if item.code == "in_inventory")

    @property
    def not_in_inventory(self) -> tuple[str, ...]:
        return tuple(
            item.metric_key for item in self.matches if item.code == "not_in_inventory"
        )


def build_metric_identity_index(
    snapshot: MetricInventorySnapshot,
    *,
    lifecycle: LifecycleSnapshot | None = None,
    current_state: CurrentStateSnapshot | None = None,
) -> MetricIdentityIndex:
    """Build the exact two-namespace index over a verified inventory snapshot."""

    return MetricIdentityIndex.from_snapshot(
        snapshot, lifecycle=lifecycle, current_state=current_state
    )


def build_inventory_authoring_ir(
    snapshot: MetricInventorySnapshot,
    *,
    lifecycle: LifecycleSnapshot | None = None,
    current_state: CurrentStateSnapshot | None = None,
) -> AuthoringIR:
    """Build the identity-layer AuthoringIR for all identities; no binding is invented.

    This full IR is not by itself materializable when the source has display
    alias collisions; build_inventory_release_candidate materializes the
    deterministic alias-unique subset instead.
    """

    if not isinstance(snapshot, MetricInventorySnapshot):
        raise TypeError("authoring IR construction requires MetricInventorySnapshot")
    index = MetricIdentityIndex.from_snapshot(
        snapshot, lifecycle=lifecycle, current_state=current_state
    )
    records = _ordered_records(snapshot)
    keep_keys = frozenset(record.metric_key for record in records)
    return _authoring_ir(records, index.lifecycle_by_key, keep_keys)


def build_inventory_release_candidate(
    snapshot: MetricInventorySnapshot,
    *,
    lifecycle: LifecycleSnapshot | None = None,
    current_state: CurrentStateSnapshot | None = None,
) -> InventoryReleaseCandidate:
    """Assess the inventory and materialize the alias-unique subset once."""

    if not isinstance(snapshot, MetricInventorySnapshot):
        raise TypeError("candidate release construction requires MetricInventorySnapshot")
    index = MetricIdentityIndex.from_snapshot(
        snapshot, lifecycle=lifecycle, current_state=current_state
    )
    lifecycle_by_key = index.lifecycle_by_key
    records = _ordered_records(snapshot)

    alias_collisions = _alias_collisions(records)
    excluded_keys = frozenset(
        member.metric_key for collision in alias_collisions for member in collision.members
    )
    materialized_records = tuple(
        record for record in records if record.metric_key not in excluded_keys
    )
    keep_keys = frozenset(record.metric_key for record in materialized_records)

    ir = _authoring_ir(materialized_records, lifecycle_by_key, keep_keys)
    report = _candidate_report(ir, materialized_records, lifecycle_by_key)
    candidate = materialize_authoring_ir(ir, report)

    binding_gaps = tuple(
        sorted(
            (
                gap
                for record in records
                for gap in _binding_gaps(record, lifecycle_by_key.get(record.metric_key))
            ),
            key=lambda gap: (gap.metric_key, gap.field),
        )
    )
    materializable = not alias_collisions
    return InventoryReleaseCandidate(
        schema_version=INVENTORY_RELEASE_SCHEMA_VERSION,
        inventory_fingerprint=snapshot.fingerprint,
        lifecycle_fingerprint=_lifecycle_fingerprint(lifecycle, current_state),
        ir=ir,
        report=report,
        candidate=candidate,
        index=index,
        materialized_metric_keys=tuple(
            record.metric_key for record in materialized_records
        ),
        materializable=materializable,
        blocked_reason=(
            None
            if materializable
            else _blocked_reason(alias_collisions, excluded_keys)
        ),
        excluded_identities=_excluded_identities(records, alias_collisions),
        alias_collisions=alias_collisions,
        pruned_dependency_edges=_pruned_dependency_edges(records, excluded_keys),
        binding_gaps=binding_gaps,
    )


def bridge_metric_contract(
    content: str,
    *,
    relations: Mapping[str, str],
    inventory: MetricInventorySnapshot,
    relation_columns: Mapping[str, Iterable[str]] | None = None,
    lifecycle: LifecycleSnapshot | None = None,
    current_state: CurrentStateSnapshot | None = None,
) -> MetricContractBridge:
    """Compile an old complaint contract and compare its keys to the inventory."""

    if not isinstance(inventory, MetricInventorySnapshot):
        raise TypeError("old-contract comparison requires MetricInventorySnapshot")
    catalog = load_metric_catalog(content)
    ir = metric_catalog_ir(catalog, relations=dict(relations))
    report = validate_authoring_ir(ir, relation_columns=relation_columns)
    candidate = materialize_authoring_ir(ir, report)
    index = MetricIdentityIndex.from_snapshot(
        inventory, lifecycle=lifecycle, current_state=current_state
    )
    matches = tuple(_contract_match(contract.metric_key, index) for contract in catalog.metrics)
    return MetricContractBridge(
        catalog=catalog,
        ir=ir,
        report=report,
        candidate=candidate,
        matches=matches,
    )


def _contract_match(metric_key: str, index: MetricIdentityIndex) -> MetricContractMatch:
    outcome = index.try_resolve(metric_key)
    if isinstance(outcome, MetricResolutionFailure):
        return MetricContractMatch(
            metric_key=metric_key,
            code="not_in_inventory",
            message=(
                "old-contract metric key is not an exact inventory identity "
                f"({outcome.code.value}): {metric_key}"
            ),
        )
    return MetricContractMatch(
        metric_key=metric_key,
        code="in_inventory",
        namespace=outcome.namespace,
        legacy_only=outcome.legacy_only,
        message=f"old-contract metric key is in inventory namespace {outcome.namespace}",
    )


def _lifecycle_maps(
    lifecycle: LifecycleSnapshot | None,
    current_state: CurrentStateSnapshot | None,
    known_keys: frozenset[str],
) -> tuple[
    dict[str, LifecycleValue],
    dict[str, Literal["CURRENT_STATE", "UNSPECIFIED"]],
    str | None,
]:
    if lifecycle is not None and not isinstance(lifecycle, LifecycleSnapshot):
        raise TypeError("lifecycle must be LifecycleSnapshot")
    if current_state is not None and not isinstance(current_state, CurrentStateSnapshot):
        raise TypeError("current_state must be CurrentStateSnapshot")
    if lifecycle is not None and current_state is not None:
        raise InventoryReleaseError(
            "contradictory explicit lifecycle inputs: lifecycle and current_state"
        )
    source: LifecycleSnapshot | CurrentStateSnapshot | None = (
        lifecycle if lifecycle is not None else current_state
    )
    if source is None:
        return {}, {}, None
    lifecycle_by_key: dict[str, LifecycleValue] = {}
    state_by_key: dict[str, Literal["CURRENT_STATE", "UNSPECIFIED"]] = {}
    for record in source.records:
        key = record.metric_key
        if key not in known_keys:
            raise InventoryReleaseError(f"explicit lifecycle references unknown metric: {key}")
        if key in lifecycle_by_key:
            raise InventoryReleaseError(f"duplicate explicit lifecycle metric identity: {key}")
        lifecycle_by_key[key] = record.lifecycle
        state_by_key[key] = "CURRENT_STATE"
    return lifecycle_by_key, state_by_key, source.fingerprint


def _lifecycle_fingerprint(
    lifecycle: LifecycleSnapshot | None,
    current_state: CurrentStateSnapshot | None,
) -> str | None:
    source = lifecycle if lifecycle is not None else current_state
    return None if source is None else source.fingerprint


def _ordered_records(snapshot: MetricInventorySnapshot) -> tuple[MetricInventoryRecord, ...]:
    return tuple(
        sorted((*snapshot.canonical, *snapshot.legacy), key=lambda record: record.metric_key)
    )


def _asset_id(metric_key: str) -> str:
    return f"metric.{metric_key}"


def _declared_status(lifecycle: LifecycleValue | None) -> AssetStatus:
    # The authoring contract has no "unspecified" or "inactive" status.  Neither
    # may become ACTIVE, and RETIRED would assert a lifecycle fact the input did
    # not state, so both are marked ERROR and reported explicitly below.
    if lifecycle == "active":
        return AssetStatus.ACTIVE
    if lifecycle == "retired":
        return AssetStatus.RETIRED
    return AssetStatus.ERROR


def _metric_asset(
    record: MetricInventoryRecord,
    lifecycle: LifecycleValue | None,
    keep_keys: frozenset[str],
) -> MetricAsset:
    return MetricAsset(
        asset_id=_asset_id(record.metric_key),
        metric_key=record.metric_key,
        display_name=record.display_name,
        source_relation=None,
        status=_declared_status(lifecycle),
        domain=record.domain or "unknown",
        aliases=(),
        kind=record.metric_type,
        dependencies=tuple(
            sorted(
                {
                    item.target
                    for item in record.dependencies
                    if item.target in keep_keys
                }
            )
        ),
        unit=record.unit,
    )


def _authoring_ir(
    records: Sequence[MetricInventoryRecord],
    lifecycle_by_key: Mapping[str, LifecycleValue],
    keep_keys: frozenset[str],
) -> AuthoringIR:
    return AuthoringIR(
        metrics=tuple(
            _metric_asset(record, lifecycle_by_key.get(record.metric_key), keep_keys)
            for record in records
        )
    )


def _binding_gaps(
    record: MetricInventoryRecord, lifecycle: LifecycleValue | None
) -> tuple[InventoryBindingGap, ...]:
    if lifecycle == "retired":
        return ()
    namespace = record.provenance.source_kind
    fields: list[tuple[str, str]] = [("source_relation", "missing_source_relation")]
    if record.metric_type == "external":
        fields.append(("external_source_binding", "missing_external_binding"))
    if record.metric_type == "derived":
        fields.extend(_DERIVED_BINDINGS)
        if record.unit is None:
            fields.append(("unit", "missing_unit"))
    fields.extend(_ACTIVATION_BINDINGS)
    return tuple(
        InventoryBindingGap(
            metric_key=record.metric_key,
            namespace=namespace,
            field=field,
            code=code,
            message=(
                f"inventory identity {record.metric_key} does not supply the authoring "
                f"binding '{field}'; a full executable release stays blocked"
            ),
        )
        for field, code in fields
    )


def _lifecycle_issues(
    record: MetricInventoryRecord, lifecycle: LifecycleValue | None
) -> tuple[AuthoringIssue, ...]:
    asset_id = _asset_id(record.metric_key)
    if lifecycle is None:
        return (
            AuthoringIssue(
                "inventory_lifecycle_unspecified",
                "no explicit lifecycle input was supplied; the identity is not active and "
                "cannot be treated as active",
                asset_id,
                "status",
            ),
        )
    if lifecycle == "inactive":
        return (
            AuthoringIssue(
                "inventory_lifecycle_inactive",
                "explicit lifecycle is inactive; the authoring contract has no inactive "
                "status, so the identity stays out of any release",
                asset_id,
                "status",
            ),
        )
    return ()


def _candidate_report(
    ir: AuthoringIR,
    records: Sequence[MetricInventoryRecord],
    lifecycle_by_key: Mapping[str, LifecycleValue],
) -> ValidationReport:
    base = validate_authoring_ir(ir)
    issues: list[AuthoringIssue] = list(base.issues)
    existing = {(issue.asset_id, issue.path, issue.code) for issue in base.issues}
    lifecycle_hit: set[str] = {
        issue.asset_id for issue in base.issues if issue.code.startswith("inventory_lifecycle_")
    }
    for record in records:
        asset_id = _asset_id(record.metric_key)
        lifecycle = lifecycle_by_key.get(record.metric_key)
        for gap in _binding_gaps(record, lifecycle):
            if (asset_id, gap.field, gap.code) in existing:
                continue
            existing.add((asset_id, gap.field, gap.code))
            issues.append(
                AuthoringIssue(
                    gap.code,
                    gap.message,
                    asset_id,
                    gap.field,
                    IssueSeverity.ERROR,
                )
            )
        if asset_id in lifecycle_hit:
            continue
        for issue in _lifecycle_issues(record, lifecycle):
            issues.append(issue)
            lifecycle_hit.add(asset_id)
    issues = sorted(issues, key=_issue_sort_key)
    error_ids = {
        issue.asset_id
        for issue in issues
        if issue.severity == IssueSeverity.ERROR and issue.asset_id
    }
    statuses = dict(base.asset_statuses)
    for asset_id in error_ids:
        statuses[asset_id] = AssetStatus.ERROR
    return replace(base, issues=tuple(issues), asset_statuses=statuses)


def _issue_sort_key(issue: AuthoringIssue) -> tuple[str, str, str, str, str]:
    return (issue.asset_id, issue.path, issue.code, issue.severity.value, issue.message)


def _alias_collisions(
    records: Sequence[MetricInventoryRecord],
) -> tuple[InventoryAliasCollision, ...]:
    groups: dict[str, list[MetricInventoryRecord]] = defaultdict(list)
    for record in records:
        groups[normalize_semantic_alias(record.display_name)].append(record)
    collisions: list[InventoryAliasCollision] = []
    for label in sorted(groups):
        members = groups[label]
        keys = sorted({record.metric_key for record in members})
        if len(keys) < 2:
            continue
        ordered = sorted(members, key=lambda record: record.metric_key)
        collisions.append(
            InventoryAliasCollision(
                normalized_label=label,
                display_name=ordered[0].display_name,
                members=tuple(
                    AliasCollisionMember(
                        metric_key=record.metric_key,
                        namespace=record.provenance.source_kind,
                        source_file=record.provenance.source_file,
                    )
                    for record in ordered
                ),
                message=(
                    f"{len(keys)} distinct identities share one normalized display label "
                    f"'{ordered[0].display_name}'; disambiguate the source display name or make "
                    "a separately reviewed change to the frozen alias-uniqueness rule"
                ),
            )
        )
    return tuple(collisions)


def _excluded_identities(
    records: Sequence[MetricInventoryRecord],
    collisions: Sequence[InventoryAliasCollision],
) -> tuple[ExcludedMetricIdentity, ...]:
    by_key = {record.metric_key: record for record in records}
    excluded: list[ExcludedMetricIdentity] = []
    for collision in collisions:
        peers = tuple(member.metric_key for member in collision.members)
        for member in collision.members:
            record = by_key[member.metric_key]
            excluded.append(
                ExcludedMetricIdentity(
                    metric_key=member.metric_key,
                    namespace=member.namespace,
                    display_name=record.display_name,
                    source_file=member.source_file,
                    collision_label=collision.display_name,
                    peer_metric_keys=tuple(key for key in peers if key != member.metric_key),
                )
            )
    return tuple(sorted(excluded, key=lambda item: item.metric_key))


def _pruned_dependency_edges(
    records: Sequence[MetricInventoryRecord],
    excluded_keys: frozenset[str],
) -> tuple[PrunedDependencyEdge, ...]:
    return tuple(
        sorted(
            (
                PrunedDependencyEdge(
                    source_metric_key=record.metric_key,
                    label=dependency.label,
                    target_metric_key=dependency.target,
                )
                for record in records
                if record.metric_key not in excluded_keys
                for dependency in record.dependencies
                if dependency.target in excluded_keys
            ),
            key=lambda edge: (edge.source_metric_key, edge.label, edge.target_metric_key),
        )
    )


def _blocked_reason(
    collisions: Sequence[InventoryAliasCollision],
    excluded_keys: frozenset[str],
) -> str:
    return (
        f"{len(collisions)} display alias collisions exclude {len(excluded_keys)} identities; "
        "a single materialize_authoring_ir call cannot cover the full 280+14 inventory, so the "
        "returned candidate covers only the alias-unique subset"
    )


__all__ = [
    "INVENTORY_RELEASE_SCHEMA_VERSION",
    "AliasCollisionMember",
    "ExcludedMetricIdentity",
    "InventoryAliasCollision",
    "InventoryBindingGap",
    "InventoryReleaseCandidate",
    "InventoryReleaseError",
    "MetricContractBridge",
    "MetricContractMatch",
    "MetricIdentityIndex",
    "MetricNamespace",
    "MetricResolutionError",
    "MetricResolutionFailure",
    "PrunedDependencyEdge",
    "ResolutionCode",
    "ResolvedMetricIdentity",
    "bridge_metric_contract",
    "build_inventory_authoring_ir",
    "build_inventory_release_candidate",
    "build_metric_identity_index",
]
