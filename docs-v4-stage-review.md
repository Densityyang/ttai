# V4 当前阶段 Review（合并裁定）

> 生成时间：2026-10-09
> 生成者：接手窗口 agent（读 `docs-v4-handoff.md` 后）
> 基线：分支 `agent/v4-p7-typed-continuation`，**HEAD `19283c9`**，工作区干净
> 本文合并两份材料：
> ① 本窗口的一手实测 + 代码核查（每条结论标注核实方式）；
> ② 外部回传的《仓库完整审查 + 升级 / Debug 方向 专家交付》（下称"外部 review"）。
>
> **本文档只做实测、裁定与登记，不含任何代码改动。**

---

## 0. 结论速览

1. **仓库当前健康**：工作区干净，五道本地门禁与 CI（push + PR）全绿，PR #45 `mergeable_state=clean`。
2. **外部 review 的事实基线过期两个提交**（它基于 `876c563`，当时工作区确实脏）。它的 **P0 两项已全部修完并 CI 验证**，它的 **P1「HITL 闭环残件」在当前代码上不成立**，它的覆盖率数字来自**另一条分支**。它的**安全加固类建议基本成立**。
3. 排除了上述过期项后，**本地无任何已授权、可开工的代码切片**。真正剩下的只有：H19a / H19b（需 owner 逐项授权，本次已决定"都先不动"）、执行就绪门禁过严（本次已决断方案 B，待授权实施）。

---

## 1. 范围与方法

| 项 | 内容 |
|---|---|
| 覆盖范围 | 门禁全量实测；CI 配置；PR/CI 状态；H19a/H19b 的**全 `src/` 范围**可达性核查；执行就绪门禁的调用点与测试覆盖；外部 review 的逐条复核 |
| 未覆盖 | 外部 review 的联网 CVE 检索结论（离线无法核实）；`docs-repo-review.md` 里的孤立代码行数统计（未复核） |
| 验证纪律 | 用 CI 的调用方式（`.venv\Scripts\pytest.exe`，**不是** `python -m pytest`）；静态断言只用于否定性结论的辅助，能力结论一律要求执行级证据 |

### 1.1 复现命令

```powershell
$env:HOME = "C:\Users\Density"; $env:USERPROFILE = "C:\Users\Density"
$env:PYTHONUTF8 = "1"; $env:TTAI_RUN_POSTGRES_INTEGRATION = "0"
Set-Location "E:\平台开发\ttai-pr07a-next"
.\.venv\Scripts\ruff.exe check src tests benchmarks
.\.venv\Scripts\pyright.exe -p pyrightconfig.json
.\.venv\Scripts\pytest.exe tests/unit -q
.\.venv\Scripts\pytest.exe tests/acceptance -q
.\.venv\Scripts\pytest.exe benchmarks/tests -q
```

---

## 2. 基线实测（本窗口跑出来的，非引用）

| 门 | 实测结果 | 与 `docs-v4-handoff.md` §10 基线一致？ |
|---|---|---|
| `ruff check src tests benchmarks` | All checks passed | ✅ |
| `pyright -p pyrightconfig.json` | 0 errors, 0 warnings, 0 informations | ✅ |
| `pytest tests/unit -q` | **2425 passed, 2 skipped**（107s） | ✅ |
| `pytest tests/acceptance -q` | **281 passed**（31s） | ✅ |
| `pytest benchmarks/tests -q` | **49 passed**（9s） | ✅ |
| `git status` | 干净（无未跟踪文件、无代理残留 scratch 文件） | ✅ |
| PR #45 | `open`，`mergeable=true`，`mergeable_state=clean` | ✅ |
| **HEAD `19283c9` 的 CI**（push 与 pull_request 两条） | **success** | ✅ 已转绿 |

**两条不是门禁的观察**：

- `ruff format --check` 会报 **245 files would be reformatted**。CI 的 `quality` 作业**只跑 `ruff check`**，不跑 `ruff format --check`（见 `.github/workflows/ci.yml`），因此这是仓库既有风格状态，不是缺陷，**不需要处理**。
- CI 的 `quality` 作业实际跑的是：空白门禁（`git diff --check`）→ `ruff check` → `pyright` → `pytest`（全量，`testpaths=["tests"]`）→ `pytest benchmarks/tests` → `pip-audit`。本窗口逐条对齐过。

---

## 3. 对「外部 review」的逐条裁定

### 3.1 已过期 / 不成立

| 外部 review 的结论 | 核实结果 | 裁定 |
|---|---|---|
| **P0**「工作区有 007 sequence 权限补丁未提交，不提交 PR 时 CI 会红」 | 已在 `e30314a` 提交：`tests/integration/test_definition_control_store.py` 的迁移清单补入 `007_confirmation_audit.sql`，并加 `GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public` | ❌ **已失效** |
| 「未跟踪 `benchmarks/tests/__init__.py`」 | 已在 `e30314a` 跟踪（+10 行） | ❌ **已失效** |
| **P1**「HITL 闭环残件：approve 后返回消息并终止的旧路径仍存在，未完整接回 typed revalidate → compile → execute」 | `src/nl2sql/orchestration/engine.py:2151-2155`：approve **走 `_approved_continuation`**（重验当前授权 → 编译 → 恰好执行一次）；仅当"无法证明可续"时 fail-closed 并打 `ContinuationUnavailable` 降级标记——这是**设计内的失败关闭**，不是残件 | ❌ **应撤销**（该结论的来源 `docs-repo-review.md:627` 是旧文档，未与代码对齐） |

### 3.2 成立（本窗口已核实）

| 外部 review 的结论 | 核实证据 | 裁定 |
|---|---|---|
| 关键依赖已安装版本：starlette 1.3.1 / aiohttp 3.14.3 / **pandas 3.0.5** | 本窗口打印 `.venv` 内 10 个关键包版本（fastapi 0.140.0、langchain 1.3.14、langgraph 1.2.9、pydantic 2.13.4、sqlalchemy 2.0.51、uvicorn 0.51.0、httpx 0.28.1） | ✅ 版本事实成立 |
| `request.url.path` 被用于权限决策，建议改 `scope["path"]` | `src/core/auth/dependencies.py` 共 **6 处**：218 / 230 / 251 / 266 / 294 / 305 | ✅ 成立 |
| Nginx 缺 Host 校验 | `nginx/nginx.conf` **完全没有 `server_name` 指令**；且 `location /api/v2/` 用 `proxy_set_header Host $host` 把客户端 Host 原样透传给应用 | ✅ 成立 |
| （本窗口补充）应用层也没有 Host 校验 | 全 `src/` **没有** `TrustedHostMiddleware`、没有 `allowed_hosts` 配置 | ✅ 缺口成立 |
| `requires-python = ">=3.13,<3.14"` | `pyproject.toml:6` | ✅ 成立 |
| 覆盖率门禁未启用 | `pyproject.toml` 无 `addopts`、无 `--cov-fail-under` | ✅ 成立 |
| H19 backlog 未完成 | 属实，但表述可比外部 review 精确得多，见 §4 | ✅ 成立 |
| P4-Q 仍为 CONTRACT_READY（非 PASS）；生产 canonical approved-compute 仍 NOT_ENABLED | 与主计划 §3.8/§8.19、交接文档 §7.3 一致 | ✅ 成立 |

### 3.3 需要修正

| 外部 review 的结论 | 实际情况 |
|---|---|
| 「实测覆盖率 **64%**」 | `docs-v4-ci-coverage-gap.md` **自述**该数字测于分支 `agent/v4-ci-hardening` 的提交 `16cde3da`（`TOTAL 12327 4388 64%`），**不是当前 HEAD**。可作为参考基线，但**不能当作当前实测值**；要拿它当门禁依据须先重测 |
| 「`requirements.txt` 与 `pyproject.toml` 漂移（3.1.0 vs ≥3.1.1），建议 `uv export` 重新导出」 | 漂移属实，但**根目录 `requirements.txt` 在本项目内零引用**（`src/`、`tests/`、`docker/`、`.github/`、`benchmarks/`、根级文档全无引用；命中的都是 vendored 的 `tt-intelligent-main/`）。`docker/Dockerfile` 走 `uv sync --frozen`（pyproject + `uv.lock`，lock 内为 3.1.1）。→ 它是**死文件**，属文档卫生问题，**不是构建风险**，优先级应下调或直接删除 |
| 「~1.4k 行完全孤立 + ~6.0k 行仅测试可达」 | 引自旧文档 `docs-repo-review.md`，**本轮未复核**，只能标"待验证" |
| **P0**「在 CI 加一条 starlette 版本兜底断言，防止间接依赖回退」 | **不建议**。CI 与 Dockerfile 都用 `uv sync --frozen` + `uv.lock`，解析结果不可静默漂移；且 `pip-audit` 已在 CI 中运行（当前绿）。手写正则断言只增加维护面 |

### 3.4 无法核实

外部 review 的 **6 次联网检索**所得的全部 CVE 编号、修复版本与上游公告结论（其自身标注 `coverage=limited`）。本窗口**离线无法验证**，态度是"未经核实、亦未证伪"。其中**本机安装版本为已核实事实**；CVE 层面的权威扫描点是 CI 的 `uv run pip-audit`（当前绿）。

---

## 4. 本窗口的增量发现（外部 review 未覆盖）

### 4.1 H19a 的精确形态

核查方式：对全 `src/` 拉取 `ApprovedCalculationBinding` 的**属性访问点**（不是两个文件的 grep）。

- `ApprovedCalculationBinding.unit` / `.precision` / `.rounding`：**声明处之外零读取**。
- canonical 链的模板执行器 `src/nl2sql/orchestration/execution.py:168-187`（`RegistryTrustedCalculationRunner`）直接返回 `trusted_template_registry.execute(...)` 的原始结果，从不读取这三个字段。
- **对照事实**：AD_HOC / 自定义定义路径**已经**应用声明精度——`src/nl2sql/semantic/calculation_runtime.py:190` 的 `_apply_precision`，docstring 明写 "Apply the DECLARED precision (decimal places) and rounding mode once"。

→ 结论不是"新功能缺失"，而是**同一个共享 typed 计算语义核（A9）下两条路径不一致**：声明了 `precision=2, rounding=half_up` 的 canonical 指标，实际返回未量化的裸值。这比"生产未启用"是可操作的结论。

### 4.2 H19b 的精确形态（并修正主计划 §8.20 的表述）

| 字段 | 是否被 canonical 链读取 | 含义 |
|---|---|---|
| `ApprovedCalculationInput.metric_contract_sha256` | **从不被读** | canonical 链**不校验"输入指标的定义是否仍是绑定时批准的那一份"**：同一 metric_key 的定义被改过，绑定依然通过。这是重放/审计正确性的真实缺口 |
| `ApprovedCalculationBinding.binding_revision` | **从不被读**（只在一个 payload 键名清单里作为字符串出现） | 绑定修订号未进入任何回执或审计 |
| `ApprovedCalculationBinding.semantic_release_checksum` | 仅在 `src/nl2sql/orchestration/p4q_acceptance.py:706` 与 witness 比对时读一次 | canonical 链本身只用 `semantic_release_id`（UUID）：**UUID 不变而 release 内容变了，绑定不会察觉** |

**修正一处**：主计划 §8.20 的 H19b 原文是「PlanValidator policy checksum 未绑定 catalog identity/revision/checksum」——这在当前代码上**已经不成立**：`src/nl2sql/orchestration/planning.py:121-128` 自提交 `040c0e5` 起就把 `calculation_catalog_checksum` 纳入 policy payload（且明确"无 catalog 时保持 payload 逐字节不变，不发明空 catalog 权威标记"）。前任窗口把 H19b 重新表述为"三个字段从不被读"是**更准确的**表述。

> 以上均为静态可达性核查。**否定性结论的最终证据形态**应是执行级反事实对照（在 H19i 那套真实 Docker PG 整链上，声明 `precision=2` 后观察输出是否被量化），该证据在获授权实施修复时一并产出。

### 4.3 执行就绪门禁：调用点唯一，且**测试零引用**

- 唯一调用点：`src/nl2sql/artifacts/service.py:884`（`require_executable_semantics(exact)`），注释说明它在**任何受治理输入被抓取之前**抛出。
- 全仓检索稳定码 `definition_semantics_incomplete_for_execution` 与函数名 `require_executable_semantics` / `semantic_execution_gaps`：**`tests/` 与 `benchmarks/` 零命中**。
- 换言之：主计划 §10.2 中"缺分母/时间/联接语义不可执行"这条**必测**，目前**没有以该模块为对象的测试**。（执行级覆盖率见 §7 待验证项——该结论应以覆盖率数字收尾。）

---

## 5. 合并后的当前阶段待办

| 优先级 | 项 | 归属 | 说明 |
|---|---|---|---|
| — | **本地无任何已授权、可开工的代码切片** | — | 所有已授权切片均已完成并提交 |
| 待授权 | **H19a**：canonical 链读取 binding 的 `unit/precision/rounding` | 本地可做 | 本次 owner 决定：**先不动** |
| 待授权 | **H19b**：canonical 链校验输入指标定义哈希 + release checksum；`binding_revision` 入回执 | 本地可做 | 本次 owner 决定：**先不动** |
| 待授权 | **执行就绪门禁（方案 B）**：新增 `is_ratio` 标志，非比率免"分母"一项 | 本地可做 | 本次 owner **已决断方案 B**，待授权实施 |
| P2 | 鉴权 6 处 `request.url.path` → `scope["path"]`；应用层补 Host 校验 | 本地可做 | 纯加固。starlette 已是修复版，属防御纵深 |
| P2 | Nginx Host 校验 | ⚠️ **需要部署域名** | 本机 CI 用 `tt-api.invalid`；硬编码 Host 会打断 `compose-contract` 作业。**不是本地能定的** |
| P3 | 补执行就绪门禁的必测（含非比率正例与比率反例） | 本地可做 | 见 §4.3 |
| P3 | 重测当前 HEAD 的覆盖率 | 本地可做（耗时） | 现有 64% 是旧分支数字 |
| P3 | 处理死文件 `requirements.txt` | 本地可做 | 零引用，纯卫生 |
| 🚫 | §8.19 全部 11 项 / P4-Q real evidence / 生产 canonical activation | **不是本地能做的** | 需真实生产环境与数据 |

---

## 6. 本次 owner 决策记录（逐字保留）

| # | 事项 | 决定 |
|---|---|---|
| 1 | H19a / H19b 是否授权开工 | **都先不动**（只在 H19 表里保留登记） |
| 2 | 执行就绪门禁过严 | **方案 B：加 `is_ratio` 标志**——非比率免"分母"一项，业务时间/联接仍强制；用既有的"仅在声明时进 checksum"写法保证历史版本 checksum 逐位不变 |

### 6.1 方案 B 的要点（供实施时遵循）

- **触发条件不变**：`semantics is None` 的版本仍视为 legacy 身份，**无缺口、可执行**（A6 兼容）。
- **规则**：声明了任何语义的版本 → `time_semantics`、`join_semantics` **恒为必需**；`denominator` **仅在 `is_ratio is False` 时被豁免**，未声明该标志（`None`）时**维持现状的失败关闭**。
- **兼容红线**：新增字段为可选，且 `DefinitionSemantics.checksum` 必须在**未声明该字段时**产出与改动前**逐位相同**的载荷（照 `custom_definition.py:236-243` 已有的"仅在存在时加入 payload"写法）。黄金基线应**在改动前**捕获。
- **A6 归属**：`is_ratio` 属于 `numerator_denominator` 轴的语义面，改动它应被判为实质变化并开新版本。
- **产品含义**：只有**显式声明"不是比率"**才豁免分母；这是有意的——它把"忘了写分母"和"本来就没有分母"区分开，后者需要用户主动作出一句业务声明。

---

## 7. 未决风险与待验证项

1. **执行就绪门禁无测试**（§4.3）——必测项目前无对应测试；方案 B 实施时必须同时补上正例与反例。
2. **H19a/H19b 仍开放**——本次决定先不动；在权威绑定发布源（§8.19 / H19g）就位前，这两条不会成为生产可达缺陷，但**一旦提前启用 catalog 即成为真实缺口**。
3. **覆盖率 64% 未在当前 HEAD 复测**——且门禁未启用，CI 不会拦住覆盖率回退。
4. **Host 校验缺口**（Nginx + 应用层均无）——修复需要部署域名，属外部输入。
5. **外部 review 的 CVE 结论未经本窗口核实**——`coverage=limited`，不得作为安全结论引用；权威以 CI `pip-audit` 为准。
6. **`docs-repo-review.md` 的行数统计（1.4k 孤立 / 6.0k 仅测试可达）未复核**——如要据此清理代码，须先独立复核可达性（"接线存在 ≠ 路径可达"）。
7. **基线会随在途切片增长**——本文件的数字是 `19283c9` 时点的快照，新切片落地后须重测再判断"失败"。

---

## 8. 证据索引

| 结论 | 证据位置 |
|---|---|
| 三个 CI 失败已修复 | 提交 `e30314a`（`benchmarks/tests/__init__.py` +10；`tests/integration/test_definition_control_store.py` +9/-1） |
| approve 走受治理续跑 | `src/nl2sql/orchestration/engine.py:2151-2155`、`_approved_continuation` 于 `:505` |
| 鉴权使用 `request.url.path` | `src/core/auth/dependencies.py:218,230,251,266,294,305` |
| Nginx 无 Host 校验 | `nginx/nginx.conf`（无 `server_name`；`:65` `proxy_set_header Host $host`） |
| 应用层无 Host 校验 | 全 `src/` 无 `TrustedHostMiddleware` / `allowed_hosts` |
| canonical 模板执行器不读 binding 语义字段 | `src/nl2sql/orchestration/execution.py:168-187` |
| AD_HOC 路径应用声明精度 | `src/nl2sql/semantic/calculation_runtime.py:190`、`:419` |
| binding 三字段读取情况 | `src/nl2sql/orchestration/approved_compute.py:71,83,85`；`src/nl2sql/orchestration/p4q_acceptance.py:706` |
| policy payload 已含 catalog checksum | `src/nl2sql/orchestration/planning.py:121-128`（提交 `040c0e5`） |
| 就绪门禁唯一调用点 | `src/nl2sql/artifacts/service.py:884` |
| 就绪门禁无测试 | 全仓检索稳定码与函数名，`tests/`、`benchmarks/` 零命中 |
| checksum 兼容写法先例 | `src/nl2sql/artifacts/custom_definition.py:236-243` |
| CI 作业定义 | `.github/workflows/ci.yml` |
| 覆盖率数字出处与口径 | `docs-v4-ci-coverage-gap.md`（测于 `agent/v4-ci-hardening` @ `16cde3da`） |

---

## 9. 一句话总结

外部 review 的方向感是对的（安全加固、覆盖率、Host 校验），但它的**事实基线落后两个提交**，导致 P0 与"必须立即提交"类结论全部失效、一条 P1 不成立；本窗口把它的每一条都对到当前 HEAD 上，并补上了它没看到的、更精确的 H19a/H19b 可达性事实与就绪门禁无测试这一缺口。**当前阶段没有阻塞项，也没有已授权的可开工项。**
