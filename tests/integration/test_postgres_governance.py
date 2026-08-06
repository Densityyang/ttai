from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.nl2sql.semantic.authoring import (
    AuthoringIR,
    JoinDefinition,
    MetricAsset,
    ViewAsset,
    validate_authoring_ir,
)
from src.nl2sql.semantic.materialization import PARSER_VERSION, materialize_authoring_ir
from src.nl2sql.semantic.registry import (
    ControlSemanticReleasePublisher,
    SemanticDocument,
    SemanticReleaseError,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_INTEGRATION = os.environ.get("TTAI_RUN_POSTGRES_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.postgres_integration,
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
        rendered = " ".join(arguments[:2])
        raise AssertionError(
            f"docker command failed ({rendered}):\n{result.stdout}\n{result.stderr}"
        )
    return result


def _mount(source: Path, target: str, *, readonly: bool = True) -> str:
    spec = f"type=bind,source={source.resolve()},target={target}"
    return f"{spec},readonly" if readonly else spec


def _write_secret(directory: Path, name: str, value: str) -> Path:
    path = directory / name
    path.write_text(value, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o644)
    return path


def _wait_for_postgres(container: str, owner: str, database: str) -> None:
    for _ in range(90):
        ready = _docker(
            "exec",
            container,
            "pg_isready",
            "--username",
            owner,
            "--dbname",
            database,
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


def _dsn(role: str, password: str, host: str, port: int, database: str) -> str:
    return (
        f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def _psql(
    stack: dict[str, Any],
    database_key: str,
    role: str,
    password: str,
    sql: str,
    *,
    database: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    item = stack[database_key]
    return _docker(
        "exec",
        "--env",
        f"PGPASSWORD={password}",
        item["container"],
        "psql",
        "--no-psqlrc",
        "--host",
        "127.0.0.1",
        "--username",
        role,
        "--dbname",
        database or item["database"],
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
        check=check,
    )


@pytest.fixture(scope="module")
def postgres_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail(f"Docker daemon is unavailable: {info.stderr.strip()}")

    temp_dir = tmp_path_factory.mktemp("postgres-governance")
    prefix = f"ttai-pr3-{uuid.uuid4().hex[:10]}"
    network = f"{prefix}-net"
    containers: list[str] = []
    stack: dict[str, Any] = {"network": network, "temp_dir": temp_dir}
    stack["app_image"] = f"{prefix}-app:integration"
    _docker("network", "create", network)

    definitions = {
        "business": {
            "image": "postgres:17-alpine",
            "database": "ttai_business",
            "owner": "business_owner",
            "app": "business_reader",
            "privileges": "readonly",
        },
        "control": {
            "image": "pgvector/pgvector:pg17",
            "database": "ttai_control",
            "owner": "control_owner",
            "app": "control_app",
            "migrator": "control_migrator",
            "backup": "control_backup",
            "privileges": "readwrite",
            "extension": "vector pg_trgm",
            "restore_database": "ttai_control_restore_test",
        },
        "checkpoint": {
            "image": "postgres:17-alpine",
            "database": "ttai_checkpoint",
            "owner": "checkpoint_owner",
            "app": "checkpoint_app",
            "migrator": "checkpoint_migrator",
            "backup": "checkpoint_backup",
            "privileges": "readwrite",
            "restore_database": "ttai_checkpoint_restore_test",
        },
    }

    try:
        for key, definition in definitions.items():
            item = dict(definition)
            item["container"] = f"{prefix}-{key}"
            item["owner_password"] = secrets.token_urlsafe(24)
            item["app_password"] = secrets.token_urlsafe(24)
            if item.get("migrator"):
                item["migrator_password"] = secrets.token_urlsafe(24)
                item["backup_password"] = secrets.token_urlsafe(24)

            secret_mounts: list[str] = []
            for secret_name in ("owner_password", "app_password", "migrator_password", "backup_password"):
                if secret_name not in item:
                    continue
                secret_path = _write_secret(temp_dir, f"{key}_{secret_name}", item[secret_name])
                secret_mounts.extend(
                    ["--mount", _mount(secret_path, f"/run/secrets/{secret_name}")]
                )

            environment = [
                "--env",
                f"POSTGRES_DB={item['database']}",
                "--env",
                f"POSTGRES_USER={item['owner']}",
                "--env",
                "POSTGRES_PASSWORD_FILE=/run/secrets/owner_password",
                "--env",
                f"DB_APP_ROLE={item['app']}",
                "--env",
                "DB_APP_PASSWORD_FILE=/run/secrets/app_password",
                "--env",
                f"DB_APP_PRIVILEGES={item['privileges']}",
            ]
            if item.get("migrator"):
                environment.extend(
                    [
                        "--env",
                        f"DB_MIGRATOR_ROLE={item['migrator']}",
                        "--env",
                        "DB_MIGRATOR_PASSWORD_FILE=/run/secrets/migrator_password",
                        "--env",
                        f"DB_BACKUP_ROLE={item['backup']}",
                        "--env",
                        "DB_BACKUP_PASSWORD_FILE=/run/secrets/backup_password",
                        "--env",
                        f"DB_RESTORE_TEST_DATABASE={item['restore_database']}",
                        "--env",
                        f"DB_RESTORE_ROLE={item['migrator']}",
                    ]
                )
            else:
                environment.extend(["--env", f"DB_OBJECT_OWNER_ROLE={item['owner']}"])
            if item.get("extension"):
                environment.extend(["--env", f"DB_REQUIRED_EXTENSION={item['extension']}"])

            _docker(
                "run",
                "--detach",
                "--name",
                item["container"],
                "--network",
                network,
                "--publish",
                "127.0.0.1::5432",
                *environment,
                *secret_mounts,
                "--mount",
                _mount(
                    ROOT / "docker/initdb/roles.sh",
                    "/docker-entrypoint-initdb.d/010-roles.sh",
                ),
                item["image"],
                timeout=120,
            )
            containers.append(item["container"])
            _wait_for_postgres(item["container"], item["owner"], item["database"])
            item["port"] = _published_port(item["container"])
            stack[key] = item
        yield stack
    finally:
        for container in reversed(containers):
            _docker("rm", "--force", "--volumes", container, check=False, timeout=30)
        _docker("network", "rm", network, check=False, timeout=30)
        _docker("image", "rm", "--force", stack["app_image"], check=False, timeout=60)


def _run_migrations(stack: dict[str, Any], temp_dir: Path) -> None:
    control = stack["control"]
    checkpoint = stack["checkpoint"]
    control_url_file = _write_secret(
        temp_dir,
        "control_migrator_url",
        _dsn(
            control["migrator"],
            control["migrator_password"],
            control["container"],
            5432,
            control["database"],
        ),
    )
    checkpoint_url_file = _write_secret(
        temp_dir,
        "checkpoint_migrator_url",
        _dsn(
            checkpoint["migrator"],
            checkpoint["migrator_password"],
            checkpoint["container"],
            5432,
            checkpoint["database"],
        ),
    )
    _docker(
        "build",
        "--file",
        "docker/Dockerfile",
        "--tag",
        stack["app_image"],
        ".",
        timeout=300,
    )
    stack["control_migrator_url_file"] = control_url_file
    stack["checkpoint_migrator_url_file"] = checkpoint_url_file
    _run_migration_container(stack)


def _run_migration_container(stack: dict[str, Any], backup_dir: Path | None = None) -> None:
    snapshot_arguments: list[str] = []
    if backup_dir is not None:
        snapshot_arguments = [
            "--env",
            "CHECKPOINT_SNAPSHOT_REQUIRED=true",
            "--env",
            "CHECKPOINT_SNAPSHOT_FILE=/backups/checkpoint/latest.dump",
            "--mount",
            _mount(backup_dir, "/backups"),
        ]
    _docker(
        "run",
        "--rm",
        "--user",
        "0:0",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "DAC_READ_SEARCH",
        "--security-opt",
        "no-new-privileges",
        "--network",
        stack["network"],
        "--env",
        "CONTROL_MIGRATOR_DATABASE_URL_FILE=/run/secrets/control_url",
        "--env",
        "CHECKPOINT_MIGRATOR_DATABASE_URL_FILE=/run/secrets/checkpoint_url",
        "--mount",
        _mount(stack["control_migrator_url_file"], "/run/secrets/control_url"),
        "--mount",
        _mount(stack["checkpoint_migrator_url_file"], "/run/secrets/checkpoint_url"),
        *snapshot_arguments,
        "--entrypoint",
        "/app/docker/scripts/migrate.sh",
        stack["app_image"],
        "--phase",
        "expand",
        timeout=120,
    )


@pytest.mark.timeout(300)
def test_roles_migrations_backup_restore_and_hitl_resume(
    postgres_stack: dict[str, Any],
) -> None:
    stack = postgres_stack
    temp_dir = Path(stack["temp_dir"])
    business = stack["business"]
    control = stack["control"]
    checkpoint = stack["checkpoint"]

    _psql(
        stack,
        "business",
        business["owner"],
        business["owner_password"],
        "CREATE TABLE orders (id integer PRIMARY KEY, amount integer NOT NULL);"
        "INSERT INTO orders VALUES (1, 42);",
    )
    selected = _psql(
        stack,
        "business",
        business["app"],
        business["app_password"],
        "SELECT amount FROM orders WHERE id = 1;",
    )
    assert selected.stdout.strip() == "42"
    denied_write = _psql(
        stack,
        "business",
        business["app"],
        business["app_password"],
        "SET default_transaction_read_only=off; INSERT INTO orders VALUES (2, 99);",
        check=False,
    )
    assert denied_write.returncode != 0
    assert any(
        message in denied_write.stderr.lower()
        for message in ("read-only transaction", "permission denied")
    )
    insert_privilege = _psql(
        stack,
        "business",
        business["owner"],
        business["owner_password"],
        "SELECT has_table_privilege('business_reader', 'orders', 'INSERT');",
    )
    assert insert_privilege.stdout.strip() == "f"

    _psql(
        stack,
        "control",
        control["owner"],
        control["owner_password"],
        "CREATE TABLE legacy_control_table (id integer PRIMARY KEY);",
    )

    for database_key in ("control", "checkpoint"):
        _docker(
            "exec",
            stack[database_key]["container"],
            "/bin/sh",
            "/docker-entrypoint-initdb.d/010-roles.sh",
            timeout=60,
        )
    legacy_owner = _psql(
        stack,
        "control",
        control["owner"],
        control["owner_password"],
        "SELECT tableowner FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename = 'legacy_control_table';",
    )
    assert legacy_owner.stdout.strip() == "control_migrator"

    _run_migrations(stack, temp_dir)

    app_write = _psql(
        stack,
        "control",
        control["app"],
        control["app_password"],
        "INSERT INTO audit_events "
        "(event_id, trace_id, stage, event_name, occurred_at, attributes) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'trace-pr3', 'query', "
        "'integration', now(), '{}'::jsonb); SELECT count(*) FROM audit_events;",
    )
    assert app_write.stdout.strip().splitlines()[-1] == "1"
    denied_ddl = _psql(
        stack,
        "control",
        control["app"],
        control["app_password"],
        "CREATE TABLE forbidden_by_app(id integer);",
        check=False,
    )
    assert denied_ddl.returncode != 0
    assert "permission denied" in denied_ddl.stderr.lower()

    role_flags = _psql(
        stack,
        "control",
        control["owner"],
        control["owner_password"],
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication "
        "FROM pg_roles WHERE rolname IN ('control_app','control_migrator','control_backup') "
        "ORDER BY rolname;",
    )
    assert all(line.endswith("|f|f|f|f") for line in role_flags.stdout.splitlines() if line)

    semantic_extensions = _psql(
        stack,
        "control",
        control["app"],
        control["app_password"],
        "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm', 'vector') ORDER BY extname;",
    )
    assert semantic_extensions.stdout.strip().splitlines() == ["pg_trgm", "vector"]
    semantic_tables = _psql(
        stack,
        "control",
        control["app"],
        control["app_password"],
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename IN ('semantic_assets', 'semantic_aliases', 'semantic_edges', "
        "'schema_snapshots', 'semantic_validation_issues', 'source_freshness') "
        "ORDER BY tablename;",
    )
    assert semantic_tables.stdout.strip().splitlines() == [
        "schema_snapshots",
        "semantic_aliases",
        "semantic_assets",
        "semantic_edges",
        "semantic_validation_issues",
        "source_freshness",
    ]
    control_app_dsn = _dsn(
        control["app"],
        control["app_password"],
        "127.0.0.1",
        control["port"],
        control["database"],
    )
    _run_async(_exercise_semantic_release_registry(control_app_dsn))

    checkpoint_app_dsn = _dsn(
        checkpoint["app"],
        checkpoint["app_password"],
        "127.0.0.1",
        checkpoint["port"],
        checkpoint["database"],
    )
    saved_config = _run_async(_store_pending_approval(checkpoint_app_dsn))

    backup_dir = temp_dir / "backups"
    backup_dir.mkdir()
    control_backup_url = _write_secret(
        temp_dir,
        "control_backup_url",
        _dsn(
            control["backup"],
            control["backup_password"],
            control["container"],
            5432,
            control["database"],
        ),
    )
    checkpoint_backup_url = _write_secret(
        temp_dir,
        "checkpoint_backup_url",
        _dsn(
            checkpoint["backup"],
            checkpoint["backup_password"],
            checkpoint["container"],
            5432,
            checkpoint["database"],
        ),
    )
    _docker(
        "run",
        "--rm",
        "--network",
        stack["network"],
        "--env",
        "CONTROL_BACKUP_DATABASE_URL_FILE=/run/secrets/control_url",
        "--env",
        "CHECKPOINT_BACKUP_DATABASE_URL_FILE=/run/secrets/checkpoint_url",
        "--env",
        "BACKUP_DIR=/backups",
        "--env",
        "BACKUP_RETENTION_DAYS=7",
        "--mount",
        _mount(ROOT / "docker/scripts/backup.sh", "/scripts/backup.sh"),
        "--mount",
        _mount(control_backup_url, "/run/secrets/control_url"),
        "--mount",
        _mount(checkpoint_backup_url, "/run/secrets/checkpoint_url"),
        "--mount",
        _mount(backup_dir, "/backups", readonly=False),
        "postgres:17-alpine",
        "/bin/sh",
        "/scripts/backup.sh",
        timeout=120,
    )
    for database_name in ("control", "checkpoint"):
        _docker(
            "run",
            "--rm",
            "--mount",
            _mount(backup_dir, "/backups"),
            "postgres:17-alpine",
            "/bin/sh",
            "-ec",
            "cd /backups/$1 && "
            "test -s latest.dump && "
            "test -s latest.dump.sha256 && "
            "sha256sum -c latest.dump.sha256 && "
            "pg_restore --list latest.dump >/dev/null",
            "verify-backup",
            database_name,
            timeout=60,
        )

    _run_migration_container(stack, backup_dir)

    control_restore_url = _write_secret(
        temp_dir,
        "control_restore_url",
        _dsn(
            control["migrator"],
            control["migrator_password"],
            control["container"],
            5432,
            control["restore_database"],
        ),
    )
    checkpoint_restore_url = _write_secret(
        temp_dir,
        "checkpoint_restore_url",
        _dsn(
            checkpoint["migrator"],
            checkpoint["migrator_password"],
            checkpoint["container"],
            5432,
            checkpoint["restore_database"],
        ),
    )
    _docker(
        "run",
        "--rm",
        "--network",
        stack["network"],
        "--env",
        "CONTROL_RESTORE_DATABASE_URL_FILE=/run/secrets/control_url",
        "--env",
        "CHECKPOINT_RESTORE_DATABASE_URL_FILE=/run/secrets/checkpoint_url",
        "--env",
        "BACKUP_DIR=/backups",
        "--mount",
        _mount(ROOT / "docker/scripts/restore-test.sh", "/scripts/restore-test.sh"),
        "--mount",
        _mount(control_restore_url, "/run/secrets/control_url"),
        "--mount",
        _mount(checkpoint_restore_url, "/run/secrets/checkpoint_url"),
        "--mount",
        _mount(backup_dir, "/backups"),
        "postgres:17-alpine",
        "/bin/sh",
        "/scripts/restore-test.sh",
        timeout=120,
    )

    restored_checkpoint_dsn = _dsn(
        checkpoint["app"],
        checkpoint["app_password"],
        "127.0.0.1",
        checkpoint["port"],
        checkpoint["restore_database"],
    )
    assert _run_async(_load_pending_approval(restored_checkpoint_dsn, saved_config)) == {
        "request_id": "approval-pr3",
        "status": "pending",
    }


async def _store_pending_approval(database_url: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "configurable": {"thread_id": "thread-pr3", "checkpoint_ns": ""}
    }
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "approval": {"request_id": "approval-pr3", "status": "pending"}
    }
    checkpoint["channel_versions"] = {"approval": 1}
    metadata = {"source": "input", "step": 0, "parents": {}}
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        return await saver.aput(config, checkpoint, metadata, {"approval": 1})


async def _load_pending_approval(database_url: str, config: dict[str, Any]) -> Any:
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        restored = await saver.aget_tuple(config)
    assert restored is not None
    return restored.checkpoint["channel_values"]["approval"]


async def _exercise_semantic_release_registry(database_url: str) -> None:
    async_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(async_url, pool_size=3, max_overflow=0, pool_pre_ping=True)
    publisher = ControlSemanticReleasePublisher(engine=engine)
    try:
        releases = await asyncio.gather(
            *(
                publisher.publish(
                    [
                        SemanticDocument(
                            document_id=f"concurrent-{index}",
                            content=f"semantic release {index}",
                            metadata={"domain": "complaint"},
                        )
                    ],
                    change_summary=f"concurrent release {index}",
                    validation_report={"ok": True, "candidate": index},
                )
                for index in range(3)
            )
        )
        versions = sorted(release.version for release in releases)
        assert len(set(versions)) == 3
        assert versions == list(range(versions[0], versions[0] + 3))

        active = await publisher.read_active()
        assert active is not None
        assert active.version == versions[-1]
        active_before_failure = active.release_id
        with pytest.raises(SemanticReleaseError, match="validation failed"):
            await publisher.publish(
                [
                    SemanticDocument(
                        document_id="invalid-candidate",
                        content="must never become active",
                        metadata={"domain": "complaint"},
                    )
                ],
                change_summary="invalid release",
                validation_report={"ok": False},
            )
        active_after_failure = await publisher.read_active()
        assert active_after_failure is not None
        assert active_after_failure.release_id == active_before_failure

        rollback_target = min(releases, key=lambda release: release.version)
        rolled_back = await publisher.rollback(rollback_target.release_id)
        assert rolled_back.release_id == rollback_target.release_id
        active_after_rollback = await publisher.read_active()
        assert active_after_rollback is not None
        assert active_after_rollback.release_id == rollback_target.release_id

        ir = AuthoringIR(
            metrics=(
                MetricAsset(
                    asset_id="metric.complaint_count",
                    metric_key="complaint_count",
                    display_name="投诉量",
                    source_relation="public.complaints",
                    aliases=("投诉数量",),
                    source_columns=("id",),
                    domain="complaint",
                    owner="complaint-analytics",
                    sensitivity="internal",
                    freshness_sla_seconds=3600,
                    legacy_metadata_inferred=True,
                ),
            ),
            views=(
                ViewAsset(
                    asset_id="view.complaint_detail",
                    name="complaint_detail",
                    source_relation="public.complaints",
                    source_alias="c",
                    columns=("c.id", "u.name"),
                    joins=(
                        JoinDefinition(
                            table="public.users",
                            alias="u",
                            join_condition="c.user_id = u.id",
                        ),
                    ),
                    domain="complaint",
                    owner="data-platform",
                    sensitivity="internal",
                    freshness_sla_seconds=3600,
                ),
            ),
        )
        report = validate_authoring_ir(
            ir,
            relation_columns={
                "public.complaints": {"id", "user_id"},
                "public.users": {"id", "name"},
            },
        )
        assert report.ok
        typed_release = await publisher.publish_candidate(
            materialize_authoring_ir(ir, report),
            change_summary="typed semantic materialization",
        )
        assert typed_release.parser_version == PARSER_VERSION

        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM semantic_assets
                           WHERE release_id = CAST(:release_id AS uuid)) AS assets,
                          (SELECT count(*) FROM semantic_aliases
                           WHERE release_id = CAST(:release_id AS uuid)) AS aliases,
                          (SELECT count(*) FROM semantic_edges
                           WHERE release_id = CAST(:release_id AS uuid)
                             AND status = 'approved') AS approved_edges,
                          (SELECT count(*) FROM semantic_validation_issues
                           WHERE release_id = CAST(:release_id AS uuid)) AS validation_issues
                        """
                    ),
                    {"release_id": typed_release.release_id},
                )
            ).mappings().one()
            alias_asset_id = (
                await connection.execute(
                    text(
                        """
                        SELECT asset_id
                        FROM semantic_aliases
                        WHERE release_id = CAST(:release_id AS uuid)
                          AND normalized_alias = :normalized_alias
                        """
                    ),
                    {
                        "release_id": typed_release.release_id,
                        "normalized_alias": "投诉量",
                    },
                )
            ).scalar_one()

        assert dict(counts) == {
            "assets": 4,
            "aliases": 8,
            "approved_edges": 3,
            "validation_issues": 1,
        }
        assert alias_asset_id == "metric.complaint_count"
        active_after_typed_publish = await publisher.read_active()
        assert active_after_typed_publish is not None
        assert active_after_typed_publish.release_id == typed_release.release_id
        assert active_after_typed_publish.parser_version == PARSER_VERSION
    finally:
        await publisher.close()


def _run_async(coroutine: Any) -> Any:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)
