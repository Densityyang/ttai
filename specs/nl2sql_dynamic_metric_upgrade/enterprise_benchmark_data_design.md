# 企业基准设计与完备数据建设规范

## 1. 目标
构建一套可长期迭代的企业基准，用于客观评估以下能力：
- 标准 NL2SQL 查询能力
- 非定义指标动态计算能力
- RAG 检索与证据充分性
- 单机部署下的稳定性与安全性

该基准应满足“可复算、可审计、可回放、可对照实验”的工程要求。

## 2. 基准分层规则

### L1 基础查询层
- 单表查询、基础筛选、排序、分页。
- 指标类型：count/sum/avg/min/max。
- 目标：验证模型基础可用性与字段映射准确性。

### L2 复杂 SQL 层
- 多表 join、group by/having、窗口函数、子查询。
- 指标类型：复合聚合、时间对比、分组排名。
- 目标：验证结构化推理与 SQL 生成稳定性。

### L3 动态指标层（核心）
- 用户仅提供自然语言规则，不提供预定义指标编码。
- 必须包含：取数来源、计算逻辑、数据类型约束、分析要求。
- 目标：验证“SQL 取数 + 代码计算”端到端正确性。

### L4 鲁棒与安全层
- 改写噪声、口语化、省略条件、错误别名、越权诱导。
- 目标：验证抗噪、拒答、权限与安全策略。

## 3. 样本配额与覆盖规则

### 3.1 来源配比
- 70%：真实日志脱敏样本（高业务真实性）
- 20%：合成难例（专门覆盖长尾结构）
- 10%：对抗样本（安全与鲁棒性）

### 3.2 业务域覆盖
- 每个核心业务域都需覆盖 L1-L4 四层样本。
- 每个业务域至少包含：
  - 时间分析样本
  - 维度钻取样本
  - 异常/缺失值样本
  - 权限相关样本

### 3.3 动态指标覆盖
- 每个动态指标样本必须显式标注：
  - 数据来源表与字段
  - 计算链路步骤
  - 中间变量定义
  - 最终输出口径

## 4. 数据结构规范（统一主键）

## 4.1 case 主表（benchmark_cases）
- case_id：全局唯一 ID
- source_type：real/synthetic/adversarial
- layer：L1/L2/L3/L4
- domain：业务域
- user_query：用户原始问题
- query_paraphrases：等价改写列表
- risk_level：low/medium/high
- permission_profile：权限画像 ID
- expected_mode：sql_only/sql_plus_code/reject
- tags：难度、时间粒度、函数类型等标签

### 4.2 schema 快照表（case_schema_snapshots）
- case_id
- schema_version
- tables_json
- columns_json
- semantic_terms_json

### 4.3 金标执行表（gold_execution_specs）
- case_id
- gold_sql
- gold_sql_result_hash
- gold_calc_code
- gold_calc_steps_json
- gold_final_value_json
- tolerance_rule_json

### 4.4 证据表（gold_evidence）
- case_id
- expected_evidence_refs
- forbidden_evidence_refs
- evidence_grading_rule

### 4.5 评测记录表（eval_runs）
- run_id
- case_id
- model_name
- embedding_name
- retriever_pipeline
- repair_strategy
- output_sql
- output_calc
- output_value
- metric_scores_json
- fail_reason

## 5. 标注规范

### 5.1 标注角色
- 标注员 A：产出金标 SQL/计算步骤。
- 标注员 B：独立复标。
- 仲裁员：处理冲突并冻结最终金标。

### 5.2 标注原则
- 结果优先：允许等价 SQL，不强制字面一致。
- 口径优先：动态指标必须写明业务口径与边界。
- 可复算优先：任何金标都必须可脚本复现。

### 5.3 错误分类（必填）
- retrieval_error
- planning_error
- sql_error
- calc_error
- verification_error
- policy_error

## 6. 数据集切分与防泄漏规则

### 6.1 切分维度
- 时间切分：训练集与测试集跨时间窗口。
- schema 切分：测试集中必须包含“新表/新字段组合”。
- 业务切分：留出未见业务子域做泛化验证。

### 6.2 防泄漏检查
- 语义近重检测：问句与改写相似度阈值过滤。
- SQL 近重检测：AST 级去重。
- 证据泄漏检测：测试集不得直接引用训练专属材料。

## 7. 评测指标与通过门槛

### 7.1 任务指标
- Execution Accuracy
- Test-Suite Accuracy
- Dynamic Metric Success@1 / Success@N
- Dynamic Metric Value Error（MAPE/SMAPE）

### 7.2 可靠性指标
- SQL 失败率
- 代码执行失败率
- 自动修复成功率
- P95 延迟

### 7.3 安全指标
- 越权请求拦截率
- 危险 SQL 拦截率
- 沙箱违规率

### 7.4 统计要求
- 全部实验至少报告均值、方差、置信区间。
- 对照实验必须给出显著性检验结果。

## 8. 对照实验协议

### 8.1 固定变量
- 同一 case 集
- 同一 schema 快照
- 同一资源配额
- 同一超时与重试策略

### 8.2 变化变量
- Embedding 方案
- 主模型方案
- 检索增强方案
- 修复策略方案
- 执行策略方案

### 8.3 输出模板
- baseline 结果
- 实验组结果
- 相对提升与显著性
- 失败案例 TopN
- 资源开销变化

## 9. 完备数据（Definition of Done）
当且仅当满足以下条件，判定为“完备 data”：
- 覆盖 L1-L4 全层，且每个核心业务域全覆盖。
- 动态指标样本均具备“金标 SQL + 金标计算 + 真值结果 + 容差规则”。
- 全量样本完成双标与仲裁，冲突闭环完成。
- 训练/验证/测试切分完成并通过泄漏检查。
- 可直接接入评测脚本运行，并可回放到单 case 级别。
- 数据版本、变更记录、废弃策略全部可追踪。

## 10. 版本治理
- 版本号规则：major.minor.patch
- 任何 gold 变更必须记录变更原因与影响范围。
- 每个版本必须附带：
  - 样本统计报表
  - 指标基线报表
  - 已知问题清单

## 11. 与当前工程衔接
- 可复用评测入口：`scripts/langfuse_eval_minimal.py`。
- 可复用会话回放：`/threads/{thread_id}/history` 与 `/state`。
- 需新增：企业基准数据目录、数据校验脚本、显著性检验脚本、泄漏检查脚本。
