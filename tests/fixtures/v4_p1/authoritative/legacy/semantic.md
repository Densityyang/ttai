# 语义层定义 —— 运维业务域指标

本文件为 NL2SQL 系统的语义层（Semantic Layer）参考文档，供 AI Agent 检索使用。  
每个 `###` 标题块为一个指标的完整定义，包含：metric key（用于代码引用）、业务含义、源表、时间列、过滤条件与计算公式。

## 通用说明

**维度类型说明**：
- `area_id`（区县维度）：按地理区县分组，外键关联区县表
- `team_id`（班组维度）：按作业班组分组，外键关联班组表
- 无维度（全局）：不按任何维度分组，汇总全量数据

**时间粒度**：`day`（日粒度）、`month`（月粒度）

**数值类型**：
- `count`：工单数量，单位：单
- `percent`：比率/及时率，单位：%，计算公式为 `ROUND((分子 / 分母) * 100, 2)`，分母为 0 时返回 NULL

**指标命名规则**：`{业务域}_{指标语义}_{维度}_{时间粒度}`

---

## 业务域：投诉（complaint）

**源表**：`silver_fault_reporting_order`  
**核心过滤条件**：`has_valid_bandwidth = true`（仅统计带宽有效工单）  
**时间列**：
- `first_arrival_time`：首次到达/受理时间（用于总数统计）
- `acceptance_time`：受理时间（用于在途统计）
- `completion_time`：竣工时间（NULL→在途，NOT NULL→已归档）
- `archive_time`：归档时间（用于及时率分母过滤）

**时效指标标志字段**：
- `is_first_response_on_time`：首响是否及时
- `is_arrival_on_time`：上门是否及时
- `is_same_day_repair_on_time`：当日修是否及时

### 投诉有效带宽工单总数（日/月）
- **metric key（日）**：`complaint_total_valid_bandwidth_count_day`
- **metric key（月）**：`complaint_total_valid_bandwidth_count_month`
- **描述**：投诉工单总数（带宽有效），可按区县、班组或全局维度统计
- **源表**：`silver_fault_reporting_order`
- **聚合**：`COUNT(id)`
- **过滤**：`has_valid_bandwidth = true`
- **时间列**：`first_arrival_time`
- **维度变种**：
  - 区县日：`complaint_total_valid_bandwidth_count_area_day`
  - 班组日：`complaint_total_valid_bandwidth_count_team_day`
  - 全局日：`complaint_total_count_overall_day`
  - 区县月：`complaint_total_valid_bandwidth_count_area_month`
  - 班组月：`complaint_total_valid_bandwidth_count_team_month`
  - 全局月：`complaint_total_count_month_overall`

### 投诉在途工单数（日/月）
- **metric key（日）**：`complaint_in_transit_count_day`
- **metric key（月）**：`complaint_in_transit_count_month`
- **描述**：竣工时间为空且带宽有效的在途工单数
- **源表**：`silver_fault_reporting_order`
- **聚合**：`COUNT(id)`
- **过滤**：`has_valid_bandwidth = true AND completion_time IS NULL`
- **时间列**：`acceptance_time`
- **维度变种**：
  - 区县日：`complaint_in_transit_count_area_day`
  - 班组日：`complaint_in_transit_count_team_day`
  - 全局日：`complaint_in_transit_count_overall_day`

### 投诉已归档工单数（日/月）
- **metric key（日）**：`complaint_archived_count_day`
- **metric key（月）**：`complaint_archived_count_month`
- **描述**：竣工时间非空且带宽有效的已归档工单数
- **源表**：`silver_fault_reporting_order`
- **聚合**：`COUNT(id)`
- **过滤**：`has_valid_bandwidth = true AND completion_time IS NOT NULL`
- **时间列**：`completion_time`
- **维度变种**：区县/班组/全局 × 日/月（命名规律同上）

### 投诉计算分母（及时率分母）
- **metric key（区县-日）**：`complaint_calc_total_count_area_day`
- **metric key（班组-日）**：`complaint_calc_total_count_team_day`
- **metric key（全局-日）**：`complaint_calc_total_count_overall_day`
- **描述**：用于计算投诉及时率的分母，统计已归档且带宽有效的工单数
- **过滤**：`has_valid_bandwidth = true AND archive_time IS NOT NULL`
- **时间列**：`first_arrival_time`

### 投诉首响及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`complaint_first_response_on_time_count_area_day`
- **metric key（班组）**：`complaint_first_response_on_time_count_team_day`
- **metric key（全局）**：`complaint_first_response_on_time_count_overall_day`
- **描述**：首响及时且已归档的工单数
- **过滤**：`has_valid_bandwidth = true AND is_first_response_on_time = true AND archive_time IS NOT NULL`

### 投诉上门及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`complaint_arrival_on_time_count_area_day`
- **metric key（班组）**：`complaint_arrival_on_time_count_team_day`
- **metric key（全局）**：`complaint_arrival_on_time_count_overall_day`
- **描述**：上门及时且已归档的工单数
- **过滤**：`has_valid_bandwidth = true AND is_arrival_on_time = true AND archive_time IS NOT NULL`

### 投诉当日修及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`complaint_same_day_repair_on_time_count_area_day`
- **metric key（班组）**：`complaint_same_day_repair_on_time_count_team_day`
- **metric key（全局）**：`complaint_same_day_repair_on_time_count_overall_day`
- **描述**：当日修及时且已归档的工单数
- **过滤**：`has_valid_bandwidth = true AND is_same_day_repair_on_time = true AND archive_time IS NOT NULL`

### 投诉首响及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`complaint_first_response_rate_area_day`
- **metric key（班组）**：`complaint_first_response_rate_team_day`
- **metric key（全局）**：`complaint_first_response_rate_overall_day`
- **类型**：derived（派生指标），单位：%
- **公式**：`CASE WHEN {total} > 0 THEN ROUND((CAST(COALESCE({on_time}, 0) AS DECIMAL) / {total}) * 100, 2) ELSE NULL END`
- **依赖**：on_time→首响及时工单数，total→投诉计算分母

### 投诉上门及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`complaint_arrival_rate_area_day`
- **metric key（班组）**：`complaint_arrival_rate_team_day`
- **metric key（全局）**：`complaint_arrival_rate_overall_day`
- **类型**：derived，单位：%
- **公式**：同首响及时率，分子替换为上门及时工单数

### 投诉当日修及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`complaint_same_day_repair_rate_area_day`
- **metric key（班组）**：`complaint_same_day_repair_rate_team_day`
- **metric key（全局）**：`complaint_same_day_repair_rate_overall_day`
- **类型**：derived，单位：%
- **公式**：同首响及时率，分子替换为当日修及时工单数

---

## 业务域：装机（installation）

**源表**：`silver_installation_work_order`  
**核心过滤条件**：`is_short_process = false`（剔除短流程工单）  
**时间列**：
- `archive_time`：归档时间（主要时间列，用于总数/已归档统计）
- `acceptance_time`：受理时间（用于在途、当日装统计）

**时效标志字段**：
- `is_first_response_on_time`：首响是否及时
- `is_arrival_on_time`：上门是否及时
- `is_archive_on_time`：归档是否及时（标准及时率）
- `is_same_day_archive_on_time`：当日装是否及时

### 装机工单总数（日/月）
- **metric key（日）**：`installation_total_count_day`
- **metric key（月）**：`installation_total_count_month`
- **描述**：剔除短流程后的装机工单总数
- **源表**：`silver_installation_work_order`
- **过滤**：`is_short_process = false`
- **时间列**：`archive_time`
- **维度变种**：
  - 区县日：`installation_total_count_area_day`
  - 班组日：`installation_total_count_team_day`
  - 全局日：`installation_total_count_overall_day`

### 装机在途工单数（日/月）
- **metric key**：`installation_in_transit_count_day/month`
- **描述**：归档时间为空的在途工单（剔除短流程）
- **过滤**：`archive_time IS NULL`（注意：在途不过滤短流程！）
- **时间列**：`acceptance_time`

### 装机已归档工单数（日/月）
- **metric key**：`installation_archived_count_day/month`
- **描述**：归档时间非空且剔除短流程的已归档工单
- **过滤**：`is_short_process = false AND archive_time IS NOT NULL`

### 装机计算分母（各及时率分母）
- **metric key（区县-日）**：`installation_same_day_calc_total_count_area_day`（用于当日装及时率）
- **metric key（班组-日）**：`installation_same_day_calc_total_count_team_day`
- **metric key（全局-日）**：`installation_same_day_calc_total_count_overall_day`
- **注意**：装机总数（`installation_total_count_*`）也用作首响/上门/标准及时率的分母

### 装机首响及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`installation_first_response_on_time_count_area_day`
- **metric key（班组）**：`installation_first_response_on_time_count_team_day`
- **metric key（全局）**：`installation_first_response_on_time_count_overall_day`
- **过滤**：`is_short_process = false AND is_first_response_on_time = true`

### 装机上门及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`installation_arrival_on_time_count_area_day`
- **metric key（班组）**：`installation_arrival_on_time_count_team_day`
- **metric key（全局）**：`installation_arrival_on_time_count_overall_day`
- **过滤**：`is_short_process = false AND is_arrival_on_time = true`

### 装机归档及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`installation_archive_on_time_count_area_day`
- **metric key（班组）**：`installation_archive_on_time_count_team_day`
- **metric key（全局）**：`installation_archive_on_time_count_overall_day`
- **过滤**：`is_short_process = false AND is_archive_on_time = true`

### 装机当日装及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`installation_same_day_archive_on_time_count_area_day`
- **metric key（班组）**：`installation_same_day_archive_on_time_count_team_day`
- **metric key（全局）**：`installation_same_day_archive_on_time_count_overall_day`
- **过滤**：`is_short_process = false AND is_same_day_archive_on_time = true AND archive_time IS NOT NULL`
- **时间列**：`acceptance_time`

### 装机首响及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`installation_first_response_rate_area_day`
- **metric key（班组）**：`installation_first_response_rate_team_day`
- **metric key（全局）**：`installation_first_response_rate_overall_day`
- **公式**：`ROUND((首响及时工单数 / 装机工单总数) * 100, 2)`
- **分母**：`installation_total_count_{dim}_day`

### 装机上门及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`installation_arrival_rate_area_day`
- **metric key（班组）**：`installation_arrival_rate_team_day`
- **metric key（全局）**：`installation_arrival_rate_overall_day`
- **公式**：`ROUND((上门及时工单数 / 装机工单总数) * 100, 2)`

### 装机标准及时率（归档及时率，KPI，区县/班组/全局-日）
- **metric key（区县）**：`installation_standard_rate_area_day`
- **metric key（班组）**：`installation_standard_rate_team_day`
- **metric key（全局）**：`installation_standard_rate_overall_day`
- **公式**：`ROUND((归档及时工单数 / 装机工单总数) * 100, 2)`

### 装机当日装及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`installation_same_day_rate_area_day`
- **metric key（班组）**：`installation_same_day_rate_team_day`
- **metric key（全局）**：`installation_same_day_rate_overall_day`
- **公式**：`ROUND((当日装及时工单数 / 当日装计算分母) * 100, 2)`
- **注意**：分母使用 `installation_same_day_calc_total_count_{dim}_day`（按 acceptance_time 统计已归档工单）

---

## 业务域：单障（single_fault）

**源表**：`silver_single_faulty_order`  
**时间列**：
- `order_acceptance_time`：工单受理时间（主要时间列，用于总数统计）
- `work_order_arrival_time`：工单到达时间（用于在途统计）
- `completion_receipt_time`：竣工回单时间（NULL→在途，NOT NULL→已归档）

**时效标志字段**：
- `is_first_response_on_time`：首响是否及时
- `is_archive_on_time`：归档是否及时

### 单障工单总数（日/月）
- **metric key（日）**：`single_fault_total_count_day`
- **metric key（月）**：`single_fault_total_count_month`
- **描述**：单障工单日度/月度总数，无额外过滤条件
- **源表**：`silver_single_faulty_order`
- **聚合**：`COUNT(id)`
- **时间列**：`order_acceptance_time`
- **维度变种**：
  - 区县日：`single_fault_total_count_area_day`
  - 班组日：`single_fault_total_count_team_day`
  - 全局日：`single_fault_total_count_overall_day`

### 单障在途工单数（日/月）
- **metric key（日）**：`single_fault_in_transit_count_day`
- **描述**：竣工回单时间为空的在途单障工单
- **过滤**：`completion_receipt_time IS NULL`
- **时间列**：`work_order_arrival_time`

### 单障已归档工单数（日/月）
- **metric key（日）**：`single_fault_archived_count_day`
- **描述**：竣工回单时间非空的已归档单障工单
- **过滤**：`completion_receipt_time IS NOT NULL`
- **时间列**：`completion_receipt_time`

### 单障计算分母（及时率分母）
- **metric key（区县-日）**：`single_fault_calc_total_count_area_day`
- **metric key（班组-日）**：`single_fault_calc_total_count_team_day`
- **metric key（全局-日）**：`single_fault_calc_total_count_overall_day`
- **描述**：已归档的单障工单数，用于计算及时率分母
- **过滤**：`completion_receipt_time IS NOT NULL`
- **时间列**：`order_acceptance_time`

### 单障首响及时工单数（区县/班组-日）
- **metric key（区县）**：`single_fault_first_response_on_time_count_area_day`
- **metric key（班组）**：`single_fault_first_response_on_time_count_team_day`
- **过滤**：`is_first_response_on_time = true AND completion_receipt_time IS NOT NULL`

### 单障归档及时工单数（区县/班组-日）
- **metric key（区县）**：`single_fault_archive_on_time_count_area_day`
- **metric key（班组）**：`single_fault_archive_on_time_count_team_day`
- **过滤**：`is_archive_on_time = true AND completion_receipt_time IS NOT NULL`

### 单障首响及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`single_fault_first_response_rate_area_day`
- **metric key（班组）**：`single_fault_first_response_rate_team_day`
- **公式**：`ROUND((首响及时工单数 / 计算分母) * 100, 2)`
- **分母**：`single_fault_calc_total_count_{dim}_day`

### 单障归档及时率（KPI，区县/班组/全局-日）
- **metric key（区县）**：`single_fault_archive_rate_area_day`
- **metric key（班组）**：`single_fault_archive_rate_team_day`
- **公式**：`ROUND((归档及时工单数 / 计算分母) * 100, 2)`

---

## 业务域：报修服务（repair_service）

**源表**：`silver_repair_service`  
**相关源表**：`silver_fault_reporting_order`（用于报修工单占比计算的投诉分母）  
**时间列**：
- `report_time`：报修时间（主要时间列）
- `completion_receipt_time`：竣工回单时间（NULL→在途，NOT NULL→已归档）

**时效标志字段**：
- `is_archive_on_time`：归档是否及时

### 报修服务工单总数（日/月）
- **metric key（日）**：`repair_service_total_count_day`
- **metric key（月）**：`repair_service_total_count_month`
- **描述**：报修服务工单总数，无额外过滤
- **源表**：`silver_repair_service`
- **时间列**：`report_time`
- **维度变种**：
  - 区县日：`repair_service_total_count_area_day`
  - 班组日：`repair_service_total_count_team_day`
  - 全局日：`repair_service_total_count_overall_day`

### 报修服务在途工单数（日/月）
- **metric key**：`repair_service_in_transit_count_day/month`
- **过滤**：`completion_receipt_time IS NULL`
- **时间列**：`report_time`

### 报修服务已归档工单数（日/月）
- **metric key**：`repair_service_archived_count_day/month`
- **过滤**：`completion_receipt_time IS NOT NULL`
- **时间列**：`completion_receipt_time`

### 报修服务计算分母（及时率分母）
- **metric key（区县-日）**：`repair_service_calc_total_count_area_day`
- **metric key（班组-日）**：`repair_service_calc_total_count_team_day`
- **metric key（全局-日）**：`repair_service_calc_total_count_overall_day`
- **过滤**：`completion_receipt_time IS NOT NULL`（已归档工单）

### 报修服务归档及时工单数（区县/班组/全局-日）
- **metric key（区县）**：`repair_service_archive_on_time_count_area_day`
- **metric key（班组）**：`repair_service_archive_on_time_count_team_day`
- **metric key（全局）**：`repair_service_archive_on_time_count_overall_day`
- **过滤**：`is_archive_on_time = true AND completion_receipt_time IS NOT NULL`

### 报修服务归档及时率（KPI，区县/班组-日）
- **metric key（区县）**：`repair_service_archive_rate_area_day`
- **metric key（班组）**：`repair_service_archive_rate_team_day`
- **公式**：`ROUND((归档及时工单数 / 分母) * 100, 2)`
- **分母**：`repair_service_calc_total_count_{dim}_day`

### 报修服务工单占比（KPI，区县/班组-日）
- **metric key（区县）**：`repair_service_order_ratio_area_day`
- **metric key（班组）**：`repair_service_order_ratio_team_day`
- **描述**：报修服务工单数占（报修+投诉）总工单数的比例
- **公式**：`ROUND((报修已归档数 / (报修已归档数 + 投诉工单数)) * 100, 2)`
- **依赖源表**：`silver_repair_service`（报修）+ `silver_fault_reporting_order`（投诉）
- **投诉分母 metric key（区县）**：`fault_reporting_calc_total_count_area_day`
- **投诉分母 metric key（班组）**：`fault_reporting_calc_total_count_team_day`
- **投诉总数辅助 metric key（区县）**：`fault_reporting_total_count_area_day`
- **投诉总数辅助 metric key（班组）**：`fault_reporting_total_count_team_day`

---

## 业务域：机器质检（inspection）

**源表**：`silver_machine_inspection_detail`  
**时间列**：`work_order_complete_time`（工单完工时间）  
**核心过滤**：`is_valid_for_metrics = 1.0`（用于指标统计的有效数据）  
**时效维度**：同时支持区县（`area_id`）和班组（`team_id`）维度

**质检标志字段**：
- `is_scene_valid`：场景是否有效（1.0=有效）
- `is_recognized`：是否识别成功（1.0=成功）
- `is_qualified`：是否合格（1.0=合格）

### 机器质检场景有效数（日）
- **metric key**：`machine_inspection_scene_valid_count_day`
- **描述**：可用于指标统计的场景有效数量
- **源表**：`silver_machine_inspection_detail`
- **过滤**：`is_valid_for_metrics = 1.0 AND is_scene_valid = 1.0`
- **维度**：同时含 `area_id` 和 `team_id`

### 机器质检识别成功数（日）
- **metric key**：`machine_inspection_recognized_count_day`
- **描述**：识别成功且用于指标统计的工单数
- **过滤**：`is_valid_for_metrics = 1.0 AND is_recognized = 1.0`

### 机器质检合格数（日）
- **metric key**：`machine_inspection_qualified_count_day`
- **描述**：质检合格且用于指标统计的工单数
- **过滤**：`is_valid_for_metrics = 1.0 AND is_qualified = 1.0`

### 机器质检识别率（日）
- **metric key**：`machine_inspection_recognition_rate_day`
- **类型**：derived，单位：%
- **公式**：`ROUND((识别成功数 / 场景有效数) * 100, 2)`
- **依赖**：recognized→`machine_inspection_recognized_count_day`，total→`machine_inspection_scene_valid_count_day`

### 机器质检已识别合格率（日）
- **metric key**：`machine_inspection_qualification_rate_day`
- **类型**：derived，单位：%
- **公式**：`ROUND((合格数 / 识别成功数) * 100, 2)`
- **依赖**：qualified→`machine_inspection_qualified_count_day`，total→`machine_inspection_recognized_count_day`

### 机器质检综合合格率（KPI，日）
- **metric key**：`machine_inspection_qualified_rate_day`
- **类型**：derived（KPI），单位：%
- **公式**：`ROUND((识别率 * 0.3 + 已识别合格率 * 0.7), 2)`
- **依赖**：recognition→识别率，qualification→已识别合格率
- **业务说明**：综合质检指标，识别率权重 30%，合格率权重 70%
