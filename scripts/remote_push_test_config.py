#!/usr/bin/env python3
"""将 core settings、api lifespan、完整 .env 推到测试机并重启服务。

环境变量:
  GYCLOUD_SSH_PASS, TT_PG_PASS（必填）
  OPENAI_API_KEY（可选，写入远程 .env）
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "bootstrap_test_server", ROOT / "scripts" / "bootstrap_test_server.py"
)
_bootstrap = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_bootstrap)
build_env_file = _bootstrap.build_env_file
ssh_connect = _bootstrap.ssh_connect


def main() -> None:
    if not os.environ.get("GYCLOUD_SSH_PASS") or "TT_PG_PASS" not in os.environ:
        print("请设置 GYCLOUD_SSH_PASS 与 TT_PG_PASS", file=sys.stderr)
        sys.exit(1)

    pw = os.environ["GYCLOUD_SSH_PASS"]
    client = ssh_connect("tunnel.gycloud.net", 60022, pw)
    sftp = client.open_sftp()

    files = [
        (ROOT / "src/core/settings.py", "/opt/tt-ai-main/src/core/settings.py"),
        (ROOT / "src/nl2sql/api.py", "/opt/tt-ai-main/src/nl2sql/api.py"),
    ]
    for local, remote in files:
        if not local.is_file():
            print(f"缺少本地文件: {local}", file=sys.stderr)
            sys.exit(1)
        sftp.put(str(local), remote)
        print(f"OK {remote}")

    env_body = build_env_file()
    with sftp.file("/opt/tt-ai-main/.env", "wb") as wf:
        wf.write(env_body)
    print("OK /opt/tt-ai-main/.env")
    sftp.close()

    client.close()

    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("remote_restart_service.py"))],
        env=os.environ,
        check=False,
    )


if __name__ == "__main__":
    main()
