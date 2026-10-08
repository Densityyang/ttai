# V4 P1 metric inventory and current-state boundary

## Scope

P1 adds a read-only, deterministic inventory snapshot and a Planner-safe
projection.  It does not publish or activate metrics, execute SQL, join the
AuthoringIR/release checksum, call an online Planner/LLM, authorize access, or
write to PostgreSQL.

Delivered so far:

- Slice 1: canonical YAML adapter plus legacy `semantic.md` adapter
  (`src/nl2sql/semantic/metric_inventory.py`).
- Slice 3: Planner-safe projection with an explicit current-state input
  (`src/nl2sql/semantic/planner_metric_projection.py`).
- Slice 2: typed canonical dependency graph, category/time mapping, and
  separated lifecycle vs source-readiness inputs.
- Slice 4 (this change): identity-layer candidate release, exact
  canonical/legacy resolution, old-contract comparison, and the fail-closed
  active-pointer rule (`src/nl2sql/semantic/inventory_release.py`).

## Authoritative inputs

The files under `tests/fixtures/v4_p1/authoritative/` are byte-preserving
copies of the authoritative sibling sources.  The adapter reads their original
YAML/Markdown shapes; it does not create a flattened 294-row mirror.

Canonical Gold YAML: 10 files, 280 unique metrics, with raw=180,
derived=56, and external=44.  The per-file SHA-256 fingerprints are recorded
by the adapter in the immutable snapshot provenance.

The copied canonical file fingerprints are:

| File | SHA-256 |
| --- | --- |
| `complaint.yaml` | `15498384e2bd404a3d0dcb379c569182dba0040ec93134b14410298890e921a0` |
| `complaint_verification.yaml` | `a4f0e50f6b3ba37e7aca4a6fe59b205b8bd28f4237516ef312e827e28c2f3295` |
| `configured_external_metrics.yaml` | `f70f8cff43bbf0e746c88be6f2c13e64d9f0ce26d47d49bd93c215d001814d79` |
| `fault_delivery_external.yaml` | `416f7ea752085c74972f209bad0e29bae6228d533c715cfd6f01c2ae04ca4b49` |
| `inspection.yaml` | `7c95f2b4eb622a98857e67e188bab4b4beaf0b7308fa041d373f684308915e33` |
| `installation.yaml` | `3d228f19c6887ae44f0386680d295b4e7cc6368c90e1cb9e4cd59e7ff944d997` |
| `repair_service.yaml` | `f69b7479086a71e06d4f4b00823b67b64b082071994f3d8a3fa04fcad47226ad` |
| `satisfaction.yaml` | `452ccfd841ac5b510cf6afc4861b114fda2235bcdb7594b2c9e1de3de1b7685e` |
| `single_fault.yaml` | `5d4bb9d7e5b8d412eeab2711d180fddfdba61c2646994cd8539a42dba6cdb449` |
| `weak_light.yaml` | `7c81b4f8f69e0b60ed1d848fd7d355a4bdcefad5184b04d65358332c1871d867` |

Legacy `semantic.md`: 93 unique metric declarations.  The source contains 75
exact canonical overlaps and four explicit `*_day/month` compact declarations
that map to canonical day/month identities, giving 79 declared overlaps.  The
remaining 14 declarations are the only `legacy_semantic` records retained in
the inventory.  Compact alias handling is an explicit allowlist; an unknown
slash shape fails closed.

The legacy file SHA-256 is
`ba03630a273b1a907fde868b8ac55386bdd41a688aa79b6c4765338431d16f11`.

## Canonical dependency graph

Every canonical `dependencies` mapping is adapted into typed
`MetricDependency(label, target)` references.  The snapshot exposes a
`dependency_graph` property rebuilt from the canonical records, which rejects:

- a target outside the 280 canonical identities;
- a metric that depends on itself;
- any dependency cycle.

Findings are deterministic `MetricDependencyIssue` values ordered by
`(code, metric_key, label, target, cycle)`, so the report is independent of
the order in which records are read.  Cycles are reported once per strongly
connected component as a canonical path starting at the smallest identity.
Legacy-only declarations do not participate in the canonical graph and
cannot carry canonical dependency edges.  The copied canonical YAML has 113
edges over 56 derived metrics and forms a valid DAG whose topological order
places every dependency before its dependent.

## Category and time mapping

`MetricInventoryRecord.categories` is the explicit typed view of the declared
dimensions whose `type` is `category`; `MetricCategory.dimension_type` is
fixed to `category` and `required` is preserved.  `time_grains` exposes the
declared grain.  44 canonical metrics declare a `category_code` category
dimension.  Empty, duplicate, or unknown-shaped category dimensions and
malformed time grains fail closed, and no category value is invented beyond
what the YAML declares.

## Lifecycle and source readiness

Canonical YAML carries no lifecycle or status field, so the inventory never
infers either fact.  Two independent, explicitly constructed inputs model
them:

- `LifecycleSnapshot` / `ExplicitMetricLifecycle` (`active` / `inactive` /
  `retired`);
- `SourceReadinessSnapshot` / `ExplicitMetricSourceReadiness` (`ready` /
  `pending_source` / `unavailable`).

`CurrentStateSnapshot` remains accepted as the lifecycle channel for backward
compatibility; supplying both it and `LifecycleSnapshot` is a contradiction
and fails closed.  Missing inputs stay `UNSPECIFIED`/unknown, and nothing
defaults to `active` or `ready`.  The projection field
`lifecycle_fingerprint` is named for what it actually carries: the
fingerprint of the resolved lifecycle channel (`LifecycleSnapshot` or
`CurrentStateSnapshot`).

The Planner projection keeps the channels separate through
`source_readiness_state`/`source_readiness` and derives `planning_readiness`:

| lifecycle | source readiness | planning_readiness |
| --- | --- | --- |
| unspecified | any | `lifecycle_unspecified` |
| active | unspecified | `active_source_unspecified` |
| active | `pending_source` | `active_pending_source` |
| active | `unavailable` | `active_source_unavailable` |
| active | `ready` | `ready` |
| inactive/retired | any | `not_active` |

Only `planning_readiness == "ready"` (and `is_planner_ready`) reports a
metric as fully ready, so an `active` metric whose source is `pending_source`
is expressible without being reported ready.  A `ready` readiness bound to a
`retired` lifecycle, a duplicate identity inside one explicit input, an
unknown metric identity, or two competing lifecycle inputs all fail closed.

## Versioning

Slice 2 changed the shape of the fingerprinted payloads: each inventory record
now carries typed dependencies and category projections, and the Planner
projection payload now includes the resolved lifecycle fingerprint and the
source-readiness fingerprint.  A fingerprint is only comparable to another
fingerprint produced by the same versioned payload shape, so every identifier
whose payload or mapping changed was bumped; otherwise the old and new
fingerprints would both claim the same version label and could not be told
apart.  The legacy adapter shape did not change and keeps its version.

| Identifier | Version |
| --- | --- |
| `INVENTORY_SCHEMA_VERSION` | `2` |
| `FINGERPRINT_SCHEMA_VERSION` | `metric-inventory-fingerprint-v2` |
| `CANONICAL_ADAPTER_VERSION` | `canonical-gold-yaml-v2` |
| `LEGACY_ADAPTER_VERSION` | `legacy-semantic-markdown-v1` (unchanged) |
| `PROJECTION_SCHEMA_VERSION` | `2` |
| `PROJECTION_FINGERPRINT_SCHEMA_VERSION` | `planner-metric-projection-fingerprint-v2` |
| `INVENTORY_RELEASE_SCHEMA_VERSION` | `1` (identity assessment; candidate checksum is the materializer's own) |

## Fingerprints

Inventory and projection fingerprints use independent, versioned canonical
JSON payloads with fixed schema/adapter versions, sorted identities,
provenance, and authoritative field fingerprints.  The inventory payload
includes each record's typed dependencies and category projections, and the
projection payload includes the lifecycle fingerprint and the source-readiness
fingerprint, so any change to a dependency, category, lifecycle, or readiness
input changes the fingerprint.  They are independent of
`AuthoringIR.checksum` and release materialization checksums.

The fixture snapshot fingerprint is
`36d0d2d27d8a426a18f40018c163abe9525b636d3f2f6531328e38e2728c8b8a`; the
Planner projection fingerprint is
`3c6a43c11fe8deb1218d79b4df7196082fe49ce027b8137a19d5e5858fcecd4d`.

## Slice 4: identity assessment, exact resolution, and a real subset candidate

`src/nl2sql/semantic/inventory_release.py` is the only bridge between the frozen
inventory and the existing authoring/materialization/registry mechanism.  It adds
no new definition data and modifies no frozen module.  It never fabricates a
`SemanticReleaseCandidate.checksum`: the one candidate it returns is the
unmodified output of a single `materialize_authoring_ir` call.

### Exact resolution

`MetricIdentityIndex` looks a `metric_key` up only as an exact identity in the
canonical (280) and legacy (14) namespaces.  A resolved identity is a typed
`ResolvedMetricIdentity` carrying the namespace, metric type, `legacy_only`,
provenance, definition checksum, and the explicit lifecycle state.  An unknown
key, a key that matches both namespaces (ambiguity), and an explicitly
`retired` key raise `MetricResolutionError` with a structured
`MetricResolutionFailure`.  There is no fuzzy, prefix, or alias matching: the
legacy compact declaration `installation_in_transit_count_day/month` is not an
identity and is never implicitly mapped to either time grain.

### Catalog/identity layer, not an executable release

The bridge builds an identity-only `AuthoringIR` (`metric_key`,
`display_name`, domain, type, unit, and canonical dependency targets) and runs
the existing `validate_authoring_ir` + `materialize_authoring_ir`.  It never
sets `source_relation`, formula, template, precision, rounding, null/zero
strategy, owner, sensitivity, or freshness, because the inventory record type
does not carry them.

`MetricInventoryRecord` deliberately drops `expression`, `source_table`,
`aggregation`, and `filters`, so an inventory metric cannot satisfy the active
release requirements of `authoring._validate_metrics`.  The candidate reports
every gap as a deterministic error (`missing_source_relation`,
`missing_formula`, `missing_template`, `missing_precision`,
`missing_rounding`, `missing_null_strategy`, `missing_zero_strategy`,
`missing_owner`, `missing_sensitivity`, `missing_freshness`,
`missing_external_binding`) plus
`inventory_lifecycle_unspecified`/`inventory_lifecycle_inactive` when no
explicit lifecycle is supplied.  `ValidationReport.ok` is therefore `False`
for both the full 294-identity assessment and the emitted subset candidate, every
materialized asset is `error`, and no metric is ever marked `active` by default.
`InventoryReleaseCandidate.create_draft` may create a DRAFT release, but the
bridge never validates or activates one.

### Frozen-materializer constraint: the full 280+14 cannot be materialized

`materialize_authoring_ir` rejects two distinct assets that share one normalized
display label.  The authoritative Gold source declares three display labels that
are each shared by two distinct canonical identities, so a single materialize call
cannot cover 280+14.  This is a real source-definition ambiguity, not an adapter
bug:

| shared label | canonical identity | source file | peer identity | peer source file |
| --- | --- | --- | --- | --- |
| 投诉工单总数（全局-日） | `complaint_total_count_overall_day` | `complaint.yaml` | `fault_reporting_total_count_overall_day` | `repair_service.yaml` |
| 投诉计算分母（全局-日） | `complaint_calc_total_count_overall_day` | `complaint.yaml` | `fault_reporting_calc_total_count_overall_day` | `repair_service.yaml` |
| 投诉计算分母（班组-日） | `complaint_calc_total_count_team_day` | `complaint.yaml` | `fault_reporting_calc_total_count_team_day` | `repair_service.yaml` |

`InventoryAliasCollision` records each group with both `metric_key`s, the shared
label, and each member's source file; `decision_required` marks it as a product
decision (disambiguate the source display name, or make a separately reviewed
change to the frozen alias-uniqueness rule).  Neither change is in this slice.
The bridge does not rename a label, does not edit the materializer, and does not
merge partial materializations.

Instead `build_inventory_release_candidate` returns an explicit blocked
assessment: `materializable=False`, a deterministic `blocked_reason`, the six
`excluded_identities` (both members of every collision), and
`release_checksum` equal to the real checksum of one
`materialize_authoring_ir(ir, report)` call over the 288 alias-unique
identities.  Eight inventory dependency edges that pointed at excluded identities
are dropped from that subset IR and listed in `pruned_dependency_edges` (label,
source, and target), so nothing disappears silently; the candidate carries the
remaining 105 dependency edges.  `ir` and `report` are exposed on the result,
so the candidate is reproducible from the disclosed inputs rather than assembled
by the bridge.

### Old complaint contract

`bridge_metric_contract` still walks the existing `load_metric_catalog` ->
`metric_catalog_ir` -> `validate_authoring_ir` -> `materialize_authoring_ir`
path, and reports every old-contract `metric_key` as `in_inventory` (with
namespace) or `not_in_inventory`.  It never silently accepts a key the inventory
does not contain, and an active contract without a deployment relation binding
still fails closed.

### Active pointer safety

`InventoryReleaseCandidate.submit` feeds the candidate through the existing
`SemanticRegistry.publish`, which validates before activating.  Because the
inventory candidate report is never `ok`, the call raises
`SemanticReleaseError`, and the active pointer, the active release state, and
the failed draft (which stays DRAFT and never becomes VALIDATED) are unchanged.

## P1 boundary as closed

P1 now delivers a verified 280+14 inventory, the canonical dependency/category
graph, the Planner-safe projection, exact identity resolution, and an identity
assessment plus a real 288-identity subset candidate (`materializable=False`,
six exclusions, three collision groups) with explicit, deterministic issues.  It
does **not** deliver an executable/active 280-metric release, physical bindings,
current-state data, authorization, or any DB/network call; it does not resolve
the three display-alias collisions, which need a source-definition decision or a
separately reviewed materializer change.  Those bindings and decisions must come
from a later approved release step (P3+ for published reads, and the backend
companion MRs for real bindings); until then every inventory metric stays
non-active and every missing binding stays visible.

## Authoritative binding (S0)

The P1 metric inventory is no longer bound to the frozen fixtures alone.  The
in-repo authoritative canonical source is `configs/semantic/gold/metrics/` (10
`*.yaml`), copied verbatim from its producer
`tt-api/src/apps/data_repository/gold/metadata/metrics`; the in-repo bound legacy
source is `configs/semantic/semantic.md`.  `verify_authoritative_sources()` in
`src/nl2sql/semantic/authoritative_sources.py` checks both against a hardcoded
SHA-256 table before the inventory is built, so a missing, extra, or drifted bound
file fails closed with that file named.  The bound copies are byte-identical to the
frozen `tests/fixtures/v4_p1/authoritative/**` bytes and reproduce the locked
inventory fingerprint
`36d0d2d27d8a426a18f40018c163abe9525b636d3f2f6531328e38e2728c8b8a` and the locked
Planner projection fingerprint
`3c6a43c11fe8deb1218d79b4df7196082fe49ce027b8137a19d5e5858fcecd4d`.
