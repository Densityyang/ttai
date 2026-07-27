# TT-AI v0.2 可治理单机交付版主 PR 实施计划

> 版本：2026-07-27 当前阶段修订版  
> 部署基线：Docker Compose v2 + Nginx + control/checkpoint 独立 PostgreSQL 卷 + 外部业务 PostgreSQL  
> 定位：从“功能完备原型”升级为“单机/少节点可稳定交付、可验证、可回滚的 NL2SQL Agent 服务”  
> 说明：本文是对原 K8s/多租户主 PR 方案的完整替换，原 Review 与技术调研结论仍作为背景材料。

---

## 1. 本次锁定的工程决策

### 1.1 本阶段必须交付

1. 使用 Docker Compose v2 统一管理 Nginx、API、后台任务和 PostgreSQL。
2. Nginx 是唯一对外入口，FastAPI 不再向宿主机直接暴露端口。
3. 产品运行于“单部署域 + 多用户”模式，不宣称具备多租户隔离能力。
4. control 与 LangGraph checkpoint 使用 Compose 独立 PostgreSQL 实例和独立命名卷；生产业务库使用现有外部 PostgreSQL，开发/测试才启用本地 business PG。
5. 保留 v2 API、结构化工件、Context Compiler、风险路由、SQL Policy、真实 HITL、发布门禁和回滚机制。
6. 所有 SQL 必须经过进程内 `QueryGateway` 模块；所有模型请求必须经过进程内 `ModelGateway` 模块。
7. 构建、迁移、索引发布和 benchmark 从 API lifespan 中拆出，使用一次性 Compose 任务；当前阶段使用 DeepSeek 与 NVIDIA NIM 做真实模型 benchmark。
8. 任意生成 Python 代码的 CodeAct 在 Compose 产品模式默认禁用；只保留白名单模板计算。
9. DeepSeek V4 Flash 是默认小模型候选；V4 Pro/1M 上下文模型只允许在 `plan` 阶段并通过预算与策略门禁调用。
10. 模型、Endpoint 和密钥均通过 provider-neutral profile/secret 注入，生产可替换其他 OpenAI-compatible 模型而不修改编排代码。

### 1.2 明确延后，不进入本主 PR

- Kubernetes、Helm、Gateway API、HPA、PDB 和 RuntimeClass。
- 独立 API Gateway/LLM Gateway/Query Gateway 集群。
- 完整多租户、租户 RLS、租户动态数据源池和租户级配额。
- 自建 Vault 集群、动态数据库账号和 Kubernetes Auth。
- GPU 推理集群、本地 80B 模型服务和分布式推理路由。
- Service Mesh、分布式任务队列和跨主机自动容灾。
- 在缺少强隔离运行时时开放任意 CodeAct。
- 把任一供应商模型 ID、API key 或 Endpoint 写死在 Agent/业务代码中。

延后不等于删除架构边界。本阶段会通过 `Protocol`/接口、结构化工件和配置层，保证 `QueryGateway`、`ModelGateway`、`SecretProvider`和 `CheckpointStore` 以后可以被拆分为独立服务。

---

## 2. 主 PR 目标与非目标

### 2.1 产品目标

- 单条低风险 NL2SQL 查询可优先使用小模型稳定生成、验证、执行和解释。
- 复杂问题只在置信度/风险达到条件时才进入 Deep 或候选共识路线。
- SQL、语义版本、检索证据、策略决策和执行回执可重放。
- 任一外部模型、Embedding、GraphRAG、Langfuse 故障有明确降级路径；模型 API 全部不可用时返回结构化错误，禁止伪造结果。
- PostgreSQL 、语义索引、镜像和配置均可回滚。
- API 容器重启后可从独立 checkpoint 数据库恢复会话/HITL 状态。

### 2.2 发布目标

- 两个 API 容器实例，Nginx 轮询/失败转移。
- 单个 API 容器故障不影响新请求；单机或磁盘故障不在本阶段 HA 承诺范围内。
- 发布可在 10 分钟内回退到前一个应用镜像/配置/语义版本。
- 所有数据库升级使用 expand-contract，应用回滚不依赖破坏性 schema 逆迁移。

### 2.3 非目标

- 不承诺主机故障时零中断。
- 不承诺多租户数据强隔离。
- 不以自建大模型、微调或 GPU 利用率为交付目标；本阶段只消费 DeepSeek/NVIDIA NIM 等外部 API。
- 不使用 Docker Compose 宣称具备集群级自愈。

---

## 3. 目标架构

```text
Client / TT-API
      |
      v
Nginx :80/:443
  - TLS / request-id / body limit / rate limit
  - SSE no-buffer
  - api-a/api-b failover
      |
      +-----------------------+
      |                       |
      v                       v
FastAPI api-a            FastAPI api-b
      |                       |
      +-----------+-----------+
                  |
                  v
          Typed LangGraph Orchestrator
          - RequestContext
          - Context Compiler
          - Risk Router + Budget
          - Candidate/Verifier/HITL
                  |
          +-------+--------+
          |                |
          v                v
  in-process          in-process
  ModelGateway        QueryGateway
  - alias/profile     - policy
  - stage allowlist   - sqlglot
  - retry/fallback    - EXPLAIN
  - token/cost budget - readonly/timeout/masking
          |                |
          v                v
 DeepSeek / NVIDIA    External Business PostgreSQL
 NIM / future API     (production, read-only)

Shared state:
  postgres-control     -> semantic releases, audit, runs, idempotency
  postgres-checkpoint  -> LangGraph checkpoints, HITL state
  postgres-business    -> local/test profile only

One-shot services:
  migrate / indexer / benchmark / backup-check / restore-test
```

### 3.1 关键边界

1. Nginx 只能访问 API 网络，不能访问任何 PostgreSQL。
2. API 只使用日常账号，不持有迁移/管理凭据。
3. `migrate` 一次性服务是唯一使用 control/checkpoint 管理凭据的应用组件。
4. 业务库使用数据库原生只读角色，SQL Guard 只是第二道防线。
5. 应用不允许绕过 `ModelGateway` 或 `QueryGateway` 直接调用外部模型/数据库。
6. API 启动不自动迁移、不自动重建索引、不自动创建业务视图。
7. 生产 API 通过外部 Docker network/DNS 访问现有业务数据库；SSH 仅用于人工运维，不进入应用数据链路。
8. API 容器不得持有 PostgreSQL superuser、数据库 owner、迁移账号、SSH 私钥或 SSH 密码。
9. `plan` 之外的阶段不得调用 Pro/1M 模型；ModelGateway 必须在发送请求前 fail-closed 校验阶段与模型等级。

---

## 4. Docker Compose 设计

### 4.1 目标文件结构

```text
compose.yaml                    # 公共服务与网络/卷
compose.dev.yaml                # 开发端口、bind mount、单 API
compose.prod.yaml               # 双 API、Nginx、资源限制、restart policy
compose.test.yaml               # 集成测试、故障注入和临时卷
deploy/
  nginx/
    nginx.conf
    conf.d/nl2sql.conf
  postgres/
    control/
    checkpoint/
    business/
  otel/
    collector.yaml
scripts/
  deploy.sh
  rollback.sh
  backup.sh
  restore-test.sh
  smoke.sh
secrets/                       # 只在服务器上存在，被 gitignore
  providers/
    deepseek_api_key           # 实际值，不进 Git/镜像/日志
    nvidia_nim_api_key
  database/
    business_ro_dsn
    control_app_dsn
    checkpoint_app_dsn
    migrator_dsn
  ops/                         # 仅人工运维，绝不挂载到应用容器
    ssh_password
deploy/private/
  ACCESS.local.md              # SSH host/user/port 与应急步骤；只引用 secret 文件
```

### 4.2 服务清单

| 服务 | 数量 | 对外端口 | 持久化 | 说明 |
|---|---:|---|---|---|
| `nginx` | 1 | 80/443 | 日志可选 | 唯一入口，代理 API/SSE |
| `api-a` | 1 | 无 | 无 | FastAPI/LangGraph 实例 A |
| `api-b` | 1 | 无 | 无 | FastAPI/LangGraph 实例 B |
| `postgres-control` | 1 | 无 | `tt_ai_control_pgdata` | 审计、语义版本、query run、outbox、pgvector |
| `postgres-checkpoint` | 1 | 无 | `tt_ai_checkpoint_pgdata` | LangGraph checkpoint/HITL |
| `postgres-business` | 0/1 | 无 | `tt_ai_business_pgdata` | 仅 `local-data/test` profile；生产使用外部业务库 |
| `migrate` | 一次性 | 无 | 无 | Alembic expand-contract 迁移 |
| `indexer` | 一次性 | 无 | 无 | 构建待选语义版本，验证后原子切换 |
| `benchmark` | 一次性 | 无 | 报告目录 | DeepSeek/NVIDIA NIM 真实评测；独立并发与成本预算 |
| `otel-collector` | 0/1 | 无 | 无 | 可选 profile，向现有观测系统转发 |

### 4.3 PostgreSQL 独立卷规则

```yaml
volumes:
  control_pgdata:
    name: tt_ai_control_pgdata
  checkpoint_pgdata:
    name: tt_ai_checkpoint_pgdata
  business_pgdata:
    name: tt_ai_business_pgdata
```

强制规则：

- 一个 PostgreSQL service 只挂载自己的 `PGDATA` 卷。
- 不允许两个 PostgreSQL 容器共享同一数据目录。
- 不允许备份只存在上述三个卷所在磁盘。
- 产品 runbook 禁止使用 `docker compose down -v`。
- 卷名显式固定，不随 Compose project name 变更。
- 升级 PostgreSQL 大版本时使用 dump/restore 或 `pg_upgrade` 流程，禁止直接用新大版本镜像打开旧 `PGDATA`。
- 产品 profile 不启动 `postgres-business`；外部业务库的卷、备份和 PostgreSQL 生命周期由其数据责任方管理，本项目只验证只读连接、schema 契约和恢复后的关键查询。

### 4.4 网络隔离

| 网络 | 成员 | 是否 internal | 用途 |
|---|---|---:|---|
| `edge_net` | Nginx、api-a、api-b | 否 | 南北向 HTTP/SSE |
| `control_net` | API、worker、control/checkpoint PG | 是 | 控制面数据 |
| `data_net` | API、local business PG | 是 | 开发/测试业务 SQL |
| `business_external_net` | API | 由 Infra 管理 | 生产环境访问现有外部业务 PostgreSQL |
| `egress_net` | API、indexer、benchmark | 否 | 外部 LLM/Embedding/Langfuse |

Compose 管理的 PostgreSQL 不发布宿主机端口。开发环境如需调试，只能在 `compose.dev.yaml` 中绑定 `127.0.0.1` 端口。生产 API 通过由 Infra 提供的 external network 或稳定 DNS 访问业务库，不在应用中建立 SSH 隧道。

### 4.5 Secrets

- `.env` 只放非敏感配置和 secret 文件路径。
- 产品密码/API key 使用 Compose `secrets` 挂载为文件，应用支持 `*_FILE` 变量。
- `secrets/` 只位于服务器，权限为最小可读，不进镜像、不进 Git、不输出日志。
- Compose secrets 只是文件挂载，不等于密钥中心；因此保留 `SecretProvider` 接口，后续再接 Vault。
- 根 `.gitignore` 必须忽略 `/secrets/`、`/deploy/private/`、`*.local.md`、`*.secret`；CI 使用 Gitleaks 扫描完整提交历史和 Compose 展开结果。
- `deploy/private/ACCESS.local.md` 可记录 SSH 地址、用户、端口和人工运维流程，但不得包含明文密码，只能引用 `secrets/ops/*`；该目录不得进入 Git、镜像、构建上下文或 CI artifact。
- SSH 凭据不挂载到 `api`、`benchmark`、`migrate`、`indexer` 或 PostgreSQL 容器；部署脚本只接受已建立的运维会话或 SSH agent。
- 当前已通过聊天/临时渠道暴露的 SSH、数据库和 provider 凭据，在正式发布前必须轮换并记录 `secret_version`，但不得记录 secret value。
- 禁止把 secret 作为 Compose 普通 `environment:` 值；仅允许 `*_FILE`，并在容器启动后校验文件权限、非空和未被日志输出。

---

## 5. Nginx 设计

### 5.1 上游和 SSE

```nginx
upstream nl2sql_backend {
    least_conn;
    server api-a:8000 max_fails=3 fail_timeout=10s;
    server api-b:8000 max_fails=3 fail_timeout=10s;
    keepalive 32;
}

server {
    listen 80;
    client_max_body_size 64k;

    location /api/v2/ {
        proxy_pass http://nl2sql_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Request-ID $request_id;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";

        proxy_buffering off;
        proxy_cache off;
        add_header X-Accel-Buffering no;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
```

实际配置还必须包含：

- `/healthz` 和 `/readyz` 独立路由。
- JSON access log，保留 request id、status、upstream address、upstream time，不记录 Authorization 和请求正文。
- 可配置 IP 级 `limit_req`/`limit_conn`；用户级配额在应用内执行。
- 生产 TLS 证书以只读 secret/bind mount 方式提供。
- 删除客户端伪造的身份头；应用只接受 TT-API/JWT 验证后的身份。
- CORS 改为明确 allowlist，禁止 `* + credentials`。

### 5.2 容器失败语义

- API 实例连接失败时，Nginx 可将“还未开始上游处理”的请求转向另一实例。
- 已经开始的 SSE 流无法无损迁移；每个事件必须含 `event_id`，客户端使用 `Last-Event-ID`/查询状态恢复。
- 创建查询使用 `Idempotency-Key`，重连不能生成两个 query run。
- 两个 API 实例共享 checkpoint/control DB，禁止使用进程内存作为生产状态真源。

---

## 6. 应用契约与 API v2

### 6.1 API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v2/nl2sql/queries` | 非流式查询 |
| POST | `/api/v2/nl2sql/queries/stream` | SSE 查询 |
| GET | `/api/v2/nl2sql/threads/{thread_id}` | 会话状态 |
| GET | `/api/v2/nl2sql/threads/{thread_id}/history` | 历史 |
| POST | `/api/v2/nl2sql/threads/{thread_id}/actions` | HITL confirm/modify/cancel |
| POST | `/api/v2/nl2sql/feedback` | 反馈 |
| GET | `/api/v2/nl2sql/capabilities` | 当前模型/检索/CodeAct 能力 |
| GET | `/healthz` | 进程存活 |
| GET | `/readyz` | profile-aware readiness：DB、迁移、semantic release、必要模型能力 |

旧 `/nl2sql/*` 路由返回 `410 Gone` 和迁移说明，不继续维护两套可执行逻辑。

### 6.2 运行模式与能力探测

```text
SERVICE_MODE=infra-dev | product
MODEL_REQUIRED=false | true
```

- `infra-dev + MODEL_REQUIRED=false`：允许在模型故障时验证 API/Nginx/DB/HITL/Policy；模型相关请求返回 `503 MODEL_PROVIDER_UNAVAILABLE`，不得生成假 SQL 或假答案。
- `product + MODEL_REQUIRED=true`：默认模型 profile 无可用 provider 时 `/readyz` 返回非 2xx，Nginx 不向该实例分发新查询。
- `/healthz` 只表示进程存活，不探测外部依赖。
- `/readyz` 返回结构化组件状态，但不返回 Endpoint、模型供应商凭据、DSN 或内部错误栈。
- `/capabilities` 分别报告 model、embedding、semantic release、GraphRAG、HITL、CodeAct 能力及降级原因。
- provider preflight 使用短 timeout 与缓存，禁止每个业务请求都调用 `/models`；模型能力快照写入 release manifest。

### 6.3 单部署域身份模型

```python
class RequestIdentity(BaseModel):
    request_id: UUID
    user_id: str
    roles: frozenset[str]
    permissions: frozenset[str]
    auth_epoch: int | None = None

class RequestContext(BaseModel):
    identity: RequestIdentity
    deployment_scope: Literal["default"] = "default"
    thread_id: UUID
    trace_id: str
    deadline_ms: int
```

约束：

- 不接受客户端提交 `tenant_id`。
- thread 命名空间为 `deployment_scope + user_id + thread_id`。
- 所有 history/state/HITL 路由强制校验 owner，角色管理员越权必须单独权限和审计事件。
- schema 可预留 `tenant_id` nullable/固定值字段，但本版不实现租户路由和 RLS。

### 6.4 核心结构化工件

- `PolicyDecision`：allow/deny/approval、行数上限、时间上限、数据范围。
- `ContextBundle`：semantic version、schema slice、evidence id、token cost、confidence。
- `QueryPlan`：intent、metric、dimension、filter、grain、risk。
- `QueryCandidate`：SQL、fingerprint、validation、cost、score。
- `ExecutionReceipt`：datasource、readonly role、elapsed、row count、plan cost、masking、error taxonomy。
- `AnswerArtifact`：answer blocks、data reference、confidence、citations、degradation flags。
- `ErrorEnvelope`：code、retryable、stage、safe message、trace id。
- `ModelRequest`：stage、alias、messages、tool schema、deadline、token/cost budget、data classification。
- `ModelReceipt`：provider、resolved model、latency、usage、finish reason、retry/fallback、profile version；不保存 secret 和默认不保存完整敏感 prompt。

Agent 节点之间不再使用中文自由文本传递成功/失败状态。

---

## 7. 核心算法与必要代码骨架

### 7.1 Context Compiler

检索组合：词法、向量和关系图结果使用 RRF 合并，只允许 active semantic release 进入上下文。

```python
from collections import defaultdict

RRF_K = 60
SOURCE_WEIGHT = {"lexical": 0.30, "vector": 0.45, "graph": 0.25}

def reciprocal_rank_fusion(ranked: dict[str, list[str]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for source, doc_ids in ranked.items():
        weight = SOURCE_WEIGHT[source]
        for rank, doc_id in enumerate(doc_ids, start=1):
            scores[doc_id] += weight / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
```

上下文预算：

| 路线 | 最大上下文 | 最大证据数 |
|---|---:|---:|
| Fast | 2K tokens | 4 |
| Standard | 6K tokens | 8 |
| Deep | 12K tokens | 16 |

置信度组成：

```text
confidence =
    0.35 * metric_coverage
  + 0.25 * schema_coverage
  + 0.20 * retrieval_agreement
  + 0.10 * approved_example_support
  + 0.10 * (1 - evidence_conflict)
```

`confidence < 0.35` 时不得盲猜 SQL，返回澄清问题。

### 7.2 风险路由

```python
from dataclasses import dataclass
from enum import StrEnum

class Route(StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"
    APPROVAL = "approval"

@dataclass(frozen=True)
class Budget:
    deadline_s: int
    llm_calls: int
    rewrites: int
    sql_attempts: int
    candidates: int
    repairs: int

BUDGETS = {
    Route.FAST: Budget(15, 2, 0, 1, 1, 0),
    Route.STANDARD: Budget(45, 5, 1, 2, 1, 1),
    Route.DEEP: Budget(120, 10, 2, 3, 3, 2),
}

def choose_route(*, risk: int, confidence: float, table_count: int,
                 deep_capacity: bool) -> Route:
    if risk <= 20 and confidence >= 0.85 and table_count <= 1:
        return Route.FAST
    if risk <= 60 and confidence >= 0.55:
        return Route.STANDARD
    if deep_capacity:
        return Route.DEEP
    return Route.APPROVAL
```

风险分：

```text
35 * restricted_data
+ 20 * three_or_more_tables
+ 20 * ambiguous_metric_or_filter
+ 15 * dynamic_calculation
+ 10 * unknown_explain_cost
```

Compose 单机资源门禁：

- Fast 最多 16 个并发。
- Standard 最多 8 个并发。
- Deep 最多 2 个并发。
- SQL 执行全局最多 8 个并发。
- 排队超过 3 秒返回 `CAPACITY_EXCEEDED` 和 `Retry-After`，不允许无界队列。
- 上述参数为首发值，必须在 50 并发压测后再调整。

### 7.3 早停算法

```python
def should_stop(*, remaining_ms: int, reserve_ms: int,
                attempts: int, max_attempts: int,
                sql_fingerprint_seen: int,
                same_error_seen: int,
                policy_denied: bool) -> str | None:
    if policy_denied:
        return "policy_denied"
    if remaining_ms <= reserve_ms:
        return "deadline_reserve"
    if attempts >= max_attempts:
        return "attempt_budget_exhausted"
    if sql_fingerprint_seen >= 2:
        return "repeated_sql"
    if same_error_seen >= 2:
        return "repeated_error"
    return None
```

- 预留总 deadline 的 20% 用于安全结束、持久化回执和响应渲染。
- Policy deny 不重试。
- 只对可重试的网络/429/5xx 错误进行最多两次带 jitter 重试。
- SQL 语法、权限、策略和数据不存在错误不得按网络错误重试。

### 7.4 QueryGateway（本版为进程内模块）

```python
class QueryGateway:
    async def execute(self, ctx: RequestContext, candidate: QueryCandidate) -> ExecutionReceipt:
        decision = await self.policy.pre_sql(ctx, candidate)
        if not decision.allowed:
            raise PolicyDenied(decision.reason)

        parsed = self.sql_parser.parse_one(candidate.sql)
        rewritten = self.rewriter.readonly_with_limit(parsed, decision.max_rows)
        explain = await self.database.explain(rewritten, timeout_ms=3_000)
        self.cost_policy.enforce(explain)

        rows = await self.database.fetch_all(
            rewritten,
            statement_timeout_ms=decision.statement_timeout_ms,
            transaction_read_only=True,
        )
        masked = self.policy.mask_result(ctx, rows)
        return ExecutionReceipt.from_execution(rewritten, explain, masked)
```

强制顺序：

```text
pre-policy
 -> sqlglot parse
 -> single statement/read-only validation
 -> LIMIT rewrite
 -> EXPLAIN with timeout
 -> cost gate
 -> BEGIN READ ONLY + statement/lock timeout
 -> fetch
 -> result size gate/masking
 -> receipt/audit
```

EXPLAIN 失败或超时时 fail-closed，不直接执行原 SQL。

首发连接池（每个 API 实例）：

| 数据库 | pool size | overflow | timeout | recycle |
|---|---:|---:|---:|---:|
| control | 3 | 2 | 3s | 900s |
| checkpoint | 3 | 2 | 3s | 900s |
| business | 3 | 2 | 3s | 900s |

两个 API 实例的单库理论最大连接为 10，保留数据库管理/迁移/监控连接余量。

### 7.5 ModelGateway 与模型分层

所有 provider 实现统一 OpenAI-compatible adapter；业务编排只引用 alias，不引用 provider model ID：

```python
from typing import Literal, Protocol

Stage = Literal["classify", "retrieve", "plan", "generate_sql", "verify", "answer"]
ModelTier = Literal["small", "pro"]

class ModelGateway(Protocol):
    async def invoke(self, request: ModelRequest) -> ModelReceipt: ...

def enforce_model_policy(*, stage: Stage, tier: ModelTier) -> None:
    if tier == "pro" and stage != "plan":
        raise PolicyDenied("pro_model_is_plan_only")
```

当前 model profile：

| Alias | 首选 | 备选 | 允许阶段 | 约束 |
|---|---|---|---|---|
| `fast.default` | `deepseek/deepseek-v4-flash` | `nvidia/<approved-small-model>` | classify/retrieve/generate_sql/verify/answer | 默认非思考或低预算；固定输出上限 |
| `plan.standard` | `deepseek/deepseek-v4-flash` thinking | `nvidia/<approved-small-model>` | plan | 中等预算，仍优先小模型 |
| `plan.pro` | `deepseek/deepseek-v4-pro` | `deepseek/deepseek-v4-flash` thinking | plan only | 只有路由、风险、预算三重门禁允许；不得被其他阶段直接指定 |
| `benchmark.nim` | `nvidia/<candidate-from-models>` | 无 | benchmark only | 由批准清单和 `/models` preflight 解析，不进入生产默认 profile |

Provider 基线：

- DeepSeek OpenAI-compatible base URL 为 `https://api.deepseek.com`，模型 ID 使用 `deepseek-v4-flash`/`deepseek-v4-pro`，禁止继续新增已淘汰 alias。
- NVIDIA 托管 NIM base URL 为 `https://integrate.api.nvidia.com/v1`；具体模型 ID不得写死在编排代码中，由 `NVIDIA_NIM_MODEL_FAST`/model profile 配置，并在 benchmark 前调用 `/models` 或最小探针确认。
- 后续新增模型只实现 provider adapter 与 versioned model profile，不修改 LangGraph 节点和 QueryGateway。
- 模型 fallback 只能在同一数据合规等级、阶段 allowlist 和剩余预算内发生；禁止因 429/5xx 自动升级到 Pro。
- Pro 调用必须记录 `plan_reason`、输入/输出 token、成本估算、触发门禁和 fallback；超长上下文不是默认能力，输入仍受 Context Compiler 预算约束。
- `ModelGateway` 是唯一允许读取 provider secret 的应用模块；所有直接 `get_llm()` 调用必须迁移，CI 架构测试阻止新增绕过点。

### 7.6 候选选择

硬门禁：Policy、parse、schema reference、EXPLAIN 和读只性必须全部通过。

```text
score =
    0.30 * semantic_alignment
  + 0.20 * schema_validity
  + 0.20 * candidate_consensus
  + 0.15 * execution_signal
  + 0.10 * cost_score
  + 0.05 * approved_experience
```

结果共识使用规范化 typed rowset 的 SHA-256，不再对自然语言结果字符串做 MD5。

- Fast：`score >= 0.88`。
- Standard：`score >= 0.82` 且与第二名差值 `>= 0.10`。
- Deep：`score >= 0.78` 且至少 2/3 候选共识。
- 不满足时进入澄清/HITL，不输出伪高置信答案。

---

## 8. 稳定性、降级、早停与回退

### 8.1 故障矩阵

| 故障 | 当前版本行为 | 禁止行为 |
|---|---|---|
| 一个 API 容器失效 | Nginx 转另一容器；SSE 客户端重连 | 丢失 query run 后重复执行 |
| 两个 API 均失效 | Nginx 502/503，监控告警 | 尝试连接数据库返回伪结果 |
| primary LLM 429/5xx/超时 | 按同一预算调用一次已配置 fallback | 无限重试或切换未审批模型 |
| DeepSeek Flash 失效 | 尝试已批准 NVIDIA small alias；无可用 provider 时结构化 503 | 自动升级 DeepSeek Pro |
| Plan Pro 失效/超预算 | 降为 Flash plan 或进入澄清/HITL | 在非 plan 阶段调用 Pro |
| 所有模型 API 失效 | `product` readiness=false；`infra-dev` 保留非模型能力 | 返回伪 SQL/伪答案或无界重试 |
| Embedding 失效 | lexical + graph，标记 degraded | 在无证据时提高置信度 |
| GraphRAG 失效 | lexical + vector | 阻断所有低风险查询 |
| 新语义索引失败 | 保留上一个 active release | 覆盖现网索引指针 |
| Deep 满载 | 低风险降为 Standard，高风险澄清/HITL | 将高风险问题强制降为 Fast |
| control PG 失效 | readiness=false，查询返回 503 | 丢失审计时继续执行 |
| checkpoint PG 失效 | 会话/HITL 路由 503；不新建有状态查询 | 静默切换到本地内存 |
| business PG 失效 | 结构化 `DATA_SOURCE_UNAVAILABLE` | 访问其他数据库或返回缓存假数据 |
| EXPLAIN 失效 | 拒绝执行 | fail-open |
| Langfuse/OTel 失效 | control PG audit/outbox + JSON 日志 | 因可选观测系统阻断低风险查询 |
| CodeAct 请求 | 能用白名单模板则计算，否则 HITL/明确不可用 | 在 API 容器中 `exec` 生成代码 |
| 客户端断开 | 取消下游 LLM/SQL，持久化 cancelled | 后台继续无界运行 |
| ExperienceStore | 产品禁用；接口保留，未来迁移 control PG | 使用 api-a/api-b 各自内存经验影响生产决策 |

### 8.2 CodeAct 收缩策略

```text
CODEACT_MODE=disabled          # 产品默认
CODEACT_MODE=trusted-template  # 只调用已审批计算函数
CODEACT_MODE=unsafe-dev        # 仅本地开发 profile，禁止生产
```

Compose 不使用以下“伪沙箱”设计：

- API 容器挂载 `/var/run/docker.sock`。
- 使用 `privileged: true`。
- 依赖子进程 timeout 宣称完整安全隔离。
- 生成代码可访问业务库、凭据、宿主文件或外网。

### 8.3 进程与容器限制

- 应用镜像非 root，根文件系统只读，`/tmp` 使用 tmpfs。
- `cap_drop: [ALL]`、`no-new-privileges:true`、`pids_limit`。
- API 首发限制：2 CPU / 4GiB/实例。
- indexer/benchmark 不与高峰在线流量同时运行，必须受独立 profile 控制。
- Dockerfile 的 `uv sync --frozen || true` 改为构建失败立即中止。
- Docker 日志配置 `max-size/max-file`，数据库卷、备份盘和容器日志分别设置磁盘水位告警。
- API 设置 `init: true`、`stop_grace_period`、显式 healthcheck/restart policy；所有连接池设置 `pool_timeout/pool_recycle/max_overflow` 并为 migrate/backup 预留连接。
- benchmark 使用独立 provider 并发信号量、单次 token 上限、总成本预算和 dry-run；不与在线流量共享无界配额。

---

## 9. 技术栈

| 类别 | 选型 |
|---|---|
| Runtime | Python 3.13 + `uv` + lockfile |
| API | FastAPI + Pydantic v2 + SSE |
| Agent | LangGraph，显式 StateGraph，不以隐式 supervisor tool loop 作为最终核心 |
| SQL | SQLAlchemy async + asyncpg + sqlglot |
| Database | PostgreSQL 16+；control 库启用 pgvector/FTS |
| Migration | Alembic，expand-contract |
| Edge | Nginx stable，HTTP/SSE reverse proxy |
| Deployment | Docker Engine + Docker Compose v2 |
| Secrets | Compose secret files + `SecretProvider` 抽象 |
| Model providers | DeepSeek V4 Flash/Pro + NVIDIA NIM；OpenAI-compatible adapter + versioned model profile |
| Retrieval | pgvector + PostgreSQL FTS + 现有 GraphRAG |
| Observability | JSON log + OTel；Langfuse 作为可选外部系统 |
| Test | pytest + pytest-asyncio + Hypothesis + testcontainers/Compose test profile |
| Quality | Ruff + Pyright + pip-audit + Trivy + Gitleaks |

本阶段不引入 Redis。幂等、配额、审计 outbox、任务状态和锁使用 control PostgreSQL，避免为单机版额外引入分布式组件。

接口依据：DeepSeek 使用官方 OpenAI-compatible Chat Completions；NVIDIA NIM 使用官方 `/v1/chat/completions`、`/v1/responses`、`/v1/models` 与健康探针。模型 ID、能力和价格可能变化，发布时必须将实际响应的 model ID/profile 版本写入 manifest，禁止依赖文档中的静态名称推断线上能力。

官方规范链接：

- DeepSeek API Quick Start：<https://api-docs.deepseek.com/quick_start/pricing-details-usd/>
- DeepSeek Chat Completions：<https://api-docs.deepseek.com/api/create-chat-completion>
- NVIDIA Hosted NIM LLM API：<https://docs.api.nvidia.com/nim/reference/llm-apis>
- NVIDIA NIM LLM API Reference：<https://docs.nvidia.com/nim/large-language-models/latest/api-reference.html>

---

## 10. 主 PR + 堆叠子 PR 设计

### 10.1 分支策略

- 集成分支：`feature/nl2sql-v2-compose`。
- 子 PR 以集成分支为 target，通过后 squash merge。
- 主 PR：`feature/nl2sql-v2-compose -> main`。
- 禁止在主 PR 中夹带 K8s、多租户或 GPU 实现。
- 租户隔离、只读数据库权限、SQL Policy 和 owner check 不允许被 feature flag 关闭。

### 10.2 依赖图

```text
PR00 -> PR01 -> PR02 -> PR03 -> PR04 -> PR05 -> PR06 -> PR07
                    \                         /
                     +-------> PR08 ---------+
all ----------------------------------------> PR09 -> PR10 -> Master PR
```

### PR00：Runtime/CI 可复现基线

内容：

- Python 3.13 与 `uv.lock` 锁定。
- 多阶段 Dockerfile、非 root、不吞构建错误。
- Ruff、Pyright、pytest、pip-audit、Trivy、Gitleaks、SBOM。
- 镜像包含 `org.opencontainers.image.revision/version` label。
- 根 `.gitignore` 和 Docker `.dockerignore` 强制排除 `secrets/`、`deploy/private/`、`.env*` 与本地 benchmark 原始敏感输出。
- Gitleaks 扫描 Git 历史、工作区和 `docker compose config`；任何 provider/SSH/DB secret 命中立即失败。
- `enable_dynamic_calc` 默认改为 false，产品 profile 对 `unsafe-dev` fail-closed。

DoD：空缓存环境能稳定构建；依赖安装失败必须导致镜像构建失败。

### PR01：v2 契约、用户身份和资源归属

内容：

- 新增核心 Pydantic 工件。
- 直接切换 v2 API，v1 返回 410。
- `RequestIdentity` 贯穿所有子图和工具。
- thread/history/state/HITL 强制 owner check。
- 请求上限：单消息 8KiB，20 条消息，总体 32KiB。

DoD：不同用户即使猜到 thread UUID 也不能读取/恢复状态。

### PR02：Docker Compose、Nginx、生命周期与 Secrets

内容：

- 新增 base/dev/prod/test Compose 文件。
- Nginx 代理、SSE、request id、限流、body limit、安全 header。
- `api-a/api-b` 和健康检查。
- 依赖注入 `AppContainer/app.state`，删除路由注册和核心服务全局单例。
- `SecretProvider(env/file)` 和 `*_FILE` 支持。
- 启动期不执行迁移/索引重建。
- 产品 profile 接入 Infra 管理的 `business_external_net`，不启动本地 business PG，也不建立 SSH 隧道。
- `infra-dev|product` 与 `MODEL_REQUIRED` 的 profile-aware readiness/capabilities。
- API lifespan 不得调用任何 RAG/index sync；删除 `_supervisor`、`_routes_registered` 等生产全局状态。

DoD：一个 API 容器停止后，Nginx 新请求仍成功；SSE 无缓冲；无模型的 infra-dev 可启动，product 缺失必要模型时不进入 readiness。

### PR03：PostgreSQL 拆分、独立卷、迁移与备份

内容：

- control/checkpoint 两个独立 Compose PostgreSQL service/URL/volume；local/test business PG 独立卷；生产业务库为外部 URL/network。
- Alembic schema与凭据分离；新增 business read-only、control app、checkpoint app、migrator、backup 角色。
- API 配置和容器中不得存在 PostgreSQL superuser/owner/admin URL；迁移账号只挂载到一次性 migrate 服务。
- 连接池、`pool_pre_ping`、timeout 和凭据分离。
- `migrate`、`backup`、`restore-test` 一次性任务。
- 旧 checkpoint 升级前导出快照，切换后清理旧数据，备份保留 7 天。

DoD：重建 control/checkpoint/local-business 任一测试容器不改变其他命名卷；API 只读账号在数据库层拒绝写入；control/checkpoint 自动 restore test 和 HITL resume 通过。

### PR04：Policy Engine + 进程内 QueryGateway

内容：

- sqlglot 单语句/只读 AST 验证和 LIMIT 改写。
- EXPLAIN fail-closed、cost/row/timeout 门禁。
- `BEGIN READ ONLY`、statement/lock/idle timeout。
- 结果大小门禁、脱敏、结构化回执与错误分类。
- 所有 NL2SQL、GenData、Dynamic Calc 取数路径必须调用 QueryGateway。

DoD：代码搜索和测试证明不存在绕过 QueryGateway 的应用级 SQL 执行点。

### PR05：Semantic Registry + Context Compiler

内容：

- 语义文档状态：draft/validated/active/retired。
- 版本、checksum、验证报告、变更说明和回退指针。
- PostgreSQL FTS + pgvector + GraphRAG + RRF。
- `indexer` 构建新 release，验证后原子更新 active pointer。
- token/evidence 预算和置信度计算。
- API 缺少 active release 时不临时构建索引；Embedding 失效只能降级到 lexical/graph 并标记 degraded。

DoD：任一索引构建失败不影响现网 active release。

### PR06：显式编排、风险路由、预算与 ModelGateway

内容：

- 使用显式 LangGraph 节点取代隐式 supervisor tool loop 作为 v2 核心。
- Fast/Standard/Deep 风险路由、调用预算、deadline 和早停。
- `ModelGateway` 提供 model alias、primary/fallback、timeout、熔断、token/cost 回执。
- DeepSeek/NVIDIA NIM provider adapter、`/models` preflight、fake provider 和 versioned model profile。
- Flash/small 优先；Pro/1M 只允许 `plan` 阶段，禁止 429/5xx 自动升级 Pro。
- `ENGINE_MODE=shadow|v2`；shadow 最多 5% 流量，只生成 plan/验证，不额外执行业务 SQL。
- 生产禁用内存 ExperienceStore；保留接口，后续如启用必须迁移 control PG。

DoD：路由决策可单测、可记录、可离线重放；任意路线不能超出预算；CI 证明除 ModelGateway/provider adapter 外无直接 `get_llm()` 调用；非 plan 请求指定 Pro 必须 fail-closed。

### PR07：候选、共识、真实 HITL

内容：

- typed candidate/receipt 和 SHA-256 rowset consensus。
- 硬门禁 + 权重评分 + margin 约束。
- LangGraph `interrupt`/`Command` 真实暂停与恢复。
- action 幂等 key、optimistic version、owner check。
- API 实例切换后可从 checkpoint PG 继续。

DoD：approve/modify/reject/cancel 都有端到端测试，重复 action 不重复执行 SQL。

### PR08：动态计算安全收缩

内容：

- `disabled|trusted-template|unsafe-dev` 三模式。
- 建立已审批计算函数注册表，输入/输出 Pydantic 契约。
- 产品 Compose 中不含 Docker socket、privileged 或任意 exec 路径。
- capabilities API 明确返回 CodeAct 不可用的原因。

DoD：生产 profile 下即使配置错误，也不能启动 `unsafe-dev`。

### PR09：观测、benchmark 和发布门禁

内容：

- query/retrieval/candidate/policy/sql/answer 统一 trace schema。
- control PG audit/outbox 是执行 SQL 的基础能力；Langfuse/OTel 只作为可选消费者。
- benchmark 直接消费 typed answer/receipt，不再从自然语言猜 SQL/值。
- benchmark matrix 至少包含 DeepSeek V4 Flash、DeepSeek V4 Pro（plan-only）和一个经 `/models` 验证的 NVIDIA small model。
- smoke 50、enterprise 200~500、BIRD mini、安全、HITL、Nginx/Compose 故障测试；先 smoke/canary，达到成本预算后才扩大。
- McNemar 二元准确率对比；paired bootstrap 延迟/成本区间。
- 每次运行固定 dataset/prompt/policy/semantic/model profile 版本，记录 provider 返回的实际 model ID、token usage、429/5xx、fallback 和总成本。
- benchmark 不保存完整密钥或默认保存原始敏感 prompt/result；报告仅包含脱敏样本、聚合指标和 trace id。

DoD：fake provider/test DB 可完成全流程；使用本地 secret profile 可运行真实 DeepSeek/NVIDIA benchmark；Pro 调用全部可证明来自 plan 阶段且未超预算。

### PR10：发布、回滚、备份与 runbook

内容：

- 镜像不可变 tag/digest、release manifest 和 checksum。
- `deploy.sh`、`rollback.sh`、`backup.sh`、`restore-test.sh`、`smoke.sh`。
- Nginx/api/db/index 升级顺序和故障处置手册。
- 磁盘水位、备份保留、恢复演练和证书轮换手册。
- 回退只使用上一镜像 digest/profile/semantic pointer，不保留可能绕过新安全边界的 legacy engine。

DoD：在预发环境完成一次升级、一次应用回滚、一次 PostgreSQL 恢复演练。

---

## 11. 关键测试设计

### 11.1 单元/属性测试

- `RequestIdentity` 下传、thread namespace 和 owner check。
- RRF 排序、证据去重、token 预算和置信度边界。
- 风险计分、路由阈值、deadline 预留和早停。
- SQL 规范化、fingerprint、typed rowset hash 和候选分数。
- sqlglot fuzz：注释、字符串、双引号标识符、CTE、UNION、嵌套查询、多语句和危险函数。
- Policy/masking/error taxonomy。
- 重复 SQL/重复错误两次必须早停。
- 新增核心模块覆盖率 `>= 90%`，项目总覆盖率 `>= 75%`。

### 11.2 Compose 集成测试

1. 启动 test profile，等待 control/checkpoint/local-business 三个 PostgreSQL healthy；产品 profile 另测外部 business PG 只读接入。
2. 运行 migrate/indexer，确认不在 API 启动时重复执行。
3. 通过 Nginx 访问全部 v2 路由，禁止直连 API 宿主端口。
4. 停止 `api-a`，确认 `api-b` 继续服务。
5. 在 api-a 创建 HITL，停止 api-a，经 api-b 恢复。
6. 断开 Embedding，验证 lexical+graph 降级标记。
7. 构建故障语义版本，验证 active pointer 不变。
8. 停止 checkpoint PG，验证有状态路由 fail-closed。
9. 停止 business PG，验证不会返回假数据/其他数据源。
10. 验证 test profile 的 control/checkpoint/business 三个 volume id 不同；产品 profile 不创建 business volume。
11. control/checkpoint 备份恢复到临时卷，验证 checksum、query run、audit/outbox、thread/HITL resume；外部业务库只执行责任方批准的恢复后关键查询。
12. Nginx 请求大小、限流、SSE 不缓冲和 upstream 失败语义。
13. 模型全部不可用时，infra-dev 返回结构化模型错误但基础路由可用；product readiness 非 2xx。
14. DeepSeek Flash 故障只能降级批准的 NVIDIA small alias；不得自动升级 Pro。

### 11.3 安全测试

- 100% 拦截 INSERT/UPDATE/DELETE/DDL/COPY/多语句和绕过变体。
- 业务只读账号在数据库层直接拒绝写操作。
- API 容器不存在管理 DB 凭据。
- `docker inspect` 中不存在明文 API key/数据库密码环境变量。
- API/benchmark 容器均不存在 SSH 凭据；SSH 只存在于宿主运维权限域。
- Gitleaks 对历史、工作区、Compose 展开配置和 benchmark artifact 无命中。
- 客户端伪造 user/role/tenant header 不生效。
- 生产 profile 没有 Docker socket、privileged、host network 或任意 CodeAct。
- Nginx/API/audit 日志不含 Authorization、secret、原始敏感行集。

### 11.4 负载与稳定性测试

负载模型：Fast/Standard/Deep = 70/25/5，先以 fake provider 做 50 并发 30 分钟基础设施压测；真实 provider 按独立 canary 并发与成本预算逐步提升，禁止直接以账号并发上限压测。

门禁：

- 非用户输入导致的 5xx `< 1%`。
- Fast P95 `<= 8s`，Standard P95 `<= 30s`，Deep P95 `<= 90s`。
- API 容器内存无持续增长，连接池无泄漏。
- 业务 SQL 并发不超过 8，等待队列有界。
- 任一 API 容器在压测中被停止后，新请求可在 10 秒内由另一容器承接。
- 断开客户端后，下游 LLM/SQL 在 5 秒内收到取消。

### 11.5 当前模型 benchmark 设计

分三阶段运行，任一阶段失败立即停止扩大：

1. `preflight`：验证 secret 文件可读、Endpoint TLS、`/models`/最小 completion、结构化输出和 usage 字段；输出只记录 secret fingerprint/version，不记录 value。
2. `smoke`：每个候选模型运行 50 条去敏样例，验证 SQL 解析、工具调用、超时、429/5xx、fallback、Pro stage policy 和成本预算。
3. `evaluation`：通过 smoke 后运行 enterprise 200~500 与 BIRD mini，固定 seed/temperature/prompt/profile；失败样例进入人工分类，不自动反复调用。

当前矩阵：

| Profile | 用途 | 是否可成为默认 | 主要比较指标 |
|---|---|---:|---|
| DeepSeek V4 Flash non-thinking | Fast/生成/回答 | 是 | Execution Accuracy、P95、token、成本 |
| DeepSeek V4 Flash thinking | Standard/Plan | 是 | 复杂查询、JSON/tool 稳定性、延迟 |
| DeepSeek V4 Pro | Plan only | 否，除非后续专项批准 | Plan 质量增益、额外成本、是否值得升级 |
| NVIDIA approved small model | 备选/对照 | 通过门禁后 | 可用性、准确率、fallback 成功率 |

硬约束：

- 单次调用输入、输出、deadline、重试次数、并发和总费用均有上限；达到日预算立即停止。
- benchmark runner 不接受命令行明文 key，只读取 secret file；错误日志不得输出 request headers。
- Pro 请求中 `stage != plan`、缺失 `plan_reason` 或预算不足时，本地策略必须在发网前拒绝。
- 模型比较使用相同 ContextBundle、数据快照、prompt/policy 版本和 SQL 执行门禁。
- benchmark 报告记录不可变运行 ID、Git revision、image digest、dataset checksum、model profile checksum 和 provider 返回的实际 model ID。

### 11.6 准确率与发布门禁

- enterprise 数据集 Execution Accuracy 相对现网非劣不超过 `-1pp`。
- L3/L4 复杂查询目标提升 `>= 3pp`；未达到时不允许默认打开 Deep。
- 危险 SQL 拦截率 100%。
- 非所有者 thread/history/HITL 访问拒绝率 100%。
- 无共识样例必须澄清/HITL，错误高置信输出为 0。
- 备份恢复、应用回滚和语义版本回退必须在预发完成过至少一次。
- 默认模型优先选择满足安全与准确率门禁的最低成本/最低延迟 small/Flash profile；Pro 只以 Plan 增益报告存在，不直接提升为全链路默认。

---

## 12. 发布、回滚和数据保护

### 12.1 发布流程

```text
1. CI 构建/扫描镜像，生成 digest + SBOM
2. 导出 control/checkpoint schema 和数据备份
3. docker compose --profile ops run --rm migrate --phase expand
4. docker compose --profile ops run --rm indexer --build-candidate
5. 对 candidate semantic release 运行 fake-provider 回归 + DeepSeek/NVIDIA model smoke/eval
6. 原子切换 active semantic release
7. 更新 api-a/api-b 镜像，等待 readiness
8. 重载 Nginx
9. 运行 smoke；只有已通过真实模型门禁的 profile 才允许最多 5% shadow
10. 观测 30 分钟后确认发布
```

### 12.2 自动中止条件

任一条命中立即停止扩大流量：

- 出现非所有者会话/数据泄漏。
- 5 分钟窗口 5xx `> 2%`。
- 重复 SQL/重复错误未在第二次早停。
- P95 连续 15 分钟超过路线门禁。
- 危险 SQL 拦截回归任一失败。
- token/请求成本较 baseline 增长 `> 20%` 且准确率无显著改善，或达到 benchmark 日预算。
- audit outbox 积压 `> 10,000` 或最旧事件 `> 15min`。
- control/checkpoint 备份或 restore check 失败。
- 任一产品容器运行于 privileged/挂载 Docker socket。

### 12.3 回滚层级

1. 禁用 Deep/`plan.pro`，只保留 Fast/Standard small profile。
2. `CODEACT_MODE=disabled`。
3. semantic active pointer 切回前一 release。
4. model/prompt profile 切回前一版本；provider 故障时可临时禁用对应 alias，但不得扩大模型权限。
5. Compose 镜像 digest 切回前一 release manifest。
6. 只有数据误写/schema 破坏时才进入数据库恢复；普通应用回滚不回滚数据库。

不保留 `ENGINE_MODE=legacy`。旧 supervisor 链路不能作为产品回退，因为它无法天然证明遵守新的 owner check、ModelGateway、QueryGateway、audit/outbox 和 CodeAct 边界。

不得回滚/关闭的安全约束：owner check、只读数据库账号、SQL Policy/QueryGateway、请求大小限制、生产 CodeAct 禁用、secret 不进镜像。

### 12.4 备份策略

| 对象 | 频率 | 保留 | 验证 |
|---|---|---|---|
| control PG | 每日 + 发布前 | 30 天 | 每周自动 restore test |
| checkpoint PG | 每日 + 升级前 | 7~30 天 | 抽样恢复 thread/HITL |
| external business PG | 由外部数据库责任方按业务 RPO 执行 | 由数据责任人确定 | 本项目验证只读连接和恢复后关键查询，不保存其生产备份 |
| local/test business PG | 按测试数据可重建策略 | 短期 | fixture checksum |
| semantic config/release | 每次发布 | 长期 | checksum + eval report |
| release manifest | 每次发布 | 长期 | image/config/schema digest |

备份输出必须离开 PostgreSQL 命名卷，并同步到另一块磁盘/对象存储。

### 12.5 Release manifest 最小契约

```yaml
release_id: "..."
previous_release_id: "..."
git_revision: "..."
image_digest: "sha256:..."
compose_config_checksum: "..."
control_schema_revision: "..."
checkpoint_schema_revision: "..."
semantic_release_id: "..."
prompt_profile: "..."
model_profile: "..."
model_capability_snapshot_checksum: "..."
policy_version: "..."
feature_flags: {}
secret_versions: {}        # 只记录版本/fingerprint，严禁 value
benchmark_run_ids: []
created_at: "..."
created_by: "..."
```

---

## 13. 开发与运维命令契约

### 13.1 私密访问资料与 secret 初始化（严禁进入 Git）

真实 SSH/数据库/provider 凭据只允许存在于部署服务器的 `secrets/` 和 `deploy/private/`。主 PR、普通 `.env`、Compose YAML、CI variable dump、benchmark 报告和聊天转存文件均不得保存实际值。

`deploy/private/ACCESS.local.md` 只记录以下信息：

- SSH host/user/port、用途、负责人和最近轮换时间；密码只引用 `secrets/ops/ssh_password`。
- 外部业务 PostgreSQL host/database、网络名称和只读角色；密码/DSN 只引用 `secrets/database/business_ro_dsn`。
- DeepSeek/NVIDIA NIM Endpoint、secret 文件路径、key fingerprint/版本和负责人；不得粘贴 key value。
- 应急登录、凭据轮换、撤销和审计步骤。SSH 只供人工运维，应用容器不调用 SSH。

服务器首次初始化必须交互输入，示例不得包含实际值：

```bash
umask 077
install -d -m 700 secrets/providers secrets/database secrets/ops deploy/private

read -rsp "DeepSeek API key: " SECRET_INPUT
printf '%s' "$SECRET_INPUT" > secrets/providers/deepseek_api_key
unset SECRET_INPUT

read -rsp "NVIDIA NIM API key: " SECRET_INPUT
printf '%s' "$SECRET_INPUT" > secrets/providers/nvidia_nim_api_key
unset SECRET_INPUT

chmod 600 secrets/providers/* secrets/database/* secrets/ops/*
```

在写入任何实际值之前，部署脚本必须确认 `/secrets/` 与 `/deploy/private/` 已被 `.gitignore` 和 `.dockerignore` 排除；CI/Gitleaks 必须以故意放置的假 secret canary 验证阻断有效。不得把 SSH 密码转换为应用环境变量，也不得在命令历史中使用 `echo <secret>`。

### 13.2 Compose 与运维命令

```bash
# 开发
docker compose -f compose.yaml -f compose.dev.yaml up --build

# 产品启动
docker compose -f compose.yaml -f compose.prod.yaml up -d --wait

# 迁移（单独执行）
docker compose -f compose.yaml -f compose.prod.yaml --profile ops run --rm migrate

# 索引候选版本
docker compose -f compose.yaml -f compose.prod.yaml --profile ops run --rm indexer

# 发布前评测
docker compose -f compose.yaml -f compose.test.yaml --profile test up \
  --abort-on-container-exit --exit-code-from tests

# 真实模型评测：runner 只读取 *_FILE，不接受命令行 key
docker compose -f compose.yaml -f compose.test.yaml --profile benchmark run --rm benchmark \
  --model-profile current-stage --stage smoke --max-cost-usd "${BENCHMARK_MAX_COST_USD}"

# 备份/恢复演练
./scripts/backup.sh
./scripts/restore-test.sh
```

实现脚本时需满足：

- `set -euo pipefail`。
- 校验 Compose project name、服务器环境和目标卷名。
- 破坏性操作需显式参数和二次确认。
- 不封装/默认执行 `down -v`。
- 每次发布写入 release manifest，不使用漂移的 `latest` 做回滚依据。
- benchmark 运行前检查日预算、数据去敏、provider preflight 和 secret 文件权限；运行后只归档脱敏报告。

---

## 14. 从 Compose 升级到集群的预留点

| 当前实现 | 后续替换 | 本版必须保留的边界 |
|---|---|---|
| Nginx | Gateway API/API Gateway | 统一 v2 HTTP/SSE 契约、request id、幂等 key |
| Compose api-a/api-b | K8s Deployment/HPA | 无本地状态、readiness、graceful shutdown |
| in-process ModelGateway | 独立 LLM Gateway | provider-neutral request/receipt 契约 |
| in-process QueryGateway | 独立 Query Gateway | PolicyDecision/ExecutionReceipt 契约 |
| env/file SecretProvider | Vault | `SecretProvider` 接口和凭据不进 state/trace |
| 固定 deployment scope | 多租户 resolver/RLS | RequestContext 不依赖全局变量 |
| control/checkpoint 独立 PG 卷 + 外部 business PG | 托管 PG/HA 集群 | 独立 URL、角色、migration、backup/restore 责任契约 |
| CodeAct disabled | gVisor/Kata Job | 结构化 sandbox request/result，能力探测 fail-closed |
| 外部 LLM/Embedding | GPU 推理集群 | OpenAI-compatible/provider adapter 与模型别名 |

启动下一阶段集群改造的触发条件：

- 单机 CPU/内存/IO 连续 30 天超过安全水位。
- 必须满足主机故障时业务不中断的 SLA。
- 开始服务多个数据/合规边界不同的客户。
- 单部署域内 API/SQL/LLM 配额已无法满足公平性要求。
- 需要开放任意 CodeAct，并必须有强隔离 RuntimeClass。
- 外部模型费用/合规要求达到 GPU 私有化门槛。

---

## 15. 最终验收清单

### 架构

- [ ] 客户端只能通过 Nginx 访问 API。
- [ ] API 双实例无本地持久状态，可交替处理同一 thread。
- [ ] ModelGateway/QueryGateway 尽管为进程内模块，但拥有独立契约与测试。
- [ ] control/checkpoint 使用不同 service、URL 和 volume；产品 business PG 使用外部只读 URL/network，local/test business PG 才使用独立卷。
- [ ] 模型编排只引用 alias/profile；DeepSeek/NVIDIA/未来 provider 可替换而不修改 LangGraph 节点。

### 稳定性

- [ ] 单 API 容器故障时新请求可继续服务。
- [ ] 所有队列、连接池、路线和重试都有明确上限。
- [ ] 重复 SQL/重复错误两次早停。
- [ ] 外部检索/模型/观测故障符合降级矩阵。
- [ ] 无模型 infra-dev 可验证基础能力；product 缺少必要模型时 readiness fail-closed。
- [ ] ExperienceStore 在产品禁用，不以 api-a/api-b 进程内存影响决策。

### 安全

- [ ] 数据库原生只读角色 + QueryGateway 双重防护。
- [ ] EXPLAIN 和 Policy 失效时 fail-closed。
- [ ] 用户 owner check 覆盖 history/state/HITL。
- [ ] 生产 CodeAct 默认禁用，无 Docker socket/privileged。
- [ ] secrets 不在代码、镜像、Compose 明文环境变量和日志中。
- [ ] SSH 凭据不进入任何应用/任务容器；API 不持有 PostgreSQL superuser/owner/migrator 凭据。
- [ ] Pro/1M 模型仅允许 plan 阶段，其他阶段在发网前 fail-closed。

### 可验证性

- [ ] typed artifacts 可被 benchmark 直接消费。
- [ ] 语义、prompt、model、policy、image 版本进入 trace/release manifest。
- [ ] 准确率、延迟、安全、容器失败、卷隔离和恢复测试全部进入发布门禁。
- [ ] DeepSeek Flash/Pro 与 NVIDIA small 候选完成 preflight/smoke；真实 key 不进入报告，Pro 调用均有 plan_reason 和预算回执。
- [ ] audit/outbox 持久化到 control PG；Langfuse/OTel 失效不丢失基础审计。

### 可回滚性

- [ ] 前一镜像 digest、Compose 配置、semantic release 和 model profile 可恢复；不依赖 legacy engine。
- [ ] 迁移使用 expand-contract，应用回滚不需破坏性数据库逆迁移。
- [ ] 备份不与 PostgreSQL 卷共享唯一故障盘。
- [ ] 预发环境已实际完成 deploy/rollback/restore 演练。

---

## 16. 锁定假设

1. 下一开发阶段继续使用 Docker Compose，不使用 K8s。
2. Nginx 是本阶段唯一转发层，不引入独立网关集群。
3. control/checkpoint PostgreSQL 按职责拆分 Compose 容器和独立命名卷；产品业务库复用现有外部 PostgreSQL，只使用专用只读角色；local/test business PG 使用独立卷。
4. 本版为单部署域，支持多用户 owner check，不支持多租户数据强隔离。
5. 当前真实模型 benchmark 使用 DeepSeek V4 Flash/Pro 与 NVIDIA NIM；默认小模型优先，Pro/1M 只允许 plan；接口保持 OpenAI-compatible/provider-neutral，未来可替换其他模型且不要求本地 GPU。
6. 生产环境任意 CodeAct 默认禁用，只保留已审批模板计算。
7. 保留 LangGraph、PostgreSQL checkpointer、pgvector、Langfuse 可选集成和 OTel 事件模型。
8. 本阶段不开始微调；先完成真实模型 benchmark、路由、策略、失败分类和回放闭环。
9. 本版完成后，再根据 SLA、并发、合规和成本数据决定 K8s、多租户、Vault 和 GPU 集群升级。
10. `infra-dev` 在模型故障时允许基础能力开发；`product` 默认 `MODEL_REQUIRED=true`。
11. 内存 ExperienceStore 在产品禁用；audit/outbox 以 control PG 为真源。
12. SSH 只用于人工运维，所有实际凭据只存在于服务器本地 secret 文件并在正式发布前轮换；主 PR、Git、镜像和 artifact 永不记录明文。
