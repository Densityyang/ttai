# TT-AI 当前阶段优化点与升级方案

> 日期：2026-07-21  
> 依据：现有仓库审查、`C:\Users\Density\Desktop\nl2sql.md` 的调研材料，以及 Claude Code、OpenAI Codex、Pi、Vanna、WrenAI、ReFoRCE、BIRD、Spider 2.0、Phoenix、OpenHands 的当前公开资料。  
> 本文假设“CC”指 Claude Code，“Pi”指 [Pi Agent Harness](https://github.com/earendil-works/pi)。外部项目用于提炼设计模式，不建议直接 Fork 后替换现有 TT-AI。

## 1. 结论：下一阶段的主题应是“收敛为可治理的 Agent 平台”

TT-AI 目前并不缺少 Agent 能力。它已经包含 Supervisor、SQL Agent、Agentic/CRAG/GraphRAG、语义层、并行候选 SQL、假设验证、SQL Guard、动态计算/CodeAct、HITL、审计、Langfuse 与 BIRD/企业案例评测。与调研材料中建议的“先做 Router → Generator → Validator → Executor 的 MVP”相比，TT-AI 已经越过 MVP 阶段。

当前真正的瓶颈是：这些能力缺少统一的任务契约、策略执行点、质量门禁与可回放评测。继续新增 Agent 只会扩大延迟、成本、故障组合与维护面。

**建议的版本定位：从 `v0.1 功能完备原型` 升级为 `v0.2 可治理、可评测的企业 NL2SQL Agent 平台`。**

升级优先级应为：

1. 统一身份/权限/预算/审计贯穿全部工具调用；
2. 将语义、检索、候选、执行结果收敛为可验证的结构化工件；
3. 以置信度和风险路由 Agent 深度，而非默认开启所有增强能力；
4. 建立以企业集为主、BIRD/Spider 2.0 为辅的发布门禁；
5. 最后才扩展数据源、模型和新 Agent。

## 2. 外部生态调研：值得借鉴的“机制”，而不是“框架替换”

| 来源 | 当前做法 | 对 TT-AI 的可迁移价值 | 建议 |
|---|---|---|---|
| [Claude Code：Subagents](https://code.claude.com/docs/en/sub-agents) | 每个子 Agent 有独立上下文、定制系统提示词、工具范围与权限；主 Agent 只接收结果摘要 | 专业 Agent 必须输入/输出最小化，检索日志与执行细节不能无限污染主链路 | 借鉴 |
| [Claude Code：Hooks](https://code.claude.com/docs/en/hooks) | 在工具执行前后可允许、拒绝、要求确认、改写参数、记录失败 | 把 SQL/代码安全从“节点内部代码”前移为统一 policy hook | 借鉴 |
| [OpenAI Codex：Subagents](https://learn.chatgpt.com/codex/agent-configuration/subagents) | 探索、审查、文档核对等读密集工作并行；权限继承并可覆写为只读 | 将 schema 探索、候选 SQL 评审、评测诊断拆成受限并行工作单元 | 借鉴 |
| [OpenAI Codex：AGENTS.md](https://learn.chatgpt.com/codex/agent-configuration/agents-md) / [MCP](https://learn.chatgpt.com/codex/extend/mcp) | 分层项目指令；以 MCP 作为工具与上下文契约 | 将领域语义、评测、只读元数据、查询审批暴露为版本化能力，而非散落 prompt | 借鉴 |
| [Pi Agent Harness](https://github.com/earendil-works/pi) | 极简 Agent loop、统一多模型 API、运行时状态；明确说明自身不提供权限边界，建议容器化 | 模型提供商解耦可借鉴；“运行时能力不等于安全边界”的警示尤其重要 | 仅借鉴适配层理念 |
| [Vanna 2.0](https://github.com/vanna-ai/vanna) | 用户身份贯穿 Agent、工具和 SQL 过滤；流式结构化组件、审计、配额与生命周期钩子 | 当前 TT-AI 最应补齐：thread 归属、用户上下文下传、RLS/行级过滤、配额与结构化前端事件 | 高优先级借鉴 |
| [WrenAI](https://github.com/Canner/WrenAI) | 将业务含义、审批定义和已验证样例作为可审阅、可版本化 context；dry-plan、结构化错误、值分布与 eval runner 是正确性原语 | 语义文件应升级为“语义资产仓库”，加入版本、审批、血缘、验证 SQL 和变更影响分析 | 高优先级借鉴 |
| [ReFoRCE](https://github.com/Snowflake-Labs/ReFoRCE) | schema 压缩 + 链接、自修复、候选共识、执行反馈驱动的列探索 | 现有并行生成/验证应从“始终执行”改为“高不确定性时升级”，并带置信度和预算 | 高优先级借鉴 |
| [BIRD](https://bird-bench.github.io/) | 除执行正确率外强调效率；已扩展 Interactive、Critic 与 LiveSQLBench | 把“SQL 能运行”升级为“正确、受控、高效、可解释”四维质量门禁 | 高优先级借鉴 |
| [Spider 2.0](https://spider2-sql.github.io/) | 面向大 schema、长上下文、跨方言、仓库级工作流；传统 benchmark 高分并不代表企业可用 | 企业集必须成为主发布门禁；不能把 Spider 1.0/BIRD 分数当生产 readiness | 高优先级借鉴 |
| [Phoenix](https://github.com/Arize-ai/phoenix) | 基于 OpenTelemetry 的 trace、数据集、实验、prompt 版本、回放与评测 | 现有 Langfuse 可保留；但 trace schema 与离线实验要标准化，避免观测只停留在日志 | 高优先级借鉴 |
| [OpenHands](https://github.com/OpenHands/openhands) | 长任务从本地机器解耦到可隔离工作区，并强调安全加固 | CodeAct 和耗时 benchmark 应迁入隔离 worker/job，而非 API 进程内部运行 | 高优先级借鉴 |

### 关键洞见

1. **CC/Codex 的核心并不是更多子 Agent，而是“每个 Agent 都有受限任务、独立上下文、最小工具面和可审计边界”。**
2. **Vanna/Wren 的核心优势不在 SQL 生成，而在把用户身份和业务语义当作一等输入。**
3. **ReFoRCE 与最新 benchmark 表明：大 schema 场景胜负主要在 context 编译、候选选择、执行反馈和拒答，不在单次模型生成。**
4. **Pi 的设计反而说明：多模型统一层可以轻量，但权限、网络、文件和进程隔离必须在运行时之外强制。**

## 3. 对当前 TT-AI 的差距映射

| 现状 | 已有基础 | 缺口 | 优化方向 |
|---|---|---|---|
| Agent 编排 | `supervisor/agent.py`、多个 LangGraph | Agent 间状态与结果契约偏松，失败类型和预算不统一 | 引入 `TaskEnvelope`、`EvidenceBundle`、`PolicyDecision`、`ExecutionReceipt` 等 Pydantic 工件 |
| 语义/RAG | Semantic YAML、QA/Semantic/Graph RAG | 语义资产缺少版本、审批、血缘、可执行验证和变更影响分析 | 建立 Semantic Registry 与 context compiler |
| SQL 生成与修复 | 并行生成、假设验证、repair loop、SQL Guard | 多候选/深度路径可能默认过度执行；缺少统一置信度与退出条件 | 使用风险驱动的 Fast/Standard/Deep 路由 |
| 授权与隔离 | 外部鉴权、schema 限制、只读 SQL 限制 | thread/history/HITL 的资源归属、行列级策略、配额、CORS 和结果脱敏不完整 | 统一 `RequestIdentity` 与 policy enforcement point |
| 代码执行 | 子进程、静态检查、内存/超时限制 | 非 Linux 下不构成强隔离；仍是进程内编排和 `exec` 模式 | 外置 sandbox worker，默认无网/非 root/只读 FS |
| 可观测性 | Langfuse、audit trail | 没有贯穿 query、retrieval、candidate、policy、SQL execution 的统一 trace/评测闭环 | 采用 OTel/OpenInference 风格事件模型，关联 dataset/prompt/semantic 版本 |
| 评测 | BIRD 适配与企业案例数据 | 当前环境无法运行完整测试；缺少发布阈值、回归集、错误 taxonomy 与性能预算 | CI + 离线评测门禁 + 线上影子回放 |
| 部署 | FastAPI、Docker、配置 | Docker 安装失败被忽略；RAG 同步耦合启动；全局单例与多 worker 风险 | Job 化初始化、依赖健康检查、显式 readiness、不可吞错构建 |

## 4. v0.2 目标架构

```text
Request
  -> Identity & Tenant Resolver
  -> Policy Enforcement Point (权限、配额、敏感级别、审批等级)
  -> Context Compiler
       (semantic version + schema slice + approved examples + value profile)
  -> Risk Router
       Fast: 单候选、低风险
       Standard: plan + generate + dry-validate
       Deep: schema exploration + 多候选 + verification + HITL
  -> Query Execution Gateway
       (native readonly role + RLS + timeout + EXPLAIN + result masking)
  -> Structured Answer Renderer
  -> Trace / Audit / Eval Feedback
```

### 4.1 必须新增的核心契约

这些契约应使用 Pydantic 模型定义、写入 trace/audit，并在 LangGraph 节点之间传递；不要让核心状态继续以松散 `dict` 演进。

| 工件 | 必填字段 | 用途 |
|---|---|---|
| `RequestIdentity` | request_id、user_id、tenant_id、roles、data_scopes、trace_id | 所有节点和工具统一上下文 |
| `PolicyDecision` | allow/deny/approval、理由、最大行数、预算、可访问数据域 | 在每次检索、SQL、代码、HITL 前后执行 |
| `ContextBundle` | semantic_version、schema_snapshot、evidence_ids、example_ids、token_cost | 可复现地解释“模型为何看到这些上下文” |
| `QueryPlan` | intent、metrics、dimensions、filters、time_range、expected_grain、risk | 把自然语言意图先结构化，SQL 只是实现 |
| `QueryCandidate` | SQL、来源策略、置信度、validation、cost、fingerprint | 供投票/验证/人工审批，避免仅保留最终 SQL |
| `ExecutionReceipt` | role、RLS/策略版本、row_count、elapsed、plan_cost、error_taxonomy | 审计与评测的原始证据 |
| `AnswerArtifact` | data_ref、masked_columns、citations、explanation、confidence | 统一 API/CLI/流式输出 |

## 5. 版本阶段上的具体优化点

### P0：2 周内，先把生产边界做实

1. **统一身份与会话归属。** 所有 `thread_id`、历史读取、HITL 操作都必须绑定 `user_id + tenant_id`；禁止仅凭猜测到的 thread id 读取会话。将 `auth_user` 由 API 层显式传入每个 Graph state 和工具。
2. **统一 Policy Hook。** 在“RAG 检索、SQL 验证、SQL 执行、代码执行、外部 MCP/HTTP 工具”前后调用同一个 policy 模块。策略结果必须可审计，不能只散落在各节点 `if` 中。
3. **数据库强制最小权限。** 使用专用只读账号、独立 schema/view、PostgreSQL RLS；SQL Guard 只作为第二道防线。EXPLAIN 或解析不可用时对高风险查询 fail-closed。
4. **移除默认宽松 CORS 与构建吞错。** 生产 CORS 改白名单；删除 Dockerfile 中 `uv sync --frozen || true`。
5. **将 CodeAct 移出 API 进程。** 最小方案为独立 Docker worker；默认禁网络、只读根文件系统、非 root、资源和输出上限。Windows/非 Linux 不允许声称具备同等级沙箱能力。
6. **启动与就绪解耦。** RAG 索引同步改为独立 job/管理命令；API 仅检查索引版本和依赖健康。提供 `/healthz`、`/readyz`、依赖状态与版本信息。

### P1：3–6 周，减少 Agent 复杂度并提升正确率

1. **实现 Context Compiler。** 将现有 QA、Semantic、Graph RAG 收敛为一次可复现的上下文编译过程：先按租户/权限过滤，再按业务域、关系图、指标、样例 SQL、值分布组织为 `ContextBundle`。
2. **将 Semantic Layer 产品化。** 每条 metric/view/join 除 YAML 内容外增加 owner、审批状态、版本、生效时间、验证 SQL、来源表/列、敏感等级和下游影响。只有“已审批语义资产”可进入生产上下文。
3. **按风险路由而不是全量多 Agent。**
   - Fast：明确指标 + 小 schema slice + 低风险，只生成一条 SQL；
   - Standard：生成结构化 plan、dry validation、一次 repair；
   - Deep：仅在歧义/高成本/跨域/低置信度时启用 schema exploration、多候选共识、hypothesis verifier 与 HITL。
4. **候选选择改为证据驱动。** 多候选不是简单投票：组合 semantic coverage、schema/link 正确性、dry-plan、EXPLAIN 成本、执行结果一致性与历史经验得分。若分差不足阈值，明确澄清或 HITL，而不是随机选择。
5. **建立错误 taxonomy。** 至少区分：身份/权限、语义缺失、schema-link、方言、语法、数据为空、结果异常、成本、超时、工具/依赖、策略拒绝、模型输出格式。每一类都有用户提示、是否重试、修复路由和告警级别。
6. **输出以结构化 artifact 为先。** API 流应传输 table/chart/text/progress/citation/approval 事件，而不是把最终文本解析回结构。Vanna 的 user-aware structured streaming 设计可作为参考。

### P2：6–10 周，形成可持续迭代闭环

1. **评测门禁。** 建立三层数据集：
   - `smoke`：50 个稳定单测级案例；
   - `enterprise-golden`：按业务域、权限、复杂度、风险标注的 200–500 个真实脱敏案例；
   - `external`：BIRD Mini-Dev + Spider 2.0 DBT/Lite 作为泛化和回归信号。
2. **评测指标不只 EX。** 发布需要同时满足：Execution Accuracy、语义正确率、SQL 效率/R-VES 类指标、策略拒绝正确率、P95 延迟、单请求 token/成本、HITL 率、无授权数据泄漏率。
3. **Trace-to-eval 闭环。** 从线上低置信度、失败、HITL 修改、用户负反馈生成候选样本；人工确认后进入黄金集和经验库。每个样本关联模型、prompt、语义、索引和代码版本。
4. **版本实验。** 对每个模型、prompt、retriever、语义和路由策略跑相同回归集；未通过阈值不允许发布。Phoenix/OpenTelemetry 方案值得采用为中立的数据面；Langfuse 可继续保留为现有追踪实现。
5. **影子与灰度。** 新策略先只生成并记录候选/差异，不影响用户；达到质量阈值后按租户/业务域逐步放量，并保留快速回退到 Fast/旧链路的开关。

## 6. 推荐的实施顺序与交付物

| 里程碑 | 交付物 | 验收标准 |
|---|---|---|
| M0：运行基线 | Python 3.13 lockfile/Docker 可复现、CI、health/readiness、测试可运行报告 | `uv sync --frozen` 和单测在干净环境可执行；构建失败不被吞掉 |
| M1：治理边界 | `RequestIdentity`、Policy Hook、thread ownership、DB RLS/只读角色、CORS 白名单 | 越权 thread/HITL、跨租户语义/RAG、写 SQL、超预算查询均被拒绝并留痕 |
| M2：上下文与路由 | Context Compiler、Semantic Registry v1、Fast/Standard/Deep Router | 每次 SQL 都能还原其语义/证据/版本；Deep 使用率与升级原因可观测 |
| M3：执行质量 | Candidate scorer、dry-plan、错误 taxonomy、结构化流式 artifact | 相同请求可解释候选为何被选中；低置信度不静默给出高风险 SQL |
| M4：评测发布门禁 | Golden set、报告、回放、影子流量和灰度开关 | 版本升级能量化正确率、性能、成本和安全回归，无“主观感觉变好”发布 |

## 7. 明确不建议做的事

- 不建议为了“使用最新框架”重写为 Vanna、Wren、DB-GPT、CrewAI、AutoGen 或 Pi；TT-AI 的已有 LangGraph 和语义/评测基础更接近目标。
- 不建议默认并行运行全部检索、所有候选和所有 verifier；这会让长尾延迟和成本失控。
- 不建议把 prompt 约束、正则 SQL 检查或 Python 静态检查当成安全边界；真实边界必须是身份、权限、数据库角色、网络与隔离运行时。
- 不建议先进行大规模微调。Spider 2.0 与 BIRD 的实践都说明真实企业场景的主要问题是上下文、语义、工具使用和执行反馈；在黄金集和错误 taxonomy 稳定前，微调会掩盖问题而非解决问题。
- 不建议把外部 benchmark 分数作为上线标准。Spider 2.0 明确显示传统基准的高分不能代表企业工作流能力；内部脱敏黄金集必须拥有最高权重。

## 8. 推荐的下一步

先启动 **M0 + M1**：修正可复现环境和交付链路，同时实现 `RequestIdentity + Policy Hook + thread ownership`。这是当前风险最高、收益最确定、并且不会推翻现有业务 Agent 的改造。

在 M1 完成前，应冻结新增 Agent/新增模型供应商的需求；在 M2 完成后，再针对企业黄金集的错误分布决定是否需要继续增强 GraphRAG、候选 SQL、模型路由或进行领域微调。

## 9. 参考资料

- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)：隔离上下文、专用工具和权限。
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)：PreToolUse 可拒绝或改写工具调用。
- [OpenAI Codex subagents](https://learn.chatgpt.com/codex/agent-configuration/subagents) 与 [AGENTS.md](https://learn.chatgpt.com/codex/agent-configuration/agents-md)：读密集并行、项目指令层级和只读 agent 模式。
- [OpenAI Codex MCP](https://learn.chatgpt.com/codex/extend/mcp)：MCP 作为受控工具与上下文接口。
- [Pi Agent Harness](https://github.com/earendil-works/pi)：统一 provider/agent runtime，以及显式外置容器化安全边界。
- [Vanna 2.0](https://github.com/vanna-ai/vanna)：user-aware tool/SQL、RLS、审计、配额与结构化流式输出。
- [WrenAI](https://github.com/Canner/WrenAI)：版本化 context、dry-plan、结构化错误和评测。
- [ReFoRCE](https://github.com/Snowflake-Labs/ReFoRCE)：schema 压缩、链接、自修复、共识与列探索。
- [BIRD](https://bird-bench.github.io/)：大规模真实数据库、执行效率和交互/诊断评测。
- [Spider 2.0](https://spider2-sql.github.io/)：面向企业级长上下文、跨方言与仓库级任务的评测。
- [Phoenix](https://github.com/Arize-ai/phoenix)：OTel tracing、版本化数据集、实验、回放和评测。
- [OpenHands](https://github.com/OpenHands/openhands)：解耦的长任务 Agent 与隔离工作区方向。
