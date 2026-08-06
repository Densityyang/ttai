from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_control_and_checkpoint_have_independent_alembic_chains() -> None:
    control_source = _source("docker/alembic/control/versions/001_control_schema.py")
    compile(control_source, "001_control_schema.py", "exec")

    control_ini = _source("docker/alembic/control.ini")
    checkpoint_ini = _source("docker/alembic/checkpoint.ini")
    assert "script_location = %(here)s/control" in control_ini
    assert "script_location = %(here)s/checkpoint" in checkpoint_ini
    assert "prepend_sys_path = %(here)s/../.." in control_ini
    assert "prepend_sys_path = %(here)s/../.." in checkpoint_ini
    assert 'get("CONTROL_MIGRATOR_DATABASE_URL")' in _source("docker/alembic/control/env.py")
    assert 'get("CHECKPOINT_MIGRATOR_DATABASE_URL")' in _source(
        "docker/alembic/checkpoint/env.py"
    )

    control_revisions = sorted((ROOT / "docker/alembic/control/versions").glob("*.py"))
    checkpoint_revisions = sorted((ROOT / "docker/alembic/checkpoint/versions").glob("*.py"))
    assert [path.stem for path in control_revisions] == [
        "001_control_schema",
        "002_semantic_registry",
        "003_audit_outbox",
        "004_semantic_registry_v3",
    ]
    assert [path.stem for path in checkpoint_revisions] == ["001_checkpoint_schema"]


def test_migration_job_is_expand_only_and_requires_checkpoint_snapshot_in_release() -> None:
    migration = _source("docker/scripts/migrate.sh")
    assert 'phase="expand"' in migration
    assert 'if test "$phase" != "expand"' in migration
    assert "checkpoint snapshot checksum mismatch" in migration
    assert "pg_restore --list" in migration
    assert "alembic -c /app/docker/alembic/control.ini upgrade head" in migration
    assert "alembic -c /app/docker/alembic/checkpoint.ini upgrade head" in migration
    assert "src.nl2sql.infra.memory.checkpoint_migrate" in migration
    checkpoint_migrator = _source("src/nl2sql/infra/memory/checkpoint_migrate.py")
    assert "get_settings" not in checkpoint_migrator
    assert "auth_enabled=False" in checkpoint_migrator

    release = yaml.safe_load(_source("docker/compose.release.yml"))
    migrate = release["services"]["migrate"]
    assert migrate["environment"]["CHECKPOINT_SNAPSHOT_REQUIRED"] == "true"
    assert migrate["volumes"][0]["read_only"] is True


def test_role_bootstrap_separates_reader_app_migrator_and_backup() -> None:
    roles = _source("docker/initdb/roles.sh")
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in roles
    assert "default_transaction_read_only = on" in roles
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in roles
    assert "GRANT USAGE, CREATE ON SCHEMA public" in roles
    assert "REVOKE CREATE ON SCHEMA public" in roles
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE" in roles
    assert "ALTER TABLE %I.%I OWNER TO %I" in roles
    assert "DB_RESTORE_TEST_DATABASE must end in _restore_test" in roles


def test_backup_and_restore_cover_both_state_databases_with_integrity_guards() -> None:
    backup = _source("docker/scripts/backup.sh")
    restore = _source("docker/scripts/restore-test.sh")

    for database_name in ("control", "checkpoint"):
        assert f"backup_database {database_name}" in backup
        assert f"restore_database {database_name}" in restore
    assert "BACKUP_RETENTION_DAYS:-7" in backup
    assert "flock --nonblock" in backup
    assert "sha256sum" in backup
    assert "pg_restore --list" in backup
    assert "--exclude-extension=pg_trgm" in backup
    assert "--no-owner" in backup and "--no-privileges" in backup
    assert "backup checksum mismatch" in restore
    assert "--single-transaction" in restore
    assert "semantic_releases" in restore and "checkpoint_migrations" in restore


def test_runtime_image_contains_migration_assets() -> None:
    dockerfile = _source("docker/Dockerfile")
    assert "postgresql-client" in dockerfile
    assert "/build/docker/alembic ./docker/alembic" in dockerfile
    assert "/build/docker/migrations ./docker/migrations" in dockerfile
    assert "--chmod=755 /build/docker/scripts/migrate.sh" in dockerfile
