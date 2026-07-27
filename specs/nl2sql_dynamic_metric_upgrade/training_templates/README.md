# 训练集模板与地址分层说明

## 1. 目录分层
- Benchmark 模板目录：`specs/nl2sql_dynamic_metric_upgrade/templates/`
- Training 模板目录：`specs/nl2sql_dynamic_metric_upgrade/training_templates/`
- Benchmark 校验脚本：`scripts/benchmark/validate_enterprise_benchmark.py`
- Training 校验脚本：`scripts/training/validate_training_dataset.py`

## 2. 训练数据文件
- `sft_train_template.jsonl`：监督微调（SFT）样本模板
- `preference_pairs_template.jsonl`：偏好对（DPO/排序）样本模板
- `tool_trajectory_template.jsonl`：工具调用轨迹样本模板
- `split_manifest_template.csv`：训练/验证/测试切分清单模板

## 3. 地址隔离规则
- 不允许将训练样本写入 `templates/` 目录。
- 不允许将 benchmark 金标写入 `training_templates/` 目录。
- 所有训练数据必须通过 `split_manifest_template.csv` 记录 split。
- 训练样本中的 `source_case_id` 必须可追踪到 benchmark case 或日志来源。

## 4. 最小流程
1. 先按模板生成训练样本文件。
2. 运行训练数据校验脚本。
3. 校验通过后再进入训练流水线。
