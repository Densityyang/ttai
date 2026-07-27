"""tail /tmp/tt-ai.log on test server. Env: GYCLOUD_SSH_PASS"""
import os
import sys

import paramiko


def main() -> None:
    pw = os.environ.get("GYCLOUD_SSH_PASS")
    if not pw:
        sys.exit("GYCLOUD_SSH_PASS")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("tunnel.gycloud.net", port=60022, username="root", password=pw, timeout=45)
    _i, o, e = c.exec_command("tail -100 /tmp/tt-ai.log 2>&1; echo ---PROCS---; ps aux | head -20")
    print((o.read() + e.read()).decode("utf-8", "replace"))
    c.close()


if __name__ == "__main__":
    main()
