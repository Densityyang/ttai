## Q: 今天有多少维修工单？
A: 使用 `work_orders` 表按 `created_at` 过滤当天日期并计数。
```sql
SELECT COUNT(*) AS count
FROM work_orders
WHERE DATE(created_at) = CURRENT_DATE;
```

## Q: 列出所有待处理的工单
A: 查询 `status = 'pending'` 的工单，按创建时间倒序并限制返回条数。
```sql
SELECT id, title, status, created_at
FROM work_orders
WHERE status = 'pending'
ORDER BY created_at DESC
LIMIT 100;
```

## Q: 本月每天的工单数量统计
A: 先过滤到本月，再按天分组统计。
```sql
SELECT DATE(created_at) AS date, COUNT(*) AS count
FROM work_orders
WHERE created_at >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY DATE(created_at)
ORDER BY date;
```

## Q: 查询最近7天的维修工单及其负责人
A: `work_orders` 与 `users` 通过负责人 ID 联表，并按最近 7 天过滤。
```sql
SELECT wo.id, wo.title, wo.status, u.name AS assignee
FROM work_orders wo
LEFT JOIN users u ON wo.assignee_id = u.id
WHERE wo.created_at >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY wo.created_at DESC
LIMIT 100;
```

## Q: 统计每个状态的工单数量
A: 按 `status` 分组并统计数量。
```sql
SELECT status, COUNT(*) AS count
FROM work_orders
GROUP BY status
ORDER BY count DESC;
```

## Q: 什么是单障工单？
A: 在本项目里，单障工单来源于 `silver_single_faulty_order`，通常按该表做统计分析。
```sql
SELECT COUNT(*) AS single_fault_total
FROM silver_single_faulty_order;
```

## Q: 什么是保修工单？
A: 本项目口径里“保修”按“报修服务”处理，对应表是 `silver_repair_service`。
```sql
SELECT COUNT(*) AS repair_service_total
FROM silver_repair_service;
```

## Q: 单障未归档看哪个字段？
A: 看 `completion_receipt_time`，为空就是未归档（在途）。
```sql
SELECT COUNT(*) AS single_fault_in_transit
FROM silver_single_faulty_order
WHERE completion_receipt_time IS NULL;
```

## Q: 报修未归档看哪个字段？
A: 看 `completion_receipt_time`，为空就是未归档（在途）。
```sql
SELECT COUNT(*) AS repair_service_in_transit
FROM silver_repair_service
WHERE completion_receipt_time IS NULL;
```

## Q: 投诉未归档看哪个字段？
A: 投诉（报障工单）看 `archive_time`，为空就是未归档。
```sql
SELECT COUNT(*) AS complaint_in_transit
FROM silver_fault_reporting_order
WHERE archive_time IS NULL;
```

## Q: 装机未归档看哪个字段？
A: 装机看 `archive_time`，为空就是未归档。
```sql
SELECT COUNT(*) AS installation_in_transit
FROM silver_installation_work_order
WHERE archive_time IS NULL;
```

## Q: 单障归档及时率怎么计算？
A: 口径是 `is_archive_on_time = true` 且已归档，分母是已归档总量。
```sql
SELECT
  DATE(order_acceptance_time) AS stat_date,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE is_archive_on_time = TRUE AND completion_receipt_time IS NOT NULL)
    / NULLIF(COUNT(*) FILTER (WHERE completion_receipt_time IS NOT NULL), 0),
    2
  ) AS single_fault_archive_on_time_rate
FROM silver_single_faulty_order
GROUP BY DATE(order_acceptance_time)
ORDER BY stat_date DESC;
```

## Q: 单障首响及时率怎么计算？
A: 口径是 `is_first_response_on_time = true` 且已归档，分母是已归档总量。
```sql
SELECT
  DATE(order_acceptance_time) AS stat_date,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE is_first_response_on_time = TRUE AND completion_receipt_time IS NOT NULL)
    / NULLIF(COUNT(*) FILTER (WHERE completion_receipt_time IS NOT NULL), 0),
    2
  ) AS single_fault_first_response_on_time_rate
FROM silver_single_faulty_order
GROUP BY DATE(order_acceptance_time)
ORDER BY stat_date DESC;
```

## Q: 报修归档及时率怎么计算？
A: 口径是 `is_archive_on_time = true` 且已归档，分母是已归档总量。
```sql
SELECT
  DATE(report_time) AS stat_date,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE is_archive_on_time = TRUE AND completion_receipt_time IS NOT NULL)
    / NULLIF(COUNT(*) FILTER (WHERE completion_receipt_time IS NOT NULL), 0),
    2
  ) AS repair_archive_on_time_rate
FROM silver_repair_service
GROUP BY DATE(report_time)
ORDER BY stat_date DESC;
```

## Q: 投诉首响及时率怎么计算？
A: 口径是 `has_valid_bandwidth = true`、`is_first_response_on_time = true` 且已归档，分母是带宽有效且已归档。
```sql
SELECT
  DATE(first_arrival_time) AS stat_date,
  ROUND(
    100.0 * COUNT(*) FILTER (
      WHERE has_valid_bandwidth = TRUE
        AND is_first_response_on_time = TRUE
        AND archive_time IS NOT NULL
    )
    / NULLIF(COUNT(*) FILTER (
      WHERE has_valid_bandwidth = TRUE
        AND archive_time IS NOT NULL
    ), 0),
    2
  ) AS complaint_first_response_on_time_rate
FROM silver_fault_reporting_order
GROUP BY DATE(first_arrival_time)
ORDER BY stat_date DESC;
```

## Q: 投诉当日修及时率怎么计算？
A: 口径是 `has_valid_bandwidth = true`、`is_same_day_repair_on_time = true` 且已归档，分母是带宽有效且已归档。
```sql
SELECT
  DATE(first_arrival_time) AS stat_date,
  ROUND(
    100.0 * COUNT(*) FILTER (
      WHERE has_valid_bandwidth = TRUE
        AND is_same_day_repair_on_time = TRUE
        AND archive_time IS NOT NULL
    )
    / NULLIF(COUNT(*) FILTER (
      WHERE has_valid_bandwidth = TRUE
        AND archive_time IS NOT NULL
    ), 0),
    2
  ) AS complaint_same_day_repair_on_time_rate
FROM silver_fault_reporting_order
GROUP BY DATE(first_arrival_time)
ORDER BY stat_date DESC;
```

## Q: 装机当日装及时率怎么计算？
A: 口径是非短流程(`is_short_process = false`)、`is_same_day_archive_on_time = true` 且已归档，分母是非短流程且已归档。
```sql
SELECT
  DATE(acceptance_time) AS stat_date,
  ROUND(
    100.0 * COUNT(*) FILTER (
      WHERE is_short_process = FALSE
        AND is_same_day_archive_on_time = TRUE
        AND archive_time IS NOT NULL
    )
    / NULLIF(COUNT(*) FILTER (
      WHERE is_short_process = FALSE
        AND archive_time IS NOT NULL
    ), 0),
    2
  ) AS installation_same_day_archive_on_time_rate
FROM silver_installation_work_order
GROUP BY DATE(acceptance_time)
ORDER BY stat_date DESC;
```

## Q: 报修服务工单占比怎么计算？
A: 口径是 报修工单数 / (报修工单数 + 投诉工单数)。
```sql
WITH repair AS (
  SELECT DATE(report_time) AS stat_date, COUNT(*) AS repair_cnt
  FROM silver_repair_service
  GROUP BY DATE(report_time)
), complaint AS (
  SELECT DATE(first_arrival_time) AS stat_date, COUNT(*) AS complaint_cnt
  FROM silver_fault_reporting_order
  GROUP BY DATE(first_arrival_time)
)
SELECT
  COALESCE(r.stat_date, c.stat_date) AS stat_date,
  COALESCE(r.repair_cnt, 0) AS repair_cnt,
  COALESCE(c.complaint_cnt, 0) AS complaint_cnt,
  ROUND(
    100.0 * COALESCE(r.repair_cnt, 0)
    / NULLIF(COALESCE(r.repair_cnt, 0) + COALESCE(c.complaint_cnt, 0), 0),
    2
  ) AS repair_ratio_pct
FROM repair r
FULL JOIN complaint c ON r.stat_date = c.stat_date
ORDER BY stat_date DESC;
```

## Q: 直接查询 Gold 指标表里的“单障归档及时率（全局-日）”
A: 用 `gold_metric_result` 按 `metric_code`、`time_grain` 查询。
```sql
SELECT
  time_value,
  value,
  unit,
  status
FROM gold_metric_result
WHERE metric_code = 'single_fault_archive_on_time_rate_overall_day'
  AND time_grain = 'day'
ORDER BY time_value DESC
LIMIT 100;
```

## Q: 直接查询 Gold 指标表里的“报修归档及时率（区县-日）”
A: 区县维度指标可按 `area_id` 分组查看。
```sql
SELECT
  time_value,
  area_id,
  value,
  unit,
  status
FROM gold_metric_result
WHERE metric_code = 'repair_service_archive_on_time_rate_area_day'
  AND time_grain = 'day'
ORDER BY time_value DESC, area_id
LIMIT 200;
```

## Q: 查询最近30天单障、报修、投诉、装机在途量对比
A: 按各自未归档字段口径，汇总成同一结果集。
```sql
WITH d AS (
  SELECT generate_series(CURRENT_DATE - INTERVAL '29 days', CURRENT_DATE, INTERVAL '1 day')::date AS stat_date
),
sf AS (
  SELECT DATE(work_order_arrival_time) AS stat_date, COUNT(*) AS cnt
  FROM silver_single_faulty_order
  WHERE completion_receipt_time IS NULL
  GROUP BY DATE(work_order_arrival_time)
),
rs AS (
  SELECT DATE(report_time) AS stat_date, COUNT(*) AS cnt
  FROM silver_repair_service
  WHERE completion_receipt_time IS NULL
  GROUP BY DATE(report_time)
),
cp AS (
  SELECT DATE(acceptance_time) AS stat_date, COUNT(*) AS cnt
  FROM silver_fault_reporting_order
  WHERE completion_time IS NULL
  GROUP BY DATE(acceptance_time)
),
ins AS (
  SELECT DATE(acceptance_time) AS stat_date, COUNT(*) AS cnt
  FROM silver_installation_work_order
  WHERE archive_time IS NULL
  GROUP BY DATE(acceptance_time)
)
SELECT
  d.stat_date,
  COALESCE(sf.cnt, 0) AS single_fault_in_transit,
  COALESCE(rs.cnt, 0) AS repair_service_in_transit,
  COALESCE(cp.cnt, 0) AS complaint_in_transit,
  COALESCE(ins.cnt, 0) AS installation_in_transit
FROM d
LEFT JOIN sf ON d.stat_date = sf.stat_date
LEFT JOIN rs ON d.stat_date = rs.stat_date
LEFT JOIN cp ON d.stat_date = cp.stat_date
LEFT JOIN ins ON d.stat_date = ins.stat_date
ORDER BY d.stat_date;
```
