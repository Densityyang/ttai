# V4 P5-C — Sandbox / ML Runtime Architecture Recon (READ-ONLY, EVIDENCE + DESIGN BOUNDARY)

- **Repo**: `E:\平台开发\ttai-pr07a-next`
- **Branch**: `agent/v4-ci-hardening` (verified: `git rev-parse --abbrev-ref HEAD`; no branch switch, no stage, no commit)
- **Document status**: architecture recon and design boundary. **This is not an implementation and describes no behavior as shipped unless labelled SHIPPED.**
- **Evidence base**: three completed read-only digests — `A_runtime_kernel.md` (486 lines), `B_isolation_deploy.md` (601 lines), `C_contracts_datapath.md` (694 lines). No recon was redone for this document; no test suite was run.
- **Label legend** (from digest A): **SHIPPED** = reachable on the production HTTP path; **DORMANT** = exists and compiles/tests but is not wired on any product path; **CLI-ONLY** = reachable only from `main.py cli`. **TODAY** = exists on disk now; **NEEDED** = required by the frozen P5-C requirements and absent today; **PROPOSED** = new design proposed here, not present in the repo.

> **Credential rule for this document**: only environment-variable / secret *names* appear. No DSN, password, token, API key, or connection string is printed or embedded anywhere in this file.

---

## RESULT

**The smallest production-grade P5-C architecture is ONE new registered executor capability inside the existing PlanExecutor kernel — not a second orchestration framework.** Concretely, the entire framework surface is:

1. one new member of the `PlanStep` discriminated union (`contracts.py:281-284`),
2. one new value for `PlanStepReceipt.kind` (currently closed to `["fetch_metric","trusted_calculation","verify"]`, `contracts.py:380`),
3. one new injected runner Protocol modelled on `MetricStepRunner` / `TrustedCalculationRunner` (`orchestration/execution.py:74-108`),
4. one new constructor field on `PlanExecutor.__init__` (`execution.py:164-174`) and one new branch in `PlanExecutor._execute_step` (`execution.py:320-380`, fail-closed default `execution_plan_step_unregistered` at `:380`),
5. one validator rule in `PlanValidator.validate_execution_plan` (`orchestration/planning.py:261-280`),
6. a small set of new frozen strict Pydantic v2 contracts in `src/nl2sql/contracts.py` (all **PROPOSED**, none exist),
7. a deployment substrate of **one new sandbox image + one new compose service + one new no-egress network**.

PlanExecutor already injects runners once and never accepts callables from the plan (module docstring `execution.py:1-6`; `__init__` `:165-174`), and dispatches purely by typed discriminator (`:331-380`). That is the registration point P5-C must reuse. **Do not build a queue/worker framework, a second graph engine, or a second plan compiler.**

**State of the world at HEAD.** The sandbox is a **DORMANT dev escape hatch**, not a product capability:
- `enable_dynamic_calc=False` (`config/settings.py:197-200`) and `codeact_mode="disabled"` (`config/settings.py:28-31`).
- Both sandbox classes self-disable unless `codeact_mode=="unsafe-dev"` (`code_executor.py:69-74`; `process_sandbox.py:186-191`), and `unsafe-dev` is structurally forbidden in product mode (`settings.py:267-273`). **The sandbox therefore cannot run in product at all today** — a strong safety property and the reason zero end-to-end isolation tests exist (`tests/unit/test_process_sandbox.py:1`).
- The whole typed kernel that would host a sandbox step is also DORMANT: `container.py:104-124` wires no `context_resolver`, no `query_plan_provider`, no `plan_executor`, so `engine.py:95-118` computes `typed_pipeline_enabled = False` in production and `compile_node`/`execute_node` are unreachable.

**Nothing in the frozen P5-C requirement set exists as code or deployment**: no `SandboxJob`, `DatasetArtifact`, `CalculationPlan`, `SandboxExecutionReceipt`, no artifact store/table, no sandbox image, no sandbox service, no sandbox network, no Docker-level resource limits, no credential-excluding worker, no network denial for a worker, no read-only sandbox rootfs, no versioned package allowlist, no seed discipline. These are **net-new additive substrate**, not hardenings.

### OPEN BLOCKER — the container launch mechanism is undecided

**There is no docker SDK usage anywhere in `src/`** (grep for `docker`/`DockerClient`/`container_create`/`podman` = 0 matches, digest B §1.5), **and `/var/run/docker.sock` is contract-forbidden** (`tests/unit/test_deployment_contracts.py:91-95`; no container in any compose file mounts it, uses `privileged`, or host networking). The recon cannot settle how a sandbox container gets launched. A decision is required between:

- **(a) a dedicated sandbox-runner service/endpoint** — the API asks a separate trusted runner component (which alone holds any container-launch authority) to start the sandbox job; or
- **(b) a rootless runtime socket/shim** exposed to a narrowly scoped launcher process.

**This decision determines the entire network and credential story**: if the launcher shares the API process, the API's four network attachments and six secret files are back in scope; if the runner is a separate component with its own network and secret allowlist, the sandbox service can be `network_mode: none` / `internal: true` with an empty secret set and is independently testable. **It is a product/architecture decision, not something this recon can settle**, and it must be resolved before any sandbox code is written.

### Parent-verified facts at HEAD (independently verified by the parent, not merely asserted by a recon agent)

1. `RequestIdentity.auth_epoch` (`src/nl2sql/contracts.py:39`) has **exactly one occurrence in all of `src/`** — the definition itself. It is never read or populated, so **no authorization-revision binding exists anywhere**.
2. `set_start_method` / `get_context` / `spawn` / `forkserver` have **zero occurrences in `src/`**, so the current sandbox child is created with the default start method = **fork** on Linux and inherits the parent environment (all DSNs and model keys).
3. `src/nl2sql/orchestration/engine.py:468-497`: when execution-plan validation outcome is not `allow`, `compile_node` merely sets `stop_reason` `execution_plan_approval_required` (or `execution_plan_validation_denied`) and returns. There is no interrupt and no approval request, so **a compiled ExecutionPlan can only halt — it can never actually be approved**.
4. **No production QueryPlanProvider exists**: only the Protocol (`src/nl2sql/orchestration/planning.py:39`) and test doubles. Registering a sandbox step is currently gated behind that missing component.
5. **No Docker-level resource limit exists anywhere in `docker/`**: `mem_limit` / `cpus` / `pids_limit` / `deploy.resources` have zero matches.
6. The compose contract test (`tests/unit/test_deployment_contracts.py:88-95`) concatenates **only** `docker/compose.base.yml` and `docker/compose.prod.yml` before asserting `/var/run/docker.sock`, `privileged:`, `network_mode: host`, `ssh_password` and `ssh_private` are absent. `docker/compose.dev.yml` and `docker/compose.release.yml` **are not covered**, so a sandbox service added to either could legally use `docker.sock` or host networking without failing that test. **Concrete test-coverage gap: close it BEFORE relying on that assertion for the sandbox service.**
7. Role-level read-only already ships in-repo and is **not new work**: `docker/initdb/roles.sh:64-87` switches on `DB_APP_PRIVILEGES` and in the readonly branch issues `ALTER ROLE %I SET default_transaction_read_only = on` (`:68`) with SELECT-only grants, while the readwrite branch issues the corresponding `RESET` (`:91`). **What is missing is only its application to the remote `agent_reader` / `agent_reader_user` roles.**

### Scope boundary

This document answers the 20 review questions (mapping in the appendix). It **does not** propose a second orchestration framework, does **not** propose installing packages now, does **not** enable `unsafe-dev`, and does **not** recommend fixing `ProcessSandbox` in place. Where something does not exist it is stated as absent rather than described as if it existed.

---

## CURRENT_SANDBOX_GAPS

Severity: **FATAL** = frozen requirement cannot be met without new substrate; **HIGH** = correctness/security property is unenforced; **MEDIUM** = discipline/coverage gap.

| # | Gap | Evidence | Severity |
|---|---|---|---|
| G1 | No sandbox image, service, or network exists in any compose file. Grep of `docker/*.yml` for `mem_limit|cpus|pids_limit|deploy:|resources:|ulimits|shm_size|oom` = **0 matches**; grep `docker/` for sandbox = **0**. | digest B §1.5, §5.1; parent fact 5 | FATAL |
| G2 | No sandbox/job/artifact contracts exist: `SandboxJob`, `DatasetArtifact`, `CalculationPlan`, `SandboxExecutionReceipt`, `sandbox_receipt` = 0 hits repo-wide. | digest A appendix `468-474`; digest C §0, Appendix A | FATAL |
| G3 | No job model at all: no job table, job id type, queue, worker pool, lease, or heartbeat. Execution is one `engine.ainvoke` per request over a LangGraph `StateGraph`. | digest A §3 (`v2.py:221-223`, `:311`); `engine.py:54-73` | FATAL |
| G4 | No authorization-revision binding: `auth_epoch` is declared and never read/populated; permissions are validated (`planning.py:149-162`) but no revision or permission set is bound into `ExecutionPlan`, `PlanValidationRecord`, `PlanExecutionRecord`, or any receipt. Revocation after approval is undetectable. | digest A §7; digest C §7.2(1); parent fact 1 | HIGH |
| G5 | Credentials are **not physically excluded**: the worker is `multiprocessing.Process` (`process_sandbox.py:246-258`) under default **fork**; the child inherits address space, `os.environ`, open FDs, DB connections and secret objects (business/control/checkpoint DSNs and model-provider keys). | digest A §10; parent fact 2 | FATAL |
| G6 | No OS isolation primitives: 0 hits in `src/` for `seccomp`, `prctl`, `NO_NEW_PRIVS`, `unshare`, `chroot`, `setuid`, `drop_privileg`; no network namespace/denial, no read-only rootfs, no image, no package allowlist. | digest A §2b, §10 | FATAL |
| G7 | No Docker-level resource bounds anywhere (not even for API/DB containers). App-level `resource.setrlimit` (`process_sandbox.py:54-63`) is Linux-only and **silently swallowed** on `ImportError/ValueError/OSError` (`:62`); `RLIMIT_NPROC (0,0)` (`:61`) is hostile to numpy/BLAS; no wall-clock rlimit. | digest B §5.1; digest A §2b | HIGH |
| G8 | `ProcessSandbox` timeout/cancellation is broken: outer `asyncio.wait_for(self._timeout + 5)` (`:204-207`) wraps `to_thread(self._wait_for_result)` (`:260`), but the only `proc.kill()` is inside `_wait_for_result` (`:269-274`). If the outer wait fires first the thread keeps running and the child is orphaned. `join` before draining the `mp.Queue` can block a large payload until kill. | digest A §2b | HIGH |
| G9 | Output is unbounded and unstructured: `SandboxResult.result: Any`, `stdout: str`, no checksums/receipt/resource usage (`schemas.py:65-73`); stdout captured into an unbounded `StringIO` (`process_sandbox.py:129-132, 155`); result payload on an `mp.Queue` with no size check. | digest A §2b; digest B §5.3 | HIGH |
| G10 | HITL cannot approve a compiled plan. `compile_node` sets `execution_plan_approval_required` and returns; there is no interrupt and no approval request (`engine.py:468-497`; `after_compile` `833-834` ENDs). HITL is reachable only from `model_node` on the deep non-shadow route (`engine.py:748-759`). | parent fact 3; digest A §8 | HIGH |
| G11 | No production `QueryPlanProvider`: only the Protocol (`planning.py:39-51`) and test doubles (`test_plan_pipeline.py:225, 246`). `typed_pipeline_enabled` is always False with production wiring, so registering a sandbox step is gated behind this missing component. | parent fact 4; digest A §0 | HIGH |
| G12 | `ExecutionReceipt` (`contracts.py:547-568`) carries no runtime/version/artifact/resource-usage fields; `PlanStepReceipt.kind` has no sandbox member (`contracts.py:380`). The frozen five-item sandbox receipt does not exist. | digest B §5.3; digest C §6.1 | HIGH |
| G13 | Compose contract test covers only `compose.base.yml` + `compose.prod.yml` (`test_deployment_contracts.py:88-95`); `compose.dev.yml` and `compose.release.yml` are uncovered, so a sandbox service placed there could use `docker.sock`/host networking undetected. | parent fact 6 | HIGH |
| G14 | Read-only role hardening is not applied to the remote agent roles: `roles.sh:64-87` implements the readonly branch (`default_transaction_read_only = on` at `:68`) but the remote `agent_reader`/`agent_reader_user` roles do not receive it. | parent fact 7 | MEDIUM |
| G15 | No test verifies runtime isolation. `tests/unit/test_process_sandbox.py:1` states "no subprocess execution in CI"; its 9 tests only exercise `_static_check`. No test launches a sandbox or asserts non-reachability. | digest B §6.1-6.2 | HIGH |
| G16 | A semantic release can become ACTIVE with `schema_snapshot_id = NULL`; `bind_schema_snapshot` (`schema_snapshot.py:1002`) has no production caller (tests only). `SemanticContextResolver.resolve` then raises `semantic_release_schema_snapshot_unbound` (`context_compiler.py:189-190`). | digest C §7.2(5) | HIGH |
| G17 | `sandbox_allowed_modules` (`settings.py:211-217`) is **module-granular**, not a versioned package allowlist; "no dynamic install" is only implied by removing pip (`docker/Dockerfile:51`). | digest B §9(8); digest C §14.3 | MEDIUM |
| G18 | Dev profile bind-mounts host source/config into API containers (`compose.dev.yml:8-10, 22-24`; `docker-compose.yml:11-13`), so a sandbox sharing that container voids "no host filesystem". | digest B §4.1, §9(7) | MEDIUM |

---

## REUSE

### Q1 — Reusable code (build the sandbox ON this; do not rebuild it)

| # | Area | Artifact (file:line) | Status | How P5-C uses it |
|---|---|---|---|---|
| R1 | **Registered-executor seam** | `PlanExecutor` injects runners once, never accepts callables from the plan (`execution.py:1-6, 165-174`); dispatch by typed discriminator `FetchMetricStep` (331), `TrustedCalculationStep` (362), `VerifyStep` (375); fail-closed default `execution_plan_step_unregistered` (380) | SHIPPED (dormant wiring) | **The single most important reuse.** Add one runner Protocol + one `__init__` field + one branch. No second orchestrator. |
| R2 | Runner Protocol shape | `MetricStepRunner` (`execution.py:74-90`), `TrustedCalculationRunner` (93-99), `ResultVerifier` (102-108) | SHIPPED | Model `SandboxJobRunner` on these signatures. |
| R3 | Source-controlled executor adapter | `RegistryTrustedCalculationRunner` (`execution.py:111-130`) wrapping `TrustedTemplateRegistry` (`agents/dynamic_calc/trusted_templates.py:72-97`; `extra="forbid"`, explicit `Literal` template ids at 20-52, 85-94) | SHIPPED | Copy the "registry of approved entrypoints, no model-authored code" pattern for `entrypoint_id`. |
| R4 | Strict/frozen contract style + canonical checksum | `StrictContract` = `ConfigDict(extra="forbid")` (`contracts.py:28-31`); frozen variants `TimeRange` (61), `ExecutionPlan` (290); `_contract_checksum` = canonical sorted-key compact JSON SHA-256 (647-655); `.checksum` properties (158-160, 206-208, 340-342, 464-466, 504-506) | SHIPPED | All new contracts follow this exactly. |
| R5 | Checkpoint-safe receipt discipline | `PlanStepReceipt` (`contracts.py:374-405`, "row values and SQL are never checkpointed" 374-375); `PlanExecutionRecord` (408-440); `ExecutionReceipt` (547-568); `ModelReceipt` versioned+checksummed shape (618-637) | SHIPPED | Direct extension point for the sandbox receipt; reuse `^[0-9a-f]{64}$` digests and lifecycle validators (395-405). |
| R6 | P5-A SQL compiler / gateway | `MetricQueryCompiler` + re-compile-before-execute authority re-read (`metric_query.py:550-635`, 568-580); `metric_plan_executor()` (633-635); `PreparedQuery` (`query_gateway.py:68-80`); `QueryReceipt` (82-131); `execution_receipt` projection (114-131); `QueryGateway` (477+) | SHIPPED/dormant | **Do not rebuild the P5-A half — wire it.** It is also the straight projection source for gateway-produced `DatasetArtifact`. |
| R7 | Deadline/budget primitives | `CallBudget` (`budget.py:56-102`), `RouteBudgetLedger` (109-261), `should_stop` (264-289); `engine._remaining_route_deadline_ms` (972-988), `_pre_route_timeout_ms` (991-1003); `execution.py:195-219` | SHIPPED | Sandbox `deadline_ms` derives from the remaining route deadline minus `reserve_ms`, never a fresh ungoverned budget. |
| R8 | Durable state / audit / identity | `CheckpointerManager` (`checkpointer.py:38-60`); `ControlAuditStore.append` + audit_outbox (`control_audit.py:79-107`); identity-bound thread keys (`ownership.py:10-29`) | SHIPPED | Broker checkpoints only digest receipts; job dispatch/approval/denial are auditable events. |
| R9 | Config knob names | `sandbox_timeout_seconds` (201), `sandbox_max_memory_mb` (206), `sandbox_allowed_modules` (211-217); `enable_dynamic_calc` (197-200) | SHIPPED | Reuse names; change semantics (module list → image + package allowlist checksum). |
| R10 | Compose hardening primitives | Non-root image user (`Dockerfile:53-54, 66`); `no-new-privileges` + `cap_drop: ALL` (`compose.base.yml:28-31`); `read_only: true` + `tmpfs: /tmp` + `cap_drop ALL` precedent (`compose.dev.yml:124-131, 158-160`; `compose.release.yml:152-154, 187-189`); `internal: true` network (`compose.base.yml:76-78`); immutable image by digest `TTAI_IMAGE_REF` (`compose.release.yml:6`); release manifest `image_digest`/`compose_config_checksum` (`deploy/release-manifest.example.yaml:4-5`) | SHIPPED | A sandbox service is a composition of primitives that already exist in this repo. |
| R11 | File-based secret mechanism | `*_FILE` env → `/run/secrets/*` (`compose.prod.yml:11-23`; `compose.release.yml:16-28`); `SecretProvider.get` file/perm checks (`src/core/secrets.py:17-45`); contract test enumerating a service's exact secret set (`test_deployment_contracts.py:61-86`) | SHIPPED | The verifiable mechanism by which the sandbox gets an **empty** secret set. |
| R12 | Deployment-contract test patterns | Set-equality network assertion (`test_deployment_contracts.py:32`); secret enumeration (79-86); docker.sock/privileged/host-network prohibition (91-95); volume inspection (107-109, 170-177); volume disjointness (126-133); ops-container hardening (158-161, 199-201) | SHIPPED | Copy verbatim for the new sandbox service. **But first close gap G13.** |
| R13 | Static high-risk call-site allowlist | `tests/unit/test_query_gateway.py:562-596`; line 584 already registers `codeact_engine/graph.py: {sandbox.execute}` as a reviewed exception | SHIPPED | Registering the new sandbox executor here is a free regression guard: a sandbox step calling a DB method without being listed fails the test. |
| R14 | Container-launch test harness | `tests/integration/test_postgres_governance.py:340-371` runs the image with `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges`; proves read-only at 409-429; docker-CLI harness 57-135 | SHIPPED | Template for the sandbox isolation integration tests. |
| R15 | Capacity / cancellation test patterns | `test_query_gateway_capacity.py:151-193`; `semaphore.py:49-141` (`CapacityExceededError` `queue_full`/`wait_timeout`); `test_plan_pipeline.py:619-646` cancellation propagation; `test_metric_query.py:250-275` | SHIPPED | Reuse for the global sandbox concurrency cap and cancellation-leak tests. |
| R16 | HITL correctness primitives | Idempotency/version/owner checks (`v2.py:279-320`; `engine.py:783-793`); tests (`test_hitl_actions.py:53-88, 127-160`); approval-outcome registration point (`planning.py:261-270`; `test_plan_pipeline.py:583-601`) | SHIPPED except the plan-approval reachability gap (G10) | Bind approval to a sandbox plan at this existing registration/validation point. |
| R17 | Role-level read-only | `docker/initdb/roles.sh:64-87`, readonly branch `ALTER ROLE %I SET default_transaction_read_only = on` (`:68`) with SELECT-only grants, readwrite `RESET` (`:91`) | SHIPPED | Apply to remote agent roles (G14); not new code. |
| R18 | Versioning/checksum discipline | DB-allocated release sequence (`registry.py:302-305`; `004_semantic_registry_v3.sql:7-25`); snapshot checksum recompute/verify (`schema_snapshot.py:1051-1082, 1151-1165`); policy version+checksum (`planning.py:70-88`); `RoutingBudgetPolicy.version/.checksum` (`contracts.py:482-506`); deterministic trace fingerprint (`trace.py:44-47`); release manifest (`deploy/release-manifest.example.yaml:1-21`) | SHIPPED | The runtime/seed/package-version descriptor (Q16) extends this, not a new versioning scheme. |
| R19 | Identity-scoped input interface | `PolicyScopedEvidenceProvider.retrieve_permitted` (`context_compiler.py:65-71` with `RequestIdentity`); `PolicyScopedEvidence` frozen dataclass (49-62); `ai_views` exposure layer (`infra/store/ai_views.py`) | SHIPPED (with known drift: `sync_ai_views_from_yaml` `ai_views.py:570` has zero callers) | Precedent for governed input provisioning (Q17). |

**Not reusable** (see RETIRE): `SandboxExecutor`, the `ProcessSandbox` isolation model, the CodeAct graph, and the LLM-authored-SQL fetch path.

## RETIRE

### Q2 — Sandbox code to retire, and why

> Explicit recon position: **do not fix `ProcessSandbox` in place.** The properties the frozen P5-C requirement names (no credential inheritance, namespaces, read-only rootfs, dropped capabilities, fixed image, versioned allowlist, network denial) are not addable to a fork child inside the API container; the sandbox is replaced by a registered executor capability with a separate process/container boundary.

| # | Artifact | Current classification | Disposition | Why (evidence) |
|---|---|---|---|---|
| T1 | `agents/dynamic_calc/code_executor.py` `SandboxExecutor` | DORMANT, `unsafe-dev` gated (`code_executor.py:69-74`) | **RETIRE OUTRIGHT** | Runs LLM-authored Python **in the API process** via `exec(code, exec_globals)` (140-177; `exec` at 171); shares the address space of the QueryGateway pool, checkpointer DSN and model keys. Boundary is regex + AST denylist only (`_FORBIDDEN_PATTERNS` 19-39, `_static_check` 112-138). `safe_builtins` (149-161) includes `type` and does not restrict attributes, so `().__class__.__base__.__subclasses__()` reaches an already-imported `os`. Timeout is `asyncio.wait_for(asyncio.to_thread(...))` (85-88): cancelling a `to_thread` future **does not stop the thread** — no hard cancellation. Nothing to salvage. |
| T2 | `agents/codeact_engine/process_sandbox.py` `ProcessSandbox` | DORMANT, `unsafe-dev` gated (`process_sandbox.py:186-191`) | **REPLACE (do not harden in place)** | "Process isolation" is `multiprocessing.Process` (246-258) and nothing else; no `set_start_method`/`get_context` anywhere in `src/` → default **fork** on Linux, inheriting address space, `os.environ`, DSNs, open DB connections, secrets. Python-level restrictions (restricted `__import__` 104-108, `safe_builtins` 110-122, AST denylist 289-315) run **inside an already-credentialed process**. Zero hits for `seccomp`/`prctl`/`NO_NEW_PRIVS`/`unshare`/`chroot`/`setuid`/`drop_privileg`. Resource limits are Linux `resource.setrlimit` (54-63) **silently swallowed** (62), `RLIMIT_NPROC (0,0)` (61) hostile to numpy/BLAS, no wall-clock rlimit. Broken timeout/cancellation (204-207 vs 269-274) and unbounded queue/output (129-156). Result model `SandboxResult` is not a `StrictContract` and not frozen (`schemas.py:65-73`). Its own tests exercise only `_static_check` (`test_process_sandbox.py:1-67`). |
| T3 | `agents/codeact_engine/graph.py` orchestration | DORMANT | **RETIRE the graph; ADAPT the UX pieces** | `code_exec_node` (211-224) constructs `ProcessSandbox()` per call; the flow generates code with an LLM (`code_generator.py`, used at 200) and SQL with an LLM (`parallel_fetcher._generate_fetch_sql` 185-202) — the model-authored freeform path inside a calculation graph. Its HITL is cosmetic: `builder.compile(name="codeact_engine")` (521) has **no checkpointer and no `interrupt``, and the comment at 490-492 admits the interrupt mechanism is not implemented while `decompose → lock` is a direct edge (493-494). **Salvageable (ADAPT, not reuse as-is):** `plan_card.py` (`CalcPlanCard`/`ConfirmedCalcPlan`), `plan_ingestion.py` (`ingest`, fail-closed validation), `validator.py` (result invariants). |
| T4 | `agents/dynamic_calc/graph.py` | DORMANT | **RETIRE the `SandboxExecutor` branch; keep the trusted-template branch** | `code_exec_node` (193-267) selects `trusted_template_registry.execute` for `trusted-template` (203-225) — the honest path — or `SandboxExecutor()` for the unsafe branch (265-266), which must die. |
| T5 | Legacy LLM-authored SQL fetch | CLI-ONLY | **Do NOT adopt as an input pattern** | `parallel_fetcher` generates SQL with an LLM (141, 185-202), invokes the freeform `sql_db_query` tool (129, 146) and returns the result as a **string** (`FetchResult.data: str`, 52) — no checksum, no schema descriptor, no artifact object. Freeform model SQL is alive only on the CLI path (`supervisor/agent.py:352` ← `main.py:183-196`, `cli.py:69-76`; `test_model_architecture.py:87` asserts the container exposes no `create_supervisor`). |

### Do NOT retire (explicitly keep)

`TrustedTemplateRegistry` (`trusted_templates.py`); `PlanExecutor` / `PlanValidator` / `PlanCompiler` (`execution.py`, `planning.py`); `QueryGateway` (`query_gateway.py`); `GatewayMetricStepRunner` + `MetricQueryCompiler` + `metric_plan_executor` (`metric_query.py:550-635`); `ControlAuditStore`; `CheckpointerManager`; `RouteBudgetLedger` (`budget.py`); `SemanticContextResolver` (`context_compiler.py:156`); the compose hardening primitives and the secret-file mechanism.

## TARGET_TOPOLOGY

### Q3 — process-per-job vs long-lived worker vs isolated container

**Today there is no job model at all.** Execution is one `engine.ainvoke` per request (`v2.py:221-223, 311`) over a LangGraph `StateGraph` with a checkpointed `V2EngineState` (`engine.py:54-73`). There is **no job table, job id type, queue, worker pool, lease, or heartbeat** in `src/` (`SandboxJob`/`sandbox_job`/`artifact` = 0 hits). The only crossing mechanism is an in-memory `data_context` dict (`process_sandbox.py:201, 317-334`; built in `codeact_engine/graph.py:408-446`) that carries live pandas objects and is not an artifact contract.

#### Decision matrix (evidence-based, not preference)

| Option | Credential exclusion | OS isolation | Hard cancellation | Resource bounds | Worker lifetime independent of checkpointer | Verdict |
|---|---|---|---|---|---|---|
| **A. In-process exec / thread** (today's `SandboxExecutor`) | None — same address space | None | **No** (a `to_thread` future cannot be stopped; `code_executor.py:85-88`) | App settings only | n/a | **RETIRE (T1)** |
| **B. Process-per-job, default fork** (today's `ProcessSandbox`) | **None** — inherits `os.environ`, FDs, DSNs, secret objects | None (no namespaces/caps/RO rootfs) | Partial at best (kill runs only on the inner path; `:269-274`) | `setrlimit` only, silently swallowed (`:62`) | Yes | **Insufficient** |
| **C. Process-per-job, explicit `spawn` context + env allowlist** (minimum viable stepping stone) | Strong *if enforced* — a spawned child receives only an allowlisted env; requires setting the start method explicitly (absent today) | Partial — still no namespaces, RO rootfs or caps | Yes — kill the process group | rlimits + wall-clock; host-level, not cgroup | Yes | **Acceptable stepping stone, not the target** |
| **D. Long-lived sandbox worker pool** | **Fails** — a worker that owns the checkpointer must hold `CHECKPOINT_DATABASE_URL`; a worker near the gateway must hold the business DSN → violates P5-C | Partial possible | Requires a **bespoke in-worker cancel protocol that does not exist** | cgroup possible | Poor — must reconnect and holds credentials | **REJECT for P5-C** |
| **E. Short-lived container-per-job** | **Strongest** — separate container, explicit empty/allowlisted secret set, no inherited FDs or address space | Yes — namespaces, `cap_drop ALL`, `no-new-privileges`, read-only rootfs, network denied | Yes — kill the container / process group | Yes — cgroup memory/CPU/PIDs + rlimits + sized tmpfs | Yes — one-shot | **TARGET** |

#### Constraints that force the choice (all evidenced)

1. **Credential ownership.** `CheckpointerManager` needs `CHECKPOINT_DATABASE_URL` (`checkpointer.py:44-55`); the QueryGateway needs the business DSN (`database.py:39-81`). A long-lived sandbox worker that also owned the checkpointer would necessarily hold those credentials → violates P5-C. A fork child still inherits them today; a container-per-job is the only option that removes them **physically**.
2. **State/checkpoint.** Resume/approval relies on LangGraph checkpoints and the identity-bound thread key (`ownership.py:10-14`; `checkpointer.py:15-73`). The sandbox job must **not** be checkpointed by the worker — the broker checkpoints only a digest receipt (`PlanStepReceipt` "row values and SQL are never checkpointed", `contracts.py:374-375`; `PlanExecutionResult` "outputs are request-local only", `execution.py:156-161`). Worker lifetime must therefore be independent of checkpointer lifetime.
3. **Cancellation.** Today cancellation is only `asyncio.CancelledError` propagation (`execution.py:232-233`; `semaphore.py:71-133`) and cooperative timeouts — no cancel token, no cancel endpoint (`v2.py:73` `cancel` is a HITL action literal only). "Hard cancellation" therefore means **kill a process group or container**.
4. **Isolation level required.** Non-root, no-new-privileges, dropped caps, PID/CPU/memory/wall bounds, read-only rootfs, bounded scratch, network denied, fixed image + versioned allowlist, no dynamic install: only satisfiable by a container, or at minimum a separately spawned, env-scrubbed process with namespaces. None of this exists; `docker/` contains no sandbox service.
5. **Concurrency.** Prod runs the API with `workers=1` (`main.py:109-125`); in-process `exec`/`to_thread` competes with request handling. Process/container-per-job is the only model that gives a hard wall-clock kill.

#### Recommendation (design, NOT implemented)

- **Target: short-lived container-per-job.** Minimum viable implementation: option C behind the **same `SandboxJob` contract**, so the contract does not change when the isolation mechanism is upgraded from process to container.
- **Broker owns credentials + checkpointer + artifacts.** It materializes `DatasetArtifact` payloads and dispatches a job; it never sends a DB handle, DSN, URL, path, or provider client across the boundary.
- **Deployment shape is a capability, not a control plane:** one new compose service + one new internal (or `none`) network + one new image, built from the existing hardening primitives (R10, R11).

#### Registration frame (the whole framework)

| Integration point | Evidence |
|---|---|
| New runner Protocol next to `MetricStepRunner` / `TrustedCalculationRunner` | `execution.py:74-108` |
| New field in `PlanExecutor.__init__` | `execution.py:164-174` |
| New branch in `PlanExecutor._execute_step` (fail-closed default preserved) | `execution.py:320-380` |
| New member of the `PlanStep` discriminated union | `contracts.py:281-284` |
| New `PlanStepReceipt.kind` value | `contracts.py:380` |
| New validator rule in `PlanValidator.validate_execution_plan` | `planning.py:261-280` |
| Unknown step kinds already rejected | `execution.py:380` |

**The sandbox image must NOT be the app image**: the app image contains `asyncpg`/`psycopg`/`sqlalchemy` (`pyproject.toml:9,23,24,27`) and `langchain`/`openai`/`langfuse` (14-20), which would hand the sandbox DB-driver and model-provider client code.

## SANDBOX_JOB_CONTRACT

### Q4 — The exact SandboxJob contract required

**Nothing in this section exists today.** `SandboxJob`, `sandbox_job`, `DatasetArtifact`, `CalculationPlan`, `artifact` all return 0 hits over `src/`; there is no executor/job registry type and no `SandboxJobReceipt` (digest A appendix `468-474`; digest C Appendix A). The shapes below are **PROPOSED**, follow the repository's frozen/strict Pydantic v2 style (`StrictContract`, `ConfigDict(extra="forbid", frozen=True)`, `^[0-9a-f]{64}$` digests, `Field(ge=..., le=...)` bounds, `@model_validator(mode="after")` cross-field checks, `.checksum` via `_contract_checksum`), and are consolidated from digest A §4 and digest C §6.2 — **the two digests differ in naming and typing; the divergences are listed below and must be reconciled before coding.**

**Hard rule for every field:** no field may reference a DB session, QueryGateway, DSN, token, authorization header, provider client, host path, URL, or callable. Code is selected by approved `entrypoint_id` only — never carried as source.

#### Step registration (the executor-capability seam)

```python
# PROPOSED additions to src/nl2sql/contracts.py
# PlanStepReceipt.kind is today a closed Literal at contracts.py:380; extend it (digest A §4 / digest C §6.2).
PlanStepKind = Literal["fetch_metric", "trusted_calculation", "verify", "sandbox_calculation"]

class SandboxStep(StrictContract):
    """New member of the PlanStep discriminated union (contracts.py:281-284)."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sandbox_calculation"] = "sandbox_calculation"
    step_id: PlanStepId
    calculation_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_config_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_refs: tuple[PlanInputRef, ...] = Field(min_length=1, max_length=16)
    depends_on: tuple[PlanStepId, ...] = Field(min_length=1, max_length=16)

class SandboxJobRunner(Protocol):
    """Injected once, mirrors MetricStepRunner (execution.py:74-90)."""
    async def run(
        self,
        *,
        step: SandboxStep,
        inputs: dict[str, JsonValue],
        deadline_ms: int,
    ) -> tuple[JsonValue, "SandboxJobReceipt"]: ...
```

#### Runtime descriptor (versioned + checksummed, per Q16)

```python
# PROPOSED. Union of digest A SandboxRuntimeConfig and digest C SandboxRuntimeConfig.
SandboxRuntimeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")]

class SandboxRuntimeConfig(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: SandboxRuntimeId
    runtime_version: str = Field(min_length=1, max_length=128)
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")  # A: runtime_image_digest / C: image_digest
    python_version: str = Field(min_length=1, max_length=32)
    package_allowlist_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    random_seed: int = Field(ge=0, le=2**32 - 1)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=128)
    locale: str = Field(default="C.UTF-8", min_length=1, max_length=64)
    thread_env: dict[str, str] = Field(default_factory=dict)   # e.g. OMP_NUM_THREADS=1, declared so the checksum covers it
    network: Literal["denied"] = "denied"                      # A uses "denied", C uses "deny"; pick one literal
    read_only_rootfs: Literal[True] = True
    no_new_privileges: Literal[True] = True
    run_as_non_root: Literal[True] = True
    dropped_capabilities: frozenset[str] = Field(min_length=1)
    cpu_ms: int = Field(ge=1, le=3_600_000)
    memory_mb: int = Field(ge=32, le=65_536)
    wall_clock_ms: int = Field(ge=1, le=3_600_000)
    pids: int = Field(ge=1, le=1_024)
    scratch_bytes: int = Field(ge=0, le=8_589_934_592)

    @property
    def checksum(self) -> str: return _contract_checksum(self)
```

#### Calculation plan (typed IR — no code field)

```python
# PROPOSED
class CalculationStep(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fetch_artifact", "transform", "aggregate", "fit_predict", "emit"]
    step_id: PlanStepId
    input_refs: dict[PlanInputName, PlanInputRef] = Field(default_factory=dict)
    depends_on: tuple[PlanStepId, ...] = Field(default=())
    # NO code field: code selection is by approved package / entrypoint id only

class CalculationPlan(StrictContract):
    """Typed, deterministic IR for staged / multi-query / post-query work."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    entrypoint_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")   # approved, source-controlled
    steps: tuple[CalculationStep, ...] = Field(min_length=1, max_length=32)
    artifact_refs: tuple[UUID, ...] = Field(min_length=1, max_length=16)
    output_schema: dict[str, Literal["int", "float", "decimal", "str", "bool", "timestamp"]] = Field(min_length=1)

    @property
    def checksum(self) -> str: return _contract_checksum(self)

    @model_validator(mode="after")
    def validate_plan_dag(self) -> "CalculationPlan": ...   # same DAG validation as ExecutionPlan (contracts.py:298-338)
```

#### Job request (dispatched by the broker; the sandbox sees nothing else)

```python
# PROPOSED
class SandboxJobRequest(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    request_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=256)
    deadline_ms: int = Field(ge=1, le=3_600_000)
    authorization_revision: str = Field(min_length=1, max_length=256)  # opaque label, see Q7
    semantic_release_id: UUID
    schema_snapshot_id: UUID
    policy_version: str = Field(min_length=1, max_length=128)
    policy_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    calculation_plan: CalculationPlan
    artifacts: tuple["DatasetArtifact", ...] = Field(min_length=1, max_length=16)
    runtime: SandboxRuntimeConfig
    runtime_config_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_contract: Literal["bounded_json", "artifact"]
```

#### Job receipt (the "execution receipt" of Q6 — secret-free and checkpoint-safe)

```python
# PROPOSED. Digest A names this SandboxJobReceipt; digest C names it SandboxExecutionReceipt with
# execution_plan_checksum + calculation_plan_checksum + runtime_config_checksum. One name must be chosen.
class SandboxArtifactRef(StrictContract):
    """Cheap join row; the full descriptor is the DatasetArtifact."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)

class SandboxJobReceipt(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: Literal["succeeded", "failed", "deadline_exceeded", "cancelled", "denied", "resource_exceeded"]
    stop_reason: str | None = Field(default=None, min_length=1, max_length=128)
    execution_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    calculation_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_config_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")            # digest over input artifacts
    input_artifacts: tuple[SandboxArtifactRef, ...] = Field(default=(), max_length=16)
    result_artifact: SandboxArtifactRef | None = None
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_id: SandboxRuntimeId
    runtime_version: str = Field(min_length=1, max_length=128)
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    python_version: str = Field(min_length=1, max_length=32)
    package_allowlist_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    random_seed: int = Field(ge=0, le=2**32 - 1)
    authorization_revision: str | None = Field(default=None, min_length=1, max_length=256)
    semantic_release_id: UUID | None = None
    schema_snapshot_id: UUID | None = None
    policy_version: str | None = Field(default=None, min_length=1, max_length=128)
    exit_code: int | None = None
    oom_killed: bool = False
    wall_clock_ms: int = Field(default=0, ge=0)
    cpu_ms: int = Field(default=0, ge=0)
    peak_memory_bytes: int = Field(default=0, ge=0)
    scratch_bytes_written: int = Field(default=0, ge=0)
    stdout_bytes: int = Field(default=0, ge=0, le=1_048_576)
    stdout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "SandboxJobReceipt":
        # mirrors PlanStepReceipt invariants (contracts.py:395-405)
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful sandbox job cannot contain an error code")
        if self.status == "succeeded" and self.result_artifact is None and self.output_digest is None:
            raise ValueError("successful sandbox job requires a result artifact or output digest")
        if self.status != "succeeded" and self.error_code is None:
            raise ValueError("failed sandbox job requires an error code")
        if self.status == "cancelled" and self.exit_code not in {None, 137, 143}:
            raise ValueError("cancelled sandbox job exit code is not a termination signal")
        return self
```

#### Digest divergences that MUST be reconciled before implementation

| Item | Digest A | Digest C | Consequence |
|---|---|---|---|
| Step kind literal | `"sandbox"` | `"sandbox_calculation"` | One value only; it appears in the `PlanStep` union and `PlanStepReceipt.kind`. |
| Receipt name/fields | `SandboxJobReceipt`; `input_checksum`, `plan_checksum` | `SandboxExecutionReceipt`; `execution_plan_checksum`, `calculation_plan_checksum`, `runtime_config_checksum` | The `ExecutionPlan` checksum and the `CalculationPlan` checksum are **different hashes**; the receipt must carry both or the binding is ambiguous. |
| Authorization revision | `int` (`Field(ge=0)`) | opaque `str` (1–256 chars) | Must match whatever the auth layer eventually exposes (Q7); an int cannot express a tenant-scoped revision label. |
| Network literal | `Literal["denied"]` | `Literal["deny"]` | Cosmetic, but fixed at freeze time. |
| Runtime size units | `cpu_ms`/`memory_mb` | `cpu_seconds`/`memory_bytes` | Pick one; mixed units in a checksummed contract are a correctness hazard. |
| Artifact descriptor | thin (`sha256`, `media_type`, `columns: tuple[str,...]`) | full (`ArtifactSchema` + `ArtifactSourceBinding`) | Recommend the digest C superset plus A's `media_type`/`data_classification`. |

### Q6 — Required execution receipt / provenance

The frozen P5-C receipt list is **input checksum, plan checksum, runtime version, artifact checksum, resource usage**. **None of the five exists in any current receipt** (digest C §6.1). What exists today:

| Existing receipt | Evidence | What it is | Why it is insufficient alone |
|---|---|---|---|
| `ExecutionReceipt` (22 fields) | `contracts.py:547-568` | SQL/rows receipt: datasource, readonly_role, row_count, `rowset_sha256`, `sql_fingerprint`, `semantic_signature`, policy/masking/source/freshness | No runtime, image, version, artifact or resource-usage concept. |
| `QueryReceipt` (dataclass) | `query_gateway.py:82-100` | Raw gateway result; authority-agnostic | Needs enrichment (`metric_query.py:615-624`); no rowset/plan hashes itself. |
| `ModelReceipt` | `contracts.py:618-637` | Versioned+checksummed provider identity (profile/prompt version+checksum) | Right *shape* precedent, no data concept. |
| `PlanStepReceipt` / `PlanExecutionRecord` | `contracts.py:374-440` | Checkpoint-safe digest-only step records; `kind` closed to 3 values | No sandbox kind; no runtime/artifact fields. |

Required receipt mapping (PROPOSED field → frozen requirement → today's status):

| Frozen requirement | Proposed field(s) | Status today |
|---|---|---|
| Input checksum | `input_checksum` + `input_artifacts: tuple[SandboxArtifactRef,...]` | Absent |
| Plan checksum | `execution_plan_checksum` + `calculation_plan_checksum` (distinct) | Absent |
| Runtime version | `runtime_id`, `runtime_version`, `runtime_image_digest`, `python_version`, `package_allowlist_checksum`, `random_seed`, `runtime_config_checksum` | Absent (no runtime/image version recorded on any execution artifact) |
| Artifact checksum | `result_artifact: SandboxArtifactRef` (`content_sha256`, `schema_checksum`), `output_digest` | Absent |
| Resource usage | `wall_clock_ms`, `cpu_ms`, `peak_memory_bytes`, `scratch_bytes_written`, `oom_killed`, `exit_code`, `stdout_bytes` | Absent |
| Authority binding | `authorization_revision`, `semantic_release_id`, `schema_snapshot_id`, `policy_version` | Absent (see AUTH_HITL_BINDING) |

**Receipt discipline to preserve:** digest-only, never row values or SQL (`contracts.py:374-375`); checkpoint-safe (`execution.py:156-161`); JSON outputs normalized through the strict `JsonValue` TypeAdapter so non-JSON outputs fail closed with `plan_step_output_not_json` (`execution.py:430-436`). Do **not** extend the SQL-shaped `ExecutionReceipt` with sandbox-only fields in a way that lets a sandbox job masquerade as a gateway query; a sibling receipt keyed by `PlanStepReceipt.kind = "sandbox_calculation"` is the honest shape.

### Q9 — Timeout, cancellation, idempotency

#### What the kernel already does

- **Timeout is real, layered and cooperative.** `RequestContext.deadline_ms` (`contracts.py:47`); route budgets `fast=4s / standard=10s / deep=30s` + `reserve_ms=800` (`budget.py:23-53`); remaining-deadline computation (`engine.py:972-988, 991-1003`); PlanExecutor sets `timeout_ms = min(deadline_ms, budget.limits.deadline_ms) - reserve_ms`, fails fast to `deadline_exceeded`, and wraps the whole step loop in `async with asyncio.timeout(timeout_ms/1000)` (`execution.py:195-219`); `TimeoutError` → `status="deadline_exceeded"`, `stop_reason="execution_deadline_exceeded"` (293-308); per-step timeout handed to the runner (342).
- **Cancellation is weak.** Only `asyncio.CancelledError` re-raise in the step loop (`execution.py:232-233`) and semaphore-waiter cancellation (`semaphore.py:71-133`). There is **no job cancel token and no cancel HTTP endpoint** (the only "cancel" is a HITL action literal, `v2.py:73`, `engine.py:770, 798`), and no process-group/container kill.
- **Idempotency is narrow.** HITL actions only: API pre-check (`v2.py:293-305`) + graph `applied_actions` (`engine.py:786-793, 808`) with `expected_version` staleness (`:783-784`); loop-breaker counters (`budget.py:159-221`) and `ExecutionReceipt.sql_fingerprint` (`contracts.py:557`). There is **no execution-level dedupe key and no persisted job ledger**.

#### Required design for the sandbox job

1. **Wall-clock deadline** = remaining route deadline minus `reserve_ms`, never a fresh budget; reject dispatch when the deadline has already elapsed. The container/process gets a wall-clock bound and a CPU bound; on expiry it is **killed** (SIGKILL → exit 137, or SIGTERM 143 for graceful cancel) and a receipt with `status="deadline_exceeded"` is emitted.
2. **Hard cancellation** requires a cancel path that does not exist: a `job_id`-scoped cancel signal propagated through the runner to a process-group/container kill. It must be recorded, and a cancelled execution must not be resumable (precedent: `test_plan_pipeline.py:183`, `:619-646`).
3. **Idempotency** = `idempotency_key` + a durable job ledger with at-most-once dispatch: a replay returns the prior receipt and never runs a second job. Today no job ledger exists; only the in-graph `applied_actions` map does. The dispatch ledger is new substrate.
4. **Stop reasons** are structured, mirroring `PlanExecutionRecord.stop_reason` / status literals (`contracts.py:408-440`), and must distinguish `denied` (pre-execution refusal) from `failed` (execution error) from `deadline_exceeded`/`cancelled`/`resource_exceeded`.

## DATASET_ARTIFACT_MINIMUM

### Q5 — Minimum DatasetArtifact contract

**`DatasetArtifact` does not exist.** Repo-wide grep for `DatasetArtifact` / `dataset_artifact` / `SandboxReceipt` / `sandbox_receipt` / `ArtifactReceipt` = **0 matches** (digest C §0). There is no artifact table and no artifact store in any migration (control migrations 001-004 create only `schema_migrations`, `semantic_releases`, `semantic_documents`, `semantic_release_pointers`, `audit_events`, `audit_outbox`, `benchmark_runs`, `schema_snapshots`, `semantic_assets`, `semantic_aliases`, `semantic_edges`, `semantic_validation_issues`, `source_freshness`). The only "Artifact" symbol is `AnswerArtifact` (`contracts.py:571-576`) — unrelated.

#### Provenance carriers that already exist (per field)

| Provenance concern | Existing field | Evidence | Missing for a DatasetArtifact |
|---|---|---|---|
| source type | `ExecutionReceipt.source_kind` `Literal["approved_aggregate","approved_detail"]` | `contracts.py:563` | No upload/API/derived kinds |
| source id | `ExecutionReceipt.source_id` | `contracts.py:564` | — |
| source selection | `ExecutionReceipt.selection_reason` | `contracts.py:565` | — |
| rowset checksum | `ExecutionReceipt.rowset_sha256` | `contracts.py:560` | — |
| output checksum | `PlanStepReceipt.output_digest` | `contracts.py:383` | — |
| data_as_of | `ExecutionReceipt.data_as_of` / `PlanStepReceipt.data_as_of` | `contracts.py:561, 385` | — |
| freshness | `ExecutionReceipt.freshness_status` / `source_checkpoint` | `contracts.py:562, 567` | — |
| row count | `ExecutionReceipt.row_count` | `contracts.py:551` | — |
| byte size | **absent** | — | No byte_size anywhere |
| column schema | **absent as a typed input descriptor** | Only physical catalog structures: `SchemaSnapshotCandidate`/`RelationSnapshot`/`ColumnSnapshot` (`schema_snapshot.py:162, 191`; payload 1226-1261) | No input-artifact column schema, no logical type enum, no unit enum |
| semantic signature | `ExecutionReceipt.semantic_signature` = sha256(plan+metric+policy+release+snapshot+checkpoint) | `metric_query.py:455-461`, `contracts.py:568` | — |
| authorization binding | **absent** | 0 matches | See AUTH_HITL_BINDING |
| degradation | `ExecutionReceipt.source_degradation` | `contracts.py:566, 17-25` | — |

The gateway already produces a **step**-scoped provenance tuple; nothing represents a **portable, transferable input dataset** with a schema descriptor and byte/row counts.

#### PROPOSED contract (digest C §5.3, frozen strict Pydantic v2)

```python
# PROPOSED additions to src/nl2sql/contracts.py near the PlanStepId/PlanInputRef aliases (contracts.py:211-225)
ArtifactSourceKind = Literal["query_gateway", "upload_csv", "upload_xlsx", "approved_api", "derived"]
ArtifactLogicalType = Literal["integer", "decimal", "float", "boolean", "date", "timestamp", "timestamptz", "text"]
ArtifactSemanticUnit = Literal["count", "percent", "ratio", "score", "duration_minutes", "unknown"]
ArtifactProvenanceClass = Literal["approved_business", "user_supplied", "approved_external", "derived"]

class ArtifactColumn(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    logical_type: ArtifactLogicalType
    nullable: bool = False
    unit: ArtifactSemanticUnit = "unknown"
    sensitivity: ModelDataClassification = "internal"

class ArtifactSchema(StrictContract):
    """Typed, order-stable descriptor of the columns the artifact actually carries."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    columns: tuple[ArtifactColumn, ...] = Field(min_length=1, max_length=256)

    @field_validator("columns")
    @classmethod
    def validate_unique_columns(cls, value: tuple[ArtifactColumn, ...]) -> tuple[ArtifactColumn, ...]:
        names = tuple(column.name for column in value)
        if len(set(names)) != len(names):
            raise ValueError("artifact column names must be unique")
        return value

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)

class ArtifactSourceBinding(StrictContract):
    """Where the artifact came from, and the authority that permitted it."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ArtifactSourceKind
    provenance_class: ArtifactProvenanceClass
    source_id: str = Field(default="", pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")       # over the OUT-OF-BAND payload bytes
    sql_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rowset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authorization_revision: str | None = Field(default=None, min_length=1, max_length=256)
    policy_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_id: UUID | None = None
    user_id: str | None = Field(default=None, min_length=1, max_length=256)

class DatasetArtifact(StrictContract):
    """The ONLY dataset object that may cross into the P5-B / P5-C runtime."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID
    schema_version: Literal["1.0"] = "1.0"
    source: ArtifactSourceBinding
    schema_def: ArtifactSchema            # name avoids shadowing BaseModel.schema
    row_count: int = Field(ge=0, le=200_000)
    byte_size: int = Field(ge=0, le=8_388_608)
    semantic_release_id: UUID | None = None
    schema_snapshot_id: UUID | None = None
    schema_snapshot_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    query_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_plan_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime | None = None
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    degradation_flags: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("data_as_of")
    @classmethod
    def validate_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("dataset data_as_of must be timezone aware")
        return value

    @model_validator(mode="after")
    def validate_source_binding(self) -> "DatasetArtifact":
        if self.source.kind == "query_gateway":
            if not self.source.source_id or not self.source.sql_fingerprint or not self.source.rowset_sha256:
                raise ValueError("gateway artifacts require source_id, sql_fingerprint, rowset_sha256")
        elif self.source.kind in {"upload_csv", "upload_xlsx", "approved_api"}:
            if self.semantic_release_id is not None or self.query_plan_sha256 is not None:
                raise ValueError("non-query artifacts cannot claim release or plan authority")
        return self

    @property
    def checksum(self) -> str:
        return _contract_checksum(self)
```

#### Design rules this encodes

1. **The descriptor is the contract; the payload travels out of band.** `DatasetArtifact` carries `content_sha256` + `row_count` + `byte_size` only — mirroring the existing rule that raw SQL and rowsets stay request-local and are never checkpointed (`contracts.py:375`; `QueryCandidate.sql` is `repr=False/exclude=True`, `contracts.py:539-544`).
2. **`data_as_of` is optional-but-validated** ("where applicable"), matching `SourceFreshnessRecord`'s timezone-aware requirement (`metric_query.py:122-136`).
3. **Authorization binding is a reference, never a token**: `authorization_revision` + `policy_fingerprint` are opaque revision labels, so no credential, DSN or bearer token can be represented.
4. **Semantic/schema snapshot reference is where applicable**: gateway artifacts must carry both; upload artifacts must NOT claim them (validator above), because a user file has no semantic release.

#### Minimum viable subset (if the full shape is deferred)

The smallest contract that still satisfies Q4/Q6/Q7 is: `artifact_id`; `content_sha256`; `byte_size`; `row_count`; a column schema (names + logical types + nullability); `source.kind` + `provenance_class`; `authorization_revision` + `policy_fingerprint`; and, for gateway-produced artifacts, `semantic_release_id` + `schema_snapshot_id` + `schema_snapshot_checksum` + `query_plan_sha256`. Everything else (units, sensitivity, freshness, degradation flags) can be added later without breaking the crossing rule, because the payload never crosses as an object.

**The hard caps are PROPOSED, not decided.** `200_000` rows / `8 MiB` are looser than the gateway envelope (`max_rows` default 200, `max_result_bytes` default 1_000_000 — `query_gateway.py:489-492`) so P5-B can carry a bounded aggregate extract larger than one SQL page, while staying far below the "never cross" tables. **These values must be confirmed as product policy, not inferred** (see TRUE_PRODUCT_DECISIONS_REMAINING).

## AUTH_HITL_BINDING

### Q7 — How authorization revision / semantic release / schema snapshot / plan hash are bound today

#### Exact existing fields

| Authority | Field | Evidence | Status |
|---|---|---|---|
| authorization | `RequestIdentity.auth_epoch: int \| None` | `contracts.py:39` | **Declared, never populated** — exactly one occurrence in all of `src/` (parent-verified); `v2._request_identity` (`v2.py:108-119`) never sets it |
| authorization | `PolicyDecision` (outcome/max_rows/timeout_ms/data_scope/reason) | `contracts.py:50-55` | Carries no revision/fingerprint |
| authorization | 0 matches in `src/` for `authorization_revision`, `AuthorizationContext`, `EffectiveScope`, `AuthorizationDecision`, `policy_fingerprint` | digest C §0, §7.2(1) | Absent |
| semantic release | `ContextBundle.semantic_release_id: UUID` | `contracts.py:128` | Required |
| schema snapshot | `ContextBundle.schema_snapshot_id: UUID` | `contracts.py:129` | Required |
| plan hash | `QueryPlan.checksum` | `contracts.py:206-208, 647-655` | SHA-256 of canonical JSON |
| plan hash | `ExecutionPlan.query_plan_sha256` | `contracts.py:292` | Binds the DAG to the proposal |
| release/snapshot | `ExecutionPlan.semantic_release_id` / `.schema_snapshot_id` | `contracts.py:293-294` | Binds the DAG to the authority |
| compiler policy | `ExecutionPlan.policy_version` | `contracts.py:295` (= `plan-compiler.v1`, `planning.py:24, 300`) | Compiler version, **not** an approval policy version |
| validator policy | `PlanValidationRecord.policy_version` / `.policy_checksum` / `.query_plan_sha256` / `.context_checksum` | `contracts.py:358-362`; checksum computed `planning.py:79-88` | Computed and stored, **dropped before execution** |
| gateway policy | `ExecutionReceipt.policy_version` (= `query-gateway-v2`, `query_gateway.py:38`); `PreparedQuery.policy_version/.fingerprint` | `contracts.py:558`; `query_gateway.py:74, 79` | Gateway-scoped only |
| semantic signature | `ExecutionReceipt.semantic_signature` | `contracts.py:568`; `metric_query.py:455-461` | sha256(plan+metric+policy+release+snapshot+checkpoint) |
| execution identity | `ExecutionReceipt.datasource` / `.readonly_role` | `contracts.py:548-549` | — |

#### Enforcement that already exists (do not duplicate)

- `_execution_plan_mismatch` denies on `query_plan_sha256`, `semantic_release_id`, `schema_snapshot_id` mismatch (`execution.py:383-394`).
- `validate_execution_plan` issues the same three denies (`planning.py:208-231`).
- `PlanCompiler.compile` refuses unless `validation.outcome == allow` and both hashes match (`planning.py:312-317`); it writes `semantic_release_id`/`schema_snapshot_id` from the context (309-335).
- `MetricQueryCompiler.compile` re-reads the ACTIVE release and snapshot and requires `release.release_id == context.semantic_release_id`, `snapshot.snapshot_id == context.schema_snapshot_id`, `release.schema_snapshot_id == snapshot.snapshot_id`, `release.schema_snapshot_checksum == snapshot.checksum` (`metric_query.py:231-240`), and re-reads freshness authority (524-547).
- `GatewayMetricStepRunner.execute` recompiles and compares SQL, params, snapshot_checksum, operation, dimension, freshness, semantic_signature, source_id/kind, selection_reason, degradation and the prepared SQL fingerprint before executing (`metric_query.py:568-580`), then verifies the gateway receipt fingerprint/policy/identity (`execution.py:343-358`).
- `execute_node` re-checks the validation record's hashes (`engine.py:515-519`).

#### Gaps (each must be closed for P5-C)

1. **Authorization revision does not exist.** `auth_epoch` is dead; `v2._request_identity` never sets it, so the only per-request authorization data is `roles`/`permissions` frozensets (`contracts.py:34-39`). A revocation or role change after approval is undetectable. (Precedent proposal: `docs-v4-p2-s1-contract-recon.md:79-81`.)
2. **`ExecutionReceipt` binds neither release, snapshot, nor plan hash** — it carries `sql_fingerprint` + `semantic_signature` only, which can *correlate* but not *verify* without the release artifacts.
3. **`PlanValidationRecord.policy_checksum` is dropped.** Computed (`planning.py:88`) and stored (`:185, 286`) but never reaches `ExecutionPlan`, `PlanExecutionRecord` or any receipt, so validator-policy provenance is lost between validation and execution.
4. **`ExecutionPlan.policy_version` is the compiler version, not an approval policy version.** No field says "executed under validator policy checksum X".
5. **A semantic release can be ACTIVE with `schema_snapshot_id = NULL`.** `_validate_schema_snapshot_binding` returns early when the candidate snapshot id is None (`registry.py:727-728`); `_validate_release_candidate` only *requires* a snapshot when assets exist (`registry.py:644-645`); the indexer publish path builds a Document-only candidate with no snapshot (`indexer.py:74-91`). `bind_schema_snapshot` (`schema_snapshot.py:1002`) and `run_snapshotter` (1085) have **no production caller** (tests only, `test_postgres_governance.py:862`, `test_schema_snapshot.py:561-575`). Consequence: `SemanticContextResolver.resolve` raises `semantic_release_schema_snapshot_unbound` (`context_compiler.py:189-190`).
6. **Registry is in-process; the persisted ACTIVE release is never registered.** `ContextCompiler.release()` calls `SemanticRegistry.get` (`context_compiler.py:150-153`) against the in-process dict state machine (`registry.py:117-135`), while `ControlSemanticReleasePublisher.read_active` (`registry.py:478-556`) returns a `SemanticRelease` that is **not inserted into any `SemanticRegistry`**, so `ContextCompiler.release()` would raise `SemanticReleaseError`. (Also recorded in `docs-v4-p4-s1-activation-recon.md:22`.)
7. **The persisted `schema_snapshot_checksum` is derived, not stored** — `semantic_releases` has no such column (`004_semantic_registry_v3.sql:41-46`); it is read via LEFT JOIN (`registry.py:488, 376`).
8. **`QueryReceipt` itself carries none of the binding** — release/snapshot/plan hashes live only on the compiler/executor side; enrichment is a field-name update in `metric_query.py:615-624`.
9. **The engine's request identity carries no revision** — the engine reads `configurable["request_identity"]` with no authorization revision (`docs-v4-p2-s1-contract-recon.md:62`).

#### PROPOSED binding for a sandbox job

A single immutable binding tuple, carried on `SandboxJobRequest` and echoed in `SandboxJobReceipt`: **`authorization_revision` + `policy_version` + `policy_checksum` + `semantic_release_id` + `schema_snapshot_id` + `schema_snapshot_checksum` + `query_plan_sha256` + `execution_plan_checksum` + `calculation_plan_checksum` + `runtime_config_checksum` + the ordered tuple of artifact `content_sha256`**. The executor re-derives and compares every hash before dispatch (same fail-closed style as `execution.py:343-358, 383-394`), and the receipt records the values, so a verifier never has to trust the caller.

### Q8 — How HITL approval is bound to the exact job/plan

#### What exists today

- `engine.py:764-815` `hitl_node` uses a LangGraph `interrupt({kind, version, actions, summary})` (`766-773`) whose payload contains **only** version, the action list, and the pending answer text — **no plan checksum, no release/snapshot, no budget, no artifact**.
- `ThreadActionRequest` (`v2.py:72-82`) = `action` + `idempotency_key` + `expected_version` + `feedback`; `v2.py:279-320` checks the thread is `awaiting_action` (306), checks `expected_version` (308-310), replays prior idempotency keys from `state.applied_actions` (293-305), and resumes with `Command(resume=...)` (311).
- In-graph idempotency: `applied_actions` keyed by `idempotency_key` (`engine.py:786-793`), status map (794-800), version increments (808-814).
- **Reachability gap (parent-verified):** the typed pipeline's `approval` outcome does **not** enter HITL. `compile_node` halts with `execution_plan_approval_required` and ends the graph (`engine.py:468-496`; `after_compile` `833-834` ENDs). HITL is reachable only from `model_node` on the deep, non-shadow route (`engine.py:748-759`; `after_model` 836-837; edge 868). **No compiled ExecutionPlan can ever be approved today.**

#### What is missing for a sandbox job

- Approval must bind to the exact immutable **plan_checksum + artifact checksums + runtime image digest + policy_version/policy_checksum + release/snapshot + authorization revision** — none of which is in the interrupt payload or the action contract.
- **Approver identity**: `ThreadActionRequest` carries no approver; `_request_identity` names the thread actor but there is no separation-of-duties ("approver ≠ requester") check and no approval-record contract — only the thread state's `applied_actions` string map.
- **Expiry / single-use semantics**, and job creation **only** from an approved, unexpired, hash-matching approval, so "approve plan A" cannot be replayed onto plan B or a new artifact set.
- The HITL thread is identity-bound (`ownership.py:10-14`, good), but the approval is not bound to a release/snapshot, so a release switch between approval and execution is invisible.

#### PROPOSED approval contract (none of this exists)

```python
# PROPOSED
class SandboxApproval(StrictContract):
    """One-shot, expiring approval of one exact immutable sandbox job definition."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    job_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=256)
    approver_id: str = Field(min_length=1, max_length=256)
    requester_id: str = Field(min_length=1, max_length=256)     # separation of duties: approver != requester
    authorization_revision: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_release_id: UUID
    schema_snapshot_id: UUID
    schema_snapshot_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    calculation_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_config_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_checksums: tuple[str, ...] = Field(min_length=1, max_length=16,
                                                description="ordered content_sha256 of input artifacts")
    approved_at: datetime
    expires_at: datetime                                        # single-use + expiring
    single_use: Literal[True] = True

    @model_validator(mode="after")
    def validate_approval(self) -> "SandboxApproval":
        if self.approver_id == self.requester_id:
            raise ValueError("approver must differ from requester")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval must expire after it is granted")
        return self
```

**Dispatch rule (PROPOSED):** the broker starts a sandbox job **iff** a `SandboxApproval` exists whose `approval_id` is unused, whose `expires_at` is in the future, whose `authorization_revision` equals the current revision, and whose entire hash tuple equals the job's. Anything else fails closed with a `denied` receipt — no partial match, no "close enough".

## ISOLATION

### Q10 — How credentials are physically excluded today

**Answer: they are NOT excluded. There is no physical credential boundary anywhere.**

- The sandbox worker is a **forked child of the API process**: `mp.Process(target=_sandbox_worker, ...)` (`process_sandbox.py:246-258`) with **no** `set_start_method`/`get_context`/`spawn`/`forkserver` anywhere in `src/` (grep = 0, parent-verified). Under Linux fork the child inherits the parent's entire address space, `os.environ`, open file descriptors, DB connections and secret objects.
- Credentials therefore present inside the worker: `DATABASE_URL`, `CONTROL_DATABASE_URL`, `CHECKPOINT_DATABASE_URL` (from env or `*_FILE` file-backed secrets — `src/core/settings.py:127-129`, `nl2sql/config/settings.py:33-40`, `src/core/secrets.py:15`); the business engine built in `infra/store/database.py:57-81`; model-provider keys (`infra/llm/gateway.py:557-559`; `SecretStr` in `config/settings.py:72-75`); the PostgreSQL checkpoint pool (`checkpointer.py:41-60`) and control audit pool (`observability/control_audit.py:38-73`) if already opened by the app. `api-a`/`api-b` additionally carry `TT_API_BASE_URL` for auth callbacks (`compose.prod.yml:7`) — another secret-adjacent value the sandbox must not see.
- What is actually withheld is only Python-level: the worker gets the `code` string and a **serialized `data_context`** (`process_sandbox.py:201, 317-334`), a restricted `__import__` (104-108) and a curated `safe_builtins` (110-122). That is a convenience allowlist inside an already-credentialed address space — not env scrubbing, not FD closing, not a namespace, not a different uid.
- No OS isolation primitives exist: 0 hits in `src/` for `seccomp`, `prctl`, `NO_NEW_PRIVS`, `unshare`, `chroot`, `setuid`, `drop_privileg`; 0 hits for network denial; `RLIMIT_*` only in `process_sandbox.py:54-63` and failures are swallowed (`:62`).
- The only real least-privilege controls are at the **deployment/DB layers**, not the sandbox: the prod image runs as non-root user `app` (`docker/Dockerfile:53-54, 66`) and DB roles are `NOSUPERUSER NOCREATEDB NOCREATEROLE` with `default_transaction_read_only=on` for the readonly privilege (`docker/initdb/roles.sh:17-37, 64-87`). None of this separates the worker from the API process, because they are the same address space / same uid / same env.

#### What is genuinely excluded from the application image (and is reusable proof)

- Secrets, `deploy/private` and all `.env*` stay out of the build context (`.dockerignore:1-4`) and are never `COPY`-ed even if present (`docker/Dockerfile:19-25, 57-64`).
- `scripts/` is never copied, so `scripts/p2/ssh_tunnel.py` (reads `P2_SSH_HOST/USER/PASSWORD`, uses paramiko — `scripts/p2/ssh_tunnel.py:5, 22-29, 49-61`) is **not in the application image**; paramiko is a dev-only dependency (`pyproject.toml:44-55`, line 47) absent from the dev-exported requirements.
- `pip` is removed from the image (`docker/Dockerfile:51`), removing the trivial in-container install path.
- Product API services receive DB/model secrets **only as files**: `*_FILE` env → `/run/secrets/...` (`compose.prod.yml:11-23`; `compose.release.yml:16-28`); the plaintext names are never set as env and this is asserted at `test_deployment_contracts.py:61-86`. `SecretProvider.get` rejects symlinks/non-regular files, rejects group/other-writable files on POSIX, rejects empty values (`src/core/secrets.py:17-35`) and recognizes read-only Docker secret mounts (`:38-45`).
- **The gap:** all of these credentials are mounted into the **same container** that hosts the in-process sandbox. The frozen requirement "sandbox worker has NO business/control/checkpoint DB credential and NO model-provider credential" is **not structurally satisfiable by hardening `ProcessSandbox`** — it requires a separate container whose env and secret list contain none of those names.

#### NEEDED (and cheap, because the mechanism exists)

A sandbox service with `secrets: []` and no `*_FILE` env, verifiable by the same exact-set assertion style as `test_deployment_contracts.py:79-86`, plus the container invariants: non-root uid, `no-new-privileges`, `cap_drop ALL`, read-only rootfs, network denied, bounded scratch, fixed image by digest. See NETWORK / FILESYSTEM / RESOURCE_CONTROLS and the open blocker in RESULT.

---

## NETWORK

### Q11 — How network is disabled / default-denied

#### TODAY

- The only denial primitive in use is Docker's internal bridge: `control_net internal: true` (`compose.base.yml:76-78`) and `data_net internal: true` (`compose.dev.yml:277-280`). An internal network has no external gateway, so containers on it cannot reach the internet or the host LAN.
- This is **not** default-deny for the APIs: `api-a`/`api-b` always also join `egress_net` (`compose.base.yml:27, 79-80`), a plain bridge with full outbound, and in prod additionally join the external business network (`compose.prod.yml:24-28`). They can reach every database and the internet simultaneously.
- There is **no `network_mode` anywhere** in any compose file. The tests only forbid the dangerous value `network_mode: host` (`test_deployment_contracts.py:93`). A service with `network_mode: none` would not violate any current assertion.
- Application-level egress control is nginx rate/connection limits (`nginx/nginx.conf:22-23, 61-62`) and QueryGateway concurrency (`compose.base.yml:13-15`). There is **no SSRF guard**. The sandbox process inherits the API network namespace, so arbitrary network is today blocked only by the AST/regex denylist in `process_sandbox.py:24-44` (imports `socket`/`http`/`urllib`/`requests`) — a **textual filter, not a kernel boundary**.

#### NEEDED

- The sandbox service must join a network with no route out. Two acceptable shapes:
  - **(a) `network_mode: none`** on the sandbox service — strongest, and explicitly allowed by the current test text.
  - **(b) a dedicated `sandbox_net` with `internal: true` and NO `egress_net` attachment** — but note `internal: true` still permits east-west traffic to any other container on that network, so the business/control/checkpoint DBs must **not** be attached to `sandbox_net`.
- The `egress_net` attachment must be **provably absent** for the sandbox service: `set(service['networks']) == {...}` assertion in the style of `test_deployment_contracts.py:32` — one line, exact precedent.
- Nothing today prevents a sandbox container from being handed a DB DSN via env; exclusion must be enforced by an explicit empty secret list plus a test enumerating it (`test_deployment_contracts.py:79-86`).
- **Tie to the open blocker:** the launch mechanism decides whether the launcher/runner process needs its own network reachability to start the job, and whether the sandbox service can be fully isolated from the control plane. Resolve the blocker before freezing the network shape.

---

## FILESYSTEM

### Q12 — How filesystem isolation is enforced

#### TODAY

- **Non-root:** the image creates a system user/group `app` (`Dockerfile:53-54`) and sets `USER app` (`:66`). The API process is root-free; compose api services do not set `user:`, so the image user applies. `migrate` overrides with `user 0:0` (`compose.dev.yml:123`).
- **Read-only root filesystem:** applied ONLY to ops containers, never the APIs — `migrate read_only: true` + `tmpfs /tmp` (`compose.dev.yml:124-126`; release `:152-154`) and `schema-snapshot` (`compose.dev.yml:158-160`; release `:187-189`). `api-a`/`api-b` have **no `read_only` and no `tmpfs`** in any overlay: they can write anywhere in the image layer and have no declared writable scratch.
- **Host filesystem exposure is significant in dev:** api containers bind-mount the host source and config trees read-only (`compose.dev.yml:8-10, 22-24`; `docker/docker-compose.yml:11-13`: `../src:/app/src:ro` and `../configs:/app/configs:ro`). That is a deliberate hot-reload choice, but if a sandbox ever shared that container the **no host filesystem** requirement is void.
- **Bounded scratch does not exist:** the only `tmpfs` declarations are the ops `/tmp` mounts above, and **no `size=`** is set on any of them, so even the ops tmpfs is unbounded (defaults to ~50% of RAM).
- Backup/restore use explicit host binds (`compose.release.yml:174-177` read-only for migrate; `:243-245` read-write for backup; `:267-270` read-only for restore-test) with the `read_only` flag pinned by `test_deployment_contracts.py:172-177`.
- Secret mounts are files under `/run/secrets` (`compose.prod.yml:17-23`), read-only by Docker default; `SecretProvider` additionally validates mode (`src/core/secrets.py:24-33`).
- **Legacy in-process sandbox filesystem guarantees: NONE.** `exec()` runs in a child with `RLIMIT_AS/CPU/NPROC` (`process_sandbox.py:54-63`), but the child shares the container filesystem namespace and runs as `app`; the only file barrier is the regex for `open(` plus builtins lacking `open`/`io` (`process_sandbox.py:42, 110-124`). Anything reachable without the literal token `open(` (for example `os` via a permitted module, or `pandas.read_*`) is a hole. **This is why the frozen requirements call `ProcessSandbox` explicitly not sufficient.**

#### NEEDED

- Sandbox worker: `read_only: true` root, a `tmpfs /work` with an explicit `size=` bound, a dedicated non-root uid, and **no host binds at all**. `DatasetArtifact`/`CalculationPlan` inputs and outputs cross the boundary by stdin/stdout or a one-shot sized tmpfs file — never by mounting project or host paths.
- The sandbox image must not contain `/app/src` or `/app/configs` (present via dev binds and via `Dockerfile:58-60` in the app image); the sandbox image is built separately.
- A test asserting the sandbox service has no bind mounts pointing outside its own tmpfs (pattern: `test_deployment_contracts.py:107-109` inspects service volumes exactly).

---

## RESOURCE_CONTROLS

### Q13 — Appropriate CPU / memory / PID / output bounds

#### What is configured TODAY (all application settings, not container limits)

| Knob | Value | Evidence |
|---|---|---|
| `sandbox_timeout_seconds` | default 30 (`ge=1`) — wall clock only | `config/settings.py:201-205` |
| `sandbox_max_memory_mb` | default 256 (`ge=32`) | `config/settings.py:206-210` |
| `sandbox_allowed_modules` | pandas, numpy, math, statistics, datetime, decimal, json, re, collections — **module-granular** | `config/settings.py:211-217` |
| `enable_dynamic_calc` | default False | `config/settings.py:197-200` |

Enforcement is POSIX-only in `process_sandbox.py:54-63`: `RLIMIT_CPU` (soft=hard=cpu_seconds), `RLIMIT_AS` (memory_mb), `RLIMIT_NPROC (0,0)`; the helper **silently no-ops** on `ImportError/ValueError/OSError` (`:62`), and the module docstring admits non-Linux degrades to thread isolation + timeout (`:7`). `RLIMIT_NPROC=0` (`:61`) is a crude non-policy bound that also affects threads on some kernels and is likely hostile to numpy/BLAS. There is **no wall-clock rlimit, no `RLIMIT_FSIZE`, no `RLIMIT_NOFILE`, no cgroup, and no output size bound**; stdout goes into an unbounded `io.StringIO` (`process_sandbox.py:129-132, 155`) and the result payload is put on an `mp.Queue` with no size check (`:151-156, 244`). The legacy in-process executor has only the settings-level timeout (`code_executor.py:84-88`) — no memory or PID bound at all.

**Docker/compose-level bounds: NONE** — grep over `docker/*.yml` for `mem_limit|cpus|pids_limit|deploy:|resources:|ulimits|shm_size|oom` = 0 matches (parent-verified). Container-level CPU/memory/PID bounds are unconfigured for the API and the databases too.

#### Good adjacent precedent (data path, not sandbox path)

`QueryGateway` constructor bounds — `timeout_seconds=30`, `plan_timeout_ms=3000`, `lock_timeout_ms=1000`, `idle_timeout_ms=10000`, `max_plan_cost=500000`, `max_plan_rows=100000`, `max_result_bytes=1000000`, with positivity validation (`query_gateway.py:485-518`), enforced at `:749-756` (cost/rows) and `:792-795` (encoded size) and pushed into the session at `:807-813`; `database.py:40-78`; `.env.example:30-35`; gateway concurrency `base.yml:13-15` asserted `<= 8` across replicas at `test_deployment_contracts.py:38-53`; semaphore `semaphore.py:49-76`.

#### Recommended concrete bounds (PROPOSED — precedent-based, values need product confirmation)

| Bound | Recommended | Precedent / rationale |
|---|---|---|
| wall clock | 30 s plan-level default, hard cap 120 s | matches `sandbox_timeout_seconds` default 30 (`settings.py:201-205`) and `ModelRequest.deadline_ms` `le=120_000` (`contracts.py:599`) |
| CPU | == wall clock (1 vCPU) | `RLIMIT_CPU` already set to timeout (`process_sandbox.py:58`) |
| memory | 512 MiB default, 1 GiB ceiling | existing default 256 (`settings.py:206-210`) is tight for pandas/ML; set both cgroup and `RLIMIT_AS` |
| PIDs | 16–32 (**not 0**) | `RLIMIT_NPROC=0` today (`process_sandbox.py:61`) breaks numpy/BLAS thread pools; a container `pids_limit` is the correct primitive and does not exist yet |
| open files | 64–256 | no bound today; cheap `RLIMIT_NOFILE` |
| file size / scratch | sized `tmpfs /work` (e.g. 256 MiB) via `size=` | no `size=` on any tmpfs today (`compose.dev.yml:126`) |
| stdout | 64 KiB hard truncate | no bound today; stdout is an unbounded `StringIO` (`process_sandbox.py:129`) |
| result payload | ≤ `NL2SQL_MAX_RESULT_BYTES` (1e6) for consistency | existing `max_result_bytes=1_000_000` (`query_gateway.py:492, 792-795`) |
| artifact count / input bytes | explicit caps, e.g. 8 artifacts / 32 MiB total | no precedent; must be chosen together with the `DatasetArtifact` contract (proposed 200k rows / 8 MiB — unconfirmed) |

#### Output bounds today

Structured/typed outputs are already bounded on the plan path: `PlanStepReceipt` allows at most 16 steps (`contracts.py:415`) and each receipt is digest-only (`374-375`); `PlanExecutionRecord` caps `step_receipts` at 16 and enforces `output_step_ids` equals the successful receipts (`415-434`); JSON outputs are normalized through a strict `JsonValue` TypeAdapter so non-JSON fails closed with `plan_step_output_not_json` (`execution.py:430-436`). **None of this applies to the CodeAct sandbox**, which returns whatever object the script assigned to `result` and only special-cases DataFrame/Series (`process_sandbox.py:134-156`). Resource-exhaustion test requirements are in TEST_GATES (Q19).







## RUNTIME_PACKAGES

### Q14 — Python / package runtime needed for numpy, pandas/polars, scipy, statsmodels, scikit-learn

#### Established runtime (already locked, no new work)

| Item | State | Evidence |
|---|---|---|
| Python | requires-python `>=3.13,<3.14`; uv lock `==3.13.*`; base image `python:3.13-slim` | `pyproject.toml:6`; `uv.lock:3`; `docker/Dockerfile:1, 28` |
| numpy | **2.5.1 locked and installed** | `uv.lock:1128-1129`; `.venv/Lib/site-packages/numpy-2.5.1.dist-info` |
| pandas | **3.0.5 locked and installed** | `uv.lock:1305-1306`; `.venv/Lib/site-packages/pandas` |
| transitive | python-dateutil 2.9.0.post0, tzdata 2026.3 | `uv.lock:1734, 2096` |
| declared direct deps | `pandas>=2.0.0`, `numpy>=1.26.0` | `pyproject.toml:35-36` |
| existing allowlist | `sandbox_allowed_modules` already names `numpy`, `pandas` (module-granular) | `config/settings.py:211-217` |

#### Genuinely new — absent from both `pyproject.toml` and `uv.lock`

`scipy`, `statsmodels`, `scikit-learn`, `polars`, `pyarrow`, `openpyxl`, `xlsxwriter`, `matplotlib`, `seaborn`, `sympy`, `numba` — **all 0 matches** in the lock (digest C §14.2). Repo-wide, `scipy|statsmodels|scikit|sklearn` appear only in a comment in `benchmarks/metrics.py:242, 267` ("simplified paired t-test that does not depend on scipy").

#### Recommendation (design)

- **P5-B needs nothing new.** It is deterministic JSON/numeric staging on the app side, and the existing decimal discipline already exists (`metric_query.py:782-789` uses `localcontext` prec=64 and `ROUND_HALF_UP`, never ambient context).
- **P5-C initial allowlist should be exactly `numpy` + `pandas`** — already locked, already the declared sandbox modules, zero new supply-chain surface. The statistics `scipy`/`statsmodels` would provide are already native SQL (`stddev`/`variance`/`percentile_cont`/`corr`/`covar_*`/`regr_*` — `docs-v4-p5:121-137`).
- **The sandbox image must be a separate fixed image pinned by digest — NOT the app venv/image**, because the app image contains `asyncpg`/`psycopg`/`sqlalchemy` (`pyproject.toml:9, 23, 24, 27`) and `langchain`/`openai`/`langfuse` (14-20). Shipping those into the sandbox would hand it DB-driver and model-provider client code, contradicting "NO business/control/checkpoint DB credential, NO model-provider credential". Pin the allowlist by a checksummed lock (`SandboxRuntimeConfig.package_allowlist_checksum`).
- The app container is **not** a P5-C worker and must not be reused as one: it joins `egress_net` (`compose.base.yml:24-27`) and has no `read_only`, no memory/CPU/PID limit, despite already having `no-new-privileges` + `cap_drop ALL` (`:28-31`).
- Versioning/installing packages is **not proposed now**; the allowlist above is the design boundary for a later slice (see IMPLEMENTATION_SLICES).

### Q16 — Deterministic seed / runtime versioning

#### Versioning discipline that already exists (the pattern to extend, not replace)

- DB-allocated monotonic release version: `semantic_release_version_seq`, `SELECT nextval(...)` (`registry.py:302-305`); sequence created in `docker/migrations/control/004_semantic_registry_v3.sql:7-25`.
- Content checksums: `SemanticReleaseCandidate.checksum` (`registry.py:79`); snapshot candidate checksum + `schema_checksum` recomputed and verified on read (`schema_snapshot.py:1051-1082, 1151-1165`).
- Parser/format versions: `SemanticRelease.parser_version` (`registry.py:106`); snapshot `parser_version` (`schema_snapshot.py:1063`) with drift detection (`:784-792`).
- Versioned + checksummed policies: `PlanValidator.policy_version`/`.policy_checksum` (`planning.py:70-88`); `RoutingBudgetPolicy.version` + `.checksum` (`contracts.py:487, 504-506`); `RoutePolicy.version` + `.checksum` (`contracts.py:448, 464-466`).
- Versioned model/prompt profile: `ModelReceipt.profile_version`/`profile_checksum`/`prompt_version`/`prompt_hash` (`contracts.py:628-631`); `ModelRequest.prompt_version` (`contracts.py:603`).
- Release manifest: `deploy/release-manifest.example.yaml` carries `git_revision`, `image_digest`, `compose_config_checksum`, schema revisions, `semantic_release_id`, prompt/model profiles, `model_capability_snapshot_checksum`, `policy_version`, feature flags, secret versions (lines 1-21). **There is no code reader for it** (grep `release_manifest|ReleaseManifest|image_digest` in `src/` = 0) — it is a deploy-time document.
- Image build provenance: `Dockerfile` `ARG VCS_REF/VERSION` → OCI labels (`docker/Dockerfile:30-33`).
- Deterministic hashing: canonical JSON, `sort_keys` + separators (`observability/trace.py:44-47`; `_contract_checksum` `contracts.py:647-655`).

#### What is missing

- **No seed concept anywhere**: 0 matches for `PYTHONHASHSEED`, `random_state`, or a `seed=` in `src/` (the only "seed" hits are graph-RAG keywords, `semantic/retrieval.py:82-83`, `infra/store/graph_rag.py`).
- **No runtime/image version is recorded on any execution artifact.**
- **No package-allowlist checksum is recorded anywhere.**

#### Proposal (PROPOSED)

1. Make `SandboxRuntimeConfig` the single versioned + checksummed runtime descriptor, mirroring `RoutingBudgetPolicy`'s version/state/checksum shape (`contracts.py:482-506`).
2. Require these to be **recorded, not inferred**, in the receipt: `runtime_id`, `runtime_version`, `image_digest`, `python_version`, `package_allowlist_checksum`, `random_seed`, `timezone`, `locale`.
3. Inject a deterministic environment: `PYTHONHASHSEED = random_seed`, `numpy` `default_rng(random_seed)`, single-thread BLAS/OpenMP env (e.g. `OMP_NUM_THREADS=1`) **declared inside `runtime_config`** so the checksum covers it, `TZ` from config, `LC_ALL` from config. If a computation is order/thread sensitive it must be **marked non-reproducible rather than silently accepted**.
4. Extend the release manifest with `sandbox_runtime_image: sha256:...` and `sandbox_package_allowlist_checksum: ...`, parallel to `model_capability_snapshot_checksum` (line 11) — keeping P5-C inside the existing release discipline rather than inventing a second one.
5. Bind seed/runtime to the plan: add `runtime_config_checksum` to the sandbox step and the receipt, and require the executor to reject a step whose `runtime_config_checksum` does not match the injected runtime (same fail-closed style as `execution.py:343-358`).

---

## ML_RUNTIME

### Q15 — Which ML packages should NOT be added yet, and why

**Do not add in this stage:** `scikit-learn`, `statsmodels`, `scipy`, `torch`, `tensorflow`, `jax`, `xgboost`, `lightgbm`, `prophet`, `transformers`, `numba`, `sympy`, `matplotlib`/`seaborn`, `polars`, `pyarrow`, and `openpyxl`-in-sandbox.

Reasons, each grounded in repo evidence:

1. **The DB already covers the statistical surface.** Core PostgreSQL provides every requested statistical / window / percentile / regression family with `EXTENSION_REQUIRED = NONE` (`docs-v4-p5:121-177, 170-177, 455-461`). The binding constraint is the compiler contract, not the math (`docs-v4-p5:457-461, 706-709`).
2. **No dynamic package installation is permitted**, and the frozen P5-C requirement is a fixed approved runtime image with a versioned allowlist. Every added package must be built into the image and re-checksummed.
3. **The data does not support ML yet.** At most ~90 contiguous daily buckets, at most 4 monthly buckets, a 2000-01-01 `CURRENT_STATE` sentinel, and seasonal decomposition is "not supportable" (`docs-v4-p5:470-519, 490-493, 506`). Adding sklearn/statsmodels now builds capability against data that cannot exercise it.
4. **Attack/latency surface.** These are large C-extension/native-binary packages (BLAS, LAPACK, OpenMP) that enlarge the sandbox image, pull native libs, and make read-only-rootfs / CPU / memory bounds and reproducible-version claims harder. The repo dependency posture is deliberately narrow.
5. **Determinism and receipt integrity.** scikit-learn's default estimators, threading and `random_state` semantics make exact replay hard; the project's existing receipts assume deterministic digests (`rowset_sha256` `candidates.py:121+`; `semantic_signature` `metric_query.py:455-461`).
6. **P5-C is "primarily a BUILD capability" and opt-in**; the residual app-side needs are enumerable and narrow (5 categories, `docs-v4-p5:713-722, 772-778`). Build the bounded runtime first; add a package only when a validated plan cannot express the computation.

---

## EXTERNAL_INPUT_PATH

### Q17 — How future CSV / XLSX / API inputs become a DatasetArtifact without granting the sandbox filesystem or network authority

#### What exists today

- **No upload, CSV, XLSX or connector surface exists.** Grep `upload|attachment|UploadFile|xlsx|read_csv|openpyxl` in `src/` = 0. The only router is `v2.py:210` (prefix `/api/v2/nl2sql`) exposing `POST /queries`, `POST /queries/stream`, `GET /threads/{id}`, `GET /threads/{id}/history`, `POST /threads/{id}/actions`, `POST /feedback`, `GET /capabilities` (`v2.py:212-335`). **No multipart route.**
- **`gen_data` performs no external fetching**: its only entry point is `query_database_with_gen_data_agent` (`gen_data/service.py:13`), with no `httpx`/`requests` usage.
- Governed-input patterns to reuse: `QueryGateway` is the single application boundary for business SQL (`query_gateway.py:1-7`; `PreparedQuery` 68-79; `QueryReceipt` 82-131); `MetricQueryCompiler` is the authorship boundary emitting parameterized SQL from typed contracts (`metric_query.py:267-466`); `PolicyScopedEvidenceProvider.retrieve_permitted` is the identity-scoped retrieval interface (`context_compiler.py:65-71`); `ai_views` is the exposure layer (`infra/store/ai_views.py`, `configs/semantic/ai_views.yaml`) — with known drift, `sync_ai_views_from_yaml` (`ai_views.py:570`) has zero callers.
- **The legacy codeact fetch is NOT a governed-input pattern.** `parallel_fetcher` generates SQL with an LLM (`parallel_fetcher.py:141, 185-202`), invokes the freeform `sql_db_query` tool (129, 146) and returns the result as a **string** (`FetchResult.data: str`, line 52) — no checksum, no schema descriptor, no artifact object. It must not be the model for `DatasetArtifact`.

#### Proposed ingestion architecture (PROPOSED — none of this exists)

**The rule: the sandbox never sees a path, a URL, a bucket key, a DSN, or a file handle.** It receives a `DatasetArtifact` descriptor plus a bounded out-of-band byte frame. All fetching, parsing, type coercion, profiling and secret scanning happen in a separate **`IngestionAuthority`** that runs **outside** the sandbox with its own narrowly-scoped credentials.

```python
# PROPOSED
IngestionSourceKind = Literal["upload_csv", "upload_xlsx", "approved_api"]

class IngestionRequest(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    source_kind: IngestionSourceKind
    authorization_revision: str = Field(min_length=1, max_length=256)
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # upload_* : opaque content-addressed handle minted by the API layer, never a filesystem path.
    upload_handle: UUID | None = None
    # approved_api : a registry key resolved by deployment config; never a client-supplied URL.
    connector_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    connector_request_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    declared_schema: ArtifactSchema | None = None
    max_rows: int = Field(default=200_000, ge=1, le=200_000)
    max_bytes: int = Field(default=8_388_608, ge=1, le=8_388_608)

    @model_validator(mode="after")
    def validate_source(self) -> "IngestionRequest":
        if self.source_kind in {"upload_csv", "upload_xlsx"} and self.upload_handle is None:
            raise ValueError("upload ingestion requires an opaque upload handle")
        if self.source_kind in {"upload_csv", "upload_xlsx"} and self.connector_id is not None:
            raise ValueError("upload ingestion cannot name a connector")
        if self.source_kind == "approved_api" and self.connector_id is None:
            raise ValueError("approved API ingestion requires a registered connector id")
        return self

class IngestionAuthority(Protocol):
    async def materialize(self, *, request: IngestionRequest, identity: RequestIdentity) -> "DatasetArtifact": ...
```

Flow:

1. The API layer accepts bytes (a new route, e.g. `POST /api/v2/nl2sql/datasets` — **adding this route is a product decision; it does not exist today**) and immediately stores them under a content-addressed opaque UUID handle. The client never supplies a path, and no path is ever handed to the sandbox.
2. `IngestionAuthority` (out-of-process, **not** the sandbox) validates the handle, enforces `max_rows`/`max_bytes`, parses CSV/XLSX/API JSON, coerces to the declared `ArtifactSchema` logical types, rejects unknown or over-broad columns, scans for secret-shaped columns (reuse the pattern source `_SENSITIVE_COLUMN`, `query_gateway.py:190-192`), then computes `content_sha256` over the canonical serialized rows frame.
3. It builds `DatasetArtifact` with `kind = upload_csv|upload_xlsx|approved_api` and `provenance_class = user_supplied|approved_external`, and **no** `semantic_release_id` / `query_plan_sha256` (enforced by the `DatasetArtifact` validator above).
4. The sandbox receives only: the `DatasetArtifact` descriptor, the bounded serialized frame (whose sha256 must equal `source.content_sha256`), the `CalculationPlan`, and the `SandboxRuntimeConfig`. The worker **re-verifies `content_sha256` before computing** and reports it back in the receipt.
5. **Network stays DENIED by default**: `approved_api` connectors are resolved from deployment config inside the trusted zone; the sandbox never performs the fetch. If a sandbox job ever needed network, it must be an explicit, separately-approved capability — not a `DatasetArtifact` source kind.
6. Gateway-produced artifacts (`kind = query_gateway`) are minted by the existing pipeline: the compiled metric path already yields `rowset_sha256`, `sql_fingerprint`, `data_as_of`, `freshness_status` and `source_*` (`metric_query.py:615-624`), so the `DatasetArtifact` for that path is a **straight projection, not new data access**.





## IMPLEMENTATION_SLICES

Slices are ordered by dependency. ~~Strikethrough~~ is not used; the **OPEN BLOCKER is S0** and gates S5-S7. S1-S4 touch only `src/` and unit tests and can proceed without any container decision, because the sandbox stays fail-closed until a runner is injected.

| Slice | Deliverable | Depends on | Gate / proof | Unblocks |
|---|---|---|---|---|
| **S0** | **Decision: container launch mechanism** — dedicated sandbox-runner service/endpoint **or** rootless runtime socket/shim. Written record of which component holds launch authority, its network attachments and its secret set. | nothing (product/architecture) | A signed-off decision document. **The recon cannot settle this.** | S5, S6, S7 |
| **S1** | Contracts only in `contracts.py`: `PlanStepKind` extension + `SandboxStep`; `SandboxRuntimeConfig`; `CalculationPlan`/`CalculationStep`; `DatasetArtifact`/`ArtifactSchema`/`ArtifactColumn`/`ArtifactSourceBinding`; `SandboxJobRequest`; `SandboxJobReceipt`; `SandboxApproval`. Reconcile the A-vs-C divergences first. | nothing | Pure Pydantic: strict/frozen, `extra="forbid"`, checksum stability, lifecycle validators, reject-unknown-field. **No behavior change; nothing executes.** | S2, S4 |
| **S2** | Registration into the kernel: new `SandboxJobRunner` Protocol (`execution.py:74-108` style), new `PlanExecutor.__init__` field (`:164-174`), new `_execute_step` branch (`:320-380`), `PlanValidator` rule (`planning.py:261-280`), `PlanStepReceipt.kind` member (`contracts.py:380`). | S1 | Unknown step still fails closed (`execution.py:380`); a **stub** runner's receipt maps into `PlanStepReceipt`; new call sites registered in `tests/unit/test_query_gateway.py:562-596`. | S3, S6 |
| **S3** | Wire the **dormant typed pipeline** so a sandbox step can ever execute: production `QueryPlanProvider` implementation, plus `context_resolver`/`plan_executor` wiring in `container.get_engine` (`container.py:104-124`). `SemanticContextResolver` already exists (`context_compiler.py:156`); `metric_plan_executor()` exists (`metric_query.py:633-635`). | S2 | `typed_pipeline_enabled` becomes True in a test (`engine.py:95-118`); `context_node`/`plan_node`/`validate_node` stop early-returning. **This is a separate missing component (parent fact 4), not sandbox work.** | S4, S6 |
| **S4** | Fix plan-approval reachability and bind approvals: `compile_node` currently halts with no interrupt (`engine.py:468-497`); route the `approval` outcome into the HITL path and carry the immutable binding tuple; persist `SandboxApproval` with expiry, single-use and separation of duties. | S1 (contract), S3 (pipeline) | An approved compiled plan can execute; stale/expired/cross-owner/cross-plan cannot (TEST_GATES Q20). | S6 |
| **S5** | Deployment substrate: sandbox image (numpy+pandas only, pinned by digest, built separately from the app image), one compose service (`secrets: []`, no egress network, `read_only`, sized tmpfs, non-root uid, `cap_drop ALL`, `no-new-privileges`, `mem_limit`/`cpus`/`pids_limit`), one no-egress network. | **S0** | New compose-contract test **after** closing gap G13 (`test_deployment_contracts.py:88-95`); does not exist yet. | S6 |
| **S6** | Runner/broker implementation: env-scrubbed dispatch (explicit start method; no inherited env/FDs), artifact materialization, hard kill on deadline/cancel, idempotency job ledger with at-most-once dispatch, `SandboxJobReceipt` mapping, cancel path. | S2, S4, S5 | TEST_GATES Q18/Q19/Q20. | S7 |
| **S7** | Artifact production paths: gateway projection (P5-A, `metric_query.py:615-624`) and `IngestionAuthority` for CSV/XLSX/API (`POST /datasets` route is a product decision — not present today). | S6, product decision on upload | Ingestion tests: caps, secret-column scan, `content_sha256` re-verification in the worker. | S8 |
| **S8** | ML enablement — **DEFERRED, not now**. Add a package only when a validated plan cannot express the computation; rebuild the image, re-checksum the allowlist, update the release manifest. | a concrete unmet computation | Why-not list in ML_RUNTIME. | — |

---

## TEST_GATES

### What already exists (and what it does or does not prove)

| Existing test | Proves | Does NOT prove |
|---|---|---|
| `tests/unit/test_trusted_calc_templates.py:30-68` | Both executors **fail closed** outside `unsafe-dev`; the trusted-template path never executes generated code | Anything about runtime containment |
| `tests/unit/test_process_sandbox.py:1-67` | A regex/AST denylist matches some strings (static only; header says "no subprocess execution in CI") | No process is spawned, no network attempted, no DB contacted, no resource bound |
| `tests/unit/test_deployment_contracts.py:25-257` | Compose **shape**: no `/var/run/docker.sock`, no `privileged`, no `network_mode: host`, no `ssh_*` (base+prod only, `:91-95`); secrets are `/run/secrets` files and names match exactly (`:61-86`); `control_net internal:true` (`:34`); ops `read_only`/`cap_drop` (`:158-161, 199-201`) | Runtime isolation; **and it never reads `compose.dev.yml` or `compose.release.yml`** |
| `tests/integration/test_postgres_governance.py:340-371, 409-429` | The **only** place hardened containers are launched; the business app role is physically read-only | Does not test a sandbox |
| `tests/unit/test_query_gateway.py:562-596` | Static allowlist of high-risk call sites; line 584 already registers `codeact_engine/graph.py: {sandbox.execute}` | — (free regression guard for the new executor) |
| `tests/unit/test_query_gateway_capacity.py:151-193`; `test_metric_query.py:250-275`; `test_plan_pipeline.py:619-646` | Capacity/cancellation leak-freedom and cancellation propagation | Nothing sandbox-specific |
| `tests/unit/test_hitl_actions.py:53-88, 127-160` | Idempotency, version staleness (409), owner isolation (404), reject/cancel terminal | That a sandbox runner was never invoked |

### MANDATORY prerequisite gate — close the compose test-coverage gap FIRST (G13)

`tests/unit/test_deployment_contracts.py:88-95` concatenates **only** `docker/compose.base.yml` and `docker/compose.prod.yml`. A sandbox service added to `docker/compose.dev.yml` or `docker/compose.release.yml` could legally use `/var/run/docker.sock`, `privileged:` or `network_mode: host` **without failing that test**. **Extend the covered file set to every compose file (including dev and release) before relying on that assertion for the sandbox service.** This is a test-coverage fix, not a sandbox feature.

### Q18 — Tests proving the sandbox cannot reach business DB / control DB / checkpoint DB / model-provider secrets / arbitrary network / host filesystem

All fit the existing stack (pytest, pytest-asyncio, pytest-timeout, and the docker-CLI harness pattern in `tests/integration/test_postgres_governance.py:57-135`).

1. **Negative-connect per database:** run the sandbox executor with a plan whose payload contains a well-formed DSN for business/control/checkpoint, and assert the executor never attempts a connection (no `psycopg`/`asyncpg` import permitted **and** absent from the image) and that the runner's environment contains none of `DATABASE_URL`, `CONTROL_DATABASE_URL`, `CHECKPOINT_DATABASE_URL` nor their `*_FILE` peers.
2. **Structural service-definition test:** enumerate the sandbox service's `secrets` and env and assert the set is exactly empty / a fixed allowlist — mirroring `test_deployment_contracts.py:79-86`. Catches credential leakage at deploy time.
3. **Live reachability (integration, opt-in marker like `postgres_integration`):** start the sandbox worker in the same compose stack as the DBs, submit a job that attempts a TCP connection to control-postgres / checkpoint-postgres / the business DSN host, and assert connection failure/timeout. **This is the actual proof**; (1) and (2) are the cheap always-on guards.
4. **Model-provider secret scan:** assert the worker's `/run/secrets` is empty and that `DEEPSEEK_API_KEY` / `NVIDIA_API_KEY` / `EMBEDDING_API_KEY` resolve to nothing — reuse `SecretProvider` (`src/core/secrets.py:17-35`) inside the sandbox image and expect failure/empty.
5. **Provider egress:** an attempted outbound call to the provider base URLs must fail.
6. **No arbitrary network:** `network_mode: none` (or exact network-set assertion, mirroring `test_deployment_contracts.py:32`) **plus** a runtime DNS/connect test against a host that resolves on the host but not from the sandbox.
7. **Egress disjointness:** assert the sandbox service's network set is disjoint from `{egress_net, business_external_net, control_net, data_net}` — a one-line assertion with exact precedent.
8. **Read-only root:** a job that opens `/` and `/app/src/...` for write must get `OSError`.
9. **Bounded scratch:** a job that writes beyond the `tmpfs size=` bound must fail with a clean ENOSPC-shaped error, not a host write.
10. **Bind-mount absence:** assert the sandbox service's `volumes` list is empty except its own tmpfs (patterns `test_deployment_contracts.py:107-109, 170-177`).
11. **Host-path probe:** assert `/app/configs` and `/app/src` are **not present** in the sandbox image at all (they are dev bind mounts today, `compose.dev.yml:9-10`, and app-image copies at `Dockerfile:58-60`).

### Q19 — Tests proving resource exhaustion is bounded

**Present today for the sandbox: none.** No test exercises `RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_NPROC`; no test asserts a Docker `mem_limit`/`cpus`/`pids_limit` because none are configured; `test_process_sandbox.py:1` deliberately states there is no subprocess execution in CI.

1. **Memory:** submit `bytearray(2**31)`; assert the worker fails with a MemoryError/rlimit-shaped error and the parent survives with bounded RSS.
2. **CPU / wall clock:** submit an infinite loop; assert a timeout receipt within `timeout + epsilon` and that the child PID is gone afterwards (`process_sandbox.py:223-229, 271-274` is the failed precedent).
3. **PIDs / fork bomb:** assert a process-spawning attempt produces a clean failure. `RLIMIT_NPROC=0` (`process_sandbox.py:61`) makes this theoretically true on Linux but **no test proves it and no container `pids_limit` exists**.
4. **File-descriptor exhaustion:** open in a loop via an allowed module; assert a clean failure.
5. **Output flood:** print a huge string; assert stdout is truncated to the configured cap and the receipt records the truncation. **Today stdout is unbounded (`process_sandbox.py:129`) — this test would currently FAIL, which is the point.**
6. **Disk:** write until the `tmpfs size=` is exhausted; assert ENOSPC, not a host write.
7. **Container-level:** assert `docker inspect` reports non-zero Memory / PidsLimit / CpuQuota for the sandbox service (requires the limits to exist first).
8. **Concurrency:** N simultaneous sandbox jobs must not exceed a global cap; assert with the capacity-leak pattern of `test_query_gateway_capacity.py:151-193` (`semaphore.py` already provides `CapacityExceededError` with `queue_full`/`wait_timeout`).

### Q20 — Tests proving reject / cancel / stale-HITL-approval cannot execute

What is **not** proven today: no test asserts a rejected, cancelled or stale-approved sandbox step never reaches the executor. It is currently vacuously true because no sandbox is wired into PlanExecutor (`execution.py:320-380` has no sandbox branch and raises at `:380`) and because both sandbox classes fail closed outside `unsafe-dev` (`test_trusted_calc_templates.py:56-68`). No test states the invariant as a property of the sandbox capability itself.

1. **Reject / cancel cannot execute:** drive the graph to `awaiting_action`, resume with `reject` (and separately `cancel`), and assert a counting/spy sandbox runner was **never invoked**, the container was never started, and no sandbox plan-step receipt exists. Pattern: the spy used by `_CountingProvider` in `test_plan_pipeline.py:656`.
2. **Stale approval cannot execute:** resume with `expected_version` behind current; assert 409 (already covered for HTTP at `test_hitl_actions.py:142-152`) **plus** assert the sandbox spy count is still 0 after the 409 — the current test only checks the HTTP status.
3. **Cross-owner cannot execute:** owner isolation returns 404 (`test_hitl_actions.py:154-160`); add the assertion that the sandbox runner was not invoked for the other owner's attempt.
4. **Idempotent replay cannot execute twice:** replay with the same `idempotency_key` returns `idempotent=true` and `engine.calls` stays 1 (`test_hitl_actions.py:147-151`); extend to assert the sandbox spy count is 1, not 2.
5. **Approval of one plan cannot execute a different plan:** after approve, assert the execution-plan checksum used equals the approved checksum (`PlanExecutionRecord.execution_plan_checksum`, `contracts.py:413`; mismatch guards already exist at `execution.py:191-194` and `planning.py:208-231`).
6. **Revoked / expired approval:** a version bump between plan display and action must invalidate execution (`engine.py:783-784` already implements this); add a test where the checkpoint version advances before submit, plus tests for `expires_at` lapse in `SandboxApproval` and for an `authorization_revision` change between approval and dispatch.

### The single most valuable missing test

A **compose-contract unit test asserting the sandbox service exists and satisfies every frozen invariant** — no secrets, no egress networks, `read_only`, `cap_drop ALL`, `no-new-privileges`, non-root uid, `pids_limit`, `mem_limit`, sized `tmpfs`, image pinned by digest — **plus a spy-based assertion that no sandbox runner is ever invoked without a valid, unexpired, hash-matching approval**. It is fast, runs in CI without Docker, and fails loudly the moment anyone weakens the sandbox. It belongs next to `tests/unit/test_deployment_contracts.py`.

## TRUE_PRODUCT_DECISIONS_REMAINING

These are decisions the recon **cannot** make. They require product/architecture ownership and, where noted, they block implementation.

| # | Decision | Why it blocks | Recon position / note |
|---|---|---|---|
| D1 | **Container launch mechanism** — dedicated sandbox-runner service/endpoint vs rootless runtime socket | **OPEN BLOCKER.** Determines the entire network and credential story, and therefore the deployment shape and the isolation tests | The recon cannot settle it. `docker.sock` is contract-forbidden (`test_deployment_contracts.py:91-95`) and no docker SDK usage exists in `src/`. Resolve before S5-S7. |
| D2 | Sandbox network shape: `network_mode: none` vs a dedicated `internal: true` sandbox network | Determines east-west reachability and whether any DB can share the sandbox network | `none` is strongest and allowed by current tests; `internal: true` still permits east-west, so DBs must not attach. |
| D3 | Sandbox resource defaults: memory (256 MiB existing vs 512 MiB proposed), PIDs (16-32), wall-clock default/cap (30 s / 120 s), scratch size (e.g. 256 MiB) | Values become checksummed contract fields; changing them later changes the runtime checksum | Recommended table in RESOURCE_CONTROLS; existing 256 MiB (`settings.py:206-210`) is tight for pandas/ML. |
| D4 | `DatasetArtifact` caps: 200k rows / 8 MiB, artifact count (≤16) and total input bytes (e.g. 8 artifacts / 32 MiB) | Caps are contract fields; they define what can ever cross the boundary | Proposed by digest C, **not confirmed**; the gateway envelope is 200 rows / 1e6 bytes (`query_gateway.py:489-492`). |
| D5 | Whether a public upload/dataset route ships at all (`POST /api/v2/nl2sql/datasets`) | A new public surface, new trust boundary, new secrets scanning requirement | Does not exist today; the ingestion design (EXTERNAL_INPUT_PATH) is contingent on it. |
| D6 | Authorization-revision model: what computes the revision, is it an int (revive `auth_epoch`) or an opaque string, and where is it read | Every binding (Q7) and every HITL/approval check (Q8) depends on it | `auth_epoch` is declared but never populated; no `AuthorizationContext` exists (`docs-v4-p2-s1-contract-recon.md:79-81` is a precedent proposal). |
| D7 | Approver identity, separation of duties, expiry duration, single-use enforcement location | Required for `SandboxApproval` to be meaningful | No approver, SoD check, or approval-record contract exists today. |
| D8 | Artifact retention: are result artifacts persisted, and for how long | Determines whether a store/table is needed beyond descriptors | The contract rule is descriptors-are-safe, payload travels out of band; retention is undefined. |
| D9 | Whether P5-C is opt-in per tenant/release and how it is gated (successor to `enable_dynamic_calc`) | Determines default-off posture and rollout | Current gates `enable_dynamic_calc=False` (`settings.py:197-200`) and `codeact_mode` default `"disabled"` (`:28-31`). |
| D10 | Sandbox image ownership, rebuild cadence, who signs the digest, how the allowlist changes | The image digest and allowlist checksum are contract fields | Release manifest already carries `image_digest`/`compose_config_checksum` (`deploy/release-manifest.example.yaml:4-5`) and has **no code reader**. |
| D11 | ML roadmap trigger: the concrete validated plan that cannot be expressed before any ML package is added | Prevents capability-first package bloat | ML_RUNTIME lists the packages to defer and six evidence-grounded reasons. |
| D12 | Whether a sandbox result feeds back into P5-A/P5-B or is terminal | Determines receipt/artifact chaining and the DAG shape | Not addressed by any digest. |
| D13 | Apply the shipped read-only role logic to remote `agent_reader`/`agent_reader_user` roles | Least-privilege completeness; low risk, ops task | `roles.sh:64-91` already implements it; only application is missing (G14). |

### Genuinely unresolved by the three digests

- **A-vs-C contract divergences** (step kind literal, receipt name and checksum fields, auth-revision type, size units, artifact richness) — must be reconciled by whoever owns the contract freeze; the mapping table is in SANDBOX_JOB_CONTRACT.
- **How a container is launched without `docker.sock`** — no digest proposes a concrete runner component design, only the two shapes (D1).
- **Sequencing of S3**: whether the dormant typed pipeline (missing production `QueryPlanProvider`, parent fact 4) is wired before or as part of the sandbox work is a program decision.
- **Registry/active-release integration gap** (digest C §7.2(6)): `read_active` returns a release that is never inserted into `SemanticRegistry`, so `ContextCompiler.release()` would raise; no owner or fix is proposed in any digest.
- **Schema-snapshot binding gap** (digest C §7.2(5)): a release can be ACTIVE with a NULL snapshot while `bind_schema_snapshot` has no production caller; no owner identified.
- **Exact authorization-revision semantics** are not defined anywhere in the repo; only the field name `auth_epoch` exists.
- **`ai_views` sync drift**: `sync_ai_views_from_yaml` (`ai_views.py:570`) has zero callers; the legacy auto-sync is lost.
- **No precedent for sandbox-specific resource values** — the recommended bounds are extrapolated from gateway/model limits, not measured.

---

## APPENDIX — Question-to-section map (Q1-Q20)

| # | Review question | Answered in | Primary evidence |
|---|---|---|---|
| 1 | Reusable code | REUSE (R1-R19) | `execution.py:1-6, 74-108, 164-174, 320-380`; `contracts.py:28-31, 374-440, 547-568, 647-655`; `metric_query.py:550-635`; `query_gateway.py:68-131` |
| 2 | Sandbox code to retire | RETIRE (T1-T5) | `code_executor.py:19-184`; `process_sandbox.py:24-334`; `codeact_engine/graph.py:211-224, 452-523`; `dynamic_calc/graph.py:193-267` |
| 3 | Process-per-job vs long-lived worker vs isolated container | TARGET_TOPOLOGY (Q3 matrix + 5 constraints) | `process_sandbox.py:246-258`; `checkpointer.py:44-55`; `database.py:39-81`; `ownership.py:10-14`; `main.py:109-125` |
| 4 | Exact `SandboxJob` contract | SANDBOX_JOB_CONTRACT (Q4, proposed code + divergence table) | 0 hits today; style authority `contracts.py:28-31, 281-284, 380, 647-655` |
| 5 | `DatasetArtifact` minimum contract | DATASET_ARTIFACT_MINIMUM (Q5) | `contracts.py:547-568`; `schema_snapshot.py:162, 191`; `query_gateway.py:489-492` |
| 6 | Execution receipt / provenance | SANDBOX_JOB_CONTRACT Q6 (five-item mapping) | `contracts.py:374-440, 547-568, 618-637`; `query_gateway.py:82-100`; `metric_query.py:615-624` |
| 7 | auth revision / semantic release / schema snapshot / plan hash binding | AUTH_HITL_BINDING Q7 | `contracts.py:39, 128-129, 206-208, 292-296, 353-371, 558-568`; `execution.py:383-394`; `planning.py:208-231, 309-335`; `metric_query.py:231-240, 568-580` |
| 8 | HITL approval bound to the exact job/plan | AUTH_HITL_BINDING Q8 (proposed `SandboxApproval`) | `engine.py:468-497, 764-815, 826-868`; `v2.py:72-90, 279-320` |
| 9 | Timeout / cancellation / idempotency | SANDBOX_JOB_CONTRACT Q9 | `execution.py:195-219, 232-233, 293-308`; `budget.py:23-53, 159-221`; `engine.py:972-1003`; `v2.py:293-305`; `engine.py:786-793` |
| 10 | Credentials physically excluded | ISOLATION Q10 (answer: not excluded) | `process_sandbox.py:246-258, 201, 317-334`; `core/secrets.py:17-45`; `compose.prod.yml:11-23`; `.dockerignore:1-4`; `Dockerfile:19-25, 51, 57-64` |
| 11 | Network disabled / default-denied | NETWORK Q11 | `compose.base.yml:24-31, 76-80`; `compose.dev.yml:277-280`; `test_deployment_contracts.py:32, 93`; `process_sandbox.py:24-44` |
| 12 | Filesystem isolation | FILESYSTEM Q12 | `Dockerfile:53-54, 66`; `compose.dev.yml:8-10, 22-24, 124-126, 158-160`; `docker-compose.yml:11-13`; `test_deployment_contracts.py:107-109, 172-177` |
| 13 | CPU / memory / PID / output bounds | RESOURCE_CONTROLS Q13 (recommended table) | `settings.py:197-217`; `process_sandbox.py:54-63, 129-156`; `query_gateway.py:485-518, 749-756, 792-795`; `database.py:40-78`; `contracts.py:415`; `execution.py:430-436` |
| 14 | Python/package runtime for numpy, pandas/polars, scipy, statsmodels, scikit-learn | RUNTIME_PACKAGES Q14 | `pyproject.toml:6, 35-36`; `uv.lock:3, 1128-1129, 1305-1306`; `Dockerfile:1, 28`; `settings.py:211-217` |
| 15 | ML packages NOT to add yet | ML_RUNTIME Q15 (six reasons) | `docs-v4-p5:121-177, 455-519, 706-722`; `uv.lock` 0 matches |
| 16 | Deterministic seed / runtime versioning | RUNTIME_PACKAGES Q16 | `registry.py:79, 106, 302-305`; `schema_snapshot.py:1051-1082, 1151-1165`; `contracts.py:482-506, 618-637`; `trace.py:44-47`; `deploy/release-manifest.example.yaml:1-21` |
| 17 | CSV/XLSX/API inputs → DatasetArtifact without sandbox FS/network authority | EXTERNAL_INPUT_PATH Q17 (proposed `IngestionAuthority`) | `v2.py:210-335`; `query_gateway.py:1-7, 190-192`; `context_compiler.py:65-71`; `parallel_fetcher.py:52, 141, 185-202` |
| 18 | Tests proving no DB / secrets / network / host FS reachability | TEST_GATES Q18 (11 tests) + G13 prerequisite | `test_process_sandbox.py:1-67`; `test_deployment_contracts.py:32, 79-95, 107-109, 170-177`; `test_postgres_governance.py:340-371` |
| 19 | Tests proving resource exhaustion is bounded | TEST_GATES Q19 (8 tests) | `process_sandbox.py:54-63, 61, 129, 223-229, 271-274`; `test_query_gateway_capacity.py:151-193`; `semaphore.py:49-141` |
| 20 | Tests proving reject / cancel / stale-HITL-approval cannot execute | TEST_GATES Q20 (6 tests) | `execution.py:320-380`; `test_hitl_actions.py:53-88, 127-160`; `test_plan_pipeline.py:183, 583-601, 619-646, 656`; `contracts.py:413` |

### Parent-verified facts cross-reference

| Fact | Stated in |
|---|---|
| `auth_epoch` exactly one occurrence (`contracts.py:39`), never read/populated | RESULT, CURRENT_SANDBOX_GAPS G4, AUTH_HITL_BINDING Q7 |
| `set_start_method`/`get_context`/`spawn`/`forkserver` = 0 hits → default fork inheritance | RESULT, ISOLATION Q10, TARGET_TOPOLOGY |
| `engine.py:468-497` compile halt — a compiled plan can never be approved | RESULT, CURRENT_SANDBOX_GAPS G10, AUTH_HITL_BINDING Q8 |
| No production `QueryPlanProvider`; sandbox registration gated | RESULT, CURRENT_SANDBOX_GAPS G11, IMPLEMENTATION_SLICES S3 |
| No Docker-level resource limits in `docker/` | RESULT, CURRENT_SANDBOX_GAPS G1/G7, RESOURCE_CONTROLS Q13 |
| Compose contract test covers only base+prod; dev/release uncovered | RESULT, CURRENT_SANDBOX_GAPS G13, TEST_GATES prerequisite |
| Role readonly ships in `roles.sh:64-91`; only remote agent roles missing it | RESULT, CURRENT_SANDBOX_GAPS G14, TRUE_PRODUCT_DECISIONS D13 |

---

*End of recon. This document is read-only analysis and design boundary; no repo behavior was modified except the creation of this file.*
