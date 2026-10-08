"""Control-PG personal library contract on a REAL PostgreSQL 17 container.

Gate (identical to tests/integration/test_postgres_governance.py):
TTAI_RUN_POSTGRES_INTEGRATION=1, otherwise the whole module skips.

The module starts its OWN pgvector/pgvector:pg17 container, applies the frozen
docker/migrations/control/001..005 in order, and exercises
ControlLibraryRepository against it.  The composed PublicationCatalogue stays
the in-memory implementation: PUBLICATION state is delegated, PERSONAL state
(installs, Stars, withdrawal acknowledgement) is durable.

Every assertion below mirrors the behaviour of InMemoryLibraryRepository,
including the literal error codes, and the last test pins the two ports'
signatures and exception identities to each other.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from src.nl2sql.artifacts import library as memory_library
from src.nl2sql.artifacts.library import (
    CertificationUnavailable,
    InMemoryLibraryRepository,
    InstalledMetricBinding,
    LibraryIdentityNotFound,
)
from src.nl2sql.artifacts.library_control_store import (
    LOCAL_DEMO_CERTIFICATION_PROVENANCE,
    NOT_CONNECTED,
    PUBLICATION_WITHDRAWN,
    UPGRADE_REQUIRES_NEWER_VERSION,
    ControlLibraryRepository,
)
from src.nl2sql.artifacts.publication import (
    PublicationCatalogue,
    PublishedVersion,
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

IDENTITY = "demo.complaint_rate"
OTHER_IDENTITY = "demo.order_volume"
# (identity_id, version) rows the personal tables may reference through their
# foreign keys; the in-memory catalogue is seeded from the SAME fixture list.
PUBLICATION_FIXTURES = ((IDENTITY, 1), (IDENTITY, 2), (OTHER_IDENTITY, 1))
CURRENT_VERSIONS = {IDENTITY: 2, OTHER_IDENTITY: 1}

DEFAULT_CODE = "library_version_not_found"
IDENTITY_CODE = "library_identity_not_found"
INSTALL_REQUIRED = "library_install_required"
ACK_MISMATCH = "library_acknowledgement_version_mismatch"

# The container is module scoped, so every test owns its own users and can
# assert on an empty personal state without depending on test order.
USER_INSTALL = "user-install"
USER_ISOLATED_A = "user-isolated-a"
USER_ISOLATED_B = "user-isolated-b"
USER_ISOLATED_C = "user-isolated-c"
USER_UNKNOWN = "user-unknown"
USER_UPGRADE_A = "user-upgrade-a"
USER_UPGRADE_B = "user-upgrade-b"
USER_UPDATE_A = "user-update-a"
USER_UPDATE_B = "user-update-b"
USER_UPDATE_C = "user-update-c"
USER_STAR_A = "user-star-a"
USER_STAR_B = "user-star-b"
USER_STAR_C = "user-star-c"
USER_ACK_A = "user-ack-a"
USER_ACK_B = "user-ack-b"

REQUIRED_METHODS = (
    "current_version",
    "published_versions",
    "certification_state",
    "is_withdrawn",
    "forkable",
    "install",
    "uninstall",
    "get_install",
    "installs_of",
    "update_available",
    "upgrade",
    "star",
    "unstar",
    "star_count",
    "starred_of",
    "is_starred",
    "acknowledge_withdrawal",
    "is_acknowledged",
    "certify_local_demo",
    "withdraw",
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

    container = f"ttai-library-{uuid.uuid4().hex[:10]}"
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
        _seed_publication_rows(container)
        yield stack
    finally:
        _docker("rm", "--force", "--volumes", container, check=False, timeout=30)


def _seed_publication_rows(container: str) -> None:
    """Insert the catalogue rows the personal foreign keys must reference.

    The in-memory catalogue is the PUBLICATION authority for these tests; these
    rows exist so the frozen 005 foreign keys can be exercised for real.
    """

    statements = [
        "INSERT INTO product_publication_versions ("
        "identity_id, version, title, owner_user_id, owner_label, source_label, "
        "definition_checksum, published_at) VALUES ("
        f"'{identity_id}', {version}, '{identity_id}', 'fixture-owner', 'fixture', "
        f"'fixture', '{'0' * 64}', 'fixture') ON CONFLICT DO NOTHING;"
        for identity_id, version in PUBLICATION_FIXTURES
    ]
    statements.append(
        "INSERT INTO product_publication_identities (identity_id, title, current_version) "
        "VALUES "
        + ", ".join(
            f"('{identity_id}', '{identity_id}', {current})"
            for identity_id, current in CURRENT_VERSIONS.items()
        )
        + " ON CONFLICT DO NOTHING;"
    )
    seeded = _psql(container, "".join(statements))
    assert seeded.returncode == 0


def _control_dsn(stack: dict[str, Any]) -> str:
    return f"postgresql+asyncpg://{APP}:{APP_PASSWORD}@127.0.0.1:{stack['port']}/{DATABASE}"


def _publication(identity_id: str, version: int) -> PublishedVersion:
    return PublishedVersion(
        identity_id=identity_id,
        version=version,
        title=identity_id,
        owner_user_id="fixture-owner",
        owner_label="fixture",
        source_label="fixture",
        definition_checksum="0" * 64,
        published_at="fixture",
    )


async def _catalogue(
    current_versions: dict[str, int] | None = None,
) -> PublicationCatalogue:
    catalogue = PublicationCatalogue()
    current_map = dict(CURRENT_VERSIONS if current_versions is None else current_versions)
    ordered = tuple(
        sorted(
            PUBLICATION_FIXTURES,
            key=lambda item: 0 if current_map.get(item[0]) == item[1] else 1,
        )
    )
    for identity_id, version in ordered:
        await catalogue.seed(
            _publication(identity_id, version),
            current=current_map.get(identity_id) == version,
        )
    return catalogue


class _RecordingCatalogue(PublicationCatalogue):
    """Delegation probe: records every catalogue read/write it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def get(self, identity_id: str, version: int) -> PublishedVersion | None:
        self.calls.append("get")
        return await super().get(identity_id, version)

    async def versions(self, identity_id: str) -> tuple[PublishedVersion, ...]:
        self.calls.append("versions")
        return await super().versions(identity_id)

    async def current_version(self, identity_id: str) -> int:
        self.calls.append("current_version")
        return await super().current_version(identity_id)

    async def certification_state(self, identity_id: str, version: int) -> str:
        self.calls.append("certification_state")
        return await super().certification_state(identity_id, version)

    async def is_withdrawn(self, identity_id: str, version: int) -> bool:
        self.calls.append("is_withdrawn")
        return await super().is_withdrawn(identity_id, version)

    async def certify_local_demo(
        self, identity_id: str, version: int, *, certified_by: str
    ) -> None:
        self.calls.append("certify_local_demo")
        await super().certify_local_demo(identity_id, version, certified_by=certified_by)

    async def withdraw(self, identity_id: str, version: int) -> None:
        self.calls.append("withdraw")
        await super().withdraw(identity_id, version)


async def _recording_catalogue(
    current_versions: dict[str, int] | None = None,
) -> _RecordingCatalogue:
    catalogue = _RecordingCatalogue()
    current_map = dict(CURRENT_VERSIONS if current_versions is None else current_versions)
    ordered = tuple(
        sorted(
            PUBLICATION_FIXTURES,
            key=lambda item: 0 if current_map.get(item[0]) == item[1] else 1,
        )
    )
    for identity_id, version in ordered:
        await catalogue.seed(
            _publication(identity_id, version),
            current=current_map.get(identity_id) == version,
        )
    return catalogue


def _run_library(
    stack: dict[str, Any],
    body: Callable[
        [ControlLibraryRepository, PublicationCatalogue], Awaitable[None]
    ],
    *,
    current_versions: dict[str, int] | None = None,
) -> None:
    """Run one body against a fresh in-memory catalogue and a fresh repository."""

    async def runner() -> None:
        catalogue = await _catalogue(current_versions=current_versions)
        repository = ControlLibraryRepository(catalogue, _control_dsn(stack))
        try:
            await body(repository, catalogue)
        finally:
            await repository.close()

    _run_async(runner())


def _run_async(coroutine: Any) -> Any:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)


# --- installs ----------------------------------------------------------------


def test_install_uninstall_and_duplicate_install(control_stack: dict[str, Any]) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        binding = await repository.install(
            user_id=USER_INSTALL, identity_id=IDENTITY, version=1
        )
        assert binding == InstalledMetricBinding(identity_id=IDENTITY, version=1)
        assert (binding.identity_id, binding.version, binding.pinned) == (IDENTITY, 1, True)
        assert (
            await repository.get_install(user_id=USER_INSTALL, identity_id=IDENTITY)
            == binding
        )
        assert await repository.installs_of(user_id=USER_INSTALL) == (binding,)

        # A duplicate install is an idempotent re-pin, never a second row and
        # never an error (the in-memory dict assignment).
        again = await repository.install(
            user_id=USER_INSTALL, identity_id=IDENTITY, version=1
        )
        assert again == binding
        assert await repository.installs_of(user_id=USER_INSTALL) == (binding,)

        # A different version for the SAME identity REPLACES the install
        # without any explicit upgrade.
        replaced = await repository.install(
            user_id=USER_INSTALL, identity_id=IDENTITY, version=2
        )
        assert replaced.version == 2
        assert await repository.installs_of(user_id=USER_INSTALL) == (replaced,)
        stored = await repository.get_install(user_id=USER_INSTALL, identity_id=IDENTITY)
        assert stored is not None and stored.version == 2

        await repository.uninstall(user_id=USER_INSTALL, identity_id=IDENTITY)
        assert (
            await repository.get_install(user_id=USER_INSTALL, identity_id=IDENTITY)
            is None
        )
        assert await repository.installs_of(user_id=USER_INSTALL) == ()
        # Idempotent: removing an absent install is acceptable.
        await repository.uninstall(user_id=USER_INSTALL, identity_id=IDENTITY)
        assert (
            await repository.get_install(user_id=USER_INSTALL, identity_id=IDENTITY)
            is None
        )

    _run_library(control_stack, body)


def test_installs_are_isolated_per_user(control_stack: dict[str, Any]) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        await repository.install(user_id=USER_ISOLATED_A, identity_id=IDENTITY, version=1)
        await repository.install(
            user_id=USER_ISOLATED_A, identity_id=OTHER_IDENTITY, version=1
        )
        await repository.install(user_id=USER_ISOLATED_B, identity_id=IDENTITY, version=2)

        assert (
            await repository.get_install(user_id=USER_ISOLATED_C, identity_id=IDENTITY)
            is None
        )
        assert await repository.installs_of(user_id=USER_ISOLATED_C) == ()

        first = await repository.installs_of(user_id=USER_ISOLATED_A)
        assert tuple(item.identity_id for item in first) == (IDENTITY, OTHER_IDENTITY)
        second = await repository.installs_of(user_id=USER_ISOLATED_B)
        assert tuple(item.version for item in second) == (2,)
        first_install = await repository.get_install(
            user_id=USER_ISOLATED_A, identity_id=IDENTITY
        )
        assert first_install is not None and first_install.version == 1

        await repository.uninstall(user_id=USER_ISOLATED_B, identity_id=IDENTITY)
        assert (
            await repository.get_install(user_id=USER_ISOLATED_B, identity_id=IDENTITY)
            is None
        )
        survivor = await repository.get_install(
            user_id=USER_ISOLATED_A, identity_id=IDENTITY
        )
        assert survivor is not None and survivor.version == 1

    _run_library(control_stack, body)


def test_install_rejects_unknown_and_withdrawn_versions(
    control_stack: dict[str, Any],
) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        for identity_id, version in ((IDENTITY, 99), ("unknown.identity", 1)):
            with pytest.raises(LibraryIdentityNotFound) as unknown:
                await repository.install(
                    user_id=USER_UNKNOWN, identity_id=identity_id, version=version
                )
            assert str(unknown.value) == DEFAULT_CODE
            assert unknown.value.code == DEFAULT_CODE
            assert await repository.get_install(
                user_id=USER_UNKNOWN, identity_id=identity_id
            ) is None

        await catalogue.withdraw(IDENTITY, 2)
        with pytest.raises(LibraryIdentityNotFound) as withdrawn:
            await repository.install(
                user_id=USER_UNKNOWN, identity_id=IDENTITY, version=2
            )
        assert str(withdrawn.value) == PUBLICATION_WITHDRAWN
        assert (
            await repository.get_install(user_id=USER_UNKNOWN, identity_id=IDENTITY)
            is None
        )

        # The non-withdrawn sibling version is still installable.
        kept = await repository.install(
            user_id=USER_UNKNOWN, identity_id=IDENTITY, version=1
        )
        assert kept.version == 1

    _run_library(control_stack, body)


def test_upgrade_is_explicit_strictly_upward_and_catalogue_checked(
    control_stack: dict[str, Any],
) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        with pytest.raises(LibraryIdentityNotFound) as missing:
            await repository.upgrade(
                user_id=USER_UPGRADE_A, identity_id=IDENTITY, to_version=2
            )
        assert str(missing.value) == INSTALL_REQUIRED

        await repository.install(user_id=USER_UPGRADE_A, identity_id=IDENTITY, version=1)
        with pytest.raises(LibraryIdentityNotFound) as same:
            await repository.upgrade(
                user_id=USER_UPGRADE_A, identity_id=IDENTITY, to_version=1
            )
        assert str(same.value) == UPGRADE_REQUIRES_NEWER_VERSION

        with pytest.raises(LibraryIdentityNotFound) as unpublished:
            await repository.upgrade(
                user_id=USER_UPGRADE_A, identity_id=IDENTITY, to_version=99
            )
        assert str(unpublished.value) == DEFAULT_CODE

        upgraded = await repository.upgrade(
            user_id=USER_UPGRADE_A, identity_id=IDENTITY, to_version=2
        )
        assert upgraded.version == 2
        stored = await repository.get_install(
            user_id=USER_UPGRADE_A, identity_id=IDENTITY
        )
        assert stored is not None and stored.version == 2
        assert (
            await repository.update_available(
                user_id=USER_UPGRADE_A, identity_id=IDENTITY
            )
            is False
        )

        await catalogue.withdraw(IDENTITY, 2)
        with pytest.raises(LibraryIdentityNotFound) as withdrawn:
            await repository.upgrade(
                user_id=USER_UPGRADE_B, identity_id=IDENTITY, to_version=1
            )
        assert str(withdrawn.value) == INSTALL_REQUIRED
        await repository.install(user_id=USER_UPGRADE_B, identity_id=IDENTITY, version=1)
        with pytest.raises(LibraryIdentityNotFound) as blocked:
            await repository.upgrade(
                user_id=USER_UPGRADE_B, identity_id=IDENTITY, to_version=2
            )
        assert str(blocked.value) == PUBLICATION_WITHDRAWN
        still_pinned = await repository.get_install(
            user_id=USER_UPGRADE_B, identity_id=IDENTITY
        )
        assert still_pinned is not None and still_pinned.version == 1

    _run_library(control_stack, body)


def test_update_available_follows_the_explicit_pointer(
    control_stack: dict[str, Any],
) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        # Not installed: nothing to advertise.
        assert (
            await repository.update_available(user_id=USER_UPDATE_A, identity_id=IDENTITY)
            is False
        )

        await repository.install(user_id=USER_UPDATE_A, identity_id=IDENTITY, version=2)
        assert (
            await repository.update_available(user_id=USER_UPDATE_A, identity_id=IDENTITY)
            is False
        )

        await repository.install(user_id=USER_UPDATE_A, identity_id=IDENTITY, version=1)
        assert (
            await repository.update_available(user_id=USER_UPDATE_A, identity_id=IDENTITY)
            is True
        )

        # A withdrawn current version is not an available update.
        await catalogue.withdraw(IDENTITY, 2)
        assert (
            await repository.update_available(user_id=USER_UPDATE_A, identity_id=IDENTITY)
            is False
        )

    _run_library(control_stack, body)

    async def pointer_behind_install(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        await repository.install(user_id=USER_UPDATE_B, identity_id=IDENTITY, version=2)
        assert (
            await repository.update_available(user_id=USER_UPDATE_B, identity_id=IDENTITY)
            is False
        )

    # The EXPLICIT pointer, never a numeric maximum.
    _run_library(control_stack, pointer_behind_install, current_versions={IDENTITY: 1})

    async def missing_pointer(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        del catalogue._current[IDENTITY]
        await repository.install(user_id=USER_UPDATE_C, identity_id=IDENTITY, version=1)
        assert (
            await repository.update_available(user_id=USER_UPDATE_C, identity_id=IDENTITY)
            is False
        )
        with pytest.raises(LibraryIdentityNotFound) as unbound:
            await repository.current_version(IDENTITY)
        assert str(unbound.value) == "publication_current_version_unbound"

    _run_library(control_stack, missing_pointer)


# --- stars -------------------------------------------------------------------


def test_stars_are_identity_scoped_counted_and_reversible(
    control_stack: dict[str, Any],
) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        assert await repository.star_count(identity_id=IDENTITY) == 0
        assert await repository.star(user_id=USER_STAR_A, identity_id=IDENTITY) == 1
        # Starring twice is idempotent and still reports the real count.
        assert await repository.star(user_id=USER_STAR_A, identity_id=IDENTITY) == 1
        assert await repository.star(user_id=USER_STAR_B, identity_id=IDENTITY) == 2
        assert await repository.star_count(identity_id=IDENTITY) == 2
        assert (
            await repository.is_starred(user_id=USER_STAR_A, identity_id=IDENTITY)
            is True
        )
        assert (
            await repository.is_starred(user_id=USER_STAR_C, identity_id=IDENTITY)
            is False
        )

        # Stars are IDENTITY scoped, never version scoped: an upgrade keeps them.
        await repository.install(user_id=USER_STAR_A, identity_id=IDENTITY, version=1)
        await repository.upgrade(user_id=USER_STAR_A, identity_id=IDENTITY, to_version=2)
        assert (
            await repository.is_starred(user_id=USER_STAR_A, identity_id=IDENTITY)
            is True
        )
        assert await repository.star_count(identity_id=IDENTITY) == 2

        # Stars of another identity are counted separately and listed in
        # ascending identity order.
        assert (
            await repository.star(user_id=USER_STAR_A, identity_id=OTHER_IDENTITY) == 1
        )
        assert await repository.star_count(identity_id=OTHER_IDENTITY) == 1
        assert await repository.star_count(identity_id=IDENTITY) == 2
        assert await repository.starred_of(user_id=USER_STAR_A) == (
            IDENTITY,
            OTHER_IDENTITY,
        )
        assert await repository.starred_of(user_id=USER_STAR_B) == (IDENTITY,)

        assert await repository.unstar(user_id=USER_STAR_A, identity_id=IDENTITY) == 1
        assert (
            await repository.is_starred(user_id=USER_STAR_A, identity_id=IDENTITY)
            is False
        )
        # Unstarring an absent star is idempotent.
        assert await repository.unstar(user_id=USER_STAR_A, identity_id=IDENTITY) == 1
        assert await repository.unstar(user_id=USER_STAR_C, identity_id=IDENTITY) == 1

        with pytest.raises(LibraryIdentityNotFound) as unknown:
            await repository.star(user_id=USER_STAR_A, identity_id="unknown.identity")
        assert str(unknown.value) == IDENTITY_CODE

    _run_library(control_stack, body)


# --- withdrawal acknowledgement ---------------------------------------------


def test_withdrawal_acknowledgement_is_idempotent_and_version_scoped(
    control_stack: dict[str, Any],
) -> None:
    async def body(
        repository: ControlLibraryRepository, catalogue: PublicationCatalogue
    ) -> None:
        with pytest.raises(LibraryIdentityNotFound) as missing:
            await repository.acknowledge_withdrawal(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=1
            )
        assert str(missing.value) == INSTALL_REQUIRED
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=1
            )
            is False
        )

        await repository.install(user_id=USER_ACK_A, identity_id=IDENTITY, version=1)
        await catalogue.withdraw(IDENTITY, 1)
        await repository.acknowledge_withdrawal(
            user_id=USER_ACK_A, identity_id=IDENTITY, version=1
        )
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=1
            )
            is True
        )
        # Idempotent dismissal.
        await repository.acknowledge_withdrawal(
            user_id=USER_ACK_A, identity_id=IDENTITY, version=1
        )
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=1
            )
            is True
        )
        # Dismissal never clears the catalogue withdrawal.
        assert await catalogue.is_withdrawn(IDENTITY, 1) is True
        assert await repository.is_withdrawn(identity_id=IDENTITY, version=1) is True

        # A version the user never received cannot be acknowledged...
        with pytest.raises(LibraryIdentityNotFound) as mismatch:
            await repository.acknowledge_withdrawal(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=2
            )
        assert str(mismatch.value) == ACK_MISMATCH
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=2
            )
            is False
        )
        # ...and acknowledgements are per user.
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_B, identity_id=IDENTITY, version=1
            )
            is False
        )

        # After the explicit upgrade the NEW version can be acknowledged, and
        # the old acknowledgement stays exactly where it was.
        await repository.upgrade(user_id=USER_ACK_A, identity_id=IDENTITY, to_version=2)
        await repository.acknowledge_withdrawal(
            user_id=USER_ACK_A, identity_id=IDENTITY, version=2
        )
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=2
            )
            is True
        )
        assert (
            await repository.is_acknowledged(
                user_id=USER_ACK_A, identity_id=IDENTITY, version=1
            )
            is True
        )

    _run_library(control_stack, body)


# --- catalogue delegation ----------------------------------------------------


def test_catalogue_reads_and_lifecycle_are_delegated(
    control_stack: dict[str, Any],
) -> None:
    async def body() -> None:
        catalogue = await _recording_catalogue()
        repository = ControlLibraryRepository(catalogue, _control_dsn(control_stack))
        try:
            assert await repository.published_versions(IDENTITY) == (1, 2)
            assert await repository.current_version(IDENTITY) == 2
            assert await repository.certification_state(identity_id=IDENTITY, version=1) == (
                "uncertified"
            )
            assert await repository.is_withdrawn(identity_id=IDENTITY, version=1) is False
            assert await repository.forkable(identity_id=IDENTITY, version=1) is False
            assert await repository.forkable(identity_id=IDENTITY, version=99) is False

            provenance = await repository.certify_local_demo(
                identity_id=IDENTITY, version=2, certified_by="local-cert-admin"
            )
            assert provenance == LOCAL_DEMO_CERTIFICATION_PROVENANCE
            assert (
                await repository.certification_state(identity_id=IDENTITY, version=2)
                == "certified"
            )
            assert catalogue._certified_by[(IDENTITY, 2)] == "local-cert-admin"

            await repository.withdraw(identity_id=IDENTITY, version=2)
            assert await repository.is_withdrawn(identity_id=IDENTITY, version=2) is True
            assert await repository.forkable(identity_id=IDENTITY, version=2) is False

            # The delegation is observable on the composed catalogue itself.
            assert {"get", "versions", "current_version"} <= set(catalogue.calls)
            assert {"certification_state", "is_withdrawn"} <= set(catalogue.calls)
            assert {"certify_local_demo", "withdraw"} <= set(catalogue.calls)

            # Unknown identities keep the catalogue's own fail-closed codes.
            with pytest.raises(LibraryIdentityNotFound) as unknown:
                await repository.current_version("unknown.identity")
            assert str(unknown.value) == "publication_identity_not_found"
            with pytest.raises(LookupError) as uncertifiable:
                await repository.certify_local_demo(
                    identity_id=IDENTITY, version=99, certified_by="local-cert-admin"
                )
            assert str(uncertifiable.value) == "publication_version_not_found"
        finally:
            await repository.close()

    _run_async(body())


# --- port parity -------------------------------------------------------------


def test_port_signatures_codes_and_exception_identities_are_frozen() -> None:
    for name in REQUIRED_METHODS:
        control = getattr(ControlLibraryRepository, name)
        memory = getattr(InMemoryLibraryRepository, name)
        assert inspect.iscoroutinefunction(control), name
        assert inspect.signature(control) == inspect.signature(memory), name
    assert inspect.iscoroutinefunction(ControlLibraryRepository.close)

    # The SAME exception objects, so callers keep catching them by type.
    assert memory_library.LibraryIdentityNotFound is LibraryIdentityNotFound
    assert memory_library.CertificationUnavailable is CertificationUnavailable
    assert CertificationUnavailable.code == NOT_CONNECTED == "NOT_CONNECTED"
    assert str(LibraryIdentityNotFound()) == DEFAULT_CODE
    assert LibraryIdentityNotFound().code == DEFAULT_CODE
    assert PUBLICATION_WITHDRAWN == "publication_withdrawn"
    assert UPGRADE_REQUIRES_NEWER_VERSION == "upgrade_requires_newer_version"
    assert LOCAL_DEMO_CERTIFICATION_PROVENANCE == "local_demo_certification"

    # No second current-version map can be mutated through this port.
    assert not hasattr(ControlLibraryRepository, "set_current_version")
