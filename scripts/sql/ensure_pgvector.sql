-- Phase 0/3: 经验记忆等向量能力依赖 pgvector。
-- 当前实例若 `SELECT * FROM pg_available_extensions WHERE name = 'vector'` 无行，
-- 需在 PostgreSQL 所在镜像/宿主机安装 vector 包后重试。
-- 以超级用户执行：
CREATE EXTENSION IF NOT EXISTS vector;
