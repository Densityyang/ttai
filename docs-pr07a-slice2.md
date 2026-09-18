# PR07A Slice 2：投诉首响及时率与组织比较、排名

本切片扩展 Slice 1 的 approved-detail-source 确定性聚合编译器，不代表完整 PR07A、企业指标上线或生产验收完成。Slice 1 投诉在途 seed、count/trend 输出及既有执行边界保留。

## 业务种子与发布

`config/metrics/complaint.yaml` 只新增 `complaint_first_response_rate`（投诉首响及时率）。所有记录先要求 `is_valid_for_metrics IS TRUE`。分母进一步要求 `has_valid_bandwidth IS TRUE` 和 `completion_time IS NOT NULL`，分子是分母中 `is_first_response_on_time IS TRUE` 的子集。NULL 标志不等于 TRUE。周期归属只使用 `acceptance_time`，不采用旧 Gold 的 first-arrival 或 archive period。

数据库通过 PostgreSQL numeric 计算 `ROUND(100 * numerator / denominator, 2)`。分母为零时 `value=null/status=no_data`；分子为零且分母正数时 `value="0.00"/status=success`。未出现任何基础资格记录的标量仍返回一个零分母结果。

日报开启、顺序 14，助手开启、百分比范围 0–100。owner、approver、部署来源及 freshness 尚未批准，seed 保持 `pending_source`、`benchmark_eligible=false`。加载 YAML 不授权执行，仍需原有 authoring IR、校验、materialization 和 active semantic release。普通 count/ratio 指标可通过 YAML 增加，不需要指标专用 Python 分支。

`MetricContract.operation` 与可选 `ratio` 对象由联合约束校验：count 禁止 ratio 定义，ratio 必须显式声明非空 denominator/numerator-only predicates、percent、0_100、两位精度和 no_data 策略。仅增加 `is_not_null` predicate；不允许原始 SQL 或公式字符串。

## 组织与意图

部署通过 `RelationBinding.organization_dimensions` 注册 `OrganizationDimensionBinding`：city_company 是无 ID 列的总体 scope；area/team 必须指定 typed identifier 和 text/integer 稳定 ID 类型。默认只有 city_company，旧部署构造方式仍可使用。物理 ID 列必须存在于批准 snapshot、允许列中，并且不属于敏感列。text/varchar 和 PostgreSQL smallint/integer/bigint 类型分别校验；不自动转换类型。

| 意图 | 分组与结果 |
| --- | --- |
| metric | 无 dimensions 或仅 city_company，返回单个标量 |
| trend | 无 dimensions 或仅 city_company，按 day/month period 升序；不补造空 bucket |
| comparison | 必须恰好一个 area 或 team，按稳定 ID 升序 |
| ranking | 必须恰好一个 area 或 team，value 降序、NULL 最后，稳定 ID 升序打破同值 |

text ID 的分组和排序使用固定 C collation；integer 按数值排序。结果只包含 `dimension_id`，不查组织名称或 PII。area/team 分组显式排除对应 NULL ID；市公司总量不要求 area/team 非空，因此占位记录仍进入总体。

QueryPlan 中组织过滤的 `field_ref` 只能是 area/team 语义键，`source` 必须为 `entity_alias`，值由上游 semantic layer 提供已解析稳定 ID。本切片不做自然语言规划、alias 查询或名称到 ID 的转换；字符串“成都”不会被替换成任意区县或班组。一个类型合法但来源中不存在的 ID 自然产生 no-data；不存在的维度、物理字段入口或未批准绑定被拒绝。

area/team 支持 eq 或 in，所有值独立绑定；in 必须为 1–100 个不重复、同类型值。text ID 长度 1–256、无 NUL；integer 不接受 bool、浮点或字符串，并检查 snapshot 所示整数类型范围。不同组织 scope 混合、多个 grouping、重复过滤字段和通过普通 YAML filter 绕过 ID binding 都被拒绝。单一 area/team 过滤可用于 scalar/trend；不能与显式 city_company 或另一组织 grouping 混用。

`QueryPlan.result_limit` 为严格整数 1–100 或 null。ranking 的 null/省略明确等于 10；其他 intent 必须保持 null。编译器只将该 typed integer 写成 LIMIT 字面量（现有 QueryGateway 要求 literal LIMIT），绝不从问题文本提取 top N。Gateway 可以收紧限制，但不得放宽排名 LIMIT。排名上限不超过 Gateway max_rows 时，恰好返回排名上限属于完整 top N；如果 Gateway 上限低于请求排名上限且结果触及该上限，则失败。其他查询触及 Gateway 上限仍保守失败，任何超过上限的返回也失败。

## 时间、结果与回执

继续使用包含首尾的业务日期，转换成 Asia/Shanghai `[start 00:00, end+1 day 00:00)`，全部绑定。timestamp/timestamptz 区分不变，day/month 只改变分组，不扩张筛选区间。

count 保留整数 `value`。ratio 返回整数 `numerator/denominator`、Decimal 或 NULL 的 `value`、`status`；仅适用时增加 period 或 dimension_id。runner 严格校验列集合、整数范围、分子不超过分母、比例精度与状态、时间 bucket、非空 typed ID、唯一性、排序及排名行数。业务比率不使用 binary float，Python 校验使用独立 Decimal context 和 ROUND_HALF_UP。

JSON 中 ratio Decimal **无损表示为两位十进制字符串**（如 `"66.67"`、`"0.00"`），不是 JSON float；NULL 保持 null。count/分子/分母保持整数。`no_data` 在空分组结果或全部 ratio 行为 no_data 时为 true；部分分组无分母时各行保留状态，整体不丢弃成功行。0 count 仍是合法数据。

SHA256 对序列化前的 typed database rowset 使用原有 canonicalization：与行/列排列无关，保留重复行、NULL 和 Decimal/text 类型差异。JSON 字符串不能直接替代 typed Decimal 重新计算同一 SHA。receipt 保留 hash，缺可信 watermark 时 `data_as_of=null/freshness_status=unknown`，执行时间不冒充数据更新时间。checkpoint 只允许 execution record，不能写入 SQL、params、组织 ID 或业务 rows。

仍保留 active release、schema snapshot/checksum、relation、实际 identity 权限、allowed-column 与执行前权威重读检查；QueryGateway 仍是唯一 executor。没有旁路执行、默认 AppContainer 接线或客户明细输出。本切片暂不记录 `unmapped_organization_rows`，未另做无治理扫描或由结果行数猜测该质量统计。

## 测试与企业验收的区别

新增单测覆盖 ratio/YAML/channel、类型与未知输入、eq/in 注入绑定、组织组合、日期/月边界、排序与 limit、Decimal/null/no_data、hash、freshness、checkpoint 和执行前重读；原 authority 拒绝矩阵扩展到 ratio。Slice 1 测试及期望保留。

只扩展既有 `tests/integration/test_query_gateway_postgres.py` 的 UUID 隔离 Docker fixture，新增 15 条 2028 年生成记录，与 Slice 1 的 2024 年记录分开。覆盖闰日上海边界、NULL eligibility/bandwidth/completion/on-time、总体占位、area/team 比较及排名同值、零分母/零分子、月份不扩窗、未知 ID 和注入值、Gateway 行上限失败。原 `postgres-contract` CI 已显式选择此文件，无须修改 CI 选择。

coding writer **没有运行任何验证或 Git 操作**。测试代码的存在不等于通过，覆盖率尚未测量。建议 Luna 执行 Ruff/Pyright、新旧 metric 单测及 plan/authoring/materialization/candidate/gateway 回归、coverage（新核心目标 ≥90%，整体维持既有门槛），再运行 opt-in PostgreSQL contracts；不能把 skip 当通过。Luna 负责 Git、PR 和五项 CI 与提交 SHA 对齐。

这些都是合成测试，不是企业库访问、真实 Gold 对账、生产 freshness 或负载证据。

## 后续范围

下一 Slice 负责 aggregate source selection、CandidateVerifier/Deep 和同 source checkpoint 的 Gold parity。Gold migration、enterprise DB 接入与审批、自然语言规划/alias、默认产品 wiring、PR07B 渲染和遗留清理均未在此实现。未批准来源/公式仍 fail closed；完整 PR07A 尚未完成。
