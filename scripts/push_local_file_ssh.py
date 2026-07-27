"""SFTP 上传单个文件到测试机并可选执行远程命令。Env: GYCLOUD_SSH_PASS"""
import os
import sys
from pathlib import Path

import paramiko


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: push_local_file_ssh.py <本地路径> <远程路径> [远程bash命令]")
        sys.exit(2)
    local = Path(sys.argv[1])
    remote = sys.argv[2]
    cmd = sys.argv[3] if len(sys.argv) > 3 else None
    pw = os.environ.get("GYCLOUD_SSH_PASS")
    if not pw or not local.is_file():
        sys.exit("GYCLOUD_SSH_PASS 或本地文件无效")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("tunnel.gycloud.net", port=60022, username="root", password=pw, timeout=45)
    sftp = c.open_sftp()
    sftp.put(str(local), remote)
    sftp.close()
    print(f"uploaded -> {remote}")
    if cmd:
        _i, o, e = c.exec_command(cmd, timeout=120)
        print((o.read() + e.read()).decode("utf-8", "replace"))
    c.close()


if __name__ == "__main__":
    main()
