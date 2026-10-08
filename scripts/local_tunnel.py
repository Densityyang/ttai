"""Persistent local TCP forward to the remote business PostgreSQL.

Local machine -> SSH jump host -> the remote business PostgreSQL.
Host, port, user and password come from secrets/ssh/tunnel.json.

Credentials live in secrets/ssh/tunnel.json (gitignored).  The process keeps
the local port bound and reconnects with a short backoff, so a dropped SSH
session is repaired without operator action.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "secrets" / "ssh" / "tunnel.json"
LOG_PATH = ROOT / "logs" / "tunnel.log"
RECONNECT_DELAY_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tunnel")


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["local_port"] = int(os.environ.get("TUNNEL_LOCAL_PORT", cfg.get("local_port", 15432)))
    cfg["ssh_port"] = int(cfg.get("ssh_port", 60022))
    cfg["remote_port"] = int(cfg.get("remote_port", 5432))
    return cfg


class ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(socketserver.BaseRequestHandler):
    transport: paramiko.Transport
    remote: tuple

    def handle(self) -> None:
        try:
            channel = self.transport.open_channel(
                "direct-tcpip", self.remote, self.request.getpeername()
            )
        except Exception as exc:
            log.warning("channel open failed: %s", exc)
            return
        if channel is None:
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [], 1)
                if self.request in readable:
                    data = self.request.recv(4096)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(4096)
                    if not data:
                        break
                    self.request.sendall(data)
        except Exception:
            pass
        finally:
            try:
                channel.close()
            except Exception:
                pass
            try:
                self.request.close()
            except Exception:
                pass


def connect(cfg: dict) -> paramiko.Transport:
    sock = socket.create_connection((cfg["ssh_host"], cfg["ssh_port"]), timeout=15)
    transport = paramiko.Transport(sock)
    transport.banner_timeout = 30
    transport.auth_timeout = 30
    transport.set_keepalive(30)
    transport.connect(username=cfg["ssh_user"], password=cfg["ssh_password"])
    return transport


def serve_once(cfg: dict) -> None:
    remote = (cfg["remote_host"], cfg["remote_port"])
    local = (cfg.get("local_bind", "127.0.0.1"), cfg["local_port"])

    log.info("connecting ssh %s@%s:%s", cfg["ssh_user"], cfg["ssh_host"], cfg["ssh_port"])
    transport = connect(cfg)
    log.info("ssh connected; forwarding %s:%s -> %s:%s", *local, *remote)

    Handler.transport = transport
    Handler.remote = remote
    server = ForwardServer(local, Handler)

    alive = threading.Event()

    def watchdog() -> None:
        while transport.is_active():
            time.sleep(2)
        log.warning("ssh transport dropped")
        alive.set()
        server.shutdown()

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close()
        try:
            transport.close()
        except Exception:
            pass


def main() -> int:
    cfg = load_config()
    reconnects = 0
    while True:
        try:
            serve_once(cfg)
        except Exception as exc:
            log.error("tunnel error: %s", exc)
        # No retry ceiling: the demo tunnel must survive indefinitely.
        reconnects += 1
        log.info("reconnect #%d in %ss", reconnects, RECONNECT_DELAY_SECONDS)
        time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
