> Read-only P2-S1 contract recon at HEAD 16cde3d (branch agent/v4-ci-hardening). This is the only file written by this task; no other file was created, modified, staged, or committed.

# P2-S1 DESIGN DIGEST — contract skeleton only (contracts + fail-closed fixtures; no invented backend capability)

Repo: E:/平台开发/ttai-pr07a-next @ branch agent/v4-ci-hardening (HEAD 16cde3d)
Nothing written except this file. No branch switch. Test suite not run. Read-only DB probe executed (see §6).
Sources: MASTER_PR_PLAN_V4.md §8.14 P2 card (L1907-1915) + §10.2 (L2252-2270); docs-v4-p2-backend-evidence.md; docs-v4-p4-shared-plan-executor-hitl-evidence.md; working tree.

---

## 1. INVENTORY — existing contracts / machinery P2-S1 must extend (file:line)

### 1a. Boundary contracts — src/nl2sql/contracts.py
- `SourceDegradation` L17-25 (closed enum incl. `metric_permission_denied`, `metric_relation_unapproved`).
- `RequestIdentity` L34-39: request_id/user_id/roles/permissions/auth_epoch. THIS is the only auth-bearing contract today.
- `RequestContext` L42-47: identity + deployment_scope + thread_id + trace_id + deadline_ms.
- `PolicyDecision` L50-55: outcome allow/deny/approval + max_rows/timeout_ms/data_scope/reason. Already the generic decision shape.
- `BoundFilter` L74-120: typed field_ref/operator/value/source(user|entity_alias|semantic_default). User filters enter here — relevant to "WHERE narrows, never authorizes".
- `ContextBundle` L123-160: semantic_release_id, schema_snapshot_id, domains, asset_ids, approved_relation_ids (L132), resolution_status, checksum.
- `QueryPlan` L163-208 (filters L173, required_permissions L179); `ExecutionPlan` L287-342 (semantic_release_id/schema_snapshot_id L293-294, policy_version L295).
- `PlanValidationRecord` L353-371 (outcome allow/deny/clarify/approval, policy_version+policy_checksum L358-359).
- `PlanStepReceipt` L374-405; `PlanExecutionRecord` L408-440.
- `RouteBudget`/`RoutingBudgetPolicy`/`RouteBudgetRecord` L469-536 (policy_version + policy_checksum + policy_state pattern to copy).
- `ExecutionReceipt` L547-568: datasource, readonly_role, masking_applied, masked_columns, policy_version, policy_outcome, rowset_sha256, source_kind/id, semantic_signature. NO authorization identity field.
- `ErrorEnvelope` L579-584 (code/retryable/stage/safe_message/trace_id) — the "no existence oracle" surface.
- `ModelRequest` L587-604 (data_classification L602, plan_reason L604); `ModelReceipt` L618-637 (profile_version/checksum, prompt_hash). No egress-policy field.

### 1b. Semantic release / schema snapshot
- src/nl2sql/semantic/registry.py: `SemanticReleaseState` L20-24; `SemanticRelease` L94-110 (release_id, checksum, state, schema_snapshot_id/checksum L107-108).
- src/nl2sql/semantic/schema_snapshot.py:
  - `RelationPolicy` L79-83: sensitivity, sensitive_columns, aggregate_coverage, freshness_sla_seconds. THE extension point for RelationCoverage.
  - `RelationSnapshot` L86-103 (mirrors policy fields, L100-103).
  - `SchemaRequirement` L106-109; `SchemaSnapshotCandidate` L162-184 (to_payload/relation_columns); `SchemaSnapshot` L188-198.
  - `load_relation_policies` L583-620; per-relation policy materialisation L521; `_candidate_payload` L1216; validation L636-762; `ControlSchemaSnapshotStore` L842+; parser version default referenced L168.

### 1c. RelationPolicy / RelationBinding / compiler
- src/nl2sql/orchestration/metric_query.py:
  - `EligibilityPolicy` L50-54; `OrganizationDimensionBinding` L57-71 (dimension Literal city_company|area|team — no employee).
  - `SourceFreshnessRecord` L112-136; `RelationBinding` L139-173 (relation_asset_id, allowed_columns, required_permissions, approved, organization_dimensions L155-157).
  - `MetricQueryCompiler` L195-219 (ctor takes identity: RequestIdentity L203, validates at L217); `compile` L221-265 (permission check L258-260); `_compile_source` L267-300 (permission + relation + coverage checks L273-294); `_select_source` ~L460-530.
  - `GatewayMetricStepRunner` L550, `metric_plan_executor` L633-635.

### 1d. QueryGateway authorization surface
- src/nl2sql/infra/governance/query_gateway.py:
  - `QueryErrorCode` L41-58 (PERMISSION_DENIED, RELATION_NOT_FOUND, POLICY_DENIED, SCHEMA_DENIED … existence-oracle risk).
  - `PreparedQuery` L68-79: sql/bind_sql/fingerprint/tables/max_rows/data_scope/policy_version.
  - `QueryReceipt` L82-131 (policy_decision L102-112, execution_receipt L114-131).
  - `PolicyEngine` L254-474: prepare L276-325 (data_scope set from allowed schema L324), validate_params L327-347, complexity L349-371, functions L373-394, `_enforce_schema` L413-445 (walks every scope/table incl. joins; falls back to schema only), `_apply_limit` L447-474.
  - `QueryGateway` L477-951: ctor L480-523 (schema/max_rows/datasource/readonly_role), prepare L525, execute L577-623, _execute_locked L625, _plan_and_run L685, _begin_read_only L801-817, _accepted_receipt L819-846 (data_scope L845), _rejected L909-938.
  - Existing secret-ish regex `_SENSITIVE_COLUMN` L190-192 + `_mask_row` used at L783 — only regex masking, not a policy.
- src/nl2sql/infra/store/database.py: single business exit `DatabaseManager.query` L178-182 → gateway.

### 1e. Model egress surface
- src/nl2sql/infra/llm/gateway.py: `ModelGateway` L304-478; `invoke` L336-390; `_enforce_policy` L464-468; `_enforce_target_policy` L470-478 (only checks request.data_classification ∈ target.allowed_data_classifications); fallback re-check L354; `get_legacy_model` L542-553 (product-refused).
- src/nl2sql/infra/llm/profiles.py: `ModelTarget.allowed_data_classifications` L36-38 default {public, internal}; fallback-cannot-broaden L96-100.

### 1f. Auth context / request binding
- src/core/auth/types.py: `AuthUser` L7-13 (user_id, telephone, roles, permissions — no org fields).
- src/core/auth/provider.py: `_map_response_to_user` L134-151 — profile company_id/department_id/team_id/token revision dropped.
- src/core/auth/dependencies.py: `_required_nl2sql_permission` L48-70 (route→permission, unmapped=fail closed); `require_user` L85-122; `require_permission` L125+.
- src/nl2sql/v2.py: `_request_identity` L108-119, `_request_context` L122-128 (the HTTP→contract bind point).
- src/nl2sql/orchestration/engine.py: `_request_identity()` L936-943 reads configurable["request_identity"]; used L165/233/286.
- src/nl2sql/ownership.py: `internal_thread_id` L10-14, `runtime_config` L17.
- src/nl2sql/semantic/context_compiler.py: `resolve` L168-178 + `retrieve_permitted` L175 (identity-scoped evidence).
- src/nl2sql/orchestration/planning.py: `PlanValidator` L64-88, `validate_query_plan` L90-190 (permission check L149-162), `validate_execution_plan` L192+.
- src/nl2sql/container.py: model gateway built L114-121; no authz provider, no gateway schema wiring here.

Note: repo-wide grep confirms NO existing `AuthorizationContext`, `ModelInputPolicy`, `RelationCoverage`, or `SensitiveField` symbol anywhere in src/ or tests/.

---

## 2. WHAT MUST BE ADDED (concrete names, fields, location, extension target)

Design rule applied: reuse the Backend model (vadmin_role_org_scope / vadmin_role_resource_permission.can_agent); P2-S1 is contracts + fixtures, so everything below is a contract with no live backend wiring claimed.

### A. src/nl2sql/contracts.py (boundary contracts)
1. `OrganizationScopeLevel = Literal["city_company","area","team","employee"]` — new alias near L13-25.
2. `EffectiveScope` (StrictContract, frozen): `scope_level: OrganizationScopeLevel`; `allowed_ids: tuple[str,...]` (authoritative compact effective scope; stable IDs only). Validator: city_company ⇒ allowed_ids == (); area/team/employee ⇒ non-empty; unique/non-blank.
3. `AuthorizationContext` (StrictContract, frozen) — references `RequestIdentity` by composition (contracts.py:34): `schema_version: Literal["1.0"]`; `identity: RequestIdentity`; `agent_enabled: bool` (projection of Backend can_agent); `effective_scope: EffectiveScope`; `authorization_revision: str` (non-empty; effective-snapshot identity); `authorization_source: Literal["backend"]`; `policy_fingerprint: str` (hex64); `checksum` property via `_contract_checksum` L647-655.
4. `AuthorizationDecision` (StrictContract): outcome Literal["allow","deny"], single canonical `reason_code` used for BOTH out-of-scope and unavailable-authorization, revision, policy_fingerprint. Implements the no-existence-oracle rule.
5. Extend `ExecutionReceipt` L547-568: add `authorization_revision: str | None`, `policy_fingerprint: str | None`. Extend `QueryReceipt` (query_gateway.py:82-131) and `PreparedQuery` (68-79) with `authorization_revision`. This is the P4 doc's "authorization revision in execution artifacts" (docs-v4-p4…:759).
6. Extend `ModelRequest` L587-604: `authorization_revision: str | None`. Extend `ModelReceipt` L618-637: `model_input_policy_version`, `model_input_policy_checksum`, `egress_outcome`.
7. Extend `SourceDegradation` L17-25 only with codes that do NOT distinguish out-of-scope from unavailable; prefer one code (`metric_scope_unauthorized`).

### B. src/nl2sql/semantic/schema_snapshot.py (RelationCoverage EXTENDS RelationPolicy)
1. `RelationCoverage` (frozen dataclass, slots): `coverage_root_org: str | None`; `coverage_org_level: OrganizationScopeLevel | None`; `row_org_field: str | None`; `minimum_query_org_level: OrganizationScopeLevel | None`; `detail_sensitivity: Literal["public","internal","restricted"]` (relation-level, NOT a PII matrix). Defaults None ⇒ fail-closed when coverage is required.
2. Add `coverage: RelationCoverage | None = None` to `RelationPolicy` L79-83 (so coverage is a policy field, not a sibling table) and mirror the fields into `RelationSnapshot` L86-103.
3. Update materialisation L521, `load_relation_policies` L583-620, `_candidate_payload` L1216 and checksum/parser-version handling (parser version referenced L168) so the snapshot checksum reflects coverage. Update validation L636-762.
4. `RelationCoverageDecision` (or reuse `PolicyDecision`): pure evaluator output consumed by fixtures.

### C. src/nl2sql/orchestration/metric_query.py (RelationBinding EXTENDS)
1. Extend `OrganizationDimensionBinding.dimension` L60 Literal to include "employee" (currently city_company|area|team only); update validator L64-71.
2. Add to `RelationBinding` L139-173: `coverage_required: bool = True` and a validator that any `row_org_field` used comes from the snapshot relation's coverage and is in allowed_columns. Keep `relation_id` property L167-169 as the join to RelationCoverage.
3. `MetricQueryCompiler` L195-219: accept `authorization: AuthorizationContext` (alongside/replacing identity L203) and store `authorization_revision`; raise fail-closed `metric_authorization_unavailable` when absent.

### D. src/nl2sql/infra/llm/gateway.py (unified ModelInputPolicy egress gate)
1. `SecretCategory = Literal["token","api_key","db_password","dsn","ssh_key","private_key","provider_credential"]` — the CHOSEN interpretation of plan "SensitiveField": a secret-only closed registry; no PII matrix, no DLP.
2. `ModelInputDecision` (StrictContract): outcome allow/deny, matched_categories, policy_version, policy_checksum, reason.
3. `ModelInputPolicy` (versioned + checksummed, mirrors `RoutingBudgetPolicy` L482-506): default ALLOW for ordinary authorized business data; DENY only secret categories. Insert `_enforce_model_input_policy` before the network call in `invoke` L336-390 and before the fallback target L354. `_enforce_target_policy` L470-478 remains the classification/stage gate.
4. Reuse/rename `_SENSITIVE_COLUMN` (query_gateway.py:190-192) as the shared secret-pattern source, or import it — note it currently masks result columns, it is not a policy.

### E. src/core/auth/provider.py + types.py (Backend adapter boundary)
1. New Protocol (skeleton only) `BackendAuthorizationProvider.load(auth_user) -> AuthorizationContext | None`; returning None = unavailable, treated identically to deny.
2. Extend `AuthUser` L7-13 (or a new `BackendAuthorizationSnapshot`) to stop dropping company_id/department_id/team_id/revision at `_map_response_to_user` L134-151. Do NOT invent the fields: contract only, mapping left behind a fail-closed flag.

---

## 3. ENFORCEMENT POINTS (where AuthorizationContext binds / where coverage is checked)

- E1 HTTP bind: src/nl2sql/v2.py:108-128 (`_request_identity`/`_request_context`) — build AuthorizationContext from AuthUser here; call provider; if None ⇒ AuthorizationDecision deny (no distinct error).
- E2 FastAPI dependency: src/core/auth/dependencies.py:85 (`require_user`) and :125 (`require_permission`); route policy :48-70 already fails closed for unmapped routes.
- E3 Runtime propagation: src/nl2sql/orchestration/engine.py:936-943 + ownership.py:17 — carry the context through `runtime_config`/configurable; re-read fail-closed at each node (identity read usage L165/233/286).
- E4 Context retrieval: src/nl2sql/semantic/context_compiler.py:168-178 (and `retrieve_permitted` L175) — scope-filter relation/domain evidence; ContextBundle.approved_relation_ids (contracts.py:132).
- E5 Plan validation: src/nl2sql/orchestration/planning.py:90-190, specifically permission rule L149-162 — add scope/coverage deny rules (deny > approval > clarify precedence L181-182).
- E6 Compiler: src/nl2sql/orchestration/metric_query.py:203/217 (identity→AuthorizationContext), 221-265 (`compile`), 267-300 (`_compile_source` relation+coverage checks at 278-294), `_select_source` ~460-530. Authz predicate is ANDed from deployment `organization_dimensions`, never from QueryPlan.filters.
- E7 Query boundary (WHERE narrowing / join coverage): src/nl2sql/infra/governance/query_gateway.py:525 (`QueryGateway.prepare`) → PolicyEngine.prepare 276-325 → `_enforce_schema` 413-445 (every table in every scope, incl. joins) → `_apply_limit` 447-474 → `_begin_read_only` 801-817. `PreparedQuery.data_scope` (78, set L324) is the existing carrier; a covered-relation allowlist would be a new optional ctor arg (contract only for S1).
- E8 Model egress: src/nl2sql/infra/llm/gateway.py:336-390 (`invoke`) / 464-478. Other egress exits from the evidence dossier §4.3 (embedding/RAG qa_rag.py, Langfuse observer, chat checkpointer, history HTTP, system prompt with telephone) are in-scope for the "unified egress gate" contract but P2-S1 only defines the decision contract + fixtures.
- E9 Receipt/no-oracle projection: query_gateway.py:114-131 (`execution_receipt`), 909-938 (`_rejected`), QueryErrorCode 41-58; contracts.py:547-568; ErrorEnvelope 579-584 + v2.py error mapping. Both "out of scope" and "authorization unavailable" must produce one identical code/safe_message.
- E10 Revocation: `authorization_revision` must be re-checked at E5/E6/E7 and on HITL resume (engine hitl_node; P4 doc notes `auth_epoch` exists contracts.py:39 but is unenforced).

---

## 4. TESTS TO CHANGE + NEW FAIL-CLOSED FIXTURES

### Existing tests that must change (adding coverage/context fields or swapping identity→AuthorizationContext)
- tests/metric_fixtures.py:82,101,112,182 (RelationPolicy/RelationBinding/RequestIdentity builders — central fixture).
- tests/unit/test_schema_snapshot.py:154-162,281,316,348,372-375 (RelationPolicy equality + checksum payload).
- tests/unit/test_metric_query.py, tests/unit/test_metric_ratio_query.py:117, tests/unit/test_metric_sources.py:164-303 (compiler identity/coverage reasons).
- tests/unit/test_plan_pipeline.py:63-64,195,214,237,774 (`_identity`, validator permission rule).
- tests/unit/test_query_gateway.py, test_query_gateway_adapters.py, test_query_gateway_capacity.py (PreparedQuery/QueryReceipt fields).
- tests/unit/test_model_gateway.py:37-38 (ModelRequest; add ModelInputPolicy).
- tests/unit/test_v2_contracts.py:74 and tests/unit/test_v2_authorization.py (request binding + route permission).
- tests/integration/test_query_gateway_postgres.py and tests/integration/test_postgres_governance.py:817-823 (real PG RelationPolicy).

### New fail-closed fixtures (map to MASTER_PR_PLAN_V4 §8.14 P2 必测, L1913)
1. scope_level × {city_company, area, team, employee} ancestor + sibling cases; siblings denied.
2. WHERE cannot create authorization: user filter on area_id/org id must never widen effective scope.
3. join-per-source: every table/relation in a joined query must be covered.
4. missing column classification: uncovered secret column ⇒ deny; unclassified ordinary business column ⇒ allow (per retired-PII rule).
5. forged client context: client-supplied AuthorizationContext/org ids ignored; server-derived only.
6. revocation: stale `authorization_revision` ⇒ deny.
7. provider fallback / unavailable authorization produces byte-identical observable to out-of-scope (no existence oracle).
8. memory / CodeAct / image / embedding egress with a secret payload ⇒ deny via ModelInputPolicy.
9. mode change does not increase privilege (mode is capability, not privilege).

---

## 5. OPEN QUESTIONS / GENUINE UNKNOWNS (need Backend owner or reviewer)

1. authorization_revision / effective snapshot — no revision/version/etag table exists anywhere (evidence §1, §10). What Backend endpoint produces the revision? Is it a hash of effective role→resource→org-scope rows, or a server timestamp? This cannot be settled from the repo.
2. org_type vocabulary mismatch — vadmin_role_org_scope.org_type is populated from old tt-api {company,department,team} (evidence §2.4), but the settled Agent scope_level is {city_company, area, team, employee}. And vadmin_area.level is a pure business dimension with no FK to role scope. Who maps area→scope, and is employee expressible at all?
3. Per-relation coverage metadata — vadmin_data_resource has table-level org_field/org_level only, and it had 0 rows; there is no coverage_root_org/coverage_org_level/minimum_query_org_level and no per-semantic-relation metadata (evidence §10; DB re-probe §6). Must RelationCoverage be authored as SchemaSnapshot RelationPolicy JSON only, or will Backend populate vadmin_data_resource? (Do not invent.)
4. Sibling semantics — existing data_scope.py computes ancestors + subtree only; no sibling query (evidence §2.5). What does "team sibling" mean operationally (same area)? Backend/reviewer must state, or the fixture should treat siblings as out-of-scope.
5. employee scope authority — gold_metric_result has employee_id (60417 non-null at area_team_employee), but organization_department is EMPTY, all 318 teams have NULL department_id and all 2160 employees have NULL department_id (fresh probe). The real tree is company→team→employee with area separate. Where does employee-level authority come from?
6. agent_enabled / can_agent — vadmin_role_resource_permission.can_agent is boolean default false and the table had 0 rows. Is agent_enabled derived from can_agent on the nl2sql/data resource? That table is role↔resource, so the user↔role resolution rule must be contracted.
7. AuthUser drops org fields — provider.py:134-151 discards company_id/department_id/team_id from the tt-api profile (evidence §1.1). Backend must either extend the profile or expose an authorization endpoint; P2-S1 should not fabricate the mapping.
8. Revocation transport — no server-side session/token-revocation store (evidence §1); auth is stateless per request, auth_epoch (contracts.py:39) is unenforced. How does revocation surface to the Agent, and with what TTL?
9. Read-only runtime identity — DB probe used postgres (superuser) as current_user/session_user; the only non-superuser login is agent_reader_user. Which identity does product row-level enforcement actually run as? Superuser audit is a provenance caveat.
10. Column-level claims — column_permissions JSONB exists but 0 rows and no mask types. Plan §8.14 P2 exit says "must not claim native column_scope exists" — P2-S1 must keep column_scope out of the contract or mark it explicitly unsupported.
11. Egress exits scope for P2-S1 — the unified gate must cover 6 exits (evidence §4.3: LLM, schema prompt, raw rows, telephone-in-system-prompt, RAG embedding, Langfuse, checkpointer, history HTTP). Which are P2-S1 contract-only vs wired? Reviewer decision; P4 doc assigns sink wiring to P2-S2 (docs-v4-p4…:578).
12. Unclassified business column policy — settled rule says ordinary authorized business data may egress by default. Confirm that "column classification missing" fails closed ONLY for secret-category columns, not for unclassified business columns (plan card L1913 lists 列分类缺失 as a required test — clarification needed so it does not silently re-introduce a PII/DLP matrix).

---

## 6. OPTIONAL DB INSPECTION (performed, read-only)

Tunnel 127.0.0.1:15432 was OPEN. DSN read internally from gitignored secrets/database/business_ro_database_url (values never printed/echoed/written). Session: BEGIN READ ONLY (server-asserted transaction_read_only=on), SET LOCAL statement_timeout='5000ms', SET LOCAL lock_timeout='1000ms', SELECT only, ended in ROLLBACK.

Freshly confirmed (matches the untracked dossier docs-v4-p2-org-db-evidence.json exactly):
- vadmin_data_resource = 0 rows; vadmin_role_org_scope = 0 rows; vadmin_role_resource_permission = 0 rows.
- vadmin_role_org_scope.org_type distinct values = []; can_agent=true count = 0; column_permissions non-null = 0.
- organization_company = 1; organization_department = 0; organization_team = 318; organization_employee = 2160.
- server 18.1; current_user = session_user = postgres (superuser — provenance caveat, not the product runtime identity).

So the three authorization tables are structurally present but EMPTY, and no per-relation coverage metadata exists. P2-S1 must be designed against an adapter contract, not against populated Backend data.
