# V4 P2/P3 真实后端证据 · 旧仓库只读审计合并卷宗

来源：`E:\平台开发\tt-intelligent-main`（tt-admin / tt-ai / tt-api / docker_env）
方式：只读（read / grep / 文件头解析）。未修改任何文件，未执行任何 git 写操作。
凭据纪律：全文只写**变量名与文件位置**，不输出任何口令/密钥/base_url 值。

---

## 0. 三句话结论

1. **授权侧有两套完整实现，但都没接线。** 列级权限（表 + 服务）与行级/组织级数据范围（枚举 + 服务 + SQL 注入点）代码齐全，**零业务调用**，全部是死代码。
2. **P2 的核心缺口是一个具体的数据结构问题**：tt-ai 的 `AuthUser` 只有 `user_id / telephone / roles / permissions`，tt-api `/user/profile` 返回的 `company_id / department_id / team_id` 在 provider 映射时**被丢弃**。
3. **P3 没有 effective published result 的 origin/revision/override 模型**；旧系统的「有效」= UPSERT 覆盖 + `max(computed_at)` + `status='success'`，「人工 override」只有批次回滚。

---

## 1. 认证与身份

| 项 | 事实 | 证据 |
|---|---|---|
| 登录入口 | `POST /login`（手机号密码/短信）、`POST /api/login`(Swagger OAuth2)、`POST /api/token`(client_credentials)、微信一键登录 | `tt-api/src/apps/vadmin/auth/auth_router.py:128/:47/:104/:182` |
| 登录表单 | `telephone / password / captcha_key / captcha_code / method("0"密码,"1"短信,"2"微信) / platform` | `auth/utils/validation/login.py:15` |
| Token | HS256 + `settings.SECRET_KEY`，`ACCESS_TOKEN_EXPIRE_MINUTES=1440` | `auth/utils/login_manage.py:44` |
| **JWT 载荷含密码哈希** | `{"sub": user.telephone, "is_refresh": False, "password": user.password}`（access 与 refresh 都带） | `auth/service.py:1406-1420` |
| 请求头 | `Authorization: Bearer <token>` | `tt-api/src/core/settings.py:107` |
| 每请求鉴权 | 验签 → 用 `telephone + password` 重新查库 | `auth/utils/validation/auth.py:55`、`auth/repository.py:82` |
| 会话 | **无服务端 session 表 / token 黑名单 / 吊销接口** | 全仓无 |
| 身份对象 `Auth` | `user / db / data_range / dept_ids` | `auth/utils/validation/auth.py:19-23` |
| 用户字段 | `telephone/name/nickname/email/.../position_id/employee_id` + `roles/position/employee` | `auth/models/user.py:20` |
| 供 AI 的 profile | `company_id/company_name/department_id/department_name/team_id/team_name/position/personnel_center/.../roles/permissions` | `auth/service.py:120-184`、`auth/schemas/user.py:8-48` |
| ⚠️ 登录响应回带密码哈希 | `UserPasswordOut` 暴露 `password` | `auth/schemas/user.py:117-120` |
| ⚠️ SECRET_KEY 硬编码默认值 | — | `tt-api/src/core/settings.py:101` |

### 1.1 tt-ai 侧共享身份对象（P2 直接相关）

```python
# tt-ai/src/core/auth/types.py:6
@dataclass(frozen=True, slots=True)
class AuthUser:
    user_id: int | str
    telephone: str | None
    roles: list[str]
    permissions: list[str]
```

- Provider 调 tt-api `/vadmin/auth/user/profile`（`tt-ai/src/core/settings.py:50`），**只映射 4 字段**，
  `company_id / department_id / team_id` 被丢弃 —— `tt-ai/src/core/auth/provider.py:134-151`。
- 鉴权入口：`tt-ai/src/core/auth/dependencies.py:52`（require_user）、`:29`（`_has_permission`，`"*"`/`"*.*.*"` 视为全量）、`:117`（`require_nl2sql_permission` → `nl2sql:invoke`/`nl2sql:stream`）。

---

## 2. 授权（列级 / 行级 / 覆盖门槛）

### 2.1 唯一实际生效：菜单/按钮 RBAC + OAuth scope
- `FullAdminAuth(permissions=[...])`：查用户→角色→菜单 perms，`is_admin` 返回 `{"*.*.*"}` —— `tt-api/src/common/security/dependencies.py:71-116`
- 权限集来源 `get_user_permissions`（遍历 `role.menus[*].perms`）—— `auth/utils/validation/auth.py:124-138`
- scope 校验 —— `auth/service.py:1376-1398`（`allowed_scopes`）

### 2.2 列级权限：**有表、有服务、零调用**
- 字段：`column_permissions JSONB -- 列级权限 {"visible": [], "editable": []}` —— `auth/models/permission.py:72-76`
- 服务：`get_column_permissions:130` / `filter_columns:155` / `filter_columns_list:184` / `_get_merged_resource_permission:274` —— `auth/services/permission_check.py`
  ```python
  if not allowed_columns: return {}
  if "*" in allowed_columns: return data
  return {k: v for k, v in data.items() if k in allowed_columns}   # 白名单隐藏，非脱敏
  ```
- **全仓无业务代码调用**，仅 `common/dependencies/data_scope.py:195`（其自身也未被使用）调用一次。
- 无 mask 类型（FULL/PARTIAL/NULL），仅 visible/editable 白名单。

### 2.3 行级 / 组织级数据范围：**设计完整，运行时未启用**
- 枚举 `DataScope`：`ALL=1 / CUSTOM=2 / COMPANY_AND_BELOW=3 / DEPARTMENT_AND_BELOW=4 / TEAM_ONLY=5 / SELF_ONLY=6` —— `common/enums.py:8`
- 组织范围计算 —— `auth/services/data_scope.py:104-171`；CUSTOM 查 `vadmin_role_org_scope` `:277-324`；递归子部门 `:173-199`
- 行过滤注入点 —— `common/repository.py:169-232`
  ```python
  resource = await self._get_resource_config()
  if not resource: return stmt          # resource_code 未传 = 完全不过滤
  ...
  return stmt.where(org_col.in_(accessible_orgs.team_ids))
  ```
- 带权限查询 API：`repository.py:1044/1078/1114/1193/1228`

**未启用硬证据（三条）**
1. `get_data_scope_filter` / `require_resource_permission` **无任何 router import**（`common/dependencies/data_scope.py:105,208` 只出现在自身定义与文档）。
2. 所有仓储 `super().__init__(model=..., db=db)`，**无一处传 `resource_code`**（20+ 处）→ `_apply_data_scope` 恒返回原 stmt。
3. 所有 `*_with_scope` 方法**全仓无调用点**。

旧文档自认前提：`tt-api/docs/permission-config-guide.md:72`「Repository 需传入 `resource_code` 才会启用自动数据范围过滤。」

### 2.4 「区县不能看市级」覆盖门槛：**不存在**
- 授权维度只有 `company / department / team` —— `enums.py:14-19`、`permission.py:106-110`
- `vadmin_area.level`(1省/2市/3区县) 是纯业务维度，**与角色权限表无 FK/关联** —— `area/models.py:22`；`role_permission/service.py:208` 只接受 `{company,department,team}`
- 无 `minimum_query_org_level` / `coverage_root_org` / `coverage_org_level`（全仓 grep 无命中）
- 用户可自由传 `area_id` 且不与身份求交 —— `tt-api/src/apps/consumer/router.py:84/:97/:107-121`

### 2.5 sibling 组织
- 只有祖先链 + 子树：`Employee.team → Team.department → Department.company`（`data_scope.py:52-102`），下级按 `parent_id`（`:190`）
- **无同级集合查询** → 兄弟组织天然不在 `accessible_org_ids` 内
- 缺陷（若启用）：`SELF_ONLY` 与 `TEAM_ONLY` 代码相同，均只返回 `team_id`（`:138-146`），`SELF_ONLY` 未按 `created_by` 收敛

---

## 3. 组织树与地域

| 层级 | 表 | 关键列 | 父/祖先 |
|---|---|---|---|
| 分公司 | `organization_company` | id,name,code,short_name,is_active | 无父，根 |
| 部门 | `organization_department` | company_id,**parent_id**,name,code | 自关联 `parent_id` |
| 班组 | `organization_team` | department_id,name,code,leader | 单父 FK |
| 员工 | `organization_employee` | company_id,department_id,team_id,employee_code,id_card,phone | 三个 FK，**无 path** |
| 地域 | `vadmin_area` | name,code(adcode),**level**,parent_id | 自关联 `parent_id` |

```python
# tt-api/src/apps/organization/department/models.py:48
parent_id: Mapped[int | None] = mapped_column(
    ForeignKey("organization_department.id", ondelete="CASCADE"), comment="上级部门ID")
```

- 存储方式：**邻接表**，无 `path/path_key/ancestor/level` 物化列；递归在 Python 里做（N+1）—— `data_scope.py:173-199`
- 员工导入按「所属区县 + 班组」建班组，区县仅用于命名/去重，**不参与授权** —— `organization/employee/service.py:1058-1070`

---

## 4. 敏感字段与模型外发（ModelInputPolicy）

**结论：无 SensitiveField、无脱敏、无 provider 许可、无 fallback 重核验；能查即能发。**

### 4.1 无 SensitiveField
明文 PII 字段全无标记：`id_card`（`organization/employee/models.py:41`）、`phone/emergency_phone`（`:97-108`）、`consumer.phone`（`consumer/models.py:39`）、`contact_phone`（`organization/{team,company,department}/schemas.py:17,33,45`）。
唯一正则是**格式校验**：`organization/employee/utils/validators.py:29` `ID_CARD_PATTERN`、`common/schemas/fields.py:13-55`。
`column_permission` / `column_mask_policy` 只存在于 `tt-api/权限控制.md:161-184`，**代码 0 引用**。

### 4.2 无脱敏
唯一 `mask` 是 SQL 字面量遮蔽（用于 schema 越权检查）—— `tt-ai/src/nl2sql/infra/store/sql_utils.py:28-44`，调用点 `database.py:301`。

### 4.3 实际外发面（全部无差别明文）
| 出口 | 位置 |
|---|---|
| LLM（单一 provider `ChatOpenAI`） | `tt-ai/src/nl2sql/infra/llm/factory.py:11-53`，`OPENAI_BASE_URL`/`MODEL_NAME`（默认 `jiutian-lan-comv3`，`core/settings.py:36-38`） |
| schema（列名/类型/注释）直塞 prompt | `tools/async_sql_tools.py:31-46`、`agents/sql_agent/graph.py:86-96` |
| **真实业务数据行（含 PII 值）回灌模型** | `tools/async_sql_tools.py:48-73`，上限 `NL2SQL_MAX_QUERY_RESULTS`=200（`database.py:37,190`），工具结果进 ToolMessage `agents/nl2sql/nodes.py:132-136` |
| **用户手机号进 system prompt** | `supervisor/agent.py:83-92`（`user_id=..., telephone=..., roles=...`），来源 `nl2sql/api.py:95-99/:356-358`、`core/auth/provider.py:189-191` |
| RAG 业务文本 embedding 外发 + 命中注入 | `qa_rag.py:150-226`、`semantic_rag.py:200-225`，检索 `qa_rag.py:243-303`，调用点 `agents/sql_agent/agentic_rag.py:27-42,122-161` |
| **Langfuse 全量上报 LLM 输入/输出** | `infra/observer/langfuse.py:47-61`，**只有 enabled 开关、无脱敏**，挂载 `core/observer.py:33-53` |
| PG checkpointer 整段对话落库 | `infra/memory/checkpointer.py:40-55` |
| 会话历史 HTTP 暴露 | `nl2sql/api.py:234-283`（只校验操作权限，无字段级过滤） |
| 历史会话压缩后再送模型 | `supervisor/agent.py:291-295` `SummarizationMiddleware` |

### 4.4 无 provider 许可、无 fallback、无 reranker、无 CodeAct
- `fallback` 命中全是 **SSE 输出兜底**（`nl2sql/api.py:445-482`）与评测默认值；只有 `ModelRetryMiddleware(max_retries=3)` 同模型重试 —— `supervisor/agent.py:296-307`
- provider 由 `OPENAI_BASE_URL`/`MODEL_NAME` 静态决定，单实例 `@lru_cache(maxsize=16)` —— `factory.py:11`
- **无 reranker**（`rerank|re-rank|重排` 全仓 0 命中），仅 FAISS distance 阈值
- **无 CodeAct/代码执行/图片生成**（`exec|subprocess|codeact` 在 tt-ai/src 0 命中）

### 4.5 权限只到表级
`docker_env/postgres/init/nl2sql_readonly_grants.sql:31-64` **按表授权、不按列**，含 `public.organization_employee`（`id_card`/`phone`）。视图列白名单见 `tt-ai/configs/semantic/ai_views.yaml`。

---

## 5. DB 拓扑、连接与凭据

### 5.1 服务与连接（生产 docker-compose.yml）

| 服务 | 监听 | 存储 | 依据 |
|---|---|---|---|
| tt-api | expose 9000，IP 177.8.0.2 | PG `177.8.0.7:5432/tt` + Redis `177.8.0.5:6379/1` | `docker-compose.yml:2-19` |
| tt-worker / tt-beat | — | 同上 | `:21-40` / `:42-61` |
| tt-nginx | 12080:80, 12088:443 | — | `:63-84` |
| tt-redis | expose 6379 | — | `:86-99` |
| tt-postgres | **expose 5432，未发布到宿主机** | PGDATA `./docker_env/postgres/data` | `:101-117` |
| tt-ai | expose 9001，IP 177.8.0.8 | PG `177.8.0.7:5432/tt`（只读身份） | `:119-142` |

`tt-ai` 日常用 `agent_reader_user`、DDL 用 `postgres` —— `tt-ai/.env.prod:9-10`；`TT_API_BASE_URL=http://177.8.0.2:9000` `:143`。

### 5.2 连接串来源（只写变量名）
`tt-api/.env.prod:13-14` `SQLALCHEMY_DATABASE_URL`/`DATABASE_URL`；`tt-ai/.env.prod:9-10` `DATABASE_URL`/`DATABASE_URL_ADMIN`；`tt-api/alembic.ini:56-65` `[dev]`(127.0.0.1:7432/tt_db) / `[pro]`(177.8.0.7:5432/tt)；`tt-api/src/core/settings.py:64` 硬编码默认 dev URL。
dev PG 发布在宿主 `127.0.0.1:7432`（`docker-compose.dev.yml:23-24`），库 `tt_db`、用户 `tt_admin`。

### 5.3 SSH tunnel：**仓库内不存在**
`tunnel` 全仓 0 命中。唯一远端 SSH 是**前端部署**：`tt-admin/deploy.sh:7-11` `REMOTE_HOST="nas.visionblue.cloud"` / `REMOTE_PORT="12322"` / `REMOTE_USER="ai"`，`:78` scp、`:88` ssh+docker restart。**不是 DB 隧道。**
→ 计划 §1.4 记录的「SSH tunnel 只读审计」属会话外手工通道，**无法从 file:line 复原**。

### 5.4 仓库自带 PG18 PGDATA 快照（强证据）
- `docker_env/postgres/data/18/docker/PG_VERSION` = `18`
- `global/pg_control` 头 16 字节 → control version `0x708` = **1800（PG18 系列）**
- `global/1262`(pg_database) ASCII 串仅 `tt_db / postgres / template0 / template1`
→ 快照是 **dev `tt_db`**；**审计观察中的生产 `tt` 库不在仓库内**，151,985 行/0/0 无法在仓库内独立复核。

### 5.5 凭据（仅变量名与位置）
`docker-compose.yml:112 POSTGRES_PASSWORD`；`docker-compose.dev.yml:32 POSTGRES_PASSWORD`；`tt-api/.env.prod` 的 `SQLALCHEMY_DATABASE_URL/DATABASE_URL/SECRET_KEY/REDIS_* /CELERY_*`；`tt-api/.env.prod.example:15-17` `NL2SQL_READER_USER/ROLE/PASSWORD`；`tt-ai/.env.prod` 的 `DATABASE_URL/DATABASE_URL_ADMIN/OPENAI_API_KEY/EMBEDDING_API_KEY/LANGFUSE_*`；`docker_env/postgres/grant_nl2sql_readonly.sh:9-15` `PGHOST/PGPORT/PGUSER/PGDATABASE/NL2SQL_*`。
⚠️ `tt-api/AGENTS.md:79` / `CLAUDE.md:79` / `src/mcp/README.md:6` **含明文口令**（位置指路，值不输出）→ 凭据管理问题。

---

## 6. 表清单（模型声明 71 张）

**Gold（4）**：`gold_metric_result`（`gold/metric_system/models.py:99`）、`gold_metric_metadata`（`:280`）、`gold_metric_dependency`（`:424`）、`gold_maintenance_metric_daily`（`gold/maintenance/models.py:30`，唯一「日报表」，dimension_type 仅 area|team）

业务唯一键（关键）：
```python
# models.py:99-112
UniqueConstraint("metric_code","time_grain","time_value","dimension_type",
    "area_id","team_id","employee_id","category_code",
    name="uq_metric_result", postgresql_nulls_not_distinct=True)
```

**Silver（18）**：`silver_care_work_order / complaint_verification / customer_service_rating / external_metric_import / fault_delivery_external_metric / fault_reporting_order / installation_delivery / installation_work_order / machine_inspection_detail / onsite_inspection_detail / poor_quality_customer / post_installation_poor_quality / post_installation_weak_light / repair_service / repair_work_order / satisfaction_evaluation / single_faulty_order / weak_light_onu_statistics`

**Bronze（20）**：对应的 `bronze_*` + `bronze_external_metric_import / bronze_skill_certification_management / bronze_weak_light_onu_statistics / bronze_table_upload_batch`

**批次/错误**：`bronze_table_upload_batch`（`batch/models.py:24`）、`batch_error_record`（`:212`）

**组织/地域**：`organization_company` / `organization_department` / `organization_team` / `organization_employee` / `organization_external_account` / `organization_certificate` / `vadmin_area`

**消费侧**：`consumer` / `customer_service_relationship` / `consumer_home_broadband_customer_count_snapshot`

**vadmin（18）**：含 `vadmin_data_resource`（`code/table_name/org_field/org_level`）、`vadmin_role_resource_permission`（含 `column_permissions` JSONB）、`vadmin_role_org_scope`（`org_type/org_id/include_children`）、`oauth_client`

**关键迁移**：`a4d4f1148b2a_3_10_1.py:80/:587-606/:669-684/:833-866`；`5cb160b5fddb_3_10_1.py:40/:65`；`de7284e45816_release_20260908.py:397-400/:431`

---

## 7. 有效发布 / override / 来源分类

### 7.1 「有效结果」三要素（无 revision 表）
```python
# gold/metric_system/repository.py:14-41   发布 = 按业务键 UPSERT，分块 500
stmt = stmt.on_conflict_do_update(constraint="uq_metric_result",
    set_={"value":..., "status":..., "computed_at":..., "source_batch_no":...,
          "source_system":..., "updated_at": func.now()})
```
```python
# gold/metric_system/service.py:88 / :130   有效 = max(computed_at)
func.max(MetricResult.computed_at).label("max_computed_at")
# :501-524  row_number() over(partition by metric_code,dimension_type,area_id,category_code
#                              order by time_value desc, computed_at desc) ... where status == SUCCESS
```
状态四值：`success/failed/partial/no_data`（`models.py:63-69`），
质量分规则 `engine/status.py:16-45`（error→FAILED/0；全缺→NO_DATA/0；部分缺→PARTIAL + 100*(1-missing/total)；value None→NO_DATA/0；否则 SUCCESS/100）。

### 7.2 「人工 override」= 批次回滚（带回滚恢复 supersede 语义）
- 入口 `batch/router.py:99-135` `DELETE /data_repository/batch/{batch_no}`（`current_superuser`）
- 写回滚字段 `batch/service.py:411-415`：`is_rollbacked/rollback_user_id/rollback_time/rollback_reason/status=ROLLBACKED`
- 三层状态 + `import_mode=append|replace|update`、`duplicate_strategy=skip|update|error` —— `batch/models.py:83-103,:173-191,:192-205`
- supersede 恢复 `gold/metric_system/external_publisher.py:294-295`：
  「恢复仍由目标批次占有的业务键；已被后批次覆盖的结果不受影响。」`:338-346` 只取 `import_time < batch.import_time` 且 `gold_status="SUCCESS"` 且未回滚的批次；`:358-366` 每业务键取最近历史成功值 → delete 当前行 → upsert 恢复。
- 另一类覆盖：`generic_external_publisher.py:203-238`（`replace_period_data` 按 time_value 整段替换 / `current_state` 按 metric_code 整表替换），开关 `external_metrics/templates.py:51-54`

**`gold_metric_metadata=0` 的解释**：元数据同步是**手动 CLI**（`tt-api/main.py:148-177` `sync_metrics`），FastAPI lifespan 只挂/摘 Redis（`src/core/event.py:16-27`）；而计算用 **YAML 定义**（`gold/metadata/metrics.yaml:13-19` → `metrics/` 目录；`engine/configuration.py:94-138`），**不读 DB 元数据表**。
→ 副作用：`/v2/metrics/metadata` 返回空、threshold 缺失 → `achievement_status` 无配置。

### 7.3 来源分类
```python
# models.py:193-204 / :156-160
source_batch_no: String(100)|None, index=True, comment="来源导入批次号"
source_system:   String(64)|None,               comment="来源外部系统"
dimension_type:  String(50), comment="all/area/team/area_team/area_team_employee"
```
- 外部宽表映射 `external_publisher.py:34-38`：`area→area`、`team→area_team`、`employee→area_team_employee`；`:98-121` 强制粒度一致性
- `source_system` 取值：`fault_delivery_analysis`、`h5_satisfaction_notice`、`complaint_reduction_status`、`home_install_delivery_analysis`、`post_install_60d_quality_analysis`、`quality_remediation_quarterly`、`single_fault_post_receipt_reinvestment_status`、**`"manual"`**（`consumer/services/consumer_service.py:975-976`，`source_batch_no=None`）
- 派生指标从依赖继承来源 `service.py:978-1011`，无依赖回落 None `:1052-1053/:1114-1115`
- `batch_no` 格式 `BATCH_YYYYMMDD_HHMMSS_XXX`（`batch/models.py:30-36`）；`category_code=fttr/gigabit/standard/all`

---

## 8. 只读消费通道

- **tt-api 侧全部要求超管**（`Depends(current_superuser)`），不是对外只读：`/v2/metrics` 前缀 `src/urls.py:65-68`；`router.py:280/:381/:448/:563/:628/:672/:756`；维护日报 `gold/maintenance/router.py:109/:149/:206/:246/:358/:389`
- **tt-ai 才是真正只读通道**：`database.py:204-210` 仅允许 SELECT/CTE、禁多语句、禁 `DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT`；`:212-213` 限定 `NL2SQL_DB_SCHEMA`；`:219-222` 执行前 EXPLAIN。env `tt-ai/.env.prod:88 NL2SQL_DB_SCHEMA=ai_views`、`:91 AI_VIEWS_AUTO_SYNC=true`
- **只读白名单 27 张**（含全部 4 张 gold 表）：`docker_env/postgres/init/nl2sql_readonly_grants.sql:31-63`（先 `REVOKE ALL` 再 `GRANT SELECT`）；代码侧同源 `tt-api/scripts/initialize/data/nl2sql_readonly_tables.json:2-30`
- **ai_views 9 视图**（`tt-ai/configs/semantic/ai_views.yaml`）：`v_metric_result`(:216, `status=success` filter) / `v_metric_metadata`(:251) / `v_maintenance_metric_daily`(:272) / `v_single_fault_order`(:8) / `v_repair_service`(:51) / `v_fault_reporting_order`(:98) / `v_installation_work_order`(:155) / `v_area`(:301) / `v_team`(:324)

---

## 9. 未找到 / 不确定（诚实边界）

1. **SSH tunnel 仓库内不存在**（`tunnel` 0 命中；`deploy.sh` 是前端 scp/ssh，非 DB 隧道）→ 隧道参数无法从 file:line 复原。
2. **生产 `tt` 库内容不在仓库**（PGDATA 快照仅 `tt_db`）→ 计划 §1.4 的 151,985 / metadata=0 / dependency=0 无法在仓库内独立复核。
3. **18.1 的 minor 版本无文件证据**（只有主版本 18 / control_version 1800）。
4. **dev 与 prod 共用挂载路径** `./docker_env/postgres/data`（`docker-compose.yml:108` / dev `:28`），`POSTGRES_*` 仅首次 initdb 生效 → 库名/凭据声明冲突。
5. **无「人工 override」专用表/API**（无 `origin/revision/superseded_by/is_current`）；**逐值人工改数并留痕能力缺失**，只有 manual 快照与批次回滚两条间接路径。
6. `gold_maintenance_metric_daily` 写入触发链未逐行追平（Celery beat 调度与 `silver/signals.py` 未核对）。
7. `tt-ai/docker/docker-compose.yml:9-10` 发布 8000:8000，与正式 compose 9001/expose 不一致，疑似遗留。
8. 生产 nginx 反代规则未逐行核对。
9. ⚠️ **安全项（非 P2 判定项）**：`docker-compose.yml:112` 硬编码 DB 口令；`AGENTS.md:79`/`CLAUDE.md:79`/`mcp/README.md:6` 含明文口令；JWT 载荷含密码哈希；`SECRET_KEY` 有硬编码默认值；Langfuse 全量上报无脱敏开关。

---

## 10. 计划条款 → 旧系统事实（映射表）

| 计划要求（§3.4 / §3.5 / §8.14 P2/P3） | 旧系统事实 | P2/P3 含义 |
|---|---|---|
| `AuthorizationContext`：subject / resource_scope / org·person_scope / provenance / **authorization_revision** | 只有 `AuthUser{user_id,telephone,roles,permissions}`；org 字段被抓取后丢弃；**无 revision/version/etag** | **全新建**；revision 只能由有效权限 snapshot/policy hash 派生 |
| `RelationCoverage`：coverage_root_org / coverage_org_level / row_org_field / minimum_query_org_level / detail_sensitivity | `vadmin_data_resource.org_field/org_level` 是**表级**配置；**无 per-relation 元数据、无覆盖门槛** | **全新建**；不得从表名或 `dimension_type=all` 猜「市级」 |
| 列级 `column_scope`（计划：待真实 backend 证据确认） | `vadmin_role_resource_permission.column_permissions` JSONB **存在但零调用**；无 mask 类型 | **证据=不存在可用实现**；计划「不得宣称原生 column_scope 已存在」已被证实 |
| 行级/组织权限 | `DataScope` 枚举 + `data_scope.py` + `repository.py:169-232` **齐全但零接线**（无 `resource_code`） | 可作**设计参考**，不可当现有能力 |
| `SensitiveField` / `ModelInputPolicy` | 无敏感注册表、无脱敏、无 provider 许可、无 fallback 重核验；6 类出口全明文 | **全新建**，需在 6 类出口插执行点 |
| 只读消费 `PublishedMetricReader` | 有 27 表白名单 + 只读角色 + 9 视图 + SELECT-only 校验 | **可复用**，但对外只读 API 需新增权限点（现全超管） |
| effective published result + override provenance | UPSERT + `max(computed_at)` + `status='success'`；override 仅批次回滚 | **可复用语义**，但缺 origin/revision/supersede 的显式模型 |
| status / DQ / freshness | 四值 + 质量分规则齐全 | **可直接对接** |
| 来源分类 | `source_system` + `source_batch_no` + `dimension_type` 三元组齐全 | **可直接对接** |

---

*本卷宗为只读审计产物，不含任何凭据明文；所有「计划明确」与「旧系统事实」均已分离标注。*
