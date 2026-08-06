# Schema v3 语义资产编写约定

`configs/semantic/semantic.md`、`qa.md` 和 `ai_views.yaml` 由
`src/nl2sql/semantic/authoring.py` 编译为一个 typed IR。业务人员填写模板即可
完成新增资产，不需要修改 Python 代码：

- `configs/semantic/templates/metric.yaml`：指标、依赖、公式、精度和 NULL/零值策略。
- `configs/semantic/templates/qa.yaml`：稳定 case ID、指标引用和只读 SQL。
- `configs/semantic/templates/view.yaml`：源关系、显式 join、列和过滤条件。

所有 active asset 都必须填写 `owner`、`sensitivity`、`freshness_sla_seconds`。
派生指标还必须声明 `calculation_template_id/version`、`formula`、`decimal_scale`、
`rounding`、`unit`、`null_strategy` 和 `zero_strategy`。维度或时间粒度不能用
“同上”“命名规律同上”等隐式简写。

## 校验

```powershell
uv run python scripts/validate_semantic_authoring.py
uv run python scripts/validate_semantic_authoring.py --strict-metadata
```

报告包含资产数量、稳定 checksum、每个资产的 active/retired/error 分类，以及
可操作的错误码。常见错误码如下：

| 错误码 | 处理方式 |
| --- | --- |
| `duplicate_metric_key` / `duplicate_metric_alias` | 为指标或别名选择唯一名称 |
| `unknown_relation` / `unknown_column` | 绑定已声明的 AI view、关系和列 |
| `formula_dependency_cycle` | 拆开循环依赖，保持派生指标 DAG |
| `illegal_join` | 使用带 `ON`/`USING` 的 inner/left/right/full join |
| `missing_owner` / `missing_sensitivity` / `missing_freshness` | 补齐治理元数据 |
| `qa_sql_parse_failed` / `qa_preflight_failed` | 修复 SQL，使其通过 SQLGlot 和 QueryGateway 只读预检 |
| `implicit_shorthand` | 把每个维度和时间粒度写成独立 asset |

前五条旧的 `work_orders/users` 通用 QA 会被编译为 `retired`，不会进入 active QA
预检。默认 release candidate 只能是 `complaint`；其他域可以校验，但不能隐式成为
默认 canary。报告中的 error 或 candidate 域 warning 都必须在候选发布前处理。
