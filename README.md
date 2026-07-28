# TT-AI

可治理的 NL2SQL Agent 服务：把自然语言问题转换为可审计、可验证、可回滚的 SQL 查询与结果。

> 当前定位：Docker Compose 单机/少节点交付版。代码和自动化 CI 已覆盖核心契约；正式上线前仍需在预发环境完成升级、应用回滚和 PostgreSQL 恢复演练。

## 能力概览

- v2 NL2SQL API，支持普通查询、SSE 流式查询、会话历史、HITL action、反馈和能力探测。
- 显式 LangGraph 编排，包含上下文编译、风险路由、预算、截止时间和早停。
- `ModelGateway` 统一模型别名、provider profile、超时、fallback、token/cost receipt 和能力快照。
- `QueryGateway` 统一 SQL policy、只读事务、EXPLAIN、行数/成本/超时门禁、脱敏和执行回执。
- Semantic Registry 管理语义版本、checksum、验证状态、active pointer 和安全回退。
- Typed candidate/consensus 与真实 HITL pause/resume，状态保存在 PostgreSQL checkpoint store。
- 持久化 control audit/outbox、typed benchmark receipts、发布 manifest 和不可变镜像 digest。
- 生产模式默认禁用 CodeAct 和任意动态代码执行；动态计算只允许已批准模板。

## 架构边界

```text
Client
  │
  ▼
Nginx（公开端口可配置） ← 唯一外部入口，负责 request-id、SSE 和限流
  │
  ├── api-a ─┐
  └── api-b ─┴── v2 LangGraph orchestration
                    ├── ModelGateway → DeepSeek / NVIDIA NIM / compatible provider
                    └── QueryGateway → external business PostgreSQL (read-only)

control PostgreSQL    semantic releases, audit/outbox, idempotency
checkpoint PostgreSQL LangGraph checkpoints and HITL state
```

本阶段是单部署域：支持多用户资源归属校验，但不承诺多租户强隔离；不包含 Kubernetes、GPU 集群、Vault 或任意 CodeAct 运行时。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v2/nl2sql/queries` | 非流式查询 |
| POST | `/api/v2/nl2sql/queries/stream` | SSE 查询 |
| GET | `/api/v2/nl2sql/threads/{thread_id}` | 会话状态 |
| GET | `/api/v2/nl2sql/threads/{thread_id}/history` | 会话历史 |
| POST | `/api/v2/nl2sql/threads/{thread_id}/actions` | approve/modify/reject/cancel |
| POST | `/api/v2/nl2sql/feedback` | 反馈 |
| GET | `/api/v2/nl2sql/capabilities` | 当前能力和降级原因 |
| GET | `/healthz` | 进程存活检查 |
| GET | `/readyz` | profile-aware readiness |

旧版 `/nl2sql/*` 路由不再执行旧逻辑，而是返回迁移提示。

## 本地开发

要求：Python 3.13、[uv](https://docs.astral.sh/uv/)。

```bash
uv sync --all-groups

# 运行质量门禁
uv run ruff check src tests
uv run pyright
uv run pytest
uv run pip-audit
```

复制 `.env.example` 作为开发配置时，只能填入本地或测试凭据。生产凭据必须使用 Compose secret 文件和 `*_FILE` 配置，不能写入 Git、镜像或日志。

开发 Compose 配置位于 `docker/docker-compose.yml` 和 `docker/compose.dev.yml`。它包含独立的 control、checkpoint 和 local business PostgreSQL；生产配置不启动 local business PostgreSQL，而是连接基础设施提供的外部只读业务库。

## 发布与回滚

发布入口和故障处置见 [`deploy/runbook.md`](deploy/runbook.md)。发布流程使用：

- `scripts/release_manifest.py`：创建和校验 release manifest、镜像 digest 与 Compose checksum；
- `scripts/deploy.sh`：迁移、可选语义索引、双 API、Nginx 和 smoke；
- `scripts/rollback.sh`：只允许回退到当前 manifest 记录的上一版本；
- `scripts/backup.sh` / `scripts/restore-test.sh`：备份和隔离恢复演练；
- `scripts/smoke.sh`：通过 Nginx 检查 `/healthz` 和 `/readyz`。

生产发布必须使用不可变 `repository@sha256:<digest>`，不得使用 `latest`；不得使用 `docker compose down -v` 作为常规运维手段。

## 目录说明

```text
src/                         应用、编排、ModelGateway、QueryGateway 和语义层
configs/semantic/            运行时语义、问答样例和 AI view 配置
benchmarks/                  typed benchmark runner、数据集和指标计算
docker/                      Dockerfile、Compose、迁移和一次性运维任务
deploy/                      release manifest 示例和发布/恢复 runbook
scripts/                     发布、备份、恢复、smoke 和 benchmark 工具
tests/                       单元测试和部署契约测试
```

## 安全边界

- API 只使用应用数据库账号；迁移、备份和恢复账号只挂载到对应的一次性任务。
- 业务数据库使用只读连接，SQL 仍必须通过 `QueryGateway` 和 policy 门禁。
- 生产 CodeAct 默认关闭，无 Docker socket、`privileged` 或任意 exec 路径。
- secret 只记录版本或 fingerprint，不记录 secret value。
- 外部模型、Embedding 和观测系统不可用时，系统按降级矩阵返回结构化错误，不伪造 SQL 或答案。

## 项目文档

运行时语义文档、benchmark 数据契约和部署 runbook 属于项目交付物。历史计划、阶段性审查报告和个人运维脚本不属于产品运行时，详见维护者的仓库清理清单。
