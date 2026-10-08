# V4 P4 — Shared Plan / Executor / HITL Evidence

C2C `v4_p4_shared_plan_executor_hitl_evidence` · read-only · no code, DB or git writes.
Every claim below was read from the working tree at HEAD `32cbbb1` and carries a `path:line`
reference. Items that could not be verified in-tree are marked UNVERIFIED.

> **Provenance note (D1, later correction — see REVIEW CORRECTIONS §3).** `32cbbb1` is the HEAD the
> evidence below was gathered at and is retained as the historical evidence base. PR #29 has since
> merged; the working tree is now at `16cde3da9bfe3e5bddc02adc313235667f4641a8`. All sampled
> citations in this document were re-verified at `16cde3d`, and no finding below changed.

---

## RESULT

The V4 target — **one shared execution kernel** — is largely written but **not switched on**.
The legacy stack is already excluded from product mode by a single fail-closed chokepoint, so the
work ahead is: wire the existing kernel into the v2 container, then retire the legacy stack by
removing its last reachable callers. It is not a rewrite — and it is not yet a replacement.

Four findings drive the whole matrix:

0. **The typed kernel is built but not wired.** `PlanExecutor` is unreachable in production:
   `AppContainer` constructs the v2 engine without `context_resolver`, `query_plan_provider` or
   `plan_executor` (`container.py:119-123`), so `typed_pipeline_enabled` is false
   (`engine.py:105-118`) and `after_route` (`engine.py:826-831`) always selects `model`, never
   `compile`. The production graph plans, validates, routes and calls the model — and **executes
   no SQL at all**. This is the most consequential fact in this report. `metric_plan_executor`
   (`metric_query.py:633`), the QUERY adapter, is referenced only by tests
   (`tests/unit/test_metric_query.py:17`, `tests/integration/test_query_gateway_postgres.py:34`)
   and `docs-pr07a-slice1.md:13`; no concrete `QueryPlanProvider` exists in `src/` at all, only
   the Protocol (`planning.py:39-51`). `docs-pr07a-slice2.md:44` states it plainly: *"QueryGateway
   is the only executor ... no default AppContainer wiring"*.
1. The typed kernel itself is real, complete and high quality where it exists: `QueryPlan` is
   documented as an *untrusted proposal*, `ExecutionPlan` is a registered typed DAG, `PlanValidator`
   is fail-closed and replayable, and `PlanExecutor` never receives arbitrary callables.
2. The entire legacy agent stack cannot construct a model in product mode:
   `get_legacy_model()` raises `ModelPolicyDenied("legacy_model_disabled_in_product")`, and all
   36 model call sites in 14 files go through it. The gate is real — but it is **one boolean**
   (`service_mode`), and the default is `infra-dev`.
3. The real HITL is complete (interrupt + action version + idempotency + durable-capable
   checkpointer). A **second, non-checkpointed pseudo-HITL** exists in `codeact_engine`, whose own
   comment claims it uses the interrupt mechanism and does not.

---

## CURRENT_RUNTIME_TOPOLOGY

### Product mode (`SERVICE_MODE=product`)

Request path:

```
v2.py router  ->  _engine_from_request (v2.py:171)
              ->  product readiness gate (v2.py:178-188)   # 503 unless status == "ready"
              ->  container.get_engine (container.py:104)
              ->  create_v2_engine(checkpointer=...) (container.py:120)
```

Graph `nl2sql_v2_explicit` (`orchestration/engine.py:869`), a nine-node explicit graph
(`engine.py:839-869`):

```
START -> receive -> context -> plan -> validate -> route
route -> compile -> execute -> END        # fast path, zero model calls
route -> model -> hitl -> END             # the path production actually takes
```

**Only the second path is reachable today.** `AppContainer` builds the engine without
`context_resolver`, `query_plan_provider` or `plan_executor` (`container.py:119-123`), so
`typed_pipeline_enabled` is false (`engine.py:105-118`), `after_route` always returns `model`
(`engine.py:826-831`), and `execute_node` / `PlanExecutor` cannot be reached. The v2 production
graph therefore performs no SQL execution at all — which also means the QueryGateway row below is
the *designated* authority, not a currently exercised one on this path.

| Concern | Sole authority | Evidence |
| --- | --- | --- |
| Business SQL exit | `QueryGateway` | `orchestration/metric_query.py:581`, `infra/store/database.py:179` |
| Generic SQL tool | `QueryGateway` | `tools/async_sql_tools.py:46` |
| Model exit | `ModelGateway` | `orchestration/engine.py:678` |
| HITL | `engine.py` `hitl_node` | `engine.py:764-815`, `v2.py:311` |
| Checkpoint | `CheckpointerManager` | `infra/memory/checkpointer.py:39,42-60`, `container.py:120` |
| Readiness | `RuntimeContainer.readiness_report` | `container.py:59-102` |
| Config safety | `AgentConfig` validators | `config/settings.py:267-273` |

Product mode additionally refuses to serve when a required component is down (`v2.py:184-188`),
and refuses an in-memory checkpointer: `container.py:66-68` marks the checkpoint unavailable when
`service_mode == "product" and memory_backend != "postgresql"`, emitting
`product_checkpoint_backend_not_durable` (`container.py:96`).

### Infra-dev mode (the default)

`service_mode` defaults to `infra-dev` (`core/settings.py:96`, `config/settings.py:24`). In this
mode the legacy supervisor is also constructed: `create_supervisor` (`supervisor/agent.py:352`)
builds a LangChain `create_agent` (`supervisor/agent.py:372`) using `get_legacy_model()`
(`supervisor/agent.py:364`), with tools `query_database_with_semantic_sql` plus
`codeact_dynamic_calculation` when CodeAct is enabled (`supervisor/agent.py:368-370`).
`_invoke_codeact_agent` (`supervisor/agent.py:334`) selects the `dynamic_calc` graph when
`codeact_mode == "trusted-template"`, otherwise the `codeact_engine` graph (`supervisor/agent.py:339-344`).

Warmup mirrors that split: `infra/runtime/registry.py:77-90`.

**The important structural fact:** because `supervisor/agent.py:364` itself calls
`get_legacy_model()`, the supervisor/CodeAct stack cannot be constructed at all in product mode.
Its exclusion is not a routing convention — it is a construction failure.

---

## TYPED_KERNEL_FINDINGS

### Contracts (`src/nl2sql/contracts.py`)

| Contract | Line | Substance |
| --- | --- | --- |
| `QueryPlan` | 163-208 | Docstring: *"Untrusted declarative proposal; it cannot execute without validation."* Frozen, `extra="forbid"`, `schema_version="3.0"`. Fields: `intent`(L169) `domain`(170) `metric_keys`(171) `dimensions`(172) `filters`(173) `time_range`(174) `grain`(175) `source_strategy`(176) `result_limit`(178) `required_permissions`(179) `unresolved_slots`(180). `result_limit` restricted to `ranking` (L182-186). `checksum` (L206). |
| `ExecutionPlan` | 287-342 | *"Registered typed DAG compiled from one validated QueryPlan."* `query_plan_sha256`(292) `semantic_release_id`(293) `schema_snapshot_id`(294) `policy_version`(295) `steps`(296, 1..16). |
| DAG validation | 298-338 | Unique step ids (301); no self-dependency (307); unknown dependency rejected (309-311); input-ref root must be a declared dependency (320-327); cycle detection (329-337). |
| `PlanStep` union | 281-284 | `FetchMetricStep`(228) \| `TrustedCalculationStep`(244) \| `VerifyStep`(264), discriminated by `kind`. |
| `PlanValidationRecord` | 353-371 | `outcome` ∈ allow/deny/clarify/approval (360); `policy_version`+`policy_checksum` (358-359); an `allow` record may not carry issues and a non-allow record must (365-371). |
| `PlanStepReceipt` | 374-405 | *"Secret-free execution metadata; row values and SQL are never checkpointed."* `output_digest`(383) `rowset_sha256`(384) `data_as_of`(385) `freshness_status`(386) `source_kind`(387) `source_id`(388) `selection_reason`(389) `source_degradation`(390) `source_checkpoint`(391) `semantic_signature`(392) `error_code`(393) + status invariants (395-405). |
| `PlanExecutionRecord` | 408-440 | *"Checkpoint-safe summary emitted by the typed PlanExecutor."* `execution_plan_checksum`(413) `status`(414) `step_receipts`(415) `output_step_ids`(416) `stop_reason`(417); success cannot carry a stop reason (421); outputs must equal successful receipts (433). |
| `RouteBudget` / `RoutingBudgetPolicy` | 469-506 | `deadline_ms` `max_model_calls` `max_sql_candidates` `max_sql_executions` `max_join_hops` `max_repairs`; routes must be exactly fast/standard/deep (497-499). |
| `RouteBudgetRecord` | 518-536 | *"Secret-free checkpoint record for route call accounting and loop breakers."* `sql_fingerprint_counts`(527) `error_counts`(528) `stop_reason`(529) — this is what makes loop-breaking survive a resume. |

### Validator (`src/nl2sql/orchestration/planning.py`)

`PlanValidator` (L64) — *"Fail-closed semantic, permission, dependency, and budget validation."*
Constructed with `policy_version`, `approved_template_ids`, `approved_invariant_ids` (L67-78),
and derives `policy_checksum` as a SHA-256 over the sorted policy payload (L79-88), so a
validation decision is replayable against the policy that produced it.

Rules, each fail-closed:

| Rule | Line | Class |
| --- | --- | --- |
| `semantic_context_conflict` | 103-110 | deny |
| `semantic_context_incomplete` | 111-118 | clarify |
| `query_plan_unresolved_slots` | 120-128 | clarify |
| `query_plan_domain_not_permitted` | 130-137 | deny |
| `query_plan_metric_not_resolved` | 139-147 | deny |
| `query_plan_permission_denied` | 149-162 | deny |
| `query_plan_detail_source_unapproved` | 164-171 | deny |
| `query_plan_detail_strategy_mismatch` | 172-179 | deny |

`identity.permissions` is the only source of permission truth (L149-154); `"*"` is honoured.

### Executor (`src/nl2sql/orchestration/execution.py`)

Module docstring L3-5: *"The executor never receives arbitrary callables from a plan. Implementations
for metric SQL, trusted calculations, and verification are injected once by the application and
selected only by the step discriminator."* This is the single most important property for the
"one kernel" claim.

| Item | Line | Substance |
| --- | --- | --- |
| `PreparedMetricStep` | 50-65 | *"Ephemeral compiled query handle; payload and SQL are never checkpointed."* `sql_fingerprint` must be lowercase SHA-256 (59-63); `join_hops` ≥ 0. |
| `MetricStepRunner` Protocol | 74-90 | *"PR07A boundary: deterministic compile first, governed execution second."* `prepare()` then `execute()`. |
| `RegistryTrustedCalculationRunner` | 111-130 | *"Adapter for source-controlled templates; independent from CodeAct mode."* — BUILD already is a registered-capability path, not arbitrary Python. |
| `TypedResultVerifier` | 133-153 | Only `typed_result_present` is supported today; domain invariants are explicitly deferred. |
| `PlanExecutionResult` | 156-161 | *"Outputs are request-local only; only `record` belongs in a checkpoint."* |
| `PlanExecutor.execute` | 176-269+ | Deep re-validation snapshots via `model_validate_json(model_dump_json())` (186-190); `_execution_plan_mismatch` before any work (191-194); deadline reserve (195-211); `asyncio.timeout` (219); per-step receipts and `gateway_receipts`. |

`orchestration/metric_query.py` is the concrete binding: `__init__(compiler, gateway: QueryGateway)`
(L551), `metric_plan_executor(compiler, gateway)` (L633), and the single execution statement
`await self._gateway.execute(current.sql, current.params)` (L581).

### Validator rules for the compiled plan (`planning.py:192-291`)

Beyond the query-plan rules above, `validate_execution_plan` re-verifies the entire binding:

| Rule | Line | Class |
| --- | --- | --- |
| `execution_plan_query_hash_mismatch` | 208-215 | deny |
| `execution_plan_semantic_release_mismatch` | 216-223 | deny |
| `execution_plan_schema_snapshot_mismatch` | 224-231 | deny |
| `execution_plan_sql_candidate_budget_exceeded` | 233-243 | deny |
| `execution_plan_sql_execution_budget_exceeded` | 244-251 | deny |
| `execution_plan_join_budget_exceeded` | 252-259 | deny |
| `trusted_calculation_approval_required` | 261-270 | approval |
| `execution_plan_invariant_unregistered` | 271-280 | deny |

Precedence is `deny > approval` and `deny > clarify` (181-182, 282-283). `PlanCompiler`
(`planning.py:294-335`) independently re-checks `outcome == "allow"` plus the query hash and
context checksum (`:312-317`) instead of trusting its caller, and binds the plan checksum into
`ExecutionPlan.query_plan_sha256` (`:330`) and the policy version (`:333`).

### The query-gateway receipt — the object P2 must extend

`ExecutionReceipt` (`contracts.py:547-568`) is the gateway's own projection: `datasource`,
`readonly_role`, `elapsed_ms`, `row_count`, `plan_cost`, `estimated_rows`, `masking_applied`,
`masked_columns`, `error_taxonomy`, `sql_fingerprint`, `policy_version`, `policy_outcome`,
`rowset_sha256`, `data_as_of`, `freshness_status`, `source_kind`, `source_id`,
`selection_reason`, `source_degradation`, `source_checkpoint`, `semantic_signature`. It is derived
from `QueryReceipt` via the `execution_receipt` property (`query_gateway.py:114-131`). P2's
`AuthorizationContext` and `RelationCoverage` should extend this, not replace it.

### The kernel is built but not wired — the actual P4 starting point

This is the finding that reframes the task.

`AppContainer` builds the v2 engine **without the three collaborators that activate the typed
pipeline** (`container.py:119-123`). `typed_pipeline_enabled` is therefore false
(`engine.py:105-118`) and `after_route` (`engine.py:826-831`) always chooses `model`.
`execute_node` (`engine.py:499-596`) — and with it `PlanExecutor` — is unreachable in production.

Corroboration:

* `metric_plan_executor` (`metric_query.py:633-635`), the adapter that would bind a compiler to a
  gateway, is referenced only by `tests/unit/test_metric_query.py:17`,
  `tests/integration/test_query_gateway_postgres.py:34` and `docs-pr07a-slice1.md:13`.
* No concrete `QueryPlanProvider` exists in `src/` — only the Protocol (`planning.py:39-51`);
  `tests/unit/test_plan_pipeline.py:225` supplies its own `_StaticQueryPlanProvider`. Likewise no
  concrete `PolicyScopedEvidenceProvider` exists in `src/` — only the Protocol
  (`context_compiler.py:65-71`). The resolver itself is **not** missing: `SemanticContextResolver`
  already exists (`context_compiler.py:156`, alongside `ContextCompiler` at `:87`) and is covered
  by `tests/unit/test_plan_pipeline.py:838`.
* `docs-pr07a-slice2.md:44` states the position plainly: *"QueryGateway is the only executor ...
  no default AppContainer wiring"*.
* `PlanCompiler` currently emits only `fetch_metrics` and `verify_result` (`planning.py:319-335`).
  `PlanExecutor` can also consume a `TrustedCalculationStep`, but the compiler never produces one.
  The `PlanStep` union is exactly `FetchMetricStep | TrustedCalculationStep | VerifyStep`
  (`contracts.py:281-284`); **there is no `detail`/exploration step kind at all**, and the contract
  table in TYPED_KERNEL_FINDINGS already lists these same three kinds.

Production today is therefore: plan → validate → route → model → HITL, with **no SQL execution at
all**. The typed kernel is a complete, tested, dormant subsystem. Any P4 slice that assumes the
kernel is live is starting from the wrong premise.

### Budget and deadline enforcement

`RouteBudgetLedger` (`budget.py:109-261`) with bootstrap limits (`:17-53`): fast 0 model calls /
1 SQL / 4000 ms; standard 3 / 2 / 10000 ms; deep 5 / 2 / 30000 ms; `reserve_ms` 800.
`BudgetExceeded` (`budget.py:105`) is raised at `:93` (`attempt_budget_exhausted`), `:99`
(`model_call_budget_exceeded`), `:187` (`join_hop_budget_exhausted`), `:253` (prior stop reason)
and `:260` (per-counter). The typed executor stops on `execution_deadline_unavailable` /
`deadline_reserve` (`execution.py:195-211`) and `execution_deadline_exceeded` (`:293-308`), wrapped
by `asyncio.timeout` (`:219`). `QueryGateway` uses its own database-level caps instead
(`query_gateway.py:807-814` timeouts, `:749` cost, `:756/778` rows, `:792` result bytes), so the
two budget systems are independent.

---

## CODEACT_FINDINGS

`src/nl2sql/agents/codeact_engine/` is a complete second execution authority.

| File | Role | Evidence |
| --- | --- | --- |
| `plan_card.py` | `CalcPlanCard`(58) `ConfirmedCalcPlan`(134) | LLM-produced plan card, then a locked variant |
| `plan_ingestion.py` | `IngestedPlan`(32) | converts a confirmed card into fetch steps |
| `hitl_protocol.py` | `decompose_to_plan_card`(34) `refine_plan_card`(82) `lock_plan`(130) `render_plan_for_chat`(216) | LLM plan-card lifecycle; uses `get_legacy_model().with_structured_output(...)` (47, 106) |
| `parallel_fetcher.py` | `parallel_fetch`(77) `_execute_fetch_step`(123) `_generate_fetch_sql`(185) `_extract_sql`(205) | LLM authors the SQL, then executes it |
| `code_generator.py` | `generate_code` / `repair_code` | LLM authors Python; `get_legacy_model()` (59, 94) |
| `process_sandbox.py` | `ProcessSandbox` | subprocess isolation via `multiprocessing` + `resource.setrlimit` (54-63) |
| `validator.py` | imported at `graph.py:45` | a second validator |
| `graph.py` | `build_codeact_graph`(452) `get_default_codeact_graph`(526) | the graph itself |

**Footprint:** `codeact_engine` is 10 files / 2,116 LOC; `dynamic_calc` is 7 files / 943 LOC.

**Deployment already pins it off**, independently of the code gates: `docker/compose.prod.yml:9-10`,
`docker/compose.release.yml:14-15,52-53`, `.env.example:70-72`, and
`deploy/release-manifest.example.yaml:15` (`dynamic_calc: disabled`). So three independent layers
keep this stack out of production — deployment config, the `unsafe-dev` config validator, and the
`get_legacy_model` product guard. None of them is structural, which is exactly why the matrix
retires the code rather than relying on the layers.

### The SQL authoring bypass (the real one)

Transport is already unified — `graph.py:463-467` builds tools from the shared db manager
(`get_nl2sql_db_manager` → `create_async_sql_tools`), and `tools/async_sql_tools.py:46` states it
executes *"通过 QueryGateway 执行一个只读 SELECT"*. What is **not** unified is who writes the SQL:

```
parallel_fetcher.py:200   llm = get_legacy_model()
parallel_fetcher.py:201   response = await llm.ainvoke([SystemMessage(content=prompt)])
parallel_fetcher.py:202   return _extract_sql(str(response.content))    # string extraction
parallel_fetcher.py:146   raw_result = str(await query_tool.ainvoke({"query": sql}))
```

So an LLM-authored SQL string reaches the gateway and is admitted or rejected by runtime policy
only. There is no plan, no `ExecutionPlan.checksum`, no validated DAG — the authoring authority is
a prompt (`FETCH_SQL_PROMPT`, L25-41) plus a code-fence string parser (`_extract_sql`, L205-220).
The same shape appears in `dynamic_calc/planner.py:16` and `hitl_protocol.py:47,106`.

### Reachability and gating

* Reachable from the supervisor: `supervisor/agent.py:343` → `get_or_create_codeact_graph()`
  (`infra/runtime/registry.py:63-74`).
* Compiled **without a checkpointer**: `graph.py:521` `builder.compile(name="codeact_engine")` —
  no `checkpointer=` argument, unlike `engine.py:869`.
* `user_action` is declared (`graph.py:72`) and read (`graph.py:339`) but **never written anywhere
  in `src/`**, so the confirm / modify / restart branches are unreachable through a normal invoke.
  The second flow is therefore not merely non-durable; it is not demonstrably functional.
* The supervisor that reaches it is itself **CLI-only**: `create_supervisor` is referenced only by
  `cli.py:69,76`, and the HTTP application registers only the v2 routes (`main.py:35-36`).
  Reachability here is static (imports and greps), not runtime-verified.
* `agents/dynamic_calc` contains no HITL, confirmation or interrupt logic at all.
* Gated by config: `config/settings.py:275-284` `codeact_capability()` requires
  `enable_dynamic_calc` and a non-`disabled` `codeact_mode`.
* `unsafe-dev` is refused in product mode at config load (`config/settings.py:269-270`).
* `parallel_fetcher` executes **only** when the graph runs, which requires a legacy model, which
  product mode refuses. So in product mode this path is unreachable by construction.

### Sandbox

`process_sandbox.py` is a genuine upgrade over in-process `exec()`: subprocess via
`multiprocessing`, `RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_NPROC` (L54-63), a forbidden-pattern list
(L24-44) and a forbidden-AST-node tuple (L46-51). Two caveats must be recorded:

* L7 claims *"该模块仅在 Linux 上完整生效；非 Linux 系统降级为线程隔离 + 超时"*. **That claim is
  false.** The implementation always uses `multiprocessing.Process` (`process_sandbox.py:246-258`),
  so there is no thread fallback. What actually degrades off Linux is `resource.setrlimit`
  (`:54-63`), whose `except (ImportError, ValueError, OSError)` branch silently does nothing
  (`:62-63`) — the process boundary survives, but every CPU / memory / process limit is lost,
  with no signal that it happened.
* The boundary remains a regex/AST denylist. The same reasoning the plan applies to
  *"prompt text 'only generate SELECT' is not an execution security boundary"* applies to a
  denylist: it is a mitigation, not a boundary.

### A second, in-process code execution path

`dynamic_calc/code_executor.py:49` `SandboxExecutor` is a *third* execution mechanism, distinct
from both the typed kernel and `ProcessSandbox`: it runs LLM-authored Python **in the current
process** via `exec(code, exec_globals)` (`:171`) behind restricted builtins (`:149-161`) and a
restricted `__import__` (`:179-184`), wrapped in `asyncio.wait_for(asyncio.to_thread(...))`
(`:85-88`). Two consequences: the timeout cannot kill a stuck thread, and the only boundary is the
same regex/AST denylist (`:19-46`, `:112-138`). It refuses to run unless
`codeact_mode == "unsafe-dev"` (`:69-74`).

### The confirmation flow is not merely unwired — it is auto-confirmed

`hitl_router` (`graph.py:337-344`) is defined but is **never passed to `add_conditional_edges`**;
the only reference in `src/` is its own definition. The edge list goes `decompose -> lock`
unconditionally (`graph.py:493`). The plan is therefore **locked without any user confirmation**,
and `user_action` (declared `:72`, read `:339`) is never written.

The supporting UX is dead for the same reason: `render_plan_for_chat` (`hitl_protocol.py:216`) has
no caller in `src/`, and the front-end blocks meant to carry it (`supervisor/schemas.py:43-72` —
`CodeResultBlock`, `HITLPlanCardBlock`, `HITLConfirmationBlock`) are never constructed. The
front-end contract exists with no producer.

### The two HITL systems are not merely duplicated — they are mutually incompatible

The codeact graph uses `hitl_status` values `awaiting_confirmation` / `pending`
(`graph.py:71, 101, 117`), whereas the served v2 action endpoint requires `awaiting_action`
(`v2.py:306`) and resumes the orchestration engine (`v2.py:311`). A codeact thread could not be
driven through the v2 HITL API even if it were reachable. Separately,
`dynamic_metric_calculation` (`supervisor/agent.py:195-218`) is defined but never appended to
`active_tools` (`:368, 370`), so that path is dead too.

---

## HITL_FINDINGS

### The real path — complete and checkpointed

`engine.py` `hitl_node` (L764-815):

| Property | Line | Evidence |
| --- | --- | --- |
| Real LangGraph interrupt | 766-773 | `interrupt({"kind": "nl2sql_hitl_action", "version": ..., "actions": ["approve","modify","reject","cancel"], "summary": ...})` |
| Invalid payload fails closed | 774-775 | `invalid_action_payload` |
| Unsupported action fails closed | 779-780 | `unsupported_action` |
| Idempotency key required | 781-782 | `missing_idempotency_key` |
| Action version enforced | 783-784 | `stale_action_version` |
| Replay is idempotent | 786-793, 808 | `applied_actions` keyed by idempotency key; a replay returns the stored status without re-executing |
| Reject/cancel execute nothing | 807 | *"Request {action_status}. No business SQL was executed."* |
| Version advances | 812 | `hitl_version: version + 1` |

HITL is requested only for the deep path (`engine.py:748-759`), and the comment at L749-750 is
explicit that *"business SQL remains blocked until the owner makes an explicit, versioned decision."*

Resume is the documented route: `v2.py:311` `engine.ainvoke(Command(resume=body.model_dump()), config)`,
served at `POST /api/v2/nl2sql/threads/{thread_id}/actions` (`v2.py:279-320`, registered `main.py:35`).
Compile binds the checkpointer: `engine.py:869` `graph.compile(checkpointer=checkpointer, name="nl2sql_v2_explicit")`;
wiring at `container.py:119-123` (also passing `trace_sink=self._audit_store`).

**The action version is checked twice.** The API layer rejects a stale client before the graph is
resumed — `v2.py:308-310` returns HTTP 409 *"action version is stale"* — and the node re-checks
independently (`engine.py:783-784`). Defence in depth rather than a single gate.

**Idempotency keys are client-supplied, not server-generated.** The contract requires one
(`v2.py:74`, length 1..256) and the API has a fast path that returns the prior status without
resuming (`v2.py:293-305`, `idempotent=true`). No generator exists in `src/`, so correctness
depends on every caller supplying a stable key.

**No session or revocation store exists.** HITL authority state (`hitl_status`, `hitl_version`,
`applied_actions`) lives only in the checkpointer. Thread identity is bound to the caller
(`ownership.py:10-14`, `internal_thread_id = deployment_scope:user_id:thread_id`), and
`RequestIdentity.auth_epoch` exists (`contracts.py:39`) but no enforcement of it was found —
UNVERIFIED. Auth is otherwise stateless per request (`core/auth/dependencies.py:85-110`).

### The second, pseudo-HITL

`codeact_engine` implements a parallel confirmation flow as **plain state**, not as a graph
interrupt: `hitl_status` ∈ pending/awaiting_confirmation/confirmed/rejected (`graph.py:71`),
a `hitl_fallback_node` (`graph.py:301`), and routers `hitl_router` (`graph.py:337`),
`after_ingest_router` (347), `after_fetch_router` (356), `after_validate_router` (379).

Verified by repository-wide search: the **only** `interrupt(` call in `src/` is `engine.py:766`.
`codeact_engine` contains none.

This matters because of the comment at `graph.py:492`:
*"这里构建完整的图结构，运行时通过 interrupt 机制实现等待"* — the graph does **not** use the
interrupt mechanism. A reader trusting that comment would believe the second flow is durable and
resumable when it is neither. This is a documentation defect on top of an architecture defect.

The UX the second flow provides is nevertheless real and worth keeping: plan card, assumptions,
ambiguity warnings, modification workflow, confirmation rendering (`hitl_protocol.py:216`
`render_plan_for_chat`).

---

## SQL_BYPASSES

**Business SQL is unified. There is exactly one business execution statement in the product path:**

```
orchestration/metric_query.py:581   await self._gateway.execute(current.sql, current.params)
```

`infra/store/database.py:179` states the rule — *"Run application SQL exclusively through
`QueryGateway`"* — and enforces it: `run_application_query` (L180-182) raises when the gateway is
absent, and `preflight` (L190-192) exists for EXPLAIN.

### Non-business database access outside the gateway

These touch the database without `QueryGateway`. None executes business SQL, but none is under the
gateway policy either, so each needs an explicit owner and an explicit exemption decision:

| # | Site | Lines | Kind |
| --- | --- | --- | --- |
| A | `infra/store/ai_views.py` | 388, 587-634 | DDL / `GRANT` / `SELECT CURRENT_USER`; `sync_ai_views_from_yaml` (570) has **no production caller** |
| B | `semantic/registry.py` | 299-927 | control-DB release documents (engine 239-244) |
| C | `semantic/schema_snapshot.py` | 356-394; 881-980; 1100-1135 | business catalog reads; control writes; one-shot snapshotter |
| D | `infra/store/database.py` | 97-108 | `metadata.reflect` schema discovery |
| E | `observability/control_audit.py` | 44, 61, 83, 95 | own `asyncpg` pool; audit/outbox writes |
| F | `infra/memory/checkpointer.py` | 49-59 | psycopg → `AsyncPostgresSaver` |
| G | `infra/memory/checkpoint_migrate.py` | 19-30 | psycopg → `PostgresSaver.setup` |
| H | `infra/governance/query_gateway.py` | 742, 774, 805-817 | `EXPLAIN`, `session.stream` SELECT, `SET` — the gateway itself |

Exhaustiveness basis: a full-`src` search for `.execute(` yields 57 matches, all classified above;
`.stream(` occurs only at `query_gateway.py:774`; the only drivers present are SQLAlchemy, asyncpg
and psycopg; there is no raw cursor or `fetchall` outside the gateway. `DatabasePurpose`
(`core/database.py:14-20`) names the separation deliberately, and
`tests/unit/test_database_boundaries.py:19-37` asserts it.

**Conclusion: there is no business-SQL bypass.** Every business SELECT in the application wrapper
path routes through `QueryGateway`. The sites above are control-plane, checkpoint, catalog or DDL
by design — which is exactly why each needs a recorded exemption rather than silent tolerance.

### Second SQL authoring authority

As detailed under CODEACT_FINDINGS: `parallel_fetcher.py:200-202` + `:146`. The transport is
governed; the authorship is not.

---

## MODEL_BYPASSES

### One chokepoint, already fail-closed

```
infra/llm/gateway.py:542   def get_legacy_model(...)
infra/llm/gateway.py:548       """Compatibility bridge for retired non-product graphs only."""
infra/llm/gateway.py:549       if get_settings().service_mode == "product":
infra/llm/gateway.py:550           raise ModelPolicyDenied("legacy_model_disabled_in_product")
infra/llm/gateway.py:551-553   ... build_legacy_provider_model(...)
```

Every legacy model call in the repository goes through this function: **36 call sites across 14 files**.

| Area | Call sites |
| --- | --- |
| `supervisor/agent.py` | 364 (plus `create_agent` 372) |
| `agents/codeact_engine/` | `code_generator.py` 59, 94; `graph.py` 291; `hitl_protocol.py` 47, 106; `parallel_fetcher.py` 200 |
| `agents/dynamic_calc/` | `planner.py` 16; `graph.py` 89, 183, 287, 312 |
| `agents/sql_agent/` | `agentic_rag.py` 274, 342, 511, 605; `parallel_generator.py` 191; `sql_generator.py` 227, 297, 310 |
| `agents/nl2sql/graph.py` | 43 |
| `agents/gen_data/agent.py` | 41 (plus `create_agent` 46) |
| `infra/context/compressor.py` | 189 |

Because product mode raises inside this function, **the whole legacy stack is structurally
disabled in product mode**. That is a genuine, already-implemented egress gate.

### The residual risk

The gate is a single boolean read from `service_mode`, whose default is `infra-dev`
(`core/settings.py:96`, `config/settings.py:24`). The safety property is therefore
*"one environment variable away"*, and the failing direction is the permissive one. The final
state must delete the function and its callers so that the property is structural rather than
conditional.

`infra/llm/factory.py:36` `build_legacy_provider_model` is the raw constructor reachable only
through the guarded function today; it should not outlive it.

### Telemetry — a third egress path

Langfuse is wired as a LangChain callback, not through `ModelGateway`:
`core/observer.py:47-59`, `agents/nl2sql/nodes.py:94-96`, implementation at
`infra/observer/langfuse.py`. It is off by default (`core/settings.py:72` `langfuse_enabled=False`)
and configured by `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (`core/settings.py:73-75, 131-132`).
Whether the shipped payload contains prompt or result content was not traced in this pass —
marked UNVERIFIED. Under the updated P2 decision, telemetry is governed by the same sink concept
as any other provider, and must never receive system secrets.

`observability/control_audit.py:3` records the deliberate opposite choice for the audit writer:
*"The writer is deliberately independent of Langfuse and OTel."*

---

## CHECKPOINT_FINDINGS

### What may be checkpointed

The contracts are explicit about what is allowed to persist:

* `PlanStepReceipt` (contracts.py:374) — *"Secret-free execution metadata; row values and SQL are
  never checkpointed."*
* `PlanExecutionRecord` (contracts.py:408) — *"Checkpoint-safe summary."*
* `RouteBudgetRecord` (contracts.py:518) — *"Secret-free checkpoint record."*
* `PlanExecutionResult` (execution.py:156-161) — *"Outputs are request-local only; only `record`
  belongs in a checkpoint."*
* `PreparedMetricStep` (execution.py:50-52) — *"payload and SQL are never checkpointed"*, exposed
  as `repr=False, compare=False` (L56).

* `QueryCandidate.sql` is excluded from serialization outright — `contracts.py:540`
  `Field(repr=False, exclude=True)`.

### Correction: the checkpoint as a whole is NOT secret-free

That guarantee holds for the typed sub-records only. `V2EngineState` (`engine.py:54-73`) also
persists raw content:

| Field | Evidence | Content |
| --- | --- | --- |
| `messages` | input `v2.py:221-223`; answers `engine.py:761` | the raw user question and model answers |
| `pending_answer` | `engine.py:753` | the generated answer |
| `model_receipt` | `engine.py:744` stores `receipt.model_dump(...)` | includes `ModelReceipt.content` = **full model completion text** (`contracts.py:636`) |
| `query_plan` | `engine.py:274` | its `filters` carry `BoundFilter.value` (`contracts.py:96`), which can hold user literal values |

Row values and SQL text remain structurally excluded. Prompt and answer content do not.
Any future statement of this guarantee must be scoped to the typed receipts, never to "the
checkpoint" as a whole.

### Backend durability

`infra/memory/checkpointer.py`: default `MemorySaver` (L39); `AsyncPostgresSaver` via
`from_conn_string` plus `setup()` when the backend is postgres (L42-60). Product mode refuses the
in-memory backend (`container.py:66-68`) and reports `product_checkpoint_backend_not_durable` (L96).

`agents/nl2sql/graph.py:3` documents that the sub-agent is intentionally stateless:
*"无状态子 Agent：不注入 checkpointer"*.

### Gap

`tests/unit/test_hitl_actions.py` exercises the real interrupt/resume path but with
`MemorySaver()` (`test_hitl_actions.py:10, 50`). No test drives resume **across a process
restart on the durable Postgres backend**, which is precisely the configuration product mode
requires. The durability claim is enforced by configuration, not by a test.

---

## MIGRATION_MATRIX

### KEEP — already correct, do not touch

* `contracts.py` typed plan / validation / receipt / ledger contracts (L163-536).
* `PlanValidator` with `policy_checksum` replayability (`planning.py:64-88`).
* `PlanExecutor` two-phase boundary and the injected-runner design (`execution.py:3-5, 164-184`).
* `QueryGateway` as the single business SQL exit (`metric_query.py:581`, `database.py:179`).
* `ModelGateway` plus the product-mode denial in `get_legacy_model` (`gateway.py:542-553`).
* The real HITL: interrupt, action version, idempotency (`engine.py:764-815`; `v2.py:311`).
* `TrustedTemplateRegistry` — source-controlled, Pydantic-contracted (`dynamic_calc/trusted_templates.py:72-85`).
* Readiness gating and product checkpoint durability rule (`container.py:59-102`; `v2.py:178-188`).
* Config-level safety validators (`config/settings.py:267-273`).

### EXTEND — wire the kernel, then widen its authority

* **Wire the typed pipeline** — the first and largest item. All three collaborators are mandatory
  **together**: `typed_pipeline_enabled = all(c is not None for c in (context_resolver,
  query_plan_provider, plan_executor))` (`engine.py:105-108`), and a partial wiring raises
  `ValueError("typed plan pipeline requires context resolver, query plan provider, and executor")`
  when the pipeline is requested (`engine.py:109-112`). There is no partial-wiring escape hatch.
  The provider must also be deterministic and zero-model: `engine.py:113-118` raises
  `ValueError("pre-route query plan provider must be deterministic and zero-model")` when
  `is_deterministic` is false. Pass all three into `create_v2_engine` (`container.py:119-123`) so
  `after_route` can select `compile`. Until this lands, everything else in this matrix is
  preparation rather than operation.
* **Build the genuinely missing collaborators.** The wiring gap is not the resolver:
  `SemanticContextResolver` already exists (`context_compiler.py:156`) alongside `ContextCompiler`
  (`context_compiler.py:87`) and is covered by `tests/unit/test_plan_pipeline.py:838`. What is
  absent from `src/` is (i) a concrete `PolicyScopedEvidenceProvider` — the Protocol is defined at
  `context_compiler.py:65-71` with zero implementations in `src/` — and (ii) a concrete
  `QueryPlanProvider` (only the Protocol at `planning.py:39-51`). State the gap as **missing
  `PolicyScopedEvidenceProvider` + concrete `QueryPlanProvider`; NOT the resolver.**
* **Extend the compiler.** `PlanCompiler` emits only `fetch_metrics` + `verify_result`
  (`planning.py:319-335`); `trusted_calculation` must become producible or BUILD cannot exist on
  the kernel at all.
* **QueryGateway surface.** The non-business DB sites are already separated by `DatabasePurpose`
  (`core/database.py:14-20`) and locked by `tests/unit/test_database_boundaries.py:19-37`. Record
  each as an explicit named exemption so the boundary is documented rather than inferred.
* **ModelGateway sinks.** Add the explicit sink/provider concept required by P2-S2, and register
  Langfuse as a governed sink rather than an unchecked LangChain callback.
* **PlanExecutor invariants.** Only `typed_result_present` exists (`execution.py:133-153`); domain
  invariants are the intended extension point.
* **Readiness.** Add a component asserting that the typed kernel is the only reachable execution
  authority, so a regression becomes a startup failure rather than a silent second path.

### ADAPT — keep the idea, move it onto the typed kernel

| Legacy idea | Evidence | Target on the typed kernel |
| --- | --- | --- |
| Plan card | `hitl_protocol.py:216` `render_plan_for_chat` | render from `QueryPlan` + `ExecutionPlan` + `PlanValidationRecord` |
| Ambiguity warnings | `plan_card.py`, `hitl_protocol.py:176 _derive_validation_criteria` | `PlanValidationIssue` with `outcome="clarify"` (`planning.py:111-128`) already encodes this |
| Modification workflow | `hitl_protocol.py:82 refine_plan_card` | the `modify` branch of `hitl_node` (`engine.py:803-805`) plus a re-plan |
| Confirmation UX | `graph.py:101-134` | the `interrupt` payload already carries `actions` and `summary` (`engine.py:766-772`) |
| Trusted dynamic calculation | `dynamic_calc/trusted_templates.py` | **already reused** by the kernel (`execution.py:19`, `RegistryTrustedCalculationRunner:111`) |

### RETIRE — remove the second authority

* `agents/codeact_engine/` **as an execution authority and as a HITL implementation**: `graph.py`,
  `hitl_protocol.py`, `parallel_fetcher.py`, `code_generator.py`, `plan_card.py`,
  `plan_ingestion.py`, `validator.py`, `process_sandbox.py`. The UX ideas survive via ADAPT; the
  authority does not.
* **LLM-authored SQL** — `parallel_fetcher.py:185-202`. Must not survive into product under any flag.
* **LLM-authored Python in product** — `code_generator.py`, `process_sandbox.py` (2,116 LOC for
  the package), **and** `dynamic_calc/code_executor.py` `SandboxExecutor`, which is in-process
  `exec()` and never should have existed next to a subprocess sandbox. The boundary in both is a
  regex/AST denylist, and the `ProcessSandbox` docstring's non-Linux fallback (`:7`) does not
  exist in the code.
* `agents/dynamic_calc/` **graph and planner** (`graph.py`, `planner.py`) as execution paths; keep
  `trusted_templates.py` only.
* `supervisor/agent.py` `create_agent` supervisor as a second orchestrator (`supervisor/agent.py:352-402`).
* `agents/sql_agent/` and `agents/gen_data/` as product authorities.
* `infra/context/compressor.py:189` legacy model use.
* `get_legacy_model` **itself** (`infra/llm/gateway.py:542-553`) once no caller remains, together
  with `build_legacy_provider_model` (`infra/llm/factory.py:36`).
* `infra/governance/sql_guard.py` compatibility helpers (L1-6 declare them superseded).

---

## RECOMMENDED_TARGET

**One execution kernel.** `QueryPlan` (untrusted proposal) → `PlanValidator` (fail-closed,
replayable) → `ExecutionPlan` (registered typed DAG, checksummed) → `PlanExecutor` (injected
runners only). QUERY / ANALYZE / BUILD are capability envelopes over these same types;
BUILD is `TrustedCalculationStep`, i.e. registered templates, never arbitrary Python.

**One HITL.** `engine.py` `hitl_node`. The codeact confirmation state machine is deleted and its UX
is re-expressed through the interrupt payload and `PlanValidationIssue`.

**One sandbox.** None in product. Any future capability is a registered executor capability behind
the same plan, policy and authorization gates.

**One artifact store, with two concerns.** `PlanExecutionRecord` + `PlanStepReceipt` carry the
*execution* artifacts; raw SQL and large row sets stay request-local. The *conversational/HITL*
checkpoint is a separate concern and may persist authorized business content — see REVIEW
CORRECTIONS.

**One egress gate.** `ModelGateway`, with explicit sinks. `get_legacy_model` is deleted so that
product safety stops depending on `service_mode`.

---

## IMPLEMENTATION_SLICES

Each slice is bounded, single-writer, and independently reviewable.

| Slice | Scope | Exit criterion |
| --- | --- | --- |
| **P4-S1** | **Wire the typed pipeline** into `AppContainer` (plan executor + deterministic query-plan provider + the existing `SemanticContextResolver` and a concrete evidence provider) | `after_route` selects `compile` on a real request, `PlanExecutor` executes, and the QUERY answer/receipt closure completes **through `QueryGateway` with an `ExecutionReceipt`** (the authoritative P4-Q gate: `MASTER_PR_PLAN_V4.md` §8.5.1, lines 1677-1696; §10.2, line 2260); the dormant subsystem becomes the operating one |
| **P4-S1b** | Freeze the kernel as *the* authority | A readiness component and a test asserting no second execution path is reachable in product |
| **P4-S2** | Port plan card / assumptions / ambiguity / modification UX onto `QueryPlan`/`ExecutionPlan`/`PlanValidationRecord`/interrupt payload; quarantine `codeact_engine` | Legacy UX reproduced without the second authority |
| **P4-S3** | Delete `get_legacy_model` and `build_legacy_provider_model` after callers are gone | A test that the symbol cannot be imported in product |
| **P4-S4** | Place the non-business DB sites under explicit QueryGateway exemptions or a named policy | Every DB touch point has an owner and a test |
| **P4-S5** | Durable-checkpointer resume test across a process restart | Product checkpoint configuration covered by a test, not only by a config rule |

Slices S1–S2 may follow immediately after P2-S1. S3 depends on the P4-S2 outcome. S4 touches
`ai_views`/`schema_snapshot` and should not run concurrently with a P2 writer on the same files.

---

## TEST_GATES

### Existing coverage that locks the kernel (verified present)

| File | Substance |
| --- | --- |
| `tests/unit/test_plan_pipeline.py` | 12 tests: DAG rejection (313), validator permission-bound and replayable (397), executor charges before execution and checkpoints only receipts (449), deadline reserve (487), non-JSON output rejection (520), trusted registry only (548), cancellation propagation (619), zero-model fast path (650), state reset (718) |
| `tests/unit/test_hitl_actions.py` | resume without re-executing SQL (63), idempotency + owner + version (127) — `MemorySaver` (10, 50) |
| `tests/unit/test_query_gateway.py` / `_adapters` / `_capacity` | gateway policy, adapters, capacity |
| `tests/integration/test_query_gateway_postgres.py` | real PostgreSQL gateway integration |
| `tests/unit/test_model_gateway.py` | model policy |
| `tests/unit/test_plan_card.py`, `test_plan_ingestion.py`, `test_process_sandbox.py` | the legacy surface |
| `tests/unit/test_adaptive_router.py`, `test_candidate_consensus.py` | routing/consensus |
| `tests/unit/test_database_boundaries.py` | 19-37 already locks the `DatabasePurpose` separation between business read-only, control, checkpoint and migrator databases |
| `tests/unit/test_model_architecture.py` | 78-90 already asserts the runtime container exposes no `create_supervisor`/`get_supervisor` and that `v2.py` cannot reach the supervisor — an existing architectural lock that P4-S1 should extend rather than invent |

Note the gap the list itself reveals: the legacy surface has tests (`test_plan_card`,
`test_plan_ingestion`, `test_process_sandbox`) which will need to be retired alongside it, and the
**real** HITL has no durable-backend test.

### Required additions

1. Product-mode single-authority test: no second execution path is reachable.
2. Durable Postgres checkpointer resume across a process restart.
3. `get_legacy_model` unreachable in product, asserted at import level.
4. No LLM-authored SQL string can reach `QueryGateway` in product.
5. `codeact_engine` not importable from the product entry point.
6. Every non-gateway DB touch point is either covered by a gateway policy test or carries an
   explicit exemption record.

---

## TRUE_PRODUCT_DECISIONS_REMAINING

1. **Is the typed pipeline meant to be live in production?** It is fully built, tested and
   documented, yet unwired (`container.py:119-123`). This could be deliberate staging behind the
   PR07A slices, or an unfinished integration. The answer decides whether P4-S1 is a wiring task or
   a decision to adopt the kernel — and everything else waits on it.
2. **Display-name collisions** — carried forward unchanged; blocks full activation, not this work.
3. **Which legacy UX items are P4 acceptance criteria?** The plan retains the ideas; P4 needs an
   explicit must-have list (plan card, assumptions, ambiguity warnings, modification workflow,
   confirmation) or scope will drift.
4. **Is `unsafe-dev` allowed to remain in the repository at all** (non-product), or must the mode
   and its graphs be deleted rather than merely gated?
5. **Delete or build-exclude the legacy graphs?** Deletion is cleaner; build-exclusion keeps the
   option open at the cost of a permanent second read path in the tree.
6. **Is Langfuse an approved sink** under P2-S2, and if so with which payload fields?

---

## OUTPUT_FILE

`docs-v4-p4-shared-plan-executor-hitl-evidence.md`

Read-only review. No file under `src/` was modified, no database was queried, and no git state was
changed by this task. The four untracked P2 evidence artifacts listed by the connector remain
untracked and were not touched.

---

## REVIEW CORRECTIONS (post-review, verified against code)

Independent review of this document issued two corrections. Both were re-verified here against the
working tree and are accepted. The first is sharper than the original text and materially changes
the P4 picture.

### 1. The HITL approval has no path to execution

This document described the real HITL **mechanics** as complete — they are — and quoted the
`engine.py:749-750` comment (*"business SQL remains blocked until the owner makes an explicit,
versioned decision"*) as though a decision existed that could unblock execution. It does not.

`hitl` is registered as a node (`engine.py:847`) and appears in exactly **one** edge statement:
`graph.add_conditional_edges("model", after_model, {"hitl": "hitl", END: END})` (`engine.py:868`).
There is **no outgoing edge from `hitl`**. It is a terminal node.

So the flow production actually has is:

```
model -> pending_answer -> hitl -> approve -> emit pending_answer -> END
```

`approve` returns the model's answer and the graph ends. Approval can never reach `compile`, which
means the deep path can never execute. The two gaps therefore compound rather than sit side by side:
`after_route` never selects `compile` because `typed_pipeline_enabled` is false (see
TYPED_KERNEL_FINDINGS), **and** even the branch that does reach HITL cannot continue past approval.

The mechanics are reusable and should be kept: `interrupt`, checkpoint/resume, `expected_version`,
idempotency key, approve/modify/reject/cancel, ownership isolation. The **business flow** is what
must change — a validated typed plan must become the object under approval, and approval must
resume into revalidate → compile → `PlanExecutor` → `QueryGateway`. "Approve an answer and stop"
must not survive as BUILD HITL.

### 2. Checkpoint scope was drawn too narrowly

This document's RECOMMENDED_TARGET said *"One artifact store (receipts only)"*. That is withdrawn.
There are two distinct concerns:

| Concern | May persist |
| --- | --- |
| Execution artifacts | plan checksum, release/snapshot IDs, authorization revision, policy checksum/version, execution receipts, output/rowset digests, HITL decision + version |
| Conversational / HITL state | authorized business content required for thread continuity, user question, plan presentation, modification feedback, answer continuity |

Raw SQL and large database row sets remain request-local unless a product feature explicitly needs
them. System secrets and credentials remain prohibited in both. A durable PostgreSQL
restart/resume integration test is required and does not exist today.

### 3. Later corrections (D1–D5, re-verified at HEAD `16cde3d`)

A later independent re-verification performed after PR #29 merged found five further defects in this
document. Each was re-verified against the working tree at
`16cde3da9bfe3e5bddc02adc313235667f4641a8`, and each is corrected in the body above. The evidence
for every correction is recorded here so the provenance of the change is visible.

**D1 — stale evidence base (annotation, not a rewrite).** The document stated the working tree is at
HEAD `32cbbb1`. That was accurate when written, but PR #29 has since merged and the tree is now at
`16cde3da9bfe3e5bddc02adc313235667f4641a8` (`git rev-parse HEAD`, re-checked). The original HEAD is
retained in the header as the historical evidence base, with a provenance note stating that all
sampled citations were re-verified at `16cde3d`.

**D2 — no `detail` step kind; the compiler/executor split was stated backwards.** The document
asserted that trust calculations plus a category of "detail steps" were consumed by `PlanExecutor`
while never being produced. There is no `detail` or exploration step kind at all. The `PlanStep` union is exactly
`FetchMetricStep | TrustedCalculationStep | VerifyStep` (`src/nl2sql/contracts.py:281-284`), which
the document's own contract table near line 113 already listed. The accurate statement is only that
`PlanCompiler` never *produces* a `TrustedCalculationStep`:
`src/nl2sql/orchestration/planning.py:319-335` emits only `fetch_metrics` and `verify_result`.
Corrected in TYPED_KERNEL_FINDINGS.

**D3 — partial wiring is impossible, and the provider must also be deterministic.** The EXTEND
section told the reader to pass `query_plan_provider` and `plan_executor` and to include the
context resolver as only conditionally required, implying partial wiring was acceptable. It is not.
`src/nl2sql/orchestration/engine.py:105-108` computes `typed_pipeline_enabled = all(c is not None
for c in (context_resolver, query_plan_provider, plan_executor))`, and `engine.py:109-112` raises
`ValueError("typed plan pipeline requires context resolver, query plan provider, and executor")`
when the pipeline is requested but not fully enabled. All three are mandatory **together**. The
adjacent hard requirement is now recorded too: `engine.py:113-118` rejects a query-plan provider
whose `is_deterministic` is false with
`ValueError("pre-route query plan provider must be deterministic and zero-model")`. Corrected in
the EXTEND section.

**D4 — the resolver is not part of the wiring gap.** The document framed `ContextResolver` as one
of the missing collaborators. A concrete `SemanticContextResolver` **already exists**
(`src/nl2sql/semantic/context_compiler.py:156`), alongside `ContextCompiler` (`:87`), and is
covered by `tests/unit/test_plan_pipeline.py:838`. The genuinely missing collaborator is a concrete
`PolicyScopedEvidenceProvider` — the Protocol is at `context_compiler.py:65-71` with zero
implementations in `src/` — together with a concrete `QueryPlanProvider` (only the Protocol at
`src/nl2sql/orchestration/planning.py:39-51`). The gap is now stated as **missing
`PolicyScopedEvidenceProvider` + concrete `QueryPlanProvider`; NOT the resolver.** Corrected in
TYPED_KERNEL_FINDINGS and the EXTEND section.

**D5 — exit criterion too weak.** The P4-S1 exit criterion said only that `after_route` selects
`compile` and `PlanExecutor` executes. That understates the authoritative P4-Q gate. The V4 master
plan `MASTER_PR_PLAN_V4.md` §8.5.1 (lines 1677-1696) requires the QUERY closure to run
question → semantic resolution → authorization → source selection → Published Gold / approved
compute → HITL/clarification when required → grounded answer → **receipt**, and §10.2 (line 2260)
requires P4-Q to reach `QUERY end-to-end baseline PASS` with facts/sources consistent with the
receipt. The exit criterion now requires a real `QueryGateway` execution carrying an
`ExecutionReceipt`. Corrected in IMPLEMENTATION_SLICES.
