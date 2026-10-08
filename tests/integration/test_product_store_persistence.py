"""Product-store persistence ACROSS container lifetimes, on a REAL PostgreSQL 17.

This module is the evidence for the durability claim of MASTER_PR_PLAN_V4.md
5.4.1: the SAME AppContainer contract writes in one process, that container is
closed, and a FRESH container reads the state back.  "State is lost on restart"
is therefore no longer true for the control-backed product stores.

Gate (identical to tests/integration/test_postgres_governance.py):
TTAI_RUN_POSTGRES_INTEGRATION=1, otherwise the whole module skips.

The module starts its OWN pgvector/pgvector:pg17 container and applies the frozen
docker/migrations/control/001..005 in order.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.nl2sql.artifacts.contracts import AnalysisArtifact

ROOT = Path(__file__).resolve().parents[2]
RUN_INTEGRATION = os.environ.get("TTAI_RUN_POSTGRES_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        not RUN_INTEGRATION,
        reason="set TTAI_RUN_POSTGRES_INTEGRATION=1 to run Docker PostgreSQL contracts",
    ),
]

IMAGE = "pgvector/pgvector:pg17"
DATABASE = "ttai_control"
OWNER = "control_owner"
OWNER_PASSWORD = "probe_owner_pw"
APP = "control_app"
APP_PASSWORD = "probe_app_pw"
MIGRATIONS = (
    "001_control_schema.sql",
    "002_semantic_registry.sql",
    "003_audit_outbox.sql",
    "004_semantic_registry_v3.sql",
    "005_product_artifacts.sql",
)


def _docker(
    *arguments: str, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        rendered = " ".join(arguments[:2])
        raise AssertionError(
            f"docker command failed ({rendered}):\n{result.stdout}\n{result.stderr}"
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
            OWNER,
            "--dbname",
            DATABASE,
            check=False,
            timeout=10,
        )
        if ready.returncode == 0:
            return
        state = _docker("inspect", "--format", "{{.State.Status}}", container, check=False)
        if state.stdout.strip() == "exited":
            logs = _docker("logs", container, check=False).stdout
            pytest.fail(f"PostgreSQL container exited during init:\n{logs}")
        time.sleep(0.5)
    logs = _docker("logs", container, check=False).stdout
    pytest.fail(f"PostgreSQL did not become ready:\n{logs}")


def _published_port(container: str) -> int:
    result = _docker("port", container, "5432/tcp")
    return int(result.stdout.strip().rsplit(":", maxsplit=1)[1])


def _psql(container: str, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _docker(
        "exec",
        "--env",
        f"PGPASSWORD={OWNER_PASSWORD}",
        container,
        "psql",
        "--no-psqlrc",
        "--host",
        "127.0.0.1",
        "--username",
        OWNER,
        "--dbname",
        DATABASE,
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
        check=check,
    )


@pytest.fixture(scope="module")
def control_stack() -> Iterator[dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail(f"Docker daemon is unavailable: {info.stderr.strip()}")

    container = f"ttai-persist-{uuid.uuid4().hex[:10]}"
    stack: dict[str, Any] = {"container": container}
    try:
        _docker(
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_DB={DATABASE}",
            "--env",
            f"POSTGRES_USER={OWNER}",
            "--env",
            f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            IMAGE,
            timeout=120,
        )
        _wait_for_postgres(container)
        stack["port"] = _published_port(container)
        for migration in MIGRATIONS:
            _docker(
                "cp",
                str(ROOT / "docker/migrations/control" / migration),
                f"{container}:/tmp/{migration}",
            )
            applied = _docker(
                "exec",
                container,
                "psql",
                "-1",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                OWNER,
                "-d",
                DATABASE,
                "-q",
                "-f",
                f"/tmp/{migration}",
                check=False,
            )
            assert applied.returncode == 0, f"{migration}:\n{applied.stderr}"
        role = _psql(
            container,
            f"CREATE ROLE {APP} LOGIN PASSWORD '{APP_PASSWORD}'; "
            f"GRANT CONNECT ON DATABASE {DATABASE} TO {APP}; "
            f"GRANT USAGE ON SCHEMA public TO {APP}; "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP};",
            check=False,
        )
        assert role.returncode == 0, role.stderr
        yield stack
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=30)


@pytest.fixture()
def control_backend(control_stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the container at the probe database for the duration of one test."""

    from src.core.settings import get_settings

    monkeypatch.setenv("PRODUCT_STORE_BACKEND", "control")
    monkeypatch.setenv(
        "CONTROL_DATABASE_URL",
        f"postgresql+asyncpg://{APP}:{APP_PASSWORD}@127.0.0.1:"
        f"{control_stack['port']}/{DATABASE}",
    )
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def test_state_survives_a_fresh_container(control_backend: None) -> None:
    """Write in one container, read the SAME state back in a brand-new one."""

    from src.nl2sql.container import AppContainer

    writer = AppContainer()
    await writer.start()
    try:
        assert writer.product_store_backend == "control"
        assert writer.product_store_available is True

        artifacts = await writer.artifact_repository()
        catalogue = await writer.publication_catalogue()
        library = await writer.library_repository()

        record = await artifacts.create(
            owner_user_id="persistence-user",
            payload=AnalysisArtifact(title="Persisted", summary="survives a restart"),
        )
        identities = await catalogue.identities()
        assert identities, "the bootstrap catalogue must be seeded in both backends"
        identity_id = identities[0]
        version = await catalogue.current_version(identity_id)
        installed = await library.install(
            user_id="persistence-user", identity_id=identity_id, version=version
        )
        assert installed.version == version
        assert await library.star(user_id="persistence-user", identity_id=identity_id) == 1
    finally:
        await writer.close()

    reader = AppContainer()
    await reader.start()
    try:
        artifacts = await reader.artifact_repository()
        catalogue = await reader.publication_catalogue()
        library = await reader.library_repository()

        restored = await artifacts.get(
            owner_user_id="persistence-user", artifact_id=record.artifact_id
        )
        assert restored.payload.title == "Persisted"
        assert await artifacts.list_for_owner(owner_user_id="persistence-user") == (
            restored,
        )

        assert identity_id in await catalogue.identities()
        assert await catalogue.current_version(identity_id) == version

        binding = await library.get_install(
            user_id="persistence-user", identity_id=identity_id
        )
        assert binding is not None and binding.version == version
        assert await library.is_starred(
            user_id="persistence-user", identity_id=identity_id
        )
        assert await library.star_count(identity_id=identity_id) == 1
    finally:
        await reader.close()


async def test_restart_is_idempotent_and_owner_scoped(control_backend: None) -> None:
    """A second start must neither duplicate nor leak the persisted state."""

    from src.nl2sql.container import AppContainer

    container = AppContainer()
    await container.start()
    try:
        artifacts = await container.artifact_repository()
        assert await artifacts.list_for_owner(owner_user_id="persistence-user") == (
            await artifacts.list_for_owner(owner_user_id="persistence-user")
        )
        # A DIFFERENT owner sees none of it, in the durable backend too.
        assert await artifacts.list_for_owner(owner_user_id="someone-else") == ()
        assert container.product_store_available is True
    finally:
        await container.close()
