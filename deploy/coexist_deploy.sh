#!/usr/bin/env bash
# ttai 与 tt-intelligent 共存部署脚本（标准发布路径）
#
# 用法（在后台运行）：
#   cd /home/ai/ttai
#   nohup bash deploy/coexist_deploy.sh > var/deploy-coexist.log 2>&1 &
#
# 决策：本地 registry + digest 发布 + deploy.sh；复用 agent_reader_user；ai_views_ttai 隔离 schema
# 幂等：可重复运行；会重新构建镜像、重新发布视图并滚动升级；LLM key 已填写时不会被覆盖
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p var
LOG="$ROOT_DIR/var/deploy-coexist.log"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 可调参数（可用环境变量覆盖） ----------
TTAI_PORT="${TTAI_PORT:-8000}"
IMAGE_REPOSITORY="${TTAI_IMAGE_REPOSITORY:-127.0.0.1:5000/ttai}"
IMAGE_TAG="${TTAI_IMAGE_TAG:-coexist-rc1}"
BUSINESS_NETWORK="${BUSINESS_EXTERNAL_NETWORK:-tt-intelligent_tt_network}"
TT_API_BASE_URL="${TT_API_BASE_URL:-http://tt-api:9000}"
CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-http://localhost:12080}"
BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-$ROOT_DIR/var/backups}"
TT_INTELLIGENT_DIR="${TT_INTELLIGENT_DIR:-/home/ai/tt-intelligent}"
TT_AI_ENV="${TT_AI_ENV:-$TT_INTELLIGENT_DIR/tt-ai/.env.prod}"
TT_COMPOSE_FILE="${TT_COMPOSE_FILE:-$TT_INTELLIGENT_DIR/docker-compose.yml}"
readonly IMAGE_REF="$IMAGE_REPOSITORY:$IMAGE_TAG"

# ---------- 0. 预检 ----------
step "0. 环境预检"
command -v docker >/dev/null || die "docker 未安装"
docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用"
docker ps --format '{{.Names}}' | grep -qx tt-postgres \
  || die "tt-postgres 容器未运行，请先启动 tt-intelligent"
docker network inspect "$BUSINESS_NETWORK" >/dev/null 2>&1 \
  || die "docker 网络 $BUSINESS_NETWORK 不存在"
if ss -tln | grep -q ":$TTAI_PORT "; then
  die "宿主端口 $TTAI_PORT 已被占用，请设置 TTAI_PORT 换一个端口"
fi
AVAIL_KB=$(df -k --output=avail / | tail -1 | tr -d ' ')
[ "$AVAIL_KB" -gt 10485760 ] || warn "磁盘剩余 $((AVAIL_KB/1024))MB，低于 10GB，构建可能失败"
command -v ollama >/dev/null && ollama list | grep -q mxbai-embed-large \
  || { step "0.1 拉取 embedding 模型"; ollama pull mxbai-embed-large; }

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
[ -x "$PYTHON_BIN" ] && "$PYTHON_BIN" -c 'import yaml' 2>/dev/null || PYTHON_BIN="python3"
$PYTHON_BIN -c 'import yaml' 2>/dev/null || die "python3 缺少 PyYAML，请 pip install pyyaml"
command -v openssl >/dev/null || die "openssl 不可用"

# ---------- 1. secret 准备 ----------
step "1. 准备 secrets"
# secret 文件必须 0644、目录 0700：compose 以 bind-mount 挂载 secret，
# 容器内 init 脚本（postgres 用户）和 api（USER app）均非 root，0600 会导致 Permission denied
mkdir -p secrets/database secrets/providers "$BACKUP_HOST_DIR"
chmod 700 secrets secrets/database secrets/providers

# 1.1 业务库只读连接串：从旧 tt-ai 的 .env.prod 提取 agent_reader_user 连接串并改写主机
[ -f "$TT_AI_ENV" ] || die "找不到 $TT_AI_ENV"
grep -oP 'DATABASE_URL=\Kpostgresql[^ ]+' "$TT_AI_ENV" | head -1 \
  | sed -E 's/@([0-9.]+):5432/@tt-postgres:5432/' \
  > secrets/database/business_ro_database_url
grep -q '^postgresql' secrets/database/business_ro_database_url \
  || die "未能从 $TT_AI_ENV 解析出业务库连接串"
chmod 0644 secrets/database/business_ro_database_url

# 1.2 control/checkpoint 八个角色密码（已存在则不覆盖，保持幂等）
for db in control checkpoint; do
  for role in owner app migrator backup; do
    f="secrets/database/${db}_${role}_password"
    [ -s "$f" ] || openssl rand -base64 24 | tr -d '\n' > "$f"
  done
done

# 1.3 control/checkpoint 六个 DSN（密码从 1.2 的文件内联，不明文输出）
for db in control checkpoint; do
  if [ "$db" = control ]; then DBNAME=ttai_control; else DBNAME=ttai_checkpoint; fi
  printf 'postgresql+asyncpg://%s_app:%s@%s-postgres:5432/%s\n' "$db" "$(cat "secrets/database/${db}_app_password")" "$db" "$DBNAME" \
    > "secrets/database/${db}_app_database_url"
  # migrator 连接串给 alembic env.py 的同步 create_engine 用（psycopg3 风格 connect_args），须为 +psycopg
  printf 'postgresql+psycopg://%s_migrator:%s@%s-postgres:5432/%s\n' "$db" "$(cat "secrets/database/${db}_migrator_password")" "$db" "$DBNAME" \
    > "secrets/database/${db}_migrator_database_url"
  # backup/restore 连接串给 pg_dump/pg_restore/psql（libpq）用，不能带驱动前缀
  printf 'postgresql://%s_backup:%s@%s-postgres:5432/%s\n' "$db" "$(cat "secrets/database/${db}_backup_password")" "$db" "$DBNAME" \
    > "secrets/database/${db}_backup_database_url"
  printf 'postgresql://%s_migrator:%s@%s-postgres:5432/%s_restore_test\n' "$db" "$(cat "secrets/database/${db}_migrator_password")" "$db" "$DBNAME" \
    > "secrets/database/${db}_restore_database_url"
done
chmod 0644 secrets/database/*

# 1.4 LLM provider keys：默认走宿主 ollama（无需真实 key）；如改用云端 DeepSeek/NVIDIA，直接改这两个文件后重跑
for name in deepseek_api_key nvidia_nim_api_key; do
  f="secrets/providers/$name"
  [ -s "$f" ] || echo 'not-needed' > "$f"
done
[ -s secrets/providers/embedding_api_key ] || echo 'not-needed' > secrets/providers/embedding_api_key
chmod 0644 secrets/providers/*

# ---------- 2. 仓库配置 ----------
step "2. 写入共存配置"

# 2.1 ai_views.yaml 保持仓库原样（schema: ai_views，单元测试与上游默认依赖它）；
#     隔离 schema 的转换在第 4 步发布视图时现场进行；运行时查询 schema 由
#     compose.coexist.yml 里的 NL2SQL_DB_SCHEMA=ai_views_ttai 决定，与仓库文件无关

# 2.2 compose 覆盖层
cat > docker/compose.coexist.yml <<'EOF'
# 与 tt-intelligent 共存的覆盖层（deploy/coexist_deploy.sh 生成）：
# - 复用 release 文件中的 external 业务网络（名字由 BUSINESS_EXTERNAL_NETWORK 决定）
# - host.docker.internal 打通宿主机 ollama / Langfuse
# - NL2SQL_DB_SCHEMA 指向隔离 schema ai_views_ttai
# - LLM 走宿主机 ollama 的 OpenAI 兼容端点（deepseek/nvidia 两个 profile 都指向本地模型），
#   MODEL_INPUT_POLICY_APPROVED_DESTINATIONS 是生产模式必需的“已批准模型目标”注入
include:
  - path: compose.release.yml

x-ollama-api: &ollama-api
  extra_hosts: ["host.docker.internal:host-gateway"]
  # typed 运行时用 views 配置的 target_schema 限定关系名；线上视图发布在隔离
  # schema ai_views_ttai（第 4 步生成的 var/ai_views_ttai.deploy.yaml），覆盖镜像
  # 内仓库默认文件（schema: ai_views），否则语义发布的关系资产与运行时对不上
  volumes:
    - ../var/ai_views_ttai.deploy.yaml:/app/configs/semantic/ai_views.yaml:ro
  environment:
    EMBEDDING_BASE_URL: http://host.docker.internal:11434/v1
    EMBEDDING_MODEL: mxbai-embed-large
    NL2SQL_DB_SCHEMA: ai_views_ttai
    DEEPSEEK_BASE_URL: http://host.docker.internal:11434/v1
    DEEPSEEK_FLASH_MODEL: qwen3.8:27b-mtp-q4_K_M
    DEEPSEEK_PRO_MODEL: qwen3.8:27b-mtp-q4_K_M
    NVIDIA_NIM_BASE_URL: http://host.docker.internal:11434/v1
    NVIDIA_NIM_MODEL_FAST: qwen3.8:27b-mtp-q4_K_M
    MODEL_INPUT_POLICY_APPROVED_DESTINATIONS: deepseek:qwen3.8:27b-mtp-q4_K_M,nvidia:qwen3.8:27b-mtp-q4_K_M
    # trusted 激活是 coexist 部署的显式选择：release 默认保持 disabled（见部署契约测试），
    # 这里按部署显式打开 typed runtime 的 trusted backend 授权链
    TYPED_RUNTIME_ACTIVATION: trusted_backend_authorization
    # 本地 ollama 27B 推理延迟高（分析解释单次调用约 47s），放宽 governed 链路与
    # 模型调用时限（默认 4s/10s/30s/30s 必然超时；60s 也会撞上 20% 预留红线）
    V2_REQUEST_DEADLINE_MS: "120000"
    V2_ROUTE_DEADLINE_FAST_MS: "120000"
    V2_ROUTE_DEADLINE_STANDARD_MS: "120000"
    V2_ROUTE_DEADLINE_DEEP_MS: "120000"
    V2_RESERVE_MS: "5000"

services:
  api-a:
    <<: *ollama-api
  api-b:
    <<: *ollama-api
  # backup 容器默认以 root 写备份文件（umask 077），宿主侧 deploy.sh 的 test -s 会权限不足；
  # 以调用者 uid/gid 运行，保证备份产物宿主侧可读
  backup:
    user: "${BACKUP_RUN_USER}"
EOF

# ---------- 3. 本地 registry 与镜像 ----------
step "3. 构建并推送镜像 $IMAGE_REF"
if ! docker ps --format '{{.Names}}' | grep -qx local-registry; then
  if docker ps -a --format '{{.Names}}' | grep -qx local-registry; then
    docker start local-registry >/dev/null
  else
    ss -tln | grep -q ':5000 ' && die "5000 端口被非 local-registry 进程占用"
    docker run -d --restart unless-stopped -p 127.0.0.1:5000:5000 --name local-registry registry:2 >/dev/null
  fi
fi
docker build -f docker/Dockerfile -t "$IMAGE_REF" .
docker push "$IMAGE_REF" >/dev/null
DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "$IMAGE_REF" | cut -d@ -f2)"
printf '镜像 digest: %s\n' "$DIGEST"

# ---------- 4. 向 tt 库发布 ai_views_ttai 视图 ----------
step "4. 发布 ai_views_ttai 视图并授权 agent_reader_user"
# 仓库 ai_views.yaml 保持 schema: ai_views；现场生成隔离 schema 副本用于发布（视图定义与仓库一致）
VIEWS_DEPLOY="$ROOT_DIR/var/ai_views_ttai.deploy.yaml"
sed -E '1,10s/^schema: ai_views$/schema: ai_views_ttai/' configs/semantic/ai_views.yaml > "$VIEWS_DEPLOY"
grep -q '^schema: ai_views_ttai' "$VIEWS_DEPLOY" || die "生成 ai_views_ttai 配置副本失败"
grep -m1 'POSTGRES_PASSWORD' "$TT_COMPOSE_FILE" \
  | sed -E 's/.*POSTGRES_PASSWORD:[[:space:]]*"?([^"]+)"?[[:space:]]*$/POSTGRES_PASSWORD=\1/' > /tmp/tt_pg.env
grep -q '^POSTGRES_PASSWORD=.\+' /tmp/tt_pg.env || die "未能从 $TT_COMPOSE_FILE 解析 POSTGRES_PASSWORD"
chmod 0644 /tmp/tt_pg.env
trap 'rm -f /tmp/tt_pg.env' EXIT

docker run --rm -i --network "$BUSINESS_NETWORK" --env-file /tmp/tt_pg.env \
  -v "$VIEWS_DEPLOY:/app/configs/semantic/ai_views.yaml:ro" "$IMAGE_REF" \
  python - <<'PY'
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from src.nl2sql.infra.store.ai_views import sync_ai_views_from_yaml

dsn = f"postgresql+asyncpg://postgres:{os.environ['POSTGRES_PASSWORD']}@tt-postgres:5432/tt"

async def main() -> None:
    engine = create_async_engine(dsn)
    await sync_ai_views_from_yaml(
        engine=engine,
        config_path="/app/configs/semantic/ai_views.yaml",
        expected_schema="ai_views_ttai",
        grantee_roles=["agent_reader_user"],
    )
    await engine.dispose()

asyncio.run(main())
PY
rm -f /tmp/tt_pg.env
trap - EXIT

docker exec tt-postgres psql -U postgres -d tt -tAc \
  "SELECT count(*) FROM pg_views WHERE schemaname='ai_views_ttai'" \
  | grep -qx '[1-9][0-9]*' || die "ai_views_ttai 视图发布失败"

# ---------- 5. release manifest ----------
step "5. 生成 release manifest"
SEM_CHECKSUM="$(sha256sum configs/semantic/ai_views.yaml configs/semantic/semantic.md configs/semantic/qa.md 2>/dev/null | sha256sum | cut -d' ' -f1)"
RELEASE_ID="coexist-$(date +%Y%m%d%H%M%S)"
MANIFEST="deploy/manifest-$RELEASE_ID.yaml"
GIT_REV="$(git rev-parse HEAD 2>/dev/null || echo unknown)"

$PYTHON_BIN scripts/release_manifest.py create \
  --output "$MANIFEST" \
  --compose-file docker/compose.coexist.yml \
  --release-id "$RELEASE_ID" \
  --previous-release-id "" \
  --git-revision "$GIT_REV" \
  --image-digest "$DIGEST" \
  --control-schema-revision head \
  --checkpoint-schema-revision head \
  --semantic-release-id "$RELEASE_ID" \
  --prompt-profile v1 \
  --model-profile v1 \
  --model-capability-snapshot-checksum "$SEM_CHECKSUM" \
  --policy-version v1 \
  --created-by "$(whoami)@$(hostname)" >/dev/null
printf 'manifest: %s\n' "$MANIFEST"

# ---------- 6. 标准发布 ----------
step "6. 执行 deploy.sh（备份 → 迁移 → 滚动升级 → smoke）"
# 历史备份文件若属主为 root（容器默认 root 写入），deploy.sh 的 test -s 会因权限失败，这里用容器修正
if [ -d "$BACKUP_HOST_DIR/control" ] && [ ! -w "$BACKUP_HOST_DIR/control" ]; then
  warn "备份目录不可写，正在用容器修正属主为 $(id -u):$(id -g)"
  docker run --rm -v "$BACKUP_HOST_DIR":/b postgres:17-alpine \
    chown -R "$(id -u):$(id -g)" /b || die "备份目录属主修正失败"
fi
export DEPLOY_ENVIRONMENT=production
export RELEASE_COMPOSE_FILE="$ROOT_DIR/docker/compose.coexist.yml"
export TTAI_IMAGE_REPOSITORY="$IMAGE_REPOSITORY"
export BACKUP_HOST_DIR
export BACKUP_RUN_USER="$(id -u):$(id -g)"
export TTAI_PORT BUSINESS_EXTERNAL_NETWORK="$BUSINESS_NETWORK" TT_API_BASE_URL CORS_ALLOWED_ORIGINS
export PYTHON_BIN
# 仅用于满足 compose 对 schema-snapshot（ops profile）变量的强制插值；该任务只在显式 --profile ops 时运行
export SCHEMA_SNAPSHOT_SOURCE_IDENTIFIER=business-primary
export SCHEMA_SNAPSHOT_APPROVED_SCHEMAS=ai_views_ttai
bash "$ROOT_DIR/scripts/deploy.sh" "$MANIFEST"

# sudo 运行时，把本次生成的文件属主还原为调用者，方便后续以普通用户维护 secrets
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  chown -R "$SUDO_USER:$SUDO_USER" "$ROOT_DIR/secrets" "$ROOT_DIR/var" \
    "$ROOT_DIR/docker/compose.coexist.yml" "$ROOT_DIR/configs/semantic" 2>/dev/null || true
fi

step "部署完成"
docker ps --filter name=ttai --format '{{.Names}}\t{{.Status}}'
printf '服务入口: http://127.0.0.1:%s\n' "$TTAI_PORT"
