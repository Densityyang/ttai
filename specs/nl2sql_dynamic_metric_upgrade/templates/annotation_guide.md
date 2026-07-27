# 企业基准标注说明

## 1. 标注目标
- 产出可复算、可审计、可对照实验的数据样本。
- 每个 case 必须能独立完成评测，不依赖隐含背景知识。

## 2. 标注角色
- 标注员A：首标，负责业务问题拆解与金标构建。
- 标注员B：复标，独立完成同一 case 标注。
- 仲裁员：处理冲突，冻结最终版本。

## 3. 字段级标注规则
- case_id：全局唯一，不可复用。
- source_type：仅允许 real/synthetic/adversarial。
- layer：仅允许 L1/L2/L3/L4。
- domain：必须来自业务域枚举表。
- user_query：保留原始自然语言，不做语义改写。
- query_paraphrases：至少 2 条等价表达。
- expected_mode：仅允许 sql_only/sql_plus_code/reject。
- gold_sql：必须可执行，且字段与 schema 快照一致。
- gold_calc_code：仅包含计算步骤，不包含外部 IO。
- gold_final_value：必须可比对，必须定义主键与指标列。
- tolerance_rule：浮点比较必须定义容差。
- expected_evidence_refs：至少 2 条。
- forbidden_evidence_refs：至少 1 条高风险误导证据。

## 4. 一致性校验规则
- gold_sql 涉及的表和列必须存在于 schema 快照。
- expected_mode=sql_only 时，gold_calc_code 允许为空。
- expected_mode=sql_plus_code 时，gold_calc_code 不能为空。
- expected_mode=reject 时，gold_sql 与 gold_calc_code 必须为空。
- query_paraphrases 不能与 user_query 完全相同。

## 5. 错误标签规范
- retrieval_error：召回证据不充分或错误。
- planning_error：任务分解错误。
- sql_error：SQL 生成或执行错误。
- calc_error：计算逻辑或运行错误。
- verification_error：结果校验错误。
- policy_error：权限或安全策略违规。

## 6. 抽检与通过门槛
- 每批样本抽检比例不低于 20%。
- 双标一致率低于 90% 时整批返工。
- 所有 L3 样本必须通过专家复核。

## 7. 交付格式
- CSV 模板目录：`specs/nl2sql_dynamic_metric_upgrade/templates/`
- JSON 模板：`benchmark_case_template.json`
- 校验脚本：`scripts/benchmark/validate_enterprise_benchmark.py`
