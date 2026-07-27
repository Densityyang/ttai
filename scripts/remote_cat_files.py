"""调试：查看远程 api.py 片段与 .env。Env: GYCLOUD_SSH_PASS"""
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
    _i, o, e = c.exec_command(
        "grep -n skip_rag /opt/tt-ai-main/src/nl2sql/api.py | head -5; "
        "echo ---ENV---; grep SKIP /opt/tt-ai-main/.env; "
        "wc -l /opt/tt-ai-main/src/nl2sql/api.py",
        timeout=30,
    )
    print((o.read() + e.read()).decode())
    c.close()


if __name__ == "__main__":
    main()
