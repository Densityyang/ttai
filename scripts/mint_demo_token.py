r"""Mint a local demo login session for the TT Admin console (LOCAL DEMO ONLY).

Reads the ADMIN business DSN from a secret FILE and the tt-api signing key from
the gitignored tt-api env file, so no credential is ever written into this
script or into Git. Writes the tokens to .demo_tokens.json (gitignored) and
prints only the profile endpoint status.

Usage:  .venv\Scripts\python.exe scripts\mint_demo_token.py [telephone]
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import jwt

ROOT = Path(__file__).resolve().parents[1]
ADMIN_DSN_FILE = ROOT / "secrets" / "database" / "business_admin_database_url"
TT_API_ENV = ROOT / "tt-intelligent-main" / "tt-api" / ".env.prod"
TOKENS_OUT = ROOT / ".demo_tokens.json"
DEFAULT_TELEPHONE = "13668164299"
TT_API_BASE = "http://127.0.0.1:9000"

_DSN = re.compile(
    r"postgresql\+asyncpg://(?P<user>[^:]+):(?P<pwd>[^@]+)@"
    r"(?P<host>[^:/]+):(?P<port>\d+)/(?P<db>.+)"
)


def _read_env(path: Path, key: str) -> str:
    match = re.search(rf"^{key}=([^\n]+)", path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SystemExit(f"{key} not found in {path}")
    return match.group(1).strip()


async def _mint(telephone: str) -> dict:
    dsn = _DSN.fullmatch(ADMIN_DSN_FILE.read_text(encoding="utf-8").strip())
    if not dsn:
        raise SystemExit("admin DSN is not a postgresql+asyncpg URL")
    parts = dsn.groupdict()
    secret = _read_env(TT_API_ENV, "SECRET_KEY")
    algo = _read_env(TT_API_ENV, "ALGORITHM")
    access_min = int(_read_env(TT_API_ENV, "ACCESS_TOKEN_EXPIRE_MINUTES"))
    refresh_min = int(_read_env(TT_API_ENV, "REFRESH_TOKEN_EXPIRE_MINUTES"))

    conn = await asyncpg.connect(
        host=parts["host"], port=int(parts["port"]), user=parts["user"],
        password=parts["pwd"], database=parts["db"], timeout=15,
    )
    try:
        row = await conn.fetchrow(
            "SELECT id, name, password, is_active FROM vadmin_auth_user "
            "WHERE telephone=$1 ORDER BY id LIMIT 1",
            telephone,
        )
    finally:
        await conn.close()
    if row is None:
        raise SystemExit(f"no admin user with telephone {telephone}")

    now = datetime.now(timezone.utc)
    payload = {"sub": telephone, "password": row["password"]}
    access = jwt.encode(
        {**payload, "is_refresh": False, "exp": now + timedelta(minutes=access_min)},
        secret, algorithm=algo,
    )
    refresh = jwt.encode(
        {**payload, "is_refresh": True, "exp": now + timedelta(minutes=refresh_min)},
        secret, algorithm=algo,
    )
    return {
        "telephone": telephone,
        "name": row["name"],
        "user_id": row["id"],
        "is_active": row["is_active"],
        "token_type": "bearer",
        "access_token": access,
        "refresh_token": refresh,
    }


def _verify(token: str) -> None:
    request = urllib.request.Request(
        f"{TT_API_BASE}/vadmin/auth/user/admin/current/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print("profile ->", response.status, response.read().decode("utf-8")[:160])
    except Exception as exc:  # pragma: no cover - operator feedback
        print("profile -> ERROR", type(exc).__name__, str(exc)[:160])


def main() -> int:
    telephone = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TELEPHONE
    payload = asyncio.run(_mint(telephone))
    TOKENS_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"minted demo session for {payload['name']} (id={payload['user_id']}, active={payload['is_active']})")
    print(f"tokens written to {TOKENS_OUT.name} (gitignored)")
    _verify(payload["access_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
