# TT-AI 企业数据库与指标执行契约

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 状态 | PR07A 准备基线，待代码实现与数据库同步验证 |
| 适用范围 | PR07A、PR07B、PR08A、PR08B、PR09A、PR09B、PR10A、PR10B |
| 业务首发范围 | 工单指标与投诉/报障域真实执行 |
| 数据库证据日期 | 2026-09-04 |
| Git 基线 | `origin/feature/nl2sql-v3-production@a5621af` |
| 数据环境 | 测试数据库；结构预期与后续生产数据库一致，数据持续累计更新 |

本文件同时记录三类内容：

1. **已验证事实**：来自只读数据库 catalog、聚合和 EXPLAIN 查询。
2. **已批准业务决策**：由业务负责人明确确认，应成为新实现的目标契约。
3. **待实施事项**：尚未进入数据库、semantic release、代码或企业评测数据集的工作。

旧 Gold 结果用于诊断现有算法和数据问题。旧行为与已批准业务决策冲突时，必须以本文件的目标契约为准，重新生成 Gold，不得以“当前 Gold 可以复现”为理由覆盖新规则。

## 2. 证据来源与优先级

### 2.1 来源

- `MASTER_PR_PLAN_V3.md`
- `数据库展示指标计算方式（持续推进版） (1).xlsx`
- `修复工单-入库.xlsx`
- `业务侧数据库说明.md`
- `一、字典里「公式计算」类指标 vs DB 实际 Gold 产出.md`
- `configs/semantic/semantic.md`
- `configs/semantic/qa.md`
- `configs/semantic/ai_views.yaml`
- 2026-09-04 对测试数据库执行的只读 catalog、聚合、权限和 EXPLAIN 检查

数据库检查在显式 `READ ONLY` 事务内完成，结束时执行 `ROLLBACK`。检查未读取或保留客户个人明细值，未修改数据库。

### 2.2 冲突处理顺序

同一指标出现冲突时，使用以下顺序：

1. 本文件中已记录的业务负责人明确决策；
2. 指标字典逐行定义；
3. 第 7 个 sheet `在途归档整理` 对“在途”的专门定义；
4. 已审批的 source-controlled semantic release；
5. 当前数据库结构与原始导入字段；
6. 旧 Gold 产出和历史说明，仅作诊断证据。

## 3. 环境定位与 freshness 边界

当前数据库是测试环境。其数据时间较旧不阻塞以下工作：

- typed contract 开发；
- MetricQueryCompiler 开发；
- QueryGateway 真实只读集成；
- 固定历史快照上的 Gold/Silver 对账；
- 初步企业案例制作和离线回放。

当前数据不能单独证明：

- 生产 freshness；
- 生产增量任务稳定性；
- 当前业务日报是否及时；
- 生产 canary 或正式发布就绪。

生产切换前必须重新验证生产数据的 watermark、刷新周期、权限、容量和回滚。测试凭据不作为当前功能开发阻塞，但任何凭据都不得进入 Git、CI 日志、模型 prompt、trace、benchmark 文件或本文档。

## 4. 指标发布与渠道矩阵

### 4.1 已批准范围

`工单类指标` 中的发布规则已经锁定：

| 工作簿范围 | 数量 | 日报 | 问询助手 | 发布状态 |
| --- | ---: | --- | --- | --- |
| 第 2–28 行，J 列编号 1–27 | 27 | 启用 | 启用并可查询 | `active` |
| 第 29–35 行，J 列为空 | 7 | 禁用 | 启用并应可查询 | `active` |
| 第 36 行，无线连接率 | 1 | 禁用 | 禁用 | `excluded_from_launch` |

当前首批七个仅问询助手启用的种子指标是：

1. 报修服务归档及时率；
2. 装机满意度表现值；
3. 装机参评率；
4. 故障满意度表现值；
5. 故障参评率；
6. 万投诉比；
7. 光接占比（FTTR 过程管控）。

这七项不是问询助手指标的封闭名单。后续可通过新增 metric YAML、校验并发布新的 semantic release 持续扩展，不要求新指标进入日报或占用 J 列编号。

这些指标“不进入日报”不等于“只能解释定义”。目标状态仍是 `queryable`。若数据源或公式尚未齐备，可暂时标记为 `pending_source`，但不得返回近似值或把指标静默移出助手范围。

无线连接率不得进入：

- active semantic release；
- active QA；
- enterprise-golden；
- benchmark holdout；
- canary；
- 本次产品上线。

### 4.2 目标 typed channel contract

每个指标资产至少登记：

```yaml
metric_key: complaint_first_response_rate
display_name: 投诉首响及时率
daily_report_enabled: true
daily_report_order: 14
assistant_enabled: true
assistant_capability: queryable
release_status: active
benchmark_eligible: false
```

`daily_report_order` 只表达日报顺序，不作为指标身份。`metric_key` 必须稳定、唯一，且不随日报编号变化。

`benchmark_eligible` 保持简单 boolean，不扩展为多层状态或 scope 模型。它与渠道状态独立：

- `false`：当前不得进入正式企业 benchmark；
- `true`：允许进入正式企业 benchmark。

指标从 `false` 切换为 `true` 的最小条件是：数据源和公式已批准、固定数据 checkpoint 可用、新 Gold 已生成且 Gold/Silver parity 通过。具体缺失原因保存在 validation report 或审查记录中，不增加到指标 schema。

## 5. 数据分层与当前数据库对象

### 5.1 分层责任

| 层级 | 责任 |
| --- | --- |
| Bronze | 保存原始导入值、导入批次和错误追踪；不作为在线指标查询首选 |
| Silver | 完成类型转换、去重、组织映射和 YAML 资格判断；作为可复算业务事实 |
| Gold | 保存版本化指标结果和计算回执；不得替代公式、来源和 snapshot 元数据 |
| `ai_views` | 向查询执行层暴露已批准关系和列；不得决定指标公式 |
| TT-AI control semantic registry | 保存 active metric、formula、policy、schema snapshot、owner 和 release 状态 |
| Enterprise dataset | 保存脱敏问题、gold facts、允许/禁止范围和冻结数据 checkpoint |

### 5.2 2026-09-04 点状观测

| 项目 | 观测结果 |
| --- | --- |
| PostgreSQL 服务端 | 18.1 |
| 数据库大小 | 约 7.7 GB |
| `public` 表 | 65 |
| `ai_views` 普通视图 | 9 |
| materialized view | 0 |
| `gold_metric_result` | 约 88 万行，183 个 metric code |
| `gold_metric_metadata` | 0 行 |
| `gold_metric_dependency` | 0 行 |
| `ai_views.v_metric_metadata` | 空 |
| 投诉明细最大业务日期 | 2026-02-05 |
| Gold 最大统计/计算日期 | 2026-02-27 |

上述行数是检查时点证据，不是固定验收值。测试数据会继续累计。

### 5.3 当前 AI 视图

- `v_area`
- `v_team`
- `v_fault_reporting_order`
- `v_installation_work_order`
- `v_repair_service`
- `v_single_fault_order`
- `v_maintenance_metric_daily`
- `v_metric_metadata`
- `v_metric_result`

当前工单视图固定过滤 `is_valid_for_metrics = true`。该行为与目标资格规则方向一致，但最终执行仍应由版本化 metric policy 明确注入，不能只依赖视图中不可见的固定 WHERE。

`agent_reader_user` 是非 superuser 登录角色，继承 `agent_reader`。检查时：

- 对 9 个 `ai_views` 具有 SELECT，无 INSERT/UPDATE/DELETE；
- 对 27 张经过选择的 `public` 表也具有 SELECT；
- 对数据库和两个业务 schema 无 CREATE；
- 未配置角色级 `default_transaction_read_only`、statement timeout 或 lock timeout；
- 数据库全局 statement timeout 和 lock timeout 均为 0。

测试阶段可继续使用现有权限。产品路径仍必须由 QueryGateway 对每次事务设置只读、statement timeout、lock timeout、行数和结果大小上限。生产切换时重新审查 public 表直读范围。

当前 AI view 由 `postgres` 拥有，未启用 RLS、security barrier 或 security invoker；这不阻塞已授权测试，但必须由 QueryGateway 和 semantic column policy 决定客户字段是否可见。

现有 semantic 已定义机器质检指标，但 `ai_views.yaml` 尚未定义对应机器质检视图。该域正式进入助手查询前，必须补 approved view 或明确只使用 approved Gold source。

## 6. `is_valid_for_metrics` 资格契约

### 6.1 已批准业务定义

数据库负责人确认：所有指标计算规则由 YAML 解析。Silver 入库时执行相应资格规则，满足基础指标统计资格的记录自动标记为 `is_valid_for_metrics = true`。

目标规则：

```text
YAML 资格规则通过
AND is_valid_for_metrics = true
AND 指标自己的时间、状态、带宽、维度等过滤
→ 该记录可参与该指标计算
```

该字段是数据层和 semantic policy 的事实，不是模型自由输入。

### 6.2 当前存在位置

| Silver 表 | 类型 | true/1.00 | 总行数 |
| --- | --- | ---: | ---: |
| `silver_fault_reporting_order` | boolean | 1,115 | 9,103 |
| `silver_installation_work_order` | boolean | 22,421 | 44,730 |
| `silver_repair_service` | boolean | 4,962 | 8,586 |
| `silver_single_faulty_order` | boolean | 1,839 | 5,082 |
| `silver_machine_inspection_detail` | numeric `1.00/0.00` | 44,097 | 45,796 |

行数是检查时点值。前四张表使用非空 boolean；机器质检使用非空 numeric。semantic 层统一解释为相同的 eligibility 概念。

### 6.3 运行时分层

```text
Silver：存储/派生资格结果
    ↓
Metric semantic asset：登记 eligibility_policy_id、字段、类型、接受值和版本
    ↓
ContextBundle：只携带已批准 policy ID/checksum
    ↓
QueryPlan：只声明 metric_key，不提交原始 eligibility SQL
    ↓
MetricQueryCompiler：按 active release 强制注入 predicate
    ↓
QueryGateway：校验只读、关系、列、复杂度、EXPLAIN、timeout 和结果限制
```

模型不得请求关闭资格过滤。缺少 eligibility policy、字段漂移或值类型不匹配时必须 fail closed。

建议的 typed 字段：

```yaml
eligibility_policy_id: work_order_metric_eligibility_v1
eligibility_field_ref: silver_fault_reporting_order.is_valid_for_metrics
eligibility_value_type: boolean
eligibility_accepted_values: [true]
eligibility_required: true
```

旧 Gold 的部分总量/在途计算未使用该字段。该行为归类为 legacy discrepancy；目标 Gold 必须按已批准 eligibility 规则重算。

## 7. 时间字段映射

### 7.1 原始导入列

`修复工单-入库.xlsx` 的关键列顺序为：

| Excel 列 | 原始业务字段 | 数据库规范字段 |
| --- | --- | --- |
| E | 受理时间 | `acceptance_time` |
| J | 生成时间 | `generation_time` |
| K | 竣工时间 | `completion_time` |
| M | 首次到达时间 | `first_arrival_time` |
| R | 首响时间 | `first_response_time` |
| S | 预约时间 | `appointment_time` |
| T | 开始施工时间 | `start_construction_time` |
| X | 工单调度时间 | `dispatch_time` / `first_dispatch_time`，按表映射 |

### 7.2 已批准映射

```text
字典“到达时间” = first_arrival_time
字典“首响时间” = first_response_time
首响耗时（分钟） = first_arrival_time - first_response_time
```

只读验证中，4,264 条同时具有两个时间和派生耗时的投诉记录全部满足上述公式；反向计算匹配 0 条。

所有时间戳在业务语义层按 `Asia/Shanghai` 解释。SQL 使用半开区间：

```sql
business_time >= :start_at
AND business_time < :end_at
```

不得用字符串截取年月日代替参数化边界。

## 8. 统计周期与在途定义

### 8.1 统计周期优先级

日、周、月、季度等统计归属一律以指标字典 **D 列“数据使用时限”** 为准。E 列只提供附加筛选条件。D/E 冲突时 D 列决定 period attribution，冲突应作为 validation issue 保留，不得静默选择 E 列。

### 8.2 Sheet7 权威在途规则

第 7 个 sheet `在途归档整理` 只定义“当日在途”和“当月在途”，不定义总量或归档。禁止由在途条件自动反推总量和归档公式。

#### 装机在途

```text
对象：开通工单
业务时间：acceptance_time
日：匹配年/月/日
月：匹配年/月
状态：archive_time IS NULL
```

#### 投诉在途

```text
对象：报障工单表
基础资格：is_valid_for_metrics = true
有效带宽：bandwidth 非空 / has_valid_bandwidth = true
业务时间：acceptance_time
日：匹配年/月/日
月：匹配年/月
状态：completion_time IS NULL
```

#### 报修服务在途

```text
对象：报修工单
基础资格：is_valid_for_metrics = true
业务时间：acceptance_time
状态：completion_receipt_time IS NULL
```

#### 单障在途

```text
对象：单障工单
基础资格：is_valid_for_metrics = true
业务时间：work_order_arrival_time
状态：completion_receipt_time IS NULL
```

装机、投诉、报修按日/月使用同一个业务时间字段，只改变时间窗口粒度。

## 9. 比率、NULL 与无数据契约

所有百分比指标统一输出 0–100：

```text
value = ROUND(100 * numerator / denominator, 2)
```

规则：

| 情况 | 输出 |
| --- | --- |
| `denominator > 0` | 计算 0–100 数值，四舍五入两位 |
| `denominator = 0` | `value = null`，`status = no_data` |
| numerator 为 0、denominator 大于 0 | `value = 0.00`，`status = success` |
| 来源数据缺失 | `value = null`，`status = source_unavailable` |
| freshness 未知 | 数值按策略决定是否可返回，同时标记 `freshness_status = unknown` |

禁止用 0 代替无数据，禁止在 Python binary float 中完成金额或业务比率的最终精度计算。

## 10. 市公司占位与组织维度契约

### 10.1 47 条已确认记录

检查时 `v_fault_reporting_order` 中有 47 条可统计记录：

- `is_valid_for_metrics = true`；
- `has_valid_bandwidth = true`；
- 均已有竣工和归档时间，不属于在途；
- 来自 4 个导入批次；
- 原始 `branch_company`、`district_county`、`assigned_team` 都为“成都”；
- `area_id`、`team_id` 均为空；
- 维表有城市级“成都市”，但没有名为“成都”的班组，也没有可唯一确定的区县/班组。

按 `first_arrival_time` 的日期分布：

| 日期 | 行数 |
| --- | ---: |
| 2026-02-02 | 1 |
| 2026-02-03 | 1 |
| 2026-02-04 | 17 |
| 2026-02-05 | 28 |

### 10.2 已批准处理

```yaml
mapping_status: city_company_placeholder
organization_scope: city_company_total_only
area_id: null
team_id: null
include_in_city_company_total: true
include_in_area_metrics: false
include_in_team_metrics: false
include_in_lower_organization_metrics: false
```

这些记录纳入市公司整体指标，但不进入区县、班组或更低组织指标。不得把“成都”自动映射成“成都市”区县维度或任意班组。

区县/班组聚合必须显式要求对应 ID 非空；市公司总体聚合不得要求 area/team 非空。结果回执应记录 `unmapped_organization_rows`。

### 10.3 其他质量观察

同一检查窗口另有：

- 7 条 `is_valid_for_metrics = false` 且 area/team 都缺失；
- 21 条 `is_valid_for_metrics = false`、area 已映射但 team 缺失。

它们不进入目标指标计算，但应保留在数据质量报告中。

## 11. Gold 现状与目标结构

### 11.1 当前问题

`gold_metric_result` 已有结果，但机器可读治理元数据缺失：

- `gold_metric_metadata = 0`；
- `gold_metric_dependency = 0`；
- `v_metric_metadata` 为空；
- 结果表没有计算批次、公式版本、semantic release、schema snapshot、source checkpoint 或 rowset hash。

投诉/报障域点状检查发现：

| 项目 | 数值 |
| --- | ---: |
| 物理结果行 | 162,918 |
| 逻辑 key | 9,744 |
| 有重复版本的 key | 8,063 |
| 重复版本中数值发生变化的 key | 4,150 |
| 单 key 最大版本数 | 95 |

当前唯一索引包含 nullable `area_id/team_id`，并采用 PostgreSQL 默认 `NULLS DISTINCT`。因此全局和空维度结果可重复写入。

### 11.2 目标 Gold 模型

Gold 应拆分为：

1. **append-only run/history**：保存每次计算的输入版本和回执；
2. **current projection**：每个逻辑 key 只返回当前批准版本。

每次计算至少记录：

```text
calculation_run_id
metric_key
formula_version
semantic_release_id
schema_snapshot_id
source_data_checkpoint
time_grain
time_value
dimension_type
area_id / team_id
numerator / denominator（适用时）
value / unit / scale
rowset_sha256
freshness_status / data_as_of
computed_at
status / safe_error_code
```

current projection 的唯一性必须使用以下一种方案：

- PostgreSQL `NULLS NOT DISTINCT`；或
- 非空规范化 `dimension_key`。

迁移步骤：

1. 按逻辑 key、`computed_at DESC, id DESC` 选择 latest-wins；
2. 将历史行迁入 history/run 表；
3. 建立 current 唯一约束；
4. 建立 `v_metric_result_current`；
5. 由 source-controlled metric catalog 生成 metadata/dependency seed；
6. 在同一个 source checkpoint 上重新生成目标 Gold。

用户负责同步和检查生成的 Gold 定义；数据库负责人负责审核并应用 DDL、索引和数据任务。

## 12. Gold/Silver 对账证据

### 12.1 对账方法

旧 Gold 必须先按逻辑 key latest-wins，随后与同一日期窗口的 Silver 聚合比较。直接 SUM 全部 Gold 物理行无效。

### 12.2 legacy 行为的诊断结果

| 项目 | 旧行为对账结果 | 说明 |
| --- | --- | --- |
| 有效带宽总量 | 11/11 天一致 | 旧 Gold 使用 `has_valid_bandwidth`，未统一应用 eligibility |
| 投诉在途 | 11/11 天一致 | 旧 Gold 使用 acceptance period + completion null，未统一应用 eligibility |
| 首响/上门/当日修及时率 | 4/4 天一致 | 使用 eligibility、有效带宽、已竣工和及时标志；旧 Gold 按 first-arrival 日期 |
| 归档量 | 只能部分对齐 | 当前 Silver 状态不能完整复放历史计算时点 |

上述结果证明旧算法可被解释，不证明其符合目标规则。

### 12.3 目标对账规则

已批准的目标变化：

- 所有 YAML 计算指标以 `is_valid_for_metrics = true` 为基础资格；
- 日、周、月、季度归属以字典 D 列为准；
- 在途以 Sheet7 为准；
- 百分比统一 0–100；
- 市公司占位记录只进入 city-company total；
- Gold 必须绑定同一 source checkpoint。

因此旧 Gold 必须重新计算。企业金标只能来自“同一 snapshot 下的已批准公式 + Silver 聚合 + 新 Gold 回执”。

## 13. 目标 metric contract

每个可执行指标的源控定义应包含：

```yaml
metric_key: complaint_first_response_rate
display_name: 投诉首响及时率
domain: complaint
owner: business-owner-id
approver: business-approver-id

daily_report_enabled: true
daily_report_order: 14
assistant_enabled: true
assistant_capability: queryable
release_status: active
benchmark_eligible: false

source_class: silver_computed
source_relation: public.silver_fault_reporting_order
approved_view: ai_views.v_fault_reporting_order
formula_version: complaint_first_response_rate.v1

eligibility_policy_id: work_order_metric_eligibility_v1
business_time_column: acceptance_time
timezone: Asia/Shanghai
supported_grains: [day, week, month]
supported_dimensions: [city_company, area, team]

numerator_predicate_id: complaint_first_response_on_time.v1
denominator_predicate_id: complaint_first_response_denominator.v1
unit: percent
value_scale: 0_100
decimal_places: 2
zero_denominator_policy: no_data
null_policy: no_data
no_data_policy: no_data

freshness_sla_seconds: null
sensitive_columns: []
allowed_permissions: [nl2sql:invoke]
```

示例中的具体 `business_time_column` 和权限在实现时必须由对应指标的 D 列及发布 policy 生成，不得把示例值复制到其他指标。

## 14. 客户明细、安全与性能边界

业务负责人允许在必要且已授权的场景披露客户数据，并以性能为重要目标。该决策不等于默认响应、CI 或评测可以无条件携带个人信息。

产品执行分为：

| 模式 | 行为 |
| --- | --- |
| aggregate/default | 只返回聚合事实，不返回客户明细 |
| restricted detail | 明确请求、明确权限、有限列、有限行、审计回执，可返回批准的客户字段 |

客户姓名、账号、电话、地址和工单标识：

- 可以在 restricted detail 响应中按权限返回；
- 不进入普通 benchmark case、公开报告或默认 trace；
- 不进入模型 prompt，除非对应模型 profile 和 data classification 明确允许；
- 必须受 QueryGateway row/size/column policy 和审计约束。

性能优先级：

1. approved Gold/aggregate；
2. indexed Silver bounded query；
3. approved detail view；
4. 无时间边界或扫描超限则拒绝。

索引必须由真实 QueryPlan 和 EXPLAIN 证据驱动。不得仅为通过测试盲目创建索引。

当前代表性证据：

- Gold 按 metric/date 查询能命中复合索引，估算 cost 约 558；
- Silver 投诉按 `first_arrival_time` 的 bounded detail 查询仍为顺序扫描，估算 cost 约 1,281；
- `silver_fault_reporting_order` 当前没有 `first_arrival_time` 时间索引。

PR07A 先保证 SQL 有界和 fail closed；DB 负责人随后根据真实查询组合评估时间列或复合/部分索引，PR08B 再冻结性能阈值。

## 15. 已检查指标的准备度

该分类只覆盖已逐行核对的相关指标，不代表全部 34 个 active 指标已经完成定义。

### 15.1 可进入 contract/编译实现

- 当日修；
- 投诉上门及时率；
- 投诉首响及时率；
- 单障首响及时率；
- 单障归档及时率；
- 报修服务归档及时率。

进入实现时仍需执行字段存在性、D 列周期和 eligibility 校验。

### 15.2 有公式但需要前置来源或映射

- H5 满意度表现值；
- 投诉验真合格率；
- 报修服务工单占比；
- 小热线重要场景上门率；
- 故障参评率。

### 15.3 缺来源、时间、字段或完整公式

- 故障交付达标率；
- 投诉处理及时率中的城市/农村映射；
- 同期万投比压降；
- 百万重复投诉比压降；
- 万客户单障回单后转投数；
- 故障满意度表现值；
- 万投诉比。

这些指标保留助手发布目标，但在前置材料齐备前使用 `pending_source` 或 `missing_business_definition`，不得输出近似业务结论。

## 16. 初步企业数据集到正式评测的 TODO

### 16.1 PR07A coding 前

| 事项 | 负责人 | 产物 |
| --- | --- | --- |
| 生成 34 项 launch/channel matrix | TT-AI | source-controlled metric catalog |
| 扩展 metric eligibility、period、ratio、organization contract | TT-AI | Pydantic/semantic schema |
| 生成 metadata/dependency seed | TT-AI | 可审查 YAML/SQL |
| 生成 Gold latest-wins/dedupe/current-view DDL | TT-AI | 可审查 migration |
| 同步检查 Gold 定义 | 用户 | 批准或修改记录 |
| 审核并应用 DB 变更 | DB 负责人 | 测试 DB migration 结果 |

### 16.2 PR07A 实现与 DoD

TT-AI 负责：

- deterministic complaint MetricQueryCompiler；
- typed time/filter/dimension/grain；
- eligibility policy 强制注入；
- aggregate-aware source selection；
- approved detail fallback；
- parameter binding；
- QueryGateway 接入；
- rowset canonicalization 和 SHA-256；
- `data_as_of`、freshness 和 execution receipt；
- count/rate/trend/comparison/ranking；
- zero、NULL、no-data、timezone 和组织维度测试。

DB 负责人负责：

- 应用 approved view、current Gold、索引和权限；
- 提供固定测试 snapshot 或 checkpoint；
- 运行/维护数据更新任务；
- 生产切换时重新验证结构、容量和账号。

PR07A DoD 需要证明：

```text
Question
→ QueryPlan
→ ExecutionPlan
→ MetricQueryCompiler
→ QueryGateway
→ typed ExecutionReceipt
```

全链路不得从自然语言拼接 SQL、字段、公式或数值。

### 16.3 PR07B

- 将执行结果转换为 AnswerFact/AnswerArtifact；
- 展示 metric、单位、时间、维度、freshness 和 citation；
- restricted detail 执行客户字段权限；
- no-data 与 unsupported capability 使用结构化响应；
- 数字必须来自 typed facts。

### 16.4 PR08A/PR08B

- 将 ratio、rate、difference、growth 等沉淀为审批模板；
- product 保持任意 CodeAct 禁用；
- 修复 benchmark 模板中的 `gold_calc_code/sql_plus_code` 旧语义；
- 使用代表性关系和行数做性能校准；
- 冻结 deadline、scan、rows、cost、join 和并发 policy。

### 16.5 PR09A 企业数据集构建

首发投诉域以约 100 条案例作为 bootstrap 准备目标；企业集合目标为 200–500 条脱敏案例。数量不是充分性的替代，最终样本量和最小有意义差异必须在运行前登记。

每条案例至少包含：

```text
case_id
domain
difficulty
question
expected_intent / expected_outcome
metric_key(s)
formula_version
time_range / grain
filters / dimensions
unit / scale
null / zero / no_data policy
allowed_relations
forbidden_scope
required_permissions
gold_facts
gold_sql_fingerprint
gold_rowset_sha256
semantic_release_id
schema_snapshot_id
source_data_checkpoint
freshness_status / data_as_of
business_owner_approval
benchmark_eligible
```

数据集必须覆盖：

- count、rate、trend、comparison、ranking；
- 日、周、月和边界日期；
- city-company total、area、team、unknown mapping；
- 47 条市公司占位规则；
- denominator zero、numerator zero、NULL、no data；
- Gold 重复版本与 divergent result；
- restricted detail 允许与拒绝；
- 模糊问题的 clarification/HITL；
- SQL 注入、DDL/DML/COPY 和越权拒绝。

普通准确率集使用脱敏/替代标识。必要的客户明细场景放入独立 restricted dataset，不进入公共 CI 输出。

### 16.6 PR09B 正式企业评测

运行前必须冻结：

- semantic release；
- schema snapshot；
- source data checkpoint；
- Gold run；
- case 集与 train/dev/holdout 划分；
- baseline；
- 模型 profile；
- 指标、统计方法、非劣 margin 和 guardrail。

Gold 和被测系统必须使用同一数据 checkpoint。禁止在看过 holdout 结果后修改公式、阈值、case 或 margin。

### 16.7 PR10

- 生产数据库连接和账号重新验收；
- 生产 freshness、水位和更新任务演练；
- migration/rollback/backup/restore test；
- restricted detail 权限演练；
- canary 自动停止条件；
- release manifest 绑定代码、镜像、semantic、schema、Gold、dataset 和 model profile。

## 17. 存储权威边界

| 内容 | 权威位置 |
| --- | --- |
| 原始业务记录、Silver 事实、Gold 结果 | 业务 PostgreSQL |
| 物理 schema、索引和 view definition | 业务 PostgreSQL + SchemaSnapshot |
| metric key、公式、资格、渠道、owner、审批 | source-controlled semantic catalog → control registry |
| 当前 Gold projection 与计算 history | 业务 PostgreSQL，绑定 semantic/snapshot/checkpoint |
| 问题、gold facts、允许/禁止范围 | enterprise dataset |
| 路由、预算、模型和发布阈值 | versioned policy/release manifest |

不得在 Excel、数据库 metadata、Markdown 和源控 YAML 中各自维护互不校验的四套公式。指标字典是业务输入，source-controlled metric catalog 是机器执行权威；两者通过版本、checksum 和审批记录关联。

## 18. 验收清单

### 18.1 文档与 semantic

- [ ] 34 个 active 助手指标均有稳定 metric key。
- [ ] 27 个日报顺序与 J 列一致。
- [ ] 7 个助手专用指标明确不进入日报。
- [ ] 无线连接率在所有 active/benchmark/canary 路径中排除。
- [ ] 每个可执行指标具有 source、formula version、period、eligibility、dimension、unit 和 no-data policy。
- [ ] 所有 incomplete 指标明确标记缺失条件和 owner。

### 18.2 数据库与 Gold

- [ ] metadata/dependency 不再为空且与 active semantic release checksum 对应。
- [ ] current Gold 不存在重复逻辑 key。
- [ ] history 保留计算批次与 source checkpoint。
- [ ] 47 条占位记录只进入 city-company total。
- [ ] area/team 结果不混入 NULL 组织记录。
- [ ] Gold/Silver parity 在同一 checkpoint 上运行。
- [ ] 索引和 EXPLAIN 满足发布前冻结的 policy。

### 18.3 执行与安全

- [ ] 模型无法关闭 eligibility 或提交 raw SQL predicate。
- [ ] 所有成功查询经过 QueryGateway 并产生 typed receipt。
- [ ] 比率按 0–100，零分母返回 no-data。
- [ ] restricted detail 只有批准角色可用。
- [ ] PII 不进入普通 benchmark、公开报告、Git、CI 或默认 trace。
- [ ] 数据库写权限不进入在线查询路径。

### 18.4 企业评测

- [ ] 每条 active case 绑定 metric/formula/semantic/schema/data checkpoint。
- [ ] Gold facts 和 rowset hash 可复放。
- [ ] dataset 完成脱敏、去重、泄漏检查和业务审批。
- [ ] train/dev/holdout 在运行前冻结。
- [ ] 无线连接率不存在于 active case。
- [ ] 样本量和统计判断不夸大结论。

## 19. 非目标

本准备文档不授权：

- 直接修改测试或生产数据库；
- 使用现有 superuser 凭据作为产品账号；
- 把旧 Gold 当作未经审批的业务权威；
- 将客户数据写入普通 benchmark 或 trace；
- 绕过 QueryGateway；
- 在 product 启用任意 CodeAct；
- 在缺少定义时输出近似指标；
- 声称当前测试数据证明生产 freshness、性能或企业准确率。

## 20. 下一步

本文件通过业务审查后，PR07A 的首个 coding slice 应先实现：

1. metric/channel/eligibility typed contract；
2. 投诉域 source-controlled metric catalog；
3. Gold metadata/dependency seed 和 current/history migration 草案；
4. 投诉指标 deterministic MetricQueryCompiler；
5. 同一 snapshot 下的 Silver/New-Gold parity runner；
6. QueryGateway 真实只读执行与 typed receipt；
7. synthetic、test-DB 和架构旁路测试。

仍缺业务公式或数据源的指标保留在 catalog 中，但不得提前标记为 `benchmark_eligible`。
