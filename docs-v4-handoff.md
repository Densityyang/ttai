# V4 开发交接文档（跨窗口上下文）

> 本文件是**新窗口的唯一上下文来源**。它记录项目身份、冻结规则、已完成工作、进行中工作、阻塞项、
> 方法论纪律、已知陷阱、测试基线与 owner 决策。
>
> 生成时间：本会话末尾。生成者：上一窗口的 agent（下称"前任"）。
> **新窗口的第一件事：完整读本文件，然后读 `NEW_WINDOW_PROMPT.md`。**

---

## 1. 项目身份

| 项 | 值 |
|---|---|
| 仓库（本地） | `E:\平台开发\ttai-pr07a-next` |
| 工作目录 | 同上（= session cwd） |
| 分支 | `agent/v4-p7-typed-continuation` |
| 已提交 HEAD | `4b3f6db`（"feat(v4): close the algorithm-layer gaps…"） |
| 已推送 | 是（`origin/agent/v4-p7-typed-continuation` = 4b3f6db） |
| 工作区 | **有大量未提交改动**（见第 5 节），全部为"P7 之后"的一批切片 |
| 目标仓库（后端/agent） | GitHub `Densityyang/ttai` |
| 目标仓库（前端） | Gitee `huang7899135/tt-intelligent` |
| 主干 main | `41af2c470e25d632ebb753e4024419dc323aabc6` |
| 已开的 PR | **#45**（open, mergeable=true, 67 files, +20708/-429） https://github.com/Densityyang/ttai/pull/45 |
| PR #45 的 CI | **`quality` 作业失败**（Whitespace gate）——**前任已在工作区修复，但尚未提交/推送**（见 §9.1） |
| 其它 CI 作业 | container ✅ / compose-contract ✅ / postgres-contract ✅ / secrets ✅ |

### 1.1 技术栈
Python 3.13 · FastAPI · Pydantic v2 · LangGraph · PostgreSQL · SQLGlot · uv（Windows 虚拟环境在 `.venv`）

---

## 2. 环境与命令（Windows，必须照做）

```powershell
$env:HOME = "C:\Users\Density"
$env:USERPROFILE = "C:\Users\Density"
$env:PYTHONUTF8 = "1"
$env:TTAI_RUN_POSTGRES_INTEGRATION = "0"   # 集成测试默认关闭；需要时置 1
```

- 临时目录：`C:\Users\Density\AppData\Local\Temp\`
  **注意**：`run_code` 里 `process.env` 是空的，`os.tmpdir()` 会是 undefined——**必须用上面这个字面路径**。
- 跑测试/工具用 `.venv\Scripts\python.exe`、`.venv\Scripts\ruff.exe`、`.venv\Scripts\pyright.exe`。
- 临时脚本需 `PYTHONPATH=<repo>` 与 `AUTH_ENABLED=false`。
- **GitHub token**：用 `git credential fill`（host=github.com）取，**绝不打印**。
- **api.github.com 必须走代理** `http://127.0.0.1:7897`。
- 集成测试会自建 PG17 容器（`TTAI_RUN_POSTGRES_INTEGRATION=1`），**必须在 finally 里 `docker rm -f -v`**。
- 本机常驻服务：前端 :5000、tt-api :9000、V4 API :9001、tunnel :15432、tt-redis/tt-postgres(7432)。

---

## 3. 产品语义（**冻结规则，不可自行修改**）

主计划文件：`MASTER_PR_PLAN_V4.md`（2744 行）。以下为**必须遵守**的冻结内容。

### 3.1 三模式（§2.1）

| 维度 | QUERY | ANALYZE | BUILD |
|---|---|---|---|
| 用户目的 | 获取答案/结果，含直接比较、同比环比、批准排名 | 解释差异原因、调查、有界诊断 | 创建或实质修改可复用业务语义 |
| 能力包 | 正式问询、批准比较/排名/有界明细；受治理 AD_HOC 和已有定义执行 | 多层下钻、诊断统计 | 自定义分母/cohort/窗口/组合/计划级 join 的定义创作 |
| CodeAct | 禁用 | 受控可选 | 受控可选，额度可更大 |
| HITL | 未解决歧义或明确允许的风险决策 | 重要分支、成本/敏感性变化等命名决策 | 新建/实质修改定义的业务确认 |

**模式由本次用户意图决定**；算术/comparison/保存结果/保留定义/SAVED 身份**本身都不选 BUILD**；模型**只能建议**切换，**不能默默升级** mode/capability。

### 3.2 A1–A16（冻结产品规则，逐字摘要）

- **A1** MODE 由本次用户意图决定，与 definition lifecycle、authority 独立。**QUERY 含**直接事实检索、直接比较、同比环比、批准排名、canonical 计算、**临时 AD_HOC 计算**、执行已有 SAVED 定义；ANALYZE = 解释/调查/诊断；BUILD = 创建/实质修改可复用语义。
- **A2** 一次性执行若解析到正式 canonical metric identity 并使用其正式发布结果/权威定义或 binding，结果可具 canonical authority；未采用正式身份的临时 A/B 为 noncanonical AD_HOC；**公式等价不能 canonicalize**。
- **A3** Save 必须指名对象：save execution/result artifact（**artifact 操作；不建定义、不选 BUILD、不 canonicalize**）≠ retain/save reusable definition（不自动执行）≠ create/materially modify definition（BUILD）。
- **A4** Custom Definition 多轴（Confirmation / Retention / Governance / Authority），**不是一条线性状态链**；原对象 invariant = **noncanonical**；治理产生**独立 canonical identity** 并以 provenance 关联。
- **A5** 分离 Immutable Definition Version / Parameter Contract / Per-run Execution Binding；auth revision、active release、data snapshot、具体 month/store_scope、budget、freshness、**普通重跑时间不新建定义版本**；**契约内参数重绑定不机械业务确认**。
- **A6** **实质语义变化**（分子/分母/population/去重/业务时间/join/NULL/zero/业务显著单位精度/Parameter Contract 扩展）**需新 Draft/version 与新业务决定**。
- **A7** 重验分支：**EXECUTE / CLARIFICATION / BUSINESS-RISK DECISION / DENY / UNAVAILABLE**；**人类确认不能修复缺失授权**。
- **A8** Semantic resolution vs invention：显式公式/关系 + 唯一解析输入可受治理执行；唯一权威实质有效含义自动解析；两个以上实质不同含义最小澄清；**无权威含义且用户未提供则不发明**。
- **A9** 分化前冻结共享 typed calculation semantic core；**共享语义表示 ≠ 共享 authority**。
- **A10** Method Authority != Metric Authority；模板/注册/allowlist 只能授权方法实现，**不能单独授权结果为 canonical business metric**。
- **A11** P4-S2 规则 path-scoped：仅 selected catalog-bound approved-compute path 内 target C 必须由正式 binding 计算产生、不得直接 fetch 替代。
- **A12** 状态分层：**P4-S2 DONE != P4-Q PASS != production enabled**。
- **A13** 测试证据 provenance 分开；不作同一运行比较。
- **A14** H19 表分离 FINDING / EVIDENCE SOURCE / VERIFICATION STATUS / CLASSIFICATION / DISPOSITION；**分类不等于已证实缺陷**。
- **A15** 流程：owner alignment → bounded C2C → **ONE MAIN CODING writer** → CODE_COMPLETE/STOP_MUTATING → 独立只读 review → 授权 closeout/exact SHA/CI → owner merge。
- **A16** 全文 stale 词扫描。

### 3.3 §2.1.1 Mode / Definition / Authority 示例矩阵（关键行）

| 用户目标/动作 | mode | 定义生命周期 | Authority |
|---|---|---|---|
| 临时"销售额 / 营业门店数"且不采用正式 binding | **QUERY** | 一次 Execution Spec；不自动创建 Custom Definition | **noncanonical AD_HOC** |
| 查询正式发布的每店销售额或执行其 approved binding | QUERY | 正式定义/身份 | canonical，仍须过全部门禁 |
| "定义门店产出 = …" | **BUILD** | Draft/Confirmed；SESSION/SAVED 分轴 | 原 custom 对象**始终 noncanonical** |
| 获取已有 SAVED 定义结果 | QUERY | 原 immutable version + 新 Execution Binding | 仍 noncanonical |
| 收藏/保存执行结果 artifact | **Artifact 操作，不因此切 mode** | **不创建可复用定义** | 保留原 authority/provenance |
| 提交 GOVERNANCE_CANDIDATE | 治理候选操作 | 原定义保留，治理轴变化 | 仍 noncanonical |
| 正式 business/DB/Gold/Semantic publication | 外部正式治理 | 创建/关联**独立 canonical identity** | 新正式身份取得权威，**不改写原 custom 或历史结果** |

### 3.4 §4.2 / §4.3 HITL

- **§4.2** HITL reasons 列表含 **`CUSTOM_METRIC_PLAN_CONFIRMATION`**（用于公式/解析确认）。
- **§4.3** 统一动作 **Confirm / Modify / Choose / Resolve / Reject / Cancel**；legacy `approve` = Confirm 别名；
  resume **必须重验当前授权/策略/数据绑定/剩余预算**；**人工等待不得让计算 deadline 继续计时**；
  **已消耗预算不得重置**。

### 3.5 §5.4.1 Control PostgreSQL
Control PG 负责 metadata / typed JSON / hashes / references / lifecycle；**不默认持久化大文件**。

### 3.6 §10.2 总验收 13 道 Gate

| Gate | 完成条件 |
|---|---|
| Definition | 280 canonical / 14 legacy 全识别、验证、明确 disposition |
| Data | complete key/status/current-state/effective publication/override 正确 |
| Scope | 权限/coverage/ModelInput/字段与撤权通过 |
| Kernel | 一套 typed executor/HITL/预算/receipt，**无旁路** |
| QUERY | 未来 required real-evidence cases 达 P4-Q-PASS；**当前仅 CONTRACT_READY**；AD_HOC/已有 custom 执行另按新增功能范围验收 |
| ANALYZE | 有界下钻、方法/分母/证据、非因果默认、artifact |
| BUILD | 创作计划卡/业务确认/稳定定义不可变/执行忠实度；参数与运行绑定分离、定义保留与结果保存分开；SAVED 重跑自动重验，无新决策不机械确认 |
| Sandbox | 同一 runtime 满足已启用 profiles 的隔离/限制/复现 |
| Artifact | Control PG + 按需受控共享存储契约、当前权限、stale、retention、历史和 canonical 边界 |
| API/UI | 三入口与共享状态/动作/反馈/SSE 实际通过 |
| Evaluation | 一套 runner，required cases 并集，独立 oracle 和高风险断言齐全 |
| Release | 同一 manifest、两仓依赖、模式启用、canary 和恢复证据 |
| Review | 独立复核及每次 merge 的**用户明确确认** |

> 可以分阶段报告 QUERY/ANALYZE/BUILD 可用，但**全部首版目标未通过不能标记总 V4 完成**。

### 3.7 §8.18 推荐依赖顺序（剩余项）

```
docs amendment → owner review → docs accepted（文档控制；不自动授权代码）
→ owner 单独批准的 bounded C2C（代码授权入口；逐 slice）
→ mode-first + semantic resolution + shared typed calc contract   ✅
→ typed clarification / decision resume                          ✅
→ QUERY AD_HOC execution                                         ✅
→ Custom Definition contracts                                    ✅
→ definition persistence + SAVED rerun revalidation               ✅
→ BUILD lifecycle integration                                    ✅（本会话）
```

### 3.8 §8.19 外部/治理阻塞台账（**11 项，全部需要真实生产环境/数据**）

| 阻塞项 | 放行所需证据 |
|---|---|
| Production AuthorizationContext | Backend authenticated subject、transport、scope、revision/revocation 契约 |
| ApprovedCalculationBinding authority | business/DB/Gold/Semantic 发布身份、版本、checksum、适用业务口径 |
| ApprovedCalculationCatalog loader/wiring | 上一项成立后的受治理加载/一致性/失效与审计验证；**禁止提前接线** |
| Effective publication / manual override | Backend/DB 的 effective contract、producer/override revision |
| Physical release / source binding | release/publication/source、schema/producer、template/binding 对照 |
| Readonly datasource / product identity | 实际只读角色、连接/权限、QueryGateway 路径与禁旁路证据 |
| Freshness / DQ / weak-light | 当前 watermark/SLA/DQ/source readiness；**缺来源不能填零** |
| CURRENT_STATE physical binding | semantic time → storage value/真实发布时间的契约证据 |
| metric_key ↔ metric_code | 正式 key/code/粒度/时间/来源双向映射及验证 |
| P4-Q real evidence / oracle / remote DB | required real cases、真实执行/capture、RealnessWitness、独立 oracle、完整 receipt 链 |

### 3.9 §8.20 H19 分类工程 backlog（**状态见第 7 节**）

**原文重要限制**：「本表只登记证据和处置门禁，**不实施修复、不访问 DB、不授权下一代码 slice**。」
→ **任何 H19 修复都需要 owner 单独授权。**

---

## 4. 方法论与纪律（**前任用血换来的，务必遵守**）

### 4.1 最高原则：**"接线存在 ≠ 路径可达"**

本项目**已出现六次**：代码、图边、常量、类型、测试全都在，但**真实路径永远走不到**。

| # | 看起来完好 | 实际 |
|---|---|---|
| 1 | typed continuation 的图边与 `continuation_ready` | resume 时 runtime scope 为空、读**幻影 state key**、clarify **绕过路由节点**、execute 用错 plan —— 4+1 个阻断点 |
| 2 | 生产 HTTP resume 路由 | API 映射表**缺 `confirm`** → **100% 失败**，57 个测试全绿却漏掉 |
| 3 | AD_HOC 三层 | 引擎分发从未接线 |
| 4 | `EX` 指标 | 40 条里 39 条无 oracle，`(None,None)` 判"对" → stub 得 **0.975** |
| 5 | 探索确认对象（19 测试全绿） | **除自身外零引用** → 产品上不可达，必测项**空真** |
| 6 | canonical 整链（"已有 unit/contract closeout"） | 那些测试用 `_RecordingCompiler`+`_FakeGateway`+手工拼记录 → **从来不构成整链证据** |

**因此**：
- 每条"已实现"结论**必须有执行级证据**（真的驱动真实路径并观察结果）。
- **禁止**源码字符串断言作为能力证据。
- **禁止**用 mock 冒充真实组件来宣称整链已证。
- **禁止**范围过窄的断言（前任曾用两个文件的 grep 推断"全仓 0 命中"，**结论是错的**）。
- 失败代理的产出**必须先评估再决定**——多次发现它们其实已完成，只是没发报告。

### 4.2 并行化纪律（避免两个写者改同一文件）

- **按文件所有权切片**，每个 slice 的任务书必须写明 **"严禁修改"清单**。
- 跨 slice 的 schema 缺口是**主要集成风险**（曾因 F1 加了 `manual_origin_ids` 而 S3 的 schema 未同步 → 12 个测试失败）。
- **代理全部停止后必须跑全量门禁**。
- 代理**任务过大会中途失败**（本会话失败 4 个）。**收窄任务书**能显著提高成功率。
- 代理可能在仓库里**留下 scratch 文件**——必须清理。

### 4.3 验证纪律
- 独立探针/脚本放 `%TEMP%`，**绝不轻信代理自述**。
- **反事实对照**是最强的验证（例：移除等待记录 → deadline 确实超时，证明"等待不计时"来自扣除）。
- 代理给出的**"失败"或"不可达"结论往往最有价值**——不要逼它通过。

---

## 5. 已完成的工作（本会话）

> 全部为**代码层、不需要生产数据**。所有切片都有执行级证据。

### 5.1 已提交（`4b3f6db`，在 PR #45 中）
- **HITL typed continuation 真正可达**：修复 5 个阻断点；resolve/confirm → 当前授权重验 → compile → **恰好执行一次**；legacy `approve` 补边；`confirm` 成一等别名。
- **生产 resume 阻断缺陷**（前任修）：`v2.py` 的 `_TYPED_RECORDED_STATUS` 缺 `"confirm"` → 每次生产 resume 都 409。已补并加**行为式同步守卫**。
- **§4.3 人工等待不吃 deadline**：声明 state 字段 `human_wait_started_at`/`human_wait_ms`/`human_wait_last_resumed_at`；缺记录**扣 0**（更严格）；有**反事实对照**证明。
- **QUERY noncanonical AD_HOC 端到端**：服务端校验、无 authority 的公式载体；显式能力门禁；**给了公式就一定挂起一次 typed 确认**（点名"公式声明输入"与"问题解析输入"两侧，复用冻结的 `CUSTOM_METRIC_PLAN_CONFIRMATION`）；**普通查询不弹**；**多输入按公式声明放行**（普通查询额度不变，有回归守卫）。
- **定义持久化**（迁移 006）：3 张表 + 生成列 + 复合外键 → **发布不可能指向不存在的定义**（数据库层强制）；悬空引用闭合。
- **A4 两轴**：governance / authority；**原地 canonicalize 每条路径都拒**（含 `model_copy` 与数据库层）。
- **A6 语义轴驱动版本边界**：实质变化开新版本并要求业务决定；仅改标题不开；契约内参数重绑定**永不**开；**无语义的版本 checksum 逐位不变**（固定 fixture 断言）。
- **SAVED 重跑当前重验**：五分支 typed；**严格度按模式区分**（product 严格失败关闭，其余带**显式可审计 degradation**）；每次重跑可查询。
- **ANALYZE**：因果门禁、分母/采样 provenance、人工来源不可冒充自动回执、下钻预算跨轮累加。
- **P9A 评测**：EX 虚高消除（缺 oracle → UNKNOWN、不进分母、不 PASS）；**默认 CLI 走 typed、零文本解析**（tripwire 证明）；harness 显式标注。
- **A6 语义轴 HTTP 暴露**：PATCH 响应为**严格超集**（`DraftUpdateResponse`）；`UpdateDraftRequest` 加可选 `semantics`；版本视图字段改名 `declared_axes`（消除与 PATCH 响应 `semantic_axes` 的**同名歧义**）。

### 5.2 **未提交**（工作区，P7 之后的一批）
| 切片 | 内容 | 状态 |
|---|---|---|
| **P7 原地修正** | 新增只服务本场景的决策种类（动作集 `confirm/resolve/reject/cancel`），**不放宽任何既有种类**；修正载荷**只能从已授权候选里选**；越权/自由文本/authority 注入**全部失败关闭**；**拒绝理由对"不存在"与"未授权"字节一致**（不泄露存在性）；**篡改 checkpoint 无法扩大候选集** | ✅ 24 测试 |
| **定义确认审计记录** | 记录服务端身份 + 时钟 + 决策引用；**不进定义 checksum**（有断言证明逐位不变）；客户端注入 10 种身份字段全被拒 | ✅ 14 测试 |
| **探索确认** | `exploration_confirmation.py`：`replaces_definition_confirmation: Literal[False]`；**只读注入**；探索确认后定义**仍为 DRAFT** | ✅ 19 测试 |
| **执行就绪门禁** | `definition_execution_readiness.py`：缺**分母/业务时间/联接**语义 → 不可执行（稳定码） | ✅（被 `service.py` 引用，**可达**） |
| **结果 artifact 保存面** | 4 条路由；**保存结果不创建定义**（定义数/版本数/checksum 全不变）；owner 隔离 foreign==absent；BUILD 对照 | ✅ 19 测试 |
| **探索确认接线 + BUILD 门禁** | 3 条路由 + 权限映射 + 容器**只读**接入（`ReadOnlyDefinitionReader`）；**发现并修复真缺口：`POST /library/certify` 改 certification 轴却无 BUILD 门禁**；产出**门禁覆盖矩阵**（9 个生命周期入口全覆盖 + 故意不 gate 的入口及理由） | ✅ 33 测试 |
| **H19i 整链集成证据** | 真实 Docker PG + 真实 SQL、**零 mock**，证明注入 catalog 时**整链可达**；**并证明默认生产接线不可达**（`metric_contract_missing`/`metric_dependency_binding_missing`） | ✅ 2 测试 |
| **审计记录持久化** | 迁移 007 + `confirmation_control_store.py` | 🔄 **进行中**（见第 7 节） |

---

## 6. 关键文件索引

### 6.1 冻结规则与计划
- `MASTER_PR_PLAN_V4.md` —— 主计划（§0.5 状态锚、§0.7 A1–A16、§2.1 模式、§4.2/4.3 HITL、§8.16–8.20、§10.2 Gate）
- `docs-v4-product-upgrade.md` —— 产品总纲（5 关键词：三模式/可追溯/可复用/分权威/人在环）
- `docs-repo-review.md` —— 仓库评审
- `docs-v4-productization-progress-and-plan.md` —— 产品化进度

### 6.2 语义与计算核心
- `src/nl2sql/semantic/calculation_contract.py` —— CalculationSpec / ExpressionSpec / ParameterSpec / Binding
- `src/nl2sql/semantic/metric_contract.py` —— 指标契约。**注意 `:49` `zero_denominator_policy: Literal["calculation_error"]` 是单值**（zero 语义不可能变化）
- `configs/semantic/gold/metrics/*.yaml` —— **403 个 gold 指标定义**（含 `source_table`+`aggregation`，**按定义现算**）
- `configs/semantic/ai_views.yaml` —— 9 个语义视图（与旧仓 `tt-intelligent-main` **逐字节相同**）

### 6.3 编排
- `src/nl2sql/orchestration/engine.py` —— 主图（~2800 行）
- `src/nl2sql/orchestration/mode_contract.py` —— 三模式能力集（`QUERY: ("deterministic_retrieval","run_scoped_derivation")`）
- `src/nl2sql/orchestration/decision_contract.py` —— HITL 决策契约
- `src/nl2sql/orchestration/planning.py` —— PlanCompiler / PlanValidator
- `src/nl2sql/orchestration/approved_compute.py` —— `ApprovedCalculationCatalog`（**src 中零构造者**）
- `src/nl2sql/orchestration/metric_query.py` —— `GatewayMetricStepRunner`（真实取数）
- `src/nl2sql/orchestration/execution.py` —— `PlanExecutor`
- `src/nl2sql/orchestration/grounding.py` —— `ground_execution_answer`

### 6.4 artifacts（产品工件）
- `service.py` —— `CustomDefinitionService`（**全部策略在这里**）
- `custom_definition.py` —— 定义/版本/轴
- `definition_store.py` / `definition_control_store.py` —— 存储端口 + 控制库实现
- `definition_semantics.py` —— A6 语义轴 + 纯 diff
- `definition_revalidation.py` —— 五分支重验门
- `definition_confirmation_audit.py` —— 确认审计（含稳定码 `ConfirmationIdentityInjection`）
- `exploration_confirmation.py` —— 探索确认
- `definition_execution_readiness.py` —— 执行就绪门禁
- `api_definitions.py` / `api_library.py` / `api_artifacts.py` / `api_exploration_confirmations.py` / `api_conflicts.py`

### 6.5 迁移
- `docker/migrations/control/001..007_*.sql` + `docker/alembic/control/versions/*.py`
- **约定**：结尾必须是 `INSERT INTO schema_migrations (version) VALUES (...) ON CONFLICT DO NOTHING;`；
  JSONB 用 `json.dumps` + `CAST(:x AS jsonb)`；
  引擎用 `create_runtime_async_engine(..., purpose=DatabasePurpose.CONTROL_APP, application_name=..., settings=get_settings())`

### 6.6 评测
- `benchmarks/registry.py` / `assertions.py` / `metrics.py` / `runner.py` / `selection.py` / `executor_adapter.py` / `typed_receipts.py`

---

## 7. 进行中 / 未完成 / 阻塞

### 7.1 进行中
- **审计记录持久化**（迁移 007 + `confirmation_control_store.py`）：把**定义确认审计**与**探索确认**从内存改为 Control PG。
  **起因**：前任核实两个 store **都只有内存实现**，控制库无对应表 → **product 模式下"谁在何时确认了这条定义"重启即丢失**，而 §8.16 P7B 必测要求**可审计**。

### 7.2 待办（**需要 owner 授权**）
| 项 | 内容 | 前任判断 |
|---|---|---|
| **H19a** | binding 的 `unit/precision/rounding/null/zero` 在 canonical 链中**从不被读取**（H19i 已证实） | **建议修**（规范字段忠实度） |
| **H19b** | `metric_contract_sha256`/`semantic_release_checksum`/`binding_revision` **从不被读** → 重放/审计正确性 | **建议修** |
| H19c/d/e/h | 防御纵深 / 未来 plan 形状 | 建议**先复核可达性再定** |
| H19f | 刻意的 V1 兼容面 | 建议**保持** |
| H19g | 缺生产权威 `ApprovedCalculationBinding` 发布源/catalog loader | 🚫 **不是本地能做的**（§8.19） |
| H19i | canonical 整链集成证据 | ✅ **已完成** |

> §8.20 明文："不实施修复、不访问 DB、**不授权下一代码 slice**" → **H19 逐项都需要 owner 明确授权**。

### 7.3 阻塞（**需要真实生产环境/数据，本地不可做**）
- §8.19 全部 11 项。
- P4-Q real evidence / oracle / remote DB（当前 **CONTRACT_READY**，非 PASS）。
- 生产 canonical approved-compute activation（**当前 `NOT_ENABLED`**；主计划**禁止**在权威源就位前提前接线）。

### 7.4 owner 已决定"暂不做"
- 正式治理审批流程（§5.2.3）
- 记忆/经验接入（P4）
- spider 数据集（当前只有 README；BIRD 有 1534 条）
- AD_HOC 聚合公式（输入是**已算好的标量**，比率 = 两标量相除，不需要聚合原表）
- Mode 1 **保持现状**（见 §8 决策记录）

---

## 8. owner 决策记录（**逐字保留**）

| # | 决策 | owner 原话/要点 |
|---|---|---|
| 1 | P1 重验门严格度 | **方案 C**：product 模式严格失败关闭；infra-dev/demo 继续但带**显式 degradation 标记** |
| 2 | P2 多输入 AD_HOC | **方案 A 精化**："mode1 仅涉及 db 查询，可以开放多个数的查询" → 按**公式声明**的数量放行，**不**笼统提额 |
| 3 | P6 公式确认 | **做法 B**："可以问题做解析，只是**必须解析出的结果必要要经过用户确认**，以免造成后续的两者没有 align 的情况" + "同意普通查询不弹" |
| 4 | P7 原地修正 | **可以原地修正**（前任先声明风险，owner 确认）；并确认这是 **Mode 1**，**不是 Mode 3** |
| 5 | **Mode 1 边界** | **按 A 执行**：Mode 1 数据面已符合 owner 原意（**只取已定义完成的指标集**，403 个 gold 指标）；AD_HOC **只扩大"能对这些数做什么运算"，不扩大"能取哪些数"** |
| 6 | 报告偏好 | "不是需要我详细决断的产品问题不要再跟我提，你来决断隐式的技术问题"；"必要的产品问题及时回传给我" |
| 7 | 节奏 | "不要过多的处于等待 gitci 的环节"；"仍然主动并行采用 subagent" |
| 8 | 目标 | "完成所有需要我负责的算法层的部分"；"完成不需要在真实生产环境生产数据上的部分" |

---

## 9. 已知陷阱与坑（**具体的、会咬人的**）

### 9.1 CI 空白门禁
- `quality` 作业跑 `git diff --check`，排除 `tests/fixtures/v4_p1/authoritative/**` 与 `MASTER_PR_PLAN_V4.md`。
- **PR #45 当前 CI 失败就是这个**（`tests/integration/test_definition_control_store.py:999` EOF 空行）。**前任已在工作区修复，尚未提交**。
- 常见触发：**文件末尾多空行**、行尾空白、制表符、CRLF。
- **未跟踪文件不在 `git diff --check` 里**——下次推送才会被检查，所以要**主动扫全仓**。

### 9.2 Pydantic / 序列化
- **`@property` 不会被 `model_dump()` 序列化**！曾导致 run-record 的 `binding.checksum` 在 wire 上丢失（2 个测试失败）。**修法**：显式 wire 模型物化它。
- `ExecuteDefinitionResponse.model_fields` 被 **`==` 精确断言（13 字段）**；`DefinitionView`/`DefinitionVersionView` 用 **`<=`**（可加字段）。**改模型前先读 `tests/unit/test_backend_freeze_contracts.py`**。
- `extra="forbid"` + 冻结模型是**安全边界**，不要放宽。

### 9.3 LangGraph
- **未声明的 state key 会被静默丢弃**——必须加进 `V2EngineState`。
- resume payload 必须 `exclude={"schema_version"}`（引擎的 `_TYPED_DECISION_PAYLOAD_KEYS` 是 deny-by-default）。

### 9.4 安全门禁（**不要试图绕过**）
- `tests/unit/test_query_gateway.py` 有**冻结的 DB 执行接收者 allowlist**；新增 DB 执行调用**必须显式登记**。
- openapi「每条路由都显式授权」的唯一性守卫：新路由**必须**在 `src/core/auth/dependencies.py` 的 `_required_nl2sql_permission` 登记。
- owner 隔离一律 **foreign == absent**（404，不泄露存在性）。

### 9.5 环境
- `run_code` 里 `process.env` 为空 → `os.tmpdir()` 是 undefined。
- `tools.edit` **要求先用 `tools.read` 读过该文件**（fs-observation policy）。
- `uv sync --all-groups` 曾**弄坏虚拟环境**——慎用。
- 集成测试必须自己清理 PG 容器。

---

## 10. 测试基线（**当前工作区**）

| 门 | 基线 |
|---|---|
| `ruff check src tests benchmarks` | All checks passed |
| `pyright -p pyrightconfig.json` | 0 errors, 0 warnings, 0 informations |
| `pytest tests/unit -q` | **2414 passed, 2 skipped** |
| `pytest tests/acceptance -q` | **281 passed** |
| `pytest benchmarks/tests -q` | **49 passed** |
| `pytest tests/integration/...` | 需 `TTAI_RUN_POSTGRES_INTEGRATION=1` |

> ⚠️ **acceptance 基线是 281，不是 248**（多个代理纠正过前任）。基线会随在途切片增长——**先跑一遍再判断"失败"**。
> ⚠️ CI 的 `postgres-contract` 作业已纳入全部集成测试文件；新增集成测试要**同时**加进 `.github/workflows/ci.yml`。

---

## 11. 待 owner 决断的产品问题（**当前唯一一个**）

### 执行就绪门禁的**过严**风险
`definition_execution_readiness.py` 的选择是：**一个版本只要声明了任何语义，就必须同时声明分母 / 业务时间 / 联接**，否则不可执行。

**契约缺口**（该文件自己在 docstring 里写明）：契约**没有**"是否比率"或"执行完整"的标志位。

**后果**：一个**非比率**的简单指标（例如只声明了 `unit_precision`）会被**误拒执行**。

**前任的判断**：**接受现状**（"声明部分语义比不声明更糟"，且失败关闭是安全的）。**精确修法**需要一个 `is_ratio`/`execution_complete` 标志 → **属实质性 schema 变更，需要 owner 决定**。

---

## 12. 前任的错误记录（**供新窗口避免重犯**）

1. **曾用静态阅读断言"typed continuation 路径完整且可达"——完全错误**，实际有 5 个阻断点。**教训：必须驱动真实路径。**
2. **曾用两个文件的 grep 断言"全仓 exploration 0 命中"——错误**（对象在它自己的文件里）。**教训：范围过窄的断言与源码字符串断言同类。**
3. **曾断言"探索确认没做"——错误**（失败代理其实做完了，只是没发报告）。**教训：失败代理的产出必须先评估。**
4. **曾把 Mode 1 与 Mode 3 混为一谈**（以为 Mode 3 也能原地修正）。**实际 A6 明文禁止 Mode 3 原地修正。**
5. **曾差点加上一个已存在的门禁**（QUERY 不得调用模型）——读代码后发现 `engine.py:2199-2211` **早已显式存在**。**教训：先读再改。**

---

## 13. 新窗口开工检查清单

```
[ ] 读本文件全文
[ ] 读 NEW_WINDOW_PROMPT.md
[ ] 跑一次全量门禁确认基线（ruff / pyright / unit / acceptance / benchmarks）
[ ] git status 看未提交改动，确认没有正在运行的代理
[ ] 确认 PR #45 的 CI 状态（前任已修空白问题但未推送）
[ ] 检查仓库里有没有代理残留的 scratch 文件
[ ] 再决定下一步（H19a/H19b 需 owner 授权；持久化切片可能已完成）
```
