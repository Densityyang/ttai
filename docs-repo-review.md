# 仓库完整 Review（细节保全版）

> 生成方式：静态分析（从 `main.py` 解析导入图 → 运行时可达性；再从 `tests/` 解析 → 测试可达性；逐文件提取模块 docstring 作为用途）。
> 排除：`.venv`、`node_modules`、`tt-intelligent-main/`（参考快照）、`secrets/`、`logs/`、`.opencode/`。

- 项目 Python 模块：**165** 个（运行时可达 **109**，仅测试 **29**，孤立 **27**）

## 一、活跃代码（产品运行时）

### 1.1 入口与 HTTP 面

| 文件 | 行数 | 用途 |
|---|---|---|
| `main.py` | 305 | tt-ai ?????? |
| `src/nl2sql/v2.py` | 922 | The v2 HTTP boundary: strict payload limits and identity-bound state access. |
| `src/nl2sql/api.py` | 197 | FastAPI lifecycle and response helpers shared by the v2 API boundary. |
| `src/nl2sql/container.py` | 985 | Application-scoped runtime dependencies; no request path relies on module globals. |
| `src/nl2sql/contracts.py` | 1212 | Versioned API and agent-boundary contracts for the NL2SQL service. |
| `src/nl2sql/ownership.py` | 159 | Identity-bound thread namespacing and the runtime configurable payload. |

入口命令：`main.py dev`（热重载）/ `main.py prod`（生产）/ `main.py cli`（交互式 Supervisor，遗留栈唯一入口）。

活跃 HTTP 接口：`/api/v2/nl2sql/queries`、`/queries/stream`、`/threads/{id}`、`/threads/{id}/history`、`/threads/{id}/actions`、`/feedback`、`/capabilities`；产品面 `/definitions`（10 个端点）、`/library`（11 个端点）、`/conflicts/personal`（2 个端点）；`/healthz`、`/readyz`；旧 `/nl2sql/*` 返回迁移提示。

### src/core/ 顶层文件

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `database.py` | 186 | 运行时 | Database runtime boundaries shared by business, control, and checkpoint stores. |
| `observer.py` | 121 | 运行时 | 全局可观测性工具 - 统一的监控封装（含 CodeAct 审计扩展） |
| `secrets.py` | 46 | 运行时 | Resolve configuration values from an environment variable or its ``_FILE`` peer. |
| `settings.py` | 323 | 运行时 | ?????? - ?????????? |

### src/core/auth/（5 文件 / 893 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `auth/demo_provider.py` | 82 | 运行时 | EXPLICIT demo-only synthetic authorization provider. |
| `auth/dependencies.py` | 278 | 运行时 | FastAPI 鉴权依赖。 |
| `auth/local_real_provider.py` | 81 | 运行时 | SERVER-OWNED local real-data authority (LOCAL DEMO ONLY). |
| `auth/provider.py` | 396 | 运行时 | tt-api 认证信息 Provider。 |
| `auth/types.py` | 56 | 运行时 | 认证相关数据模型。 |

### src/nl2sql/ 顶层文件

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `__init__.py` | 2 | 仅测试 | NL2SQL 模块：使用 LangGraph SQL Agent 实现自然语言转 SQL 查询 |
| `api.py` | 197 | 运行时 | FastAPI lifecycle and response helpers shared by the v2 API boundary. |
| `cli.py` | 139 | 孤立 | NL2SQL 命令行工具 |
| `container.py` | 985 | 运行时 | Application-scoped runtime dependencies; no request path relies on module globals. |
| `contracts.py` | 1212 | 运行时 | Versioned API and agent-boundary contracts for the NL2SQL service. |
| `ownership.py` | 159 | 运行时 | Identity-bound thread namespacing and the runtime configurable payload. |
| `v2.py` | 922 | 运行时 | The v2 HTTP boundary: strict payload limits and identity-bound state access. |

### src/nl2sql/agents/（42 文件 / 6463 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `agents/__init__.py` | 1 | 孤立 |  |
| `agents/codeact_engine/__init__.py` | 2 | 仅测试 | CodeAct Engine -- HITL 确认 + 动态指标计算。 |
| `agents/codeact_engine/code_generator.py` | 158 | 运行时 | CodeAct 代码生成器 -- 基于 ConfirmedCalcPlan 生成可执行 Python 代码。 |
| `agents/codeact_engine/graph.py` | 538 | 运行时 | CodeAct Engine LangGraph 图 -- HITL 确认 + 动态指标计算编排。 |
| `agents/codeact_engine/hitl_protocol.py` | 246 | 运行时 | HITL 确认协议 -- 计算计划的人机协作确认流程。 |
| `agents/codeact_engine/parallel_fetcher.py` | 228 | 运行时 | 并行取数器 -- 根据 ConfirmedCalcPlan 的取数指令并行执行 SQL。 |
| `agents/codeact_engine/plan_card.py` | 157 | 运行时 | HITL 计算计划卡片与确认后计划的数据模型。 |
| `agents/codeact_engine/plan_ingestion.py` | 131 | 运行时 | 计划摄入模块 -- 校验并拆解 ConfirmedCalcPlan 为可执行指令。 |
| `agents/codeact_engine/process_sandbox.py` | 335 | 运行时 | 进程隔离沙箱 -- CodeAct 代码的安全执行环境。 |
| `agents/codeact_engine/prompts.py` | 102 | 运行时 | CodeAct Engine 提示词模板。 |
| `agents/codeact_engine/validator.py` | 229 | 运行时 | 结果验证器 -- 对照 ConfirmedCalcPlan 的 ValidationCriteria 校验代码执行结果。 |
| `agents/dynamic_calc/__init__.py` | 1 | 仅测试 |  |
| `agents/dynamic_calc/code_executor.py` | 185 | 运行时 | 受限 Python 代码执行器 -- CodeAct 范式的关键组件。 |
| `agents/dynamic_calc/graph.py` | 499 | 运行时 | 动态指标计算 Agent 图 -- CodeAct 核心链路。 |
| `agents/dynamic_calc/planner.py` | 32 | 运行时 | 动态指标计算 -- 计划生成器。 |
| `agents/dynamic_calc/prompts.py` | 61 | 运行时 | 动态指标计算提示词模板。 |
| `agents/dynamic_calc/schemas.py` | 74 | 运行时 | 动态指标计算数据契约。 |
| `agents/dynamic_calc/trusted_templates.py` | 150 | 运行时 | Approved, typed dynamic-calculation templates. |
| `agents/gen_data/__init__.py` | 10 | 孤立 | GenData Agent 包。 |
| `agents/gen_data/agent.py` | 98 | 运行时 | GenData create_agent 构建与获取。 |
| `agents/gen_data/middleware.py` | 102 | 孤立 | GenData Agent 中间件。 |
| `agents/gen_data/prompts.py` | 57 | 孤立 | GenData Agent 提示词模板。 |
| `agents/gen_data/schemas.py` | 51 | 孤立 | GenData Agent 结构化输出 schema。 |
| `agents/gen_data/service.py` | 37 | 孤立 | GenData Agent 查询服务。 |
| `agents/gen_data/tools.py` | 78 | 孤立 | GenData Agent 工具定义。 |
| `agents/nl2sql/__init__.py` | 1 | 孤立 |  |
| `agents/nl2sql/graph.py` | 108 | 运行时 | SQL Agent 图构建 |
| `agents/nl2sql/nodes.py` | 141 | 孤立 | SQL Agent 节点定义 |
| `agents/nl2sql/prompts.py` | 71 | 孤立 | SQL Agent 提示词模板 |
| `agents/nl2sql/service.py` | 43 | 仅测试 | SQL Agent 查询服务 |
| `agents/nl2sql/state.py` | 14 | 孤立 | SQL Agent 状态定义 |
| `agents/sql_agent/__init__.py` | 2 | 孤立 | 语义 SQL 实验 Agent。 |
| `agents/sql_agent/adaptive_router.py` | 204 | 仅测试 | 自适应 RAG 路由器 -- 基于查询复杂度自动选择检索策略。 |
| `agents/sql_agent/agentic_rag.py` | 697 | 仅测试 | Agentic RAG 预处理子图 -- Self-RAG / CRAG / Adaptive RAG 风格。 |
| `agents/sql_agent/experience_store.py` | 311 | 仅测试 | 经验记忆库 -- Memo-SQL 风格的成功查询/错误修复对存储。 |
| `agents/sql_agent/graph.py` | 152 | 运行时 | 语义 SQL Agent 图构建。 |
| `agents/sql_agent/hypothesis_verifier.py` | 251 | 仅测试 | 假设验证器 -- APEX-SQL 风格的数据画像。 |
| `agents/sql_agent/parallel_generator.py` | 297 | 仅测试 | 多策略并行 SQL 生成 + 锦标赛选优 -- Agentar-Scale-SQL 风格。 |
| `agents/sql_agent/prompts.py` | 33 | 孤立 | sql_agent 提示词模板。 |
| `agents/sql_agent/service.py` | 37 | 孤立 | 语义 SQL Agent 查询服务。 |
| `agents/sql_agent/sql_generator.py` | 506 | 孤立 | SQL Generator -- 显式 generate-execute-diagnose-repair 闭环。 |
| `agents/sql_agent/state.py` | 33 | 仅测试 | sql_agent 状态定义。 |

### src/nl2sql/artifacts/（15 文件 / 3711 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `artifacts/__init__.py` | 2 | 孤立 | Owner-scoped, DEMO/local artifact package (non-durable in Foundation B). |
| `artifacts/api_conflicts.py` | 245 | 运行时 | Owner-scoped personal semantic-conflict HTTP surface. |
| `artifacts/api_definitions.py` | 550 | 运行时 | Definition / Publication HTTP router (10 product endpoints). |
| `artifacts/api_library.py` | 454 | 运行时 | Library HTTP router (11 product endpoints). |
| `artifacts/build_run.py` | 81 | 运行时 | Server-owned BUILD run capability validation for semantic mutations. |
| `artifacts/contracts.py` | 91 | 运行时 | Artifact contracts: typed, owner-scoped, process-local (DEMO/local only). |
| `artifacts/custom_definition.py` | 208 | 运行时 | Custom Definition: four INDEPENDENT axes over a shared CalculationSpec. |
| `artifacts/custom_definition_execution_service.py` | 117 | 运行时 | Server-side execution seam for reusable Custom Definitions. |
| `artifacts/library.py` | 275 | 运行时 | Library repository: PERSONAL user state over the SHARED publication catalogue. |
| `artifacts/personal_conflict_product_service.py` | 255 | 运行时 | Server-owned personal semantic-conflict application service. |
| `artifacts/product_library_service.py` | 404 | 运行时 | Product Library orchestration: catalogue, personal library and authority. |
| `artifacts/publication.py` | 219 | 运行时 | Generic immutable PUBLICATION core (product path, not a fixture type). |
| `artifacts/publication_service.py` | 146 | 运行时 | Product publication service: Definition -> immutable published version. |
| `artifacts/repository.py` | 152 | 运行时 | Artifact repository: owner-scoped, fail-closed, DEMO/local and non-durable. |
| `artifacts/service.py` | 512 | 运行时 | Custom Definition service: the BUILD vertical flow (DEMO/local, non-durable). |

### src/nl2sql/config/（1 文件 / 291 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `config/settings.py` | 291 | 运行时 | nl2sql ?????? |

### src/nl2sql/demo/（3 文件 / 426 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `demo/__init__.py` | 2 | 孤立 | EXPLICIT demo-only runtime package.  Never selected in product mode. |
| `demo/fixtures.py` | 157 | 运行时 | EXPLICIT demo fixtures.  Synthetic values only - never business data. |
| `demo/runtime.py` | 267 | 运行时 | Demo request-scoped typed runtime: synthetic, deterministic, no database. |

### src/nl2sql/infra/（28 文件 / 6004 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `infra/__init__.py` | 1 | 孤立 |  |
| `infra/context/__init__.py` | 2 | 孤立 | Context Engineering 模块 -- Phase 4。 |
| `infra/context/compressor.py` | 334 | 仅测试 | 上下文压缩策略 -- Phase 4 Context Engineering。 |
| `infra/context/prompt_builder.py` | 142 | 运行时 | KV-Cache 友好的提示构建器 -- Phase 4 Context Engineering。 |
| `infra/governance/__init__.py` | 1 | 仅测试 |  |
| `infra/governance/query_gateway.py` | 1179 | 运行时 | The single application boundary for executing business SQL. |
| `infra/governance/semaphore.py` | 224 | 运行时 | 进程内并发控制与 QueryGateway 有界容量治理。 |
| `infra/governance/sql_guard.py` | 196 | 仅测试 | Compatibility helpers backed by the canonical :mod:`query_gateway` policy. |
| `infra/llm/__init__.py` | 1 | 仅测试 |  |
| `infra/llm/factory.py` | 57 | 运行时 | LLM 工厂 |
| `infra/llm/gateway.py` | 781 | 运行时 | The only v2 boundary for provider secrets and model network calls. |
| `infra/llm/model_input_policy.py` | 236 | 运行时 | Approved-destination and technical-secret gate applied before model egress. |
| `infra/llm/profiles.py` | 123 | 运行时 | Immutable, checksummed model profile contracts. |
| `infra/memory/__init__.py` | 1 | 孤立 |  |
| `infra/memory/checkpoint_migrate.py` | 39 | 孤立 | One-shot LangGraph checkpoint schema migration entry point. |
| `infra/memory/checkpointer.py` | 86 | 运行时 | Checkpointer ?? - ???????????????? |
| `infra/observer/__init__.py` | 1 | 仅测试 |  |
| `infra/observer/audit_trail.py` | 243 | 仅测试 | 全链路审计 -- Phase 4 安全强化。 |
| `infra/observer/langfuse.py` | 309 | 运行时 | Langfuse 可观测性集成（含 CodeAct 审计扩展） |
| `infra/runtime/__init__.py` | 2 | 孤立 | 运行时资源注册器包。 |
| `infra/runtime/registry.py` | 91 | 运行时 | 运行时单例注册器。 |
| `infra/store/__init__.py` | 1 | 孤立 |  |
| `infra/store/ai_views.py` | 669 | 运行时 | ai 视图 YAML 配置同步。 |
| `infra/store/database.py` | 291 | 运行时 | 数据库连接管理。 |
| `infra/store/graph_rag.py` | 288 | 运行时 | GraphRAG：基于 ai_views.yaml 的关系图增强检索。 |
| `infra/store/qa_rag.py` | 336 | 运行时 | QA RAG retriever and sync logic based on FAISS. |
| `infra/store/semantic_rag.py` | 320 | 仅测试 | Semantic RAG retriever：基于 FAISS 对 semantic.md 进行向量检索。 |
| `infra/store/sql_utils.py` | 50 | 运行时 | SQL 文本扫描工具。 |

### src/nl2sql/local_real/（5 文件 / 1283 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `local_real/__init__.py` | 2 | 仅测试 | LOCAL real-data deployment adapter (local demo profile only). |
| `local_real/deployment.py` | 196 | 运行时 | Local real-data deployment adapter (LOCAL DEMO ONLY). |
| `local_real/governed_inputs.py` | 183 | 运行时 | Local-real governed input adapter for reusable Definition execution. |
| `local_real/live_probe.py` | 363 | 运行时 | Bounded, read-only live probes for the local-real case (NOT the KPI path). |
| `local_real/semantics.py` | 539 | 运行时 | Local-real semantic release construction (LOCAL DEMO, in-memory only). |

### src/nl2sql/observability/（6 文件 / 761 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `observability/__init__.py` | 2 | 仅测试 | Typed, privacy-safe observability contracts and control-plane persistence. |
| `observability/content_policy.py` | 303 | 运行时 | Value-level technical-secret scanner and scrubber shared by every egress sink. |
| `observability/control_audit.py` | 148 | 运行时 | Control-plane audit/outbox writer. |
| `observability/secret_source.py` | 138 | 运行时 | Bounded deployment secret VALUES for observability egress scrubbing. |
| `observability/sink_policy.py` | 103 | 运行时 | Approved observability sink registry and metadata/content envelope. |
| `observability/trace.py` | 67 | 运行时 | Unified request trace schema shared by runtime and benchmarks. |

### src/nl2sql/orchestration/（20 文件 / 12122 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `orchestration/__init__.py` | 2 | 仅测试 | Explicit, budgeted NL2SQL v2 orchestration. |
| `orchestration/analysis_evidence.py` | 477 | 运行时 | Bounded ANALYZE evidence and model-input projection. |
| `orchestration/approved_compute.py` | 422 | 运行时 | Trusted canonical approved-computation binding (P4-S2 kernel). |
| `orchestration/budget.py` | 311 | 运行时 | Request deadline, route budgets, call accounting, and deterministic early stop. |
| `orchestration/candidates.py` | 288 | 运行时 | Deterministic candidate verification; only safe evidence leaves the request. |
| `orchestration/custom_calculation_execution.py` | 244 | 运行时 | Shared Calculation Runtime execution for one reusable Custom Definition. |
| `orchestration/decision_contract.py` | 751 | 运行时 | Typed clarification / decision / resume control-plane contracts. |
| `orchestration/deterministic_query_plan.py` | 981 | 运行时 | Deterministic, zero-model QueryPlan proposal for the typed QUERY path. |
| `orchestration/engine.py` | 3051 | 运行时 | The explicit LangGraph v2 request graph. |
| `orchestration/execution.py` | 628 | 运行时 | Bounded executor for registered typed plan steps. |
| `orchestration/governed_calculation_inputs.py` | 154 | 运行时 | Generic governed metric-input adapter for reusable calculations. |
| `orchestration/grounding.py` | 538 | 运行时 | Pure, deterministic grounded-answer construction from request evidence. |
| `orchestration/metric_query.py` | 1352 | 运行时 | Deterministic, release-bound aggregate queries through QueryGateway. |
| `orchestration/mode_contract.py` | 232 | 运行时 | Product-mode / run-envelope capability contract (contract-only, no I/O). |
| `orchestration/p4q_acceptance.py` | 806 | 仅测试 | P4-Q acceptance harness: a PURE evaluator over already-captured typed evidence. |
| `orchestration/planning.py` | 862 | 运行时 | Typed query-plan validation and deterministic execution-plan compilation. |
| `orchestration/routing.py` | 123 | 运行时 | Deterministic risk routing with replayable decisions. |
| `orchestration/run_lineage.py` | 70 | 运行时 | Identity-namespaced proof for the persisted current run of one thread. |
| `orchestration/shadow.py` | 15 | 运行时 | Deterministic, capped sampling for non-executing shadow plans. |
| `orchestration/typed_runtime.py` | 815 | 运行时 | Request-scoped typed runtime composed from the published legacy AI-view deployment. |

### src/nl2sql/semantic/（23 文件 / 11274 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `semantic/__init__.py` | 2 | 仅测试 | 语义层模块。 |
| `semantic/authoring.py` | 1392 | 运行时 | Schema-v3 semantic authoring contract and deterministic validation. |
| `semantic/authoritative_sources.py` | 143 | 运行时 | Authoritative in-repo binding for the V4 P1 metric inventory sources. |
| `semantic/calculation_contract.py` | 717 | 运行时 | Shared typed calculation semantic contract (contract-only slice). |
| `semantic/calculation_runtime.py` | 452 | 运行时 | Pure deterministic typed calculation evaluator (migration foundation). |
| `semantic/context_compiler.py` | 281 | 运行时 | Release-scoped retrieval fusion, evidence budgets and confidence. |
| `semantic/indexer.py` | 128 | 仅测试 | One-shot semantic release indexer; never invoked by API lifespan. |
| `semantic/inventory_release.py` | 903 | 仅测试 | P1 slice 4: identity-layer assessment, exact resolution, and a real subset candidate. |
| `semantic/materialization.py` | 515 | 运行时 | Deterministically materialize validated authoring IR into typed release rows. |
| `semantic/metric_contract.py` | 169 | 运行时 | YAML count/ratio contracts compiled into the existing semantic authoring IR. |
| `semantic/metric_inventory.py` | 1160 | 运行时 | V4 P1 metric inventory boundary. |
| `semantic/metric_layer.py` | 213 | 仅测试 | 指标语义层：指标匹配与指标结果查询。 |
| `semantic/metric_match.py` | 159 | 运行时 | Pure deterministic metric identity matching over one semantic release. |
| `semantic/models.py` | 77 | 孤立 | 语义层核心数据模型。 |
| `semantic/personal_conflict_contract.py` | 270 | 运行时 | Personal semantic-conflict contract (Agent-side, contract-only, no I/O). |
| `semantic/personal_conflict_service.py` | 459 | 运行时 | Pure personal-library conflict projection over actual semantic packages. |
| `semantic/planner_metric_projection.py` | 479 | 仅测试 | P1 Planner-safe projection of a verified metric inventory. |
| `semantic/policy_evidence.py` | 428 | 运行时 | Release-scoped, policy-scoped evidence retrieval for the ContextCompiler. |
| `semantic/published_reader.py` | 214 | 仅测试 | P3 Slice 2: the PublishedMetricReader contract/seam (read-only, evidence-typed). |
| `semantic/published_result.py` | 194 | 仅测试 | Immutable Agent-side contract for ONE authoritative Effective Published Metric Result. |
| `semantic/registry.py` | 950 | 运行时 | Immutable semantic release registry with an explicit active pointer. |
| `semantic/retrieval.py` | 95 | 仅测试 | Active-release lexical retrieval for API paths. |
| `semantic/schema_snapshot.py` | 1874 | 运行时 | Offline, bounded PostgreSQL schema snapshots for semantic release validation. |

### src/nl2sql/supervisor/（4 文件 / 742 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `supervisor/__init__.py` | 1 | 孤立 |  |
| `supervisor/agent.py` | 403 | 运行时 | Supervisor Agent - 协调无状态子 Agent |
| `supervisor/prompts.py` | 45 | 运行时 | Supervisor Agent 提示词模板（Phase 4: KV-Cache 友好重构）。 |
| `supervisor/schemas.py` | 293 | 运行时 | Supervisor and canonical public response-block contracts. |

### src/nl2sql/tools/（2 文件 / 74 行）

| 文件 | 行数 | 分类 | 用途 |
|---|---|---|---|
| `tools/__init__.py` | 1 | 孤立 |  |
| `tools/async_sql_tools.py` | 73 | 运行时 | Asynchronous database tools exposed to SQL-capable agents. |

<!-- 本文件由仓库静态盘点生成：入口导入图可达性 + 测试可达性 + 逐文件 docstring。 -->

## 目录

- [一、活跃代码（产品运行时）](#一活跃代码产品运行时)
- [二、中间过程 / 遗留栈（运行时不加载）](#二中间过程--遗留栈运行时不加载)
- [三、测试](#三测试111-文件--1644-用例--41431-行)
- [四、文档](#四文档19-份根目录-md约-540-kb)
- [五、脚本、配置与资产](#五脚本配置与资产)
- [六、未纳入 Git 的本机专属区域](#六未纳入-git-的本机专属区域)
- [七、已经完成的部分（按阶段归档，勿遗漏）](#七已经完成的部分按阶段归档勿遗漏)
- [八、尚未完成 / 已知缺口（不要误认为已完成）](#八尚未完成--已知缺口不要误认为已完成)
- [九、清理建议（按风险从低到高）](#九清理建议按风险从低到高)

## 二、中间过程 / 遗留栈（运行时不加载）

### 2.1 仅测试可达的模块（设计留档 / 早期切片）

| 模块 | 行数 | 说明 |
|---|---|---|
| `src/nl2sql/semantic/inventory_release.py` | 903 | P1 切片 4：指标清单身份评估与精确解析 |
| `src/nl2sql/orchestration/p4q_acceptance.py` | 806 | P4-Q 验收夹具（纯评估器，运行时不加载） |
| `src/nl2sql/agents/sql_agent/agentic_rag.py` | 697 | 旧 Agentic RAG 预处理子图（Self-RAG/CRAG/Adaptive） |
| `src/nl2sql/semantic/planner_metric_projection.py` | 479 | P1：Planner 安全投影 |
| `src/nl2sql/infra/context/compressor.py` | 334 | 上下文压缩策略（Phase 4） |
| `src/nl2sql/infra/store/semantic_rag.py` | 320 | 语义 RAG 检索（FAISS） |
| `src/nl2sql/agents/sql_agent/experience_store.py` | 311 | 经验记忆库（Memo-SQL 风格） |
| `src/nl2sql/agents/sql_agent/parallel_generator.py` | 297 | 多策略并行 SQL 生成 + 锦标赛 |
| `src/nl2sql/agents/sql_agent/hypothesis_verifier.py` | 251 | 假设验证器（APEX-SQL 风格画像） |
| `src/nl2sql/infra/observer/audit_trail.py` | 243 | 全链路审计（Phase 4） |
| `src/nl2sql/semantic/published_reader.py` | 214 | P3 Slice 2：PublishedMetricReader 契约/接缝 |
| `src/nl2sql/semantic/metric_layer.py` | 213 | 早期指标语义层（匹配 + 结果查询） |
| `src/nl2sql/agents/sql_agent/adaptive_router.py` | 204 | 自适应 RAG 路由 |
| `src/nl2sql/infra/governance/sql_guard.py` | 196 | 旧 SQL 守卫兼容门面 |
| `src/nl2sql/semantic/published_result.py` | 194 | P3 Slice 1：Effective Published Metric Result 契约 |
| `src/nl2sql/semantic/indexer.py` | 128 | 一次性语义索引器（lifespan 不调用） |
| `src/nl2sql/semantic/retrieval.py` | 95 | 活跃发布词法检索 |
| `src/nl2sql/agents/nl2sql/service.py` | 43 | 旧 nl2sql 子图服务 |
| `src/nl2sql/agents/sql_agent/state.py` | 33 | 旧 sql_agent 状态定义 |

其余为各包 `__init__.py`（2 行以内）。

### 2.2 完全孤立（无任何引用）

| 模块 | 行数 | 判断 |
|---|---|---|
| `src/nl2sql/agents/sql_agent/sql_generator.py` | 506 | 旧 generate-execute-diagnose-repair 闭环，已被现有路径取代 |
| `src/nl2sql/agents/nl2sql/nodes.py` | 141 | 旧 nl2sql 子图节点残件 |
| `src/nl2sql/cli.py` | 139 | 旧命令行入口，已被 `main.py cli` 取代 |
| `src/nl2sql/agents/gen_data/middleware.py` | 102 | GenData Agent 中间件（未接线） |
| `src/nl2sql/agents/gen_data/tools.py` | 78 | GenData 工具定义（未接线） |
| `src/nl2sql/semantic/models.py` | 77 | 早期语义数据模型（未使用） |
| `src/nl2sql/agents/nl2sql/prompts.py` | 71 | 旧提示词 |
| `src/nl2sql/agents/gen_data/prompts.py` | 57 | GenData 提示词 |
| `src/nl2sql/agents/gen_data/schemas.py` | 51 | GenData 结构化输出 schema |
| `src/nl2sql/infra/memory/checkpoint_migrate.py` | 39 | 一次性 checkpoint 迁移入口（仅被文档/脚本提及） |
| `src/nl2sql/agents/gen_data/service.py` | 37 | GenData 查询服务 |
| `src/nl2sql/agents/sql_agent/service.py` | 37 | 旧 sql_agent 服务 |
| `src/nl2sql/agents/sql_agent/prompts.py` | 33 | 旧提示词 |
| `src/nl2sql/agents/nl2sql/state.py` | 14 | 旧状态定义 |

### 2.3 遗留栈的真实接线

- `supervisor/`（742 行）：**只被 `main.py cli` 引用**；`main.py create_app` 只注册 v2 路由，HTTP 产品面不加载 Supervisor。
- `agents/`（42 文件 / 6,463 行）：其中 18 个经 `supervisor → infra/runtime/registry` 可达（即 CLI 路径），9 个仅测试，15 个孤立。
- `src/nl2sql/api.py` 内的 `_register_supervisor_routes` 属旧应用残留；`stream_blocks` 被 v2 流式路由复用（参数名为历史遗留）。

### 2.4 其他中间产物

- `benchmarks/`：runner、adapters、metrics、typed_receipts、agent_bridge、bird_eval + 数据集（BIRD dev 724 KB / SQL 266 KB / tables 155 KB；enterprise 三份 jsonl 用例）
- `specs/nl2sql_dynamic_metric_upgrade/`：spec/tasks/checklist + 企业基准数据设计 + 标注模板 + 训练模板（SFT/偏好对/工具轨迹/切分清单）
- `scripts/p2/`：三个一次性侦察脚本（组织联系人、库证据、隧道）
- 根目录：`_build_slide2.py`、`周例会ppt_928_yhx.pptx`、`.coverage`、`%SystemDrive%/`（未展开变量产生的垃圾目录）

## 三、测试（111 文件 / 1,644 用例 / 41,431 行）

### tests/unit（100 文件）

| 测试文件 | 用例 | 行数 | 覆盖主题 |
|---|---|---|---|
| `__init__.py` | 0 | 2 |  |
| `test_ad_hoc_carrier.py` | 40 | 1440 | QUERY AD_HOC noncanonical execution carrier (contracts + executor + grounding). |
| `test_ad_hoc_compiler.py` | 10 | 386 | PlanCompiler.compile_ad_hoc: pre-resolved noncanonical AD_HOC compilation. |
| `test_adaptive_router.py` | 11 | 79 | Tests for adaptive RAG router -- complexity detection and routing decisions. |
| `test_agent_bridge.py` | 13 | 56 | Tests for Phase 5 agent_bridge module. |
| `test_analysis_evidence.py` | 8 | 261 |  |
| `test_app_lifecycle.py` | 7 | 172 |  |
| `test_artifact_product_foundation.py` | 41 | 810 | Focused tests for the artifact / definition / library product foundation. |
| `test_audit_trail.py` | 15 | 103 | Tests for Phase 4 audit trail. |
| `test_authoritative_sources.py` | 13 | 151 | Focused P1-S0 tests for the authoritative in-repo source binding. |
| `test_authorization_contracts.py` | 32 | 383 | Pure unit tests for the P2-S1 authorization contract skeleton. |
| `test_authorization_wiring.py` | 22 | 400 | P2-S1 slice 2A: authorization across the typed runtime boundary. |
| `test_backend_freeze_contracts.py` | 3 | 219 |  |
| `test_benchmark_adapters.py` | 8 | 89 | Tests for Phase 5 benchmark dataset adapters. |
| `test_benchmark_metrics.py` | 32 | 226 | Tests for Phase 5 benchmark metrics computation. |
| `test_bird_eval.py` | 12 | 67 | Tests for Phase 5 BIRD evaluation helpers. |
| `test_calculation_runtime.py` | 45 | 1021 | Pure typed calculation evaluator: migration-foundation behaviour. |
| `test_calculation_semantic_contract.py` | 27 | 483 | Shared calculation semantic contract + shared ProductMode (contract-only slice). |
| `test_candidate_consensus.py` | 14 | 205 |  |
| `test_canonical_approved_compute.py` | 60 | 1338 | P4-S2 canonical approved-compute kernel contracts. |
| `test_checkpoint_scrub.py` | 3 | 46 | P2-S2 shared checkpoint scrubber. |
| `test_context_compressor.py` | 14 | 114 | Tests for Phase 4 context compression. |
| `test_cors_product_methods.py` | 3 | 70 | CORS must permit the product PATCH preflight a real browser sends. |
| `test_crag_routing.py` | 5 | 54 | Tests for CRAG three-tier confidence routing logic. |
| `test_custom_calculation_execution.py` | 6 | 215 |  |
| `test_database_boundaries.py` | 7 | 74 |  |
| `test_database_runtime.py` | 6 | 124 |  |
| `test_decision_contract.py` | 30 | 601 | Typed clarification / decision / resume contracts (contract-only slice). |
| `test_definition_execution_service.py` | 8 | 366 |  |
| `test_demo_e2e.py` | 8 | 346 | B-RT E2E: the real HTTP demo journey (A + B) and the zero-model proof. |
| `test_demo_isolation.py` | 8 | 116 | B3: demo synthetic authority can never be reached in product mode. |
| `test_demo_runtime.py` | 10 | 162 | B-RT subgate: demo runtime, factory selection and capability wire. |
| `test_deployment_contracts.py` | 9 | 316 |  |
| `test_deterministic_query_path.py` | 63 | 1545 | Deterministic QUERY plan authority: grammar, time boundary and engine exits. |
| `test_embedding_egress_policy.py` | 3 | 40 | P2-S2 embedding egress: technical-secret denial on the legacy consumer. |
| `test_experience_store.py` | 9 | 118 | Tests for experience store (Memo-SQL). |
| `test_graphrag_enhanced.py` | 7 | 97 | Tests for enhanced GraphRAG -- caching, early stop, keyword query. |
| `test_hitl_actions.py` | 2 | 161 |  |
| `test_hypothesis_verifier.py` | 4 | 73 | Tests for hypothesis verifier (static logic only). |
| `test_inventory_release.py` | 12 | 423 | Focused P1 slice 4 tests for the inventory release bridge. |
| `test_local_real_mode1.py` | 45 | 1105 | Focused tests for the local real-data Mode1 vertical (non-live). |
| `test_metric_dependency_graph.py` | 11 | 252 | Focused P1 tests for the canonical dependency graph and typed mappings. |
| `test_metric_inventory.py` | 9 | 257 | Focused P1 tests for the authoritative metric inventory boundary. |
| `test_metric_query.py` | 38 | 910 |  |
| `test_metric_ratio_query.py` | 27 | 582 | Slice 2 contracts: generated authority only, no enterprise access. |
| `test_metric_sources.py` | 20 | 439 |  |
| `test_mode2_analysis_integration.py` | 12 | 578 |  |
| `test_mode_contract.py` | 10 | 167 | Product-mode / run-envelope capability contract tests. |
| `test_mode_runtime_binding.py` | 9 | 124 | B2: ProductMode bound to the real runtime; QUERY is HARD zero-model. |
| `test_model_architecture.py` | 5 | 91 | Architecture gates for the PR06 v2 model boundary. |
| `test_model_gateway.py` | 28 | 842 |  |
| `test_model_input_policy.py` | 21 | 429 | P2-S2 ModelInputPolicy: destination, secret denial, checksum, readiness. |
| `test_observability_controls.py` | 6 | 137 |  |
| `test_observability_sink_policy.py` | 20 | 381 | P2-S2 approved-sink registry and metadata/content envelope. |
| `test_observer_egress_policy.py` | 30 | 713 | Iteration 3 R4/R5: observer/Langfuse egress policy and callback gating. |
| `test_orchestration_policy.py` | 8 | 183 |  |
| `test_organization_identity.py` | 9 | 143 | OrganizationIdentity: org data is CONTEXT, never authorization. |
| `test_p2s3_authorization_writer.py` | 14 | 363 | P2-S3 trusted Backend authorization carrier seam (new-run resolution only). |
| `test_p4q_acceptance.py` | 107 | 1576 | P4-Q acceptance harness contract tests (fixture/contract-level only). |
| `test_parallel_generator.py` | 6 | 84 | Tests for parallel generator tournament logic (no LLM calls). |
| `test_personal_conflict_contract.py` | 11 | 197 | Personal semantic-conflict contract tests. |
| `test_personal_conflict_http.py` | 6 | 307 |  |
| `test_personal_conflict_service.py` | 7 | 230 |  |
| `test_plan_card.py` | 3 | 112 | Tests for CalcPlanCard and ConfirmedCalcPlan data models. |
| `test_plan_ingestion.py` | 5 | 109 | Tests for plan_ingestion module. |
| `test_plan_pipeline.py` | 27 | 1538 |  |
| `test_planner_metric_projection.py` | 13 | 311 | Focused P1 tests for the Planner-safe metric projection. |
| `test_postgres_operations.py` | 5 | 95 |  |
| `test_process_sandbox.py` | 9 | 68 | Tests for process sandbox (static checks only, no subprocess execution in CI). |
| `test_product_http_journey.py` | 19 | 772 | HTTP product journey over ONE real app/container boundary. |
| `test_product_library_service.py` | 19 | 491 | Service-level vertical: shared catalogue, certification, withdrawal, fork. |
| `test_prompt_builder.py` | 11 | 81 | Tests for Phase 4 KV-Cache friendly prompt builder. |
| `test_public_block_contracts.py` | 5 | 146 |  |
| `test_published_reader.py` | 27 | 482 | P3 Slice 2: PublishedMetricReader contract/seam tests. |
| `test_published_result.py` | 28 | 394 | P3 Slice 1: immutable Effective Published Metric Result contract. |
| `test_query_gateway.py` | 23 | 701 | Contract tests for the process-local QueryGateway boundary. |
| `test_query_gateway_adapters.py` | 3 | 134 | Regression tests for production adapters that feed the QueryGateway. |
| `test_query_gateway_capacity.py` | 7 | 200 |  |
| `test_release_manifest.py` | 3 | 104 |  |
| `test_resume_authorization_refresh.py` | 4 | 188 | B1-FIX: the action/resume HTTP boundary re-fetches CURRENT authorization. |
| `test_runtime_safety_settings.py` | 5 | 52 | Runtime safety defaults required by the PR00 delivery baseline. |
| `test_s1c_activation_seam.py` | 16 | 818 | P4-S1c activation seam: request-scoped factory, guard, revision and receipt. |
| `test_schema_snapshot.py` | 18 | 797 | Offline SchemaSnapshot contracts and release binding invariants. |
| `test_secret_provider.py` | 10 | 105 |  |
| `test_semantic_authoring.py` | 9 | 242 | Schema-v3 semantic authoring compiler and validator tests. |
| `test_semantic_materialization.py` | 6 | 193 | Typed semantic release materialization contracts. |
| `test_semantic_registry.py` | 10 | 177 | Release and context compiler invariants. |
| `test_slot_reinjection.py` | 13 | 286 | Deterministic slot-bound replan capability (time/grain, user source only). |
| `test_sql_guard_enhanced.py` | 9 | 132 | Compatibility tests for the legacy SQL guard facade. |
| `test_sse_contract.py` | 3 | 129 |  |
| `test_trusted_calc_templates.py` | 3 | 69 |  |
| `test_typed_actions_api.py` | 4 | 263 |  |
| `test_typed_benchmark.py` | 4 | 125 |  |
| `test_typed_clarification_decision.py` | 18 | 760 | Typed clarification suspension -> decision -> cumulative slot-bound replan. |
| `test_typed_resume_authorization.py` | 6 | 114 | B1: typed resume -> CURRENT authorization -> execution. |
| `test_typed_runtime.py` | 22 | 651 | P4-S1b: typed deployment adapter and request-scoped typed runtime factory. |
| `test_v2_authorization.py` | 9 | 234 |  |
| `test_v2_contracts.py` | 9 | 254 |  |
| `test_v2_engine.py` | 4 | 202 |  |
| `test_validator.py` | 9 | 76 | Tests for result validator. |
| **小计** | **1487** | **34647** | |

### tests/acceptance（5 文件）

| 测试文件 | 用例 | 行数 | 覆盖主题 |
|---|---|---|---|
| `test_c_falsification_probes.py` | 5 | 137 | C falsification probes: sabotage the runtime and prove C tests would fail. |
| `test_c_independent_acceptance.py` | 38 | 1408 | Coding-C INDEPENDENT acceptance tests (first non-live pass). |
| `test_c_post_hardening_acceptance.py` | 48 | 1597 | Coding-C POST-HARDENING independent acceptance (sections A-G). |
| `test_c_post_hardening_falsification.py` | 7 | 126 | C post-hardening falsification probes. |
| `test_c_reacceptance.py` | 46 | 1538 | Coding-C final re-acceptance: PH-1 + D1 + D2 + D3 + D4. |
| **小计** | **144** | **4806** | |

### tests/integration（3 文件）

| 测试文件 | 用例 | 行数 | 覆盖主题 |
|---|---|---|---|
| `__init__.py` | 0 | 2 |  |
| `test_postgres_governance.py` | 1 | 944 |  |
| `test_query_gateway_postgres.py` | 12 | 793 | Real PostgreSQL contracts for the PR4 QueryGateway. |
| **小计** | **13** | **1739** | |

### tests/ 根与夹具

- `conftest.py`（27 行）：共享 fixture
- `metric_fixtures.py`（210 行）：单测与 Docker 契约共用的合成发布指标权威
- `fixtures/v4_p1/authoritative/canonical/*.yaml`：10 份权威指标源的**逐字节副本**（含 SHA-256 校验）
- `fixtures/v4_p1/authoritative/legacy/semantic.md`：旧语义声明副本


## 四、文档（19 份根目录 md，约 540 KB）

| 文档 | 大小 | 类别 | 内容 |
|---|---|---|---|
| `MASTER_PR_PLAN_V4.md` | 169 KB | 主计划 | 三模式/共享内核/统一评测的最终推进版；含 A1–A16 修订矩阵、P0–P10 实施卡、H19 backlog |
| `README.md` | 5 KB | 入口 | 能力概览、架构边界、API 表、本地开发、发布回滚、安全边界 |
| `docs-enterprise-db-contract.md` | 29 KB | 契约 | 企业数据库与指标执行契约 |
| `docs-semantic-authoring.md` | 2 KB | 规范 | Schema v3 语义资产编写约定与错误码表 |
| `docs-v4-p1-current-state.md` | 15 KB | 证据 | P1 指标清单与当前状态边界（280 canonical + 14 legacy） |
| `docs-v4-p2-backend-evidence.md` | 24 KB | 证据 | P2/P3 真实后端证据 · 旧仓库只读审计合并卷宗 |
| `docs-v4-p2-s1-contract-recon.md` | 21 KB | 证据 | P2-S1 契约骨架设计摘要（含 file:line 清单） |
| `docs-v4-p2-s1b-relation-coverage-recon.md` | 26 KB | 证据 | P2-S1 Slice 2B：RelationCoverage + 组织范围强制 |
| `docs-v4-p4-s1-activation-recon.md` | 22 KB | 证据 | P4-S1「激活 typed QUERY 路径」插入点设计 |
| `docs-v4-p4-shared-plan-executor-hitl-evidence.md` | 52 KB | 证据 | P4 共享 Plan/Executor/HITL 证据 |
| `docs-v4-p5-remote-db-calculation-capability.md` | 59 KB | 证据 | P5 远端 PostgreSQL 计算能力审计 |
| `docs-v4-p5-sandbox-runtime-architecture-recon.md` | 119 KB | 证据 | P5-C 沙箱/ML 运行时架构侦察（只读） |
| `docs-v4-calculation-runtime-release-candidate.md` | 10 KB | 发布候选 | 计算运行时 RC：迁移分类、公式文法、结果与错误语义 |
| `docs-v4-frontend-reuse-matrix.md` | 4 KB | 前端 | 旧前端 → V4 复用矩阵与工作台接缝 |
| `docs-v4-ci-coverage-gap.md` | 4 KB | CI | V4 CI 覆盖缺口 |
| `docs-v4-product-upgrade.md` | 7 KB | 产品 | V4 产品升级说明（产品视角，本次新增） |
| `docs-pr07a-slice1.md` | 6 KB | 切片报告 | 投诉在途确定性 count |
| `docs-pr07a-slice2.md` | 8 KB | 切片报告 | 投诉首响及时率与组织比较/排名 |
| `docs-pr07a-slice3.md` | 8 KB | 切片报告 | 聚合源选择与确定性校验 |

其他文档：`deploy/runbook.md`（发布/回滚/恢复演练手册）、`specs/.../` 6 份规格、`benchmarks/datasets/spider/README.md`、`configs/semantic/{semantic.md,qa.md}`（语义与问答样例）。

## 五、脚本、配置与资产

### 5.1 scripts/（21 项）

| 脚本 | 类别 | 用途 |
|---|---|---|
| `deploy.sh` | 发布 | 按 manifest 顺序部署（迁移→双 API→Nginx→smoke） |
| `rollback.sh` | 发布 | 只允许回退到当前 manifest 记录的上一版本 |
| `backup.sh` | 运维 | control/checkpoint 备份 + 校验 + latest 指针 |
| `restore-test.sh` | 运维 | 隔离恢复演练（仅允许 *_restore_test 库名） |
| `smoke.sh` | 运维 | 经 Nginx 检查 /healthz 与 /readyz |
| `release_manifest.py` | 发布 | 创建与校验不可变 release manifest（镜像 digest + compose checksum） |
| `lib/deploy_common.sh` | 发布 | 部署脚本共享函数 |
| `dev.sh` | 开发 | uv run python main.py dev |
| `lint.sh` | 质量 | 静态检查 |
| `validate_semantic_authoring.py` | 校验 | 语义资产编译校验（含 --strict-metadata） |
| `benchmark/validate_enterprise_benchmark.py` | 校验 | 企业基准数据校验 |
| `training/validate_training_dataset.py` | 校验 | 训练数据集校验 |
| `langfuse_eval_minimal.py` | 评测 | Langfuse 最小评测函数库 |
| `local-demo-start.ps1` | 演示 | 一键启动隧道 + 9001 + 9000（显式注入全部环境变量） |
| `local-demo-stop.ps1` | 演示 | 停止本地演示服务 |
| `local_tunnel.py` | 演示 | 常驻 SSH 隧道（paramiko，自动重连） |
| `mint_demo_token.py` | 演示 | 签发本地演示登录态（从 secret 文件读凭据，脚本内无密钥） |
| `p2/org_contact_reconcile.py` | 侦察 | 组织联系人核对（一次性） |
| `p2/org_db_evidence.py` | 侦察 | 库证据采集（一次性） |
| `p2/ssh_tunnel.py` | 侦察 | 早期隧道脚本（环境变量传凭据） |
| `sql/ensure_pgvector.sql` | 资产 | pgvector 扩展准备 |

### 5.2 配置资产

| 路径 | 内容 |
|---|---|
| `configs/semantic/gold/metrics/*.yaml`（10 份） | **权威指标源，合计 280 个指标**：complaint 54、installation 56、repair_service 51、single_fault 39、configured_external 36、weak_light 19、inspection 12、fault_delivery_external 9、complaint_verification 4、satisfaction 0（显式 `metrics: []`） |
| `configs/semantic/ai_views.yaml` | 9 个 AI 视图：v_single_fault_order、v_repair_service、v_fault_reporting_order、v_installation_work_order、v_metric_result、v_metric_metadata、v_maintenance_metric_daily、v_area、v_team（schema `ai_views`） |
| `config/metrics/complaint.yaml` | 部署绑定：`complaint_in_transit_count`（count）、`complaint_first_response_rate`（ratio），均 `pending_source` |
| `config/metrics/repair_service_local_real.yaml` | 本机真实数据用例绑定：及时数/分母/比率三个指标 + 零分母策略 |
| `configs/semantic/templates/{metric,qa,view}.yaml` | 业务人员填写模板（无需改 Python） |
| `configs/semantic/{semantic.md,qa.md}` | 旧语义声明（93 条）与问答样例 |
| `docker/`（30 项） | Dockerfile、5 个 compose、control/checkpoint 两套 alembic 迁移（004 semantic_registry_v3）、角色初始化脚本、备份/迁移/恢复脚本 |
| `deploy/` | release manifest 示例 + runbook + 两个 demo env 示例 |
| `nginx/nginx.conf` | 唯一外部入口（request-id、SSE、限流） |
| `.github/workflows/ci.yml` | CI 流水线 |
| `.gitleaks.toml` / `.gitleaksignore` | 密钥扫描配置与豁免 |
| `.gitattributes` | `* text=auto eol=lf`，数据集 `-text` |

## 六、未纳入 Git 的本机专属区域

| 目录/文件 | 规模 | 说明 |
|---|---|---|
| `tt-intelligent-main/` | 1.1 GB / 75,510 文件 | 前端 + tt-api/tt-ai 参考快照；前端已单独推送至 Gitee |
| `.venv/` | 464 MB | Python 3.13 虚拟环境 |
| `.opencode/` | 52.6 MB | 工具工作区 |
| `%SystemDrive%/` | 1.3 MB | 变量未展开产生的垃圾目录，建议删除 |
| `secrets/` | 5 文件 | SSH 隧道凭据、只读/管理员 DSN、DeepSeek key |
| `logs/`、`var/`、`.local/` | 小 | 运行日志与状态 |
| `.env.demo`、`.demo_tokens.json`、`_build_slide2.py`、`周例会ppt_928_yhx.pptx` | — | 本机演示/个人产物 |
| Gitee 前端工作副本 | — | `E:\平台开发\tt-intelligent`（已连 Gitee，HEAD 3eb373d） |


## 七、已经完成的部分（按阶段归档，勿遗漏）

### 7.1 数据面与权限面（P0–P3）

| 阶段 | 交付物 | 代码 | 测试 |
|---|---|---|---|
| P1 指标清单 | 280 canonical + 14 legacy 的只读清单；依赖 DAG（113 边 / 56 派生）；类别与时间粒度映射；生命周期与来源就绪**分离** | `semantic/metric_inventory.py`(1160)、`inventory_release.py`(903)、`planner_metric_projection.py`(479)、`authoritative_sources.py`(143) | `test_metric_inventory`(9)、`test_metric_dependency_graph`(11)、`test_planner_metric_projection`(13)、`test_inventory_release`(12)、`test_authoritative_sources`(13) |
| P2 授权与输入政策 | AuthorizationContext 契约、RelationCoverage 组织范围强制、SensitiveField、ModelInputPolicy 目的地门禁、观察者出口脱敏、checkpoint 脱敏 | `contracts.py`、`semantic/schema_snapshot.py`(1874)、`infra/llm/model_input_policy.py`(236)、`observability/*`(761) | `test_authorization_contracts`(32)、`test_authorization_wiring`(22)、`test_model_input_policy`(21)、`test_observability_sink_policy`(20)、`test_observer_egress_policy`(30)、`test_checkpoint_scrub`(3) |
| P3 有效发布读取 | PublishedMetricReader 只读接缝；Effective Published Metric Result 不可变契约 | `semantic/published_reader.py`(214)、`published_result.py`(194) | `test_published_reader`(27)、`test_published_result`(28) |

### 7.2 共享执行与 HITL（P4）

- 共享 Plan / Executor / HITL / Budget / Grounded 协议：`orchestration/` 12,122 行（20 文件）
- **P4-S2 规范化批准计算内核 = DONE**（`CANONICAL_APPROVED_COMPUTE_KERNEL_READY`；PR #41 Draft/Open/unmerged；生产 **NOT_ENABLED**）
  - 代码：`approved_compute.py`(422)、`custom_calculation_execution.py`(244)、`governed_calculation_inputs.py`(154)
  - 测试：`test_canonical_approved_compute`(60)、`test_custom_calculation_execution`(6)
- **P4-Q 验收 = CONTRACT_READY，尚未 PASS**（`p4q_acceptance.py` 806 行 + `test_p4q_acceptance` 107 个用例；缺真实证据 RealnessWitness）
- 计算运行时：`semantic/calculation_contract.py`(717) + `calculation_runtime.py`(452) 纯确定性求值器；56/56 派生公式文法可表示；`docs-v4-calculation-runtime-release-candidate.md`

### 7.3 V4 产品面（本次交付重点）

| 能力 | 后端 | 端点 | 测试 |
|---|---|---|---|
| QUERY 问数（零模型确定性） | `orchestration/deterministic_query_plan.py`(981)、`metric_query.py`(1352) | `POST /queries` | `test_deterministic_query_path`(63)、`test_metric_query`(38)、`test_metric_ratio_query`(27)、`test_metric_sources`(20) |
| ANALYZE 诊断（受治理取数 + 一次模型解读） | `orchestration/analysis_evidence.py`(477)、`engine.py` 分析节点 | `POST /queries`（effective_mode=ANALYZE） | `test_mode2_analysis_integration`(12)、`test_analysis_evidence`(8) |
| BUILD 定义创作 | `artifacts/service.py`(512)、`custom_definition.py`(208)、`build_run.py`(81) | `POST /definitions` 等 10 个 | `test_artifact_product_foundation`(41)、`test_definition_execution_service`(8) |
| 目录/资产库 | `product_library_service.py`(404)、`library.py`(275)、`publication.py`(219)、`publication_service.py`(146) | `/library` 11 个 | `test_product_library_service`(19) |
| 冲突工作台 | `personal_conflict_service.py`(459)、`personal_conflict_contract.py`(270)、`personal_conflict_product_service.py`(255) | `/conflicts/personal` 2 个 | `test_personal_conflict_service`(7)、`test_personal_conflict_http`(6)、`test_personal_conflict_contract`(11) |
| 澄清/决策/恢复 | `decision_contract.py`(751) | `/threads/{id}/actions` | `test_decision_contract`(30)、`test_typed_clarification_decision`(18)、`test_typed_actions_api`(4) |
| 产品级 HTTP 旅程 | — | 全链路 | `test_product_http_journey`(19)、`test_demo_e2e`(8) |

**前端**：30 个文件已推送至 Gitee `tt-intelligent`（`3eb373d`）：V4 助手协议（metadata→block→done）、线程级 run 状态、六个新内容块视图、三个工作台、路由保留、令牌无关的登录模板。

### 7.4 本地真实数据演示通道（本机新增，已完成）

- `local_real/`（5 文件 / 1,283 行）：内存语义发布、活体探测、受治理输入、冻结用例绑定
- `demo/`（3 文件 / 426 行）：纯合成演示运行时（无数据库，product 模式结构性禁用）
- 授权 provider：`core/auth/demo_provider.py`(82)、`local_real_provider.py`(81)
- 演示脚本：`local-demo-start.ps1`、`local-demo-stop.ps1`、`local_tunnel.py`、`mint_demo_token.py`
- 实测链路：本机 → SSH 隧道 → 远端真实库（只读）→ DeepSeek 官方；三模式端到端可用

### 7.5 本次会话为跑通链路所做的 8 处修复（均已提交）

| # | 文件 | 修复 |
|---|---|---|
| 1 | `orchestration/typed_runtime.py`、`local_real/deployment.py` | 为无索引的发布视图增加**有界 bootstrap 扫描**上限（部署常量） |
| 2 | `container.py` | local-real 快照补上 `city_company` 组织覆盖声明 |
| 3 | `orchestration/budget.py` | 路由预算支持环境变量覆盖（默认值不变，测试不受影响） |
| 4 | `v2.py` | 请求上下文真正采用配置的 request deadline |
| 5 | `infra/llm/gateway.py` | `json_object` 模式自动补 JSON 提示（DeepSeek 兼容） |
| 6 | `orchestration/analysis_evidence.py` | 精确 JSON schema 进 system 提示 + 严格数字引用规则 |
| 7 | `orchestration/engine.py` | Mode3 BUILD 确定性 proposal（plan_card） |
| 8 | 启动脚本 | 清理 `HTTP_PROXY`/`NO_PROXY`（含 `[::1]` 会令 httpx 崩溃） |

### 7.6 测试与质量证据（区分来源）

| 证据 | 结果 | 来源 |
|---|---|---|
| 本次全量 `pytest tests` | **2,256 passed / 64 skipped** | 本会话实际运行 |
| 历史 closeout full-unit | 1,414 passed / 2 skipped | 主计划 §1.6 已接受记录（未重跑） |
| CI | 1,416 passed / 62 skipped，SUCCESS | 主计划 §1.6（绑定完整 SHA，未重跑） |
| 静态检查 | ruff clean；pyright 0 errors | 主计划 §1.6 |

> 三者是**不同运行**，不可混比（主计划 A13 明确要求）。

## 八、尚未完成 / 已知缺口（不要误认为已完成）

| 项 | 当前状态 | 说明 |
|---|---|---|
| P4-Q 真实验收 | **CONTRACT_READY，非 PASS** | 需要真实 required cases + RealnessWitness + 完整证据链 |
| 生产 canonical approved-compute | **NOT_ENABLED** | 内核与 CI 就绪，但未在生产启用 |
| 后端 PR #41 | Draft / Open / **unmerged** | 需 owner 决定合并 |
| HITL 闭环 | 存在 approve 后返回消息并终止的旧路径 | 尚未完整接回 typed revalidate → compile → execute |
| H19a–H19i | 工程 backlog（9 项） | 绑定/校验和、accounting seam、grounding 防御深度、权威 loader、集成证据等 |
| 前端仓库 | **已解决** | 本次已推送 Gitee，后续需在 `E:\平台开发\tt-intelligent` 继续 |

明确未纳入本轮范围：CSV/XLSX 上传联合分析、预测/建模、外部数据抓取、定时订阅与自动推送、Agent 自主 canonical 发布、多租户强隔离/K8s、第二套评测栈。

## 九、清理建议（按风险从低到高）

1. **删除垃圾**：`%SystemDrive%/`（1.3 MB，变量未展开产物）。
2. **可删除的孤立代码**（合计约 1.4k 行）：`src/nl2sql/cli.py`、`agents/sql_agent/sql_generator.py`、`agents/nl2sql/{nodes,prompts,state,service}.py`、`agents/gen_data/{middleware,tools,prompts,schemas,service}.py`、`semantic/models.py`、`infra/memory/checkpoint_migrate.py`。
3. **需明确定位的“仅测试可达”模块**（约 6.0k 行）：P1/P3 切片产物与旧 RAG 组件。建议在文件头标注「设计留档 / 未接线」，或在主计划里登记去向，避免误当活跃路径。
4. **文档分层**：`docs-v4-p2/p4/p5-*` 共约 338 KB 属阶段证据，建议移入 `docs/archive/` 或加冻结日期前缀，与 README/契约类文档区分。
5. **遗留栈标注**：`supervisor/` 与 `agents/` 建议在 docstring 明确「仅 CLI/回归使用，HTTP 产品面不接线」。
6. **可考虑忽略**：`.mobilework-sessions.json`。
