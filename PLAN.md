# TT-AI 升级计划

## 1. 目标定位

将当前 NL2SQL 系统升级为“可控的动态指标计算代理系统”，核心目标不是可视化，而是支持用户在对话中以自然语言定义以下内容并得到计算结果：

- 取数来源
- 计算逻辑
- 数据类型约束
- 分析要求

系统必须在“数据库与运行环境同机部署”的前提下，兼顾性能、稳定性、安全性与可审计性。

## 2. 当前现状与差距

### 2.1 当前能力

- Supervisor 当前主路径仅启用语义 SQL 查询链路。
- RAG 由 QA 与 Semantic 两路组成，具备工具调用与有限轮次重试能力。
- 语义层与索引同步机制已具备基础工程化能力。

### 2.2 核心差距

- 缺少“动态指标计算”执行闭环，无法承接非预定义指标。
- RAG 缺少显式检索质量判别与重写节点，鲁棒性不足。
- 索引重建与上下文注入策略仍偏重，单机场景下资源竞争风险较高。
- SQL/Code 失败后的自动修复流程尚未形成统一标准链路。

## 3. 单机部署约束与设计原则

### 3.1 约束

- DB、API、Agent、向量检索同机运行，CPU/IO/内存/GPU 资源共享。
- 高并发下，索引构建、向量检索与 SQL 查询会产生抖动放大。

### 3.2 原则

- 优先“可控准确”而非“全自动自由生成”。
- 计算链路分层：取数与计算分离，失败可回退。
- 强制读写隔离：动态代码只读数据、限制执行资源、全量审计。
- 以“可验证指标”驱动优化，不做黑盒调参。

## 4. 目标架构（To-Be）

### 4.1 三层执行面

- 规划层（Supervisor）：意图识别与任务分解（取数/计算/解释）。
- 取数层（SQL Specialist）：只负责生成与执行可约束 SQL。
- 计算层（Code Analyst）：只负责动态指标逻辑执行与结果结构化输出。

### 4.2 RAG 能力层

- Schema-RAG：面向表、字段、指标口径、历史 SQL 的结构化召回。
- GraphRAG：基于 `ai_views.yaml` Join 图进行多跳关系增强召回。
- Self-RAG/CRAG：增加检索评分、重写与降级决策。

### 4.3 安全与可控层

- SQL 白名单/只读策略/自动 LIMIT 注入/超时熔断。
- Code 沙箱（导入白名单、资源配额、执行超时、禁网与文件权限隔离）。
- 全链路审计：输入、检索证据、SQL、代码、错误与最终结果。

## 5. 并行优化试验（A/B/C）

### 试验 A：RAG 路线

- A1：现有向量 RAG（baseline）
- A2：向量 RAG + reranker + 证据压缩
- A3：GraphRAG + Self-RAG（目标路线）

### 试验 B：动态计算路线

- B1：SQL-only（baseline）
- B2：SQL + 模板函数执行
- B3：SQL + 沙箱 Python 动态代码（目标路线）

### 试验 C：修复策略路线

- C1：一次生成，不修复
- C2：SQL 执行反馈修复
- C3：SQL + Code 双环路修复（目标路线）

### 统一评测指标

- Dynamic Metric Success\@1 / Success\@N
- SQL 执行成功率
- 计算结果一致性（与离线真值对比）
- P95 延迟、单机资源水位（CPU/内存/IO）
- 安全违规率（越权、写操作、超配额执行）

## 6. 分阶段实施

### Phase 1：动态指标计算 MVP（关键）

- 建立 Code Analyst 子图：Plan -> Generate -> Execute -> Repair -> Return。
- 与 SQL Specialist 串联：先取原子数据，再执行自然语言定义计算。
- 输出仅聚焦结果与可解释过程，不将可视化作为主目标。

### Phase 2：RAG 升级

- 在 `agentic_rag` 中加入 Grader 与 Query Rewrite 节点。
- 引入 GraphRAG（Join 图、指标依赖图）进行多跳检索增强。
- 统一 API/CLI 索引同步语义，降低运行差异。

### Phase 3：Adaptive RAG + 可靠性 ✅

- [x] 自适应路由器 (`adaptive_router.py`): Fast / Standard / Deep 三路策略
- [x] CRAG 三级置信升级: grade_node 重构 → Correct / Ambiguous / Incorrect 分流
- [x] 知识精炼节点 (`refine_node`): 低分裁剪 + GraphRAG 补充 + 经验记忆注入
- [x] GraphRAG 增强: 关键词倒排索引、图缓存 + 早停、列注释查询
- [x] ExperienceStore 线程安全: threading.Lock 保护写操作和 hit_count 更新
- [x] 并发安全审计: 所有节点无共享可变状态，异步检索使用 asyncio.gather
- [x] CRAG 阈值可配置: crag_correct_threshold / crag_ambiguous_threshold 从配置读取
- [x] 单元测试: 路由决策、三级置信路由、GraphRAG 缓存/早停/关键词查询

### Phase 4：Context Engineering + 安全强化 ✅

- [x] SQL Guard AST 全面校验：sqlparse 解析树禁止写操作 + 笛卡尔积检测 + 关键词拦截
- [x] EXPLAIN 成本估算：超阈值拒绝执行（TUNABLE: explain_cost_threshold）
- [x] DatabaseManager.validate_query 升级为 full_validate_query（含异步成本评估）
- [x] 上下文压缩：新旧结果分层 + 超长 schema 裁剪 + 全局摘要兜底
- [x] KV-Cache 友好提示重构：固定前缀 + 动态后缀 + 工具可用性掩码
- [x] Supervisor prompts.py 改用 prompt_builder（保持接口兼容）
- [x] 全链路审计 AuditTrail：trace_id 贯穿 + 路由/RAG/SQL/HITL/CodeAct 事件记录
- [x] 可配置参数：explain_cost_threshold / context_recent_full_rounds / context_global_summary_threshold
- [x] 单元测试：SQL Guard AST 校验 + 上下文压缩 + 审计 trail + prompt builder

## 7. 技术选型（单机优先）

- 编排：LangGraph（延续现有体系）
- 结构化检索：FAISS/pgvector（二选一，按运维复杂度）
- 图检索：NetworkX（内存图）+ 本地缓存
- 代码执行：受限 Python 沙箱（优先进程隔离）
- SQL 约束：SQL AST 校验与重写（只读、限流、超时）

## 8. 交付标准

- 可直接回答“非定义指标”类问题，且结果可复现。
- 提供计算证据链：数据来源、计算步骤、关键中间量。
- 在单机负载下满足稳定性目标并可审计可回滚。

## 9. 基准与对照实验设计（必须执行）

### 9.1 多基准体系

- 通用复杂 SQL：Spider 2.0、BIRD。
- 中文能力：DuSQL / CSpider。
- 多轮对话（可选）：SParC / CoSQL。
- 鲁棒性：Dr.Spider（改写、噪声、schema 干扰）。
- 企业专用：动态指标自建评测集（日志脱敏 + 专家标注）。

### 9.2 对照组矩阵

- Embedding 对照：bge-m3 vs gte-large vs 本地可部署候选。
- 检索链路对照：Vector-RAG vs Vector+Rerank vs GraphRAG+Self-RAG。
- 生成模型对照：主模型 A/B（参数规模与成本分层）。
- 修复策略对照：无修复 vs SQL 修复 vs SQL+Code 双修复。
- 执行策略对照：SQL-only vs SQL+模板计算 vs SQL+动态代码计算。

### 9.3 统一评测口径

- 语义正确性：Execution Accuracy、Test-Suite Accuracy。
- 动态计算正确性：指标结果误差（MAPE/SMAPE）与一致性。
- 工程性能：P50/P95、吞吐、资源水位、超时率。
- 安全治理：越权率、危险 SQL 拦截率、代码沙箱违规率。
- 业务可用：成功率、可解释性完整度、人工复核通过率。

## 10. 微调与后训练策略（门槛触发）

### 10.1 默认策略

- 第一阶段不默认微调，先做检索、约束、路由、修复的工程优化。
- 仅当“连续迭代后仍存在稳定能力缺口”时进入训练阶段。

### 10.2 触发门槛

- 核心指标连续两轮迭代提升小于 2pp，且距上线阈值仍大于 5pp。
- 错误归因中模型能力缺口占比大于 40%（非检索/数据脏问题）。
- 动态指标复杂样本成功率持续低于目标阈值。

### 10.3 训练路径

- SFT：先做格式与行为对齐（SQL 约束、工具调用规范、拒答策略）。
- 偏好优化：再做 DPO/排序优化，降低误调用与冗余步骤。
- 验证器奖励：引入可执行性、引用一致性、安全约束作为奖励或过滤。

### 10.4 数据设计（训练与评测一体化）

- 数据来源配比：70% 真实日志脱敏、20% 合成难例、10% 对抗样本。
- 样本结构：
  - NL2SQL：问句、schema 快照、术语映射、gold SQL、执行结果、错误标签。
  - Agent 链路：问题、候选证据、计划、工具轨迹、最终答案与引用。
- 切分原则：按时间与 schema 切分，避免泄漏，确保新 schema 泛化评估。

