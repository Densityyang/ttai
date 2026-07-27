# TT-AI 项目整体 Review 报告

> 审查日期：2026-07-21  
> 审查范围：项目目录、源码、配置、Docker 文件、测试、benchmark/spec 文档及依赖声明  
> 审查方式：静态代码审查 + 当前环境下的编译/测试可执行性检查  
> 说明：本报告不包含真实生产数据库、LLM、向量服务和鉴权服务上的端到端压测，因此对线上吞吐、准确率和故障恢复能力的判断属于基于代码的风险评估。

## 1. 总体结论

TT-AI 是一个功能覆盖面较广的 NL2SQL 智能服务，整体采用“API 接入层—Supervisor 编排层—专业 Agent—基础设施与治理层—数据库/向量存储”的分层设计。项目已经超出简单的 Prompt-to-SQL 原型，具备以下较完整的能力链：

- Semantic Layer：通过 `ai_views.yaml` 管理业务指标、维度、Join 和虚拟视图。
- 多 Agent 编排：Supervisor 协调 NL2SQL、动态计算、数据生成等能力。
- 检索增强：QA RAG、Semantic RAG、GraphRAG，以及 Agentic/CRAG 路由。
- SQL 质量控制：SQL 解析、只读约束、LIMIT 注入、笛卡尔积检测、EXPLAIN 成本评估和修复循环。
- 工程化能力：PostgreSQL checkpointer、审计轨迹、Langfuse 观测、HITL、benchmark 与单元测试。

但当前更像“能力丰富的研究型/增强型工程原型”，尚未达到可直接承载高可靠生产流量的成熟度。最大问题不是缺少功能，而是功能之间的运行边界、生命周期、版本约束和安全纵深还没有完全收敛。

### 综合评分

| 维度 | 评分 | 判断 |
|---|---:|---|
| 架构完整性 | 8/10 | 分层、模块化和领域拆分较好，但全局单例与启动流程耦合明显 |
| NL2SQL 能力设计 | 8/10 | Semantic Layer、RAG、多候选生成和验证链路较先进 |
| 稳定性 | 5/10 | 有超时、重试/修复和降级意识，但缺少可验证的端到端运行证据 |
| 鲁棒性 | 6/10 | 对 SQL、上下文和 Agent 失败有防护，但错误分类和资源隔离仍不充分 |
| 安全性 | 5/10 | 具备鉴权和 SQL Guard，但 CORS、代码执行和租户/线程隔离需加强 |
| 可观测性 | 7/10 | Langfuse、audit trail 和结构化运行配置较好，缺少指标/健康检查闭环 |
| 测试与交付质量 | 4/10 | 单测数量可观，但当前环境无法收集测试，缺少真实集成和 CI 证据 |
| 先进性 | 8/10 | 技术路线明显领先于传统单 Agent NL2SQL，但复杂度和成本也较高 |

**综合判断：6.5/10；建议定位为“具备生产化潜力的高级原型”，在完成 P0/P1 整改后再扩大生产流量。**

## 2. 项目结构与架构评估

### 2.1 当前架构

```text
客户端 / CLI
      |
      v
FastAPI + LangServe + Chat Completions + HITL API
      |
      v
Supervisor Agent
      |
      +--> NL2SQL Graph
      |      +--> Agentic RAG / CRAG / GraphRAG
      |      +--> Semantic Layer / Metric Layer
      |      +--> 并行 SQL 生成 / 假设验证 / 经验库
      |      +--> SQL Guard / EXPLAIN / 执行与修复
      |
      +--> Dynamic Calc / CodeAct
      +--> Gen Data
      |
      v
基础设施：LLM Factory、DB Manager、Checkpointer、向量索引、审计、Langfuse、Runtime Registry
      |
      v
PostgreSQL / FAISS 或 Chroma / 外部 LLM、Embedding、鉴权服务
```

### 2.2 优点

1. **领域边界基本清晰。** `agents`、`infra`、`semantic`、`supervisor` 的拆分符合业务域与基础设施分离原则。
2. **编排方式适合复杂查询。** LangGraph 的显式状态和节点图适合处理多轮推理、工具调用、SQL 修复、HITL 中断与恢复。
3. **Semantic Layer 方向正确。** 由业务语义约束底层表结构，能够减少模型直接猜表名、指标口径漂移和跨表 Join 错误。
4. **治理能力不是事后补丁。** SQL Guard、结果上限、超时、EXPLAIN、审计等已进入核心调用链，设计意识较成熟。
5. **评测意识较强。** `benchmarks/` 和 `specs/nl2sql_dynamic_metric_upgrade/` 表明项目在尝试建立企业案例、HITL、鲁棒性和训练数据闭环。

### 2.3 架构问题

- `api.py` 使用全局 `_supervisor` 和 `_routes_registered`，`database.py`、checkpointer、RAG 索引也存在全局/单例状态。这对开发模式方便，但在多 worker、测试隔离、热重载和多应用实例场景下容易出现重复初始化、状态不一致和资源泄漏。
- API 生命周期中启动 RAG 同步、数据库连接、runtime warmup、checkpointer 初始化和 Supervisor 创建串行耦合。任何一个慢依赖都可能拉长启动时间；严格模式下 RAG 失败会直接阻止服务启动。
- 业务 Agent 数量和策略较多，存在“能力叠加大于边界收敛”的迹象。需要明确 Fast/Standard/Deep 路径、每个路径的预算、失败策略和最大外部调用次数，否则延迟和成本不可控。
- `core` 与 `nl2sql/config` 存在两套配置入口，且 `.env` 加载规则不完全一致，后续容易出现同名配置被不同模块解释不同或测试环境行为不一致。
- Dockerfile 使用 `uv sync --frozen || true`，会吞掉依赖安装失败，导致镜像构建成功但运行时缺包，属于交付链路中的高风险问题。

## 3. 稳定性评估

### 3.1 已有稳定性措施

- Agent 图有 `recursion_limit` 和 `graph_timeout`。
- SQL 执行、校验和代码执行有超时控制。
- SQL Guard 限制只读查询、禁止多语句、限制 CTE/子查询深度，并尝试基于 EXPLAIN 控制成本。
- RAG 启动同步提供 strict/非 strict 模式。
- 数据库通过 async SQLAlchemy 管理连接池，checkpointer 支持内存和 PostgreSQL 两种后端。
- SQL 和代码均有 repair loop，失败结果可以反馈给生成器修复。

### 3.2 主要稳定性风险

1. **启动稳定性不足。** 默认 `RAG_STARTUP_SYNC_STRICT=true`，而 RAG 同步依赖 Embedding 服务和本地索引；只要外部 Embedding 不可用，生产服务可能无法启动。
2. **测试不可执行。** 当前环境为 Python 3.10.9，而项目要求 `>=3.13,<3.14`。执行 `pytest` 时出现 Python 语法不兼容、`langchain_core`/`sqlparse` 缺失以及 pytest 插件配置未识别，测试在收集阶段失败，无法证明现有代码质量。
3. **连接与资源边界需要压测。** LangGraph、数据库、RAG、Langfuse 和多 Agent 并行任务同时存在，尚未看到针对连接池耗尽、取消传播、客户端断开、队列堆积的集成验证。
4. **多 worker 风险。** Uvicorn `workers` 可配置，但全局 Supervisor、checkpointer、RAG 索引和数据库管理器按进程分别初始化；如果将索引同步或 schema 自动同步放入每个 worker 启动，会造成重复工作和竞态。
5. **异常分类偏粗。** 多处捕获 `Exception` 后继续降级或只记录字符串，容易将依赖故障、输入错误、安全拒绝和内部 bug 混为一谈，影响告警、重试和用户提示。

## 4. 鲁棒性与安全评估

### 4.1 SQL 安全

SQL Guard 是项目的亮点，已经覆盖 AST/关键字检查、只读入口、schema 限制、LIMIT、笛卡尔积、EXPLAIN 成本和查询超时。但仍应注意：

- `detect_cartesian_product` 主要依赖 SQL 文本/`sqlparse` 的启发式分析，不能替代 PostgreSQL 解析器或数据库权限隔离。
- `estimate_query_cost` 在 EXPLAIN 出错时记录 warning 并返回无错误，属于 fail-open；当成本评估不可用时，查询仍可能继续执行。
- 真正的安全底线应是数据库账号只读、独立 schema、网络与角色权限隔离，而不是只依赖模型前的字符串/AST 检查。
- 需要增加 PostgreSQL 方言覆盖测试，包括注释、字符串、双引号标识符、嵌套 CTE、函数、UNION、视图、权限错误和恶意输入。

### 4.2 CodeAct/代码沙箱

代码执行已尝试使用子进程、静态检查、资源限制和超时，这是正确方向；但源码自身也明确指出非 Linux 环境会降级为线程/超时隔离，且 `_sandbox_worker` 仍然在受限 globals 中执行 `exec()`。因此：

- 不能将该机制视为强安全边界，尤其不能直接运行不可信用户代码或不可信模型输出。
- 仅靠禁用模块名和 AST 黑名单存在绕过面，Python 对象模型、已注入对象、允许模块和反射能力都需要专项威胁建模。
- 建议生产使用容器/微 VM/独立沙箱服务，配合只读文件系统、无网络、非 root、seccomp/AppArmor、cgroup、进程/文件句柄/输出大小限制，并将输入数据按租户隔离。

### 4.3 鉴权、CORS 与数据隔离

- 默认开启鉴权，并校验 `TT_API_BASE_URL`，说明接入控制已有基础。
- `main.py` 配置为 `allow_origins=["*"]` 且 `allow_credentials=True`。这是不应保留在生产的宽松跨域策略，应改为显式白名单，并按环境区分。
- thread_id 可由 header、query 或 body 注入。历史查询和 HITL 接口需要验证 thread 所属用户/租户，不能只依赖“拥有接口权限”；否则存在跨会话读取或操作风险。
- 日志、audit trail、Langfuse 和错误信息可能携带用户问题、SQL、结果或敏感字段，需要建立脱敏、保留周期和访问控制策略。

## 5. 先进性评估

项目在技术路线上的先进性较明显：

- 从单步 NL2SQL 演进到带状态的多 Agent 图编排。
- 用 Semantic Layer 和 Metric Layer 管理业务口径。
- 同时引入 QA/语义/图关系检索，并通过 Agentic RAG/CRAG 做自适应路由。
- 通过并行 SQL 生成、候选竞争和 hypothesis verification 提升复杂查询成功率。
- 引入经验存储、HITL、审计和 benchmark，具备面向持续优化的雏形。
- Dynamic Calc/CodeAct 解决 SQL 难以表达的二次计算场景。

先进性带来的代价是调用链复杂、LLM 次数多、延迟和成本上升、故障组合变多。建议以 benchmark 驱动能力开关：每个增强模块都要有准确率收益、延迟成本、失败率和可解释性指标，避免默认全开启导致生产行为不可预测。

## 6. 测试与工程质量

### 6.1 现状

仓库有约 124 个 Python 文件、22 个测试 Python 文件，测试覆盖 adaptive router、RAG 路由、经验库、GraphRAG、SQL Guard、上下文压缩、沙箱、审计和 benchmark 等关键模块，方向是对的。

### 6.2 需要补强

- 当前没有可确认通过的 CI 流程；应将 Python 3.13、依赖安装、lint、类型检查、单测、集成测试和 benchmark smoke test 固化到 CI。
- 缺少真实 PostgreSQL/向量库/Embedding/LLM 的可替代集成测试，尤其是连接池、checkpointer、RAG 索引同步和 lifespan。
- 缺少 API 契约测试：鉴权失败、thread 隔离、流式中断、客户端断开、HITL confirm/modify/restart、OpenAI 兼容格式。
- 缺少安全回归集：SQL 注入、多语句、注释绕过、schema 越权、代码沙箱逃逸、输出过大和资源耗尽。
- `pyrightconfig.json`、pytest 配置和实际环境版本需要统一验证，避免“声明了工具但运行环境未安装”的假绿/假红。

## 7. 优先级整改建议

### P0：上线前必须完成

1. 固化 Python 3.13 运行时和依赖安装流程；删除 Dockerfile 中的 `|| true`，让构建失败真实暴露。
2. 将 CORS 改为显式来源白名单，检查所有生产默认值。
3. 为 thread/history/HITL 增加用户或租户归属校验，验证跨会话访问不可行。
4. 将 CodeAct 从进程内/弱隔离方案升级为真正的独立沙箱或明确限制为受信任代码。
5. 数据库使用最小权限只读账号；EXPLAIN/Guard 失败时默认 fail-closed，至少对高风险或无法评估的查询拒绝执行。
6. 增加健康检查、就绪检查、依赖状态和基本 Prometheus/OTel 指标。

### P1：近期完成

1. 重构应用生命周期：使用 app state/依赖注入管理 Supervisor、DB、checkpointer 和索引服务，避免全局状态。
2. 将 RAG/semantic index 同步从每个 worker 启动中剥离为一次性 job 或独立管理命令。
3. 统一配置模型、`.env` 加载规则、默认端口和生产/开发 profile。
4. 为 Agent 链路增加预算控制：最大 LLM 调用次数、工具轮数、总 token、总执行时间和并发数。
5. 对异常建立结构化错误码和可重试性分类，避免宽泛 `except Exception` 直接降级。
6. 完善 API、数据库、RAG、鉴权和沙箱集成测试，并把企业 benchmark 指标接入发布门禁。

### P2：持续优化

1. 建立准确率、执行正确率、语义一致性、延迟、成本、拒答率和人工介入率的版本化看板。
2. 增加 schema/metric 变更的版本、回滚和兼容策略。
3. 对多 Agent 策略做 A/B 或离线回放，按问题难度动态选择最小成本路径。
4. 建立提示词、模型、检索索引和业务语义配置的可追溯版本。

## 8. 建议的下一轮验证计划

1. 在干净 Python 3.13 环境执行 `uv sync --frozen`、`pytest`、lint 和 pyright。
2. 使用 Docker 启动 PostgreSQL/pgvector，验证 lifespan、schema、checkpointer 和索引同步。
3. 使用 mock LLM/Embedding 跑 API 契约测试和流式断连测试。
4. 用恶意 SQL、越权 thread、恶意 Python 代码和大结果集做安全/资源压测。
5. 用企业 benchmark 做基线，记录每个增强模块开启前后的准确率、P95 延迟、token 成本和失败原因。

## 9. 最终判断

项目的产品和技术方向是正确的，且在 NL2SQL 领域已经具备较强的能力组合；Semantic Layer、检索增强、候选 SQL 验证、HITL 和可观测性是最值得保留的核心资产。当前最需要做的是“收敛复杂度并补齐生产边界”：先把环境、构建、资源生命周期、权限隔离和强安全边界做实，再继续增加 Agent 策略。完成 P0/P1 后，项目可以进入更可信的灰度和 benchmark 驱动迭代阶段。
