"""Docker PostgreSQL contract for the Control-PG publication catalogue.

The catalogue is gated exactly like the governance contract: nothing runs unless
TTAI_RUN_POSTGRES_INTEGRATION=1, and the container is created, migrated with
001..005 and torn down by this module alone.

What is proven here is BEHAVIOURAL EQUIVALENCE with the process-local port
(src/nl2sql/artifacts/publication.py), including the frozen error codes, plus the
three invariants the DATABASE owns: an immutable published version, a
certification axis that always names its certifier, and an explicit monotonic
current-version pointer that can never dangle.
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
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.settings import get_settings
from src.nl2sql.artifacts.custom_definition import ParameterContract
from src.nl2sql.artifacts.publication import (
    PublicationCatalogue,
    PublishedSemanticPackage,
    PublishedVersion,
)
from src.nl2sql.artifacts.publication_control_store import (
    ControlPublicationCatalogue,
    PublicationIntegrityError,
)
from src.nl2sql.semantic.calculation_contract import (
    AggregateOperand,
    CalculationInputSpec,
    CalculationSpec,
    CaseOperand,
    CompareOperand,
    LiteralOperand,
    NullLiteralOperand,
    ParameterSpec,
    RoundOperand,
    WhenBranch,
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
)
APPLICATION_NAME = "ttai-publication-race"
IDENTITY = "metric.publication.control"


def _identity(label: str) -> str:
    """One identity per test, so the module-scoped container stays independent."""

    return f"{IDENTITY}.{label}"


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

    container = f"ttai-publication-{uuid.uuid4().hex[:10]}"
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
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
            f" TO {CONTROL_APP};",
            role=CONTROL_OWNER,
            password=CONTROL_OWNER_PASSWORD,
        )
        applied = _psql(
            stack,
            "SELECT count(*) FROM schema_migrations;",
            role=CONTROL_OWNER,
            password=CONTROL_OWNER_PASSWORD,
        )
        assert applied.stdout.strip() == str(len(CONTROL_MIGRATIONS))
        stack["port"] = _published_port(container)
        yield stack
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=60)


@pytest.fixture
def demo_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the runtime Settings constructible for the database_url path only."""

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _run(control_postgres: dict[str, Any], scenario: Any) -> Any:
    """Run one scenario against a fresh engine bound to the module container."""

    async def runner() -> Any:
        engine = create_async_engine(
            _async_dsn(control_postgres),
            pool_size=4,
            max_overflow=0,
            pool_pre_ping=True,
        )
        catalogue = ControlPublicationCatalogue(engine=engine)
        try:
            return await scenario(catalogue)
        finally:
            await catalogue.close()

    return _run_async(runner())


def _spec(calculation_id: str) -> CalculationSpec:
    return CalculationSpec(
        calculation_id=calculation_id,
        expression=AggregateOperand(function="count_distinct", role="base"),
        inputs=(
            CalculationInputSpec(
                role="base",
                provenance="published_gold",
                metric_key="complaint_count",
            ),
        ),
        parameters=(
            ParameterSpec(
                name="period",
                value_type="string",
                allowed_values=("mtd", "ytd"),
            ),
        ),
        unit="count",
    )


def _rich_spec() -> CalculationSpec:
    """A spec that exercises the whole round trip: CASE, compare, ROUND, Decimal."""

    return CalculationSpec(
        calculation_id="calc.publication.control.rich",
        expression=CaseOperand(
            whens=(
                WhenBranch(
                    condition=CompareOperand(
                        operator="gt",
                        left=AggregateOperand(function="sum", role="base"),
                        right=LiteralOperand(value=Decimal("2.50")),
                    ),
                    then=RoundOperand(
                        operand=LiteralOperand(value=Decimal("0.125")), digits=3
                    ),
                ),
            ),
            otherwise=NullLiteralOperand(),
        ),
        inputs=(
            CalculationInputSpec(
                role="base",
                provenance="definition_backed_computed",
                metric_key="complaint_count",
            ),
        ),
        parameters=(
            ParameterSpec(
                name="period",
                value_type="string",
                required=True,
                allowed_values=("mtd", "ytd"),
                description="reporting window",
            ),
        ),
        unit="percent",
        precision=3,
        rounding="half_even",
    )


def _checksum(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def _fixture_version(
    identity_id: str,
    version: int,
    *,
    title: str | None = None,
    value: str | None = None,
    derived_from: tuple[str, int] | None = None,
) -> PublishedVersion:
    """A display/install-only legacy entry: it carries NO semantic package."""

    return PublishedVersion(
        identity_id=identity_id,
        version=version,
        title=title or identity_id,
        owner_user_id="fixture-owner",
        owner_label="Fixture Owner",
        source_label="fixture",
        definition_checksum=_checksum("fixture", identity_id, str(version)),
        published_at="fixture",
        unit="ratio",
        value=value,
        derived_from_identity=None if derived_from is None else derived_from[0],
        derived_from_version=None if derived_from is None else derived_from[1],
    )


def _semantic_version(
    identity_id: str,
    version: int,
    *,
    spec: CalculationSpec | None = None,
    title: str | None = None,
) -> PublishedVersion:
    calculation = spec or _spec(f"calc.publication.control.{version}")
    return PublishedVersion(
        identity_id=identity_id,
        version=version,
        title=title or identity_id,
        owner_user_id="publication-owner",
        owner_label="Publication Owner",
        source_label="local_demo",
        definition_checksum=_checksum(identity_id, str(version)),
        published_at=f"2026-02-{version:02d}T00:00:00Z",
        unit=calculation.unit,
        semantic=PublishedSemanticPackage(
            calculation=calculation,
            parameter_contract=ParameterContract(parameters=tuple(calculation.parameters)),
            source_definition_id="def_" + "a" * 32,
            source_definition_version=version,
            source_definition_checksum="b" * 64,
        ),
    )


def _observed(value: Any) -> Any:
    """A serialization-independent view of one returned value."""

    if isinstance(value, PublishedVersion):
        semantic = value.semantic
        return (
            value.identity_id,
            value.version,
            value.title,
            value.owner_user_id,
            value.owner_label,
            value.source_label,
            value.definition_checksum,
            value.published_at,
            value.unit,
            value.value,
            value.derived_from_identity,
            value.derived_from_version,
            value.forkable,
            None
            if semantic is None
            else (
                semantic.calculation.checksum,
                semantic.parameter_contract.checksum,
                semantic.source_definition_id,
                semantic.source_definition_version,
                semantic.source_definition_checksum,
            ),
        )
    if isinstance(value, tuple):
        return tuple(_observed(item) for item in value)
    return value


async def _record(steps: list[tuple[str, Any]], label: str, call: Any) -> None:
    try:
        result = await call()
    except (LookupError, ValueError) as exc:
        steps.append((label, f"{type(exc).__name__}:{exc}"))
    else:
        steps.append((label, _observed(result)))


async def _trace(catalogue: Any) -> list[tuple[str, Any]]:
    """One scripted session, driven identically against BOTH implementations."""

    steps: list[tuple[str, Any]] = []
    identity = _identity("differential")
    v1 = _fixture_version(identity, 1)
    v2 = _semantic_version(identity, 2)
    v3 = _semantic_version(identity, 3)

    async def scoped_identities() -> tuple[str, ...]:
        # Other tests share the module container, so only THIS identity is
        # compared; the full listing has its own assertions elsewhere.
        return tuple(
            candidate
            for candidate in await catalogue.identities()
            if candidate == identity
        )

    await _record(steps, "get_unknown", lambda: catalogue.get(identity, 1))
    await _record(steps, "versions_unknown", lambda: catalogue.versions(identity))
    await _record(steps, "identities_before", scoped_identities)
    await _record(steps, "current_unknown", lambda: catalogue.current_version(identity))
    await _record(steps, "title_unknown", lambda: catalogue.title(identity))
    await _record(
        steps, "certification_unknown", lambda: catalogue.certification_state(identity, 1)
    )
    await _record(steps, "certified_by_unknown", lambda: catalogue.certified_by(identity, 1))
    await _record(steps, "withdrawn_unknown", lambda: catalogue.is_withdrawn(identity, 1))

    await _record(steps, "seed_v1_current", lambda: catalogue.seed(v1, current=True))
    await _record(steps, "seed_v1_idempotent", lambda: catalogue.seed(v1, current=True))
    await _record(
        steps,
        "seed_v1_overwrite_refused",
        lambda: catalogue.seed(_fixture_version(identity, 1, title="replacement")),
    )
    await _record(steps, "current_after_seed", lambda: catalogue.current_version(identity))
    await _record(steps, "title_after_seed", lambda: catalogue.title(identity))
    await _record(steps, "seed_v2_current", lambda: catalogue.seed(v2, current=True))
    await _record(steps, "seed_v3_historical", lambda: catalogue.seed(v3, current=False))
    await _record(steps, "current_after_advance", lambda: catalogue.current_version(identity))
    await _record(steps, "seed_v1_regression", lambda: catalogue.seed(v1, current=True))
    await _record(steps, "publish_existing", lambda: catalogue.publish(v2))
    await _record(
        steps,
        "publish_without_semantics",
        lambda: catalogue.publish(_fixture_version(identity, 4)),
    )
    await _record(steps, "publish_v4", lambda: catalogue.publish(_semantic_version(identity, 4)))
    await _record(steps, "current_after_publish", lambda: catalogue.current_version(identity))
    await _record(steps, "versions", lambda: catalogue.versions(identity))
    await _record(steps, "identities", scoped_identities)
    await _record(
        steps,
        "certify_v4",
        lambda: catalogue.certify_local_demo(identity, 4, certified_by="demo-certifier"),
    )
    await _record(steps, "certification_v4", lambda: catalogue.certification_state(identity, 4))
    await _record(steps, "certified_by_v4", lambda: catalogue.certified_by(identity, 4))
    await _record(steps, "withdrawn_v4", lambda: catalogue.is_withdrawn(identity, 4))
    await _record(steps, "withdraw_v4", lambda: catalogue.withdraw(identity, 4))
    await _record(
        steps, "certification_after_withdrawal", lambda: catalogue.certification_state(identity, 4)
    )
    await _record(steps, "withdrawn_after_withdrawal", lambda: catalogue.is_withdrawn(identity, 4))
    await _record(
        steps,
        "certify_missing_version",
        lambda: catalogue.certify_local_demo(identity, 9, certified_by="demo-certifier"),
    )
    await _record(steps, "withdraw_missing_version", lambda: catalogue.withdraw(identity, 9))
    await _record(steps, "get_v1", lambda: catalogue.get(identity, 1))
    await _record(steps, "get_v4", lambda: catalogue.get(identity, 4))
    return steps


def test_construction_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError) as failure:
        ControlPublicationCatalogue()
    assert str(failure.value) == "provide exactly one of database_url or engine"

    engine = create_async_engine("postgresql+asyncpg://control_app:unused@127.0.0.1:1/db")
    try:
        with pytest.raises(ValueError) as both:
            ControlPublicationCatalogue(
                "postgresql+asyncpg://control_app:unused@127.0.0.1:1/db", engine=engine
            )
        assert str(both.value) == "provide exactly one of database_url or engine"
    finally:
        _run_async(engine.dispose())


def test_a_privileged_role_dsn_is_refused(
    control_postgres: dict[str, Any], demo_settings: None
) -> None:
    owner_dsn = (
        f"postgresql+asyncpg://{CONTROL_OWNER}:{CONTROL_OWNER_PASSWORD}"
        f"@127.0.0.1:{control_postgres['port']}/{CONTROL_DATABASE}"
    )
    with pytest.raises(ValueError) as failure:
        ControlPublicationCatalogue(owner_dsn)
    assert str(failure.value) == "control_app database URL uses a privileged role"


def test_a_control_app_database_url_builds_the_catalogue(
    control_postgres: dict[str, Any], demo_settings: None
) -> None:
    async def scenario() -> None:
        catalogue = ControlPublicationCatalogue(_async_dsn(control_postgres))
        try:
            item = _semantic_version(_identity("dsn"), 1)
            await catalogue.publish(item)
            assert await catalogue.current_version(item.identity_id) == 1
            assert await catalogue.get(item.identity_id, 1) == item
        finally:
            await catalogue.close()

    _run_async(scenario())


def test_seed_is_idempotent_and_refuses_to_overwrite(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("seed")
        item = _fixture_version(identity, 1)
        assert await catalogue.seed(item) == item
        assert await catalogue.seed(item) == item
        assert await catalogue.get(identity, 1) == item
        assert len(await catalogue.versions(identity)) == 1

        with pytest.raises(ValueError) as failure:
            await catalogue.seed(_fixture_version(identity, 1, title="replacement"))
        assert str(failure.value) == "seed refuses to overwrite an existing publication"
        assert await catalogue.get(identity, 1) == item

    _run(control_postgres, scenario)


def test_seed_current_pointer_advances_and_never_rolls_back(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("monotonic")
        await catalogue.seed(_fixture_version(identity, 1), current=True)
        assert await catalogue.current_version(identity) == 1
        await catalogue.seed(_semantic_version(identity, 2), current=True)
        assert await catalogue.current_version(identity) == 2
        await catalogue.seed(_semantic_version(identity, 3), current=False)
        assert await catalogue.current_version(identity) == 2
        assert await catalogue.seed(_semantic_version(identity, 2), current=True) == (
            await catalogue.get(identity, 2)
        )
        with pytest.raises(ValueError) as failure:
            await catalogue.seed(_fixture_version(identity, 1), current=True)
        assert str(failure.value) == "publication_current_version_regression"
        assert await catalogue.current_version(identity) == 2

    _run(control_postgres, scenario)


def test_current_version_requires_an_explicit_pointer(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("unbound")
        with pytest.raises(LookupError) as unknown:
            await catalogue.current_version(identity)
        assert str(unknown.value) == "publication_identity_not_found"

        await catalogue.seed(_fixture_version(identity, 1), current=False)
        assert identity in await catalogue.identities()
        assert await catalogue.title(identity) == identity
        with pytest.raises(LookupError) as unbound:
            await catalogue.current_version(identity)
        assert str(unbound.value) == "publication_current_version_unbound"

        # A missing pointer is never repaired by inference, by seed or publish.
        with pytest.raises(LookupError) as seed_unbound:
            await catalogue.seed(_semantic_version(identity, 2), current=True)
        assert str(seed_unbound.value) == "publication_current_version_unbound"
        with pytest.raises(LookupError) as publish_unbound:
            await catalogue.publish(_semantic_version(identity, 2))
        assert str(publish_unbound.value) == "publication_current_version_unbound"
        assert len(await catalogue.versions(identity)) == 1

    _run(control_postgres, scenario)


def test_publish_is_immutable_and_requires_a_semantic_package(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("publish")
        item = _semantic_version(identity, 1)
        assert await catalogue.publish(item) == item
        assert await catalogue.certification_state(identity, 1) == "uncertified"

        with pytest.raises(ValueError) as duplicate:
            await catalogue.publish(item)
        assert str(duplicate.value) == "published version is immutable"

        # The immutability refusal precedes the semantic requirement.
        with pytest.raises(ValueError) as existing_without_semantics:
            await catalogue.publish(_fixture_version(identity, 1))
        assert str(existing_without_semantics.value) == "published version is immutable"

        with pytest.raises(ValueError) as semantics:
            await catalogue.publish(_fixture_version(identity, 2))
        assert str(semantics.value) == "publication requires a semantic package"
        assert await catalogue.get(identity, 2) is None
        assert len(await catalogue.versions(identity)) == 1

    _run(control_postgres, scenario)


def test_publish_historical_version_preserves_the_pointer(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("historical")
        await catalogue.publish(_semantic_version(identity, 2))
        assert await catalogue.current_version(identity) == 2
        await catalogue.publish(_semantic_version(identity, 1))
        assert await catalogue.current_version(identity) == 2
        assert [item.version for item in await catalogue.versions(identity)] == [1, 2]
        listing = await catalogue.identities()
        assert identity in listing
        assert list(listing) == sorted(listing)

    _run(control_postgres, scenario)


def test_certification_and_withdrawal_are_independent_axes(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("axes")
        await catalogue.publish(_semantic_version(identity, 1))
        await catalogue.publish(_semantic_version(identity, 2))
        semantic = await catalogue.get(identity, 2)

        assert await catalogue.certification_state(identity, 2) == "uncertified"
        assert await catalogue.certified_by(identity, 2) is None
        assert await catalogue.is_withdrawn(identity, 2) is False

        await catalogue.certify_local_demo(identity, 1, certified_by="demo-certifier")
        assert await catalogue.certification_state(identity, 1) == "certified"
        assert await catalogue.certified_by(identity, 1) == "demo-certifier"
        assert await catalogue.is_withdrawn(identity, 1) is False

        await catalogue.withdraw(identity, 1)
        assert await catalogue.is_withdrawn(identity, 1) is True
        assert await catalogue.certification_state(identity, 1) == "certified"
        assert await catalogue.certified_by(identity, 1) == "demo-certifier"

        await catalogue.certify_local_demo(identity, 2, certified_by="second-certifier")
        await catalogue.withdraw(identity, 2)
        assert await catalogue.certification_state(identity, 2) == "certified"
        assert await catalogue.certified_by(identity, 2) == "second-certifier"
        assert await catalogue.is_withdrawn(identity, 2) is True

        # Neither axis ever touches the immutable semantic package.
        assert await catalogue.get(identity, 2) == semantic
        assert await catalogue.current_version(identity) == 2

    _run(control_postgres, scenario)


def test_missing_versions_report_the_frozen_error_codes(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("missing")
        with pytest.raises(LookupError) as certify:
            await catalogue.certify_local_demo(identity, 1, certified_by="demo-certifier")
        assert str(certify.value) == "publication_version_not_found"
        with pytest.raises(LookupError) as withdraw:
            await catalogue.withdraw(identity, 1)
        assert str(withdraw.value) == "publication_version_not_found"

        assert await catalogue.get(identity, 1) is None
        assert await catalogue.certification_state(identity, 1) == "uncertified"
        assert await catalogue.certified_by(identity, 1) is None
        assert await catalogue.is_withdrawn(identity, 1) is False

    _run(control_postgres, scenario)


def test_semantic_package_round_trips_completely(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("roundtrip")
        calculation = _rich_spec()
        item = _semantic_version(identity, 1, spec=calculation)
        await catalogue.publish(item)

        restored = await catalogue.get(identity, 1)
        assert restored is not None
        assert restored == item
        assert restored.forkable is True
        assert restored.semantic is not None
        assert restored.semantic.calculation == calculation
        assert restored.semantic.calculation.checksum == calculation.checksum
        # The published-package invariant survives the round trip.
        assert restored.semantic.parameter_contract.parameters == tuple(
            restored.semantic.calculation.parameters
        )
        assert restored.semantic.parameter_contract == item.semantic.parameter_contract

        expression = restored.semantic.calculation.expression
        assert isinstance(expression, CaseOperand)
        condition = expression.whens[0].condition
        assert isinstance(condition, CompareOperand)
        assert isinstance(condition.right, LiteralOperand)
        assert condition.right.value == Decimal("2.50")
        assert isinstance(expression.otherwise, NullLiteralOperand)

        # A display/install-only legacy entry keeps its value and derivation.
        legacy = _fixture_version(
            identity, 2, value="0.37", derived_from=("metric.source.control", 4)
        )
        assert await catalogue.seed(legacy) == legacy
        restored_legacy = await catalogue.get(identity, 2)
        assert restored_legacy == legacy
        assert restored_legacy is not None
        assert restored_legacy.semantic is None
        assert restored_legacy.forkable is False
        assert restored_legacy.value == "0.37"
        assert restored_legacy.derived_from_identity == "metric.source.control"
        assert restored_legacy.derived_from_version == 4

    _run(control_postgres, scenario)


def test_a_lost_primary_key_race_is_reported_as_immutability(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            _async_dsn(control_postgres),
            pool_size=4,
            max_overflow=0,
            pool_pre_ping=True,
            connect_args={"server_settings": {"application_name": APPLICATION_NAME}},
        )
        catalogue = ControlPublicationCatalogue(engine=engine)
        identity = _identity("race")
        item = _semantic_version(identity, 1)
        try:
            async with engine.connect() as blocker:
                transaction = await blocker.begin()
                # The same immutable version is already written but not yet
                # visible to any other reader.
                await catalogue._insert_version(blocker, item)
                publisher = asyncio.create_task(catalogue.publish(item))
                for _ in range(200):
                    blocked = (
                        await blocker.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name = :name "
                                "AND wait_event_type = 'Lock'"
                            ),
                            {"name": APPLICATION_NAME},
                        )
                    ).scalar_one()
                    if blocked:
                        break
                    await asyncio.sleep(0.05)
                await transaction.commit()
            with pytest.raises(ValueError) as failure:
                await publisher
            assert str(failure.value) == "published version is immutable"
            # The refusal comes from the DATABASE key conflict, not the pre-check.
            assert isinstance(failure.value.__cause__, IntegrityError)
            assert len(await catalogue.versions(identity)) == 1
        finally:
            await catalogue.close()

    _run_async(scenario())


def test_direct_sql_cannot_rewrite_a_published_version(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("immutable")
        item = _semantic_version(identity, 1)
        await catalogue.publish(item)

        rewritten = _psql(
            control_postgres,
            "UPDATE product_publication_versions SET semantic = '{}'::jsonb "
            f"WHERE identity_id = '{identity}' AND version = 1;",
            check=False,
        )
        assert rewritten.returncode != 0
        assert "published_version_is_immutable" in rewritten.stderr

        retitled = _psql(
            control_postgres,
            "UPDATE product_publication_versions SET title = 'rewritten' "
            f"WHERE identity_id = '{identity}' AND version = 1;",
            check=False,
        )
        assert retitled.returncode != 0
        assert "published_version_is_immutable" in retitled.stderr

        anonymous = _psql(
            control_postgres,
            "UPDATE product_publication_versions SET certification = 'certified' "
            f"WHERE identity_id = '{identity}' AND version = 1;",
            check=False,
        )
        assert anonymous.returncode != 0
        assert "product_publication_certification_provenance" in anonymous.stderr

        assert await catalogue.get(identity, 1) == item
        assert await catalogue.certification_state(identity, 1) == "uncertified"

    _run(control_postgres, scenario)


def test_direct_sql_cannot_move_or_dangle_the_current_pointer(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("pointer")
        await catalogue.publish(_semantic_version(identity, 1))
        await catalogue.publish(_semantic_version(identity, 2))

        regression = _psql(
            control_postgres,
            "UPDATE product_publication_identities SET current_version = 1 "
            f"WHERE identity_id = '{identity}';",
            check=False,
        )
        assert regression.returncode != 0
        assert "publication_current_version_regression" in regression.stderr

        # publication_current_version_invalid (a pointer naming a missing
        # version) is therefore unreachable, and is preserved as a defensive
        # refusal instead of being inferred away.
        dangling = _psql(
            control_postgres,
            "INSERT INTO product_publication_identities "
            "(identity_id, title, current_version) VALUES "
            "('metric.publication.control.dangling', 'dangling', 7);",
            check=False,
        )
        assert dangling.returncode != 0
        assert "product_publication_current_exists" in dangling.stderr

        removed = _psql(
            control_postgres,
            "DELETE FROM product_publication_versions "
            f"WHERE identity_id = '{identity}' AND version = 2;",
            check=False,
        )
        assert removed.returncode != 0
        assert "product_publication_current_exists" in removed.stderr

        assert await catalogue.current_version(identity) == 2

    _run(control_postgres, scenario)


def test_direct_sql_incoherent_semantic_package_is_refused_on_read(
    control_postgres: dict[str, Any],
) -> None:
    async def scenario(catalogue: ControlPublicationCatalogue) -> None:
        identity = _identity("incoherent")
        incoherent = json.dumps(
            {
                "calculation": _rich_spec().model_dump(mode="json"),
                "parameter_contract": {"parameters": []},
                "source_definition_id": "def_" + "a" * 32,
                "source_definition_version": 1,
                "source_definition_checksum": "b" * 64,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # A direct writer CAN insert: the immutability trigger guards updates
        # only, and the JSONB column carries no cross-field check.
        _psql(
            control_postgres,
            "INSERT INTO product_publication_versions ("
            "identity_id, version, title, owner_user_id, owner_label, source_label,"
            "definition_checksum, published_at, unit, semantic"
            ") VALUES ("
            f"'{identity}', 1, '{identity}', 'owner', 'Owner', 'local_demo',"
            f"'{'a' * 64}', '2026-03-01T00:00:00Z', 'count', "
            f"'{incoherent}'::jsonb);",
        )
        _psql(
            control_postgres,
            "INSERT INTO product_publication_identities "
            "(identity_id, title, current_version) VALUES "
            f"('{identity}', '{identity}', 1);",
        )

        with pytest.raises(PublicationIntegrityError) as read:
            await catalogue.get(identity, 1)
        assert str(read.value) == "publication_semantic_package_incoherent"
        with pytest.raises(PublicationIntegrityError):
            await catalogue.versions(identity)
        with pytest.raises(PublicationIntegrityError):
            await catalogue.current_version(identity)

        # A package that is not even an object is refused the same way.
        malformed = _identity("malformed")
        _psql(
            control_postgres,
            "INSERT INTO product_publication_versions ("
            "identity_id, version, title, owner_user_id, owner_label, source_label,"
            "definition_checksum, published_at, unit, semantic"
            ") VALUES ("
            f"'{malformed}', 1, '{malformed}', 'owner', 'Owner', 'local_demo',"
            f"'{'a' * 64}', '2026-03-01T00:00:00Z', 'count', '[1,2,3]'::jsonb);",
        )
        with pytest.raises(PublicationIntegrityError):
            await catalogue.versions(malformed)

    _run(control_postgres, scenario)


def test_matches_the_in_memory_port_operation_for_operation(
    control_postgres: dict[str, Any],
) -> None:
    control_trace = _run(control_postgres, _trace)
    assert control_trace == _run_async(_trace(PublicationCatalogue()))


def _run_async(coroutine: Any) -> Any:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)
