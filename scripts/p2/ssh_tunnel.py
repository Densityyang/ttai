#!/usr/bin/env python
"""Local port forward to the remote business PostgreSQL through the jump host.

Credentials come from the environment only and are never printed:
    P2_SSH_HOST / P2_SSH_PORT / P2_SSH_USER / P2_SSH_PASSWORD
    P2_DB_HOST / P2_DB_PORT / P2_TUNNEL_PORT

Usage: set the environment, run it, then point the evidence harness at
127.0.0.1:<P2_TUNNEL_PORT>.
"""

from __future__ import annotations

import os
import select
import socket
import sys
import threading

import paramiko

HOST = os.environ["P2_SSH_HOST"]
PORT = int(os.environ.get("P2_SSH_PORT", "60022"))
USER = os.environ.get("P2_SSH_USER", "root")
PASSWORD = os.environ["P2_SSH_PASSWORD"]
DEST_HOST = os.environ.get("P2_DB_HOST", "tt-postgres")
DEST_PORT = int(os.environ.get("P2_DB_PORT", "5432"))
LISTEN_HOST = os.environ.get("P2_TUNNEL_LISTEN", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("P2_TUNNEL_PORT", "15432"))


def pump(left, right) -> None:
    try:
        while True:
            ready, _, _ = select.select([left, right], [], [], 120)
            if not ready:
                continue
            for src in ready:
                dst = right if src is left else left
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    except Exception:
        return


def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    transport = client.get_transport()
    assert transport is not None
    transport.set_keepalive(30)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LISTEN_HOST, LISTEN_PORT))
    listener.listen(32)
    print(f"tunnel ready {LISTEN_HOST}:{LISTEN_PORT} -> {DEST_HOST}:{DEST_PORT} "
          f"via {USER}@{HOST}:{PORT}", flush=True)

    while True:
        conn, addr = listener.accept()
        try:
            chan = transport.open_channel("direct-tcpip", (DEST_HOST, DEST_PORT), addr)
        except Exception as exc:
            print(f"forward failed: {type(exc).__name__}", flush=True)
            conn.close()
            continue
        threading.Thread(target=pump, args=(conn, chan), daemon=True).start()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(0)
