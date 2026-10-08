"""Local-real semantic release construction (LOCAL DEMO, in-memory only).

Builds a bounded, in-memory ACTIVE SemanticRelease for the ONE frozen real-data
case.  It is deliberately NOT production semantic publication:

* no Control-DB write and no production active-pointer mutation;
* the canonical authoritative sources are VERIFIED (locked SHA-256) before any
  release is built, and their formulas are never re-authored in Python - the
  executable contracts come from the deployment binding YAML, whose eligibility
  predicates are transcribed from the canonical Gold definition;
* executability is restricted to the frozen case closure, so loading the whole
  verified inventory does not make unrelated metrics executable.

The release still has to satisfy the SAME typed contract as production: it is
bound to a VALIDATED SchemaSnapshot through bind_schema_snapshot(), so the
deterministic semantic + schema-snapshot checksum contract is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.nl2sql.local_real.deployment import FROZEN_REAL_CASE_DEPENDENCIES
from src.nl2sql.semantic.authoring import (
    validate_authoring_ir,
)
from src.nl2sql.semantic.materialization import materialize_authoring_ir
from src.nl2sql.semantic.metric_contract import (
    MetricCatalog,
    load_metric_catalog,
    metric_catalog_ir,
)
from src.nl2sql.semantic.registry import SemanticRelease, SemanticReleaseState
from src.nl2sql.semantic.schema_snapshot import (
    SchemaRequirement,
    SchemaSnapshot,
    SchemaSnapshotCandidate,
    SchemaSnapshotState,
    bind_schema_snapshot,
    validate_schema_snapshot,
)

# Provenance of this local deployment projection.  It is an infra-dev deployment
# fact, NEVER production governance authority and never a published release.
LOCAL_REAL_DEMO_PROVENANCE = "local_real_demo"
LOCAL_REAL_CONTRACT_BINDING_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "metrics" / "repair_service_local_real.yaml"
)
# The deployment owner recorded on the local projection.  It marks the contracts
# as ACTIVE for THIS deployment only; the canonical source stays authoritative.
LOCAL_REAL_DEPLOYMENT_OWNER = "local-real-demo"
# Daily grain: the deployment declares a 24h freshness SLA for this case.
LOCAL_REAL_FRESHNESS_SLA_SECONDS = 86_400

# The frozen closure identities, named for the canonical machine-check.
NUMERATOR_KEY = "repair_service_archive_on_time_count_overall_day"
DENOMINATOR_KEY = "repair_service_calc_total_count_overall_day"
KPI_KEY = "repair_service_archive_rate_overall_day"
FROZEN_REAL_CASE_VIEW_NAME = "v_repair_service"
# The canonical business source the approved view is built from.
CANONICAL_SOURCE_TABLE = "silver_repair_service"
# The RAW frozen dependencies.  Each is independently bound to the canonical
# physical source; the derived KPI declares no source_table of its own.
RAW_FROZEN_KEYS: tuple[str, ...] = (NUMERATOR_KEY, DENOMINATOR_KEY)


class LocalRealSemanticError(RuntimeError):
    """The bounded local-real semantic release cannot be constructed."""


@dataclass(frozen=True, slots=True)
class LocalRealSemanticBundle:
    """The VERIFIED local semantics: candidate, validated snapshot, release."""

    candidate: Any
    snapshot: SchemaSnapshot
    release: SemanticRelease
    metric_keys: tuple[str, ...]
    inventory_fingerprint: str
    provenance: str = LOCAL_REAL_DEMO_PROVENANCE


def verify_authoritative_source_fingerprint() -> str:
    """Verify the locked authoritative sources and return the inventory fingerprint.

    Raises AuthoritativeSourceError when any bound source is missing, extra or
    drifted, so a tampered formula can never reach a release.
    """

    from src.nl2sql.semantic.authoritative_sources import load_authoritative_inventory

    return str(load_authoritative_inventory().fingerprint)


def load_frozen_case_contracts(
    *, binding_path: Path | None = None
) -> MetricCatalog:
    """Load the frozen case deployment binding (NOT a formula definition)."""

    path = binding_path or LOCAL_REAL_CONTRACT_BINDING_PATH
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalRealSemanticError(
            "local-real contract binding is unavailable"
        ) from exc
    catalog = load_metric_catalog(content)
    if not catalog.metrics:
        raise LocalRealSemanticError("local-real contract binding is empty")
    return catalog


def build_bounded_release_candidate(
    *,
    catalog: MetricCatalog,
    relation_id: str,
    view_columns: tuple[str, ...],
    metric_keys: tuple[str, ...],
) -> Any:
    """Materialize the frozen closure, activated for THIS deployment only.

    Only the requested closure is activated; every other contract in the binding
    stays non-active, so it can never become executable here.
    """

    allowed = set(metric_keys)
    selected = tuple(m for m in catalog.metrics if m.metric_key in allowed)
    missing = sorted(allowed - {m.metric_key for m in selected})
    if missing:
        raise LocalRealSemanticError(
            "local-real contract binding is missing: " + ",".join(missing)
        )
    activated = tuple(
        type(metric).model_validate(
            {
                **metric.model_dump(mode="json"),
                # The deployment activates the contract; the semantics are
                # unchanged and still come from the bound canonical source.
                "release_status": "active",
                "owner": LOCAL_REAL_DEPLOYMENT_OWNER,
                # A published ACTIVE asset must declare its freshness SLA; the
                # canonical inventory does not carry one, so the DEPLOYMENT
                # supplies it explicitly rather than leaving it unclassified.
                "freshness_sla_seconds": LOCAL_REAL_FRESHNESS_SLA_SECONDS,
            }
        )
        for metric in selected
    )
    ir = metric_catalog_ir(
        MetricCatalog(metrics=activated),
        relations={metric.source_ref: relation_id for metric in activated},
    )
    report = validate_authoring_ir(ir, relation_columns={relation_id: frozenset(view_columns)})
    if not report.ok:
        codes = sorted({issue.code for issue in report.issues})
        raise LocalRealSemanticError(
            "local-real authoring validation failed: " + ",".join(codes)
        )
    return materialize_authoring_ir(ir, report)


def validate_relation_snapshot(
    snapshot_candidate: SchemaSnapshotCandidate,
    *,
    relation_id: str,
    required_columns: tuple[str, ...],
) -> SchemaSnapshot:
    """Validate the bounded live slice against the columns the closure requires.

    A missing relation or a missing required column is an ERROR issue, so the
    snapshot never reaches VALIDATED and the deployment fails closed.
    """

    report = validate_schema_snapshot(
        snapshot_candidate,
        requirements=(SchemaRequirement(relation_id, tuple(required_columns)),),
    )
    if not report.ok:
        codes = sorted({
            issue.code
            for issue in report.issues
            if issue.severity.value == "error"
        })
        raise LocalRealSemanticError(
            "local schema snapshot failed validation: " + ",".join(codes)
        )
    now = datetime.now(UTC)
    return SchemaSnapshot(
        snapshot_id=str(uuid4()),
        state=SchemaSnapshotState.VALIDATED,
        candidate=snapshot_candidate,
        validation_report=report.to_dict(),
        created_at=now,
        validated_at=now,
    )


def build_local_active_release(
    *,
    candidate: Any,
    snapshot: SchemaSnapshot,
    change_summary: str = "local-real in-memory semantic release",
) -> SemanticRelease:
    """Bind the VALIDATED snapshot and construct the in-memory ACTIVE release.

    The bound checksum is the existing deterministic semantic + schema-snapshot
    checksum; nothing here invents a snapshot id or checksum.
    """

    bound = bind_schema_snapshot(candidate, snapshot)
    if bound.schema_snapshot_id != snapshot.snapshot_id:
        raise LocalRealSemanticError("local release snapshot id mismatch")
    if bound.schema_snapshot_checksum != snapshot.checksum:
        raise LocalRealSemanticError("local release snapshot checksum mismatch")
    return SemanticRelease(
        release_id=str(uuid4()),
        version=1,
        checksum=bound.checksum,
        state=SemanticReleaseState.ACTIVE,
        documents=bound.documents,
        validation_report=dict(bound.validation_report),
        change_summary=change_summary,
        previous_release_id=None,
        created_at=datetime.now(UTC),
        schema_version=bound.schema_version,
        parser_version=bound.parser_version,
        schema_snapshot_id=bound.schema_snapshot_id,
        schema_snapshot_checksum=bound.schema_snapshot_checksum,
    )


def executable_metric_keys(release: SemanticRelease) -> tuple[str, ...]:
    """The ACTIVE executable metric identities of a release."""

    from src.nl2sql.orchestration.typed_runtime import executable_metric_contracts

    return tuple(sorted(c.metric_key for c in executable_metric_contracts(release)))


def assert_release_is_bounded(
    release: SemanticRelease, *, metric_keys: tuple[str, ...]
) -> None:
    """Fail closed unless executability is EXACTLY the frozen closure.

    This proves SET EQUALITY, not merely the absence of unexpected metrics: a
    missing frozen metric is just as much a defect as a stray one.
    """

    executable = set(executable_metric_keys(release))
    expected = set(metric_keys)
    unexpected = sorted(executable - expected)
    if unexpected:
        raise LocalRealSemanticError(
            "local release exposes an unrelated executable metric: "
            + ",".join(unexpected)
        )
    missing = sorted(expected - executable)
    if missing:
        raise LocalRealSemanticError(
            "local release is missing a frozen executable metric: " + ",".join(missing)
        )
    if release.state is not SemanticReleaseState.ACTIVE:
        raise LocalRealSemanticError("local release is not ACTIVE")


def load_canonical_gold_metrics() -> dict[str, Any]:
    """The VERIFIED canonical Gold metric definitions for the frozen case.

    Parsed from the SHA-256-verified canonical YAML, so these facts come from the
    authoritative source rather than from the separately transcribed deployment
    binding under test.
    """

    import yaml

    from src.nl2sql.semantic.authoritative_sources import AUTHORITATIVE_CANONICAL_DIR

    path = AUTHORITATIVE_CANONICAL_DIR / "repair_service.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = document.get("metrics") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise LocalRealSemanticError("canonical source has no metric list")
    result: dict[str, Any] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            result[entry["name"]] = entry
    return result


def _canonical_predicates(entry: Any) -> set[tuple[str, str]]:
    """The canonical filter set of one raw Gold metric, as (field, operator)."""

    filters = entry.get("filters") if isinstance(entry, dict) else None
    if not isinstance(filters, list):
        return set()
    result: set[tuple[str, str]] = set()
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = item.get("column")
        operator = item.get("operator")
        if not isinstance(field, str) or not isinstance(operator, str):
            continue
        # The deployment vocabulary expresses the same facts with its own
        # operators; only the CANONICAL not_null/eq facts are mapped here.
        mapped = {"not_null": "is_not_null", "is_null": "is_null"}.get(operator)
        if operator == "eq" and item.get("value") is True:
            result.add((field, "is_true"))
        elif mapped is not None:
            result.add((field, mapped))
    return result


def assert_binding_matches_canonical(
    *,
    catalog: MetricCatalog,
    canonical: Mapping[str, Any] | None = None,
) -> None:
    """Machine-check the DEPLOYMENT BINDING against the VERIFIED canonical source.

    A verified source SHA proves the canonical bytes are intact; it does NOT prove
    that the separately transcribed deployment binding still agrees with them.
    This comparison does, and fails closed with a stable reason otherwise.

    The deployment YAML is a TECHNICAL binding, never an independent business
    semantic authority.
    """

    source = canonical if canonical is not None else load_canonical_gold_metrics()
    by_key = {metric.metric_key: metric for metric in catalog.metrics}
    # A. identity: the binding may neither invent nor drop a frozen identity.
    if set(by_key) != set(FROZEN_REAL_CASE_DEPENDENCIES):
        raise LocalRealSemanticError("local_real_binding_canonical_mismatch: identity")
    for key in sorted(by_key):
        metric = by_key[key]
        entry = source.get(key)
        if not isinstance(entry, dict):
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: identity " + key
            )
        # B. display identity.
        if str(entry.get("display_name", "")).strip() != metric.display_name.strip():
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: display_name " + key
            )
        # C. business time.  A DERIVED metric declares no time_column of its own,
        # so it must inherit the business time of the raw dependencies it is
        # defined over; a raw metric must match its canonical time column exactly.
        canonical_time = entry.get("time_column")
        if isinstance(canonical_time, str) and metric.business_time_column != canonical_time:
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: business_time " + key
            )
        if not isinstance(canonical_time, str) and metric.operation == "ratio":
            inherited = {
                source.get(target, {}).get("time_column")
                for target in (entry.get("dependencies") or {}).values()
            }
            if inherited != {metric.business_time_column}:
                raise LocalRealSemanticError(
                    "local_real_binding_canonical_mismatch: business_time " + key
                )
        # H. time grain.
        canonical_grain = entry.get("time_grain")
        if isinstance(canonical_grain, str) and tuple(metric.supported_grains) != (
            canonical_grain,
        ):
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: grain " + key
            )
        # I. unit / value meaning.  A ratio carries its unit on the ratio block;
        # a raw count carries the canonical unit string directly.
        canonical_unit = entry.get("unit")
        if isinstance(canonical_unit, str) and canonical_unit.strip():
            if metric.operation == "ratio":
                if canonical_unit.strip() != "%" or metric.ratio is None:
                    raise LocalRealSemanticError(
                        "local_real_binding_canonical_mismatch: unit " + key
                    )
                if metric.ratio.unit != "percent" or metric.ratio.value_scale != "0_100":
                    raise LocalRealSemanticError(
                        "local_real_binding_canonical_mismatch: unit " + key
                    )
            elif metric.operation != "count":
                raise LocalRealSemanticError(
                    "local_real_binding_canonical_mismatch: unit " + key
                )
        # E/F. raw eligibility must agree exactly with the canonical filters.
        if metric.operation == "count":
            canonical_filters = _canonical_predicates(entry)
            declared = {(item.field, item.operator) for item in metric.predicates}
            if declared != canonical_filters:
                raise LocalRealSemanticError(
                    "local_real_binding_canonical_mismatch: raw_semantics " + key
                )
            # The aggregation identity is the canonical count(id).
            aggregation = entry.get("aggregation")
            if isinstance(aggregation, dict):
                if aggregation.get("operation") != metric.operation:
                    raise LocalRealSemanticError(
                        "local_real_binding_canonical_mismatch: aggregation " + key
                    )
                if aggregation.get("column") != "id":
                    raise LocalRealSemanticError(
                        "local_real_binding_canonical_mismatch: aggregation " + key
                    )
    # D. source mapping: the binding may only reference the approved view, which
    # is built from the canonical business source.
    if {metric.source_ref for metric in catalog.metrics} != {FROZEN_REAL_CASE_VIEW_NAME}:
        raise LocalRealSemanticError("local_real_binding_canonical_mismatch: source_ref")
    # EVERY raw frozen dependency must be bound to the ONE approved physical
    # source.  Checking only the numerator would let denominator source-table
    # drift through, silently admitting a second physical source.
    for raw_key in RAW_FROZEN_KEYS:
        entry = source.get(raw_key)
        if not isinstance(entry, dict):
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: source_table"
            )
        if entry.get("source_type") != "raw":
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: source_type " + raw_key
            )
        if entry.get("source_table") != CANONICAL_SOURCE_TABLE:
            raise LocalRealSemanticError(
                "local_real_binding_canonical_mismatch: source_table " + raw_key
            )
    # G. derived dependency graph.
    _assert_derived_dependencies(by_key, source.get(KPI_KEY))


def _assert_derived_dependencies(by_key: Mapping[str, Any], entry: Any) -> None:
    """G: on_time/total must resolve to the canonical dependency identities."""

    kpi = by_key.get(KPI_KEY)
    if kpi is None or kpi.ratio is None:
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )
    dependencies = entry.get("dependencies") if isinstance(entry, dict) else None
    if not isinstance(dependencies, dict):
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )
    if dependencies.get("on_time") != NUMERATOR_KEY:
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )
    if dependencies.get("total") != DENOMINATOR_KEY:
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )
    denominator = {
        (item.field, item.operator) for item in kpi.ratio.denominator_predicates
    }
    numerator = {(item.field, item.operator) for item in kpi.ratio.numerator_predicates}
    if denominator != {("completion_receipt_time", "is_not_null")}:
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )
    if ("is_archive_on_time", "is_true") not in numerator:
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )
    # The numerator must remain an explicit SUBSET of the denominator, matching
    # the canonical CASE expression rather than restating it as a formula.
    if not denominator <= numerator:
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: numerator_not_subset"
        )
    if kpi.operation != "ratio" or kpi.ratio.unit != "percent":
        raise LocalRealSemanticError(
            "local_real_binding_canonical_mismatch: derived_dependencies"
        )

def build_local_real_semantics(
    *,
    metric_keys: tuple[str, ...],
    relation_id: str,
    view_columns: tuple[str, ...],
    snapshot_candidate: SchemaSnapshotCandidate,
    required_columns: tuple[str, ...],
    catalog: MetricCatalog | None = None,
    binding_path: Path | None = None,
) -> LocalRealSemanticBundle:
    """Construct the bounded local-real semantics.

    Order is fixed and fail-closed: verify the authoritative sources, load the
    deployment binding, materialize the activated closure, validate the live
    snapshot, bind it, then build the in-memory ACTIVE release.
    """

    fingerprint = verify_authoritative_source_fingerprint()
    contracts = catalog or load_frozen_case_contracts(binding_path=binding_path)
    # SHA verification alone does NOT prove the deployment binding still agrees
    # with the canonical source; this machine-check does, before materialization.
    assert_binding_matches_canonical(catalog=contracts)
    candidate = build_bounded_release_candidate(
        catalog=contracts,
        relation_id=relation_id,
        view_columns=view_columns,
        metric_keys=metric_keys,
    )
    snapshot = validate_relation_snapshot(
        snapshot_candidate,
        relation_id=relation_id,
        required_columns=required_columns,
    )
    release = build_local_active_release(candidate=candidate, snapshot=snapshot)
    assert_release_is_bounded(release, metric_keys=metric_keys)
    return LocalRealSemanticBundle(
        candidate=candidate,
        snapshot=snapshot,
        release=release,
        metric_keys=metric_keys,
        inventory_fingerprint=fingerprint,
    )


__all__ = [
    "LOCAL_REAL_CONTRACT_BINDING_PATH",
    "LOCAL_REAL_DEMO_PROVENANCE",
    "LOCAL_REAL_DEPLOYMENT_OWNER",
    "LocalRealSemanticBundle",
    "LocalRealSemanticError",
    "assert_release_is_bounded",
    "build_bounded_release_candidate",
    "build_local_active_release",
    "build_local_real_semantics",
    "executable_metric_keys",
    "load_frozen_case_contracts",
    "validate_relation_snapshot",
    "verify_authoritative_source_fingerprint",
]