"""SSH 跳板机 + 内网 PostgreSQL 环境探测（密码仅从环境变量读取，勿写入仓库）。

用法（PowerShell 示例）:
  $env:GYCLOUD_SSH_PASS='...'
  $env:TT_PG_PASS='...'
  uv run python scripts/remote_env_probe.py

依赖: paramiko（已列入 pyproject dev 组）。
"""

import os
import sys

import paramiko


def main() -> None:
    ssh_pass = os.environ.get("GYCLOUD_SSH_PASS")
    pg_pass = os.environ.get("TT_PG_PASS")
    if not ssh_pass or not pg_pass:
        print("请设置环境变量 GYCLOUD_SSH_PASS、TT_PG_PASS", file=sys.stderr)
        sys.exit(1)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        "tunnel.gycloud.net",
        port=60022,
        username="root",
        password=ssh_pass,
        timeout=45,
        banner_timeout=45,
        auth_timeout=45,
    )

    def run(cmd: str, timeout: int = 120) -> str:
        _i, out, err = client.exec_command(cmd, timeout=timeout)
        return (out.read() + err.read()).decode("utf-8", "replace")

    safe_pg = pg_pass.replace("'", "'\\''")
    psql = f"PGPASSWORD='{safe_pg}' psql -h tt-postgres -U postgres -d tt"

    print("=== DB extensions ===")
    print(run(f'{psql} -c "SELECT extname, extversion FROM pg_extension ORDER BY 1;"'))

    print("=== pgvector available? ===")
    print(run(f'{psql} -tAc "SELECT name, default_version FROM pg_available_extensions WHERE name = \'vector\';"'))

    print("=== schemas (sample) ===")
    print(run(f'{psql} -c "SELECT nspname FROM pg_namespace WHERE nspname NOT IN (\'pg_toast\',\'pg_catalog\') ORDER BY 1 LIMIT 40;"'))

    print("=== ai_views table count ===")
    print(run(f'{psql} -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = \'ai_views\';"'))

    print("=== ai_views objects ===")
    q = (
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = 'ai_views' ORDER BY table_type, table_name;"
    )
    print(run(f'{psql} -c "{q}"'))

    print("=== tunnel host: docker / python (app 运行时) ===")
    print(run("command -v docker; docker --version 2>&1; command -v python3; python3 --version 2>&1; command -v uv; uv --version 2>&1"))

    client.close()


if __name__ == "__main__":
    main()
