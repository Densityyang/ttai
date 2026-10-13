"""PG17 execution evidence for durable, owner-scoped HITL action claims."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.nl2sql.hitl_action_claim_control_store import ControlHITLActionClaimStore
from src.nl2sql.hitl_action_claims import (
    HITLActionClaimInput,
    HITLActionResponseSummary,
    hitl_action_payload_checksum,
    hitl_idempotency_key_checksum,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_INTEGRATION = os.environ.get("TTAI_RUN_POSTGRES_INTEGRATION") == "1"
CONTROL_IMAGE = "pgvector/pgvector:pg17"
CONTROL_DATABASE = "ttai_control"
CONTROL_OWNER = "control_owner"
CONTROL_OWNER_PASSWORD = "probe_owner_pw"
CONTROL_APP = "control_app"
CONTROL_APP_PASSWORD = "probe_app_pw"
MIGRATION = "009_hitl_action_claims.sql"

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        not RUN_INTEGRATION,
        reason="set TTAI_RUN_POSTGRES_INTEGRATION=1 to run Docker PostgreSQL contracts",
    ),
]


def _docker(*arguments: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker command failed ({' '.join(arguments[:2])}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _wait_for_postgres(container: str) -> None:
    for _ in range(90):
        ready = _docker(
            "exec",
            container,
            "pg_isready",
            "--host",
            "127.0.0.1",
            "--username",
            CONTROL_OWNER,
            "--dbname",
            CONTROL_DATABASE,
            check=False,
            timeout=10,
        )
        if ready.returncode == 0:
            return
        state = _docker("inspect", "--format", "{{.State.Status}}", container, check=False)
        if state.stdout.strip() == "exited":
            pytest.fail("PostgreSQL fixture exited during initialization")
        time.sleep(0.5)
    pytest.fail("PostgreSQL fixture did not become ready")


def _published_port(container: str) -> int:
    result = _docker("port", container, "5432/tcp")
    return int(result.stdout.strip().rsplit(":", maxsplit=1)[1])


def _psql(container: str, sql: str) -> str:
    result = _docker(
        "exec",
        "--env",
        f"PGPASSWORD={CONTROL_APP_PASSWORD}",
        container,
        "psql",
        "--no-psqlrc",
        "--host",
        "127.0.0.1",
        "--username",
        CONTROL_APP,
        "--dbname",
        CONTROL_DATABASE,
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def control_postgres() -> Iterator[dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail("Docker daemon is unavailable for HITL claim integration test")

    container = f"ttai-hitl-claims-{uuid.uuid4().hex[:10]}"
    _docker(
        "run",
        "--detach",
        "--name",
        container,
        "--env",
        f"POSTGRES_DB={CONTROL_DATABASE}",
        "--env",
        f"POSTGRES_USER={CONTROL_OWNER}",
        "--env",
        f"POSTGRES_PASSWORD={CONTROL_OWNER_PASSWORD}",
        "--publish",
        "127.0.0.1::5432",
        CONTROL_IMAGE,
        timeout=180,
    )
    stack: dict[str, Any] = {"container": container}
    try:
        _wait_for_postgres(container)
        _docker(
            "exec",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            CONTROL_OWNER,
            "-d",
            CONTROL_DATABASE,
            "--command",
            "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());",
        )
        migration_path = ROOT / "docker/migrations/control" / MIGRATION
        _docker("cp", str(migration_path), f"{container}:/tmp/{MIGRATION}")
        _docker(
            "exec",
            container,
            "psql",
            "-1",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            CONTROL_OWNER,
            "-d",
            CONTROL_DATABASE,
            "-q",
            "-f",
            f"/tmp/{MIGRATION}",
            timeout=120,
        )
        port = _published_port(container)
        _docker(
            "exec",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            CONTROL_OWNER,
            "-d",
            CONTROL_DATABASE,
            "--command",
            f"CREATE ROLE {CONTROL_APP} LOGIN PASSWORD '{CONTROL_APP_PASSWORD}'; "
            f"GRANT CONNECT ON DATABASE {CONTROL_DATABASE} TO {CONTROL_APP}; "
            f"GRANT USAGE ON SCHEMA public TO {CONTROL_APP}; "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {CONTROL_APP};",
        )
        stack["port"] = port
        stack["dsn"] = (
            f"postgresql+asyncpg://{CONTROL_APP}:{CONTROL_APP_PASSWORD}"
            f"@127.0.0.1:{port}/{CONTROL_DATABASE}"
        )
        yield stack
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=60)


def _claim(
    *,
    owner: str = "alice",
    thread: UUID | None = None,
    version: int = 3,
    key: str = "approved-once",
    feedback: str = "raw-feedback-must-not-be-stored",
    decision_value: str = "raw-decision-payload-must-not-be-stored",
) -> HITLActionClaimInput:
    return HITLActionClaimInput(
        owner_user_id=owner,
        thread_id=thread or UUID("11111111-1111-1111-1111-111111111111"),
        pending_version=version,
        idempotency_key_sha256=hitl_idempotency_key_checksum(key),
        payload_sha256=hitl_action_payload_checksum(
            {
                "action": "modify",
                "feedback": feedback,
                "decision_value": decision_value,
                "version": version,
            }
        ),
    )


def _response() -> HITLActionResponseSummary:
    return HITLActionResponseSummary(
        target_type="typed_decision",
        status="recorded",
        version=3,
        decision_status="resolved_pending_revalidation",
        continuation_status="current_authorization_denied",
    )


async def test_control_claims_are_atomic_durable_private_and_owner_scoped(
    control_postgres: dict[str, Any],
) -> None:
    dsn = str(control_postgres["dsn"])
    first = ControlHITLActionClaimStore(database_url=dsn)
    second = ControlHITLActionClaimStore(database_url=dsn)
    fresh = ControlHITLActionClaimStore(database_url=dsn)
    claim = _claim()
    try:
        await first.ping()
        outcomes = await asyncio.gather(
            first.claim(claim=claim), second.claim(claim=claim)
        )
        assert sorted(result.outcome for result in outcomes) == [
            "claimed",
            "in_progress",
        ]
        claimant = first if outcomes[0].outcome == "claimed" else second
        completer = second if claimant is first else first

        # An independent store instance sees the unfinished row after the
        # original instance is disposed, modeling a service/process restart.
        await claimant.close()
        pending = await fresh.lookup(claim=claim)
        assert pending is not None and pending.outcome == "in_progress"

        await completer.complete(claim=claim, response=_response())
        await completer.close()
        replay = await fresh.lookup(claim=claim)
        assert replay is not None and replay.outcome == "replay"
        assert replay.response == _response()

        assert (
            await fresh.lookup(claim=_claim(key="another-key"))
        ).outcome == "conflict"
        assert (
            await fresh.lookup(claim=_claim(feedback="different feedback"))
        ).outcome == "conflict"
        assert (
            await fresh.claim(claim=_claim(owner="bob"))
        ).outcome == "claimed"
        legacy_claim = _claim(version=4, key="legacy-approved")
        assert (await fresh.claim(claim=legacy_claim)).outcome == "claimed"
        legacy_response = HITLActionResponseSummary(
            status="approved", version=5
        )
        await fresh.complete(claim=legacy_claim, response=legacy_response)
        legacy_replay = await fresh.lookup(claim=legacy_claim)
        assert legacy_replay is not None
        assert legacy_replay.outcome == "replay"
        assert legacy_replay.response == legacy_response

        stored_projection = _psql(
            str(control_postgres["container"]),
            "SELECT idempotency_key_sha256 || '|' || payload_sha256 || '|' || "
            "response_summary::text FROM hitl_action_claims "
            "WHERE owner_user_id = 'alice' AND pending_version = 3",
        )
        assert "approved-once" not in stored_projection
        assert "raw-feedback-must-not-be-stored" not in stored_projection
        assert "raw-decision-payload-must-not-be-stored" not in stored_projection
        assert "slot_bindings" not in stored_projection
    finally:
        await first.close()
        await second.close()
        await fresh.close()
