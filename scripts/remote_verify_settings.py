"""远程验证 Settings 是否读到 SKIP_RAG_STARTUP_SYNC。Env: GYCLOUD_SSH_PASS"""
import os
import sys

import paramiko


def main() -> None:
    pw = os.environ.get("GYCLOUD_SSH_PASS")
    if not pw:
        sys.exit(1)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("tunnel.gycloud.net", port=60022, username="root", password=pw, timeout=45)
    cmd = r"""export PATH=/root/.local/bin:$PATH
cd /opt/tt-ai-main
uv run python -c "from src.core.settings import get_settings; get_settings.cache_clear(); s=get_settings(); print('skip_rag_startup_sync=', s.skip_rag_startup_sync); print('rag_startup_sync_strict=', s.rag_startup_sync_strict)"
"""
    _i, o, e = c.exec_command(cmd, timeout=60)
    print((o.read() + e.read()).decode())
    c.close()


if __name__ == "__main__":
    main()
