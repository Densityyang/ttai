"""Docker PostgreSQL contract for the Control-PG definition store.

The definition store is gated exactly like the other control-plane contracts:
nothing runs unless TTAI_RUN_POSTGRES_INTEGRATION=1, and the container is
created, migrated with 001..009 and torn down by this module alone.

What is proven here is the end of the HALF-persisted state:

* a definition created in one container is read back, WITH its exact version,
  its four independent axes and its per-version lifecycle, by a FRESH container;
* a published version written in one container RESOLVES its source_definition_id
  to the same definition in a fresh container, with a matching checksum, so the
  dangling reference is gone;
* a dangling or incoherent published row still FAILS CLOSED on read;
* every documented invariant is also a DATABASE-level guarantee: a direct SQL
  writer cannot violate an axis implication, rewrite an exact version, re-own a
  definition, move the current pointer backwards, skip a version, or name an
  absent definition from a publication.
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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.nl2sql.artifacts.custom_definition_execution_service import (
    CustomDefinitionExecutionRefused,
    CustomDefinitionExecutionService,
)
from src.nl2sql.artifacts.definition_control_store import (
    ControlDefinitionStore,
    DefinitionStoreIntegrityError,
)
from src.nl2sql.artifacts.definition_revalidation import DefinitionRevalidationGate
from src.nl2sql.artifacts.definition_run_record import (
    DefinitionRunRecordConflict,
    new_run_id,
)
from src.nl2sql.artifacts.definition_run_record_control_store import (
    ControlDefinitionRunRecordStore,
    DefinitionRunRecordIntegrityError,
)
from src.nl2sql.artifacts.definition_semantics import (
    DefinitionSemantics,
    ExecutionApplicability,
)
from src.nl2sql.artifacts.publication_control_store import (
    ControlPublicationCatalogue,
)
from src.nl2sql.artifacts.publication_service import PublicationSourceUnresolved
from src.nl2sql.contracts import TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
from src.nl2sql.semantic.calculation_contract import (
    CalculationExecutionBinding,
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
    "008_definition_run_records.sql",
    "009_hitl_action_claims.sql",
)
GOVERNED_METRIC = "demo.revenue"
CHECKSUM_A = "a" * 64
CHECKSUM_B = "b" * 64


def _definition_id(label: str) -> str:
    return "def_" + hashlib.md5(label.encode("utf-8")).hexdigest()


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.definition.control",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key=GOVERNED_METRIC
            ),
        ),
        unit="count",
    )


def _version_payload(
    definition_id: str, version: int, *, semantic_closed: bool
) -> str:
    """A REAL DefinitionVersion JSON, so direct-SQL rows stay readable."""

    from src.nl2sql.artifacts.custom_definition import (
        DefinitionVersion,
        derive_parameter_contract,
        utcnow,
    )

    spec = _spec()
    model = DefinitionVersion(
        definition_id=definition_id,
        version=version,
        calculation=spec,
        parameter_contract=derive_parameter_contract(spec),
        title="T",
        semantic_closed=semantic_closed,
        created_at=utcnow(),
    )
    return json.dumps(
        model.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )


def _definition_payload(definition_id: str, version: int) -> str:
    return _version_payload(definition_id, version, semantic_closed=False)


def _exact_payload(definition_id: str, version: int) -> str:
    return _version_payload(definition_id, version, semantic_closed=True)


def _semantic_payload(definition_id: str, version: int, checksum: str) -> str:
    return json.dumps(
        {
            "calculation": _spec().model_dump(mode="json"),
            "parameter_contract": {"parameters": []},
            "source_definition_id": definition_id,
            "source_definition_version": version,
            "source_definition_checksum": checksum,
        },
        ensure_ascii=False,
        separators=(",", ":"),
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
    database: str = CONTROL_DATABASE,
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
        database,
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
        check=check,
    )


def _owner_psql(
    stack: dict[str, Any],
    sql: str,
    *,
    database: str = CONTROL_DATABASE,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _psql(
        stack,
        sql,
        role=CONTROL_OWNER,
        password=CONTROL_OWNER_PASSWORD,
        database=database,
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

    container = f"ttai-definitions-{uuid.uuid4().hex[:10]}"
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
        _psql(
            stack,
            f"CREATE ROLE {CONTROL_APP} LOGIN PASSWORD '{CONTROL_APP_PASSWORD}';"
            f"GRANT CONNECT ON DATABASE {CONTROL_DATABASE} TO {CONTROL_APP};"
            f"GRANT USAGE ON SCHEMA public TO {CONTROL_APP};"
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
            f" TO {CONTROL_APP};"
            # The 007 tables are the FIRST control tables with a sequence, so the
            # application role needs the same sequence privileges the production
            # bootstrap grants (docker/initdb/roles.sh: readwrite).  Without this
            # the fixture reaches the tables and then fails on the sequence.
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


class _RunRecordInputFetcher:
    async def fetch_metric_input(self, **kwargs: object) -> ResolvedCalculationInput:
        return ResolvedCalculationInput(
            role=str(kwargs["role"]),
            metric_key=str(kwargs["metric_key"]),
            value=Decimal("1"),
            unit="count",
            data_as_of=datetime(2026, 10, 13, tzinfo=UTC),
            time_range=TimeRange(
                start=date(2026, 10, 13), end=date(2026, 10, 13)
            ),
            provenance="published_gold",
            source_id="gold.control.fixture",
            receipt_step_id="fetch_control_fixture",
            fact_id="d" * 64,
        )


# --- cross-container durability -------------------------------------------------


def test_definition_version_axes_and_lifecycle_survive_a_fresh_container(
    control_postgres: dict[str, Any], control_backend: None
) -> None:
    async def scenario() -> tuple[str, str, str, str]:
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
            await definitions.confirm(
                owner_user_id="alice", definition_id=definition_id
            )
            await definitions.save(owner_user_id="alice", definition_id=definition_id)
            # A5: the run-scoped values live in the binding, never in a new
            # version, so confirming/saving leaves EXACTLY one saved version.
            saved_versions = await definitions.list_saved_versions(
                owner_user_id="alice", definition_id=definition_id
            )
            assert [item.version for item in saved_versions] == [1]
            publications = await writer.publication_service()
            published = await publications.publish(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            assert published.semantic is not None
            return (
                definition_id,
                published.definition_checksum,
                published.semantic.source_definition_checksum,
            )
        finally:
            await writer.close()

    definition_id, definition_checksum, source_checksum = _run_async(scenario())
    assert definition_checksum == source_checksum

    # Governance is ORTHOGONAL to the lifecycle axes: a proposal recorded while
    # the definition is stored must survive the restart exactly like they do.
    _psql(
        control_postgres,
        "UPDATE custom_definitions SET governance = 'GOVERNANCE_CANDIDATE' "
        f"WHERE definition_id = '{definition_id}';",
    )

    async def reader_scenario() -> None:
        from src.nl2sql.container import AppContainer

        reader = AppContainer(governed_metric_key_resolver=_resolver)
        await reader.start()
        try:
            definitions = reader.custom_definition_service()
            restored = await definitions.get_owned_definition(
                owner_user_id="alice", definition_id=definition_id
            )
            assert restored.current_version.version == 1
            assert restored.axes.confirmation == "CONFIRMED"
            assert restored.axes.retention == "SAVED"
            assert restored.axes.publication == "PUBLISHED"
            assert restored.axes.certification == "UNCERTIFIED"
            assert restored.axes.governance == "GOVERNANCE_CANDIDATE"
            assert restored.axes.authority == "noncanonical"

            exact = await definitions.get_exact_version(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            assert exact.checksum == definition_checksum
            assert exact.semantic_closed is True

            lifecycle = await definitions.get_version_lifecycle(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            assert lifecycle.confirmation == "CONFIRMED"
            assert lifecycle.retention == "SAVED"

            saved = await definitions.list_saved_versions(
                owner_user_id="alice", definition_id=definition_id
            )
            assert [item.version for item in saved] == [1]

            # THE core acceptance: the published version's source resolves to a
            # definition that actually exists after the restart.
            publications = await reader.publication_service()
            source = await publications.resolve_published_source(
                identity_id=definition_id, version=1
            )
            assert source.definition_id == definition_id
            assert source.checksum == source_checksum
            catalogue = await reader.publication_catalogue()
            published_read = await catalogue.get(definition_id, 1)
            assert published_read is not None
            assert published_read.semantic is not None
            assert published_read.semantic.source_definition_id == definition_id
        finally:
            await reader.close()

    _run_async(reader_scenario())


def test_a_later_revision_does_not_rewrite_the_exact_v1_lifecycle(
    control_postgres: dict[str, Any], control_backend: None
) -> None:
    """A4 across a restart: per-version lifecycle survives a later revision."""

    async def writer_scenario() -> str:
        from src.nl2sql.container import AppContainer

        writer = AppContainer(governed_metric_key_resolver=_resolver)
        await writer.start()
        try:
            definitions = writer.custom_definition_service()
            draft = await definitions.create_draft(
                owner_user_id="alice", title="Rate", calculation=_spec()
            )
            definition_id = draft.definition_id
            for step in ("mark_semantic_closed", "confirm", "save"):
                await getattr(definitions, step)(
                    owner_user_id="alice", definition_id=definition_id
                )
            await definitions.create_revision(
                owner_user_id="alice", definition_id=definition_id
            )
            return definition_id
        finally:
            await writer.close()

    definition_id = _run_async(writer_scenario())

    async def reader_scenario() -> None:
        from src.nl2sql.container import AppContainer

        reader = AppContainer(governed_metric_key_resolver=_resolver)
        await reader.start()
        try:
            definitions = reader.custom_definition_service()
            v1 = await definitions.get_version_lifecycle(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            v2 = await definitions.get_version_lifecycle(
                owner_user_id="alice", definition_id=definition_id, version=2
            )
            assert (v1.confirmation, v1.retention) == ("CONFIRMED", "SAVED")
            assert (v2.confirmation, v2.retention) == ("DRAFT", "SESSION")
            current = await definitions.get_owned_definition(
                owner_user_id="alice", definition_id=definition_id
            )
            assert current.current_version.version == 2
            # A4: the axes stay FOUR independent fields with no aggregate status.
            assert current.axes.publication == "UNPUBLISHED"
            assert current.axes.certification == "UNCERTIFIED"
        finally:
            await reader.close()

    _run_async(reader_scenario())


# --- read-time fail-closed ------------------------------------------------------


def test_a_grandfathered_dangling_publication_fails_closed_on_read(
    control_postgres: dict[str, Any],
) -> None:
    """A row published by 005 (before definitions were persisted) is REFUSED."""

    identity = _definition_id("grandfathered")
    absent = _definition_id("never-persisted")
    semantic = _semantic_payload(absent, 1, CHECKSUM_A)
    _owner_psql(
        control_postgres,
        "ALTER TABLE product_publication_versions "
        "DROP CONSTRAINT IF EXISTS product_publication_source_definition_exists;",
    )
    try:
        _psql(
            control_postgres,
            "INSERT INTO product_publication_versions ("
            "identity_id, version, title, owner_user_id, owner_label, source_label,"
            "definition_checksum, published_at, unit, semantic"
            ") VALUES ("
            f"'{identity}', 1, '{identity}', 'alice', 'alice', 'local_demo',"
            f"'{CHECKSUM_A}', '2026-01-01T00:00:00Z', 'count', "
            f"'{semantic}'::jsonb);",
        )
    finally:
        # Restore EXACTLY the NOT VALID form 006 installs, so the pre-existing
        # dangling row is grandfathered and every later row is enforced.
        _owner_psql(
            control_postgres,
            "ALTER TABLE product_publication_versions "
            "ADD CONSTRAINT product_publication_source_definition_exists "
            "FOREIGN KEY (source_definition_id, source_definition_version, "
            "source_definition_checksum) "
            "REFERENCES custom_definition_versions "
            "(definition_id, version, checksum) NOT VALID;",
        )

    async def scenario() -> None:
        from src.nl2sql.artifacts.publication_service import PublicationService
        from src.nl2sql.artifacts.service import CustomDefinitionService

        catalogue = ControlPublicationCatalogue(database_url=_async_dsn(control_postgres))
        try:
            # The row IS readable as a published version...
            published = await catalogue.get(identity, 1)
            assert published is not None and published.semantic is not None
            # ...but its provenance is unprovable, so the source read FAILS CLOSED
            # with a typed error instead of serving a dangling definition id.
            publications = PublicationService(
                definitions=CustomDefinitionService(), catalogue=catalogue
            )
            with pytest.raises(PublicationSourceUnresolved) as failure:
                await publications.resolve_published_source(
                    identity_id=identity, version=1
                )
            assert str(failure.value) == "publication_source_definition_missing"
        finally:
            await catalogue.close()

    _run_async(scenario())


def test_a_checksum_mismatch_is_refused_on_read(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("checksum-read")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 1, '{_definition_payload(definition_id, 1)}'::jsonb);",
    )
    _psql(
        control_postgres,
        "INSERT INTO custom_definition_versions ("
        "definition_id, version, payload, checksum"
        ") VALUES ("
        f"'{definition_id}', 1, '{_exact_payload(definition_id, 1)}'::jsonb,"
        f"'{CHECKSUM_A}');",
    )

    async def scenario() -> None:
        store = ControlDefinitionStore(database_url=_async_dsn(control_postgres))
        try:
            # A direct writer CAN desynchronise the recorded checksum from the
            # payload; the read path REFUSES it instead of serving it.
            _owner_psql(
                control_postgres,
                "ALTER TABLE custom_definition_versions "
                "DISABLE TRIGGER custom_definition_versions_immutable;",
            )
            try:
                _owner_psql(
                    control_postgres,
                    "UPDATE custom_definition_versions SET checksum = "
                    f"'{CHECKSUM_B}' WHERE definition_id = '{definition_id}' "
                    "AND version = 1;",
                )
            finally:
                # The immutability trigger is restored even when the write above
                # fails, so no later test inherits a disabled guard.
                _owner_psql(
                    control_postgres,
                    "ALTER TABLE custom_definition_versions "
                    "ENABLE TRIGGER custom_definition_versions_immutable;",
                )
            with pytest.raises(DefinitionStoreIntegrityError):
                await store.get_version(definition_id=definition_id, version=1)
        finally:
            await store.close()

    _run_async(scenario())


# --- database-level invariants (illegal writes refused) -------------------------


@pytest.mark.parametrize(
    "confirmation,retention,publication,certification",
    [
        ("DRAFT", "SAVED", "UNPUBLISHED", "UNCERTIFIED"),
        ("DRAFT", "SESSION", "PUBLISHED", "UNCERTIFIED"),
        ("CONFIRMED", "SESSION", "PUBLISHED", "UNCERTIFIED"),
        ("CONFIRMED", "SAVED", "UNPUBLISHED", "CERTIFIED"),
    ],
)
def test_database_refuses_axis_implication_violations(
    control_postgres: dict[str, Any],
    confirmation: str,
    retention: str,
    publication: str,
    certification: str,
) -> None:
    definition_id = _definition_id(
        f"axes-{confirmation}-{retention}-{publication}-{certification}"
    )
    payload = _definition_payload(definition_id, 1)
    result = _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', '{confirmation}', '{retention}',"
        f"'{publication}', '{certification}', 1, '{payload}'::jsonb);",
        check=False,
    )
    assert result.returncode != 0
    assert "custom_definitions_" in result.stderr


def test_database_refuses_an_in_place_canonicalization(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("canonicalize")
    payload = _definition_payload(definition_id, 1)
    inserted = _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, governance, authority, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 'NONE', 'canonical', 1, '{payload}'::jsonb);",
        check=False,
    )
    assert inserted.returncode != 0
    assert "custom_definitions_authority_noncanonical" in inserted.stderr

    # A LEGAL row can still never be canonicalized in place.
    legal_id = _definition_id("canonicalize-update")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, governance, authority, current_version, current_payload"
        ") VALUES ("
        f"'{legal_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 'NONE', 'noncanonical', 1, "
        f"'{_definition_payload(legal_id, 1)}'::jsonb);",
    )
    canonicalized = _psql(
        control_postgres,
        "UPDATE custom_definitions SET authority = 'canonical' "
        f"WHERE definition_id = '{legal_id}';",
        check=False,
    )
    assert canonicalized.returncode != 0
    assert "custom_definitions_authority_noncanonical" in canonicalized.stderr


def test_database_refuses_an_unknown_governance_value(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("governance")
    payload = _definition_payload(definition_id, 1)
    result = _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, governance, authority, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 'BOGUS', 'noncanonical', 1, '{payload}'::jsonb);",
        check=False,
    )
    assert result.returncode != 0
    assert "custom_definitions_governance_known" in result.stderr


def test_database_refuses_to_rewrite_an_exact_version(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("immutable-version")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 1, '{_definition_payload(definition_id, 1)}'::jsonb);",
    )
    _psql(
        control_postgres,
        "INSERT INTO custom_definition_versions ("
        "definition_id, version, payload, checksum"
        ") VALUES ("
        f"'{definition_id}', 1, '{_exact_payload(definition_id, 1)}'::jsonb,"
        f"'{CHECKSUM_A}');",
    )
    rewritten = _psql(
        control_postgres,
        "UPDATE custom_definition_versions SET checksum = "
        f"'{CHECKSUM_B}' WHERE definition_id = '{definition_id}' AND version = 1;",
        check=False,
    )
    assert rewritten.returncode != 0
    assert "exact_definition_version_immutable" in rewritten.stderr


def test_database_refuses_to_re_own_a_definition(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("owner")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 1, '{_definition_payload(definition_id, 1)}'::jsonb);",
    )
    reowned = _psql(
        control_postgres,
        f"UPDATE custom_definitions SET owner_user_id = 'bob' "
        f"WHERE definition_id = '{definition_id}';",
        check=False,
    )
    assert reowned.returncode != 0
    assert "definition_owner_immutable" in reowned.stderr


def test_database_refuses_a_current_version_regression(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("regression")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 2, '{_definition_payload(definition_id, 2)}'::jsonb);",
    )
    regressed = _psql(
        control_postgres,
        "UPDATE custom_definitions SET current_version = 1, current_payload = "
        f"'{_definition_payload(definition_id, 1)}'::jsonb "
        f"WHERE definition_id = '{definition_id}';",
        check=False,
    )
    assert regressed.returncode != 0
    assert "definition_current_version_regression" in regressed.stderr


def test_database_refuses_a_skipped_exact_version(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("skipped")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 1, '{_definition_payload(definition_id, 1)}'::jsonb);",
    )
    _psql(
        control_postgres,
        "INSERT INTO custom_definition_versions ("
        "definition_id, version, payload, checksum"
        ") VALUES ("
        f"'{definition_id}', 1, '{_exact_payload(definition_id, 1)}'::jsonb,"
        f"'{CHECKSUM_A}');",
    )
    skipped = _psql(
        control_postgres,
        "INSERT INTO custom_definition_versions ("
        "definition_id, version, payload, checksum"
        ") VALUES ("
        f"'{definition_id}', 3, '{_exact_payload(definition_id, 3)}'::jsonb,"
        f"'{CHECKSUM_B}');",
        check=False,
    )
    assert skipped.returncode != 0
    assert "definition_version_not_monotonic" in skipped.stderr


def test_database_refuses_a_publication_naming_an_absent_definition(
    control_postgres: dict[str, Any],
) -> None:
    identity = _definition_id("dangling-write")
    absent = _definition_id("absent-source")
    semantic = _semantic_payload(absent, 1, CHECKSUM_A)
    result = _psql(
        control_postgres,
        "INSERT INTO product_publication_versions ("
        "identity_id, version, title, owner_user_id, owner_label, source_label,"
        "definition_checksum, published_at, unit, semantic"
        ") VALUES ("
        f"'{identity}', 1, '{identity}', 'alice', 'alice', 'local_demo',"
        f"'{CHECKSUM_A}', '2026-01-01T00:00:00Z', 'count', "
        f"'{semantic}'::jsonb);",
        check=False,
    )
    assert result.returncode != 0
    assert "product_publication_source_definition_exists" in result.stderr


def test_database_refuses_to_delete_a_referenced_definition_version(
    control_postgres: dict[str, Any],
) -> None:
    definition_id = _definition_id("referenced-delete")
    identity = _definition_id("referenced-publication")
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'DRAFT', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 1, '{_definition_payload(definition_id, 1)}'::jsonb);",
    )
    _psql(
        control_postgres,
        "INSERT INTO custom_definition_versions ("
        "definition_id, version, payload, checksum"
        ") VALUES ("
        f"'{definition_id}', 1, '{_exact_payload(definition_id, 1)}'::jsonb,"
        f"'{CHECKSUM_A}');",
    )
    semantic = _semantic_payload(definition_id, 1, CHECKSUM_A)
    _psql(
        control_postgres,
        "INSERT INTO product_publication_versions ("
        "identity_id, version, title, owner_user_id, owner_label, source_label,"
        "definition_checksum, published_at, unit, semantic"
        ") VALUES ("
        f"'{identity}', 1, '{identity}', 'alice', 'alice', 'local_demo',"
        f"'{CHECKSUM_A}', '2026-01-01T00:00:00Z', 'count', "
        f"'{semantic}'::jsonb);",
    )
    removed = _psql(
        control_postgres,
        "DELETE FROM custom_definition_versions "
        f"WHERE definition_id = '{definition_id}' AND version = 1;",
        check=False,
    )
    assert removed.returncode != 0
    assert "product_publication_source_definition_exists" in removed.stderr


def test_control_store_round_trips_through_the_port(
    control_postgres: dict[str, Any],
) -> None:
    """The durable port, exercised directly (not only through AppContainer)."""

    async def scenario() -> None:
        store = ControlDefinitionStore(database_url=_async_dsn(control_postgres))
        try:
            await store.ping()
            absent = _definition_id("port-absent")
            assert await store.get_definition(definition_id=absent) is None
            assert await store.get_version(definition_id=absent, version=1) is None
            assert (
                await store.get_lifecycle(definition_id=absent, version=1) is None
            )
            assert await store.list_versions(definition_id=absent) == ()

            from src.nl2sql.artifacts.custom_definition import (
                CustomDefinition,
                DefinitionAxes,
                DefinitionVersion,
                DefinitionVersionLifecycle,
                derive_parameter_contract,
                new_definition_id,
                utcnow,
            )

            definition_id = new_definition_id()
            version = DefinitionVersion(
                definition_id=definition_id,
                version=1,
                calculation=_spec(),
                parameter_contract=derive_parameter_contract(_spec()),
                title="Rate",
                semantic_closed=True,
                semantics=DefinitionSemantics(
                    population="eligible service visits",
                    execution_applicability=ExecutionApplicability(
                        denominator="NOT_APPLICABLE",
                        time_semantics="NOT_APPLICABLE",
                        join_semantics="NOT_APPLICABLE",
                    ),
                ),
                created_at=utcnow(),
            )
            definition = CustomDefinition(
                definition_id=definition_id,
                owner_user_id="alice",
                axes=DefinitionAxes(
                    confirmation="CONFIRMED",
                    governance="GOVERNANCE_CANDIDATE",
                ),
                current_version=version,
            )
            await store.put_definition(definition=definition)
            await store.put_lifecycle(
                definition_id=definition_id,
                version=1,
                lifecycle=DefinitionVersionLifecycle(
                    confirmation="CONFIRMED", retention="SAVED"
                ),
            )
            await store.put_version(version=version)
            # put_version is IDEMPOTENT for an identical exact version...
            await store.put_version(version=version)

            restored = await store.get_definition(definition_id=definition_id)
            assert restored == definition
            assert restored is not None
            # Governance/Authority are ORTHOGONAL axes and must survive too.
            assert restored.axes.governance == "GOVERNANCE_CANDIDATE"
            assert restored.axes.authority == "noncanonical"
            restored_exact = await store.get_version(
                definition_id=definition_id, version=1
            )
            assert restored_exact is not None
            assert restored_exact.semantics == version.semantics
            assert restored_exact.semantics is not None
            assert version.semantics is not None
            assert (
                restored_exact.semantics.execution_applicability
                == version.semantics.execution_applicability
            )
            assert await store.list_versions(definition_id=definition_id) == (
                version,
            )
            lifecycle = await store.get_lifecycle(
                definition_id=definition_id, version=1
            )
            assert lifecycle is not None
            assert (lifecycle.confirmation, lifecycle.retention) == (
                "CONFIRMED",
                "SAVED",
            )
            listed = await store.list_definitions()
            assert definition in listed
        finally:
            await store.close()

    _run_async(scenario())


def test_legacy_semantic_jsonb_without_applicability_loads_without_rewrite(
    control_postgres: dict[str, Any],
) -> None:
    from src.nl2sql.artifacts.custom_definition import (
        DefinitionVersion,
        derive_parameter_contract,
        utcnow,
    )

    definition_id = _definition_id("legacy-applicability-payload")
    semantics = DefinitionSemantics(population="eligible service visits")
    version = DefinitionVersion(
        definition_id=definition_id,
        version=1,
        calculation=_spec(),
        parameter_contract=derive_parameter_contract(_spec()),
        title="Legacy semantic version",
        semantic_closed=True,
        semantics=semantics,
        created_at=utcnow(),
    )
    legacy_payload = version.model_dump(mode="json")
    legacy_semantics = legacy_payload["semantics"]
    assert isinstance(legacy_semantics, dict)
    legacy_semantics.pop("execution_applicability")
    legacy_json = json.dumps(
        legacy_payload, ensure_ascii=False, separators=(",", ":")
    )
    _psql(
        control_postgres,
        "INSERT INTO custom_definitions ("
        "definition_id, owner_user_id, confirmation, retention, publication,"
        "certification, current_version, current_payload"
        ") VALUES ("
        f"'{definition_id}', 'alice', 'CONFIRMED', 'SESSION', 'UNPUBLISHED',"
        f"'UNCERTIFIED', 1, '{legacy_json}'::jsonb);",
    )
    _psql(
        control_postgres,
        "INSERT INTO custom_definition_versions ("
        "definition_id, version, payload, checksum"
        ") VALUES ("
        f"'{definition_id}', 1, '{legacy_json}'::jsonb, '{version.checksum}');",
    )

    async def scenario() -> None:
        store = ControlDefinitionStore(database_url=_async_dsn(control_postgres))
        try:
            restored = await store.get_version(definition_id=definition_id, version=1)
            assert restored is not None
            assert restored.semantics == semantics
            assert restored.semantics is not None
            assert restored.semantics.execution_applicability is None
            assert restored.checksum == version.checksum
        finally:
            await store.close()

    _run_async(scenario())
    stored_marker = _psql(
        control_postgres,
        "SELECT (payload->'semantics') ? 'execution_applicability' "
        "FROM custom_definition_versions "
        f"WHERE definition_id = '{definition_id}' AND version = 1;",
    )
    assert stored_marker.stdout.strip() == "f"


def test_control_alembic_chain_upgrades_008_then_009(
    control_postgres: dict[str, Any],
) -> None:
    """Exercise the real 007 -> 008 -> 009 Alembic path on isolated PG17."""

    database = "ttai_control_alembic"
    _owner_psql(
        control_postgres,
        f"CREATE DATABASE {database};",
        database="postgres",
    )
    container = str(control_postgres["container"])
    for migration in CONTROL_MIGRATIONS[:7]:
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
            database,
            "-q",
            "-f",
            f"/tmp/{migration}",
            timeout=120,
        )

    environment = os.environ.copy()
    environment["CONTROL_MIGRATOR_DATABASE_URL"] = (
        f"postgresql+psycopg://{CONTROL_OWNER}:{CONTROL_OWNER_PASSWORD}"
        f"@127.0.0.1:{control_postgres['port']}/{database}"
    )
    environment.pop("CONTROL_MIGRATOR_DATABASE_URL_FILE", None)

    def alembic(*arguments: str) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ROOT / "docker/alembic/control.ini"),
                *arguments,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, (
            f"Alembic {' '.join(arguments)} failed with exit {result.returncode}"
        )

    alembic("stamp", "007_confirmation_audit")
    alembic("upgrade", "008_definition_run_records")
    assert _owner_psql(
        control_postgres,
        "SELECT version_num FROM alembic_version;",
        database=database,
    ).stdout.strip() == "008_definition_run_records"
    assert _owner_psql(
        control_postgres,
        "SELECT count(*) FROM schema_migrations "
        "WHERE version = '008_definition_run_records';",
        database=database,
    ).stdout.strip() == "1"
    assert _owner_psql(
        control_postgres,
        "SELECT to_regclass('public.hitl_action_claims') IS NULL;",
        database=database,
    ).stdout.strip() == "t"

    alembic("upgrade", "009_hitl_action_claims")
    assert _owner_psql(
        control_postgres,
        "SELECT version_num FROM alembic_version;",
        database=database,
    ).stdout.strip() == "009_hitl_action_claims"
    assert _owner_psql(
        control_postgres,
        "SELECT count(*) FROM schema_migrations WHERE version IN ("
        "'008_definition_run_records', '009_hitl_action_claims');",
        database=database,
    ).stdout.strip() == "2"
    assert _owner_psql(
        control_postgres,
        "SELECT to_regclass('public.hitl_action_claims') IS NOT NULL;",
        database=database,
    ).stdout.strip() == "t"


def test_definition_run_records_are_owner_scoped_immutable_and_durable(
    control_postgres: dict[str, Any], control_backend: None
) -> None:
    from src.nl2sql.container import AppContainer

    async def scenario() -> tuple[str, str, str]:
        writer = AppContainer(
            governed_metric_input_fetcher=_RunRecordInputFetcher(),
            governed_metric_key_resolver=_resolver,
        )
        await writer.start()
        try:
            assert writer.product_store_available is True
            definitions = writer.custom_definition_service()
            draft = await definitions.create_draft(
                owner_user_id="alice", title="Run record", calculation=_spec()
            )
            await definitions.mark_semantic_closed(
                owner_user_id="alice", definition_id=draft.definition_id
            )
            await definitions.confirm(
                owner_user_id="alice", definition_id=draft.definition_id
            )
            await definitions.save(
                owner_user_id="alice", definition_id=draft.definition_id
            )

            outcome = await writer.custom_definition_execution_service().execute(
                owner_user_id="alice",
                definition_id=draft.definition_id,
                version=1,
                binding=CalculationExecutionBinding(
                    calculation_id=_spec().calculation_id,
                    spec_checksum=_spec().checksum,
                    parameters=(),
                ),
            )
            assert outcome.result.value == Decimal("1")

            store = writer.custom_definition_execution_service().run_records
            records = await store.list_for_version(
                owner_user_id="alice", definition_id=draft.definition_id, version=1
            )
            assert len(records) == 1
            record = records[0]
            assert record.owner_user_id == "alice"
            assert record.receipt is not None

            # A strict gate with no current-authority evidence refuses before
            # input resolution, but the refusal itself is durably queryable.
            refused_service = CustomDefinitionExecutionService(
                definitions=definitions,
                input_resolver=writer.calculation_input_resolver(),
                revalidation=DefinitionRevalidationGate(
                    governed_metric_authority=definitions._is_governed_metric_key,
                    strict=True,
                ),
                run_records=store,
            )
            refused = await refused_service.execute_revalidated(
                owner_user_id="alice",
                definition_id=draft.definition_id,
                version=1,
                binding=CalculationExecutionBinding(
                    calculation_id=_spec().calculation_id,
                    spec_checksum=_spec().checksum,
                    parameters=(),
                ),
            )
            assert isinstance(refused, CustomDefinitionExecutionRefused)
            refused_record = await store.get(
                owner_user_id="alice",
                definition_id=draft.definition_id,
                version=1,
                run_id=refused.run_id,
            )
            assert refused_record is not None
            assert refused_record.receipt is None
            assert refused_record.validation.branch == "UNAVAILABLE"

            # Exact retry is idempotent and does not add a second record.
            await store.put(record=record)
            assert len(
                await store.list_for_version(
                    owner_user_id="alice",
                    definition_id=draft.definition_id,
                    version=1,
                )
            ) == 2

            with pytest.raises(DefinitionRunRecordConflict):
                await store.put(
                    record=record.model_copy(
                        update={"degradations": ("changed",)}
                    )
                )
            assert await store.get(
                owner_user_id="bob",
                definition_id=draft.definition_id,
                version=1,
                run_id=record.run_id,
            ) is None

            # Concurrent exact retries resolve to one immutable row.
            concurrent = record.model_copy(update={"run_id": new_run_id()})
            await asyncio.gather(
                store.put(record=concurrent), store.put(record=concurrent)
            )
            assert len(
                await store.list_for_version(
                    owner_user_id="alice",
                    definition_id=draft.definition_id,
                    version=1,
                )
            ) == 3

            # A foreign owner cannot insert a record for Alice's definition;
            # the database FK rejects the write and the transaction leaves no row.
            foreign = record.model_copy(
                update={"owner_user_id": "bob", "run_id": new_run_id()}
            )
            with pytest.raises(DefinitionRunRecordConflict):
                await store.put(record=foreign)
            assert await store.get(
                owner_user_id="bob",
                definition_id=draft.definition_id,
                version=1,
                run_id=foreign.run_id,
            ) is None

            version_mismatch = record.model_copy(
                update={"version": 2, "run_id": new_run_id()}
            )
            with pytest.raises(DefinitionRunRecordConflict):
                await store.put(record=version_mismatch)
            assert await store.get(
                owner_user_id="alice",
                definition_id=draft.definition_id,
                version=2,
                run_id=version_mismatch.run_id,
            ) is None
            return (
                draft.definition_id,
                record.run_id,
                concurrent.run_id,
                refused.run_id,
            )
        finally:
            await writer.close()

    definition_id, run_id, concurrent_run_id, refused_run_id = _run_async(
        scenario()
    )

    async def restart_read() -> None:
        reader = AppContainer(governed_metric_key_resolver=_resolver)
        await reader.start()
        try:
            assert reader.product_store_available is True
            records = await reader.custom_definition_execution_service().run_records.list_for_version(
                owner_user_id="alice", definition_id=definition_id, version=1
            )
            assert {record.run_id for record in records} == {
                run_id,
                concurrent_run_id,
                refused_run_id,
            }
            refused_after_restart = next(
                record for record in records if record.run_id == refused_run_id
            )
            assert refused_after_restart.receipt is None
            fetched = await reader.custom_definition_execution_service().run_records.get(
                owner_user_id="alice",
                definition_id=definition_id,
                version=1,
                run_id=run_id,
            )
            assert fetched is not None
            assert fetched.owner_user_id == "alice"
        finally:
            await reader.close()

    _run_async(restart_read())

    indexes = _owner_psql(
        control_postgres,
        "SELECT string_agg(indexname, ',' ORDER BY indexname) "
        "FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname IN ("
        "'custom_definitions_identity_owner_uidx', "
        "'definition_run_records_identity', "
        "'definition_run_records_version_created_idx');",
    )
    assert indexes.stdout.strip() == (
        "custom_definitions_identity_owner_uidx,"
        "definition_run_records_identity,"
        "definition_run_records_version_created_idx"
    )

    blocked_update = _psql(
        control_postgres,
        "UPDATE definition_run_records "
        "SET payload_checksum = payload_checksum "
        "WHERE owner_user_id = 'alice' "
        f"AND definition_id = '{definition_id}' AND version = 1 "
        f"AND run_id = '{run_id}';",
        check=False,
    )
    assert blocked_update.returncode != 0
    assert "definition_run_record_append_only" in blocked_update.stderr

    blocked_delete = _psql(
        control_postgres,
        "DELETE FROM definition_run_records "
        "WHERE owner_user_id = 'alice' "
        f"AND definition_id = '{definition_id}' AND version = 1 "
        f"AND run_id = '{run_id}';",
        check=False,
    )
    assert blocked_delete.returncode != 0
    assert "definition_run_record_append_only" in blocked_delete.stderr
    still_present = _owner_psql(
        control_postgres,
        "SELECT count(*) FROM definition_run_records "
        "WHERE owner_user_id = 'alice' "
        f"AND definition_id = '{definition_id}' AND version = 1 "
        f"AND run_id = '{run_id}';",
    )
    assert still_present.stdout.strip() == "1"

    # Simulate out-of-band corruption: one row gets a bad checksum and another
    # remains JSON-shaped but no longer decodes as the declared strict model.
    _owner_psql(
        control_postgres,
        "ALTER TABLE definition_run_records DISABLE TRIGGER "
        "definition_run_records_append_only; "
        "UPDATE definition_run_records SET payload_checksum = repeat('0', 64) "
        f"WHERE run_id = '{run_id}'; "
        "UPDATE definition_run_records SET "
        "payload = jsonb_set(payload, '{validation}', 'null'::jsonb) "
        f"WHERE run_id = '{concurrent_run_id}'; "
        "ALTER TABLE definition_run_records ENABLE TRIGGER "
        "definition_run_records_append_only;",
    )

    async def reject_corruption() -> None:
        store = ControlDefinitionRunRecordStore(database_url=_async_dsn(control_postgres))
        try:
            with pytest.raises(DefinitionRunRecordIntegrityError):
                await store.get(
                    owner_user_id="alice",
                    definition_id=definition_id,
                    version=1,
                    run_id=run_id,
                )
            with pytest.raises(DefinitionRunRecordIntegrityError):
                await store.get(
                    owner_user_id="alice",
                    definition_id=definition_id,
                    version=1,
                    run_id=concurrent_run_id,
                )
        finally:
            await store.close()

    _run_async(reject_corruption())
