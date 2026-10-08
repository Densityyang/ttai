# V4 P5 — Remote PostgreSQL Calculation Capability Audit

- **Task id:** `v4_p5_remote_db_calculation_capability`
- **Repo / branch:** `E:\平台开发\ttai-pr07a-next` @ `agent/v4-ci-hardening` (audit was strictly read-only; the branch was never switched, nothing was staged or committed)
- **Audited server:** PostgreSQL 18.1 (Debian), database `tt`, reached read-only through a tunnelled connection
- **Date:** 2026-09-18

> **This is an evidence document, not a product decision.** It records what the remote
> PostgreSQL can physically do and what shape the business data is in, so that the question
> "should complex calculation be pushed down to PostgreSQL, run in a controlled Calculation
> Runtime, need a future forecast/ML runtime, or not be supported at all" can be answered from
> facts. Where the evidence is insufficient that is stated explicitly rather than smoothed over.

---

## 1. Execution safety and provenance

Every probe in this audit ran under the following discipline:

| Control | How it was applied |
|---|---|
| Read-only transaction | `BEGIN READ ONLY`, and the **server-asserted** `SHOW transaction_read_only` was checked to be `on` before any probe |
| Statement timeout | `SET statement_timeout = '10s'` |
| Lock timeout | `SET lock_timeout = '1s'` |
| Statement class | `SELECT` and `pg_catalog` introspection only |
| Forbidden | No DDL, no DML, no temp tables, no stored-procedure or user-defined-function execution |
| Termination | Always `ROLLBACK`; the connection was never left in a transaction |
| Output | Metadata, counts, distributions and MIN/MAX only - **no business row contents**, no names, phones, accounts or free text |
| Credentials | Never printed, echoed or embedded |

### 1.1 Provenance caveat (applies to every finding below)

The auditing connection authenticated as **`postgres`, a SUPERUSER**
(`current_user = session_user = postgres`, `rolsuper = t`). Read-only was therefore enforced by this
audit session itself, **not** by a restricted role. Two consequences follow, and they must be carried
through the whole document:

1. The capability findings are an **upper bound** on what is physically possible on this server.
   The product channel is far stricter - it runs as the non-superuser `agent_reader_user` and is
   additionally gated in-process by `query-gateway-v2` (section 8).
2. The superuser identity is **not** the product runtime identity. Section 8 resolves the product
   login separately, from configuration evidence.

Probe scripts and raw JSON were written **outside the repository** (under `C:\Users\Density\.dsh\p5\`).
No file in either repository was modified by this audit.

---

## 2. DB engine and extensions

| Item | Value |
|---|---|
| `version()` | PostgreSQL 18.1 (Debian 18.1-1.pgdg13+2) on x86_64-pc-linux-gnu, gcc (Debian 14.2.0-19) 14.2.0, 64-bit |
| `server_encoding` | UTF8 |
| `TimeZone` | Asia/Shanghai |
| `current_database` | `tt` |
| `max_connections` | 100 |
| `shared_preload_libraries` | (empty) |
| `default_transaction_read_only` | `off` (server default) |

### 2.1 Installed extensions

**Installed extensions: `plpgsql` 1.0 only.** Nothing else is installed.

### 2.2 Procedural languages (catalog status only; no procedural code was executed)

| `lanname` | trusted | ispl | installed |
|---|---|---|---|
| `c` | f | f | built-in |
| `internal` | f | f | built-in |
| `plpgsql` | **t** | t | yes (1.0) |
| `sql` | **t** | f | built-in |

**No untrusted procedural language is installed, and `plpython3u` is not even available as a control
file.** `plpgsql` (trusted) is the only procedural language present. This is a load-bearing fact for
section 10: DB-side arbitrary code execution is not merely disallowed here, it is **not reachable**.

### 2.3 Requested extensions - installed vs available

| Extension | Installed | Available (control file) |
|---|---|---|
| `pg_stat_statements` | NO | yes (1.12) |
| `tablefunc` | NO | yes (1.0) |
| `pg_trgm` | NO | yes (1.6) |
| `postgres_fdw` | NO | yes (1.2) |
| `btree_gin` / `btree_gist` / `citext` / `pgcrypto` / `uuid-ossp` / `unaccent` / `cube` / `earthdistance` / `fuzzystrmatch` / `hstore` | NO | yes |
| `vector` / `pgvector` | NO | **not available at all** |
| `timescaledb` | NO | **not available at all** |
| `plpython3u` / `plpython` | NO | **not available at all** |
| `hll`, `tdigest` | NO | not available |
| `pg_cron`, `pg_partman` | NO | not available |
| `postgis`, `madlib` | NO | not available |
| `plr` / `plperl` / `plv8` / `pltcl` | NO | not available |

The full `pg_available_extensions` list contains 46 entries, all of them stock contributions shipped
with the Debian package (`amcheck`, `bloom`, `dblink`, `file_fdw`, `intarray`, `ltree`, `pageinspect`,
`pg_buffercache`, `pg_walinspect`, `sslinfo`, `tsm_system_rows`, `xml2`, ...). **No third-party
analytics, time-series, ML or vector extension is packaged on this server at all.**

### 2.4 Extension findings

- **DB-side advanced analytics is not available out of the box:** no vector/embedding type, no
  time-series, no ML, no statistical extension. Only **core PostgreSQL 18.1 SQL** is usable.
- `pg_stat_statements` is available as a control file but is not installed **and** is absent from
  `shared_preload_libraries`, so activating it needs DDL **plus a server restart**.
- `tablefunc` (crosstab/connectby) and `pg_trgm` need `CREATE EXTENSION` - DDL, outside this audit's
  read-only scope, and an Ops/policy decision rather than a capability question.
- `postgres_fdw` is available but not installed, so **no cross-database or external-DB access path
  currently exists**.
- No untrusted procedural language exists, so there is no DB-side Python/ML/R mechanism.

---

## 3. Analytical SQL capability matrix

Method: presence was verified from `pg_proc` (`prokind`, `pg_aggregate` membership) plus tiny catalog
probes against `pg_class` / `pg_namespace` / `generate_series`. **No business-data query was run to
prove syntax.** Each probe ran inside a `SAVEPOINT` so that a failing probe could not abort the
session.

### 3.1 Aggregates

| Capability | Status |
|---|---|
| `count`, `count(DISTINCT expr)` | SUPPORTED_BUILTIN |
| `count(DISTINCT (a,b))` multi-column | SUPPORTED_BUILTIN (via row constructor; bare `count(DISTINCT a,b)` is a **syntax error**) |
| `sum`, `avg`, `min`, `max` | SUPPORTED_BUILTIN |
| `stddev` / `stddev_samp` / `stddev_pop` | SUPPORTED_BUILTIN |
| `variance` / `var_samp` / `var_pop` | SUPPORTED_BUILTIN |
| `percentile_cont`, `percentile_disc` (ordered-set) | SUPPORTED_BUILTIN |
| `mode() WITHIN GROUP` | SUPPORTED_BUILTIN |
| `corr` | SUPPORTED_BUILTIN |
| `covar_pop`, `covar_samp` | SUPPORTED_BUILTIN |
| `regr_slope` / `_intercept` / `_r2` / `_count` / `_avgx` / `_avgy` / `_sxx` / `_sxy` / `_syy` | SUPPORTED_BUILTIN (all 9) |
| `array_agg` (+`ORDER BY`), `string_agg` | SUPPORTED_BUILTIN |
| `json_agg`, `jsonb_agg`, `json_object_agg`, `jsonb_object_agg`, `jsonb` paths/operators | SUPPORTED_BUILTIN |

### 3.2 Window functions

| Capability | Status |
|---|---|
| `row_number`, `rank`, `dense_rank`, `percent_rank`, `cume_dist`, `ntile` | SUPPORTED_BUILTIN |
| `lag`, `lead`, `first_value`, `last_value`, `nth_value` | SUPPORTED_BUILTIN |
| Cumulative window (`UNBOUNDED PRECEDING .. CURRENT ROW`) | SUPPORTED_BUILTIN |
| Moving / rolling window (`ROWS n PRECEDING`) | SUPPORTED_BUILTIN |
| Named `WINDOW` clause, `RANGE` frames, `PARTITION BY` | SUPPORTED_BUILTIN |

### 3.3 Relational

| Capability | Status |
|---|---|
| Inner / cross joins, `LATERAL` joins | SUPPORTED_BUILTIN |
| CTE, recursive CTE (`WITH RECURSIVE`) | SUPPORTED_BUILTIN |
| `FILTER` clause on aggregates | SUPPORTED_BUILTIN |
| `CASE` | SUPPORTED_BUILTIN |
| `GROUPING SETS`, `ROLLUP`, `CUBE` | SUPPORTED_BUILTIN |
| `grouping(col)` | SUPPORTED_BUILTIN |
| `grouping_id(...)` | **NOT_AVAILABLE** (no `pg_proc` entry; parser-level construct absent on 18.1) - trivially replaced by `grouping()` |

### 3.4 Time

| Capability | Status |
|---|---|
| `date_trunc` | SUPPORTED_BUILTIN |
| `date_bin` | SUPPORTED_BUILTIN (hour / 15-min / day origins verified) |
| Interval arithmetic, `justify_interval`, `age` | SUPPORTED_BUILTIN |
| `extract` (year / epoch / isodow / isoyear / week) | SUPPORTED_BUILTIN |
| Timezone conversion (`AT TIME ZONE`, `timezone()`) | SUPPORTED_BUILTIN |

### 3.5 Matrix verdict

**`EXTENSION_REQUIRED` entries for the requested list: NONE.** Every requested capability is
`SUPPORTED_BUILTIN` except `grouping_id()`, which is `NOT_AVAILABLE` and replaceable.

Core PostgreSQL 18.1 alone can express the full statistical / regression / percentile / window /
time-bucketing surface needed for metric computation. **The SQL engine is not the limiting factor**
for anything requested in this audit.

## 4. Relevant relation data-shape inventory

Every relation named in the task exists as a physical table in `public`; **none is absent**. However
**six are empty (0 rows) and therefore unusable today**:

`gold_metric_metadata`, `gold_metric_dependency`, `silver_customer_service_rating`,
`silver_weak_light_onu_statistics`, `consumer_home_broadband_customer_count_snapshot`,
`organization_department`.

(`reltuples = -1` on several of these means never-populated / never ANALYZEd, not a missing table.)

### 4.1 Inventory (exact rows are `count(*)`; shape columns are catalog-derived)

| Relation | exact rows | total size | PK | FKs | time cols | numeric | text | org cols | status / category cols |
|---|---:|---:|---|---|---:|---:|---:|---|---|
| `gold_metric_result` | 151,985 | 321.7 MB | `id` | 3 | 4 | 6 | 10 | area_id, team_id, employee_id | dimension_type, value_type, status, category_code |
| `gold_metric_metadata` | **0** | 40 KB | `metric_code` | 0 | 2 | 3 | 14 | - | source_type, calculation_type, value_type |
| `gold_metric_dependency` | **0** | 32 KB | `id` | 2 | 2 | 1 | 3 | - | - |
| `gold_maintenance_metric_daily` | 148,226 | 33.1 MB | `id` | 2 | 4 | 6 | 2 | area_id, team_id | dimension_type |
| `silver_fault_reporting_order` | 9,103 | 33.3 MB | `id` | 4 | 12 | 11 | 24 | area_id, team_id, employee_id | service_request_type, work_order_type, status, return_reason_category, 6 x `is_*` |
| `silver_repair_service` | 8,586 | 61.0 MB | `id` | 4 | 5 | 7 | 21 | area_id, team_id, employee_id | status, request_type, system_resource_1/2/3_level, 8 x `is_*` |
| `silver_single_faulty_order` | 5,082 | 10.0 MB | `id` | 4 | 6 | 8 | 23 | area_id, team_id, employee_id | status, complaint_status, 3 x `is_*` |
| `silver_installation_work_order` | 44,730 | 168.1 MB | `id` | 4 | 13 | 12 | 41 | area_id, team_id, employee_id | work_order_type, work_order_status, package_type, business_category, 6 x `is_*` |
| `silver_repair_work_order` | 20,895 | 45.9 MB | `id` | 4 | 10 | 6 | 19 | area_id, team_id, employee_id | repair_type, status, customer_type |
| `silver_customer_service_rating` | **0** | 82 KB | `id` | 4 | 3 | 8 | 8 | area_id, team_id, employee_id | flag_status |
| `silver_care_work_order` | 204,502 | 345.6 MB | `id` | 4 | 6 | 6 | 26 | area_id, team_id, employee_id | order_status, care_type |
| `silver_installation_delivery` | 272,955 | 405.3 MB | `id` | 4 | 2 | 6 | 21 | area_id, team_id, employee_id | category, work_order_type, 18 x `is_*` |
| `silver_onsite_inspection_detail` | 121,957 | 267.2 MB | `id` | 4 | 5 | 7 | 24 | area_id, team_id, employee_id | report_status |
| `silver_machine_inspection_detail` | 45,796 | 81.3 MB | `id` | 4 | 4 | 14 | 18 | area_id, team_id, employee_id | work_order_type, coverage_scenario_type, 5 x `is_*` |
| `silver_post_installation_poor_quality` | 6,856 | 9.2 MB | `id` | 4 | 3 | 6 | 12 | area_id, team_id, employee_id | poor_quality_type, current_indoor_network_status |
| `silver_satisfaction_evaluation` | 3,391 | 4.2 MB | `id` | 4 | 5 | 13 | 23 | **all NULL** | survey_file_type, scene_category, match_status |
| `silver_external_metric_import` | 3,207 | 6.4 MB | `id` | 4 | 3 | 7 | 14 | area_id, team_id, employee_id | dimension_type, category_code |
| `silver_fault_delivery_external_metric` | 1,745 | 2.2 MB | `id` | 4 | 3 | **43** | 13 | area_id, team_id, employee_id | - (43 precomputed rate columns) |
| `silver_post_installation_weak_light` | 134 | 0.38 MB | `id` | 4 | 3 | 6 | 14 | area_id, team_id, employee_id | work_order_type, service_type |
| `silver_weak_light_onu_statistics` | **0** | 0.10 MB | `id` | 4 | 3 | 6 | 20 | area_id, team_id, employee_id | 2 x `is_*` |
| `silver_complaint_verification` | 22 | 0.19 MB | `id` | 4 | 2 | 10 | 2 | area_id, team_id, employee_id | - |
| `silver_poor_quality_customer` | 1,179,598 | 883.3 MB | `id` | 4 | 2 | 6 | 5 | area_id, team_id, employee_id | 4 x `is_*` |
| `vadmin_area` | 208 | 0.12 MB | `id` | 1 (self) | 3 | 4 | 2 | self-referencing hierarchy | level, is_active, is_deleted |
| `organization_team` | 318 | 0.24 MB | `id` | 1 | 3 | 2 | 5 | department_id | is_active, is_deleted |
| `organization_employee` | 2,160 | 1.31 MB | `id` | 4 | 5 | 5 | 17 | team_id, department_id, company_id | business_type, position_category, status |
| `organization_company` | 1 | 0.08 MB | `id` | 0 | 3 | 2 | 7 | - | is_active, is_deleted |
| `organization_department` | **0** | 0.05 MB | `id` | 2 | 3 | 4 | 6 | company_id | is_active, is_deleted |
| `consumer` | 2,141,757 | 574.8 MB | `id` | 1 | 4 | 4 | 12 | area_id | customer_type, current_package_type, status |
| `consumer_home_broadband_customer_count_snapshot` | **0** | 0.04 MB | `id` | 3 | 3 | 5 | 3 | area_id | dimension_type |
| `batch_error_record` | 1,586,843 | 2,174.4 MB | `id` | 0 | 2 | 3 | 6 | - | error_type |

### 4.2 Structural readings that matter for analysis

**`gold_metric_result`** (the main Gold result store)

- Unique key `(metric_code, time_grain, time_value, dimension_type, area_id, team_id, employee_id, category_code)`
  - one row per metric / grain / bucket / dimension combination.
- `time_value date NOT NULL`; numeric target `value numeric(18,6)`; `data_quality_score integer`.
- FK-linked org triple: `area_id -> vadmin_area`, `team_id -> organization_team`,
  `employee_id -> organization_employee` (all `ON DELETE SET NULL`).
- Distributions: `time_grain` day 104,332 / month 47,587 / quarter 66;
  `dimension_type` area_team_employee 60,417 / team 40,767 / area_team 40,706 / area 9,079 / all 1,016;
  `status` success 147,916 / **partial 4,013** / **no_data 56** (note `ai_views.v_metric_result` filters to `status = 'success'`);
  `value_type` percent 67,760 / count 53,562 / **percentage 30,487** / decimal 110 / score 66;
  `category_code` all 121,982 / gigabit 11,003 / standard 10,921 / ftrr 8,079.
- **231 distinct `metric_code`**; 2,215 distinct (metric, grain, date) combinations.
- `MIN/MAX time_value = 2000-01-01 .. 2026-09-01`, but only **106 distinct dates overall**.
- Null rates: `value` 3,032 (2.0%), `area_id` 41,831, `team_id` 10,351, `employee_id` 91,568.
  The null matrix confirms the org columns are **mutually exclusive by `dimension_type`**
  (area_team_employee always fills all three; area_team fills area+team; team fills team only;
  area fills area only; all fills none). This is a star-schema-style aggregate fact table.
- **Two items that affect time-series work, both now governed by product decisions** (section 11):
  a `2000-01-01` value (176 rows, 8 metrics) in month data, and **two spellings for the same
  concept** (`percent` vs `percentage`). Neither is a database defect: the `2000-01-01` value is the
  **CURRENT_STATE storage sentinel**, not year-2000 history, and `percent`/`percentage` are to be
  normalised to one canonical semantic value type by the Calculation Runtime.

**`gold_maintenance_metric_daily`** (the only true pre-aggregated daily fact)

- Unique `(metric_code, stat_date, dimension_type, area_id, team_id)`;
  `numerator integer NOT NULL`, `denominator integer NOT NULL`, `value numeric(5,2) NOT NULL`.
- `dimension_type` team 128,535 / area 19,691; **14 distinct `metric_code`**.
- Span `2025-10-26 .. 2026-02-27`, **49 distinct dates**; the area series starts 2025-10-26 but the
  team series only from 2026-01-11.

**Silver tables are event-level.** Each is one row per work-order / service / inspection, carrying a
rich set of business timestamps (acceptance, generation, arrival, first response, appointment,
start-construction, completion, archive) plus derived `*_duration_minutes` numerics, boolean
`is_*_on_time` / `is_valid_for_metrics` flags, an integer `area_id`/`team_id`/`employee_id` org triple
with FK integrity, and a `status`/`*_type` categorical. **`created_at`/`updated_at` are ETL ingestion
timestamps, not business time**, and must never be used as an analysis time axis.

- `silver_satisfaction_evaluation` is the exception: it has questionnaire score columns but its
  `area_id`/`team_id`/`employee_id` are **NULL on all 3,391 rows**, so it cannot be grouped by org.
- `silver_external_metric_import` and `silver_fault_delivery_external_metric` are already-aggregated
  imported rate/count rows keyed by `time_value` + org; the latter has 43 numeric rate columns for a
  single `time_value` bucket (2026-07-01).
- `organization_department` is empty and `organization_team.department_id` is all-NULL, so **the
  department layer is inert**. The reachable grouping tree is `vadmin_area` (self-FK area hierarchy)
  + `organization_team`, with `organization_employee` as a third dimension on the event tables.

### 4.3 Check verdict

| Question | Verdict |
|---|---|
| Usable time dimension? | **YES broadly** - Gold has date bucket + grain; every Silver event table has multiple business timestamps. Caveats: the `2000-01-01` sentinel in Gold month data, and short Silver spans. |
| Usable numeric target? | **YES** - Gold `value numeric(18,6)` (additive counts and percentages mixed; `value_type` disambiguates); Gold daily facts have numerator/denominator/value; Silver has durations and flags. |
| Usable grouping dimensions? | **YES** - area/team/employee integer IDs with FK integrity on Gold and every Silver table, plus `dimension_type`, `category_code` and status/type flags. |

Caveats: empty metadata/dependency and customer-rating tables; mixed semantics inside
`gold_metric_result.value`; duplicate `percent`/`percentage` spellings; some Gold rows are
partial/no_data; org columns are deliberately sparse and `team_id` is NULL on a significant fraction
of raw Silver rows.

---

## 5. Size and index evidence (push-down pressure)

### 5.1 Explicit classification thresholds (stated as required)

| Class | Rule |
|---|---|
| SMALL | < 10,000 rows **and** < 1 MB total |
| MEDIUM | 10,000-999,999 rows **or** 1 MB-99.99 MB |
| LARGE | 1,000,000-49,999,999 rows **or** 100 MB-9.99 GB |
| VERY_LARGE | >= 50,000,000 rows **or** >= 10 GB |

Class = max(row-class, byte-class).

### 5.2 Server totals

**Database total: 7,887 MB.** `public` schema: **74 tables / 9 views / 70 sequences / 429 indexes**,
with **0 materialized views, 0 partitioned tables, 0 row-level-security tables, 0 triggers, 0
user-defined functions**. `ai_views` schema: 9 plain views (0 bytes).

### 5.3 Top relations by total size

| # | relation | reltuples (est) | total | index size | class |
|---:|---|---:|---:|---:|---|
| 1 | `batch_error_record` | 1,580,897 | 2.1 GB | 71.5 MB | LARGE |
| 2 | `silver_poor_quality_customer` | 1,179,607 | 883 MB | 174 MB | LARGE |
| 3 | `bronze_poor_quality_customer` | 1,179,607 | 820 MB | 110 MB | LARGE |
| 4 | `consumer` | 2,141,757 (stale) | 575 MB | 185 MB | LARGE |
| 5 | `silver_installation_delivery` | 272,996 | 405 MB | 47.5 MB | LARGE |
| 6 | `bronze_installation_delivery` | 273,979 | 403 MB | 38.3 MB | LARGE |
| 7 | `bronze_care_work_order` | 229,116 | 372 MB | 29.9 MB | LARGE |
| 8 | `silver_care_work_order` | 204,522 | 346 MB | 46.4 MB | LARGE |
| 9 | `gold_metric_result` | 162,515 | 322 MB | **167 MB** | LARGE |
| 10 | `silver_onsite_inspection_detail` | 111,417 | 267 MB | 29.4 MB | LARGE |
| 11 | `bronze_onsite_inspection_detail` | 111,528 | 258 MB | 19.5 MB | LARGE |
| 12 | `bronze_installation_work_order` | 51,742 | 195 MB | 12.5 MB | LARGE |
| 13 | `silver_installation_work_order` | 44,730 | 168 MB | 13.7 MB | LARGE |
| 14 | `bronze_fault_reporting_order` | 39,144 | 115 MB | 9.1 MB | LARGE |
| 15 | `bronze_post_installation_poor_quality` | 94,566 | 94 MB | 7.5 MB | MEDIUM |
| 16 | `silver_machine_inspection_detail` | 45,796 | 81 MB | 10.1 MB | MEDIUM |
| 17 | `bronze_machine_inspection_detail` | 45,873 | 72 MB | 7.5 MB | MEDIUM |
| 18 | `silver_repair_service` | 8,586 | 61 MB | 6.9 MB | MEDIUM |
| 19 | `bronze_repair_work_order` | 26,092 | 55 MB | 3.9 MB | MEDIUM |
| 20 | `silver_repair_work_order` | 20,895 | 46 MB | 4.8 MB | MEDIUM |

**No relation is VERY_LARGE.** Note the statistics caveat: `consumer` has `reltuples = 2,141,757` but
`n_live_tup = 46,077` with `n_dead_tup = 37,952` and `last_analyze`/`last_autoanalyze` NULL, so its row
count is unreliable while its byte size (575 MB) is still LARGE.

### 5.4 Index coverage

- **All 429 indexes are btree.** No BRIN, GIN, GiST, hash, partial or expression indexes anywhere.
- `pg_statistic_ext = 0` - **no extended / multi-column statistics exist**. This matters for
  correlated org+time filters, where the planner has no multivariate information.
- **Org dimensions are indexed only as separate single columns** (`area_id`, `team_id`, `employee_id`,
  `consumer_id`, `batch_no`) on the silver/fact tables.
- **No composite (time x area/team/employee/status) index exists anywhere except on the gold metric
  table.** `gold_metric_result` carries
  `ix_metric_result_query(metric_code, time_grain, time_value, dimension_type)`,
  `ix_metric_result_dimension(dimension_type, area_id, team_id, employee_id, category_code)` and
  `uq_metric_result(...)`.
- Index bloat is visible: `gold_metric_result` has 167 MB of indexes against a 155 MB heap;
  `silver_poor_quality_customer` has 174 MB of indexes.
- **Byte inflation on bronze/silver is driven by a wide `row_data` JSON staging column**, not by row
  count - e.g. `bronze_poor_quality_customer` is 820 MB for ~1.18 M rows with only ~110 MB of indexes.

### 5.5 Observed workload

Cumulative `pg_stat_user_tables`: heavy index-scan usage on the silver/fact and gold tables
(`silver_poor_quality_customer` ~684k, `consumer` ~834k, `silver_care_work_order` ~248k,
`gold_metric_result` ~96k), while the big bronze/silver load tables show near-zero sequential scans
(`seq_scan = 2`, consistent with batch loads). Metric reads already go through the gold layer.

### 5.6 Push-down pressure (evidence framing only)

The relations that must be aggregated in SQL rather than pulled into an application worker are the
LARGE set: silver/bronze fact tables at roughly 0.1-1.2 M rows each (scan volume ~250 MB-2.1 GB) plus
`consumer`. Sub-MEDIUM tables (post-installation poor quality, machine inspection, repair and
work-order tables at <= ~95k rows) are small enough to materialize if that were ever needed. Gold
metric outputs are already pre-computed and exposed through `ai_views`, so the current architecture
already pushes metric materialization to the backend.

---

## 6. Gold formula / SQL expressiveness fit

### 6.1 What actually holds the formula authority

**The database Gold metadata is not the live formula authority.** `gold_metric_metadata` and
`gold_metric_dependency` are empty (section 4), so `src/nl2sql/semantic/metric_layer.py`, which reads
`... FROM v_metric_metadata`, resolves **no candidates at all today**.

The live definitions are **repository YAML**: `configs/semantic/gold/metrics/*.yaml` (10 files), with
`calculation_type: aggregation` (operation `count`/`sum`/`avg` over one source table with filters) or
`calculation_type: expression` (a derived expression string over named dependencies, e.g.
`CASE WHEN {total} > 0 THEN ROUND(CAST(COALESCE({on_time},0) AS DECIMAL)/{total}*100,2) ELSE NULL END`).

The runtime compiler contract is **much narrower than the YAML**
(`src/nl2sql/semantic/metric_contract.py`, `src/nl2sql/orchestration/metric_query.py`):

- `operation` is `Literal["count", "ratio"]` only; `ratio` means explicit numerator/denominator
  predicate subsets - **no free formula string, no weights, no ordering**.
- `supported_grains` are `day` and `month`; `supported_dimensions` are `city_company|area|team`
  (**single dimension only**).
- Predicates are only `is_true` / `is_null` / `is_not_null`; filters are only `eq` / `in`.
- Emitted SQL is **single-relation, no joins**:
  `SELECT [DATE_TRUNC(grain) AS period][, dim AS dimension_id] COUNT(*) | SUM(agg.value) [FILTER ...]
  FROM one_relation WHERE time >= ... AND time < ... AND predicates [GROUP BY ...] [ORDER BY ...] [LIMIT n]`.
- Aggregate mode is gated on `AggregateContract`, which requires distinct columns: `metric_key`,
  `formula_version`, `release_id`, `snapshot_id`, `checkpoint`, `time`, `grain`, `dimension`, `value`,
  `numerator`, `denominator`, `status`, `data_as_of`, plus a freshness record matched to the active
  release/snapshot/checkpoint.

### 6.2 The schema mismatch that currently blocks the aggregate path

| Gold table | Missing columns required by `AggregateContract` |
|---|---|
| `gold_metric_result` | `formula_version`, `release_id`, `snapshot_id`, `checkpoint`, `data_as_of`, `numerator`, `denominator` |
| `gold_maintenance_metric_daily` | `formula_version`, `release_id`, `snapshot_id`, `checkpoint`, `data_as_of` |

Neither Gold table can satisfy `AggregateContract` as-is. **The reachable runtime path today is
`approved_detail` (filter + count) over `ai_views.v_metric_result`** (`status = 'success'`, indexed on
`time_value`), not the pre-aggregated Gold facts.

### 6.3 Execution envelope for any pushed-down SQL

`src/nl2sql/infra/governance/query_gateway.py` (`PolicyEngine`, `POLICY_VERSION=query-gateway-v2`):

| Limit | Value |
|---|---|
| Statements | exactly one `SELECT`; no write/DDL/locking |
| Joins | cartesian joins forbidden; joins must carry `ON`/`USING` (`LATERAL` exempt) |
| CTEs | <= 5 |
| Nesting depth | <= 5 |
| Forbidden functions | `dblink`, `lo_*`, `pg_read_*`, `pg_sleep`, `set_config`, `nextval`, ... |
| Schema | restricted to the allowed scope |
| Rows | default `max_rows = 200` |
| Result size | <= 1 MB |
| Plan | `EXPLAIN` total cost <= 500,000; plan rows <= 100,000 |
| Transaction | read-only, with statement / lock / idle timeouts and an always-injected `LIMIT` |

### 6.4 Classification

Legend: **N** = `SQL_NATIVE`; **C** = `SQL_NATIVE_BUT_NEEDS_COMPILER_SUPPORT`;
**P** = `POST_QUERY_RUNTIME_LIKELY`; **I** = `INSUFFICIENT_EVIDENCE`. "Reachable today" means
expressible through the current `count`/`ratio`, `day|month`, single-dimension compiler contract.

| # | Formula family | Class | Reasoning |
|---:|---|---|---|
| 1 | Direct aggregation | **N** | `COUNT(*)` / `SUM(agg.value)` already emitted by `metric_query.py`. Reachable today. |
| 2 | Filtered aggregation | **N** | Gateway predicates plus `count(*) FILTER (WHERE ...)`. Reachable today. |
| 3 | Ratios / percentages | **N** | `COUNT(*) FILTER (num) / COUNT(*) FILTER (den)` with explicit `no_data` on zero denominator. Reachable today. |
| 4 | Weighted averages | **C** | `SUM(x*w)/SUM(w)` is plain SQL, but the contract has no weighted operation and no weight-field binding. Gold daily facts carry numerator/denominator, so weighted rates are derivable server-side. |
| 5 | Group-by dimension | **N** | Single `area_id`/`team_id` implemented; multi-dimension combinations are explicitly rejected. Reachable today. |
| 6 | Multi-source joins | **C** | The gateway allows constrained joins and every Silver/Gold table has FK-linked org columns, but the compiler compiles exactly one relation and has no join contract. |
| 7 | Conditional aggregation | **N** | `FILTER (WHERE ...)` is the ratio mechanism; boolean `is_*` flags are prevalent for pivoting. Reachable today. |
| 8 | Ranking / top-k | **N** | `ORDER BY value DESC NULLS LAST ... LIMIT {ranking_limit}` implemented and result-shape-validated. Reachable today. |
| 9 | MoM / YoY | **C** | Trivially `LAG(value,1)` / `LAG(value,12) OVER (PARTITION BY dim ORDER BY period)` or a self-join on `time_value - interval`; the compiler emits only intra-window trend buckets. Gold month series are also short. |
| 10 | Lag / lead | **C** | Core window functions, not blocked by the gateway, but absent from the compiler. |
| 11 | Rolling windows | **C** | `AVG/SUM(...) OVER (... ROWS BETWEEN n PRECEDING AND CURRENT ROW)` is core SQL; the compiler emits no window frame. Needs period-continuity handling. |
| 12 | Cumulative totals | **C** | `SUM(...) OVER (ORDER BY period)`; not in the compiler. |
| 13 | Percentiles | **C** | `percentile_cont`/`percentile_disc` are core (no `tablefunc` needed) but not permitted by the contract. Raw distributional percentiles are available from Silver detail numerics. |
| 14 | Standard deviation | **C** | `stddev_samp`/`stddev_pop` are core; not in the compiler, which blocks dispersion reporting. |
| 15 | Correlation | **C** | `corr`, `covar_*`, `regr_*` are core; they need two aligned numeric series in one query, which the contract cannot express. |
| 16 | Cohort / segment style analysis | **C** (multi-step variants **P**) | Single-query segmentation is native SQL; classic multi-step cohort retention/attribution needs staged CTEs or application orchestration, and only one dimension per query is allowed today. |

### 6.5 Verdict

PostgreSQL can **natively express all 16 families**. The concrete gap is the tt-ai compiler contract,
which admits only direct / filtered / conditional aggregation, ratios, single-dimension group-by and
top-k. Families 4, 6 and 9-16 are `SQL_NATIVE_BUT_NEEDS_COMPILER_SUPPORT` - a **controlled compiler
extension problem, not a database limitation**. **Nothing here forces post-query computation except
multi-step cohort orchestration.** No business semantics were inferred.

---

## 7. Forecast / ML technical readiness

No model was trained or executed. Only aggregate `MIN`/`MAX`/`count`/`distinct` were used; no series
values were read.

### 7.1 Time-series structure per representative series

| Series | time column | numeric target | history span | distinct buckets | level | missing-period risk |
|---|---|---|---|---:|---|---|
| `gold_metric_result` (day) | `time_value` | `value numeric(18,6)` | 2025-10-26 .. 2026-07-28 | 100 dates; per metric max 76, **21 series >= 30**, 4 >= 60, **0 >= 90** | already aggregated | **HIGH** |
| `gold_metric_result` (month) | `time_value` | `value` | 2000-01-01 .. 2026-09-01 | 9 dates; max 4 months per metric; **0 >= 12** | already aggregated | **HIGH** (sentinel) |
| `gold_maintenance_metric_daily` | `stat_date` | numerator/denominator/value | 2025-10-26 .. 2026-02-27 | 49 dates | aggregated | MEDIUM |
| `silver_repair_work_order` | `acceptance_time` | durations | 2025-09-19 .. 2026-02-23 | 89 dates | event-level | ~5 months |
| `silver_repair_service` | `report_time` | `archive_duration_minutes` | 2025-10-26 .. 2026-02-27 | 43 dates | event-level | ~4 months |
| `silver_installation_work_order` | `acceptance_time` | durations/flags | 2025-11-23 .. 2026-02-06 | 76 dates | event-level | ~2.5 months |
| `silver_fault_reporting_order` | `acceptance_time` | durations/flags | 2026-01-11 .. 2026-02-05 | 17 dates | event-level | ~4 weeks |
| `silver_single_faulty_order` | `work_order_arrival_time` | durations | 2025-12-29 .. 2026-02-05 | 12 dates | event-level | ~5 weeks |
| `silver_care_work_order` | `creation_time` | flags | 2026-01-05 .. 2026-02-23 | 10 dates | event-level | ~7 weeks |
| `silver_onsite_inspection_detail` | `entry_time` | `problem_count` | 2026-01-29 .. 2026-02-25 | 28 dates | event-level | ~4 weeks |
| `silver_machine_inspection_detail` | `work_order_complete_time` | port/quantity | 2026-01-29 .. 2026-02-25 | 28 dates | event-level | ~4 weeks |
| `silver_satisfaction_evaluation` | `stat_date` | q1..q3 scores | 2026-08-01 .. 2026-08-09 | 9 dates | event-level | ~9 days; **no org grouping** |
| `silver_fault_delivery_external_metric` | `time_value` | 43 rate columns | 2026-07-01 only | **1** | aggregated | single snapshot |
| `silver_external_metric_import` | `time_value` | external metrics | 2000-01-01 sentinel .. 2026-08-01 | 5 | aggregated | sentinel present |
| `silver_customer_service_rating`, `silver_weak_light_onu_statistics`, `consumer_home_broadband_customer_count_snapshot` | - | - | no data | **0** | - | unusable |

**Sentinel handling.** The `2000-01-01` values in month data are the **CURRENT_STATE storage sentinel**,
not genuine year-2000 history (product ruling, section 11). Any MoM / YoY / trend / forecast over these
series must **exclude CURRENT_STATE sentinel records according to metric semantics**, and must **not**
apply a naive global rule such as `date == 2000-01-01 -> CURRENT_STATE` without semantic context.

`created_at` / `updated_at` / `delete_datetime` appear on nearly every table but are ingestion/audit
timestamps (they cluster in Jan-Feb 2026 and Sep 2026), **not business event time**, and must not be
used as forecasting axes.

### 7.2 ML-readiness classification

| Capability | Class | Reasoning |
|---|---|---|
| Moving average | **DB-SQL POSSIBLE** | `AVG(value) OVER (ORDER BY period ROWS BETWEEN n-1 PRECEDING AND CURRENT ROW)`. |
| Exponential smoothing | **APP-RUNTIME NEEDED** | EMA is recursive/sequential; SQL would need a recursive CTE iterating per row, and the gateway caps CTE count/depth and forbids procedural execution. |
| Linear trend | **DB-SQL POSSIBLE** | `regr_slope`/`regr_intercept` over the available 30-76 bucket series. |
| Seasonal decomposition | **NOT ENOUGH DATA EVIDENCE** | No series has >= 2 seasonal cycles: day series max 76 buckets (0 >= 90), month series max 4 months (0 >= 12), spans are weeks-months with gaps and sentinel dates. |
| Simple regression | **DB-SQL POSSIBLE** | `regr_*` / `corr` are core; multiple candidate numeric targets and org dimensions exist. |
| Anomaly detection | **DB-SQL POSSIBLE** (statistical baseline) | Z-score / IQR against the per-metric bucket value is native SQL. Model-based or robust detectors are `APP-RUNTIME`. |
| Forecast with confidence / uncertainty notice | **APP-RUNTIME NEEDED** | SQL can produce a point estimate and even a residual stddev, but interval construction, translation into business language and the mandatory uncertainty disclaimer are application concerns - and the product never writes the business DB. |

### 7.3 Verdict

Time column, numeric target and organisation/group dimensions exist on Gold and Silver, and two Gold
tables are already aggregated to a time bucket: **the database has the ingredients, not the history.**
The limiting factor is history depth - at most ~90 contiguous daily buckets, at most 4 monthly buckets,
and many Silver domains span only weeks. Seasonal decomposition is **not supportable** from current
data; smoothing, trend and regression are mechanically possible for a **minority** of metrics (21
day-series with >= 30 buckets, 14 daily facts) but not for the long tail. **No predictive accuracy is
claimed or assessed.**

---

## 8. Runtime read-only identity evidence

### 8.1 Audit identity (provenance caveat)

`current_user = session_user = postgres`, **SUPERUSER** (`rolsuper`, `rolcreaterole`, `rolcreatedb`,
`rolreplication`, `rolbypassrls` all true). The audit DSN parsed (password never printed) uses
username `postgres`, host `127.0.0.1`, port `15432`, database `tt`. **Read-only was enforced by this
audit session, not by a restricted role.**

### 8.2 Roles

| `rolname` | super | inherit | createrole | createdb | canlogin | replication | bypassrls | connlimit | rolconfig |
|---|---|---|---|---|---|---|---|---|---|
| `agent_reader` | f | t | f | f | **f (NOLOGIN)** | f | f | -1 | NULL |
| `agent_reader_user` | f | t | f | f | **t (LOGIN)** | f | f | -1 | NULL |
| `postgres` | t | t | t | t | t | t | t | -1 | NULL |

`pg_auth_members`: `agent_reader_user` is a member of `agent_reader` (`admin_option = false`) and has
`rolinherit = true`, so it inherits the role's `SELECT` grants. **`agent_reader_user` is the only
non-superuser LOGIN role in the catalog.**

### 8.3 Grants

- `agent_reader`: `SELECT` on **27 relations**, all in `public` (`consumer`, `customer_service_relationship`,
  `gold_maintenance_metric_daily`, `gold_metric_dependency`, `gold_metric_metadata`, `gold_metric_result`,
  `organization_certificate`, `organization_company`, `organization_department`, `organization_employee`,
  `organization_external_account`, `organization_team`, `silver_care_work_order`,
  `silver_customer_service_rating`, `silver_fault_reporting_order`, `silver_installation_delivery`,
  `silver_installation_work_order`, `silver_machine_inspection_detail`, `silver_onsite_inspection_detail`,
  `silver_poor_quality_customer`, `silver_post_installation_poor_quality`,
  `silver_post_installation_weak_light`, `silver_repair_service`, `silver_repair_work_order`,
  `silver_single_faulty_order`, `silver_weak_light_onu_statistics`, `vadmin_area`).
- `agent_reader_user`: `SELECT` on **9 `ai_views` views** (`v_area`, `v_fault_reporting_order`,
  `v_installation_work_order`, `v_maintenance_metric_daily`, `v_metric_metadata`, `v_metric_result`,
  `v_repair_service`, `v_single_fault_order`, `v_team`).
- **Effective product access = 27 inherited + 9 direct = 36 relations.**
- `PUBLIC` grants: 190 `SELECT` + 1 `UPDATE`, **all** on `pg_catalog` / `information_schema` system
  objects (the single `pg_settings` `UPDATE` is the standard privilege that lets any user run `SET`).
  **No business table is granted to `PUBLIC`**, so business data is not world-readable. No
  column-level grants.
- **No `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` on any business relation for either role.**

### 8.4 Role- and database-level runtime settings

- Both roles have `rolconfig = NULL`: **no role-level `default_transaction_read_only`,
  `statement_timeout` or `lock_timeout`.** `default_transaction_read_only` is `off` at server default.
- `pg_db_role_setting` is **empty** (no per-database/per-role overrides).
- `max_connections = 100`; `rolconnlimit = -1` for both roles.
- Database ACL for `tt`: `PUBLIC=Tc` (TEMP+CONNECT), `postgres=CTc`, **`agent_reader=c` (CONNECT only)**.
- The `10000 ms` / `1000 ms` timeouts observed during the audit were set **by this probe session**, not
  by role or database defaults.

**Consequence: read-only on the product channel is enforced by `SELECT`-only privileges, not by a
transaction-read-only GUC.**

### 8.5 Product login determination - `CONFIG-EVIDENCED`

**`agent_reader_user` is the intended product login.** Configuration evidence (repository, read-only):

- `tt-intelligent-main/tt-ai/.env.prod:9` - `DATABASE_URL=postgresql+asyncpg://agent_reader_user:***@177.8.0.7:5432/tt`
  - the tt-ai product service uses `agent_reader_user` against the **same database (`tt`)** this audit
  read through the tunnel. `DATABASE_URL_ADMIN` is the separate DDL/admin credential.
- `tt-ai/src/core/settings.py:66-68` - `NL2SQL_READER_USER = "agent_reader_user"`,
  `NL2SQL_READER_ROLE = "agent_reader"`.
- `docker_env/postgres/init/nl2sql_readonly_grants.sql` + `grant_nl2sql_readonly.sh` - create NOLOGIN
  role `agent_reader`, create LOGIN `agent_reader_user`, `GRANT` the role, whitelist `SELECT`.
- The non-prod `tt-ai/.env.example` and `tt-api` dev configs point at a different dev database
  (`tt_db:7432`); production points at `tt`, matching the audited server.

DB evidence **corroborates** (`agent_reader_user` is the only non-superuser LOGIN role, is a member of
`agent_reader`, and holds the 9 `ai_views` grants) but alone would not be decisive. The audit's own
superuser connection is a provenance caveat, **not** the product identity.

---

## 9. Legacy DB architecture comparison

Legacy tree read: `E:\平台开发\tt-intelligent-main` (and its byte-identical in-workspace copy used for
MCP review). Citations without a prefix are `tt-ai/src/nl2sql/`. Verdicts are exactly one of
`KEEP CONCEPT` / `SUPERSEDED BY QUERYGATEWAY` / `DO NOT REUSE`.

| # | Legacy pattern | Legacy evidence | Verdict |
|---:|---|---|---|
| 1 | Dedicated read role | `docker_env/postgres/init/nl2sql_readonly_grants.sql:10-22`; `tt-api/scripts/initialize/initialize.py:269-336` | **KEEP CONCEPT** |
| 2 | Whitelist grants | `nl2sql_readonly_grants.sql:31-64`; `.../data/nl2sql_readonly_tables.json:2-30`; `initialize.py:178-291` | **KEEP CONCEPT** (DB-layer; partly lost) |
| 3 | `ai_views` exposure layer | `tt-ai/src/nl2sql/config/settings.py:84-95`; `infra/store/ai_views.py:452-484,570-644`; `database.py:58-81` | **KEEP CONCEPT** (sync wiring lost) |
| 4 | Schema restriction | `infra/store/database.py:20-23,212-213,300-308` | **SUPERSEDED BY QUERYGATEWAY** |
| 5 | SELECT/CTE-only enforcement | `infra/store/database.py:194-227` | **SUPERSEDED BY QUERYGATEWAY** |
| 6 | EXPLAIN validation | `infra/store/database.py:215-227` | **SUPERSEDED BY QUERYGATEWAY** |
| 7 | Statement timeout | `infra/store/database.py:36,186-189,219-222` | **SUPERSEDED BY QUERYGATEWAY** |
| 8 | Max-rows cap | `infra/store/database.py:37,190` | **SUPERSEDED BY QUERYGATEWAY** |

### 9.1 Why patterns 4-8 are superseded (each with its replacement)

- **Schema restriction** -> `query_gateway.py:413-445` `PolicyEngine._enforce_schema` traverses real
  SQLGlot scopes: it rejects `table.catalog`, rejects any `table.db` that is not the allowed schema, and
  injects the allowed schema onto unqualified tables. AST-based, so it cannot be defeated by comments or
  quoting the way the legacy regex could. Function namespaces are restricted at `381-394`, and
  `SET LOCAL search_path` is applied at `816-817`.
- **SELECT/CTE-only** -> `query_gateway.py:276-325` parses with SQLGlot, requires exactly one statement,
  requires an `exp.Query`, and rejects a forbidden-expression set (`_FORBIDDEN_EXPRESSIONS`, `194-208`).
  Complexity and function limits live at `349-394`. `sql_guard.py` now owns **no** execution logic and
  delegates everything.
- **EXPLAIN validation** -> `query_gateway.py:533-575` `preflight`; `672-763` runs
  `EXPLAIN (FORMAT JSON)` and enforces `max_plan_cost` and `max_plan_rows`; `1051-1093` is a strict plan
  parser that **fails closed** on missing or malformed plan fields. The legacy check had no thresholds.
- **Statement timeout** -> `query_gateway.py:801-817` `_begin_read_only` is the first statement on every
  fresh session and sets `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout`, `SET LOCAL
  lock_timeout`, `SET LOCAL idle_in_transaction_session_timeout` and `search_path`. Legacy had **zero**
  `statement_timeout` and relied only on a client-side `asyncio.wait_for`. The current control is
  enforced by PostgreSQL itself.
- **Max-rows cap** -> `query_gateway.py:447-474` injects a literal `LIMIT`, rejects non-literal / negative
  / `FETCH ... WITH TIES` limits, and clamps; `765-799` streams and **hard-fails** with `ROWS_EXCEEDED`
  or `RESULT_TOO_LARGE`. The legacy `fetchmany` was a **silent truncation**.

**Do not revive the legacy regex / `fetchmany` / raw-`EXPLAIN` code.**

### 9.2 The one `DO NOT REUSE` rule, and it is still alive in the current repo

**An LLM must never be the SQL authorship authority.** In the legacy tree the model wrote the SQL
(`agents/sql_agent/prompts.py:10-26`, `sql_generator.py:20-29`, `tools/async_sql_tools.py:48-73`).
**That channel still exists in the current repository:**

- `src/nl2sql/tools/async_sql_tools.py:44-64` - `sql_db_query` still takes a freeform model-authored
  `query: str` and passes it to `DatabaseManager.query()` -> `QueryGateway`. The gateway now **governs**
  it, but the **author is still the model**.
- `src/nl2sql/agents/sql_agent/sql_generator.py` - a 505-line current generate -> execute -> diagnose ->
  repair SQL loop.
- Other model-SQL surfaces: `agents/sql_agent/graph.py:50`, `agents/codeact_engine/parallel_fetcher.py:129,146,200-202`,
  `agents/codeact_engine/graph.py:467`, `agents/dynamic_calc/graph.py:452`, `agents/gen_data/tools.py:30`,
  `agents/nl2sql/graph.py:45`.
- `src/nl2sql/infra/runtime/registry.py:9-36` still registers those legacy SQL-agent graphs.
- `tests/unit/test_query_gateway.py:677` actively asserts `"create_async_sql_tools(db_manager)" in agent_source`
  - **the test suite locks the model-SQL path in place.**

The intended deterministic path **already exists but is dormant**:

- `src/nl2sql/orchestration/planning.py:294-335` `PlanCompiler.compile` builds a typed DAG and fails
  closed on hash mismatch.
- `src/nl2sql/orchestration/metric_query.py:221-466` `MetricQueryCompiler` deterministically emits
  **parameterized** SQL (assembly `446-454`; the ranking `LIMIT` comes only from the validated typed
  `plan.ranking_limit`).
- `src/nl2sql/orchestration/engine.py:95-118` requires a deterministic, zero-model `QueryPlanProvider`
  and hard-rejects a partially wired pipeline.
- **But `src/nl2sql/container.py:119-123` calls `create_v2_engine(checkpointer, model_gateway, trace_sink)`
  with no `context_resolver` / `query_plan_provider` / `plan_executor`, so `typed_pipeline_enabled = False`**;
  `after_route` (`engine.py:826-831`) then sends only `fast` to `compile` and everything else to
  `model_node`. The project's own evidence doc agrees: `docs-v4-p4-shared-plan-executor-hitl-evidence.md:267-283`.

**Relevance to P5:** the correct shape is typed `QueryPlan` -> `PlanValidator` -> deterministic
`PlanCompiler`/`MetricQueryCompiler` parameterized SQL -> `QueryGateway` as **defense in depth**. The
gateway patterns 4-8 are only acceptable *after* authorship is removed; they must never stand in for
authorship control, and any calculation pushed into the DB must travel the compiled path.

### 9.3 Containment status of the model-SQL path (precision on 9.2)

Section 9.2 is accurate as a statement about **code that exists**. It must not be read as a statement
that the path is currently reachable through the shipped product. Independent verification at HEAD
establishes the containment:

- The legacy **supervisor / freeform-SQL** path is **CLI-only**, and the container is forbidden by test
  from using it: `tests/unit/test_model_architecture.py:78-90` asserts `create_supervisor` is not in
  `container.py`, `get_supervisor` is not in `v2.py`, and `get_legacy_model` is not in the runtime registry.
- **Both sandboxes are dormant by default and cannot be switched on in product mode.**
  `codeact_mode` defaults to `"disabled"` (`src/nl2sql/config/settings.py:28-31`), and
  `src/nl2sql/config/settings.py:269-270` raises
  `ValueError("CODEACT_MODE=unsafe-dev is not permitted when SERVICE_MODE=product")`. Each sandbox also
  self-gates on `unsafe-dev` at runtime.

**Consequence for prioritisation:** the authorship debt is **real** (the code, the wiring into agent
graphs, and the test that locks it in all exist) but it is **latent, not currently exploitable through
the HTTP product path**. That is a statement about urgency, not about correctness - the disposal in
section 11.2 item 2 is unchanged.

### 9.3 Silently lost versus legacy

1. **`ai_views` startup auto-sync (highest value).** Legacy `database.py:58-81` invoked
   `sync_ai_views_from_yaml` on connect. Current `src/nl2sql/infra/store/database.py:57-82` does not;
   `sync_ai_views_from_yaml` has **zero callers**; `ai_views_auto_sync` (`config/settings.py:124`) is
   never read; `_resolve_ai_views_grantee_roles` is orphaned. **`configs/semantic/ai_views.yaml` can
   silently drift from the physical `ai_views` schema.**
2. **DB-level per-table whitelist grants.** The legacy 27-table allowlist is replaced in current
   provisioning by `docker/initdb/roles.sh:70` `GRANT SELECT ON ALL TABLES IN SCHEMA public`. New
   environments lose the physical read whitelist; only the pre-existing database retains it
   (`docs-enterprise-db-contract.md:175`).
3. **Role-level read-only / timeout settings.** `docs-enterprise-db-contract.md:177-178` records that
   the deployed role has no role-level `default_transaction_read_only`, statement timeout or lock
   timeout. This matches section 8.4 exactly. Current code compensates per connection and per
   transaction, but the **role-level hardening that would protect non-application connections is absent**.

---

## 10. Sandbox implications

This section answers the P5 decision question **from the database evidence only**. It deliberately does
not upgrade its verdict because some extension *could* be installed.

### 10.1 (a) What can realistically be pushed into PostgreSQL?

**Categorically: essentially the entire requested analytical surface.** Every requested aggregation,
window, statistical, percentile, regression, relational and time-bucketing family is
`SUPPORTED_BUILTIN` on core PostgreSQL 18.1 with **`EXTENSION_REQUIRED = NONE`** (section 3). The only
missing function, `grouping_id()`, is replaceable by `grouping()`.
A numeric percentage would be unwarranted here - what the evidence supports is the categorical
statement that **the database is not the constraint**. The binding constraints are:
the tt-ai compiler contract (`count`/`ratio`, `day|month`, single dimension), the `AggregateContract`
column mismatch against both Gold tables (section 6.2), and the gateway envelope.

### 10.2 (b) What still requires application-side computation?

A short and specific list:

1. **Recursive / sequential algorithms**, above all exponential smoothing: SQL would need a recursive
   CTE iterating per row, and the gateway caps CTE count and nesting depth and forbids procedural execution.
2. **Forecast interval construction and the mandatory uncertainty notice** - a presentation and
   disclosure obligation, not a math one.
3. **Multi-step cohort / retention orchestration** - staged composition that exceeds a single `SELECT`.
4. **Model-based (non-statistical) anomaly detection and ML scoring.**
5. **Anything exceeding the execution envelope**: more than one `SELECT`, more than 5 CTEs, nesting
   deeper than 5, more than 200 rows, more than 1 MB, plan cost above 500,000 or plan rows above 100,000.
6. **External / cross-database IO** - `postgres_fdw` is available but **not installed**, so no FDW path exists today.

### 10.3 (c) Does the DB evidence justify a general arbitrary-code sandbox?

**Answer: `PARTIAL` - a sandbox is useful only for bounded advanced/ML jobs; the evidence does NOT
justify a general arbitrary-code sandbox.**

The reasoning, from the evidence:

- **The analytical math needs no arbitrary code.** All 16 requested formula families are natively
  expressible in core SQL with zero extensions (sections 3 and 6). If the need were "run statistics",
  the database already covers it.
- **DB-side arbitrary code is not reachable, let alone approved.** No untrusted procedural language is
  installed, and `plpython3u` / `plperl` / `plv8` / `pltcl` / `plr` are not even available as control
  files (section 2.2-2.3). This is a statement about capability, not merely policy.
- **The data volumes are bounded and modest.** The largest relation is 2.1 GB / 1.58 M rows and **no
  relation is VERY_LARGE** (section 5.3). Anything that must be aggregated can be aggregated in SQL
  first.
- **The residual application-side needs are enumerable and narrow** (section 10.2) - five categories,
  each of which is a *bounded* computation over an *aggregated* input, not general-purpose code.

The evidence therefore does **not** support `YES: current business computation fundamentally requires
arbitrary code`. It supports a **restricted** runtime for those five categories. Two qualifications
must be recorded honestly:

- The sandbox question is ultimately a **product** question about forecast/ML ambition, and this audit
  provides the DB evidence, not that decision. If V4 intends open-ended analyst-supplied computation,
  the answer changes for reasons the database cannot speak to.
- The repository **already contains** a codeact engine and a `dynamic_calc` agent
  (`agents/codeact_engine/`, `agents/dynamic_calc/`). A sandbox-shaped capability therefore already
  exists in the codebase; what this evidence addresses is whether the **data and SQL layer** demands
  one. It does not.

### 10.4 (d) Maximum input shape for an application-side runtime

Prefer **aggregated and bounded** input; never raw unbounded rows. Concretely:

- **Preferred grain:** one row per (metric, time bucket, dimension) - exactly the `gold_metric_result`
  shape (231 metrics x at most ~100 buckets) or the `gold_maintenance_metric_daily`
  numerator/denominator/value shape.
- **Hard caps consistent with the existing envelope:** the input must have been produced by a single
  gateway-admitted `SELECT`, so <= 200 rows and <= 1 MB by default.
- **For time-series forecasting:** a single series should be at most a few hundred buckets; the actual
  observed maximum is 76 daily buckets and 4 monthly buckets, so the real constraint is history depth,
  not transfer size.
- **Never cross into the runtime with:** `silver_poor_quality_customer` (1.18 M rows / 883 MB),
  `batch_error_record` (1.58 M / 2.1 GB), `consumer`, or any raw Silver event table. Cohort or detail
  extracts must be **aggregated to the (time bucket x organisation dimension) grain first**.

### 10.5 (e) Responsibility split

| Responsibility | Owner | Scope |
|---|---|---|
| **SQL compiler** (deterministic typed plan -> parameterized SQL) | tt-ai | Direct, filtered and conditional aggregation; ratios; single- and (once extended) multi-dimension group-by; top-k / ranking; joins across approved relations. **Extension candidates, all already native SQL:** weighted averages, MoM/YoY via `LAG`/`LEAD`, cumulative and rolling windows, percentiles, `stddev`/`variance`, correlation and regression (`regr_*`). |
| **Calculation Runtime** (bounded, non-recursive, application-side) | tt-ai | Staged multi-step cohort composition; result-shape post-processing beyond the gateway envelope; unit and percentage normalisation (note the live `percent` vs `percentage` duplication); orchestrating several gateway queries into one answer. |
| **ML / forecast runtime** (bounded, advisory only) | tt-ai, separate runtime | Exponential smoothing; seasonal decomposition (currently **not supportable** - insufficient history); model-based anomaly detection; forecast intervals plus the uncertainty notice. **Must not masquerade as Gold business fact**; the product never writes the business DB. |

**Forecast capability is currently constrained by history, not by runtime:** at most ~90 contiguous
daily buckets, at most 4 monthly buckets, and a `2000-01-01` sentinel polluting month data.

### 10.6 Section verdict in one line

Push the analytical computation **into PostgreSQL through the deterministic compiler** (it already
supports every requested family), keep a **bounded, aggregated-input Calculation Runtime** for staged
cohort work and result post-processing, and a **separate, advisory-only ML runtime** for smoothing and
forecast intervals; a **general arbitrary-code sandbox is not justified by this evidence**.

---

## 11. Dispositions and remaining unknowns

The product review `v4_p5_advanced_calculation_alignment` (iteration 2) accepted this audit and
**closed six of the nine items below**. They are kept here as resolved rather than deleted, so the
evidence trail stays intact. Item 5 was removed from the unknowns list at the reviewer's explicit
request ("REMOVE the Silver-recency item from TRUE_EXTERNAL_UNKNOWNS").

### 11.1 Closed by product decision

| # | Original unknown | Disposition |
|---:|---|---|
| 1 | Can the Gold aggregate path ever be used? | **P3/P4 integration decides** how the authoritative Gold read contract maps current DB publication facts into the typed execution/receipt contract. Not a P5 architecture reason for change. Do **not** invent DB values merely to satisfy the contract, and do **not** block P5 sandbox architecture on this mismatch. Until it is mapped, only `approved_detail` over `ai_views.v_metric_result` is reachable. |
| 2 | Are the empty Gold tables retired or merely unpopulated? | **Does not reopen metric-definition authority.** The authoritative metric-definition source remains the governed canonical Gold semantic/YAML source already bound by P1. Do not wait for these tables; if Backend later populates them, that is a future integration/migration decision. |
| 3 | The `2000-01-01` sentinel | **Resolved: it is the CURRENT_STATE storage sentinel, not a historical business date.** Time-series / MoM / YoY / forecast training must **exclude CURRENT_STATE sentinel records according to metric semantics**. Do **not** implement a naive global `date == 2000-01-01 -> CURRENT_STATE` rule without semantic context. |
| 4 | `value_type` duplicate spelling | **Not a P5 blocker.** The Calculation Runtime operates on a **canonical semantic value type**; DB/source cleanup may happen later. Duplicate spelling must not alter arithmetic semantics. |
| 5 | ~~Data recency~~ | **RESOLVED AND REMOVED.** Confirmed by the product owner as **development/test data incompleteness**: the first version was built around Feb 2026, was intentionally not released because product quality/accuracy was insufficient, development paused, and V2/V4 work resumed recently; production business data will be completed before real launch. **Not** a P5, DB-correctness or data-platform blocker. During development it is expected that a current-period question may have no authoritative answer. Required Agent behaviour: (A) if an authoritative row exists with `status=no_data`, report the DB `no_data` semantics; (B) if no authoritative current-period result exists, report that current authoritative data is unavailable / not yet present; (C) do **not** fabricate a value, extrapolate stale data and present it as current fact, silently substitute Feb/older data for this week, or treat an ML forecast as the formal current metric. |
| 8 | Role-level hardening | **Defense-in-depth, NOT a P5 blocker.** Backend/DB/Ops should eventually harden `agent_reader` / `agent_reader_user` with appropriate `default_transaction_read_only`, `statement_timeout` and `lock_timeout` where operationally appropriate. The application already protects its own connection. |

Also frozen by the same review: **do not install extensions merely because P5 exists.** No requested
analytical capability needs `pg_stat_statements`, `tablefunc` or `pg_trgm`.

**Addendum to disposition 8 (role-level hardening).** This is not a pattern that has to be invented.
The repository's own compose provisioning already implements exactly it:
`docker/initdb/roles.sh:64-87` switches on `DB_APP_PRIVILEGES`, and in the `readonly` branch issues
`ALTER ROLE %I SET default_transaction_read_only = on` (`:68`) alongside `SELECT`-only grants, while the
`readwrite` branch issues the corresponding `RESET` (`:91`). So the mechanism ships in-tree; what is
missing is only its application to the *remote* `agent_reader` / `agent_reader_user` roles, whose
`rolconfig` is NULL (section 8.4). Framing this as "apply our own existing provisioning pattern to the
remote roles" is more actionable than framing it as new hardening work.

### 11.2 Still open

1. **`organization_department` is empty and `organization_team.department_id` is all-NULL.** The
   department layer is inert. Is any department-level grouping or authorisation intended? (The same
   finding that blocks the P2 authorisation slice.)
2. **Is the model-authored SQL path (`sql_agent` / `async_sql_tools`) to be retired, and when?** The
   deterministic typed pipeline exists but is dormant behind the `container.py:119-123` wiring gap,
   while `tests/unit/test_query_gateway.py:677` locks the model-SQL path in. The review confirmed this
   as **legacy debt to be retired during the P4/P5 migration**, and fixed the governing rule: *an LLM
   may propose a plan; an LLM must NOT remain the SQL authorship authority in final production.* This is
   still the largest architectural item this audit surfaced, and it conditions whether pushed-down
   calculation can be trusted.
3. **May any extension be installed at all?** Still an Ops/policy question (each needs
   `CREATE EXTENSION`, and `pg_stat_statements` additionally needs a server restart) - subject to the
   freeze above.

---

## Appendix - evidence provenance

- Read-only discipline and the SUPERUSER provenance caveat: section 1.
- Raw probe scripts and JSON (outside both repositories): `C:\Users\Density\.dsh\p5\`
  (`probe_A*.py` / `probe_A*_out.json`, `probe_b_*.py` / `out_meta.json` / `out_agg.json` / `out_cov.json`).
- No file in either repository was modified by this audit; the only repository artifact is this document.
