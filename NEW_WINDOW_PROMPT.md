# 新窗口第一份 Prompt（直接粘贴使用）

---

你接手一个进行中的开发任务。**第一件事：完整读 `docs-v4-handoff.md`（405 行）**——那是上一窗口留下的完整上下文，包含项目身份、冻结规则、已完成工作、阻塞项、方法论纪律、已知陷阱与测试基线。读完再动手。

## 一、项目一句话

**TT-AI：可治理的 NL2SQL agent**（Python 3.13 / FastAPI / Pydantic v2 / LangGraph / PostgreSQL / SQLGlot / uv）。
仓库 `E:\平台开发\ttai-pr07a-next`，分支 `agent/v4-p7-typed-continuation`，**HEAD = `924bae1`（工作区干净）**，PR **#45** 已开（https://github.com/Densityyang/ttai/pull/45）。
产品核心是**三模式（QUERY / ANALYZE / BUILD）+ 可追溯 + 可复用 + 分权威 + 人在环**。

## 二、我的目标（**长期有效，逐字**）

> 完成所有需要我负责的**算法层**的部分，不要过多处于等待 git/CI 的环节；
> 对齐主 PR 中的 gate 门禁，**逐步对齐完成不需要在真实生产环境生产数据上的部分**。
> 继续，完成未完成部分，**仍然并行推进**，直到完成所有算法的开发任务。

## 三、我的工作方式（**请照此配合**）

1. **技术问题你自己决断**："不是需要我详细决断的产品问题不要再跟我提，你来决断隐式的技术问题，你判断修就帮我修，你判断暂时不动就先不动。"
2. **产品问题必须及时回传**：用**平实语言 + 一个具体例子**说明，**不要用术语**，并给出选项让我决断。
3. **不要等我**：不要卡在等 CI/等 git；把 git/CI 批处理到自然检查点。
4. **主动并行用 subagent**：按**文件所有权**切片，每个 slice 的任务书必须写明"严禁修改"清单。
5. **不要过度汇报**：代理落地后直接验证、直接修、继续下一批。

## 四、你必须遵守的方法论（**上一窗口用血换来的**）

### 最高原则：**"接线存在 ≠ 路径可达"**

本项目**已出现六次**：代码、图边、常量、类型、测试全都在，但真实路径永远走不到。
最近一次：一个"探索确认"对象有 **19 个测试全绿**，但**除自身外零引用** → 产品上根本不可达，对应的必测项是**空真的**。

**因此**：
- 每条"已实现"结论**必须有执行级证据**（真的驱动真实路径并观察结果）。
- **禁止**源码字符串断言作为能力证据。
- **禁止**用 mock 冒充真实组件来宣称整链已证。
- **禁止范围过窄的断言**（上一窗口曾用两个文件的 grep 推断"全仓 0 命中"，结论是错的）。
- 代理给出**"失败"或"不可达"的结论往往最有价值**——不要逼它通过。
- 失败代理的产出**必须先评估再决定**（上一窗口有 4 个代理中途失败，但其中多个其实已完成，只是没发报告）。
- 代理任务**过大会失败**——**收窄任务书**能显著提高成功率。
- 代理可能留下 **scratch 文件**——必须清理。

### 环境（Windows，必须照做）
```powershell
$env:HOME = "C:\Users\Density"
$env:USERPROFILE = "C:\Users\Density"
$env:PYTHONUTF8 = "1"
$env:TTAI_RUN_POSTGRES_INTEGRATION = "0"   # 集成测试默认关；需要时置 1
```
- 用 `.venv\Scripts\python.exe` / `ruff.exe` / `pyright.exe`。
- 临时目录字面量：`C:\Users\Density\AppData\Local\Temp\`（`run_code` 里 `process.env` 为空，`os.tmpdir()` 是 undefined）。
- GitHub token 用 `git credential fill` 取，**绝不打印**；api.github.com **必须走代理** `http://127.0.0.1:7897`。
- 集成测试自建 PG17 容器，**必须在 finally 里 `docker rm -f -v`**。

## 五、开工第一步（**请先做这个，再谈其它**）

1. 读 `docs-v4-handoff.md` 全文。
2. 跑一次全量门禁，确认基线：
   ```
   ruff check src tests benchmarks     → 期望 All checks passed
   pyright -p pyrightconfig.json       → 期望 0 errors
   pytest tests/unit -q                → 期望 2425 passed, 2 skipped
   pytest tests/acceptance -q          → 期望 281 passed
   pytest benchmarks/tests -q          → 期望 49 passed
   ```
3. `git status` **应为干净**（HEAD = `924bae1`）；确认**没有代理在运行**；检查有没有代理残留的 scratch 文件。
4. 确认 **PR #45 在 `924bae1` 上的 CI 状态**。**已知**：`4b3f6db` 那次 `quality` 作业的 **Whitespace gate** 失败（文件末尾多空行），**已在 `924bae1` 修复并推送**——请确认新一次 CI 是否转绿。
5. 把当前真实状态告诉我（**包括与我预期不符的地方**），然后按下面的"待办"推进。

## 六、待办与阻塞（详见交接文档第 7 节）

### 进行中
**无。** 所有已授权的切片均已完成并提交（`924bae1`）。

> 上一窗口最后完成的是**审计记录持久化**（迁移 007）：两类审计记录落 Control PG，审计表 **append-only（DB 层拒绝 UPDATE/DELETE）**、**复合外键使悬空引用不可能**、actor/time 不可空；**全新容器能逐字段读回，而内存实现在同一场景下会丢**（对照证明持久化是真的）。

### 待你授权（**H19 需要逐项授权**）
主计划 §8.20 明文："本表只登记证据和处置门禁，**不实施修复、不访问 DB、不授权下一代码 slice**。"

| 项 | 内容 | 上一窗口的判断 |
|---|---|---|
| **H19a** | binding 的 `unit/precision/rounding/null/zero` 在 canonical 链中**从不被读取**（H19i 已证实） | **建议修** |
| **H19b** | `metric_contract_sha256`/`semantic_release_checksum`/`binding_revision` **从不被读** → 重放/审计正确性 | **建议修** |
| H19c/d/e/h | 防御纵深 / 未来 plan 形状 | 建议**先复核可达性再定** |
| H19f | 刻意的 V1 兼容面 | 建议**保持** |
| H19g | 缺生产权威 `ApprovedCalculationBinding` 发布源/catalog loader | 🚫 不是本地能做的 |
| H19i | canonical 整链集成证据 | ✅ **已完成** |

**→ 请先问我要 H19a/H19b 的授权**，再开工。

### 已完成（上一窗口，全部在 `924bae1` 中）
HITL typed continuation 真正可达 · §4.3 人工等待不吃 deadline · QUERY AD_HOC 端到端（含公式确认做法 B） · 定义持久化（迁移 006）· A4 两轴 · **A6 语义轴驱动版本边界** · SAVED 重跑五分支重验 · ANALYZE 证据门禁 · P9A 评测（EX 虚高消除）· **P7 原地修正（Mode 1）** · 定义确认审计 · 探索确认（+接线）· 执行就绪门禁 · 结果 artifact 保存面 · **BUILD 门禁覆盖矩阵（修复 certify 缺口）** · **H19i 整链集成证据** · **审计记录持久化（迁移 007）**

### 阻塞（**不要碰**）
- §8.19 全部 11 项（需要真实生产环境/数据）。
- P4-Q real evidence / oracle / remote DB（当前 **CONTRACT_READY**，非 PASS）。
- 生产 canonical approved-compute activation（当前 **NOT_ENABLED**；主计划**禁止**在权威源就位前提前接线）。

### 我已决定"暂不做"
- 正式治理审批流程、记忆/经验接入、spider 数据集、AD_HOC 聚合公式。

## 七、我锁定的决策（**不要推翻**）

1. **Mode 1 边界（按 A 执行）**：Mode 1 **只取已定义完成的指标集**（403 个 gold 指标）。
   AD_HOC **只扩大"能对这些数做什么运算"，不扩大"能取哪些数"**。**不要改 `mode_contract.py`。**
2. **P6 公式确认（做法 B）**：给了公式就**一定**让用户确认一次解析结果（即使一致也确认）；**普通查询不弹**。
3. **P7 原地修正**：**可以原地修正**，这是 **Mode 1** 的能力（**不是 Mode 3**——A6 明文禁止 Mode 3 原地修改已确认定义）。
   修正载荷**只能从已授权候选里选**，越权/自由文本/authority 注入**必须失败关闭**，且**拒绝理由不得泄露存在性**。
4. **P1 重验门严格度（方案 C）**：product 模式严格失败关闭；其它模式带**显式可审计 degradation**。
5. **P2 多输入**：按**公式声明**的数量放行，**不**笼统提额；普通查询额度**一字不变**。

## 八、还有一个产品问题等我们讨论（**唯一一个**）

`definition_execution_readiness.py` 的**过严**风险：一个版本**只要声明了任何语义**，就必须同时声明**分母 / 业务时间 / 联接**，否则不可执行。
契约里**没有**"是否比率"或"执行完整"的标志位 → 一个**非比率的简单指标**（例如只声明了 `unit_precision`）会被**误拒执行**。

上一窗口的判断是**接受现状**（"声明部分语义比不声明更糟"，且失败关闭是安全的），精确修法需要 `is_ratio`/`execution_complete` 标志 → **属实质性 schema 变更**。

**请先给我讲清楚这个的利弊和一个具体例子，再问我怎么定。**

---

**现在开始：先读 `docs-v4-handoff.md`，然后按第五节开工。**
