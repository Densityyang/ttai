# P2-S1 Slice 2B — RelationCoverage + typed organization-scope/coverage enforcement — READ-ONLY RECON

Repo: `E:/平台开发/ttai-pr07a-next`
Branch: `agent/v4-p2-s1-authorization-contract`
HEAD: `161189cfadf097247859e506a94dce0bc3b5763f` ("test: prove a disabled context cannot stamp a receipt")
Scope: read-only, evidence-first recon for slice 2B only. The **only** file written is this report.
No src/, tests/, docker/, migrations, .github/, or config was created or modified; no dependency installed;
no test suite run; nothing staged/committed/pushed. Line numbers are from the HEAD above.

Frozen semantics respected throughout: Backend/DB owns organization truth and effective authorization and
returns the final Agent-facing scope vocabulary `city_company | area | team | employee`;
tt-ai does not reinterpret `org_type`, does not resolve users to roles, and does not derive employee,
sibling, ancestor or hierarchy scope. RelationCoverage is tt-ai semantic/release metadata over
DB/Backend-owned physical facts, not a second IAM model. Raw scope ids are opaque and not globally unique
across levels; every membership test must be level-aware and fail closed.

---

## 0. Brief-accuracy flags (checked against HEAD, not worked around)

| # | Brief statement | Reality at HEAD |
|---|---|---|
| 1 | "while `evaluate_config_hyz` has zero active runtime call sites" | **No symbol `evaluate_config_hyz` exists anywhere.** The intended function is `evaluate_config_authorization` (`src/nl2sql/ownership.py:94-123`). It too has **zero non-test call sites** — repo-wide grep finds only its definition and `tests/unit/test_authorization_wiring.py` (import L31; calls L209-390). The *substance* of the correction is correct; the *name* in the brief is wrong. |
| 2 | "Q2: is `aggregate_coverage` only a declared field that nothing reads?" | **It is read and enforced.** `src/nl2sql/orchestration/metric_query.py:293-294` raises `PlanStepError("metric_aggregate_coverage_unapproved")` when an approved aggregate's metric key is absent from `relation.aggregate_coverage`. It is *not* dead metadata, but it is enforced on only one of two source paths (see §2). |
| 3 | "`RelationCoverage`" | **No such type/symbol exists** in `src/` or `tests/`. The only occurrence is a docstring reference in `src/nl2sql/contracts.py:212`. Slice 2B must author it (or an equivalent). |
| 4 | "`_select_source` ... in metric_query.py" | Present, but at `metric_query.py:468-522` (not the ~L460 the prior recon note used). |
| 5 | "`OrganizationDimensionBinding` (dimension literal city_company|area|team, NO employee)" | Confirmed: `metric_query.py:60`; `MetricContract.supported_dimensions` also excludes `employee` (`src/nl2sql/semantic/metric_contract.py:64`). |
| 6 | "query gateway already enforces schema across every table in every scope including joins" | Confirmed: `PolicyEngine._enforce_schema` iterates all `traverse_scope` scopes and all `scope.tables` (`query_gateway.py:413-445`); cartesian joins banned (`349-371`). |

---

## 1. INVENTORY — what exists today

### 1a. Relation policy / schema snapshot — `src/nl2sql/semantic/schema_snapshot.py`
- `RelationPolicy` (L78-83, frozen slots dataclass): `sensitivity="unclassified"`, `sensitive_columns=()`,
  `aggregate_coverage=()`, `freshness_sla_seconds=None`. **This is the only relation-level policy shape.**
- `RelationSnapshot` (L86-103) mirrors those four fields plus physical catalog facts (columns, PK, FKs,
  indexes, partition/parent, estimated_rows, total_bytes).
- `_POLICY_FIELDS` (L33-38) is the closed allow-list for policy JSON: exactly `aggregate_coverage`,
  `freshness_sla_seconds`, `sensitive_columns`, `sensitivity`. `load_relation_policies` (L583-632)
  rejects any unknown field (L600-605) and only coerces the four fields.
- `SchemaRequirement` (L106-109), `SchemaSnapshotCandidate` (L161-184: `to_payload`, `relation_columns`),
  `SchemaSnapshot` (L187-198: state + candidate + validation_report; `checksum` property = candidate checksum).
- `validate_schema_snapshot` (L635-767): empty snapshot (error), relation bound (error), required
  relation/column missing (error), no columns (warning), `sensitivity_unclassified` (**warning**),
  `freshness_unknown` (warning), `invalid_freshness_sla` (error), `sensitive_column_missing` (error),
  then `_schema_drift_issues` (L770-839). **There is no coverage issue code and no coverage check.**
- `ControlSchemaSnapshotStore` (L842-...): `publish` persists `candidate.to_payload()` + report into
  `schema_snapshots` (L868-953); `read` reloads. Only path `load_relation_policies` is used is the
  module CLI (`L1487`); otherwise tests.

### 1b. Metric query compiler — `src/nl2sql/orchestration/metric_query.py`
- `EligibilityPolicy` (L50-54). `OrganizationDimensionBinding` (L57-71): `dimension: Literal["city_company","area","team"]`,
  `field: Identifier | None`, `value_type: Literal["text","integer"] | None`; `city_company` is a total scope
  with no ID column (L66-68), area/team require a typed stable ID column (L69-70).
- `AggregateColumns` (L74-96), `AggregateContract` (L99-109), `SourceFreshnessRecord` (L112-136).
- `RelationBinding` (L139-173): `relation_asset_id`, `schema_name/relation_name`, `allowed_columns`,
  `required_permissions`, `approved: Literal[True]`, `timestamp_kind`, `max_days`,
  `organization_dimensions` (L155-157, default = city_company only), `aggregate`, detail fallback flags.
- `MetricQueryCompiler.__init__` (L195-219) takes **`identity: RequestIdentity` only** (L203) — no
  `AuthorizationContext`. `compile` (L221-265) re-runs `PlanValidator().validate_query_plan` (L224-228),
  requires active release + snapshot + contract, permission check L258-260, then `_select_source`.
- `_compile_source` (L267-466): permission (L273-277), relation approval from context + active release +
  snapshot (L278-289), aggregate sensitivity (L291-292), **aggregate_coverage (L293-294)**, column
  allow-list/classification check (L309-311), type checks, scan caps, then `_organization_scope` (L301).
- `_organization_scope` (L671-701): maps plan dimensions/filters named `city_company|area|team` onto the
  binding's `organization_dimensions`, and **binds org filter values to physical ID columns** in the
  filter loop (L382-411). It never consults the caller's authorized scope ids.
- `_select_source` (L468-522): aggregate-first with detail fallback; only codes in `_SOURCE_REJECTIONS`
  (L41-47) are degradable, anything else aborts the compile.
- `GatewayMetricStepRunner` (L550-635), `metric_plan_executor` (L638-640). **Neither is wired into a
  production call site** (repo-wide grep: only tests/integration construct them; `engine.py:119` builds a
  bare `PlanValidator()`). The compiler is dormant, which is exactly the "active engine enforcement is LATER" gate.

### 1c. Query gateway — `src/nl2sql/infra/governance/query_gateway.py`
- `QueryErrorCode` (L41-58): `invalid_sql, parameter_mismatch, parameter_invalid, policy_denied,
  schema_denied, plan_failed, cost_exceeded, rows_exceeded, capacity_exceeded, timeout, connection_error,
  transient_database_error, permission_denied, relation_not_found, database_error, result_too_large,
  audit_unavailable`.
- `PreparedQuery` (L68-80): `sql, bind_sql, fingerprint, parameter_names, tables, max_rows, data_scope,
  policy_version`. `data_scope` is **the allowed *schema* name only** (set at L324 from `allowed_schema`),
  not an organization scope.
- `PolicyEngine.prepare` (L276-325): single read-only query, forbidden-expression/function checks,
  complexity + cartesian-join ban (L349-371), function namespace check (L373-394), then
  `_enforce_schema` (L312 → L413-445) across *all* scopes/tables, then `_apply_limit` (L313/L447-474).
  `_begin_read_only` (L801-817) asserts read-only transaction per execution. The gateway has **no semantic
  scope vocabulary** and receives only SQL text.

### 1d. Plan validation — `src/nl2sql/orchestration/planning.py`
- `PlanValidator` (L64-88). `validate_query_plan` (L90-190) takes `plan, context, identity` — **no
  AuthorizationContext**. Deny/clarify precedence: `issues = tuple(deny or clarify)` and
  `outcome = "deny" if deny else "clarify" if clarify else "allow"` (L181-182).
- `validate_execution_plan` (L192-291) precedence: `deny` beats `approval` beats `allow` (L282-283).
- Existing query-plan checks: context conflict/incomplete (L103-118), unresolved slots (L120-128),
  domain permitted (L130-137), metrics resolved (L139-147), plan permissions (L149-162), detail-source
  approval (L164-171), detail strategy match (L172-179). No scope/coverage check.

### 1e. Context compilation — `src/nl2sql/semantic/context_compiler.py`
- `ContextCompiler.compile` (L91-148) fuses release-scoped evidence; takes no identity and no plan.
- `SemanticContextResolver.resolve` (L168-250) receives `identity: RequestIdentity`, calls the
  identity-scoped `PolicyScopedEvidenceProvider.retrieve_permitted` (L175-178), and emits
  `approved_relation_ids` (L242) and `approved_edge_ids` (L243). It carries **no scope level, no allowed
  scope ids, and no coverage metadata**. The workspace `contracts.ContextBundle` (L313-350) has no scope
  fields either.

### 1f. Authorization contract (slice 2A, frozen)
- `ScopeLevel = Literal["city_company","area","team","employee"]` (`contracts.py:27`).
- `AuthorizationContext` (L60-117): `schema_version`, `authorization_revision` (opaque, non-blank),
  `agent_enabled`, `scope_level`, `allowed_scope_ids` (unique within one level, L99-113).
- `evaluate_authorization` (L173-237): canonical typed membership; `(level,id)` must be paired else deny
  (L227-231), level must equal `context.scope_level` (L230), id must be in `allowed_scope_ids` (L232);
  all failures collapse to the one public `AUTHORIZATION_DENIED` (`AUTHORIZATION_DENIED_REASON` L129; value
  L167-170). Exact wording on typed scope at L203-212.
- `RequestContext.authorization` (L57), comment L50-56. `ExecutionReceipt.authorization_revision`
  (L753-757). `SourceDegradation` enum (L17-25).
- `evaluate_config_authorization` (`ownership.py:94-123`) is the declared "single fail-closed enforcement
  seam for a runtime configurable" but has **zero runtime call sites**; `runtime_config` (`ownership.py:31-61`)
  projects the context under `AUTHORIZATION_CONFIG_KEY` (L21), yet `v2.py:122-128` builds `RequestContext`
  without `authorization`, so production never populates it today.
- `BackendAuthorizationProvider` / `load_authorization` (`src/core/auth/provider.py:24-66`) is a
  **contract only** — "no HTTP call, endpoint, or payload mapping is defined" (L28-31); no production caller.

---

## 2. What `aggregate_coverage` means today, and whether it is enforced

**Meaning.** It is a per-relation tuple of *metric keys* the approved relation is allowed to serve as an
approved aggregate. Declared on `RelationPolicy` (`schema_snapshot.py:82`) and `RelationSnapshot` (L102),
loaded from the policy JSON allow-list (L625-629), and serialized into the candidate payload (L1228). It is
**not** an organization-row coverage or scope concept.

**Enforcement.** Exactly one runtime reader/check:
- `metric_query.py:293-294` — `if binding.aggregate is not None and metric.metric_key not in relation.aggregate_coverage: raise PlanStepError("metric_aggregate_coverage_unapproved")`.
- It fires **only on the approved-aggregate path**; the approved-detail path (`binding.aggregate is None`,
  L375-411) never consults it. The detail path is validated for columns/types/rows but not coverage.
- The code is in `_SOURCE_REJECTIONS` (L41-47), so an uncovered aggregate degrades to detail fallback when
  allowed (proven by `tests/unit/test_metric_sources.py:285,303`).

**Searches performed to prove no other reader.** `grep aggregate_coverage` over `src/` returns only:
definition/load/serialize sites in `schema_snapshot.py` (L34, 82, 102, 557, 625-628, 1228, 1326-1327) and
the enforcement site `metric_query.py:293-294`. `validate_schema_snapshot` (L635-767) contains **no**
reference to `aggregate_coverage`.

**Checksum/drift nuance (material for 2B).** `aggregate_coverage` is included in `_candidate_payload` →
`_relation_payload` (L1228) and therefore in `candidate.checksum` (which release binding compares,
`metric_query.py:239`). It is **excluded** from `_relation_structure_payload` (L1264-1274), which is what
`_schema_drift_issues` compares (L803-838). So coverage changes alter the release-bound candidate
checksum but are **not** surfaced as a drift issue — a gap any new coverage metadata inherits.

---

## 3. Exact gap between what exists and slice 2B

**Missing metadata (tt-ai must AUTHOR as semantic/release metadata).**
- PROPOSED: extend `RelationPolicy` / `RelationSnapshot` with a `RelationCoverage`-shaped block carrying
  (a) the org level the relation's rows are rooted at, (b) the physical column holding the org id,
  (c) the minimum query org level at which the relation may be queried, (d) a detail sensitivity class.
  Field names/encodings are a design decision; the report only fixes *what* is missing.
- A coverage validation rule in `validate_schema_snapshot`: today nothing rejects a relation whose
  declared coverage is absent, unclassified, or references a non-existent column. Existing precedents to
  mirror: `sensitivity_unclassified`, `freshness_unknown`, `sensitive_column_missing`,
  `required_column_missing` (`schema_snapshot.py:707-746`).
- A level-aware coverage/scope decision shape consumed by the compiler (PROPOSED; could reuse
  `PolicyDecision` `contracts.py:240-245`).

**Missing check (tt-ai must implement the *gate*, not the *truth*).**
- The compiler maps org filters to ID columns (`metric_query.py:382-411, 671-701`) but never checks
  that (i) the plan's org level is within the relation's coverage and (ii) each org filter value is a
  member of the caller's trusted `AuthorizationContext.allowed_scope_ids` **at the matching level**.
  A `team` filter and an `area` context with a colliding raw id are currently indistinguishable —
  the exact collision the frozen semantics forbid.

**Missing trust boundary.**
- The trusted context exists (`AuthorizationContext`, `evaluate_authorization`) but does not reach the
  metric path: `MetricQueryCompiler.__init__` takes only `RequestIdentity` (L203); `PlanValidator`
  takes only identity; `ContextBundle` has no scope fields; `v2.py:122-128` never sets
  `RequestContext.authorization`; `evaluate_config_authorization` has zero runtime call sites.
  So slice 2B must define **where** the trusted context is injected into the dormant compiler path
  (constructor injection), not wire a live engine gate.

**tt-ai AUTHOR vs tt-ai CONSUME.**
- AUTHOR: RelationCoverage metadata, its loading/validation/checksum participation, drift treatment, and
  the level-aware coverage+membership gate.
- CONSUME: `AuthorizationContext.scope_level`, `allowed_scope_ids`, `authorization_revision`,
  `agent_enabled`; the final scope vocabulary; and any Backend-supplied per-relation org field/level.
  tt-ai must not derive hierarchy, siblings, ancestors, employee scope, or roles.

---

## 4. Where coverage and typed scope enforcement should BIND

| Candidate seam | Evidence | Verdict |
|---|---|---|
| **Context compilation** (`context_compiler.py:91-250`) | Builds identity-scoped `approved_relation_ids`; takes no plan and no AuthorizationContext; has no physical column/snapshot access | **No** for scope/coverage enforcement. It may at most carry release metadata forward. |
| **Plan validation** (`planning.py:90-190`) | Runs before compile, but sees only `plan, context (no scope), identity`; no binding, no snapshot, no coverage | **Precheck only, not authority.** Could reject an obviously out-of-coverage plan, but cannot bind a physical column or prove id membership. Keep it non-authoritative to avoid a second model. |
| **Metric query compiler** (`metric_query.py:_compile_source` L267-466; `_organization_scope` L671-701) | The unique point where metric + `RelationBinding` + snapshot `RelationSnapshot` + plan filters meet; already enforces `aggregate_coverage` (L293) and maps scope→ID column (L382-411) | **Correct for BOTH coverage and typed scope.** Coverage gate beside L293; typed membership gate in the filter loop (`_organization_scope`/L382-411), reusing `evaluate_authorization` semantics. |
| **Query gateway schema enforcement** (`query_gateway.py:312,413-445`) | Enforces schema across every table/scope incl. joins, but receives only SQL text and knows no org vocabulary | **Defense-in-depth only.** Do not put scope authority here; it cannot be level-aware and would duplicate Backend truth. |

Rationale: the compiler is the only seam that simultaneously holds the trusted `(level,id)` request, the
relation's coverage metadata, and the physical org column binding. It is also already the established
fail-closed boundary for relation/source eligibility, and it is currently dormant — satisfying the gate
that active engine enforcement is a later slice. PROPOSED constructor change: inject the trusted
`AuthorizationContext` (or a decision primitive) into `MetricQueryCompiler.__init__` (L195-219).

---

## 5. Fail-closed surface and existing deny vocabulary

- **Uncovered relation** (plan org level outside relation coverage; aggregate metric not covered): deny in
  `_compile_source`, reusing `metric_aggregate_coverage_unapproved` (`contracts.py:19`,
  `metric_query.py:294`). An org-coverage denial has **no existing literal**; PROPOSED to either reuse
  `metric_grain_or_dimension_unsupported` / `metric_dimension_combination_unsupported` or add a new
  `SourceDegradation` literal (labelled PROPOSED).
- **Insufficient/cross-level scope**: deny through `evaluate_authorization`'s canonical
  `authorization_denied` (`contracts.py:129,167-170,214-218`), which already collapses unpaired/cross-level/
  out-of-set requests to one indistinguishable deny. The compiler should surface it as an authorization
  denial, not invent a new public shape.
- **Unclassified column**: deny with `metric_column_unapproved` (`contracts.py:20`,
  `metric_query.py:309-311`); the column check already fails on `used & set(relation.sensitive_columns)`
  and on columns outside `allowed_columns`.
- **Critical degradation constraint:** only codes in `_SOURCE_REJECTIONS` (`metric_query.py:41-47`) may
  degrade to the detail fallback; a coverage/scope denial must be **non-degradable** so it cannot be
  laundered into a detail read. Any new code must be placed deliberately (either outside the rejection set
  to abort, or handled so it never selects a fallback).
- **Snapshot validation:** new coverage issues should reuse the `SchemaSnapshotIssue` machinery
  (`schema_snapshot.py:112-127`) with severity ERROR for missing/invalid coverage and WARNING for
  unclassified coverage, mirroring L707-746.

---

## 6. Open items that require real Backend evidence (deliverable — do not guess)

1. Whether Backend/DB populates any per-relation organization coverage metadata at all, and through which
   contract/endpoint (none is defined; `provider.py:28-31` is explicitly a contract stub).
2. The physical column(s) carrying org ids per relation, their types (`text` vs `integer`), and stability.
3. Whether raw scope ids are in practice unique within a level, and what guarantees Backend gives.
4. Whether Backend can ever return `employee` scope for a metric query. `OrganizationDimensionBinding`
   and `MetricContract.supported_dimensions` exclude `employee` (`metric_query.py:60`,
   `metric_contract.py:64`), so an `employee` context currently has no eligible binding and must deny —
   confirm this is the intended product behaviour.
5. Whether `city_company` is genuinely a total scope requiring no ID column (assumed at
   `metric_query.py:66-68`).
6. Whether Backend returns exactly one `scope_level` per context (contract assumes so,
   `contracts.py:74-82`) or ever a multi-level/hierarchical set.
7. Whether a per-relation coverage root/minimum query level is derivable from `vadmin_data_resource` or
   any Backend source, or must be authored as tt-ai Release metadata (prior recon evidence reported that
   table as table-level only with 0 rows; re-confirm against current Backend).
8. How `aggregate_coverage` relates to organization coverage — extend the same field or add a distinct
   `RelationCoverage` block.
9. The closed vocabulary for sensitivity / detail classification: today `sensitivity` is a free string
   defaulting to `"unclassified"` (`schema_snapshot.py:80,606-610`), with no enum. Any coverage gate that
   keys on sensitivity needs an agreed closed set.
10. `authorization_revision` rotation semantics for resume/HITL revalidation and the exact time at which
    a context is considered stale.
11. Whether Backend can supply a per-request effective scope that changes mid-conversation, and the
    invalidation/checkpoint implications.
12. Whether the org id column mapping is tamper-proof (i.e., Backend-owned) so tt-ai may treat it as a
    physical fact rather than a tt-ai assertion.

---

## 7. Test surfaces a slice 2B change would touch

Existing modules/fixtures:
- `tests/metric_fixtures.py` — synthetic relations/bindings: `RelationSnapshot(... aggregate_coverage=())`
  L82; `organization_dimensions` L106-110; `AggregateAuthority` builds the aggregate relation with
  `aggregate_coverage=(metric.metric_key,)` L171 and the aggregate binding L182-198.
- `tests/unit/test_schema_snapshot.py` — `_policies()` L154-167 (includes `aggregate_coverage` L159);
  policy JSON round-trip L363-384; `build_schema_snapshot_candidate` L178-187.
- `tests/unit/test_metric_sources.py` — coverage denial parametrized L285 and fixture mutation L303;
  fallback semantics L289-322.
- `tests/unit/test_metric_query.py` / `tests/unit/test_metric_ratio_query.py` — compiler + runner.
- `tests/unit/test_plan_pipeline.py` — `PlanValidator` precedence L443-665; engine pipeline.
- `tests/unit/test_authorization_contracts.py` — typed/cross-level membership L106-110, closed vocabulary
  L116-124, unpaired/cross-level denies L175-192, canonical deny equivalence L343-355.
- `tests/unit/test_authorization_wiring.py` — `evaluate_config_authorization` L209-390, `runtime_config`
  key L103-136.
- `tests/integration/test_postgres_governance.py` — `aggregate_coverage` fixture L822.
- `tests/integration/test_query_gateway_postgres.py` — `metric_plan_executor` execution L368-375,
  L466-468, L685-695, L747-751.

Fail-closed cases a 2B change should add:
1. Relation declared with no/insufficient coverage ⇒ snapshot validation ERROR and compiler deny.
2. Plan org level below the relation's minimum coverage level ⇒ deny (non-degradable).
3. `team` filter value equal to an authorized `area` id ⇒ deny (cross-level collision; level-aware).
4. Unpaired scope (level without id, or id without level) ⇒ canonical authorization deny.
5. `employee` context with no eligible binding ⇒ deny, never widen.
6. Org column present in the plan but not in `allowed_columns`/coverage ⇒ deny with
   `metric_column_unapproved`.
7. Coverage denial must NOT appear as `source_degradation`/detail fallback.

---

## 8. Risks of accidentally creating a second IAM model / duplicating Backend authority

1. **Hierarchy re-derivation.** Encoding ancestor/sibling/area→team/team→employee expansion in
   RelationCoverage would recreate an IAM model. Avoid: coverage is opaque per-relation physical/semantic
   metadata; membership is tested only against the exact supplied `(scope_level, id)`; no expansion, no
   `org_type` mapping. Reuse the frozen primitive `evaluate_authorization` (`contracts.py:203-237`).
2. **Duplicating Backend authority.** Deciding org truth or resolving users→roles in tt-ai. Avoid:
   consume `AuthorizationContext` verbatim; the only decision tt-ai may render is a typed *membership
   test* against already-resolved Backend facts.
3. **Cross-level id collision.** Treating a raw id as globally unique. Avoid: every check carries the
   `scope_level`; unpaired or mismatched level denies (`contracts.py:227-233`, proven by
   `test_authorization_contracts.py:106-110`). Coverage checks must be keyed by level, not id alone.
4. **Coverage as a shadow permission set.** Using coverage metadata to *grant* access. Avoid: coverage
   only *narrows*; authorization remains Backend-owned and is checked independently.
5. **Fail-open via detail fallback.** A coverage/scope denial placed in `_SOURCE_REJECTIONS` could be
   degraded into a detail read. Avoid explicitly (§5).
6. **Silent coverage change.** Because coverage is absent from `_relation_structure_payload`
   (`schema_snapshot.py:1264-1274`), changing a relation's org column/coverage may not raise a drift
   issue. Avoid by adding coverage to the drift comparison or bumping the parser/snapshot version.
7. **Second deny vocabulary.** Inventing parallel denial codes. Avoid: reuse
   `SourceDegradation`/`metric_aggregate_coverage_unapproved`/`metric_column_unapproved` and the
   canonical `authorization_denied`; add at most one new literal, labelled PROPOSED, only if unavoidable.

---

## 9. Carried-forward documentation correction (from the product reviewer)

**Exact current wording** at `src/nl2sql/contracts.py:50-56` (the `RequestContext.authorization` comment):

> `# Optional carrier; absence must keep the pre-existing runtime-configurable`
> `# path unchanged.  A supplied, parseable context is ALWAYS evaluated: a`
> `# present-but-unusable one (stale revision, disabled, empty or out-of-scope)`
> `# denies regardless of any requirement flag.  Only when the carrier is`
> `# absent -- or malformed, which the accessor collapses to absent -- does the`
> `# requirement flag choose between a fail-closed deny (required) and the`
> `# existing path (not required).`

The phrase **"A supplied, parseable context is ALWAYS evaluated"** is too strong while the evaluation
seam has no active runtime call site: the actual evaluator `evaluate_config_authorization`
(`src/nl2sql/ownership.py:94-123`) is invoked only from tests (`tests/unit/test_authorization_wiring.py`),
and production never sets `RequestContext.authorization` (`src/nl2sql/v2.py:122-128`).

**Intended wording (carry forward to the next implementation slice that touches this contract):**
*when evaluated through the authorization enforcement seam, a supplied parseable context is always
evaluated.* The correction is documentation-only; no behavioural change is proposed here.

---

### Provenance

All claims above are from the working tree at HEAD `161189c`. The only write performed by this task is
this report. The compile-time re-verification and runner/engine wiring were **not** executed, per the
read-only constraint and the explicit gate that active engine enforcement is a later slice.
