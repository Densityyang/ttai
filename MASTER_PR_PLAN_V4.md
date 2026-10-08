# TT-AI MASTER PR PLAN V4
## 三模式、共享执行内核、统一评测的最终推进版

> 原始冻结日期：2026-09-16；当前增量对齐日期：2026-09-22  
> 文档状态：PLAN FROZEN / EXECUTION BASELINE（产品与架构对齐完成）；不是代码完成、验证通过或生产上线证明。  
> 产品模式：QUERY / ANALYZE / BUILD（Metric Lab）。  
> 本轮范围确认：BUILD 首版先做数据库上的自定义计算；CSV/XLSX 上传联合分析、预测产品化、外部数据接入后置。  
> 细致颗粒度：参照用户提供的《TT-AI MASTER PR PLAN V4.md》，保留字段级契约、算法顺序、接口示例、逐PR输入/输出/负路径/DoD和运维细则；精简重复建设，不压缩必要实施规格。  
> 核心裁决：三个产品入口，共用一套执行内核、一套 HITL/checkpoint、一套沙箱与取数 broker、一套 artifact 存储、一套评测运行器。  
> 当前开发流程：owner alignment → bounded C2C → ONE MAIN CODING writer → 停止写入 → 独立只读 review → 获授权的 allowlist Git/Draft/精确 SHA CI → parent review → owner Gate。  
> 当前动作与授权边界：本文件修订属文档控制；docs accepted 不自动授权产品代码修改。产品代码变更只能经 owner 单独批准的 bounded C2C 启动；一旦该 bounded C2C 获授权，主计划不再要求以 docs-acceptance 作为第二次 Gate（除非 owner 明确把该 Gate 设为该 slice 前置）。后续 slice 各自仍需独立 bounded dispatch。  
> 版本说明：本文件仍是唯一 V4 主计划；2026-09-16 七项修订保留于附录 A.4，2026-09-22 当前规则见 §0.5–§0.7 及对应正文；A1–A16 amendment matrix 定位另见 A.6。旧稿仅作历史来源，不创建 V5/V4.3 或竞争主计划。

## 阅读与使用约定

- 产品决策以本轮用户确认和本文裁决为准；附件中的“ALIGNED”“done”不是新的执行授权或当前验证证明。
- DB/PR/CI 的历史观察保留日期，后续 readiness 追加新证据，不回写旧观察。
- 固定产品边界、可校准参数、待 backend 绑定、已实现代码四者分开。
- 每个工作包可以分多个 bounded slices，但不因三个模式复制三个服务或三套基础设施。
- 本文件是后续 CODING_BRIEF 的来源。唯一 coding writer 每次只接收当前 slice 必需的信息；独立 reviewer 按任务检查对应契约及全文一致性。
- 本次只修改本文件并做文档一致性验证，不修改产品代码、测试、AGENTS、launcher 或其他参考文件；不运行产品测试、不访问真实 DB、不 stage/commit/push/更新 PR/触发 CI/merge。

---

# 0. 最终裁决：保留什么、合并什么、后置什么

## 0.1 必须保留

1. 280 canonical + 14 legacy 的已确认定义边界。
2. 原 DB review 结果、CURRENT_STATE 语义、Gold-first 和有效人工修正优先级。
3. QueryGateway、ModelGateway、Semantic Release、SchemaSnapshot、typed plan、receipt。
4. AuthorizationContext、RelationCoverage、SensitiveField、ModelInputPolicy。
5. 单一 LangGraph checkpoint/HITL 状态与当前权限重新核验。
6. ANALYZE 的有界下钻、诊断来源、非因果默认、Analysis Memory。
7. BUILD 新建/实质修改可复用业务定义时的结构化计划卡与业务确认、定义版本不可变、执行忠实度；复用重跑按 §5.2 自动重验，不按模式机械确认。
8. 沙箱无 DB、无网络、无 shell/subprocess、无额外底层权限。
9. V3 的主要编译/候选/grounding 算法、安全门禁、规模验证和发布恢复要求。
10. 单 writer、focused-first、Draft、独立复核和用户最终 merge 决定。

## 0.2 合并建设

| 对象 | 最终建设方式 |
|---|---|
| 三模式编排 | 一套 LangGraph 执行内核，mode-specific planner/政策/能力包 |
| QueryPlan / DiagnosticPlan / CustomMetricPlan | 共用执行 envelope 和 typed steps，保留各自必要语义 payload |
| 三模式 HITL | 一个 HITL Engine；按 reason 和 confirmation policy 区分 |
| ANALYZE / BUILD CodeAct | 一个隔离 runtime；差别放在 capability/resource/library profile |
| AnalysisArtifact / CustomMetricArtifact | 一个 artifact 仓储和访问控制；typed payload/lifecycle 不同 |
| 记忆与成功经验 | 共用 artifact/证据引用；不新建自动信任的 Experience Store |
| 三模式 benchmark | 一个 case registry、runner、manifest、oracle/断言机制和报告 |
| Query / Diagnostic / Build 结果对比 | 同一次评测报告中的切片；不是固定三轮全量运行 |
| 数据源扩展 | readiness 和明确的 source binding 变更，不新建“Source Expansion 服务” |

三个模式需要不同的行为断言，不需要三套评测平台。共享安全检查只执行必要的去重集合；模式边界不同的真实行为仍分别验证。

## 0.3 恢复早期计划的产品意图，替换旧技术手段

恢复 BUILD 创建/实质修改可复用定义的产品流程（不作为全部 QUERY 或 SAVED 重跑的流程）：

~~~text
自然语言需求
→ 结构化计算计划卡
→ 用户确认/修改
→ ConfirmedCustomMetricPlan / immutable Definition Version
→ 用户要求执行时建立 Per-run Execution Binding 并重验
→ 受治理取数与计算
→ 验证
→ 结果、证据与可显式保留的 reusable definition
~~~

不恢复：

- 低置信后退化为全库探索。
- 每个请求自动 COUNT/DISTINCT 假设探测。
- 多策略全部执行后多数投票或“锦标赛”选业务真值。
- 未校准的 0.7/0.4、80% 接受率、70% 成功率等作为生产门槛。
- 第二套 supervisor、通用 GraphRAG、Self-RAG/CRAG 自由循环。
- 依赖自然语言公式、代码意图声明或 is_locked=true 就声称执行契约已锁定。
- 仅凭 multiprocessing/resource/AST/正则就宣称沙箱边界完整。
- 每次成功就自动信任并复用历史 SQL/修复经验。
- A/B/C 多组组件消融默认进入每次 PR 或每次发布。
- 旧 SSH 主机、端口、数据库名和 todos=done 作为当前环境事实。

## 0.4 本轮不做

用户已选择“先数据库计算”。

本主 PR 不交付：

- CSV/XLSX 上传与数据库联合分析。
- 自动预测产品、自动模型搜索、训练平台、模型托管/MLOps。
- ExternalDataGateway、任意互联网数据抓取。
- 定时订阅、异常推送、创建业务任务、发送通知或修改业务系统的 Action Plane。
- Agent 自主 canonical 发布或人工修正 Gold。
- Redis、业务 ResultCache、第二套向量/评测/观测平台。
- Kubernetes、跨主机 HA、完整多租户平台、领域微调或自建推理集群。

What-if、复杂比率、cohort、窗口与跨表组合，首版限定在批准数据库资源、已确认计算契约和可验证方法内。不得为“能力更强”提前安装大量尚无验收用例的模型/算法包。

---

## 0.5 2026-09-22 当前修订锚点

**CURRENT PRODUCT RULE**：mode 由本次用户意图决定；算术、比较、执行次数、SAVED 身份都不决定 mode 或 canonicality。QUERY 可返回正式事实、正式批准计算、临时 noncanonical AD_HOC 计算或已有 SAVED 定义的结果；ANALYZE 负责诊断解释；BUILD 创建/实质修改可复用业务语义。模型只能建议切换，不能提升能力。

- Mode / Definition lifecycle / Authority 分开；Method Authority != Metric Authority。正式身份及权威 binding 决定 canonicality，公式相等、模板登记、确认、结果收藏或定义留存均不能 canonicalize。
- Custom Definition 按确认、保留、治理分别建模，原对象 invariant = noncanonical。正式发布产生/关联独立 canonical identity，保留来源链接，不改写原对象及历史结果。
- Immutable Definition Version/hash 只覆盖稳定业务语义与 Parameter Contract；Per-run Execution Binding、当前权限/release/data/budget 与 receipt 分开。合法参数重绑定不新建定义版本，也不因参数变化本身机械 HITL；独立风险门禁仍有效。
- save execution/result artifact 与 retain/save reusable definition 是不同操作。临时计算的运行/审计记录和结果 artifact 不自动创建可复用定义。
- HITL 按 clarification / business confirmation / permitted risk decision 分类；需要继续执行的 Resolve/Confirm 应恢复到当前重验 → compile → governed execution。
- 本文已在模式表、字段契约、共享 actions、实施卡及验收矩阵原位修订旧规则；附录 A.4 的旧强制 SAVED 重跑确认只保留为 SUPERSEDED 历史。

**CURRENT IMPLEMENTED STATE — 2026-09-22**：

| 层次 | 当前结论 | 不代表 |
|---|---|---|
| P4-S2 engineering slice | DONE；CANONICAL_APPROVED_COMPUTE_KERNEL_READY；PR #41 Draft/Open/unmerged | 已合并或已生产启用 |
| P4-Q real-evidence acceptance | P4-Q-CONTRACT_READY；NOT P4-Q-PASS | fixture/local 已达真实验收 |
| Production canonical approved compute | NOT_ENABLED | 内核/CI 通过即可启用 |

P4-S2 DONE != P4-Q PASS != production enabled；即使未来 P4-Q PASS，也不自动生产上线。

**IMPLEMENTATION-PENDING PRODUCT CONTRACT**：QUERY noncanonical AD_HOC formula execution、完整 typed clarification/decision resume、Custom Definition lifecycle、SAVED rerun revalidation、完整 BUILD integration。以上是已对齐的产品规则，不冒充已实现能力。

自 2026-09-16 后的实现进展及证据范围见 §1.6；外部权威/物理绑定缺口见 §8.19；H19 分类见 §8.20。当前单 writer 控制流程见 §8.11；后续推荐依赖见 §8.18，不固定未来 PR 粒度。

## 0.6 按 claim 类型选择 authority source

不存在一条覆盖所有事实和产品规则的全局线性优先级。

| Claim 类型 | 真源与处理方式 |
|---|---|
| 现在实现了什么 | 当前工作区可核验源码与执行路径；live code 不能覆盖 owner 的目标产品规则 |
| Git/PR/CI 当前状态 | 实际仓库、远端 PR、绑定精确 SHA 的 CI；快照/回传不得替代实时核验 |
| 产品应该如何工作 | 最新 owner alignment；已接受 C2C、5+2 consistency guards 及本轮 A1–A16 amendment matrix（§0.7）一并生效 |
| 正式指标权威及生产绑定 | 正式 business/DB/Gold/Semantic publication 与可信 binding 证据；本地模板/代码不能补造 |
| 历史 DB/测试/验收观察 | 原始带日期、范围和来源的记录；新观察追加，不改写历史 |
| H19 风险/工程发现 | 可读证据、复现、可达性与独立核验状态；分类/优先级不等于已证实 defect |

实现与 owner 规则冲突时登记 IMPLEMENTATION GAP。未独立核验的 handoff 结论保留来源标签；正式生产绑定未知时记录 EXTERNAL / GOVERNANCE BLOCKER。

## 0.7 Amendment matrix 冻结（A1–A16，2026-09-22 增量对齐）

**CURRENT PRODUCT RULE — FROZEN**：以下矩阵是本轮 owner 冻结的完整修订面；每项与正文落点一致，正文任何冲突处以本节与对应正文为准。历史矛盾项按 §8.11 与附录 A.4 显式保留为 HISTORICAL / SUPERSEDED，不得据旧实现覆盖新规则。

| # | 冻结规则 | 正文落点 |
|---|---|---|
| A1 | MODE 由本次用户意图决定，与 definition lifecycle、authority 独立。QUERY 含直接事实检索、直接比较、同比环比、批准排名、canonical 计算、临时 AD_HOC 计算、执行已有 SAVED 定义；ANALYZE = 解释/调查/诊断；BUILD = 创建或实质修改可复用业务语义。比较与算术不决定 mode；执行次数/一次/保存/复用/SESSION/SAVED/流行度/确认不决定 canonicality | §0.5、§2.1、§2.1.1 |
| A2 | 一次性执行若解析到正式 canonical metric identity 并使用其正式发布结果或权威定义/binding，结果可具 canonical authority；未采用正式身份的临时 A/B 为 noncanonical AD_HOC；公式等价不能 canonicalize；same formula != same metric identity != same authority | §0.5、§2.1.1、§3.8.1、§3.8.2 |
| A3 | Save 必须指名对象：save execution/result artifact（artifact 操作；不建定义、不选 BUILD、不 canonicalize）≠ retain/save reusable definition（不自动执行；Confirmed 语义未变不重复语义确认）≠ create/materially modify definition（BUILD）；EXECUTE SAVED DEFINITION 的 mode 由当前意图决定；QUERY 复用不取得 BUILD/CodeAct 权限 | §2.1.1、§5.2.1、§7.3、§9.2 |
| A4 | Custom Definition 多轴（Confirmation / Retention / Governance / Authority），不是一条线性状态链；原对象 invariant = noncanonical；Confirmed + SAVED + GOVERNANCE_CANDIDATE + noncanonical 合法；正式治理另建/关联独立 canonical identity 并保留 provenance linkage，不原地改写、不追溯历史结果权威；AD_HOC QUERY 不进入 definition lifecycle | §4.1.1、§5.2.1、§5.2.3、§9.2.1 |
| A5 | 分离 Immutable Definition Version / Parameter Contract / Per-run Execution Binding；auth revision、active release、data snapshot、具体 month/store_scope、budget、freshness、普通重跑时间不新建定义版本；契约内参数重绑定不机械业务确认；独立风险门禁仍有效 | §0.5、§4.1.2、§4.5、§5.2.2 |
| A6 | 实质语义变化（分子/分母/population/去重/业务时间/join/NULL/zero/业务显著单位精度/Parameter Contract 扩展）需新 Draft/version 与新业务决定 | §4.5、§9.2.1 |
| A7 | 重验分支：EXECUTE / CLARIFICATION / BUSINESS-RISK DECISION / DENY / UNAVAILABLE；人类确认不能修复缺失授权 | §4.2、§5.2.2、§7.5.1 |
| A8 | Semantic resolution vs invention：显式公式/关系 + 唯一解析输入可受治理执行；唯一权威实质有效含义自动解析；两个以上实质不同含义最小澄清；无权威含义且用户未提供则不发明 | §3.8.1 |
| A9 | 在 AD_HOC 与 Custom Definition 分化前冻结共享 typed calculation semantic core（CalculationSpec/ExpressionSpec/ParameterSpec/Parameter Contract/input roles/unit-precision/rounding/NULL-zero/provenance）；INVARIANT：共享语义表示 != 共享 authority | §3.8.2、§8.18 |
| A10 | Method Authority != Metric Authority；模板/注册/allowlist 只能授权方法实现，不能单独授权结果为 canonical business metric | §3.8.2、§8.5.2、§9.2.1 |
| A11 | P4-S2 规则 path-scoped：仅 selected catalog-bound approved-compute path 内 target C 必须由正式 binding 计算产生、不得直接 fetch 替代；正式 Published Gold/已发布 canonical 可经自身 governed reader 读取；内部 dependency fetch 不 grounding，独立请求同指标可另契约 grounding；scalar-only/无 nested DAG/单 bound output 是 V1 限制而非 QUERY 全局限制 | §8.5.2、§9.2.1 |
| A12 | 状态分层：P4-S2 DONE != P4-Q PASS != production enabled；P4-S2 = 完成 bounded slice、exact-SHA CI accepted、PR #41 Draft/Open/unmerged；P4-Q 当前 CONTRACT_READY；production canonical approved compute NOT_ENABLED；实现待完成项见 §0.5 | §0.5、§1.6、§8.5、§8.18 |
| A13 | 测试证据 provenance 分开：local/closeout full-unit 1414 passed / 2 skipped；CI pytest collection/log 1416 passed / 62 skipped；不作同一运行比较 | §1.6 |
| A14 | H19 表分离 FINDING / EVIDENCE SOURCE / VERIFICATION STATUS / CLASSIFICATION-PRIORITY / PRODUCTION-ENABLEMENT DISPOSITION；分类不等于已证实 defect；未独立复核标 handoff/recon finding — pending dedicated verification；H19c 只描述 accounting seam；H19i 明确 production enablement 前必需 integrated evidence | §8.20 |
| A15 | 流程：owner alignment → bounded C2C → ONE MAIN CODING writer → CODE_COMPLETE/STOP_MUTATING → 独立只读 review → 授权 closeout/exact SHA/CI → owner Gate；Luna/Astra 仅历史；依赖逻辑见 §8.18；P4-S2 production activation 与 P4-Q real-evidence PASS 是独立门禁线；不冻结未来 PR 粒度 | §8.11、§8.18、§10.3 |
| A16 | 全文 stale 词扫描：BUILD / BEFORE_EXECUTION / comparison / ANALYZE / SAVE / SAVED / AD_HOC / SESSION / one-off / canonical / authority / parameter / definition version / P4-Q PASS / P4-S2 / direct fetch / dependency fetch / Luna / Astra / Slice3 / registry / template / HITL / resume；矛盾历史陈述必须显式标 HISTORICAL 或 SUPERSEDED | §8.11、A.4、A.5、A.6、§10.4 |

---

# 1. 已确认基线：保留原结果，不冒充实时状态

## 1.1 关键路径

| 用途 | 路径 |
|---|---|
| 历史计划快照目录（2026-09-16） | E:/平台开发/tt-ai-main |
| 实际 Agent worktree | E:/平台开发/ttai-pr07a-next |
| 业务架构快照 | E:/平台开发/tt-intelligent-main |
| 原总 review | E:/平台开发/tt-ai-main/TT_AI_TOTAL_PR_REVIEW_2026-09-10.md |
| V3 计划 | E:/平台开发/tt-ai-main/MASTER_PR_PLAN_V3.md |
| 当前唯一维护主计划 | E:/平台开发/ttai-pr07a-next/MASTER_PR_PLAN_V4.md |
| 历史主计划来源（不在本任务同步维护） | E:/平台开发/tt-ai-main/MASTER_PR_PLAN_V4.md |

业务架构快照不等于可提交的 Gitee clone。当前连接工作区名 tt-ai-main 映射到实际 Git worktree E:/平台开发/ttai-pr07a-next；历史计划目录不代替当前维护文件。

## 1.2 工程观察记录

**HISTORICAL OBSERVATION — RETAINED FOR PROVENANCE**：以下沿用 2026-09-14 核验记录；原 2026-09-16 文档收敛未刷新 GitHub 或数据库。2026-09-22 工程观察另列 §1.6，未重新访问 DB。

| 项目 | 原核验记录 |
|---|---|
| branch | agent/pr07a-enterprise-db-contract |
| HEAD | a96898d |
| PR | Densityyang/ttai #28，OPEN / DRAFT |
| base | feature/nl2sql-v3-production |
| CI | run 34175090873，五项 SUCCESS |
| dirty Slice3 | 7 modified + 2 untracked |
| Slice3 状态 | 已知范围，当前差异尚未完成独立验证 |
| Gitee | 真实 clone 尚待确认 |

CI green 只覆盖它验证的 commit。执行前若发现新 HEAD 或 Slice3 已完成，核对对应 diff/CI 后复用证据，不退回旧基线。

## 1.3 指标全集

~~~text
D_canonical = 280
raw = 180
derived = 56
external = 44

D_legacy_compat = 14
Q_agent = 294
~~~

- Q_agent=294 保持指本轮正式定义与兼容标识集合。
- 自定义 metric IDs 使用独立 namespace，不计入上述数字，不覆盖 canonical/legacy 标识。
- 新 canonical 只能经业务/DB/Gold 治理及新 Semantic Release 产生。
- 缺数据不等于缺定义；合法定义不因暂无结果而 retired。
- External 44 和 legacy 14 保持 published-only。
- Legacy exact code 不隐式 alias 到不同时间粒度，尤其 area_day → area_month。
- Complaint 是 reference domain；installation 是 transfer domain；总验收需要跨域证据。

## 1.4 DB 观察快照

原观察日期：2026-09-14。

原材料未提供可用于重放的 DB snapshot ID。因此只保留观察日期和来源，不补造 snapshot_id，不将这份记录称为可重放数据集。

| 项目 | 原观察值 |
|---|---:|
| Gold rows | 151,985 |
| observed codes | 231 |
| observed canonical | 217 |
| observed legacy | 14 |
| unexplained observed codes | 0 |
| canonical-but-unobserved | 63 |
| success | 147,916 |
| partial | 4,013 |
| no_data | 56 |
| employee rows | 60,417 |
| distinct employees | 1,492 |
| source_batch_no 非空 | 73,070 |
| source_system 非空 | 73,070 |
| gold_metric_metadata | 0 |
| gold_metric_dependency | 0 |

| Dimension shape | Rows |
|---|---:|
| all | 1,016 |
| area | 9,079 |
| team | 40,767 |
| area_team | 40,706 |
| area_team_employee | 60,417 |

观察到的 category：all、fttr、gigabit、standard。它不是每个指标都支持全部 category 的证明。

未观察到结果的 63 项：

~~~text
complaint area_month     16
installation area_month 12
weak-light              19
inspection               6
repair_service           9
external-derived         1
~~~

Weak-light 记录：

~~~text
bronze_weak_light_onu_statistics = 0
silver_weak_light_onu_statistics = 0
consumer_home_broadband_customer_count_snapshot = 0
silver_post_installation_weak_light = 134
weak-light Gold rows = 0
~~~

不能用相邻表非空证明 weak-light 已 ready；不能将 source 缺失写成业务值 0。

连接观察保持：DB=tt，PostgreSQL 18.1，审查身份 postgres，SSH tunnel，只读事务并 ROLLBACK。它们不构成产品部署连接或生产权限证明。

后续新观察记录至少含 snapshot/evidence ID、observed_at、来源、probe 版本、definition fingerprint、集合/状态/维度/category 分布、异常和证据引用。新观察追加，不篡改旧观察。

## 1.5 Status 与时间

原观察状态是 success/partial/no_data；已审查源码支持 failed，实际 adapter 以绑定 schema-supported status 为准。不把 failed 写成已在该次 DB 观察到。

~~~text
partial != success
missing row != no_data
zero != no_data
NULL != zero
~~~

八项 CURRENT_STATE 已由用户/DB 同事确认。对应两个模板 current_state=true、default_account_period_raw=20000101：

~~~text
home_broadband_complaint_per_10k_yoy_reduction_month
home_broadband_complaint_per_10k_reduction_challenge_month
home_broadband_complaint_per_10k_month
home_broadband_repeat_complaint_rate_month
home_broadband_repeat_complaint_per_million_reduction_challenge_month
home_broadband_repeat_complaint_per_million_per_10k_month
home_broadband_repeat_complaint_per_million_yoy_reduction_month
single_fault_post_receipt_reinvestment_per_10k_customer_month
~~~

Planner/ContextBundle 仅消费 CURRENT_STATE。PublishedMetricReader/adapter 才映射 stored_time_value=2000-01-01；内部 receipt 同时记录 semantic_time、stored_time_value 和真实 published/computed_at。

- 历史月份请求不能命中占位值。
- 不从 _month 后缀推断真实月份。
- 不将 computed_at/import_time 自动当业务期间。
- 该映射也适用于 BUILD 的语义预览，不能经 profiling/memory 把存储占位值重新喂给 Planner。
- 不对其他合法 calendar 数据全局替换 2000 年。
- 八项不再作为未确认时间异常；普通质量门禁保留。

---

## 1.6 2026-09-22 accepted closeout baseline

**CURRENT IMPLEMENTED STATE / dated evidence**：本表记录本次修订采用的 post-P4-S2 基线；不把历史测试描述为本轮重新运行。

| 项目 | 记录 | 来源/范围 |
|---|---|---|
| Workspace / root | tt-ai-main / E:/平台开发/ttai-pr07a-next | 连接 workspace_info、实际 Git root 核验 |
| Branch | agent/v4-p4-s2-canonical-approved-compute | 本轮修改前 live Git |
| HEAD | 37409ff0e58db4983bf23c8b17b0d04c71742ef6 | 本轮修改前 live Git |
| Tracked / staged | 0 tracked dirty；0 staged；主计划为现有 untracked reference | 本轮修改前 live Git；其他参考文件保留 |
| PR | [#41](https://github.com/Densityyang/ttai/pull/41)，Draft/Open/unmerged | accepted post-P4-S2 closeout HANDOFF；本轮未提供可复核的 live remote GitHub 核验记录 |
| CI | [35674533302](https://github.com/Densityyang/ttai/actions/runs/35674533302)，SUCCESS，绑定上述完整 SHA | accepted post-P4-S2 closeout HANDOFF；本轮未提供可复核的 live remote GitHub 核验记录 |
| Local / closeout full-unit | 1414 passed / 2 skipped | accepted handoff 与 PR description；未在本轮重跑 |
| CI pytest | 1416 passed / 62 skipped | 上述 CI quality job 的 pytest 日志；不是本地 full-unit 同一次运行 |
| Focused P4-S2/P4-Q | 415 passed | accepted handoff 与 PR description；未在本轮重跑 |
| Adjacent calculation/runtime | 196 passed | PR description 的 closeout 记录；未在本轮重跑 |
| Static | ruff clean；pyright 0 errors；diff check clean | accepted closeout 记录及 CI 对应步骤；不代表本次运行产品检查 |

两组测试计数分别归属各自 scope/source，不合并、不用原始数量差推断覆盖优劣。

自 2026-09-16 后，本工作区 Git history 可定位以下实现增量；提交存在仅证明已实现代码，不证明生产绑定/启用或整个工作包完成：

| 提交 | 可定位实现 |
|---|---|
| 48546d3 / 3c846e0 / 7abef4c | typed runtime factory、request-scoped activation seam、确定性 query plan authority |
| f96665c | typed facts / grounded answer closure |
| a148449 | P2-S2 model-input / observability egress policy |
| dfdad72 | P2-S3 trusted authorization carrier seam |
| 85d2611 / f100159 | P3 effective published result / published metric reader contracts |
| e95a6fb | P4-Q acceptance harness |
| 37409ff | P4-S2 canonical approved-compute kernel |

完整 typed resume、AD_HOC、Custom Definition 与 SAVED rerun 等仍按 §0.5 标为实现待完成。P0/Slice3 的旧工程理由保留作历史，不能由旧快照重新把它排为当前下一 slice。

---

# 2. 一个内核、三个产品模式

## 2.1 模式边界

| 维度 | QUERY | ANALYZE | BUILD |
|---|---|---|---|
| 用户目的 | 获取答案/结果，包括直接比较、同比环比、批准排名 | 解释差异原因、调查和有界诊断 | 创建或实质修改可复用业务语义 |
| 默认行为 | 确定性优先，Fast/Standard | 有界自主诊断 | 先形成计划卡 |
| 数据权限 | 当前授权 | 同一授权，不增权 | 同一授权，不增权 |
| 能力包 | 正式问询、批准比较/排名/有界明细；目标支持受治理 AD_HOC 和已有定义执行 | 多层下钻、诊断统计，可复用受治理计算 | 自定义分母/cohort/窗口/组合/计划级 join 的定义创作 |
| CodeAct | 禁用 | 受控可选 | 受控可选，额度可更大 |
| HITL | 未解决歧义或明确允许的风险决策 | 重要分支、成本/敏感性变化等命名决策 | 新建/实质修改定义的业务确认；风险决策独立检查 |
| 输出 | 带明确 authority/provenance 的正式、AD_HOC 或 custom 结果 | AnalysisArtifact，可引用保留各自权威的事实 | Custom Definition 及适用的 CustomMetricArtifact |
| 正式发布权限 | 无 | 无 | 无 |

模式按本次用户意图，由明确入口或用户接受的 server-owned mode-switch 请求确定。算术、comparison、保存结果、保留定义或 SAVED 身份本身都不选择 BUILD；普通比较不自动进入 ANALYZE。模型只能建议切换，不能默默升级 mode/capability；QUERY 复用 SAVED 定义也不能取得 CodeAct 权限。所需能力不在当前 mode 内时，返回对应限制或提出显式切换建议。

- QUERY 失败、低置信、缺数据不自动升级 ANALYZE。
- ANALYZE 定义新指标时可产生 BUILD preview，执行前必须明确接受 BUILD 能力/成本包并确认计算契约。
- 可以用一次清晰的“进入实验室并按此计划执行”操作合并模式接受和计划确认；不能省去其中的含义。
- 选择 ANALYZE 后，授权范围内的正常人员/工单下钻不逐层弹窗。
- 用户确认不能批准组织越权、ModelInputPolicy 禁止的数据外发或底层特权。
- QUERY/ANALYZE/BUILD 是唯一共享 ProductMode 词汇，由本次用户意图决定；`QueryPlan.intent`（metric/trend/comparison/ranking/detail）只是执行形态意图，不决定 ProductMode。comparison/ranking/trend 操作本身不等于 ANALYZE；共享 ProductMode 复用现有 P4-Q observed-mode 词汇，不再维护重复 mode 字面量。

### 2.1.1 Mode / Definition / Authority 示例矩阵

以下是 CURRENT PRODUCT RULE；实现覆盖范围按 §0.5 判断。

| 用户目标/动作 | 本次 mode 或操作性质 | 可复用定义/生命周期 | Authority |
|---|---|---|---|
| 临时“销售额 / 营业门店数”且不采用正式指标 binding | QUERY | 一次 Execution Spec；不自动创建 Custom Definition | noncanonical AD_HOC |
| 查询正式发布的每店销售额或执行其正式 approved binding | QUERY，即使只执行一次 | 正式定义/身份 | canonical，仍须通过全部门禁 |
| “定义门店产出 = …” | BUILD | Draft/Confirmed；SESSION/SAVED 分轴 | 原 custom 对象始终 noncanonical |
| 获取已有 SAVED 定义结果 | QUERY | 原 immutable definition version + 新 Execution Binding | 仍 noncanonical |
| 用该定义解释差异原因 | ANALYZE | 可复用原定义 | 不因诊断或复用提升权威 |
| 收藏/保存执行结果 artifact | Artifact 操作，不因此切 mode | 不创建可复用定义 | 保留原 authority/provenance |
| 将已确认定义从 SESSION 保留为 SAVED | Definition retention 操作 | 不重算、不因保留范围变化重复业务确认 | 不提升权威 |
| 提交 GOVERNANCE_CANDIDATE | 治理候选操作 | 原定义保留，治理轴变化 | 仍 noncanonical |
| 正式 business/DB/Gold/Semantic publication | 外部正式治理 | 创建/关联独立 canonical identity | 新正式身份取得权威，不改写原 custom 或历史结果 |

重复执行不意味着 canonical；算式与正式公式相等也不能替代正式身份与 binding。ANALYZE 内进行计算不因此变成 BUILD。

## 2.2 共享组成

~~~text
三模式入口
→ Mode/Capability/Budget Policy
→ Authorization + RelationCoverage + SensitiveField
→ Semantic Release / SchemaSnapshot / Artifact References
→ mode-specific planning strategy
→ shared PlanValidator / HITL
→ shared PlanCompiler
→ shared Typed PlanExecutor
    ├─ PublishedMetricReader → QueryGateway → Effective Published Result
    ├─ DataRequest / SQLCandidate → QueryGateway → FrozenDataset
    ├─ TrustedCalculation
    ├─ ProductCapability → approved backend adapter
    └─ Sandboxed CodeAct
→ shared ResultVerifier / ModelInputPolicy / Grounding
→ typed Artifact / Receipt / Audit / Checkpoint
~~~

不同策略和 typed payload 可以分模块；不因三个入口复制三套 supervisor、executor、HITL、sandbox 或存储服务。

## 2.3 保留的技术选择

- Python 3.13 / FastAPI / Pydantic v2 / LangGraph / PostgreSQL / SQLGlot / uv / Docker Compose。
- QueryGateway 为所有 Agent 业务 SQL 的出口。
- ModelGateway 为受控模型调用入口；provider adapter 只转换协议。
- control PG 保存语义、策略、runs、audit/outbox、artifact 元数据与 typed JSON；必要的大文件使用受控共享存储，具体职责见 §5.4.1；checkpoint PG 负责恢复。
- pg_trgm/FTS 和 pgvector fallback 复用，不恢复危险 FAISS 反序列化。
- Langfuse 可选导出；control audit 为真源。
- 不新增 PydanticAI、LiteLLM、LlamaIndex、Vanna、DB-GPT 或第二评测栈。
- 旧代码可作为迁移材料，不能作为生产 fallback。

## 2.4 部署拓扑与网络隔离

~~~text
Client / TT-admin
    |
    v
Nginx（TLS / request-id / rate / body limit / SSE no-buffer）
    |                             |
    v                             v
  API-A                         API-B
    +-------------+---------------+
                  |
          shared Typed LangGraph
          /          |           \
   ModelGateway  QueryGateway   typed backend adapter
       |             |                 |
 approved models  business PG         TT-api
                  read-only

control PG: semantic / schema / policy / run / audit / outbox / artifact metadata + typed JSON
checkpoint PG: graph state / HITL / idempotent recovery
isolated worker: only authorized frozen datasets and scoped output
~~~

| 网络/组件 | 允许职责 | 禁止 |
|---|---|---|
| edge_net | Nginx与API通信 | Nginx访问PostgreSQL |
| control_net | API、control PG、批准ops任务 | 模型/Sandbox获得control连接 |
| checkpoint_net | API与checkpoint PG | 业务取数经checkpoint旁路 |
| business_external_net | QueryGateway及批准评测访问业务库 | 普通HTTP/模型节点直连DB |
| Sandbox job | 独立数据输入/临时输出/受控运行环境 | 任意网络、DB、其他job、宿主文件和socket |

- control/checkpoint职责和持久卷分离；双API没有仅本实例可恢复的业务状态。
- migrate、schema-snapshot、semantic-indexer、semantic-validate、benchmark、backup-check、restore-test为one-shot任务。
- API启动不自动迁移、全库抓schema或重建索引。
- API/PG不开放不必要的宿主机端口；产品不使用审查用SSH tunnel作为业务连接。
- Sandbox创建/销毁由受控运行基础设施管理；不向Agent或生成代码授予Docker socket/host shell。

## 2.5 运行profile与readiness

~~~text
EXECUTION_MODE = infra-dev | product
CODEACT_MODE = disabled | sandboxed | unsafe-dev
TRUSTED_CALCULATION_ENABLED = true | false
~~~

这些是目标配置语义。实施时与现有SERVICE_MODE等设置做显式映射、兼容与校验，不能新增一套未被读取的“纸面配置”。

| Profile | 允许 | 必须阻止 |
|---|---|---|
| infra-dev | 固定样例、fake provider、受控基础设施验证 | 以stub证明企业正确率 |
| product + disabled | 三模式中不需要CodeAct的已批准执行 | 任意代码执行fallback |
| product + sandboxed | 仅ANALYZE/BUILD且能力门禁通过的隔离执行 | QUERY调用CodeAct；未验证runtime放行 |
| unsafe-dev | 独立本地样本探索 | product启动、发布benchmark、canary和生产流量 |

- product共享必要依赖包括持久化checkpoint、control audit、兼容的release/schema、可信权限及相关数据绑定。
- MODEL_REQUIRED等必要依赖遵守已冻结部署profile，不由请求自行关闭。
- /readyz只在必要条件成立时返回ready；/healthz存活不能替代它。
- Sandbox、特定模型或artifact子能力不可用时，准确报告受影响范围；不能误报全部能力可用。
- optional embedding/Langfuse故障可降级，但不能改变权限、事实或审计真源。
- QUERY的CodeAct禁止是mode硬规则，不受全局sandboxed配置放开。

## 2.6 连接池、队列和实例预算

| 项目 | Bootstrap |
|---|---:|
| API replicas | 2 |
| Uvicorn workers per replica | 1 |
| active SQL per API | 4 |
| waiting SQL per API | 8 |
| acquire wait | 3s |
| aggregate active SQL ceiling | 8 |
| business pool_size | 3 |
| business max_overflow | 2 |
| pool_timeout | 3s |
| pool_recycle | 900s |
| connect_timeout | 3s |

- 当前进程内governor不等于通用跨进程租约；增加worker/replica前必须重新核算总额。
- pool上限、SQL活动数和等待队列是不同限制，均应观测。
- control/checkpoint、业务连接和ops连接分别核算，不把业务pool参数当所有数据库的总预算。
- 队列必须显式有界；超容量/等待上限返回CAPACITY_EXCEEDED和适用的Retry-After。
- 等待不能突破当前请求剩余deadline；取消后释放排队位置、lease和连接。
- 模型/HITL/代码计算期间不占用业务查询连接。
- 各模式可以有不同配额，但合计不能突破系统总容量；最终配额在统一校准中冻结。

---

# 3. 语义、数据访问与确定性算法

## 3.1 Semantic Authoring / Release

输入：

- Gold canonical YAML；
- 批准的 producer/current-state 配置；
- 14 项 legacy definitions；
- approved aliases/QA；
- approved relation/column/join/sensitivity/source policies。

~~~text
输入指纹
→ deterministic parser（含 anchors/merge）
→ Typed IR
→ validation
→ materialization
→ candidate release
→ release checks
→ atomic active pointer
~~~

要求：

1. 不用正则行数代替 YAML 解析计数。
2. 不为适配 complaint-only schema 改写正式公式。
3. Catalogue identity、生命周期、source readiness 和 compute eligibility 分离。
4. 280 项全识别不等于全部有数据或全部可重算。
5. 未知依赖、非法 join、缺失表列和缩写产生明确 issue。
6. semantic.md/QA 是 authoring/migration input，运行时不以 Markdown 为第二权威。
7. 同输入产生稳定 checksum；区分原文件、normalized definitions、parser/mapping 和 release 指纹。
8. version 使用 sequence/identity；active pointer 加锁、事务切换；失败不改变 active。
9. release checksum 绑定 schema 兼容范围、approved edges 和 parser/embedding profile。
10. 新请求绑定 active release；执行中不静默切换语义版本。

复用 semantic_releases/assets/aliases/edges、schema_snapshots、release_pointers、validation_issues、source_freshness 等现有职责，不为三模式复制表组。

## 3.2 Context Compiler

~~~text
policy-before-retrieval
→ canonical/legacy exact code
→ approved alias exact
→ normalized lexical / pg_trgm / FTS
→ vector fallback
→ approved examples / permitted historical artifact references
→ bounded ContextBundle
~~~

- exact identity 不能被相似度覆盖。
- 检索前过滤权限，不能先召回敏感内容再决定能否显示。
- QUERY/ANALYZE 使用 release 内 approved join 路径。
- BUILD 可以在能力包内提出计划级 join，但不修改全局 ApprovedJoinGraph。
- Memory 只提供带来源/时效的历史上下文，不能覆盖正式定义或当前事实。
- 未解析 slots、冲突和不唯一的业务口径进入 clarification/HITL。
- Embedding 不可用时使用已有 lexical 能力，不制造证据。
- source_confidence 或 RRF 分数不是执行许可，也不是业务正确率。

Query bootstrap context：Fast 2K tokens/6 evidence/2 relations/0 hop；Standard 6K/12/5/1。旧 Deep 10K/20/8/2 仅保留为历史参考，不直接成为 ANALYZE/BUILD 上限。

## 3.3 Schema exploration / profiling 的裁决

- QUERY 不恢复常驻 COUNT/DISTINCT 假设探测。
- 默认预览从授权 SchemaSnapshot、语义元数据和已批准统计中完成。
- BUILD 可浏览更宽的已授权 schema 子集，不等于访问全数据库系统 schema。
- 当前首版 profiling 仅作为注册的有界 DataRequest：如类型/缺失率/批准的聚合或受限值域统计。
- profiling 仍经过授权、RelationCoverage、SensitiveField、QueryGateway、预算和 receipt。
- 受治理的有界 profiling 不因“需要实际 profiling”本身机械触发 HITL：当它处于当前授权内、source/relation 访问已获许可、操作位于已批准的探索契约内、当前 budget/capability/policy 允许，且未引入新的实质业务/风险决定时，可继续执行，再形成正式计算计划卡。
- 仅当 profiling 引入新的命名实质决定时才进入同一 HITL 引擎，适用时如 exploration-scope decision、material cost escalation、sensitivity/risk decision 或其他 policy 允许的显式人类决定。
- 人类确认不能增加数据授权、不能放行禁止的 relation/source、不能覆盖 ModelInputPolicy、不能越过 server policy ceiling。
- 不允许以“做计划”为由执行未确认的 BUILD 业务取数、CodeAct 或成本无界探测。
- 预览成本可明确为静态估算；真正执行前还要 EXPLAIN，超限暂停，不扩大范围。
- 用户确认探索计划不等于确认最终新指标口径。

探索是 BUILD 内的受控阶段，不新增第四模式或独立探索 Agent。

## 3.4 统一权限与 RelationCoverage

最小 AuthorizationContext：

~~~text
subject
resource_scope
org/person_scope
provenance
authorization_revision
~~~

revision 可以由有效权限 snapshot/config/policy hash 得到，不要求数据库天然存在同名字段；也不能用不反映实际权限变化的固定 hash 充数。column_scope 等原生字段待真实 backend 证据确认；现有列 allowlist 不取消。

Relation metadata 至少表达：

~~~text
coverage_root_org
coverage_org_level
row_org_field
minimum_query_org_level
detail_sensitivity
~~~

- 先判断 resource/relation 是否可访问，再取数。
- WHERE 只能缩小合法范围，不能创造 relation 访问资格。
- 区县用户不能临时给 city-level relation 加 WHERE 就越过覆盖门槛。
- 区域 scoped view/resource 必须由 backend 正式发布相应契约。
- sibling 组织不能相互读取；不从表名或 dimension_type=all 猜“市级”。
- 每个 join source 都独立通过权限/覆盖检查。
- BUILD 的能力包更宽，但数据授权仍是相同用户的实际授权。

## 3.5 SensitiveField / ModelInputPolicy

“用户能查”与“数据能送给哪个模型”分别判定。

任何准备发送给外部模型的信息，都必须先经过 ModelInputPolicy；覆盖用户输入、ContextBundle、schema、列名、sample rows、feature metadata、工单/自由文本/含 PII 的值、计划与代码生成上下文、分析证据、CodeAct 输出/错误/图片、memory、embedding/reranking、telemetry。

~~~text
Plan / schema / metadata / samples
→ ModelInputPolicy → ModelGateway → plan / code generation

Authorized Dataset → CodeAct Sandbox → ResultVerifier

CodeAct output / analysis evidence
→ ModelInputPolicy（再次执行）→ ModelGateway → synthesis / answer
~~~

Authorization 决定用户能否读取；ModelInputPolicy 决定已读取信息能否发送给当前模型/provider。生成前放行不代表后续结果可外发：ANALYZE、BUILD 的解释/总结调用必须针对实际输出再次检查，其他外部模型调用同样适用统一前置门禁。

- provider fallback 重新核验政策。
- 未批准的字段/provider 组合不外发。
- 不以 ANALYZE/BUILD 模式选择代替外发许可。
- 代码输出继承输入敏感性，不能由生成代码自行宣称已脱敏。
- 无法证明可外发的内容保留在授权 artifact，不喂给模型。
- 权限撤销后先停止无权访问，不是只打 stale 标记仍暴露。
- 具体字段、掩码和 provider 列表在 P2 Gate 冻结，不编造现有 backend 能力。

## 3.6 Effective Published Result / Manual Override

Gold 是正式 Published Metric Authority；P3 读取 DB/backend 提供的 effective projection。Manual Override 能力整体归 TT-intelligent / DB / Backend / 数据治理侧；以下有效值规则是 backend 对外提供的契约，不是 Agent 实现第二套 precedence 的要求。

~~~text
Automated Value + Active Manual Override = Effective Published Value
ACTIVE manual override > automated result
~~~

例：自动值 80，R1=85；以后自动值 81/82/83 继续更新，有效值仍 85，直到 supersede 或 revoke。

- 当前系统只服务全市；人工修正管理员定义为 `GLOBAL_SUPER_ADMIN within current city-wide system scope`，即当前全市系统范围内的全局超级管理员。其身份、资源权限和写入校验由 backend 负责。
- Agent 仅只读消费 effective published result 与 manual override provenance；不创建、修改、撤销 override，不决定或实现 override precedence，不实现人工维护页面或 derived propagation。
- 普通读取有效值不要求读者也是修正管理员。
- Agent 三模式均不得更新 Gold 或自行发布 override。
- 状态 ACTIVE/SUPERSEDED/REVOKED，业务键/revision/operator/reason/effective_from 可追溯。
- 同键有效修正唯一；替换、撤销和审计由 backend 事务治理。
- ETL 不清除 ACTIVE 修正；Memory TTL 不使修正失效。
- validity、freshness 和 memory freshness 分开；不能因新鲜度提示静默回退自动值。
- effective binding 不完整时不读自动表兜底，不由 Agent 自行 join 出第二生效规则。
- downstream invalidation/recompute/republication 归 Gold pipeline，不由 Agent 自动写回。

Manual-origin parity 验证 Agent 读取的有效值与 provenance；生命周期变更和写权限测试由 backend companion work 提供证据，不转为 Agent 开发范围，也不强求有效值与自动底表相等。诊断/实验如使用自动底表，必须明确来源，不能将其解释为人工修正值的完整依据。

## 3.7 PublishedMetricReader

完整 key：

~~~text
metric_code
time_grain
time_value
dimension_type
area_id
team_id
employee_id
category_code
~~~

- 保留 NULL 语义，核验实际 NULLS NOT DISTINCT 约束。
- duplicate 按完整 key 检查；趋势、排名、多指标的不同 key 是正常多行。
- 无批准规则时不采用 latest computed_at/import_time 猜版本。
- 不用 success-only view 承担完整 Gold 能力。
- 不把缺少 numerator/denominator/release/checkpoint 的真实 Gold 强绑到 synthetic aggregate contract。
- 不补造 data_as_of、lineage、分子/分母。
- 多行歧义、未知状态、缺发布绑定明确停止。
- partial/no_data/failed/missing 和质量状态分别保留。
- 已隔离的 effective result 不由自动结果替代。

## 3.8 SourceResolver / Definition-backed Compute

~~~text
识别 metric/capability/time semantics
→ 授权与质量条件
→ exact effective published lookup
→ 保留已发布状态
→ missing 时查询 ServingPolicy
→ 仅批准的 raw/derived 子集可计算
→ 其他情况 clarification/HITL/unavailable/rejected
~~~

V3 aggregate-aware 规则保留在计算内部，不能让更便宜的 Silver aggregate 排在正式有效发布值之前。

TARGET ARCHITECTURE — Derived dependency 总体设计（拓扑递归是目标能力；P4-S2 V1 当前禁止 self/nested catalog-bound DAG，见 §8.5.2）：

~~~text
dependency refs
→ unknown/cycle check
→ topological order
→ leaf facts
→ status/quality propagation
→ grain/unit/scope/dimension/category validation
→ Trusted Calculation
→ ResultVerifier
~~~

partial/no_data/missing/failed 依赖不能默默变成功。External/legacy 仍不重算。自定义口径可显式规定合法数据缺失处理，但不能把权限拒绝、取数失败或未知 source 当成“缺失填零”。

### 3.8.1 Semantic resolution 与业务语义发明的边界

| 输入/解析结果 | 当前产品规则 |
|---|---|
| 用户明确给出 A/B，输入语义均可解析 | 在本次 mode 的能力、授权及计算门禁内执行；无正式身份/binding 时为 noncanonical AD_HOC |
| “每店销售额”等简称仅有一个权威且实质有效的解释 | 自动解析，保留正式或 custom 定义的真实身份及来源 |
| “人均销售额”等仍有两种以上实质有效的人员口径 | 仅询问消除歧义所必需的最小问题 |
| “门店效率”没有权威/已确认 custom 定义，用户也未给公式 | 不发明业务公式；展示有依据的候选或引导显式 BUILD 创作 |
| 已有正式公式且请求采用该正式身份/binding | 一次性执行也可 canonical；公式偶然相等不算采用权威定义 |

共享 resolution vocabulary 是 additive 词汇，用于表达冻结的解析结论；不替换既有 `ContextBundle.resolution_status`：

~~~text
resolved                          已有唯一权威且实质有效的含义
clarification_required            两个以上实质不同含义，仅询问消除歧义所必需的最小问题
no_authoritative_definition       无权威含义且用户未提供；不发明业务语义
~~~

### 3.8.2 共享 typed calculation semantic core（冻结契约）

**PRODUCT CONTRACT — FROZEN；STATUS: 首个 bounded contract-only slice（MODE_SEMANTIC_SHARED_CALC_CONTRACT_V1）**：在 QUERY AD_HOC 与 Custom Definition 实现分化前冻结共享的计算含义表示。该 slice 只落地 typed 契约类型，不启用任何新的执行行为；不要求另建大 PR，也不强迫重写 P4-S2 已验收内核。类型存在不等于执行行为已启用。

概念等价类型（名称可随仓库约定，语义必须覆盖）：

~~~text
CalculationSpec / ExpressionSpec        稳定、可复用的计算含义
CalculationInputSpec                    input role → 输入身份/角色/provenance 要求
ParameterSpec                           Parameter Contract：声明名/类型/语义约束
ParameterBinding / ExecutionBinding     某一次运行的具体参数值绑定
typed NULL / zero / unit / precision    业务显著的缺失/零值/单位/精度策略
~~~

要求：

1. 结构严格：未知/额外字段一律拒绝（extra=forbid）；无任意 Python、无无类型公式字符串充当权威。
2. 不可变：语义对象 frozen；确定性序列化得到 checksum，可重放核对。
3. Expression 结构 typed、有界；V1 只表达当前已计划的共享计算语义，不设计无限表达式语言。
4. 数值有限且类型有效；role/parameter 标识非空且唯一。
5. Parameter Contract（声明）与 per-run binding（具体值）结构分离；合法参数值变化不改变 CalculationSpec 身份/checksum，也不机械触发业务确认（独立风险/政策门禁仍有效）。
6. provenance 要求 typed；输入身份/角色要求是稳定语义，不随当前 auth/release/data 变化。

**Authority 不得内生于共享语义**：共享 semantic core 本身不授予、不编码 canonical authority。canonical=true、approved=true、saved=true、governance_candidate=true、template_registered=true 等字段不能作为权威；严格 CalculationSpec 必须拒绝注入这些 authority/lifecycle 字段。Canonical authority 仍只在正式 ApprovedCalculationBinding/catalog 路径；Custom Definition identity/lifecycle 仍在共享表达式语义之外。

| 包装对象 | 在共享计算语义之外增加的内容 |
|---|---|
| QUERY AD_HOC | 单次 execution spec、运行 binding/receipt；默认无可复用 definition identity；不因此 canonical |
| Custom Definition | 独立 custom identity、immutable version、确认/保留/治理轴；原对象恒 noncanonical |
| Canonical approved compute | 正式 canonical identity、ApprovedCalculationBinding 与权威发布证据；可使用兼容语义原语 |

**Method Authority != Metric Authority**：注册/版本化 trusted method 仅证明某方法允许执行；template/approved_template_ids、ExpressionSpec、公式、代码成功或用户确认均不提供正式指标权威。共享语义表示不共享 authority。临时结果及 custom 输出不得伪造 canonical metric key。

## 3.9 QueryGateway / CandidateVerifier

所有 Agent 业务 SQL 保留：

~~~text
policy / authorization / relation coverage
→ semantic/source constraints
→ column/filter/join allowlist
→ complete binds
→ SQLGlot single read-only statement
→ LIMIT / time-bound rule
→ EXPLAIN fail-closed
→ cost / scan gates
→ READ ONLY transaction
→ statement / lock timeout
→ row / byte limits
→ masking / receipt
→ ROLLBACK / release connection
~~~

- 业务身份是专用只读角色，不是 postgres/owner/migrator/SSH 身份。
- control/checkpoint/artifact 写入使用专用 typed repositories，不是模型 SQL 工具。
- 架构测试识别全部应用级业务 DB 调用旁路。
- 不以模型声称的 signature 自证语义；与已验证计划、资源和实际候选对照。
- 同一语义下按批准 source、relation/join/rows/cost 的确定性顺序选择。
- 不同业务口径先澄清/HITL，不通过结果多数投票确定口径。
- 同义候选去重；允许比较的候选 rowset 分歧停止高置信回答。
- rowset canonicalization 固定列/类型、保留 NULL/重复和有序语义，使用 SHA-256。
- 截断数据不能进入声称全量的计算。

## 3.10 时间、数值和 Grounded Answer

- Calendar TimeRange 保留 start/end 首尾都包含，允许同日，默认 Asia/Shanghai。
- 编译一次为 [start 当日零点, end 次日零点)，月粒度不改写请求日期边界。
- CURRENT_STATE、calendar、daily 日期分开。
- 正式金额/比率使用批准的 Decimal、rounding、unit、NULL/zero policy。
- 已确认投诉比率口径按正式定义执行，不全局套到其他 raw/derived。
- count/rate/trend/comparison/ranking/bounded detail 默认有确定性 renderer。
- 数字、单位、时间、来源和比较关系必须引用 typed facts/receipts。
- 不从自然语言回答中提取 SQL/数字作为业务真值。
- 模型润色不合规时回退确定性 renderer。
- answer.delta 在事实和必要 receipt 校验之后产生；不得先发错误数字再回退。
- 默认响应不含 SQL、DSN、内部栈和敏感原始 rowset。

### 3.10.1 confidence_band 的确定性含义

保留现有 `confidence_band` API 字段，含义是确定性 evidence / verification indication。它只能由以下可核对证据及已批准政策派生：精确语义解析、来源权威性、发布状态、数据质量状态、新鲜度、验证结果、provenance 完整性、receipt 完整性。

禁止采用 LLM self-reported confidence，也不能让模型生成或上调该字段。证据缺失必须按现有门禁返回不足证据/澄清/不可用等适用状态，不以主观置信覆盖缺口。首版不新增概率分数或评分框架；具体 band 映射由确定性政策及后续校准冻结，不能将 band 解释为已校准正确率。

## 3.11 DataQualityException Registry

八项CURRENT_STATE确认后移除对应“时间未确认”的原因，不删除质量登记机制。

| 字段 | 契约 |
|---|---|
| exception_id | 稳定、可审计标识 |
| affected metric/key/resource scope | 精确影响范围，不能无证据扩大到全域 |
| snapshot/publication/override references | 对应数据版本，未知时明确未知 |
| issue_type | date/category/dimension/source/partial/freshness/binding等 |
| evidence_refs | 可核对证据，不在默认报告塞原始PII |
| business_confirmation | 确认来源与状态，不由模型伪造 |
| serving_policy | serve/warning/unverified/quarantine/HITL的明确决定 |
| benchmark_eligible / canary_eligible | 布尔值；理由放报告，不另建复杂评分 |
| resolution_status | open/resolved等受治理生命周期 |
| created_at / resolved_at | 审计时间 |

质量门禁作用于success、partial、no_data、failed、missing以及derived dependency；不能只在success分支调用。手工修正优先也不能绕过真正的质量异常；遇到异常时停止/提示，不能静默改用自动值。

## 3.12 PlanValidator与候选选择的固定顺序

~~~text
1. 严格schema与字段类型
2. mode / capability envelope
3. metric/custom definition/source identity与provenance
4. time / grain / dimension / category
5. current authorization / relation coverage / sensitive fields
6. source eligibility / effective publication / override binding
7. dependency / join / population / comparability
8. quality / freshness / required evidence
9. confirmation/plan hash（适用时）
10. budget / capacity
11. AUTO_EXECUTE / HITL_REQUIRED / CLARIFICATION_REQUIRED / REJECTED / RESULT_UNAVAILABLE（目标 outcome 与现有 wire 状态显式映射）
~~~

输出不再只是PASS/FAIL。Missing字段、歧义、禁止访问、容量不足、无发布结果分别使用正确状态，不通过统一“低置信”掩盖。

候选筛选在语义成立后执行：
1. 所有硬门禁通过；
2. 遵守既定source优先级；
3. relation/join更少；
4. estimated rows/cost更低；
5. 同signature与等价AST只执行一次；
6. 相同业务目标的合法候选出现结果差异时暂停；
7. 不同业务含义不能用数值投票决定；
8. 后续候选仍扣同一请求预算；
9. 记录淘汰/降级理由，不写模糊成功结论。

Rowset规范化固定列顺序、类型/Decimal精度、时间表示、NULL和重复行。无序结果规范排序，有序/ranking结果验证其顺序；hash只是数据证据的一部分，不替代语义与质量验证。

## 3.13 轻量上下文工程

采用早期计划中仍有价值的机制：
- versioned稳定system prompt前缀，动态部分只追加有界ContextBundle；
- 当前必要证据保留，较早数据压缩成带hash/revision的引用；
- memory只召回当前有权访问的相关记录；
- 超长schema只传授权子集；
- tool availability/masking可改善提示，但真正授权在服务端执行前重复检查。

不增加progress.md生产状态系统、全库重写循环或自动信任的经验SQL库；恢复真源仍是LangGraph checkpoint、typed artifacts和control audit。缓存友好仅作为优化目标，不在未测量前宣称节省比例。

---

# 4. 共享执行、HITL 与确认契约

## 4.1 类型边界

Pydantic strict contracts 共用 envelope，payload 按 kind 区分，不能做一个充满可选字段且不校验语义的大字典。

| 契约 | 主要职责 |
|---|---|
| ContextBundle | release/schema、允许资产、冲突、slots、降级 |
| QueryPlan | 正式问询的 typed intent/time/filter/dimension/category |
| DiagnosticPlan | 已验证起点、分析分支、数据请求、方法和预算 |
| DraftCustomMetricPlan | 新建/修改可复用业务定义的可审阅稳定语义及 Parameter Contract |
| ConfirmedCustomMetricPlan | 已确认不可变 Definition Version 的语义载荷及独立确认记录；与每次执行契约分离 |
| ExecutionPlan | 公共 typed DAG、mode、policy/auth、plan hash、budget |
| DataRequest/SQLCandidate | 有界、可验证的取数提案，不是 DB 连接 |
| FrozenDataset | 选择条件、hash、数据/授权版本、字段/敏感性、数量、receipt |
| HITLRequest/Decision/ResumeToken | 一个公共暂停/选择/确认协议 |
| SQL/Calculation/Capability/CodeAct Receipt | 各类执行的证据，非 SQL 不造 SQL fingerprint |
| ArtifactEnvelope | owner、kind、revision、provenance、访问/时效/重放信息 |

原 QueryPlan calendar 契约通过显式转换保留；不为新模式补造旧计划缺少的权限、时间或 source。

### 4.1.1 Identity、status与provenance分轴

| 轴 | 内容 |
|---|---|
| definition_kind | canonical / legacy_compat / custom namespace |
| canonical source_type | raw / derived / external；不加diagnostic作为第四正式来源 |
| fact_provenance | PUBLISHED_GOLD / DEFINITION_BACKED_COMPUTED / PRODUCT_PUBLISHED_STATE / DIAGNOSTIC_ANALYSIS / AD_HOC_METRIC |
| publication_origin | AUTOMATED / MANUAL_OVERRIDE，适用时填写 |
| publication_status | 绑定schema支持的状态；missing独立 |
| resolution outcome | answer / clarification / hitl / result_unavailable / rejected |
| execution status | accepted/running/suspended/completed/failed/cancelled等运行生命周期 |
| definition confirmation | Draft / Confirmed，独立于保留和治理 |
| definition retention | SESSION / SAVED；不决定确认、验证或权威 |
| definition governance | NONE / GOVERNANCE_CANDIDATE / REVIEW 等适用状态 |
| metric authority | 系统公共轴 canonical / noncanonical；原 Custom Definition invariant = noncanonical |
| artifact lifecycle | 按对象记录结果 artifact 的留存/归档；不能与 definition 各轴混用 |
| validation status | 未验证、通过、失败或验证不足，不能由saved推导 |
| freshness/replay | 当前/过时/不可验证，以及输入仍可重放或已不可用 |

未知值保持NULL/unknown及原因，不能用空串或0补齐证据。Canonical/legacy数量不包含custom IDs。

### 4.1.2 计划与上下文字段

| 契约 | 最小字段 |
|---|---|
| MetricDefinition | metric_code/asset_id、domain、definition/source type、lifecycle、time semantics、supported grain/dim/category、dependency refs、unit/precision、definition/source fingerprints、approved execution/disposition refs |
| AuthorizationContext | subject、resource_scope、org/person_scope、trusted provenance、effective authorization revision |
| ContextBundle | semantic_release_id、schema_snapshot_id、mode、permitted assets/relations/edges、resolution_status、unresolved_slots、conflicts、degradation、context checksum |
| BoundFilter | field_ref、registered operator、typed scalar/list/range value、来源；空in集合单独校验 |
| QueryPlan | schema_version、intent、metric refs、typed time/filter/dim/category、comparison/ranking intent、result_limit、unresolved slots；source hint不构成授权 |
| DiagnosticPlan | verified starting fact refs、目标/分支、bounded rounds、DataRequests、方法/特征、assumptions、required decisions、budget profile和input bindings |
| DraftCustomMetricPlan | identity/purpose、typed population/formula/joins/filters/time/output/validation、draft version、歧义和假设 |
| ConfirmedCustomMetricPlan / Definition Version | deep-immutable 业务语义、Parameter Contract、稳定 source-role/provenance 约束、definition version/hash、修改链；不含当前运行环境 |
| Confirmation record | confirmed definition version/hash、confirmed_by/at、decision/request/checkpoint 引用；审计记录与 definition hash 载荷分离 |
| Per-run Execution Binding | definition version/hash、具体参数、当前 auth/release/schema/source/data/policy/capability binding、run budget、run/request/checkpoint；校验及 receipt 引用 |
| SourceDecision | selected source/capability、理由、required permissions/bindings、quality/freshness/override状态、degradation、能否执行 |
| ExecutionPlan | definition/spec hash 引用、execution binding、mode、当前 semantic/schema/policy/auth refs、registered steps/input refs、适用确认记录、预算与取消上下文；独立 execution_plan_hash |
| QueryCandidate | candidate_id、origin、SQL AST/SQL及binds、服务器验证的semantic signature、relations/joins、source binding、估算及门禁结果 |

正式比率/金额使用Decimal；统计方法采用的其他数值类型必须有限、可序列化并声明精度/容差。filter、formula和step不得退回任意自然语言程序。

### 4.1.3 Fact、dataset和receipt字段

| 契约 | 最小字段与约束 |
|---|---|
| PublishedMetricResult | 完整8字段key、value/type/unit、schema status/error/DQ、effective origin/revision、真实计算/发布时间、data_as_of/freshness、source/publication refs |
| FrozenDataset | dataset_id/hash、selection definition、resource/column refs、row/byte counts、sampling/truncation、schema/dtypes/timezone、sensitivity、auth/policy/data/override版本、receipt refs |
| ExecutionReceipt | execution/run/step IDs、plan hash、policy outcome、semantic/schema/auth、source/checkpoint、SQL/bind fingerprints、approved relations、EXPLAIN rows/cost、elapsed/row count、rowset hash、masking、freshness、error/degradation |
| CalculationReceipt | step/template/version、input refs/hashes、output hash、unit/precision/rounding/NULL/zero policy、elapsed、validation |
| CapabilityReceipt | fixed operation/version、request/result hash、backend revision、auth/scope、date/status semantics、elapsed/error；不补SQL字段 |
| CodeActReceipt | execution plan hash、适用 definition/confirmation refs、code ref/hash、input dataset hashes、runtime/image/library/resource profile、seed/tolerance、output hashes、exit/timeout/cancel/resource 状态、validation 与外发政策；动态环境不回写定义 hash |
| ModelReceipt | actual alias/profile/provider、stage/mode、request projection hash、policy decision、tokens/cost可用性、latency、retry/fallback |
| AnswerFact | fact_id、metric/custom/diagnostic reference、scope/time/dim/category、value/unit、provenance/origin、quality、freshness、receipt/data hash refs |
| AnswerArtifact | outcome、typed blocks/facts/citations、confidence band、mode、versions、degradation/limitations、artifact refs |

Receipt由可信Runtime产生并核验，不接受CodeAct自行提交的“执行已获授权”作为证明。默认模型/用户视图是受控projection，不是完整内部receipt。

### 4.1.4 Artifact payload

AnalysisArtifact记录analysis_id、question、starting facts、summary、statistics、patterns、hypotheses、method/selection、datasets/receipts/code refs、model/semantic/auth/data/override版本和完成限制。

CustomMetricArtifact分别引用 custom definition identity/version/hash 与 per-run result/receipt；定义包含稳定 formula/population/joins/time/unit/policies 和 Parameter Contract，运行结果包含实际参数/source/data/auth/validation。确认、保留、治理、权威分轴记录，不以一个 lifecycle 字段替代。

两者共用持久化、访问、版本、retention与replay机制。缺少关键输入或独立验证的结果可明确保存为未验证记录，但不能标为已验证或自动晋升正式指标。

## 4.2 一套 HITL Policy

**CURRENT PRODUCT RULE — reason-oriented HITL**：

| 原因 | 行为 |
|---|---|
| CLARIFICATION | 业务语义或必需参数尚不能唯一解析，提出最小澄清 |
| BUSINESS CONFIRMATION | 新建/实质修改可复用业务语义，确认明确 Draft/version |
| RISK / POLICY DECISION | 仅针对服务端政策天花板内明确允许、命名的重大决策 |
| 当前授权/政策禁止 | DENY / REJECTED；确认不能修复授权失败 |
| 必需 capability/source/data 缺失 | UNAVAILABLE / result_unavailable，按原因记录 |
| 定义与参数有效且门禁通过，无新决策 | AUTO_EXECUTE；不机械重复确认 |

BEFORE_EXECUTION 仅描述某个适用人类决策必须发生在相关执行之前，不再是 BUILD 全模式或 SAVED 每次重跑的固定策略。新建/实质修改定义仍保留业务确认；确认不自动表示请求执行。

公共 policy 输出：

~~~text
AUTO_EXECUTE
HITL_REQUIRED
CLARIFICATION_REQUIRED
REJECTED
RESULT_UNAVAILABLE
~~~

以上为目标 outcome 语义；与现有 wire 状态显式映射，不声称本轮已新增枚举。

公共 reasons 包括：

~~~text
AMBIGUOUS_METRIC / TIME / SCOPE
CANONICAL_LEGACY_AMBIGUITY
HISTORICAL_CURRENT_STATE_AMBIGUITY
ANALYSIS_BRANCH_SELECTION
ANALYSIS_COST_ESCALATION
SENSITIVE_DATA_ESCALATION
HYPOTHESIS_CONFIRMATION
CUSTOM_METRIC_PLAN_CONFIRMATION
EXPLORATION_PLAN_CONFIRMATION
HIGH_RISK_EXECUTION
~~~

“敏感性/成本升级”指既有授权和政策天花板内的重要变化，不是通过用户确认批准原本禁止的数据访问/外发。

## 4.3 动作与 checkpoint

统一动作：Confirm、Modify、Choose、Resolve、Reject、Cancel。兼容旧 wire action=approve，作为 Confirm 的旧别名，不建立第二套动作接口。

- 请求绑定 owner、hitl_request_id、expected_version、idempotency key 和 payload hash。
- 同 key 同 payload 返回原结果；同 key 不同 payload 冲突。
- 多实例原子取得执行资格，不能只做内存字典的先查后写。
- 人工确认记录由服务端产生，模型文本“用户已确认”无效。
- 等待时持久化 checkpoint、释放 SQL/计算资源。
- 人工等待不保持活动计算 deadline 运行；恢复后仍保留已消耗总预算，不自动重置额度。
- 恢复重验当前权限、政策、数据绑定及剩余预算。
- ResumeToken 绑定原状态，不把原自然语言再解释成一个新计划。
- 一个活动执行的待决策状态不能被普通新请求覆盖。
- 实质定义语义修改必须产生新 Draft/Definition Version，不能原地改 confirmed definition bytes。契约内参数绑定只更新本次 Execution Binding；独立风险决定与恢复校验仍有效。

**TARGET ARCHITECTURE**：需要继续执行的 Resolve/Confirm → 恢复绑定 run state → 当前重验 → compile → governed execution → grounding/result；不能用一条“已批准”消息代替尚待执行的结果。

**CURRENT IMPLEMENTATION GAP — accepted handoff，非本轮新增实现**：当前 orchestration 仍有 approve 后返回消息并终止的 HITL 路径，尚未完整接回 typed revalidate → compile → execute。后续 readiness 应核验实际可达路径；本次不修代码。

## 4.4 BUILD 计划卡

至少覆盖，非适用项明确 N/A；计划卡分为稳定定义与本次执行预览两部分，预览可展示动态字段，但不得把它们混入 Definition Version/hash：

~~~text
Metric identity / purpose
Sources / join paths / cardinality and dedup rules
Population / denominator / numerator
Filters / time semantics / resolved window / grain
Dimensions / categories
NULL / zero / partial / missing policy
Computation steps / intermediates / CodeAct requirement
Output type / unit / precision
Assumptions / unresolved slots
Permission / capability envelope
Estimated cost basis / hard budget / data-version policy
Validation criteria
~~~

- 不显示未经校准的“来源置信度92%”作为正确性保证。
- 不对缺失时间默默默认最近30天。
- 分母/关联/去重/精度等核心含义未明确时不能确认执行。
- 原四要素卡片可作为 UI 分组，但执行契约必须 typed，不以自然语言公式为唯一程序定义。
- 优先 SQL/Trusted Calculation。BUILD 不因名字叫实验室就必须生成 Python。

## 4.5 确认后不变性与执行忠实度

**CURRENT PRODUCT RULE / IMPLEMENTATION-PENDING CONTRACT**：

| 对象 | 不可变边界 / hash 内容 | 另行记录 |
|---|---|---|
| Immutable Definition Version | 稳定业务语义、Parameter Contract、输入身份/角色/provenance 类型约束、公式/人口/去重/时间口径/单位及精度规则 | definition identity/version/hash 与修改链 |
| Confirmation record | 对指定定义版本的服务端人类决策证据 | confirmed_by/at、request/checkpoint/decision refs；不纳入定义语义 hash |
| Per-run Execution Binding / ExecutionPlan | 本次具体参数与执行上下文，冻结为本次 execution_plan_hash/绑定证据 | current authorization revision、active release、source/data snapshot、policy、capability、run budget、run/request/checkpoint |
| Receipt / validation | 实际执行与重验结果 | 数据/代码/template 版本、消耗、provenance、验证结果 |

Definition hash 使用定义语义载荷的确定性序列化；这里的 canonical serialization 是规范化编码，不赋予 canonical 指标权威。is_locked=true 或 shallow frozen 单独不足以证明不可变。

当前 auth revision、active release、data snapshot、声明参数的具体 month/store 值、run budget 改变，均不因字段变化本身创建新 Definition Version。若定义明确固定某来源身份或时间语义，该稳定约束仍须满足；不能以“动态绑定”为由替换分母或业务含义。定义可约束所需能力/来源类别，但实际授权和预算属于每次运行门禁。

执行必须记录：

~~~text
definition_version / definition_hash
confirmation_record_ref (when applicable)
execution_binding_ref / execution_plan_hash
actual DataRequest/source/filter/join signatures
input dataset hashes
code hash / template version
intermediate/output facts
validation results
policy/auth/data binding
~~~

以下稳定业务含义的实质变化需要新 Draft/Definition Version 和适用的人类业务确认：

- 分子/分母、population、去重、source-role/身份要求、join 或过滤语义；
- 业务时间口径、粒度、维度/category 的定义约束；
- NULL/zero/partial/missing、公式/假设、业务相关单位/精度；
- Parameter Contract 自身扩展或变更。

例如已声明 month/store_scope 参数，2026-08/Store A 改为 2026-09/Store B 且仍满足同一契约，只产生新的 Execution Binding。当前数据刷新、正常重跑时间、仍满足定义的发布 revision、正常权限重验不自动创建新版本。

具体参数重绑定不因参数变化本身机械 HITL；成本/风险变化可以触发独立、政策允许的决策。资源不足或权限禁止须拒绝/不可用，不能靠确认越过硬门禁，也不能因 run budget 变化无理由改写定义版本。

允许同一契约内的技术修复，但仍重验。不能通过“修复”放宽筛选、换分母、吞掉空数据或修改业务公式。

Hash equality 只证明版本关联，不单独证明任意 Python 语义正确。关键统计尽量由可验证的取数/结构化算子产出；对中间量和最终值进行独立检查。缺少关键验证覆盖时不得标记 validation=PASS，不得以“代码能运行”声明计算正确。

## 4.6 Shared Budget

~~~text
系统总容量
→ mode budget
→ 当前 run 剩余预算
→ round / step 子预算
~~~

保留 Query bootstrap：

| 项目 | Fast | Standard |
|---|---:|---:|
| 请求 deadline | 4s | 10s |
| model calls | 0 | ≤3 |
| SQL candidates | 1 | ≤2 |
| SQL executions | 1 | ≤2 |
| join hops | 0 | ≤1 |
| repairs | 0 | ≤1 |
| SQL timeout | 2s | 5s |

其他原 bootstrap：EXPLAIN 1s；Query scan estimate 100,000；返回最多200行/不超过1MiB；provider可重试1次；相同SQL/error阈值2；reserve20%。

旧 Deep 30s/5 model/2 SQL 只保留历史参考，不直接限制完整 ANALYZE/BUILD，也不在 QUERY 中暗中启用。

ANALYZE/BUILD 必须显式配置 rounds、SQL/model/CodeAct、工单/行数/字节、tokens、deadline、cost、CPU/RAM/output 上限。开发前有有限验证 profile，生产前校准冻结，缺配置不采用无限值。

- 所有额外 lookup、profiling、candidate、fallback、补数和重试均计费。
- 共享执行 ledger，不按 step 或 mode 跳转重新发预算。
- 查询与复杂计算分别排队/限额，但共同服从系统总 SQL/模型/主机容量。
- 预算提升必须在 server policy ceiling 内，有明确用户决策记录。
- provider失败不能自动升级更贵或不获准处理数据的模型。
- 预留验证、审计和响应时间；超时采用剩余预算和阶段上限中更严格的值。

现有ExecutionPlan最多16个registered steps的基础结构上限保留。复杂工作流的有限rounds并不重置全run预算；如确需调整结构上限，应有明确profile、复杂度与容量验证，不能因为BUILD更自由就取消上限。

历史V3 Deep profile完整保留为迁移/校准参考：deadline30s、model≤5、candidate≤2、SQL≤2、join≤2、repair≤1、SQL timeout10s、context10K/evidence20/relations8。该表不重新开放QUERY Deep，也不构成ANALYZE/BUILD的固定SLA。

不可重试的权限、policy、schema和口径错误不按429/网络故障处理。网络/429/5xx仅在冻结profile允许范围内重试，并检查剩余deadline。评测总预算也必须在case/调用前预留，receipt回来后结算；不能全部执行完才发现预算超支。

---

# 5. ANALYZE、BUILD、Sandbox 与统一 Artifact

## 5.1 ANALYZE 核心流程

~~~text
Verified Starting Fact
→ DiagnosticPlan
→ org/team/employee contribution
→ bounded work-order retrieval
→ feature extraction
→ statistics / optional CodeAct
→ ResultVerifier
→ DiagnosticAnswer / AnalysisArtifact
~~~

- 正常下钻不逐层确认；重要分析分支、明显成本增长、超出原选定敏感范围或关键假设决策可 checkpoint HITL。
- 模式选择不解决指标身份/时间歧义，仍需先澄清起点。
- 冻结数据后释放业务连接；补数重新走治理和版本检查。
- 不将不同水位数据拼成未标注的同一快照。
- 抽样/截断和选择分母可见，不能将样本比例称为全量贡献。
- 诊断统计为 DIAGNOSTIC_ANALYSIS，允许非 Gold 统计但不能冒充正式 KPI。
- observed pattern != causal conclusion；无批准证据契约时只报告观察/假设。
- 人工修正值与自动底表不相等时保留两者 provenance，不强行“解释”修正差额。

## 5.2 Custom Definition、BUILD 与已有定义重跑

**CURRENT PRODUCT RULE / IMPLEMENTATION-PENDING PRODUCT CONTRACT**：BUILD 是用户有意创建或实质修改可复用业务语义；不是由算术、一次执行或 SAVED 身份触发。

### 5.2.1 创作、确认、留存与结果分开

~~~text
用户要求创建/实质修改可复用定义
→ permitted metadata preview
→ DraftCustomMetricPlan（稳定业务语义 + Parameter Contract）
→ Resolve/Modify
→ business-semantic confirmation
→ immutable Confirmed Definition Version
→ 可在 SESSION 保留；显式 retain/save reusable definition 后为 SAVED
~~~

用户还要求执行时，另建 Per-run Execution Binding，经当前门禁后执行 governed fetch → SQL/Trusted Calculation/按 mode 允许的 Sandbox → validation → custom result/receipt。创建定义、保留定义和执行计算不是同一个自动动作。

| 轴 | 适用值/不变量 |
|---|---|
| Confirmation | Draft / Confirmed |
| Retention | SESSION / SAVED |
| Governance | NONE / GOVERNANCE_CANDIDATE / REVIEW 等后续受治理状态 |
| Authority | 系统公共分类 canonical / noncanonical；原 Custom Definition 对象恒为 noncanonical |

Confirmed + SAVED + GOVERNANCE_CANDIDATE + noncanonical 是合法组合。上述概念不是一条线性状态链；类型命名与存储映射在后续 bounded contract 中实现。

首版仍支持批准数据库上的自定义指标/比率/分母、cohort、时间窗口、跨表/跨域组合、分类规则、可验证统计和有限 what-if。

- QUERY/ANALYZE 使用 approved joins；BUILD 可在当前授权资源内提出 plan-scoped join。
- 新 join 必须描述 keys、方向、grain、cardinality、去重/预聚合、NULL/未匹配规则和规模风险。
- 未证实关联语义或无法控制行膨胀时暂停，不用 DISTINCT 隐藏口径问题。
- 用户确认不把该 join 写入全局 ApprovedJoinGraph，不新增数据权限。
- 保留/保存定义不等于执行、结果已验证或已成为正式指标。
- QUERY AD_HOC 默认只有单次 execution spec；可留下 execution/receipt/audit 或保存结果 artifact，但不自动创建 Custom Definition，不自动成为 SESSION/SAVED，不使用虚假 canonical metric key。
- 从临时公式转为可复用定义必须由用户明确要求创作并走 BUILD 语义确认；不从保存一次结果推断该意图。

### 5.2.2 SAVED 定义重跑与自动重验

~~~text
选择已有 SAVED definition
→ 读取 immutable Definition Version
→ 按 Parameter Contract 绑定本次具体参数
→ 当前 authorization / release / source / policy / relation / capability 重验
→ 当前 data availability / freshness / DQ / budget 等门禁
→ 按原因进入 execute / clarify / decision / deny / unavailable
~~~

定义语义未变、参数合法、当前门禁通过且没有新的命名人类决策时直接执行。询问结果可属于 QUERY；诊断使用可属于 ANALYZE；实质修改定义才是 BUILD。QUERY 复用不能获得 BUILD/CodeAct 权限。

参数变化本身不创建新 Definition Version、不机械 HITL；独立风险门禁仍有效。实质语义或 Parameter Contract 变化必须新 Draft/version 和新业务决策。授权/政策禁止直接拒绝；语义或参数不唯一才澄清；必需来源/能力/数据缺失返回不可用。确认不授予永久权限，不能将所有重验失败转成待确认。

每次记录新的 Execution Binding、execution plan、validation 和 receipt；不能把旧结果冒充当前数据结果。定义 hash 与动态字段的边界以 §4.5 为唯一详细契约。

### 5.2.3 正式治理与身份链接

~~~text
用户显式申请治理候选包（原 custom definition/version 不变）
→ 外部 business / DB / Gold / Semantic governance
→ 正式发布创建/关联独立 canonical identity 及权威 binding
→ 保留 candidate/custom 与正式身份的 provenance linkage
~~~

治理结果不把原 custom identity 原地转成 canonical，不追溯改写历史结果权威。保存定义、复用、分享、安装、流行度、用户确认、模板注册均不能 canonicalize。本轮只定义治理候选契约，不自动发外部通知或执行业务发布。

## 5.3 一个 Sandboxed CodeAct

ANALYZE/BUILD 使用同一个 runtime，不建立两个沙箱平台。能力差异由经过验证的 profile 表达，不为更大 profile 默认加新库。

~~~text
Plan / schema / metadata / samples
→ ModelInputPolicy
→ ModelGateway → plan / code generation
→ confirmed/validated plan binding
→ Authorized FrozenDataset
→ isolated CodeAct worker
→ ResultVerifier（result and adherence validation）
→ CodeAct output / analysis evidence
→ ModelInputPolicy（再次执行）
→ ModelGateway → allowed synthesis / answer / artifact
~~~

硬约束：

~~~text
no DB credentials or connection
no network inside worker
no shell/subprocess
no host filesystem or other job data
no Docker socket / privileged
no runtime package installation
no canonical publish capability
temporary scoped workspace
approved libraries
bounded CPU/RAM/time/input/output
~~~

- 外部模型 API 位于 ModelGateway；Sandbox 禁网络不妨碍 Runtime 使用模型。
- 不将同进程 exec、线程 timeout、AST/regex、multiprocessing 单独当安全边界证明。
- 隔离技术/版本、镜像、库和开发资源 profile 在编码前 readiness 冻结；实际逃逸/终止/资源测试通过才启用。
- 生成代码不得访问 artifact repository、ModelGateway client、DB session 或服务凭据对象。
- Sandbox不可用时可以继续不需要它的合法流程；需要它的任务明确不可用，不在 API 进程回退执行。
- Runtime 填写可信 receipt；不信任代码自己返回的“已授权”“已确认”字段。

跨隔离边界只接收限定schema、大小和深度的安全序列化结果/文件引用，不对不受信任的pickle/cloudpickle或任意Python对象做反序列化。输出路径由broker分配并核验，不接受目录穿越、symlink或跨job引用。输入dataset、代码、输出文件分别有hash和来源。

补数：

~~~text
CodeAct proposal: DataRequest / SQLCandidate
→ PlanValidator
→ current authorization / coverage / sensitive policy
→ remaining budget
→ QueryGateway
→ new dataset version/hash
→ Sandbox
~~~

SQLCandidate 是未信任提案，不是 generic execute_sql。超出 BUILD 已确认语义的补数需新确认；权限拒绝不能由 HITL 放行。

## 5.4 一个 Artifact 仓储

公共 envelope：

~~~text
artifact_id / kind / owner / scope
revision / lifecycle / validation status
source and plan references
policy/auth/data/override revisions
created_at / freshness / retention / replay availability
~~~

typed payload：

| 类型 | 记录 |
|---|---|
| AnalysisArtifact | 起点事实、统计/模式/假设、选择范围、方法、中间结果和限制 |
| CustomMetricArtifact | custom identity/immutable definition version 与稳定语义；独立关联 execution binding/result/receipt/validation；确认、保留、治理分轴 |
| Dataset/Code/Output references | hash、受控内容引用、环境及 receipt、数量和敏感分类 |

- 自定义 ID 使用独立 namespace；默认私有，不覆盖 canonical code。
- 保存 execution/result artifact、归档指定 artifact、保留/保存 reusable definition、生成指定 definition version 的治理候选分别记录对象和版本；均不是业务系统 Action Plane。
- 保留/保存可审阅 reusable definition 不代表验证成功；保存 execution/result artifact 也不创建定义。validation、definition confirmation/retention/governance 分开。
- 不建立第二个 Experience Store 自动信任历史 SQL/代码。
- 不默认持久化全部原始 PII 工单、手机号、地址。
- hash 本身不能恢复已删除内容；输入/代码过期后明确 replay unavailable。
- 后续引用先检查当前访问权，再检查数据、manual override、语义和政策变化。
- 无权内容不能仅标 stale 后仍展示；history、预览、memory和模型召回都适用。
- Memory不能成为当前业务事实 authority，不能跳过 effective Gold/授权/版本验证。
- 历史memory不能修改系统指令、mode、能力包或预算。

### 5.4.1 Artifact Persistence Contract（首版冻结）

采用 Control PostgreSQL + Controlled Shared Artifact Storage 两层职责；仍是同一 Artifact repository、权限策略和生命周期，不增加独立存储平台。

| 层 | 负责内容 | 首版边界 |
|---|---|---|
| Control PostgreSQL | metadata、typed JSON artifacts、hashes、references、lifecycle、freshness/stale state、semantic release binding、authorization revision | 保存 AnalysisArtifact、CustomMetricArtifact、HITL 请求/决定/批准元数据、CodeAct receipts 和 small structured results |
| Controlled Shared Artifact Storage | 必要的大型 CodeAct 输出、生成文件、较大分析表、图表/导出内容 | 按需启用受控共享存储；PG 保存引用、hash、owner/scope 和生命周期，两 API 使用同一受权访问路径 |

- `do not persist large files by default`：首版不默认保存大文件，更不默认保存全部原始输入。需要持久化时，内容与引用都受当前授权、敏感分类和 retention 约束；API 本地磁盘或 Sandbox 临时目录不充当共享持久化真值。
- HITL 的业务控制记录进入 control PG；LangGraph 执行状态、checkpoint 和幂等恢复继续由既有 checkpoint PG 承担。记录通过既有 run/checkpoint 引用关联，不复制一套 HITL 状态机，不因本契约迁移或替换 checkpoint 职责。
- 存储内容是否仍存在与 hash 是否存在分别报告；删除/过期后保持既有 stale/replay unavailable 语义，不能只凭引用宣称可重放。
- 上传文件、Notebook-like artifacts、大规模对象持久化平台后置。具体大小阈值、retention 和资源预算进入实现/校准冻结，不在本文虚构数值或指定新增对象存储服务。

## 5.5 输出说明

ANALYZE 使用：

> 分析说明：以上内容由 {model_profile} 基于截至 {data_as_of} 的已授权业务数据、当前筛选范围及受控分析步骤生成，属于诊断性分析，可能受数据完整性、样本范围和模型推理误差影响，不等同于正式发布指标，也不默认构成确定性因果结论。正式指标请以 Gold/业务系统发布结果为准；涉及人员、具体工单处置或重要业务决策时，请结合原始业务记录进行人工复核。

使用 CodeAct 时追加其受控计算和回执说明。时间未知时明确未确认。

Custom Definition 的执行结果无论本次 mode，均标明“自定义口径/未正式发布”、definition version/hash、execution binding/receipt、方法、数据范围、NULL/zero/partial/missing 处理和 validation 状态。QUERY AD_HOC 标明临时 noncanonical 计算及实际来源，不冒充正式指标，也不重复显示重型分析说明。

---

# 6. 一套评测：减少重复执行，保留必要风险断言

## 6.1 历史可复用基础及缺口记录

**HISTORICAL OBSERVATION — 2026-09-16 文档来源**：以下清单保留当时源码观察，不是 2026-09-22 全量复核结论。后续 typed runtime/grounding/P4-Q 增量见 §1.6；尚未重新核验的旧缺口不自动计为当前已确认缺陷。

当时只读检查记录实际 worktree 已有：

| 文件/入口 | 可复用职责 |
|---|---|
| benchmarks/adapters.py: BenchmarkCase | 数据集适配、case_id、domain/layer/tags |
| benchmarks/typed_receipts.py | typed receipt、manifest、预算与配对统计基础 |
| benchmarks/runner.py: run_typed_benchmark | 注入 executor、逐 case 收集 receipt、生成报告 |
| benchmarks/metrics.py: CaseResult/BenchmarkReport | 统一结果和报告 |
| benchmarks/agent_bridge.py | 迁移对象，现存文本/SQL提取不能作为新企业验收链 |
| agents/codeact_engine/plan_card.py | 产品意图和迁移素材，不是已满足不可变契约的证明 |

当时记录的静态缺口（后续 readiness 先核验是否仍成立，再确定 bounded 修订）：

1. typed runner 将 execution_accepted + answer_type=answer 作为成功基础，不能代替独立事实/结果比较。
2. 旧 TypedAnswerReceipt/manifest 缺三模式、有效发布/确认计划/复杂 artifact 等必要证据。
3. manifest 强制 deepseek.flash/deepseek.pro/benchmark.nim 三模型组合，形成不必要耦合。
4. 旧 bridge 仍解析自然语言答案/SQL；企业发布必须退出该路径。
5. 旧实验矩阵/ablation CLI 不进入默认 PR/发布流程。
6. 旧 ConfirmedCalcPlan 仅有可变对象和 is_locked 标志；不能直接宣称已锁定。

保留复用骨架的目标；当前实际缺口按新证据确认，不直接重放历史修复清单，不建立三套 runner。

## 6.2 一个 Case Registry

每个 case 至少有：

~~~text
scenario_id / case_id
mode
domain / complexity / capability / risk tags
input and optional interaction script
dataset/snapshot/effective-publication references
expected outcome
expected facts/results and tolerance
expected plan constraints / allowed sources / forbidden scope
expected HITL reason/actions
expected confirmed-plan adherence
required receipts / validation assertions
~~~

- 同一个业务场景可以有模式不同的 case variant。
- 行为和期望不同的 variant 是不同测试，不能为了去重抹掉。
- 共同数据 fixture、oracle 与 shared-security 检查复用。
- Oracle来自批准事实、独立 reference SQL/计算或审核标注，不只由待测模型/编译器同时生成输入与“正确答案”。
- 临时统计和复杂计划的语义验证不足时不得标记 PASS。

## 6.3 同一次执行收集多个维度

~~~text
一次 case execution
→ plan/decision trace
→ data/SQL receipts
→ computation/code receipts
→ output facts/artifact
→ shared assertions
→ applicable mode/capability assertions
→ one CaseResult
~~~

| 维度 | 适用性 |
|---|---|
| 权限、覆盖、ModelInput、预算、取消、receipt | 公共基础 |
| identity/effective Gold/status/time/dimension/category | 正式事实相关 case |
| ambiguity、HITL reason、checkpoint、choice/resolve | 发生对应控制行为的 case |
| drilldown/contribution/pattern/causal/memory | 诊断 case |
| plan completeness/denominator/join/ConfirmedPlan adherence | BUILD case |
| fetch/calculation/CodeAct correctness/repair/reproducibility | 使用对应能力的 case |

旧 Plan Quality、SQL Accuracy、CodeAct E2E 合并为这些断言维度。不为每个维度重新运行同一端到端 case。

## 6.4 执行选择与去重

| 时机 | 执行集合 |
|---|---|
| coding iteration | focused unit/contract，无默认真实模型全量评测 |
| PR validation | shared core regression + 受影响 capability/mode/risk cases |
| 专项校准 | 已登记问题所需 case/profile；有预算的候选比较 |
| release candidate | 当前启用能力的 required cases 并集，一次受控评测任务 |
| canary | 实际启用流量与冻结断言；不要求294标识各获得流量 |

- change→capability→case 映射有版本、可审查，不能模型随意挑有利样本。
- 无法可靠判定影响范围时扩大到共享回归，不静默漏测。
- 公共安全用例去重；新的 mode/envelope/不同 sandbox policy 带来的差异仍测。
- 不做固定“3 modes × 3 models × 全数据”。
- 每次调用只选择需要的 model profile；manifest记录实际执行者。
- 重用历史 baseline 结果必须匹配语义、数据、权限、policy和评测版本；不匹配则重跑对应范围。
- code/fixture/hash完全一致的证据可复用，政策或能力变化不能沿用旧安全结果。
- 单次run内相同 execution identity 去重；需要重复验证稳定性时显式声明 repetition/seed，不假装是去重失败。

## 6.5 一个 Manifest / 一个报告

Manifest包含：run_id、case selection/checksum、oracle/assertion版本、data/replay引用、semantic/schema、effective publication/override、权限/覆盖/ModelInput、policy、实际model profiles、sandbox/environment、git/image、seed/tolerance、预算和结果引用。

报告包含：

- 一张执行摘要和高严重度失败清单；
- 按 mode/domain/capability/risk 的切片；
- shared gate结果；
- 计划/取数/计算/回答/忠实度的诊断维度；
- latency、token、调用、成本和复现范围；
- 明确哪些未运行、不可用、样本不足或仅fixture。

各 mode 的切片不是三份互相独立的通过结论，更不能用整体平均掩盖 BUILD 偷换口径、泄漏或错误因果。

## 6.6 正确性与统计

保留 V3 的 full-system paired baseline、McNemar 和 paired bootstrap 方法，但只在比较问题需要时执行。

- 正式值按 typed facts/rowset 与独立 oracle 比较，不按SQL字符串或执行成功。
- 正确 clarification/HITL/unavailable/rejection 必须有预标注 expected outcome。
- 分开回答正确率、正确自主完成覆盖、误拒答和非回答正确性。
- 计划质量采用必要字段/约束/歧义/假设/确认忠实度，不采用卡片文本逐字匹配。
- 用户接受率是交互指标，不是正确性证明，也不固定80%门槛。
- 金额/比率、零真值、非数值和集合结果使用对应比较器；MAPE不作为所有结果的唯一指标。
- 同一case一次运行可同时得到fetch、compute、plan-adherence结果。
- 模型多候选/更多CodeAct是否有收益需预登记、样本充分且不越成本/延迟护栏。
- 不做默认组件消融，不把完整系统增益归因一个模块。

配对按稳定case identity和可比较的mode/data/policy条件对齐，不能按数组位置或min(len)截短后比较。缺失、失败和未执行的case必须保留状态；不静默丢弃失败样本来提高lift。

### 6.6.1 Outcome与分母

统一case结果至少可区分：
~~~text
CORRECT_ANSWER
CORRECT_CLARIFICATION
CORRECT_HITL
CORRECT_RESULT_UNAVAILABLE
CORRECT_REJECTION
INCORRECT_ANSWER
INCORRECT_CONFIDENT_ANSWER
FALSE_REJECTION
UNNECESSARY_CLARIFICATION
AUTHORIZATION_FAILURE
SOURCE_ROUTING_FAILURE
PROVENANCE_FAILURE
DATA_QUALITY_FAILURE
CONFIRMED_PLAN_DEVIATION
SANDBOX_OR_MODEL_INPUT_FAILURE
~~~

| 指标 | 分母和解释 |
|---|---|
| 事实/数值回答正确率 | 实际产出回答的适用cases；必须与独立oracle比较 |
| 正确自主完成覆盖 | 预先标注可自主完成的cases；拒答不进入成功数 |
| 正确处理率 | 全部执行cases，按各自expected outcome判定 |
| 误拒答/多余澄清 | 预先标注可合法完成的cases |
| HITL/澄清正确性 | 应触发相应reason/阶段的cases，包含错误触发/漏触发 |
| Plan adherence | 经确认后发生执行的BUILD cases；同时检查实际取数/逻辑而非只看hash |
| Fetch/compute/repair | 使用对应能力的cases或尝试；明确首次与含修复结果 |
| Safety failure | 全部适用的安全/模式/权限case；禁止被平均准确率掩盖 |

缺样本、缺oracle、未运行和不适用分开显示。无数据不能产生伪造的0%/100%统计结论。不同mode适用面不同，不直接用一个不说明分母的总准确率替代切片。

保留企业 golden 200–500 的准备目标和 complaint 约100的历史bootstrap，不把条数替代统计充分性，不为三个模式机械各复制一份200–500。新模式补足真实覆盖和独立oracle后再允许相关发布。

## 6.7 一级风险

任一出现即不得通过对应发布门禁：

~~~text
用户确认 A，实际执行 B
unauthorized disclosure / ModelInput leakage
dangerous SQL or sandbox boundary failure
high-confidence wrong formal value/pattern
unsupported causal conclusion
manual override ignored
memory bypassing current authorization
false validation success / missing required receipt
CodeAct declared reproducible but cannot reproduce within declared criteria
~~~

测试平台只建一套；这些差异化风险断言必须保留。

---

# 7. API 与用户流程

## 7.1 入口

| 方法 | 路径 |
|---|---|
| POST | /api/v2/nl2sql/queries |
| POST | /api/v2/nl2sql/queries/stream |
| POST | /api/v2/nl2sql/analyses |
| POST | /api/v2/nl2sql/analyses/stream |
| POST | /api/v2/nl2sql/builds |
| POST | /api/v2/nl2sql/builds/stream |
| GET | /api/v2/nl2sql/threads/{thread_id} |
| GET | /api/v2/nl2sql/threads/{thread_id}/history |
| POST | /api/v2/nl2sql/threads/{thread_id}/actions |
| POST | /api/v2/nl2sql/feedback |
| GET | /api/v2/nl2sql/capabilities |
| GET | /healthz |
| GET | /readyz |

不因三模式复制 history/actions/feedback/capabilities。上传和外部数据API本轮不增加。

## 7.2 Request/Response 兼容

既有请求保留 messages/thread_id，增加默认兼容 schema_version=2.1 和受限 options。内部新plan版本显式迁移，不用文档V4强行更改URL为v4。

- 8KiB单消息、最多20条、合计32KiB边界保留。
- 普通客户端不提交权威 user/role/tenant/permission、raw SQL、credentials、任意URL或文件路径。
- 消息role/文本不能覆盖服务端system prompt和政策。
- requested route/profile只能在mode允许范围内选择或降低预算。
- QUERY只Fast/Standard；复杂意图给明确切换提议，不自动执行更大包。
- BUILD 创建/实质修改定义先形成 DraftPlanCard 并完成适用业务确认，不直接执行未确认的新语义；已有定义查询、定义保留和结果保存按各自对象流程处理。
- 保存 execution/result artifact、保留/复用 reusable definition 均采用 server-owned 对象引用，重新核验访问和对应版本，不混用保存对象。

公共响应保留 run/thread、status/outcome、route/mode、facts/blocks/citations、semantic/schema/policy、time/freshness/degradation、执行摘要。

按 mode 和实际对象返回 typed artifact view、definition/confirmation/execution 引用及 validation。执行完成、保存结果 artifact、保留定义、确认定义与验证通过分别表达；复用定义的 QUERY 也保留其 definition/receipt 证据。

### 7.2.1 Query请求示例

~~~json
{
  "schema_version": "2.1",
  "messages": [
    {"role": "user", "content": "统计本月各区县投诉首响及时率"}
  ],
  "thread_id": null,
  "options": {
    "requested_mode": "auto",
    "max_latency_ms": 10000,
    "include_explain": false
  }
}
~~~

QUERY的requested_mode只支持auto/fast/standard；不能通过deep、codeact或mode=build字段扩大权限包。include_explain要求独立权限。用户指定更短deadline可以限制执行，不能据此关闭必要验证。

ANALYZE 使用相同消息基础和允许的 profile；QUERY/ANALYZE 可携带已有 SAVED definition 的 server-owned 引用与 typed 参数，读取原 immutable version 后绑定并重验，不自动解析成新 Draft。BUILD 新建/实质修改业务定义时才生成 Draft/version。预算由本次有效 mode policy 提供，不继承例中的 10 秒。

### 7.2.2 响应字段与结果示例

| 字段 | 要求 |
|---|---|
| schema_version | 公共契约版本 |
| thread_id / run_id / trace_id | 服务端可追溯标识；重试不伪造新执行 |
| mode / effective_route | 实际模式与路线，不照抄客户端hint |
| status | 当前attempt生命周期 |
| outcome | answer/clarification/hitl/result_unavailable/rejected，失败/取消时可无业务outcome |
| blocks / facts / citations | typed公共投影，满足权限、grounding和大小限制 |
| semantic_release_id / schema_snapshot_id / policy_version | 实际绑定版本；不能补造 |
| confidence_band | 由 §3.10.1 的语义、来源、发布、质量、新鲜度、验证及 provenance/receipt 完整性证据确定性派生；禁止模型自评分 |
| data_as_of / freshness / time_semantics | 未知可null并标原因；current-state不得变业务年份 |
| degradation_flags / limitations | partial、采样、缺数据、未验证等 |
| execution | elapsed_ms、row_count、budget/receipt摘要，不默认raw SQL |
| plan_ref / confirmed_plan_hash | 按真实对象区分 definition/confirmation/execution refs；confirmed 引用必须有服务端确认记录，适用于复用该定义的 QUERY/ANALYZE，不限 BUILD；动态运行值不进入 definition hash |
| artifact_refs | 适用Analysis/Custom记录，读取仍验证权限 |

示意的“合法但无对应发布行”响应，不代表当前DB具体查询结果：

~~~json
{
  "schema_version": "2.1",
  "thread_id": "<uuid>",
  "run_id": "<uuid>",
  "trace_id": "<uuid>",
  "mode": "QUERY",
  "status": "completed",
  "outcome": "result_unavailable",
  "effective_route": "fast",
  "blocks": [],
  "facts": [],
  "citations": [],
  "semantic_release_id": "<verified-release-id>",
  "schema_snapshot_id": "<verified-snapshot-id>",
  "policy_version": "<verified-policy>",
  "confidence_band": "low",
  "data_as_of": null,
  "freshness": "unknown",
  "time_semantics": "CALENDAR",
  "degradation_flags": ["published_result_missing"],
  "limitations": ["no matching published row in the requested scope"],
  "execution": {"elapsed_ms": 0, "row_count": 0}
}
~~~

示例中的elapsed_ms=0仅是结构占位，实际响应必须填测量值。BUILD预览返回DraftPlan/HITL与可选动作，不把预览状态写成execution completed或validation PASS。

## 7.3 Shared actions

同一 /threads/{id}/actions 处理分类型控制对象：

- HITL decision：confirm/approve、modify、choose、resolve、reject、cancel。
- Artifact operation：save execution/result artifact、归档指定 artifact；不因此创建可复用定义或切换 BUILD。
- Definition retention/governance：retain/save reusable definition、生成指定版本治理候选；语义未变时不因保留操作重复业务确认，不自动重算。
- 复用 SAVED definition 绑定当前参数并自动重验；preview 可展示本次绑定，但不是每次必须人工确认的门禁。只有实际需要的新业务/风险决策才走 HITL。
- 以上区分是目标对象/动作语义，本次不设计新的 API；后续映射既有 shared actions，并保持 target/type/version 和幂等契约。

响应说明 action 是否被接受、是否幂等、当前版本及 run/结果引用。确认并不自动表示计算成功。

确认/恢复可以通过同一动作接口按 Accept 返回JSON或typed SSE；复用同一执行claim，不为stream重新启动计算。默认旧JSON行为兼容。

### 7.3.1 动作请求约束

公共控制字段：target_type/target_id、action、expected_version、idempotency_key；HITL绑定hitl_request_id和适用plan hash。

| Action | 允许输入 | 不允许 |
|---|---|---|
| Confirm / legacy approve | 对应待确认request/plan版本 | 客户端替换confirmed内容或confirmed_by |
| Modify | 实质定义修改的 typed patch/feedback 生成新 Draft/version；合法运行参数变化仅更新 Execution Binding 并重验 | 原地改 Confirmed Definition 后继续执行；把参数重绑定强制当定义变更 |
| Choose | 服务端提供的choice_id | 任意下一节点、URL或权限 |
| Resolve | 缺失slot的typed binding | 绕过语义/权限验证 |
| Reject | 对应待决策目标 | 默默确认其他目标 |
| Cancel | 当前可取消attempt | 将取消标为no_data或自动重启 |
| Save execution/result artifact / archive artifact | 当前 owner 可操作的 execution/result artifact/version | 隐式创建可复用定义、重算、写 Gold |
| Retain/save reusable definition / candidate | 当前 owner 可操作的 definition/version，明确保留或治理动作 | 自动重算、仅因 retention 变化重复业务确认、发布 canonical/外部通知 |

旧请求未带target信息时，只允许明确匹配唯一待决策目标；不能猜选多个pending对象。hitl_request_id/ResumeToken过期、状态变化或version冲突需返回结构化冲突。

示意确认：
~~~json
{
  "target_type": "hitl_request",
  "target_id": "<hitl-id>",
  "action": "confirm",
  "expected_version": 2,
  "idempotency_key": "<client-stable-key>",
  "confirmed_plan_hash": "<server-presented-hash>"
}
~~~

服务端比较已保存的待确认对象，不能把客户端hash当确认事实。应测试同key不同payload、多实例同时确认、已终态重复动作、跨用户、角色撤销、部分执行后修改，以及JSON/SSE两种返回方式的单次执行性。

## 7.4 SSE

统一 envelope：

~~~text
schema_version / event_id / event
run_id / thread_id / trace_id
payload
~~~

公共事件：

~~~text
run.started
context.resolved
plan.ready
clarification.required
hitl.required
execution.started
execution.completed
fact.ready
artifact.ready
answer.delta
answer.completed
run.failed
run.completed
heartbeat
~~~

### 7.4.1 Envelope与事件payload

~~~json
{
  "schema_version": "2.1",
  "event_id": 4,
  "event": "execution.completed",
  "run_id": "<uuid>",
  "thread_id": "<uuid>",
  "trace_id": "<uuid>",
  "payload": {
    "step_id": "<step-id>",
    "receipt_ref": "<verified-receipt-ref>",
    "status": "completed"
  }
}
~~~

| Event | 最小payload |
|---|---|
| run.started | mode、run/trace与有效profile摘要 |
| context.resolved | semantic/schema、resolution_status、允许公开的未解决项 |
| plan.ready | plan_ref/hash/version、draft/validated/confirmed状态 |
| clarification.required | slots、允许的Resolve选项 |
| hitl.required | request_id、reason、安全摘要、version、allowed actions、plan绑定 |
| execution.started | step/kind、预算/数据引用的安全摘要 |
| execution.completed | receipt_ref、status、允许公开的数量/freshness |
| fact.ready | 经过验证和访问检查的fact引用/公共投影 |
| artifact.ready | artifact_ref、kind、validation/lifecycle/freshness |
| answer.delta | 已验证文本或typed block delta，不是原始LLM token |
| answer.completed | 最终公共artifact/facts/citations/degradation |
| run.failed | ErrorEnvelope及可安全公开的终止状态 |
| run.completed | 唯一成功/业务非回答/取消终态及最终引用 |
| heartbeat | 保活、event_id、当前run，不携带新业务结论 |

- event_id在attempt内有序，客户端按run/event去重；不把相同事件重发视为新执行。
- hitl.required后暂停执行并释放资源；HTTP流终结不等于整个逻辑任务已完成。
- 默认Cache-Control no-cache/no-transform、X-Accel-Buffering no，Nginx关闭SSE buffer。
- 声称支持恢复的实现必须验证checkpoint/原plan/已消耗预算；不能仅开放Last-Event-ID CORS header就声称支持重放。
- 新LLM delta能力不能破坏当前“完整结果后才输出”的安全基线。

- payload是白名单typed projection，不透传任意on_chain_end/内部tool事件。
- 当前态占位日期、secret、未批准PII、rawSQL/stack不进入默认流。
- 已验证事实和必要receipt先于数值delta。
- 沿用正常业务结果/HITL 以 run.completed 结束当前 HTTP attempt 的事件兼容约定；HITL payload/线程状态必须明确 suspended/pending，这不是逻辑任务或计算已完成。适用的人类决定后按 §4.3 恢复执行。基础设施失败以 run.failed 结束；取消以 run.completed(status=cancelled) 结束；一次 attempt 唯一终态。
- 断线/取消后5秒内取消下游的既有要求保留；等待人类时checkpoint化。
- 不默认承诺Last-Event-ID原流重放；恢复先查询thread/history并使用明确的resume/action状态，不静默重复POST执行。
- 未知执行状态不能盲重试；同idempotency key不能产生第二次执行。

## 7.5 API必交付行为

- history有界分页：默认20、上限100、明确next_cursor，不一次读取无限checkpoint。
- state/history/artifact视图遵守当前权限，不只看owner。
- actions处理并发、409版本冲突、404非owner和统一ErrorEnvelope。
- feedback写入control/audit成功后才202；重试/幂等策略明确，不再空accepted。
- capabilities来自实际配置/release/依赖/权限，不硬编码可用。
- 保留原bool字段兼容需要时由实际状态派生；新mode能力分层，不让旧codeact=true误解为QUERY可执行。
- manual_override_write/canonical_publish不是Agent工具。
- /healthz只表示存活；/readyz检查必要共享依赖。可选Sandbox/BUILD未就绪不伪装为QUERY数据错误。
- OpenAPI snapshot、例子、错误、SSE payload和兼容测试同PR交付。

错误保留403/404/409/422/503/504的既有语义，扩展mode、plan-confirmation、effective-publication、ModelInput、Sandbox、budget原因。业务不可用/澄清不是一律500。

### 7.5.1 ErrorEnvelope与状态码

ErrorEnvelope统一包含code、safe message、retryable、trace/run引用、gate/reason和允许公开的missing slots。不能包含DSN、secret、原始敏感行或内部栈。

| HTTP | Code/类别 | 行为 |
|---:|---|---|
| 403 | POLICY_DENIED / RELATION_COVERAGE_DENIED | 不用重试或HITL提高权限 |
| 403 | MODEL_INPUT_DENIED | 不改投未批准provider；可合法完成的本地结果仍需明确限制 |
| 404 | THREAD_NOT_FOUND / ARTIFACT_NOT_FOUND | 不泄漏他人对象是否存在 |
| 409 | HITL_VERSION_CONFLICT / IDEMPOTENCY_CONFLICT / THREAD_BUSY | 刷新状态或采用明确新请求，不重复执行 |
| 422 | QUERY_CONTRACT_INVALID | 修正输入 |
| 422 | MISSING_BUSINESS_DEFINITION / UNSUPPORTED_CAPABILITY | 返回缺失条件；不生成近似事实 |
| 422 | CONFIRMED_PLAN_INVALID / PLAN_RECONFIRMATION_REQUIRED | 停止并分类；只有实质业务语义变更或新的命名决策进入适用 HITL，授权失败拒绝，缺依赖不可用，不以确认修复所有无效状态 |
| 503 | CAPACITY_EXCEEDED | 可重试时带Retry-After |
| 503 | SEMANTIC_RELEASE_UNAVAILABLE / EFFECTIVE_PUBLICATION_UNVERIFIED | 不读旧/自动来源兜底 |
| 503 | MODEL_PROVIDER_UNAVAILABLE / BUSINESS_DATABASE_UNAVAILABLE | 不造SQL、值或缓存业务结果 |
| 503 | SANDBOX_UNAVAILABLE / REQUIRED_POLICY_UNAVAILABLE | 关闭相应能力，不在宿主/API内执行 |
| 504 | QUERY_DEADLINE_EXCEEDED | 取消下游并结算已有证据 |

HTTP流已建立后用typed terminal error表达失败，不发送第二个伪HTTP响应；客户端据结构化状态处理。正常的clarification/HITL/result_unavailable走业务outcome，不滥用503。

### 7.5.2 Capabilities、history与feedback细则

capabilities保留model、embedding、semantic_release、approved_join_graph、plan_executor、hitl、trusted_calc等原必要能力，增加query_mode、diagnostic_analysis、metric_lab、sandboxed_codeact、analysis_memory/custom_metric_artifact、daily/publication和manual_override_provenance。

每项包含适用mode、configured/enabled/availability、policy/version、degradation/reason及批准模板/能力范围。值由实际状态生成，不以一个bool承诺全量可执行。旧graph_rag等兼容字段不得被解释成新增通用GraphRAG服务。

history按授权run/checkpoint/artifact投影返回，默认20、最大100、next_cursor；分批返回和权限过滤不能悄悄改写为“完整历史”。feedback的rating/comment只有control持久化成功才202，返回feedback引用；明确可选客户端幂等key及同key不同payload冲突。保存反馈不直接更新运行时QA/定义。

---

# 8. 分阶段 PR 与开发节奏

## 8.1 工作包与依赖

这些是逻辑工作包，不是未来 GitHub PR 号；原依赖表/图保留工作包职责与工程理由，不是 2026-09-22 从 P0 重启的执行队列。当前推荐推进顺序见 §8.18，bounded grouping 可经 live-code readiness 调整；跨仓真实写入另开关联 MR。

| ID | 唯一主要交付 | 实现前置 | 必要退出证据 |
|---|---|---|---|
| P0 | 历史 #28 Slice3 收口职责（非当前下一步） | 当时实际 worktree/diff/ownership | 历史对应 SHA / PG / CI 证据，不用旧 dirty 快照推断当前 |
| P1 | 280+14语义与基础typed contracts | P0 | 解析/集合/别名/time/release/兼容报告 |
| P2 | 共享授权、Coverage、SensitiveField、ModelInput | P0 | 正反向/撤权/provider/数据投影contract |
| P3 | Effective Published Reader、status/DQ/override/product读取适配 | P1、P2 contract | 全key/状态/current-state/override/receipt/真实PG |
| P4 | 共享Plan/Executor/HITL/Budget/Grounded protocol，含P4-Q闭环验收 | P3 | 多step、确认/恢复、计费、grounding、无旁路；QUERY end-to-end baseline PASS（future exit；P4-Q 当前 CONTRACT_READY，见 §8.5.1） |
| P5 | 共享Sandbox与DataRequest broker | P4 contract | 隔离、资源、取消、补数、code/input/output证据 |
| P6 | 共享Artifact仓储和历史复用 | P4 contract | typed variants、owner、stale、retention/replay |
| P7A | ANALYZE模式策略与应用 | P3、P4、P6 | 下钻/模式/材料决策/诊断证据；CodeAct启用另需P5 |
| P7B | BUILD 创作/确认、定义保留及执行复用集成 | P1、P3、P4、P6 | 业务语义确认、忠实度、多轴 lifecycle；CodeAct 启用另需 P5 |
| P8 | API/现有前端兼容/原生三入口 | P7A、P7B及必要backend | OpenAPI/SSE/actions/feedback/capabilities/用户流程 |
| P9A | 统一评测runner、case/oracle/manifest/report | P4 contract | 去重选样、正确性断言、无固定三模型/文本bridge |
| P9B | 统一校准、holdout与放行证据 | P8、P9A、启用能力的真实依赖 | 一个报告含必要切片、冻结policy和高风险门禁 |
| P10A | 兼容部署、迁移、备份/回滚脚本 | P4/P8稳定接口 | 实际预发/恢复/双API/Sandbox/模式隔离演练 |
| P10B | release/canary及总验收 | P9B、P10A、用户Gate | 启用范围完整证据、停止与回滚 |

P9A可与功能开发交错，必须先于真实性能校准。P10A脚本可提前实现，生产部署不能提前。代码依赖和生产数据/安全门禁分开。

~~~mermaid
flowchart TD
  P0 --> P1
  P0 --> P2
  P1 --> P3
  P2 --> P3
  P3 --> P4
  P4 --> P5
  P4 --> P6
  P4 --> P9A
  P6 --> P7A
  P6 --> P7B
  P4 --> P7A
  P4 --> P7B
  P7A --> P8
  P7B --> P8
  P8 --> P9B
  P9A --> P9B
  P8 --> P10A
  P9B --> P10B
  P10A --> P10B
~~~

P5是ANALYZE/BUILD CodeAct capability的条件依赖，不阻止其不需要代码的contract/SQL/TrustedCalc开发。任何声明启用CodeAct的放行范围都必须带P5证据。

## 8.2 P0：历史 Slice3 收口理由

**HISTORICAL ENGINEERING RATIONALE**：原 P0 用于收口当时 Slice3，不是当前 immediate next step；原 a96898d/dirty 观察保持 §1.2 的日期。以下保留历史范围和验收理由；若未来出现相关回归，另做 bounded readiness，不重新启动旧队列。

范围：

~~~text
CandidateVerifier / ordered gates
aggregate/detail source selection
freshness / semantic signature
degradation receipt
metric compiler / corresponding PG fixtures
~~~

不加入三模式、Metric Lab、Memory、Sandbox或人工修正写入。失败只发最小修复brief。缺实际PG证据而声称真实执行链完成时报告GATE_GAP，不用unit绿灯代替。

## 8.3 P1/P2：可以独立准备，不并行写同一worktree

P1 slices：
1. 输入bundle/anchors/280+14 inventory与fingerprint。
2. typed identity、dependency、dimension/category、lifecycle/readiness。
3. CURRENT_STATE Planner-safe投影与storage binding隔离。
4. release/materialization/exact resolution与兼容测试。

P2 slices：
1. 最小AuthorizationContext和RelationCoverage。
2. SensitiveField/ModelInputPolicy及mode envelope。
3. current auth/revision、memory/CodeAct输出、provider fallback负路径。

真实backend字段/权限未确认时contract fixture可以推进，真实serving关闭。不使用空policy作为allow-all。

## 8.4 P3：一套受治理数据面

Slices：
1. effective projection和八字段key，NULL/多正常key/duplicate。
2. schema-supported status、missing、质量与freshness。
3. override origin/revision、发布绑定与receipt。
4. Daily/Publication固定backend adapter及日期/zero/只读契约。

P3 只读对接 effective published result 与 manual override provenance。override 的创建/修改/撤销、precedence、人工维护页及 derived propagation 全部归 §8.9 backend companion work，不进入 Agent 实现清单。

Daily复用backend语义，不能调用“无结果自动计算”的维护接口，也不借用superuser。approved source readiness和weak-light等证据活动从P1后可独立开展；只有真实binding/代码变化才开相应PR，不制造空的“数据扩展平台”工作包。

## 8.5 P4：共享机制先于模式应用

P4 工作包同时包含已实现内核和待交付闭环。P4-S2 DONE 只适用于 §8.5.2；P4-Q 当前为 CONTRACT_READY，完整 typed resume 仍有缺口，不能由一项 slice 完成推导整个 P4 完成。

目标组成（非要求逐项新开 PR）：
1. 公共typed ExecutionPlan、step/receipt union和scope/budget。
2. 公共HITLRequest/Decision/ResumeToken、原子幂等、plan hash。
3. ConfirmedPlan编译、material change detection、修复边界。
4. 公共fact grounding、safe events、checkpoint与取消。
5. P4-Q — QUERY Mode Baseline Closure：快速问询端到端闭环验收。

已有V3 typed实现必须复用。QUERY短路径也是同一内核，不重写成新supervisor。

### 8.5.1 P4-Q — QUERY Mode Baseline Closure

这是 P4 内的 bounded acceptance slice，不增加新的大 PR、executor、benchmark 或工作包依赖。输入为现有 QUERY 入口/兼容 adapter、P1/P2/P3 合同和本阶段共享内核；复用已有入口完成最小集成，P8 继续负责完整公共 API/SSE、三入口和前端整合。

~~~text
question
→ semantic resolution
→ authorization
→ source selection
→ Published Gold / approved compute
→ HITL / clarification when required
→ Grounded Answer
→ receipt
~~~

上图列出闭环要素；前序发现歧义、授权缺口或必须确认的风险时，应立即按共享 HITL 政策暂停，不能先执行再补确认。正常快速问询不强制增加确认步骤。

**验收范围**：用统一 case registry 中的有界代表用例覆盖 Published Gold 与 approved compute 路径，以及语义歧义/澄清恢复、权限拒绝、缺失或不可用状态；检查答案事实与来源/receipt 一致，QUERY 无法隐式升级 ANALYZE/BUILD 或启用 CodeAct。P3 已有 key/status/current-state/override 门禁复用，必要的端到端断言不能被 reader 单元测试替代。

**CURRENT IMPLEMENTED STATE**：P4-Q-CONTRACT_READY，NOT P4-Q-PASS。当前 bounded required case categories 为 published_gold、approved_compute、clarification_resume、authorization_denied、missing、unavailable；各 case 保持 QUERY，CodeAct 不使用。

**未来输出与退出**：只有 required cases 的 real evidence、相同且真实的实现 revision、完整一致的 RealnessWitness、execution/gateway/grounding 证据链齐全，且无 readiness/integrity/contract gaps，才允许 P4-Q-PASS（QUERY end-to-end baseline PASS）。fixture/synthetic/local_integration 不能产生 PASS；真实性标签指证据来源类别，不仅是命令运行地点。P4-Q PASS 仍不自动生产上线，P8 与 P9/P10 的部署/发布门禁独立保留。

### 8.5.2 P4-S2 — Canonical approved compute V1

**CURRENT IMPLEMENTED STATE — DONE**：CANONICAL_APPROVED_COMPUTE_KERNEL_READY。采用 §1.6 的 accepted closeout baseline；PR #41 Draft/Open/unmerged；生产 NOT_ENABLED。

在已选择的 P4-S2 catalog-bound approved-compute 路径内：

~~~text
requested canonical C
→ formal ApprovedCalculationBinding for C
→ governed internal dependency fetch A/B
→ TrustedCalculation C (inputs: <fetch>.value)
→ Verify C
→ one grounded canonical fact for C
~~~

该路径的不变量：

- C 必须由对应正式 binding calculation 产生，不能直接 fetch C 代替要求的计算。正式 Published Gold 直接读取仍可使用其独立有效 reader path；此处不更改 Gold-first。
- 每个 dependency fetch 经过正常 QueryGateway/授权/source/relation/质量门禁并计入 SQL budget；H19c 的具体 join-hop accounting 核验另列 backlog。
- 验证 role → fetch → metric → input ref → context 的精确绑定、binding identity/checksum、template version/checksum、semantic release；不以模板登记或 approved_template_ids 充当 Metric Authority。
- dependency provenance 必须有唯一所属计算，不允许 orphan/multiply-owned provenance、legacy provenance laundering 或重复 canonical producer。
- 每个 requested bound output 必须恰有一个 canonical calculation；计算不能引入未请求的 canonical output。
- 作为 INTERNAL calculation dependency 的 fetch 不独立 grounding 答案事实；计算产出一个 canonical fact。同一个依赖指标在另一个合法、独立请求契约中仍可正常 grounding。

**P4-S2 V1 fail-closed limits**：metric-intent、scalar/single-row dependencies、单个 catalog-bound requested output；不支持 self/nested catalog-bound DAG、recursive canonical DAG、multi-row/alignment/trend/ranking compute、multi-output 或 mixed/multi-canonical bound compiler shapes。禁止嵌套由 validator 与 dependency runner 双层拒绝。限制仅作用于该 V1 路径，不是 QUERY 永久全局限制；扩展属于 DEFERRED CAPABILITY。

生产权威 ApprovedCalculationBinding 发布源及 catalog loader/wiring 尚未就绪；当前 typed runtime/compiler/validator 的可选 catalog 默认 None，不启用正式 catalog。没有权威 source-of-truth 前不能先接生产 catalog。生产启用还需 §8.19–§8.20 的权威、物理绑定、集成证据和发布门禁；本次不实现接线或 H19 修复。

## 8.6 P5/P6：只建设一次

P5 slices：
1. runtime readiness/隔离方案与有限验证profile冻结。
2. disposable worker/资源/取消/secret/网络/文件边界。
3. 数据broker、CodeActReceipt、输出ModelInput与复现。
4. ANALYZE/BUILD不同envelope下的必要差异测试。

P6 slices：
1. ArtifactEnvelope/repository/owner与typed payload，落实 §5.4.1 的 Control PG + 按需受控共享存储契约。
2. AnalysisArtifact历史引用与freshness/撤权。
3. CustomMetricArtifact 的定义版本与结果分离；confirmation、SESSION/SAVED retention、governance 分轴；保存结果 artifact 与保留 reusable definition 分别实现。
4. retention、恢复、输入过期和replay availability。

不单独做三套memory服务或每种artifact一个数据库。

## 8.7 P7A/P7B：只做模式特有差异

P7A：DiagnosticPlan、下钻/分支策略、material-decision HITL、模式与非因果输出。普通人员/工单下钻不重复确认。

P7B：结构化计划卡、确认前探索边界、ConfirmedPlan执行、计划级join/自定义计算、自定义生命周期。首版数据库优先，上传/预测产品化/外部数据后置。

模式代码只通过公共gateway/executor/artifact/HITL服务，不复制底层实现。

## 8.8 P8：一个前端工作台、三个明确入口

- 保留旧QUERY兼容adapter，只做协议和状态转换，不复活旧Agent。
- 同一个thread可有不同mode的明确run，模式切换必须可见且被用户接受。
- PlanCard按任务显示适用字段，不能为简单count强制填写无意义分子/分母。
- Query歧义Resolve、Analysis分支Choose、BuildConfirm/Modify复用同一交互组件。
- retain/save reusable definition 与 save execution/result artifact 分开；任一保留操作均不代表 canonical 或 validation 通过，不自动重算。
- 显示正式/诊断/自定义 provenance，不用同名标题混淆。
- 三模式不会各做一套thread/history/反馈页。

## 8.9 Backend companion MRs

以下由对应 backend/DB 仓负责；列入计划是为了绑定跨仓契约和放行证据，不把其写入功能分配给 Agent。人工修正能力整体在 Agent 开发范围之外。

| MR | 责任 |
|---|---|
| B-AUTH | 真实组织/资源覆盖/权限适配；经证实的字段和revision |
| B-PUBLISH | ManualOverride创建/修改/撤销、precedence、GLOBAL_SUPER_ADMIN权限/审计、人工维护页面、effective published projection与发布证明 |
| B-DATA | Derived invalidation/republication/rollback与查询专用Daily/Publication语义 |

Agent 仅消费只读 effective result/provenance；Release Manifest 绑定 backend revision、contract 与 provenance 引用，不要求 Agent 实现这些 lifecycle 或派生传播功能。

B-DATA可按producer和product接口拆bounded MR，不强塞一个大差异。没有真实Gitee clone时不得在快照伪装提交。对应真实启用被阻塞，纯Agent contract不被整体冻结。

## 8.10 每个PR的必填交付项

~~~text
goal
verified worktree / branch / base / HEAD
dependencies and evidence scope
allowed files/modules
input/output and compatibility contract
non-goals
positive/negative/edge acceptance
focused tests / required integration / CI
exit gate
known gaps / next step
~~~

一轮唯一 coding writer 只做一个 bounded slice；brief 包含必要契约和证据引用，不把整个历史任务一次交给 writer。

## 8.11 开发与Git流程

**CURRENT CONTROL WORKFLOW**：

~~~text
owner/product alignment
→ bounded C2C / verified baseline / exact allowlist
→ ONE MAIN CODING writer
→ STOP_MUTATING / CODE_COMPLETE
→ independent READ-ONLY review
→ parent ACCEPT / REWORK（需要时仅原 writer 做 bounded rework）
→ separately authorized allowlist Git closeout / Draft PR
→ exact-commit CI when applicable
→ EXECUTED
→ ChatGPT parent review
→ owner Gate
→ next bounded slice
~~~

- 同一 worktree 只有一个 writer；可按授权使用只读 reviewer/subagents，不委派写入。
- 写入期间不对半成品运行并发 format/Git 或冲突验证；短时无 diff 不是 writer 停止证据。
- 不自动扩 scope、启动第二 writer、转 Ready 或 merge。Git/CI closeout 与 coding 分开授权，merge 始终由用户明确决定。
- review/validation 失败先分类，业务最小 rework 回同一 writer，infra/flaky/dependency 按实际责任处理。
- 大的下一 slice 使用干净上下文和最小契约引用；真实 base 沿用已核验集成分支，stacked PR 父分支变化后复核 diff/CI。
- ChatGPT 快照不代替 live Git/CI。untracked 文档需修改前后内容/范围证据，不能仅看普通 git diff。
- 文档接受与产品代码授权是两个独立控制：docs-only 修订只检查文档，不执行产品测试或 Git closeout，也不自动授权任何产品 slice；产品代码变更须由 owner 单独批准的 bounded C2C 启动，且该授权不自动扩展到后续 slice。

**HISTORICAL PROCESS — SUPERSEDED 2026-09-22**：原 Luna readiness → Astra coding → Luna validation/Git/CI 流程仅保留过程来源，不再是当前模型路由。现有 AGENTS/launcher 的旧 Sol 手动 gate 或历史模型名不自动授予启动权限；本次按 owner 明确的单文件授权工作，不修改这些文件。未来以当次 owner dispatch 和 bounded scope 控制推进。

## 8.12 Handoff模板

~~~text
C2C: CODING_BRIEF
task: V4-P<n>-S<n>
worktree: <verified>
branch: <verified>
baseline: <SHA>
goal: <one sentence>
scope:
  files: <allowlist>
  behavior: <bounded>
non_goals: <explicit>
acceptance: <positive/negative/compatibility>
existing_contracts: <reused interfaces>
constraints:
  writer: ONE_MAIN_CODING
  no_git: true
  no_push: true
  no_merge: true
  no_full_suite: true
  subagents: READ_ONLY_IF_AUTHORIZED
  no_scope_expansion: true
completion:
  CODE_COMPLETE:
    changed_files
    completed
    focused_checks_if_any
    unresolved_risks
~~~

~~~text
WRITE_LEASE
worktree: <path>
baseline: <SHA>
owner: MAIN_CODING_WRITER
allowed_paths: <allowlist>

# 完成后
owner: NONE
state: WRITE_FROZEN
~~~

~~~text
C2C: EXECUTED
task: V4-P<n>-S<n>
iteration: <n>
workspace:
  worktree:
  branch:
  baseline:
  final_head:
git:
  commit:
  pushed:
  pr:
  target:
changed_files: <paths>
validation:
  scope_match:
  targeted_tests:
  static:
  integration:
  ci:
evidence: <readable refs / diff available>
known_gaps: <concise>
risks: <maximum 3>
NEXT_EXPECTED_STEP: CHATGPT_INDEPENDENT_REVIEW
~~~

## 8.13 V3工作继承与迁移归属

| V3工作 | 当前归属 | 保留与变更 |
|---|---|---|
| PR04 QueryGateway | P0/P3/P4/P10A | 保留SQL/容量基础，对新增业务调用补旁路测试 |
| PR05A Authoring | P1 | 复用IR，扩为280+14和Planner-safe time语义 |
| PR05B Registry/Context | P1/P2/P4 | 保留原子release/snapshot/检索；加强scope与三模式复用 |
| PR06A ModelGateway | P2/P4/P5 | 统一provider，补ModelInput和mode预算；不新增第二网关 |
| PR06B 编排预算 | P4 | 共用typed executor/HITL，不复刻三条自由loop |
| PR07A | P0/P3 | #28收口；真实effective Gold单独接入 |
| PR07B Answer/HITL/API | P4/P8 | 补完整facts、动作、SSE/OpenAPI/兼容 |
| PR08A Trusted Calculation | P4/P5/P7B | 优先可信SQL/算子；生成代码仅隔离worker |
| PR08B 性能校准 | P9B/P10A | 保留规模/资源实验，共用runner与manifest |
| PR09A Typed Trace/Benchmark | P9A | 前置公共harness，退出文本bridge |
| PR09B Enterprise baseline | P9B | 同一任务的必要cases/断言并集，不复制三轮全量 |
| PR10A 发布恢复 | P10A | 加mode/artifact/Sandbox兼容，保留备份/restore |
| PR10B Canary | P10B | 覆盖声明启用范围；共享报告、按风险停止 |
| 可选PR11 Redis | 后置 | 本主PR不引入 |

以下实施卡与 §4–7 契约共同构成 CODING_BRIEF 来源；历史工作包归属不等于当前待办。按 §8.18 和 fresh readiness 确定当前 bounded slice，不一次执行全部目标。

## 8.14 P0–P3实施卡

### P0：历史 Slice3 收口实施卡

**HISTORICAL ENGINEERING RATIONALE**：保留原输入/输出/必测要求，不作为当前下一步。

**输入**：实际worktree/branch/base/HEAD、当前staged/unstaged/untracked/conflict、writer结束依据、已知9项历史差异、相关源码和最后可信CI/测试记录。

**输出**：当前差异清单、scope/ownership报告、focused及必要真实PG验证、最小修复记录、最终SHA/required CI和EXECUTED。

**不纳入**：新的三模式/目录/业务仓/完整Gold/Memory/Sandbox实现。

**必测**：CandidateExpectedEvidence完整性、semantic/source/freshness hard gates、fallback理由、aggregate/detail等价fixture、rowset hash、malformed输入fail-closed、大表time/scan gate。

**退出**：当前SHA的证据可信；历史green不覆盖dirty；缺PG则GATE_GAP。失败只修本slice；来源未知/仍有writer/分支错时停止新编码。

### P1：语义目录与基础契约

**输入**：批准Gold YAML/producer模板、14项legacy定义、别名/QA/view、当前schema/source证据、既有authoring/materialization/registry接口。

**输出**：canonical adapter、typed inventory、依赖/维度/category/time映射、checksum和validation report、candidate release及旧contract转换。

**必测**：280/14精确集合、anchors/merge、duplicate code/alias、unknown relation/column/dependency、cycle、active与pending_source分离、current-state不进Planner存储日期、同输入稳定checksum、失败active pointer不变。

**不纳入**：Agent自有公式、强制所有raw/derived可计算、实数缺失填0、外部/legacy重算、真实业务DB改造。

**退出**：可复核的目录及明确issue；只有满足完整发布条件才激活。Source尚无数据与定义错误分别记录。

### P2：共享权限与输入政策

**输入**：当前auth接口、实际组织/资源树证据、来源分类、后端可提供的revision或等价snapshot/hash、批准字段/provider范围。

**输出**：AuthorizationContext provider、RelationCoverage/SensitiveField/ModelInput policies、Runtime前置检查、policy fingerprint和拒绝/降级映射。

**必测**：city/area/team/employee祖先与sibling、WHERE不能造权限、join每个source、列分类缺失、伪造client context、撤权、provider fallback、memory/CodeAct错误/图片/embedding外发、mode变更不增权。

**退出**：contract fixture可过；真实scope/transport未证实时affected serving关闭。不得宣称原生column_scope已存在。

### P3：有效发布读取与产品读取

**输入**：P1资产、P2授权合同、实际Gold/effective source字段、backend override/publish证明、批准的只读产品接口。

**输出**：PublishedMetricReader、完整key/status adapter、DQ/freshness/override provenance、typed receipts、Daily/Publication只读adapter和日期语义。

**必测**：逐key重复与正常多key、NULLS NOT DISTINCT应用状态、success/partial/no_data/failed/missing、CURRENT_STATE、只读消费自动值80→override85→自动81/82/83仍85及supersede/revoke后的effective结果/provenance、读者不需管理员写权限、binding缺失不自动表兜底、日报零分母/无发布/缺快照、GET不隐式触发计算。Agent 用 contract fixtures/只读集成验证消费行为；override 实际变更、写权限、ETL不覆盖与生命周期证据由 backend companion 提供。

**退出**：真实PG和角色/source契约可核对；fixture不等于有效发布和跨仓证明。业务接口缺真实read-only合同则相应能力不可启用。

## 8.15 P4–P6实施卡

### P4：统一执行与HITL

**输入**：已有V3 typed kernel、P1/P2/P3合同、mode capability/budget、统一plan/receipt schemas。

**输出**：共享Validator/Compiler/Executor、HITL状态与resume、Immutable ConfirmedPlan、累计预算、facts/grounding、公共safe event协议，以及 §8.5.1 P4-Q 的 QUERY 入口到答案/receipt 闭环证据。

**必测**：未知step/悬空ref/cycle、模式硬限制、多SQL/lookup/重试总计费、同SQL去重、material change重确认、同key不同payload、多API并发claim、暂停释放资源、恢复权限与旧state、取消≤5s、低置信/无证据不输出结论；confidence_band 只由确定性证据派生，P4-Q 正常/澄清/拒绝/不可用路径闭环。

**完整 P4 工作包未来退出**：同一 contract fixtures 可重放，且 P4-Q 另外具备 §8.5.1 所需 real evidence/RealnessWitness 后达到 P4-Q-PASS；fixtures 本身不能产生 PASS。当前 P4-S2 DONE 不等于本工作包退出。Confirm hash 不是唯一语义验证；不复制 supervisor/executor。

### P5：共享Sandbox

**输入**：已冻结的隔离设计/runtime/library/有限资源profile、P4 step协议、P2数据政策、FrozenDataset。

**输出**：受控runner adapter、job生命周期、CodeActReceipt、DataRequest补数、资源/取消控制、输出校验和复现报告。

**必测**：DB/network/shell/subprocess/host/socket/secret拒绝、跨job与路径穿越、危险对象反序列化、输出超量、CPU/RAM/time超限实际终止、provider不在worker、恶意输出不能自降敏感性、补数重验、不同mode/profile的实际差异。

**退出**：同一预期生产runtime的真实隔离与复现证据。只有Python单元测试/AST检查不够；不足时capability关闭，不回退进程内exec。

### P6：公共artifact与历史复用

**输入**：公共envelope、两种payload、owner/scope规则、retention/stale/replay政策、plan/dataset/code/receipt refs，以及 §5.4.1 Artifact Persistence Contract。

**输出**：同一 repository/index/访问策略、Control PG metadata/typed JSON/receipts、必要共享内容引用、Analysis/Custom typed artifacts；定义 confirmation/retention/governance 与运行结果独立记录、historical context projection；checkpoint PG 保持恢复职责。

**必测**：保存结果 artifact 与保留 reusable definition 不混用，均不等于验证/发布；保留定义不自动执行或重复业务确认；两类操作各自 owner/version/重复请求幂等；custom namespace 不碰 280/14；撤权后 history/memory/preview 受限；override/data/policy 变化重验、过期输入不冒充可重放；旧 SQL/代码不成为权威；small JSON/receipts 持久化、默认不存大文件、双 API 共享访问/撤权、HITL/checkpoint 引用一致。

**退出**：可读取、可复用定义且有明确当前有效性；不增加独立Experience Store、ResultCache或canonical发布通道。

## 8.16 P7A/P7B/P8实施卡

### P7A：Diagnostic workflow

**输入**：已验证起点、P2/P3/P4/P6能力；需CodeAct的case额外要求P5。

**输出**：DiagnosticPlan/branch策略、有界组织→人员→工单分析、统计/模式/假设、AnalysisArtifact与说明。

**必测**：允许的一次直接employee查询与迭代诊断区别、普通下钻无重复弹窗、material branch/cost/sensitivity才HITL、budget不逐轮重置、选择分母/采样范围、因果越界、manual-origin与自动底表不混为同一依据、Memory stale不冒充当前事实。

**退出**：已声明分析方法有独立oracle/证据和正确限制；不以大量拒答或免责声明代替正确性。不能偷偷转BUILD。

### P7B：Metric Lab

**输入**：用户新建定义或明确要求实质修改的已有定义、授权 schema/资源、P4 共享确认/执行、P6 多轴生命周期；按需 P5。仅要求已有 SAVED 定义结果的请求按本次意图处理，不因此进入 P7B 创作流程。

**输出**：Draft/Confirmed Definition Version、Parameter Contract、metadata/有界探索、受治理 join 与计算、独立 Execution Binding/result、确认/保留/治理多轴、治理候选包；QUERY AD_HOC 不进入定义 lifecycle。

**必测**：新建/实质修改定义业务确认、缺分母/时间/联接语义不可执行、探索确认不代替定义确认、同 contract 技术 repair、实质人口/分母/过滤语义变化新 Draft/version、参数契约内重绑定不新版本、不机械 HITL、独立风险 gate 有效；实际 source/filter/join/中间量对照；保留定义不自动重算，保存结果不创建定义；SAVED rerun 当前重验；custom 对象及历史结果不因治理改为 canonical，未批准库/数据不可绕过。

**退出**：自定义口径按已确认定义和本次合法 binding 执行，定义可显式保留/保存后复用，结果 artifact 可独立保存；忠实度/验证可审查。上传、预测服务和外部数据后置。

### P8：公共API与前端

**输入**：P4控制协议、P7两种应用策略、P3产品读取、实际backend/Gitee契约、§7 schemas。

**输出**：版本化REST/SSE/OpenAPI、shared actions/history/feedback/capabilities、QUERY兼容adapter、三入口与同一PlanCard/决定组件。

**建议交付顺序**：公共REST/错误与持久化 → safe SSE与取消 → 旧QUERY兼容 → 原生三入口/PlanCard/artifact。

**必测**：JSON/SSE协议一致、字段/大小限制、owner/版本/幂等、feedback实际入库、history有界、单次执行/唯一终态、确认A执行B拦截、mode切换清晰、没有旧engine fallback、QUERY接口无法打开CodeAct/BUILD。

**退出**：前后端可依据同一OpenAPI和示例直接联调；业务仓代码独立MR，不在快照伪提交。Schema/版本/源码/CI证据绑定实际修订。

## 8.17 P9A/P9B/P10实施卡

### P9A：统一评测基础

**输入**：已有BenchmarkCase/typed runner/receipt/report、P4 contracts、公共oracle与case标签。

**输出**：一套case registry、executor adapter、assertion/oracle、selection、manifest/report；legacy bridge迁移与默认多矩阵退出。

**必测**：执行成功但值错、receipt缺失、confirmed hash相同但实际分母变、应澄清却回答、应回答却拒绝、共享用例去重但mode差异不漏、缺oracle不PASS、统计分母、预算调用前检查、隐私脱敏、旧manifest/数据迁移不改历史。

**退出**：一次case可产生多维断言，一份报告可切三mode；无需强制三个provider。fake-provider测试只证明harness，不冒充真实准确率。

### P9B：校准与release评测

**输入**：P9A、实际功能版本、批准的有效发布/DB snapshot与独立golden、预登记比较目标和预算、启用scope。

**输出**：有理由的case selection、有限profile实验、candidate→frozen policy、同一统一报告中的模式/能力/风险切片及release结论。

**必测/实验**：30+表/千万行规模、cold/warm/降级/容量、QUERY保护、BUILD忠实度、Sandbox/profile、数据/override/memory变化、正确非回答与误拒答；对需要比较的候选采用paired baseline。

**退出**：calibration与holdout分离；相同manifest可核对；高风险失败为0观察；样本不足如实声明。不能用一份总体平均给未测mode放行。

### P10A：部署与恢复准备

**输入**：稳定API/contract、两仓兼容矩阵、模式/沙箱政策、镜像及迁移脚本。

**输出**：digest/SBOM/扫描、expand migration、candidate构建、双API顺序更新、统一manifest、smoke/rollback/backup/restore runbook。

**必测**：新旧代码与active release兼容、失败candidate不激活、api-b/api-a ready/failover、SSE/HITL恢复、Sandbox关闭不造QUERY故障、secret和网络/端口边界、应用/语义/DB恢复。

**退出**：实际预发演练完成；不依赖破坏性逆迁移或legacy engine。可以提前做脚本，不能提前部署生产。

### P10B：Canary与总验收

**输入**：统一frozen report、required cases和复现引用、完整两仓/权限/effective publish证据、P10A恢复证据、用户放行。

**输出**：按enabled scope的shadow/canary证据、观察窗口/有效样本、停止/回滚记录和最终交付清单。

**必测**：安全事件即时停止；缺benchmark/backup/restore证据阻止扩流；未启用mode不算完成；样本不足不按时钟自动扩大；所有结果能追到实际SHA/image/policy/data。

**退出**：本次数据库优先三模式目标全部满足才总完成。QUERY先上线是阶段成果，不替代ANALYZE/BUILD的必要证据。

---

## 8.18 Post-P4-S2 推荐依赖逻辑

**RECOMMENDED DEPENDENCY ORDER**，不冻结未来 PR 号、数量或逐项单独 slice：

~~~text
docs amendment / owner review / docs accepted（文档控制；不自动授权代码）
→ owner 单独批准的 bounded C2C（代码授权入口；逐 slice）
→ mode-first + semantic resolution
  + shared typed calculation semantic / parameter / provenance contract
→ typed clarification / decision resume
→ QUERY AD_HOC execution
→ Custom Definition contracts
→ definition persistence + SAVED rerun revalidation
→ BUILD lifecycle integration
~~~

共享计算语义先对齐，避免 AD_HOC 与 Custom Definition 形成不兼容表达；不要求先交付全部持久化，也不要求重写已验收 canonical 内核。bounded grouping 经 fresh live-code readiness 调整。P5/P7A/P8 及后续工作依真实能力依赖推进，不因逻辑编号排成固定施工顺序。

另有两条单独受门禁控制的工作线：生产 canonical approved-compute activation，以及 P4-Q real-evidence capture/PASS。两者需协调依赖，但不与 docs accepted、内核 DONE 或彼此的状态等同。权威 binding source-of-truth 不存在时禁止先做生产 catalog wiring。

## 8.19 External / Governance blocker ledger

以下为当前 activation/readiness ledger；未知外部事实不能由本地代码、fixture 或 owner 产品决策补造。责任域表示所需证据的提供方，不虚构已承诺人员或日期。

| 阻塞项 | 当前证据边界 | 放行所需证据 / 责任域 |
|---|---|---|
| Production AuthorizationContext | carrier seam 已有，不等于可信生产权限来源已证明 | Backend authenticated subject、transport、scope、revision/revocation 契约 |
| ApprovedCalculationBinding authority | 无已证明的生产权威发布源 | business/DB/Gold/Semantic 发布身份、版本、checksum 与适用业务口径 |
| ApprovedCalculationCatalog loader/wiring | 当前 typed runtime 未接生产 catalog，默认 None | 上一项成立后的受治理加载/一致性/失效与审计验证；禁止提前接线 |
| Effective publication / manual override | P3 contracts 已有，生产物理有效值/precedence 仍需独立证据 | Backend/DB 的 effective contract、producer/override revision 与只读消费证明 |
| Physical release / source binding | 本地 semantic release 不证明实际生产 source 匹配 | release/publication/source、schema/producer、template/binding 对照 |
| Readonly datasource / product identity | 审查连接与角色不代表产品身份 | 实际只读角色、连接/权限、QueryGateway 路径与禁旁路证据 |
| Freshness / DQ / weak-light | §1.4 的历史观察仍只属于原日期 | 当前 watermark/SLA/DQ/source readiness；缺来源不能填零 |
| CURRENT_STATE physical binding | 八项语义已确认，物理适配仍需真实环境证明 | semantic time → storage value/真实发布时间的契约证据 |
| metric_key ↔ metric_code | typed identity 不自动证明外部 key 一致 | 正式 key/code/粒度/时间/来源双向映射及验证 |
| P4-Q real evidence / oracle / remote DB | CONTRACT_READY；fixture/local 不替代真实证据 | required real cases、真实执行/capture、RealnessWitness、独立 oracle、revision 和完整 receipt 链 |

## 8.20 H19 classified engineering backlog

**ENGINEERING BACKLOG**：依据 accepted post-P4-S2 owner/parent 回传登记，不是九个已确认漏洞，也不自动排为 H19a→H19i 开发队列。优先级分类与缺陷是否已证实是独立列；未独立复核的描述不冒充本轮复现。除明确已有局部证据外，后续 readiness 应先核验可达性、影响与复现。

| ID | Finding / 待核验描述 | 分类 | Evidence source | Verification status | Activation disposition |
|---|---|---|---|---|---|
| H19a | binding 含 unit/precision/rounding/null/zero 字段，runtime 未逐字段独立证明所有语义不变量 | correctness/security before activation | accepted H19 handoff | 本轮未独立复核完整语义覆盖；先确定 normative 字段及其 authority | 若为 canonical 规范字段，启用前须证明执行/验证忠实度 |
| H19b | PlanValidator policy checksum 未绑定 catalog identity/revision/checksum | replay/audit correctness before activation | accepted H19 handoff；planning.py policy_payload 可定位 | 本轮局部静态检查支持该 checksum 缺项，未做整体重放复现 | 启用前完成 catalog revision/checksum 与审计身份契约审查，不能将局部缺项直接等同已发生漏洞 |
| H19c | context-approved edges、prepared steps 与实际 compiler relations 存在需核查的 accounting seam | correctness/security review before activation | accepted H19 handoff | 已记录核查 seam；尚需专项 join-hop accounting 验证，未证实预算绕过 defect | 启用前专项 recon；按复现结果决定修复或记录已有有效门禁 |
| H19d | normal PlanExecutor 外 fabricated/plan-absent receipt 的 grounding 防御深度 | defense-in-depth | accepted H19 handoff | 本轮未独立复现外部可达 shape | 按实际可达性和已有可信 receipt 边界决定加固范围 |
| H19e | grounding projection 可接受比当前 compiler 输出更宽的 shape | defense-in-depth / latent generic-shape issue | accepted H19 handoff | 本轮未独立核验 reachability | 扩展 compiler/plan authoring 前复核，不能据此断言现有路径可利用 |
| H19f | provenance-free legacy TrustedCalculation 兼容面，P4-S2 compiler 不生成该形状 | deliberate V1 limitation / compatibility surface | accepted H19 handoff；planning.py legacy allowlist 分支 | 局部静态检查可定位兼容分支；未复现可达 authority bypass | 保持兼容边界；外部可达 shape 若成立再评估升级处理 |
| H19g | 无生产权威 ApprovedCalculationBinding/catalog 发布 loader/source 接入 typed runtime | external/governance gap | accepted handoff；当前 catalog optional/default None seams | 当前代码默认未接 catalog；不能从仓库断言外部机构不存在任何发布源 | BLOCKS PRODUCTION P4-S2 ACTIVATION，先取得权威来源再接线 |
| H19h | canonical bound output 已有 exact closure；通用未来 plan shape 尚无同等 universal closure 契约 | plan-contract hardening / defense-in-depth | accepted H19 handoff | 本轮未独立核验未来/手写 plan 可达性 | 外部/manual plan authoring 可达或能力扩展时重新审查优先级 |
| H19i | 尚需 canonical compiler → validator → real metric runner → calculator → grounding 集成证据 | integration test debt | accepted H19 handoff；已有 unit/contract closeout | 已有测试不等于该生产式整链已获证明；本轮不运行集成验证 | production enablement 前必须具备相应 integrated evidence，不是可选“desirable”项 |

本表只登记证据和处置门禁，不实施修复、不访问 DB、不授权下一代码 slice。

---

# 9. CI、校准、发布与运维门禁

## 9.1 验证分层

本地iteration：focused unit → related lint/typecheck → necessary integration。

PR/release保留：
~~~text
uv lock --check
Ruff
Pyright
appropriate pytest
git diff --check
Gitleaks
Docker build
Compose contract
real PostgreSQL integration
required dependency/container security gates
~~~

- 项目锁定Python3.13/uv；保留现有.venv，不用系统/Anaconda替代。
- Linux CI实际覆盖Windows不适用的required tests。
- 新数据库测试必须进入postgres-contract实际选择范围，不以旧测试或skip代替。
- P9A修改benchmarks时，将其相关lint/typecheck/contract tests明确纳入同PR的CI检查，不能只因为旧src/tests检查绿色就宣称评测器已验证。
- 核心代码覆盖率≥90%、总体目标≥75%且不低于同口径baseline。未达到明确记录缺口，不能把普通quality绿灯当覆盖率已达标。
- 新功能验收和历史baseline债务分开，不让P0为覆盖率目标无边界补测试。
- V3完整工程/安全门禁保留，不等于每次小修改重复所有重型本地命令。

## 9.2 核心测试矩阵

| 类别 | 必测 |
|---|---|
| Semantic | 280/14、anchors/alias/dependency、失败不激活、rollback |
| Time | inclusive dates、月末/闰日/时区、CURRENT_STATE隐藏storage sentinel |
| Data | complete key、NULL、status/missing、partial、truncate、source binding |
| Override | Agent只读effective值/provenance；ACTIVE优先、ETL不覆盖、supersede/revoke和写权限由backend提供契约/验证证据，operator/reader权限分开 |
| Authorization | org/sibling/coverage、列、join、撤权、客户端篡改 |
| ModelInput | plan/schema/列名/sample/feature生成前门禁；结果/分析证据synthesis前再次门禁；provider变化、文本/代码输出/错误/图片/memory/embedding外发 |
| Mode | 算术/直接比较/同比环比/批准排名不强制 BUILD/ANALYZE；SAVED 身份不决定 mode；结果查询可 QUERY、诊断可 ANALYZE、实质定义创作才 BUILD；不隐式升级、QUERY 禁 CodeAct |
| HITL | reason/actions、checkpoint、hash、原子并发、幂等、修改重验 |
| Plan | 未知step/ref/cycle、per-run累计预算、取消/早停 |
| BUILD | 新建/实质修改定义业务确认；分母/join/去重/NULL/zero、定义忠实度、同义技术 repair；SAVED 重跑自动重验，无新命名决策时直接执行 |
| Compute | Decimal/units、独立oracle、中间量、错误注入、验证有效性 |
| Sandbox | no DB/network/shell/subprocess/host/secret、限额、kill、跨job隔离 |
| Artifact | Control PG typed JSON/receipts 及按需共享内容、默认不存大文件、owner/stale/retention/replay；保存结果 artifact 不创建定义，保留 reusable definition 不自动重算/重确认，不 canonicalize，历史不可冒充当前 |
| API | schema/分页/feedback/错误/SSE顺序与唯一终态、safe delta；confidence_band仅由确定性证据派生 |
| Runtime | 双API、池/队列、preflight、migration兼容、backup/restore |

### 9.2.1 2026-09-22 新增行为与负路径验收

以下是后续实现必须满足的验收契约，不是本轮已执行测试或已实现能力。

| 类别 | 必须覆盖 |
|---|---|
| Semantic resolution | 明确 A/B 输入可解析时受治理执行；唯一权威简称自动解析；实质多解最小澄清；未定义业务概念不发明公式 |
| Authority | 一次 canonical execution 仍保留正式身份；临时无 canonical binding 的 A/B 为 noncanonical；公式相等、模板/allowlist、确认、保存结果/保留定义均不 canonicalize；无假 canonical key |
| Shared semantics | AD_HOC/custom/canonical 可共享 typed 原语；input roles/bindings/provenance 类型化；共享表示不提供额外 authority |
| Definition axes | Draft/Confirmed 与 SESSION/SAVED、governance 独立；Confirmed + SAVED + GOVERNANCE_CANDIDATE + noncanonical 合法；原 custom 对象及历史结果不能原地转正式身份 |
| Version / binding | 合法 month/store_scope 参数变化不新 Draft/version，不因参数本身机械 HITL；改 Parameter Contract、分母/去重/业务时间/joins/NULL/zero/业务相关 unit/precision 才触发实质定义变更 |
| Immutable hash guard | authorization revision、active release、data snapshot、具体参数值、run budget/request/checkpoint 改变不污染 definition hash；execution binding/hash 和 receipts 记录新值；稳定 source-role/provenance 约束仍被验证 |
| Save object guard | save execution/result artifact 不创建定义；retain/save reusable definition 不自动执行/重确认；各操作按具体 target/version 做权限与幂等 |
| Rerun | 当前授权/release/source/policy/relations/capabilities/data/DQ/freshness/预算重验；通过且无新命名决策直接执行；语义歧义澄清，禁止访问拒绝，缺依赖不可用，独立风险门禁仍有效 |
| HITL resume | 创建/实质修改定义保留业务确认；确认不能增权；需要执行的 Confirm/Resolve 恢复到当前重验→compile→execute；定义保留/确认与执行完成分开 |
| P4-S2 scoped invariants | 仅 selected catalog-bound path 禁 direct C 替代计算；独立正式 published reads 合法；内部 dependency fetch 不 grounding，独立请求同指标可按另一合法契约 grounding；scalar/self/nested/single-output 是 V1 路径限制 |
| P4-Q / production | fixture/synthetic/local_integration 不能 PASS；真实 required cases/witness/证据完整才 PASS；PASS 也不自动生产启用 |

## 9.3 性能与稳定性保留

V3 bootstrap：
~~~text
50 concurrent requests
30 minutes
2 API replicas / 1 worker each
SQL active total 8
per API SQL active 4 / queue 8 / wait 3s
~~~

旧70/25/5的Fast/Standard/Deep混合只保留为历史压测参考，三模式正式负载由实际回放校准，不把它写成真实产品分布。

- QUERY先以4/10s候选P95校准；复杂模式独立SLA/成本但共享总容量。
- cold/warm、normal/degraded provider、aggregate/detail分别报告。
- 30+表/千万行fixture在nightly/release；普通CI用小数据。
- 不持有DB连接等待LLM、HITL或Sandbox。
- 断开后5秒内取消下游；停一API后新请求10秒内由另一API承接。
- 内存、连接池、队列不能持续泄漏或无界。
- 复杂模式不能通过新增worker突破总资源上限；测量其对QUERY的影响。
- 无Redis条件下完成核心功能和性能证据。

## 9.4 参数冻结

以下不猜生产数值：
- ANALYZE/BUILD rounds、SQL/model/CodeAct calls、工单/行/字节、deadline/cost。
- Sandbox CPU/RAM/time、library/runtime及复现精度。
- profiling方法/范围和material escalation阈值。
- Memory/artifact retention、stale/replay策略。
- backend relation/override的物理schema和derived propagation。
- mode SLA、队列配额、非劣margin、最小有意义增益及canary阈值。

编码/验证前必须有有限开发profile；生产前经安全验证、业务确认和校准冻结。无法证明满足硬边界的capability保持关闭。Intent/安全/确认不可变性不等待效果校准。

## 9.5 一个Release Manifest

至少绑定：

~~~yaml
release_id: ""
previous_release_id: ""
agent_git_revision: ""
agent_image_digest: ""
backend_git_revision: ""
backend_image_digest: ""
compose_checksum: ""
control_schema_revision: ""
checkpoint_schema_revision: ""
semantic_release_id: ""
semantic_checksum: ""
schema_snapshot_id: ""
schema_snapshot_checksum: ""
definition_fingerprint: ""
producer_configuration_fingerprint: ""
effective_publication_binding: ""
override_policy_revision: ""
business_observation_reference: ""
data_replay_reference: ""
authorization_policy_revision: ""
relation_coverage_policy_revision: ""
model_input_policy_revision: ""
mode_policy_bundle: ""
prompt_model_profile_bundle: ""
sandbox_runtime_image_digest: ""
sandbox_library_resource_policy: ""
artifact_schema_lifecycle_policy: ""
evaluation_run_ids: []
required_case_selection_checksum: ""
enabled_modes: []
enabled_capabilities: []
feature_flags: {}
secret_versions: {}
created_at: ""
created_by: ""
~~~

同一manifest引用mode-specific子policy，不复制三份发布事实。secret_versions只存引用，不存secret。用户日常分析raw数据不装入release manifest。

Manual Override 在 manifest 中只记录 backend revision / contract / provenance：`backend_git_revision` 与 `override_policy_revision` 绑定提供方版本，`effective_publication_binding` 引用实际 effective contract 及其 provenance 证据。该记录不意味着 Agent 实现 override lifecycle、precedence、维护页面或 derived propagation。

Observation timestamp/count不代替可重放dataset，也不证明某YAML产出了某Gold批次。Actual producer/override binding必须有backend证据。

## 9.6 发布顺序

~~~text
冻结当前release和启用范围
→ build/scan/digest/SBOM
→ control/checkpoint backup
→ expand migration
→ schema/semantic/policy candidate
→ contract/fake-provider/必要integration
→ 统一eval required selection + frozen evidence
→ 确认两仓兼容
→ 部署兼容旧active release的新代码
→ api-b ready → api-a ready
→ 激活新的兼容schema/semantic/policy组合
→ Nginx/SSE/HITL/mode smoke
→ shadow / frozen canary
~~~

不能先激活旧实例无法读取的新契约。API/semantic/backend/Artifact/Sandbox升级和回滚使用兼容矩阵，不凭单一image green推断可用。

## 9.7 Canary与停止

保留V3的候选节奏作为校准起点：
- 5%：至少一个业务日且≥100有效请求；
- 25%：至少一个业务日且≥300；
- 100%：连续两个业务日稳定；
- 样本不足延长，不因时间到自动扩流。

按enabled mode/capability采样，同一观测任务统一报告。未启用能力不制造假流量；也不宣称其完成。无需294个标识每个获得生产流量。

发现一例安全/一级语义事件即停止受影响范围：
- 越权/PII泄漏；
- 危险SQL或Sandbox边界失效；
- 高置信错误/无依据因果；
- 确认A执行B；
- 忽略manual override；
- version mismatch或隔离质量问题泄漏。

共享权限/Gateway缺陷不能只关一个mode掩盖。

原性能停止候选值保留作校准参考：5分钟非业务5xx>2%；P95持续15分钟越界；SQL超冻结容量；outbox最旧事件>15分钟或积压>10,000。最终使用冻结policy。

Required benchmark、backup、restore或兼容证明缺失时停止发布/扩流，不能以在线暂未报错代替这些证据。

## 9.8 回滚与备份

回滚顺序按兼容性和影响范围：
1. 关闭受影响mode/capability；
2. 关闭CodeAct/昂贵路径/可选计算；
3. 回退兼容semantic/schema/policy/model组合；
4. 回退Agent/backend镜像；
5. 仅真实数据/schema损坏时DB restore。

不得关闭权限、RelationCoverage、ModelInputPolicy、QueryGateway、只读身份、secret隔离、Sandbox边界或QUERY CodeAct禁用。不回退legacy Agent。

| 对象 | 频率 | 保留与验证 |
|---|---|---|
| control PG | 每日/发布前 | 30天；每周restore test |
| checkpoint PG | 每日/升级前 | 默认30天；HITL抽样恢复验证 |
| external business PG | 数据责任方负责 | 按业务RPO；恢复后只读与binding验证 |
| semantic/release manifest | 每次发布 | 长期；checksum/digest |
| Analysis/Custom artifacts | 按生命周期策略 | owner/revocation/stale/replay/恢复验证 |

备份保留不等于业务memory保存期限。恢复后重验当前权限、effective publication/override、artifact可访问性、idempotency、预算和Sandbox政策。

---

# 10. 交付清单、总验收与立即下一步

## 10.1 必须交付

- 本主计划及只读readiness/证据索引。
- Architecture/data flow和职责边界。
- Canonical/legacy authoring、CURRENT_STATE、Semantic Release/rollback。
- QueryGateway/ModelGateway/权限/ModelInput/RelationCoverage。
- Plan/Execution/ConfirmedPlan、HITL/resume/idempotency。
- Shared Sandbox/DataRequest/FrozenDataset/receipt及安全验证。
- AnalysisArtifact/CustomMetricArtifact/lifecycle/memory访问策略，以及 Control PG + 按需受控共享存储契约。
- Effective Published Result/Manual Override provenance 的只读消费接口与 backend revision/contract 证据；人工修正写入和生命周期由 backend companion 交付。
- API/OpenAPI/SSE/errors/兼容和三入口用户流程。
- 一套评测case/oracle/selection/manifest/report和统计方法。
- calibration/freeze/required CI、deploy/canary/rollback/backup/restore runbook。

后续how-to/reference可分别落在负责的代码PR中；不再复制另一份主计划或三套相同规范。

## 10.2 总验收

| Gate | 完成条件 |
|---|---|
| Definition | 280 canonical/14 legacy全识别、验证、明确disposition |
| Data | complete key/status/current-state/effective publication/override正确 |
| Scope | 权限/coverage/ModelInput/字段与撤权通过 |
| Kernel | 一套typed executor/HITL/预算/receipt，无旁路 |
| QUERY | 未来 required real-evidence cases 达 P4-Q-PASS；当前仅 CONTRACT_READY；正式事实、approved 计算、错误/澄清/不可用正确；AD_HOC/已有 custom 执行另按新增功能范围验收 |
| ANALYZE | 有界下钻、方法/分母/证据、非因果默认、artifact |
| BUILD | 创作计划卡/业务确认/稳定定义不可变/执行忠实度；参数与运行绑定分离、定义保留与结果保存分开；SAVED 重跑自动重验，无新决策不机械确认 |
| Sandbox | 同一runtime满足已启用profiles的隔离/限制/复现 |
| Artifact | Control PG + 按需受控共享存储契约、当前权限、stale、retention、历史和canonical边界；checkpoint职责保持 |
| API/UI | 三入口与共享状态/动作/反馈/SSE实际通过 |
| Evaluation | 一套runner，required cases并集，独立oracle和高风险断言齐全 |
| Release | 同一manifest、两仓依赖、模式启用、canary和恢复证据 |
| Review | 独立复核及每次merge的用户明确确认 |

可以分阶段报告QUERY/ANALYZE/BUILD可用，但全部首版目标未通过不能标记总V4完成。后置的上传、预测产品化、外部数据和Action Plane不列为本次欠交付。

## 10.3 立即下一步

~~~text
2026-09-22 V4 amendment（仅本文，文档控制）
→ owner / parent full consistency review
→ docs accepted（不自动授权代码）
→ owner 单独批准的 bounded C2C（代码授权入口）
→ 按 §8.18 推荐依赖实施
~~~

不再采用旧 P0/Slice3 → P1 immediate-next 队列。文档接受本身不授权产品代码；产品代码变更只经 owner 单独批准的 bounded C2C 启动。本轮 amendment 未自动授权代码，首个 bounded product slice（MODE_SEMANTIC_SHARED_CALC_CONTRACT_V1）已另获 owner 授权，后续 slice 仍需各自 bounded dispatch。不得提前接入缺权威 source-of-truth 的生产 catalog。

## 10.4 本次成稿检查边界

本轮整合已接受的 2026-09-22 amendment、五项精确化修正及两项 consistency guards，并在此基础上按 owner 冻结的 A1–A16 amendment matrix 做增量对齐（§0.7、§3.8.1–§3.8.2、A.6），只修改本文件。验证范围为全文规则一致性、Save 对象、definition hash/运行环境边界、状态/证据分层、mode/authority 分离、共享计算语义 authority invariant、Markdown 结构、原历史观察保留和文件范围。

本 docs 阶段未运行产品测试、未访问真实 DB、未改源码/测试/config/参考材料、未 stage/commit/push/更新 PR/触发 CI。§1.6 的测试是已接受历史 closeout/CI 证据，不是本轮重跑。文档接受不自动授权产品代码；另获 owner 单独批准的 bounded product slice 属独立代码控制，不在本 docs 边界内。

PLAN FROZEN / EXECUTION BASELINE 表示当前产品与架构规则已对齐；实现进度以 §0.5 的状态层和具体证据为准。预算、CodeAct resource limits、retention 和 provider-specific policies 仍按相应实现/校准门禁冻结。文档中的“必须/DoD/Gate”不冒充完成证明。

---



# 附录 A. 来源与覆盖

## A.1 依据

- [V3主计划](E:/平台开发/tt-ai-main/MASTER_PR_PLAN_V3.md)
- [用户指定的详细颗粒度参照稿](<C:/Users/Density/Downloads/TT-AI MASTER PR PLAN V4.md>)
- [当前操作索引](<C:/Users/Density/Downloads/TT_AI_CURRENT_LOCAL_INDEX (1).md>)
- [V4.1对齐稿](<C:/Users/Density/Downloads/TT-AI MASTER PR PLAN V4.1 — 准确率优先的横向 Data Agent 生产收敛计划.md>)
- [第一轮产品review](C:/Users/Density/Downloads/TT_AI_V4_POST_REVIEW_ALIGNMENT_SUMMARY.md)
- [三模式增量review](C:/Users/Density/Downloads/TT_AI_V4_2_INCREMENTAL_REVIEW_ALIGNMENT_2.md)
- [早期HITL/CodeAct计划](C:/Users/Density/.cursor/plans/industry-leading_nl2sql_upgrade_d9e1adc9.plan.md)
- [开发协作规范](<C:/Users/Density/Downloads/TT-AI Codex开发节奏和主从子智能体协作规范及路由.md>)
- [最终七项修改alignment](C:/Users/Density/Downloads/TT_AI_LATEST_PR_FINAL_MODIFICATIONS_ALIGNMENT.md)

本轮用户明确质疑三套benchmark并选择首版数据库计算，这两项高于附件中的旧THREE-BENCHMARK_MODEL=ALIGNED和宽泛首版能力列表。

## A.2 V3必要内容覆盖表

| V3内容 | 本文位置 |
|---|---|
| 部署/启动/容量 | §2.3–2.6、§9.3、§9.6–9.8 |
| Authoring/Control PG/Release | §3.1 |
| SchemaSnapshot/大表策略 | §3.2–3.3、§3.9 |
| 核心typed contracts | §4.1、§5.4 |
| Context Compiler | §3.2 |
| DataQuality/PlanValidator | §3.11–3.12 |
| Route/Budget/早停 | §4.6 |
| MetricQueryCompiler | §3.7–3.10 |
| CandidateVerifier/rowset | §3.9 |
| Grounded Answer | §3.10、§7.4 |
| Plan/TrustedCalc/CodeAct | §3.8、§4、§5 |
| API/HITL/SSE | §4.2–4.5、§7 |
| PR/单写/验证/Git/User Gate | §8 |
| Benchmark/统计/性能 | §6、§9.1–9.4 |
| Release/Canary/Rollback/Backup | §9.5–9.8 |
| 文档与总验收 | §10 |

## A.3 实施者不得误读的结论

- 三种产品意图不等于三个Agent runtime或三个benchmark平台。
- 同一评测运行可以产出多个切片；该测的差异不能因去重被删。
- More capability不等于more data privilege。
- ConfirmedPlan hash不等于任意Python已被证明语义正确。
- Artifact已保存不等于结果已验证或已canonical。
- CURRENT_STATE不等于2000年历史月份。
- Manual override优先不等于Agent可写Gold。
- 历史DB/CI观察不等于当前生产证明。

## A.4 最终七项修订定位（2026-09-16）

**HISTORICAL OBSERVATION — RETAINED FOR PROVENANCE**：下表保留 2026-09-16 决策及当时落点。被取代的规则显式标记；当前契约以正文对应位置为准，不将旧决策伪装成从未存在。

| 最终 alignment 要求 | 正文与执行落点 |
|---|---|
| Manual Override 整体归 DB/Backend；Agent 仅只读effective result/provenance | §3.6、§8.4、§8.9、P3实施卡、§9.5 |
| 当前全市系统管理员为 GLOBAL_SUPER_ADMIN within current city-wide system scope | §3.6、B-PUBLISH职责 |
| P4 内增加 QUERY 端到端 bounded acceptance slice | §8.1、§8.5.1 P4-Q、P4实施卡、§10.2 |
| Control PostgreSQL + Controlled Shared Artifact Storage | §2.3–2.4、§5.4.1、P6实施卡、§9.2 |
| 每次外部模型调用前执行ModelInputPolicy；生成前与synthesis前分别核验 | §3.5、§5.3、§9.2 |
| 保留confidence_band，只能由确定性证据派生，禁止LLM自评分 | §3.10.1、§7.2.2、P4实施卡、§9.2 |
| BUILD首次及saved metric每次重跑均强HITL；降低确认仅未来校准（SUPERSEDED PRODUCT RULE — 2026-09-22） | 原落点 §5.2、P7B、§9.2、§10.2；现由 §2.1 mode-first、§4.5 定义/参数/运行分离、§4.2 reason-oriented HITL、§5.2.2 自动重验取代 |

**历史范围声明（2026-09-16）**：当时“不改原 DB review、指标全集、三模式边界、统一评测设计、已确认开发节奏和既有主要算法/API细节”的限制属于该次修订，不禁止 owner 后续更新产品规则。2026-09-22 保留原 DB/指标全集/统一评测事实，按最新 owner alignment 修订 mode、definition、authority、HITL、流程与当前状态；不新增竞争主计划。

## A.5 2026-09-22 amendment / change log

| 修订面 | 当前结论及正文落点 |
|---|---|
| 真源 | 按 claim 类型选择 authority source；实现与目标不符记 gap，§0.6 |
| 产品模式 | 算术/比较/SAVED/执行次数不决定 mode/canonicality；QUERY 直接比较与 ANALYZE 诊断分开，§2.1 |
| 语义解析与共享计算 | 显式公式、唯一/多解、禁止发明；共享 typed semantic/parameter/provenance core，§3.8.1–§3.8.2 |
| 定义与权威 | 多轴模型、原 custom invariant=noncanonical、正式身份另建/关联；Method Authority != Metric Authority，§4.1、§5.2 |
| 参数与最终 guard 1 | Immutable Definition Version/hash、Parameter Contract、Per-run Execution Binding 分离；动态 auth/release/data/参数/budget 不改定义版本，§4.1.2、§4.5 |
| 保存对象与最终 guard 2 | 所有 active Save 指明 result/execution artifact 或 reusable definition，不新设计 API，§5、§7.3、P6/P7B、§9.2 |
| HITL / rerun | 新建/实质修改业务确认；合法参数重绑定不机械确认，独立风险 gate 保留；失败按原因分流，目标恢复后执行，§4.2–§4.5、§5.2.2 |
| 当前工程证据 | P4-S2 DONE / PR #41 Draft/Open/unmerged；本地与 CI 测试分别标来源，§0.5、§1.6 |
| P4-S2 / P4-Q / production | 路径限定不扩大为 QUERY 全局规则；CONTRACT_READY、未来 real PASS 与生产启用分层，§8.5 |
| 流程/依赖 | 单 writer、只读 review、授权 Git/Draft/精确 SHA CI、owner Gate；推荐依赖不冻结 PR 粒度，§8.11、§8.18、§10.3 |
| 阻塞与 H19 | 外部绑定不补造；H19 evidence/verification/activation 分列、不把分类当已证实 defect；H19i 启用前必需集成证据，§8.19–§8.20 |
| 历史 | 原 2026-09-14 DB/CI、280/14、weak-light、CURRENT_STATE、V3 校准和 A.4 保留；旧操作规则原位修订/历史标注 |

来源：owner 接受的 docs-only C2C task `v4_master_plan_20260922_incremental_amendment`（2026-09-22），以及本会话最后确认的五项精确化修正与两个 consistency guards。H19 未复现项目按 §8.20 保留证据限制；本次只形成待 parent 全文 consistency review 的文档结果，不声明 parent ACCEPT 或产品实现完成。

## A.6 2026-09-22 amendment matrix A1–A16 定位（本轮增量对齐）

**CURRENT PRODUCT RULE — FROZEN**：本轮在既有 2026-09-22 amendment 基础上，按 owner 冻结的 A1–A16 修订矩阵做增量对齐。新增/强化落点如下；历史矛盾项按 §8.11 与 A.4 保持 HISTORICAL / SUPERSEDED，不被静默改写。

| 增量 | 落点 |
|---|---|
| 完整 A1–A16 冻结矩阵 | §0.7 |
| claim 类型 authority source（live code 不得覆盖 owner 目标规则） | §0.6 |
| mode-first、ProductMode 与 QueryPlan.intent 分离 | §2.1、§3.8.2 |
| semantic resolution vocabulary（additive，不替换 resolution_status） | §3.8.1 |
| 共享 typed calculation semantic/parameter/provenance contract 冻结 + authority 分离 invariant | §3.8.2 |
| 状态层（P4-S2 DONE / P4-Q CONTRACT_READY / production NOT_ENABLED）与实现待完成项 | §0.5、§1.6 |
| H19 evidence/verification/disposition 分列 | §8.20 |
| 依赖逻辑与不冻结 PR 粒度 | §8.18、§10.3 |

**本轮范围与授权**：本 amendment 属文档控制，不自动授权产品代码。首个 bounded product slice `MODE_SEMANTIC_SHARED_CALC_CONTRACT_V1`（共享 ProductMode、语义解析词汇、共享 typed calculation semantic/parameter/provenance contract）经 owner 单独批准，只落地 contract-only 代码，不启用新执行行为，不进入 AD_HOC 执行、Custom Definition 持久化、HITL resume 集成或生产 canonical activation；该授权不自动扩展到后续 slice，后续 slice 仍需各自 bounded dispatch。
