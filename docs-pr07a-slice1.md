# PR07A Slice 1：投诉在途确定性 count

本次只实现 PR07A 的第一个可独立审查切片，**不代表完整 PR07A 完成或企业指标已上线**。

## 本次能力

- `config/metrics/complaint.yaml` 是一个可扩展目录，只登记已确认名称与公式的投诉在途量。审批人、负责人及部署来源没有可靠证据，保留 `pending_source`。日报编号不推测，`benchmark_eligible` 默认 false。
- `MetricContract` 是受限 typed count 规则；普通同类指标可增加 YAML。只支持 complaint、count、day/month 和市公司总体；支持受批准字段的 typed eq filter。率、任意公式、组织维度等未支持的规则必须拒绝。
- 目录先经 `metric_catalog_ir`，再走既有 `validate_authoring_ir`、`materialize_authoring_ir` 与 semantic release 发布流程。执行规则同时进入 asset payload 和 release document metadata，参与既有 release checksum；没有新增绕过 active release 的指标权威。
- `MetricQueryCompiler` 从可信 active-release reader 与 snapshot reader 读取权威状态，校验 release/snapshot id 与 checksum、active metric/relation、实际请求 identity 权限、部署 source binding、最小允许列、类型和 eligibility policy。QueryPlan 的 required_permissions 不能取消源和指标要求。
- 基础资格 `is_valid_for_metrics IS TRUE` 无条件执行；当前 seed 还要求 `has_valid_bandwidth IS TRUE`、`completion_time IS NULL`。市公司总体不要求 area/team 非空。
- `GatewayMetricStepRunner` 在执行前重读权威状态。SQL 只经过 QueryGateway，保持参数绑定、只读事务、EXPLAIN 成本/行数门禁、超时、结果门禁、容量限制、预算与取消传播。触及结果行数上限时保守失败，不能把截断 trend 当完整结果。
- 显式 `metric_plan_executor(compiler, gateway)` 接线复用已有 PlanExecutor。调用者仍先经 PlanValidator→PlanCompiler→execution-plan validation；runner 自身也重新验证 proposal，不能靠伪造 validation record 获得权限。

## 部署配置与当前接线边界

显式部署时必须提供请求所属的可信 identity、`ControlSemanticReleasePublisher.read_active`、已批准 snapshot store 的 `read`（或实现相同接口的可信适配器）、经批准的 RelationBinding 和 EligibilityPolicy。编译器是请求范围对象，不得在不同 identity 间复用。RelationBinding 指明 source_ref 对应的 relation asset id、物理 schema/relation、允许列、timestamp 类型、权限和最大时间跨度。只能授予聚合必需字段，不能把视图全部 PII 列作为默认允许列。

YAML pending seed 不能直接执行。必须确认 owner/approver、来源、权限、freshness SLA，并通过现有 schema/release 审批后发布 active。现有 authoring 对 active asset 的 freshness SLA 要求仍保留；SLA 配置不等于已经知道数据更新时间。没有企业批准 registry 的默认 AppContainer 仍 fail closed。本次提供可复用 factory 及经其运行的测试；没有把未经批准 fixture 接到生产请求入口，也没有补齐自然语言 proposal 或 grounded answer 渲染。

`aggregate_first` 在此切片仅可落到显式批准的 detail source 并强制有界时间；`detail_required` 也只返回 count，不能返回客户明细。完整 aggregate catalog 优先级及 freshness-aware source selection 留待后续实现。上线前须确认批准视图下层时间索引和 EXPLAIN 预算，不能把本次小型 fixture 当作企业大表性能证明。

## 时间与结果语义

既有 TimeRange 接受同日 start/end，现明确为两个**包含在内的日期**。执行转换为上海时间 `[start 00:00, end + 1 day 00:00)`；月粒度只改变分组，不暗中扩大日期范围。timestamp without time zone 绑定上海本地无时区 datetime；timestamptz 绑定 Asia/Shanghai 有时区 datetime，并在上海时区分组。未知时区、超长跨度、日期溢出均拒绝。

无符合记录的 count 返回合法 0；无 bucket 的 trend 返回空 rows 和 no_data=true，不补造零值日期。rowset SHA256 忽略列和行排序但保留重复行、NULL 及类型区别；Decimal 通过字符串规范化保留精度，datetime 区分无时区本地值和 UTC 时刻，拒绝非有限数及不支持类型。

PlanStepReceipt 保留规范化 rowset_sha256、data_as_of、freshness_status；没有可信 source watermark 时 data_as_of=null、freshness_status=unknown，执行时间不冒充数据新鲜度。只有 record 可进入 checkpoint；业务 rows、SQL、参数仍为请求内临时结果。

## 验证与企业验收的区别

测试代码已编写，coding writer 未运行 Ruff/Pyright/pytest/coverage/Docker，交给 Luna 验证。

- 单测覆盖 YAML 扩展、关键拒绝分支、日期界限、参数绑定、规范化和 checkpoint。
- 现有 Docker PostgreSQL fixture 新增 11 条生成记录，覆盖上海闰日日界、基础资格/带宽 NULL、已竣工、市公司 NULL area/team 占位。只在 UUID 隔离容器内建表并使用只读角色；无企业库访问。
- `tests/integration/test_query_gateway_postgres.py` 新增五组经真实 QueryGateway 的 count/trend/zero/no_data contracts；CI `postgres-contract` 显式运行该文件以及既有 governance contracts。设置 opt-in 后 Docker 不可用应失败；跳过不算 DoD。
- 建议 Luna 先 Ruff/Pyright，再新单测/既有 plan、authoring、materialization、candidate、gateway 回归及 coverage；整体覆盖率至少 75% 且不低于 baseline，新核心模块目标至少 90%。最后真实 Docker contracts；CI 与当前提交 SHA 对齐。小 fixture 不构成真实企业结果、负载或 Gold parity 验收。

## PR07A 后续范围

仍待完成：完整投诉 count/rate/trend/comparison/ranking、分母为零策略、组织 area/team alias、aggregate catalog 与 source/fallback 选择、Candidate Verifier 和删除旧候选魔法权重、Deep 有容量时的候选执行与分歧 HITL、同 source checkpoint 的新 Gold parity、真实企业准备与验收。其它指标没有批准来源/公式时继续 fail closed；本切片没有伪造全部 34 个指标或 owner 审批，也没有进行完整 Gold migration。
