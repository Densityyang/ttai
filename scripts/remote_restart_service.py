"""重启测试机上的 tt-ai。Env: GYCLOUD_SSH_PASS"""
import os
import sys
from pathlib import Path

import paramiko

PAYLOAD = Path(__file__).with_name("_remote_restart_payload.sh")


def main() -> None:
    pw = os.environ.get("GYCLOUD_SSH_PASS")
    if not pw:
        sys.exit(1)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("tunnel.gycloud.net", port=60022, username="root", password=pw, timeout=45)
    sftp = c.open_sftp()
    body = PAYLOAD.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
    with sftp.file("/tmp/restart_ttai.sh", "wb") as rf:
        rf.write(body)
    sftp.close()
    _i, o, e = c.exec_command("bash /tmp/restart_ttai.sh", timeout=200)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    o.channel.recv_exit_status()
    print(out + err)
    c.close()


if __name__ == "__main__":
    main()
