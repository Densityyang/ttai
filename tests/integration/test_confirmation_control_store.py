"""Docker PostgreSQL contract for the two durable confirmation records.

The records are gated exactly like the other control-plane contracts: nothing runs
unless TTAI_RUN_POSTGRES_INTEGRATION=1, and the container is created, migrated
with 001..007 and torn down by this module alone.

What is proven here, at execution level:

* a definition confirmation recorded in one container is read back by a FRESH
  container, FIELD BY FIELD, so "who confirmed this exact version" survives a
  restart;
* a run-scoped exploration confirmation survives the same restart;
* the process-local implementations LOSE both records across instances, which is
  the gap the durable implementations close;
* every documented invariant is also a DATABASE-level guarantee: a direct SQL
  writer cannot rewrite or delete an audit record, cannot confirm a version that
  does not exist (or whose checksum does not match), cannot omit the actor or the
  time, cannot store replaces_definition_confirmation = TRUE, and cannot reuse a
  (run_id, exploration_id) key;
* the migration is idempotent;
* the in-memory and control implementations are differentially equivalent over
  the same operations;
* a corrupted stored payload is the ONE typed integrity error, never a bare
  pydantic ValidationError;
* the confirmation stores are part of the startup ping/readiness gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.nl2sql.artifacts.confirmation_control_store import (
    ConfirmationStoreIntegrityError,
    ControlConfirmationAuditStore,
    ControlExplorationConfirmationStore,
)
from src.nl2sql.artifacts.definition_confirmation_audit import (
    ConfirmationRecord,
    InMemoryConfirmationAuditStore,
)
from src.nl2sql.artifacts.exploration_confirmation import (
    ExplorationConfirmation,
    ExplorationDefinitionReference,
    InMemoryExplorationConfirmationStore,
)
from src.nl2sql.artifacts.service import DefinitionNotFound
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)

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

CONTROL_IMAGE = "pgvector/pgvector:pg17"
CONTROL_DATABASE = "ttai_control"
CONTROL_OWNER = "control_owner"
CONTROL_OWNER_PASSWORD = "probe_owner_pw"
CONTROL_APP = "control_app"
CONTROL_APP_PASSWORD = "probe_app_pw"
CONTROL_MIGRATIONS = (
    "001_control_schema.sql",
    "002_semantic_registry.sql",
    "003_audit_outbox.sql",
    "004_semantic_registry_v3.sql",
    "005_product_artifacts.sql",
    "006_definition_store.sql",
    "007_confirmation_audit.sql",
)
GOVERNED_METRIC = "demo.revenue"
MISSING_DEFINITION = "def_" + "0" * 32


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.confirmation.control",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key=GOVERNED_METRIC
            ),
        ),
        unit="count",
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
            logs = _docker("logs", container, check=False).stdout
            pytest.fail(f"PostgreSQL container exited during init:\n{logs}")
        time.sleep(0.5)
    logs = _docker("logs", container, check=False).stdout
    pytest.fail(f"PostgreSQL did not become ready:\n{logs}")


def _published_port(container: str) -> int:
    result = _docker("port", container, "5432/tcp")
    return int(result.stdout.strip().rsplit(":", maxsplit=1)[1])


def _psql(
    stack: dict[str, Any],
    sql: str,
    *,
    role: str = CONTROL_APP,
    password: str = CONTROL_APP_PASSWORD,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _docker(
        "exec",
        "--env",
        f"PGPASSWORD={password}",
        stack["container"],
        "psql",
        "--no-psqlrc",
        "--host",
        "127.0.0.1",
        "--username",
        role,
        "--dbname",
        CONTROL_DATABASE,
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
        check=check,
    )


def _owner_psql(
    stack: dict[str, Any], sql: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _psql(
        stack,
        sql,
        role=CONTROL_OWNER,
        password=CONTROL_OWNER_PASSWORD,
        check=check,
    )


def _async_dsn(stack: dict[str, Any]) -> str:
    return (
        f"postgresql+asyncpg://{CONTROL_APP}:{CONTROL_APP_PASSWORD}"
        f"@127.0.0.1:{stack['port']}/{CONTROL_DATABASE}"
    )


@pytest.fixture(scope="module")
def control_postgres() -> Iterator[dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TTAI_RUN_POSTGRES_INTEGRATION=1")
    info = _docker("info", check=False, timeout=30)
    if info.returncode != 0:
        pytest.fail(f"Docker daemon is unavailable: {info.stderr.strip()}")

    container = f"ttai-confirmations-{uuid.uuid4().hex[:10]}"
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
        for migration in CONTROL_MIGRATIONS:
            source = ROOT / "docker/migrations/control" / migration
            _docker("cp", str(source), f"{container}:/tmp/{migration}")
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
                f"/tmp/{migration}",
                timeout=120,
            )
        # The 007 tables are the FIRST control tables with a sequence, so the
        # application role needs the same sequence privileges the production
        # bootstrap grants (docker/initdb/roles.sh: readwrite).
        _psql(
            stack,
            f"CREATE ROLE {CONTROL_APP} LOGIN PASSWORD '{CONTROL_APP_PASSWORD}';"
            f"GRANT CONNECT ON DATABASE {CONTROL_DATABASE} TO {CONTROL_APP};"
            f"GRANT USAGE ON SCHEMA public TO {CONTROL_APP};"
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
            f" TO {CONTROL_APP};"
            f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public"
            f" TO {CONTROL_APP};",
            role=CONTROL_OWNER,
            password=CONTROL_OWNER_PASSWORD,
        )
        applied = _owner_psql(stack, "SELECT count(*) FROM schema_migrations;")
        assert applied.stdout.strip() == str(len(CONTROL_MIGRATIONS))
        stack["port"] = _published_port(container)
        yield stack
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=60)


@pytest.fixture(autouse=True)
def _constructible_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make runtime Settings constructible for the database_url path only."""

    from src.core.settings import get_settings

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def control_backend(
    control_postgres: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point the AppContainer at the probe database for the duration of one test."""

    from src.core.settings import get_settings

    monkeypatch.setenv("PRODUCT_STORE_BACKEND", "control")
    monkeypatch.setenv("CONTROL_DATABASE_URL", _async_dsn(control_postgres))
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _run_async(coroutine: Any) -> Any:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)


def _resolver(key: str) -> bool:
    return key == GOVERNED_METRIC


def _seed_confirmed_version(
    stack: dict[str, Any], label: str
) -> tuple[str, int, str]:
    """Create a REAL definition with one EXACT confirmed version."""

    async def scenario() -> tuple[str, int, str]:
        from src.nl2sql.artifacts.custom_definition import (
            CustomDefinition,
            DefinitionAxes,
            DefinitionVersion,
            derive_parameter_contract,
            utcnow,
        )
        from src.nl2sql.artifacts.definition_control_store import (
            ControlDefinitionStore,
        )

        definition_id = "def_" + hashlib.md5(
            f"{label}-{uuid.uuid4().hex}".encode("utf-8")
        ).hexdigest()
        spec = _spec()
        version = DefinitionVersion(
            definition_id=definition_id,
            version=1,
            calculation=spec,
            parameter_contract=derive_parameter_contract(spec),
            title="T",
            semantic_closed=True,
            created_at=utcnow(),
        )
        store = ControlDefinitionStore(database_url=_async_dsn(stack))
        try:
            await store.put_definition(
                definition=CustomDefinition(
                    definition_id=definition_id,
                    owner_user_id="alice",
                    axes=DefinitionAxes(),
                    current_version=version,
                )
            )
            await store.put_version(version=version)
        finally:
            await store.close()
        return definition_id, version.version, version.checksum

    return _run_async(scenario())


def _insert_audit(
    stack: dict[str, Any],
    *,
    definition_id: str,
    version: int,
    checksum: str,
    actor_sql: str = "'alice'",
    confirmed_at_sql: str = "now()",
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    sql = (
        "INSERT INTO definition_confirmation_audit ("
        "schema_version, definition_id, version, definition_checksum, "
        "confirmed_by, confirmed_at, decision_reference) VALUES ("
        f"'1.0', '{definition_id}', {version}, '{checksum}', "
        f"{actor_sql}, {confirmed_at_sql}, NULL);"
    )
    return _psql(stack, sql, check=check)


def _insert_exploration(
    stack: dict[str, Any],
    *,
    exploration_id: str,
    run_id: str,
    reference_sql: str = "NULL",
    replaces_sql: str = "FALSE",
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    sql = (
        "INSERT INTO exploration_confirmations ("
        "schema_version, exploration_id, run_id, subject, confirmed_by, "
        "confirmed_at, definition_reference, replaces_definition_confirmation) "
        "VALUES ("
        f"'1.0', '{exploration_id}', '{run_id}', 's', 'alice', now(), "
        f"{reference_sql}, {replaces_sql});"
    )
    return _psql(stack, sql, check=check)


# --- cross-container durability -------------------------------------------------


def test_confirmation_audit_survives_a_fresh_container(
    control_postgres: dict[str, Any], control_backend: None
) -> None:
    async def writer_scenario() -> tuple[str, ConfirmationRecord]:
        from src.nl2sql.container import AppContainer

        writer = AppContainer(governed_metric_key_resolver=_resolver)
        await writer.start()
        try:
            assert writer.product_store_backend == "control"
            assert writer.product_store_available is True
            definitions = writer.custom_definition_service()
            draft = await definitions.create_draft(
                owner_user_id="alice", title="Rate", calculation=_spec()
            )
            definition_id = draft.definition_id
            await definitions.mark_semantic_closed(
                owner_user_id="alice", definition_id=definition_id
            )
            confirmed = await definitions.confirm(
                owner_user_id="alice",
                definition_id=definition_id,
                decision_reference="hitl-42",
            )
            record = await definitions.get_confirmation_record(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            assert record.definition_checksum == confirmed.current_version.checksum
            return definition_id, record
        finally:
            await writer.close()

    definition_id, record = _run_async(writer_scenario())

    async def reader_scenario() -> None:
        from src.nl2sql.container import AppContainer

        reader = AppContainer(governed_metric_key_resolver=_resolver)
        await reader.start()
        try:
            definitions = reader.custom_definition_service()
            restored = await definitions.get_confirmation_record(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            # FIELD BY FIELD, not as one aggregate.
            assert restored.schema_version == record.schema_version == "1.0"
            assert restored.definition_id == record.definition_id == definition_id
            assert restored.version == record.version == 1
            assert restored.definition_checksum == record.definition_checksum
            assert restored.confirmed_by == record.confirmed_by == "alice"
            assert restored.confirmed_at == record.confirmed_at
            assert restored.decision_reference == record.decision_reference == "hitl-42"
            assert restored == record
            assert restored.model_dump() == record.model_dump()
            listed = await definitions.list_confirmation_records(
                owner_user_id="alice", definition_id=definition_id
            )
            assert listed == (record,)
            exact = await definitions.get_exact_version(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            assert restored.definition_checksum == exact.checksum
        finally:
            await reader.close()

    _run_async(reader_scenario())


def test_exploration_confirmation_survives_a_fresh_container(
    control_postgres: dict[str, Any], control_backend: None
) -> None:
    async def writer_scenario() -> tuple[str, ExplorationConfirmation]:
        from src.nl2sql.container import AppContainer

        writer = AppContainer(governed_metric_key_resolver=_resolver)
        await writer.start()
        try:
            definitions = writer.custom_definition_service()
            draft = await definitions.create_draft(
                owner_user_id="alice", title="Rate", calculation=_spec()
            )
            definition_id = draft.definition_id
            await definitions.mark_semantic_closed(
                owner_user_id="alice", definition_id=definition_id
            )
            explorations = writer.exploration_confirmation_service()
            record = await explorations.confirm_exploration(
                owner_user_id="alice",
                run_id="run-durable",
                subject="explore the closed draft",
                definition_id=definition_id,
                version=1,
            )
            assert record.definition_reference is not None
            return definition_id, record
        finally:
            await writer.close()

    definition_id, record = _run_async(writer_scenario())

    async def reader_scenario() -> None:
        from src.nl2sql.container import AppContainer

        reader = AppContainer(governed_metric_key_resolver=_resolver)
        await reader.start()
        try:
            explorations = reader.exploration_confirmation_service()
            restored = await explorations.get_owned_exploration_confirmation(
                owner_user_id="alice",
                run_id="run-durable",
                exploration_id=record.exploration_id,
            )
            assert restored.schema_version == record.schema_version == "1.0"
            assert restored.exploration_id == record.exploration_id
            assert restored.run_id == record.run_id == "run-durable"
            assert restored.subject == record.subject
            assert restored.confirmed_by == record.confirmed_by == "alice"
            assert restored.confirmed_at == record.confirmed_at
            assert restored.replaces_definition_confirmation is False
            reference = restored.definition_reference
            assert reference is not None
            assert reference == record.definition_reference
            assert reference.definition_id == definition_id
            assert reference.version == 1
            assert reference.semantic_closed is True
            assert restored == record
            assert restored.model_dump() == record.model_dump()
            listed = await explorations.list_owned_for_run(
                owner_user_id="alice", run_id="run-durable"
            )
            assert listed == (record,)
        finally:
            await reader.close()

    _run_async(reader_scenario())


def test_process_local_confirmation_records_are_lost_on_a_new_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME scenario on the memory backend: a fresh container sees NOTHING."""

    from src.core.settings import get_settings

    monkeypatch.setenv("PRODUCT_STORE_BACKEND", "memory")
    get_settings.cache_clear()
    try:

        async def writer_scenario() -> tuple[str, ConfirmationRecord]:
            from src.nl2sql.container import AppContainer

            writer = AppContainer(governed_metric_key_resolver=_resolver)
            definitions = writer.custom_definition_service()
            assert isinstance(
                definitions.confirmation_audit, InMemoryConfirmationAuditStore
            )
            draft = await definitions.create_draft(
                owner_user_id="alice", title="Rate", calculation=_spec()
            )
            definition_id = draft.definition_id
            await definitions.mark_semantic_closed(
                owner_user_id="alice", definition_id=definition_id
            )
            await definitions.confirm(
                owner_user_id="alice",
                definition_id=definition_id,
                decision_reference="hitl-1",
            )
            record = await definitions.get_confirmation_record(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            return definition_id, record

        definition_id, record = _run_async(writer_scenario())
        assert record.confirmed_by == "alice"

        async def reader_scenario() -> None:
            from src.nl2sql.container import AppContainer

            reader = AppContainer(governed_metric_key_resolver=_resolver)
            definitions = reader.custom_definition_service()
            # The process-local dicts were discarded with the writer, so BOTH the
            # definition AND its audit trail are gone.
            assert (
                await definitions.confirmation_audit.get(
                    definition_id=definition_id, version=1
                )
                is None
            )
            assert (
                await definitions.confirmation_audit.list_for_definition(
                    definition_id=definition_id
                )
                == ()
            )
            with pytest.raises(DefinitionNotFound):
                await definitions.get_owned_definition(
                    owner_user_id="alice", definition_id=definition_id
                )

        _run_async(reader_scenario())
    finally:
        get_settings.cache_clear()


# --- database-level rejection (NOT application-level) ---------------------------


def test_database_rejects_updating_an_audit_record(
    control_postgres: dict[str, Any]
) -> None:
    definition_id, version, checksum = _seed_confirmed_version(
        control_postgres, "audit-update"
    )
    inserted = _insert_audit(
        control_postgres,
        definition_id=definition_id,
        version=version,
        checksum=checksum,
    )
    assert inserted.returncode == 0, inserted.stderr

    tampered = _psql(
        control_postgres,
        "UPDATE definition_confirmation_audit SET confirmed_by = 'mallory' "
        f"WHERE definition_id = '{definition_id}';",
        check=False,
    )
    assert tampered.returncode != 0
    assert "confirmation_audit_append_only" in tampered.stderr

    other_identity = MISSING_DEFINITION
    identity = _psql(
        control_postgres,
        "UPDATE definition_confirmation_audit SET definition_id = "
        f"'{other_identity}' WHERE definition_id = '{definition_id}';",
        check=False,
    )
    assert identity.returncode != 0
    assert "confirmation_audit_append_only" in identity.stderr

    # The refused writes changed NOTHING.
    remaining = _owner_psql(
        control_postgres,
        "SELECT count(*) FROM definition_confirmation_audit "
        f"WHERE definition_id = '{definition_id}' AND confirmed_by = 'alice';",
    )
    assert remaining.stdout.strip() == "1"


def test_database_rejects_deleting_an_audit_record(
    control_postgres: dict[str, Any]
) -> None:
    definition_id, version, checksum = _seed_confirmed_version(
        control_postgres, "audit-delete"
    )
    assert (
        _insert_audit(
            control_postgres,
            definition_id=definition_id,
            version=version,
            checksum=checksum,
        ).returncode
        == 0
    )
    deleted = _psql(
        control_postgres,
        "DELETE FROM definition_confirmation_audit "
        f"WHERE definition_id = '{definition_id}';",
        check=False,
    )
    assert deleted.returncode != 0
    assert "confirmation_audit_append_only" in deleted.stderr
    remaining = _owner_psql(
        control_postgres,
        "SELECT count(*) FROM definition_confirmation_audit "
        f"WHERE definition_id = '{definition_id}';",
    )
    assert remaining.stdout.strip() == "1"


def test_database_rejects_a_dangling_definition_version(
    control_postgres: dict[str, Any]
) -> None:
    dangling = _insert_audit(
        control_postgres,
        definition_id=MISSING_DEFINITION,
        version=1,
        checksum="c" * 64,
        check=False,
    )
    assert dangling.returncode != 0
    assert "definition_confirmation_audit_version_exists" in dangling.stderr

    # A REAL version with a MISMATCHED checksum is equally impossible.
    definition_id, version, _checksum = _seed_confirmed_version(
        control_postgres, "audit-dangling"
    )
    mismatch = _insert_audit(
        control_postgres,
        definition_id=definition_id,
        version=version,
        checksum="d" * 64,
        check=False,
    )
    assert mismatch.returncode != 0
    assert "definition_confirmation_audit_version_exists" in mismatch.stderr

    # A real version with a version number that does not exist is refused too.
    missing_version = _insert_audit(
        control_postgres,
        definition_id=definition_id,
        version=version + 1,
        checksum="e" * 64,
        check=False,
    )
    assert missing_version.returncode != 0
    assert "definition_confirmation_audit_version_exists" in missing_version.stderr


def test_database_rejects_a_blank_or_absent_audit_actor_and_time(
    control_postgres: dict[str, Any]
) -> None:
    definition_id, version, checksum = _seed_confirmed_version(
        control_postgres, "audit-actor"
    )
    blank = _insert_audit(
        control_postgres,
        definition_id=definition_id,
        version=version,
        checksum=checksum,
        actor_sql="''",
        check=False,
    )
    assert blank.returncode != 0
    assert "definition_confirmation_audit_actor_present" in blank.stderr

    absent_actor = _insert_audit(
        control_postgres,
        definition_id=definition_id,
        version=version,
        checksum=checksum,
        actor_sql="NULL",
        check=False,
    )
    assert absent_actor.returncode != 0
    assert 'null value in column "confirmed_by"' in absent_actor.stderr

    absent_time = _insert_audit(
        control_postgres,
        definition_id=definition_id,
        version=version,
        checksum=checksum,
        confirmed_at_sql="NULL",
        check=False,
    )
    assert absent_time.returncode != 0
    assert 'null value in column "confirmed_at"' in absent_time.stderr


def test_database_rejects_a_true_replaces_definition_confirmation(
    control_postgres: dict[str, Any]
) -> None:
    refused = _insert_exploration(
        control_postgres,
        exploration_id="exp_" + uuid.uuid4().hex,
        run_id="run-replaces",
        replaces_sql="TRUE",
        check=False,
    )
    assert refused.returncode != 0
    assert "exploration_confirmations_replaces_never" in refused.stderr

    stored_id = "exp_" + uuid.uuid4().hex
    accepted = _insert_exploration(
        control_postgres,
        exploration_id=stored_id,
        run_id="run-replaces",
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    # The same literal is enforced for a raw UPDATE, not only for an INSERT.
    flipped = _psql(
        control_postgres,
        "UPDATE exploration_confirmations SET replaces_definition_confirmation = TRUE "
        f"WHERE exploration_id = '{stored_id}';",
        check=False,
    )
    assert flipped.returncode != 0
    assert "exploration_confirmations_replaces_never" in flipped.stderr


def test_database_enforces_the_run_exploration_uniqueness(
    control_postgres: dict[str, Any]
) -> None:
    exploration_id = "exp_" + uuid.uuid4().hex
    first = _insert_exploration(
        control_postgres,
        exploration_id=exploration_id,
        run_id="run-unique",
    )
    assert first.returncode == 0, first.stderr
    duplicate = _insert_exploration(
        control_postgres,
        exploration_id=exploration_id,
        run_id="run-unique",
        check=False,
    )
    assert duplicate.returncode != 0
    assert "exploration_confirmations_run_exploration_key" in duplicate.stderr


# --- migration idempotency ------------------------------------------------------


def test_migration_007_is_idempotent(control_postgres: dict[str, Any]) -> None:
    container = control_postgres["container"]
    source = ROOT / "docker/migrations/control/007_confirmation_audit.sql"
    _docker("cp", str(source), f"{container}:/tmp/007_replay.sql")
    replay = _docker(
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
        "/tmp/007_replay.sql",
        check=False,
        timeout=120,
    )
    assert replay.returncode == 0, replay.stderr
    versions = _owner_psql(
        control_postgres, "SELECT version FROM schema_migrations ORDER BY version;"
    )
    assert "007_confirmation_audit" in versions.stdout
    count = _owner_psql(control_postgres, "SELECT count(*) FROM schema_migrations;")
    assert count.stdout.strip() == str(len(CONTROL_MIGRATIONS))


# --- differential contract ------------------------------------------------------


def test_memory_and_control_confirmation_stores_are_differentially_equivalent(
    control_postgres: dict[str, Any]
) -> None:
    first = _seed_confirmed_version(control_postgres, "diff-a")
    second = _seed_confirmed_version(control_postgres, "diff-b")
    dsn = _async_dsn(control_postgres)

    async def scenario() -> None:
        memory_audit = InMemoryConfirmationAuditStore()
        control_audit = ControlConfirmationAuditStore(database_url=dsn)
        memory_exploration = InMemoryExplorationConfirmationStore()
        control_exploration = ControlExplorationConfirmationStore(database_url=dsn)
        try:
            audit_records = (
                ConfirmationRecord(
                    definition_id=first[0],
                    version=first[1],
                    definition_checksum=first[2],
                    confirmed_by="alice",
                    confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
                    decision_reference="hitl-a",
                ),
                ConfirmationRecord(
                    definition_id=first[0],
                    version=first[1],
                    definition_checksum=first[2],
                    confirmed_by="bob",
                    confirmed_at=datetime(2026, 1, 2, tzinfo=UTC),
                    decision_reference=None,
                ),
                ConfirmationRecord(
                    definition_id=second[0],
                    version=second[1],
                    definition_checksum=second[2],
                    confirmed_by="alice",
                    confirmed_at=datetime(2026, 1, 3, tzinfo=UTC),
                    decision_reference="hitl-b",
                ),
            )
            for record in audit_records:
                await memory_audit.put(record=record)
                await control_audit.put(record=record)
            for definition_id in (first[0], second[0], MISSING_DEFINITION):
                memory_get = await memory_audit.get(
                    definition_id=definition_id, version=1
                )
                control_get = await control_audit.get(
                    definition_id=definition_id, version=1
                )
                assert (memory_get is None) == (control_get is None)
                if memory_get is not None and control_get is not None:
                    assert memory_get.model_dump() == control_get.model_dump()
                memory_list = await memory_audit.list_for_definition(
                    definition_id=definition_id
                )
                control_list = await control_audit.list_for_definition(
                    definition_id=definition_id
                )
                assert memory_list == control_list
                assert [item.model_dump() for item in memory_list] == [
                    item.model_dump() for item in control_list
                ]

            reference = ExplorationDefinitionReference(
                definition_id=first[0],
                version=1,
                definition_checksum=first[2],
                semantic_closed=True,
            )
            exploration_records = (
                ExplorationConfirmation(
                    exploration_id="exp_" + "1" * 32,
                    run_id="run-diff",
                    subject="s1",
                    confirmed_by="alice",
                    confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
                    definition_reference=reference,
                ),
                ExplorationConfirmation(
                    exploration_id="exp_" + "2" * 32,
                    run_id="run-diff",
                    subject="s2",
                    confirmed_by="alice",
                    confirmed_at=datetime(2026, 1, 2, tzinfo=UTC),
                    definition_reference=None,
                ),
                ExplorationConfirmation(
                    exploration_id="exp_" + "3" * 32,
                    run_id="run-other",
                    subject="s3",
                    confirmed_by="bob",
                    confirmed_at=datetime(2026, 1, 3, tzinfo=UTC),
                    definition_reference=None,
                ),
            )
            for record in exploration_records:
                await memory_exploration.put(record=record)
                await control_exploration.put(record=record)
            for run_id in ("run-diff", "run-other", "run-absent"):
                memory_list = await memory_exploration.list_for_run(run_id=run_id)
                control_list = await control_exploration.list_for_run(run_id=run_id)
                assert memory_list == control_list
                assert [item.model_dump() for item in memory_list] == [
                    item.model_dump() for item in control_list
                ]
            for record in exploration_records:
                memory_get = await memory_exploration.get(
                    run_id=record.run_id, exploration_id=record.exploration_id
                )
                control_get = await control_exploration.get(
                    run_id=record.run_id, exploration_id=record.exploration_id
                )
                assert memory_get is not None
                assert control_get is not None
                assert memory_get.model_dump() == control_get.model_dump()
        finally:
            await memory_audit.close()
            await control_audit.close()
            await memory_exploration.close()
            await control_exploration.close()

    _run_async(scenario())


# --- typed integrity error over a REAL corrupted row ----------------------------


def test_a_corrupt_stored_exploration_payload_is_the_typed_integrity_error(
    control_postgres: dict[str, Any]
) -> None:
    exploration_id = "exp_" + uuid.uuid4().hex
    record = ExplorationConfirmation(
        exploration_id=exploration_id,
        run_id="run-corrupt",
        subject="s",
        confirmed_by="alice",
        confirmed_at=datetime.now(UTC),
        definition_reference=ExplorationDefinitionReference(
            definition_id="def_" + "a" * 32,
            version=1,
            definition_checksum="b" * 64,
            semantic_closed=True,
        ),
    )
    dsn = _async_dsn(control_postgres)

    async def seed() -> None:
        store = ControlExplorationConfirmationStore(database_url=dsn)
        try:
            await store.put(record=record)
        finally:
            await store.close()

    _run_async(seed())

    # The DB CHECK accepts an EXTRA member; the model (extra="forbid") does not.
    extra = json.dumps({"forged": "x"})
    corrupted = _psql(
        control_postgres,
        "UPDATE exploration_confirmations SET definition_reference = "
        "definition_reference || "
        f"'{extra}'::jsonb WHERE exploration_id = '{exploration_id}';",
        check=False,
    )
    assert corrupted.returncode == 0, corrupted.stderr

    async def read() -> None:
        store = ControlExplorationConfirmationStore(database_url=dsn)
        try:
            with pytest.raises(ConfirmationStoreIntegrityError) as failure:
                await store.get(
                    run_id="run-corrupt", exploration_id=exploration_id
                )
            assert str(failure.value) == "confirmation_store_row_incoherent"
        finally:
            await store.close()

    _run_async(read())


# --- startup ping / readiness gate ----------------------------------------------


def test_start_pings_the_confirmation_stores(
    control_postgres: dict[str, Any],
    control_backend: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.nl2sql.artifacts import confirmation_control_store as module

    pings: list[str] = []
    audit_ping = module.ControlConfirmationAuditStore.ping
    exploration_ping = module.ControlExplorationConfirmationStore.ping

    async def recording_audit(self: Any) -> None:
        pings.append("audit")
        await audit_ping(self)

    async def recording_exploration(self: Any) -> None:
        pings.append("exploration")
        await exploration_ping(self)

    monkeypatch.setattr(module.ControlConfirmationAuditStore, "ping", recording_audit)
    monkeypatch.setattr(
        module.ControlExplorationConfirmationStore, "ping", recording_exploration
    )

    async def scenario() -> None:
        from src.nl2sql.container import AppContainer

        container = AppContainer(governed_metric_key_resolver=_resolver)
        await container.start()
        try:
            assert container.product_store_available is True
        finally:
            await container.close()

    _run_async(scenario())
    assert pings == ["audit", "exploration"]


def test_an_unreachable_confirmation_audit_store_blocks_readiness(
    control_postgres: dict[str, Any],
    control_backend: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.nl2sql.artifacts import confirmation_control_store as module

    async def failing_ping(self: Any) -> None:
        raise RuntimeError("audit unreachable")

    monkeypatch.setattr(module.ControlConfirmationAuditStore, "ping", failing_ping)

    async def scenario() -> dict[str, object]:
        from src.nl2sql.container import AppContainer

        container = AppContainer(governed_metric_key_resolver=_resolver)
        await container.start()
        try:
            return container.readiness_report(model_available=False)
        finally:
            await container.close()

    report = _run_async(scenario())
    assert "product_store_initialization_failed" in report["degradation_reasons"]
