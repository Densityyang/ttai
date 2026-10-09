"""§8.16 P7B acceptance: saving a RESULT artifact never creates a definition.

EXECUTION-LEVEL evidence over a REAL HTTP surface (FastAPI + TestClient), never
a source-string assertion.  Every test drives the registered artifact routes and
then inspects the ACTUAL definition store, the ACTUAL artifact store and the
ACTUAL execution/fetcher counters.

The frozen distinctions this file proves:

* A3 - "save execution/result artifact" is an ARTIFACT operation: it creates no
  definition, selects no BUILD and canonicalizes nothing.  A
  CustomDefinitionArtifact only POINTS AT an already-existing version.
* A4 - the original object stays noncanonical and an AD_HOC result never enters
  the definition lifecycle.
* §8.16 P7B - "saving a result does not create a definition".
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.api_artifacts import register_artifact_routes
from src.nl2sql.artifacts.custom_definition import DefinitionVersion
from src.nl2sql.artifacts.definition_store import InMemoryDefinitionStore
from src.nl2sql.artifacts.repository import InMemoryArtifactRepository
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)

METRIC = "demo.revenue"
ARTIFACTS_PATH = "/api/v2/nl2sql/artifacts"


def _spec(calculation_id: str = "calc.demo") -> CalculationSpec:
    return CalculationSpec(
        calculation_id=calculation_id,
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key=METRIC
            ),
        ),
        unit="count",
    )


class _ReadOnlyDefinitionService:
    """The ONLY definition-service member an artifact save may reach.

    The artifact route is allowed to RESOLVE an exact version (owner-scoped) so a
    reference can be proven to exist.  Every OTHER access - in particular any
    lifecycle mutation such as create_draft / create_revision / confirm / save /
    mark_semantic_closed / execute_version - raises a hard AssertionError, so a
    200 response is itself execution-level proof that no definition was created
    or modified by the save.
    """

    def __init__(self, inner: CustomDefinitionService) -> None:
        self._inner = inner
        self.reads: list[tuple[str, int]] = []

    async def get_exact_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersion:
        self.reads.append((definition_id, version))
        return await self._inner.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(
            f"artifact save touched a forbidden definition-service member: {name}"
        )


class _CountingExecutor:
    """Records any attempt to execute; the artifact surface must make none."""

    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, **_kwargs: Any) -> None:
        self.execute_calls += 1
        raise AssertionError("artifact save must not execute a definition")


class _CountingFetcher:
    """Records any governed-input fetch; the artifact surface must make none."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        raise AssertionError("artifact save must not fetch governed input")


class _ArtifactContainer:
    """A real product object graph: the SAME accessors the stock container has."""

    def __init__(self) -> None:
        self.definition_store = InMemoryDefinitionStore()
        self.definitions = CustomDefinitionService(
            store=self.definition_store, governed_metric_keys={METRIC}
        )
        self.definition_read_port = _ReadOnlyDefinitionService(self.definitions)
        self.artifacts = InMemoryArtifactRepository()
        self.executor = _CountingExecutor()
        self.fetcher = _CountingFetcher()
        self.execution_service_lookups = 0

    def custom_definition_service(self) -> _ReadOnlyDefinitionService:
        return self.definition_read_port

    async def artifact_repository(self) -> InMemoryArtifactRepository:
        return self.artifacts

    def custom_definition_execution_service(self) -> _CountingExecutor:
        # If the artifact route ever resolves the execution service, this counter
        # proves it.  A correct save never does.
        self.execution_service_lookups += 1
        return self.executor


def _client(container: _ArtifactContainer, *, user_id: str) -> TestClient:
    app = FastAPI()
    app.state.container = container
    register_artifact_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id=user_id, telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app)


async def _saved_definition(
    container: _ArtifactContainer, *, owner: str = "alice"
) -> tuple[str, DefinitionVersion]:
    """A CONFIRMED + SAVED definition built through the real service API."""

    service = container.definitions
    draft = await service.create_draft(
        owner_user_id=owner, title="Margin", calculation=_spec()
    )
    await service.mark_semantic_closed(
        owner_user_id=owner, definition_id=draft.definition_id
    )
    await service.confirm(owner_user_id=owner, definition_id=draft.definition_id)
    await service.save(owner_user_id=owner, definition_id=draft.definition_id)
    exact = await service.get_exact_version(
        owner_user_id=owner, definition_id=draft.definition_id, version=1
    )
    return draft.definition_id, exact


async def _definition_snapshot(container: _ArtifactContainer) -> tuple[Any, ...]:
    """The FULL observable definition state: identities, versions, lifecycle."""

    definitions = await container.definition_store.list_definitions()
    rows: list[Any] = []
    for definition in definitions:
        versions = await container.definition_store.list_versions(
            definition_id=definition.definition_id
        )
        version_rows: list[Any] = []
        for version in versions:
            lifecycle = await container.definition_store.get_lifecycle(
                definition_id=definition.definition_id, version=version.version
            )
            version_rows.append((version.version, version.checksum, lifecycle))
        rows.append(
            (
                definition.definition_id,
                definition.owner_user_id,
                definition.axes,
                definition.current_version.version,
                definition.current_version.checksum,
                tuple(version_rows),
            )
        )
    return tuple(rows)


def _assert_no_execution(container: _ArtifactContainer) -> None:
    assert container.execution_service_lookups == 0
    assert container.executor.execute_calls == 0
    assert container.fetcher.calls == []


# ---------------------------------------------------------------------------
# 1. Saving an AnalysisArtifact creates NO definition and NO version.
# ---------------------------------------------------------------------------


async def test_http_save_analysis_artifact_creates_no_definition() -> None:
    container = _ArtifactContainer()
    definition_id, exact = await _saved_definition(container)
    before = await _definition_snapshot(container)
    assert len(before) == 1
    assert len(before[0][5]) == 1  # exactly ONE version before the save
    client = _client(container, user_id="alice")

    response = client.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "title": "Margin result",
                "summary": "run-scoped, noncanonical analysis",
            }
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["artifact_type"] == "analysis"
    assert body["owner_user_id"] == "alice"
    assert body["artifact_id"].startswith("art_")
    # A4: the original object is noncanonical for its whole life.
    assert body["payload"]["noncanonical"] is True

    # The save created NOTHING in the definition lifecycle.
    after = await _definition_snapshot(container)
    assert after == before
    assert len(after) == 1
    assert len(after[0][5]) == 1
    exact_after = await container.definitions.get_exact_version(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    assert exact_after.checksum == exact.checksum
    # No definition reader was even needed for an analysis payload.
    assert container.definition_read_port.reads == []
    _assert_no_execution(container)

    # The artifact itself IS persisted and owner-readable.
    got = client.get(f"{ARTIFACTS_PATH}/{body['artifact_id']}")
    assert got.status_code == 200, got.text
    assert got.json() == body
    listed = client.get(ARTIFACTS_PATH)
    assert listed.status_code == 200
    assert [item["artifact_id"] for item in listed.json()["artifacts"]] == [
        body["artifact_id"]
    ]


# ---------------------------------------------------------------------------
# 2. Saving a CustomDefinitionArtifact only POINTS AT an existing version.
# ---------------------------------------------------------------------------


async def test_http_save_custom_definition_artifact_creates_no_version() -> None:
    container = _ArtifactContainer()
    definition_id, exact = await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")

    response = client.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "definition_id": definition_id,
                "definition_version": 1,
                "definition_checksum": exact.checksum,
            }
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["artifact_type"] == "custom_definition"
    assert body["payload"] == {
        "definition_id": definition_id,
        "definition_version": 1,
        "definition_checksum": exact.checksum,
    }

    # The version count and the checksum are UNCHANGED: no create_draft, no
    # create_revision, no confirm/save, no new version.
    after = await _definition_snapshot(container)
    assert after == before
    assert len(after[0][5]) == 1
    exact_after = await container.definitions.get_exact_version(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    assert exact_after.checksum == exact.checksum
    # The ONE definition-service member the save used was the owner-scoped READ.
    assert container.definition_read_port.reads == [(definition_id, 1)]
    _assert_no_execution(container)


# ---------------------------------------------------------------------------
# 3. A reference to an ABSENT version is refused with zero state change.
# ---------------------------------------------------------------------------


async def test_http_save_rejects_absent_version_with_zero_state_change() -> None:
    container = _ArtifactContainer()
    definition_id, exact = await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")

    response = client.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "definition_id": definition_id,
                "definition_version": 2,  # never created
                "definition_checksum": exact.checksum,
            }
        },
    )

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "artifact_definition_version_not_found"}
    assert await container.artifacts.list_for_owner(owner_user_id="alice") == ()
    assert await _definition_snapshot(container) == before
    _assert_no_execution(container)


async def test_http_save_rejects_checksum_mismatch_with_zero_state_change() -> None:
    container = _ArtifactContainer()
    definition_id, _exact = await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")

    response = client.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "definition_id": definition_id,
                "definition_version": 1,
                "definition_checksum": "0" * 64,
            }
        },
    )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "artifact_definition_checksum_mismatch"}
    assert await container.artifacts.list_for_owner(owner_user_id="alice") == ()
    assert await _definition_snapshot(container) == before
    _assert_no_execution(container)


# ---------------------------------------------------------------------------
# 4. Cross-user access is 404 and leaks NO existence.
# ---------------------------------------------------------------------------


async def test_http_cross_user_artifact_is_indistinguishable_from_absent() -> None:
    container = _ArtifactContainer()
    definition_id, exact = await _saved_definition(container)
    alice = _client(container, user_id="alice")
    bob = _client(container, user_id="bob")

    created = alice.post(
        ARTIFACTS_PATH,
        json={"payload": {"title": "private", "summary": "alice only"}},
    )
    assert created.status_code == 200, created.text
    artifact_id = created.json()["artifact_id"]

    foreign = bob.get(f"{ARTIFACTS_PATH}/{artifact_id}")
    absent = bob.get(f"{ARTIFACTS_PATH}/art_{'0' * 32}")
    assert foreign.status_code == absent.status_code == 404
    # Byte-for-byte identical: no existence oracle.
    assert foreign.json() == absent.json() == {"detail": "artifact_not_found"}
    assert bob.get(ARTIFACTS_PATH).json() == {"artifacts": []}

    # A foreign ARTIFACT is also unwritable by the same ONE failure.
    foreign_put = bob.put(
        f"{ARTIFACTS_PATH}/{artifact_id}",
        json={"payload": {"title": "stolen", "summary": "stolen"}},
    )
    assert foreign_put.status_code == 404
    assert foreign_put.json() == {"detail": "artifact_not_found"}

    # A foreign DEFINITION reference is equally indistinguishable from absent.
    foreign_ref = bob.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "definition_id": definition_id,
                "definition_version": 1,
                "definition_checksum": exact.checksum,
            }
        },
    )
    absent_ref = bob.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "definition_id": "def_" + "0" * 32,
                "definition_version": 1,
                "definition_checksum": exact.checksum,
            }
        },
    )
    assert foreign_ref.status_code == absent_ref.status_code == 404
    assert foreign_ref.json() == absent_ref.json() == {
        "detail": "artifact_definition_version_not_found"
    }
    assert bob.get(ARTIFACTS_PATH).json() == {"artifacts": []}

    # Alice's artifact is untouched and still readable by Alice.
    still = alice.get(f"{ARTIFACTS_PATH}/{artifact_id}")
    assert still.status_code == 200
    assert still.json() == created.json()


# ---------------------------------------------------------------------------
# 5. Client-injected server fields are a 422 with zero state change.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "injected",
    [
        {"owner_user_id": "bob"},
        {"artifact_type": "custom_definition"},
        {"artifact_id": "art_" + "a" * 32},
        {"schema_version": "1.0"},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"updated_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_http_save_rejects_top_level_server_fields(
    injected: dict[str, Any],
) -> None:
    container = _ArtifactContainer()
    await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")

    body = {"payload": {"title": "t", "summary": "s"}, **injected}
    response = client.post(ARTIFACTS_PATH, json=body)

    assert response.status_code == 422, (injected, response.text)
    assert await container.artifacts.list_for_owner(owner_user_id="alice") == ()
    assert await _definition_snapshot(container) == before
    _assert_no_execution(container)


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "t", "summary": "s", "noncanonical": False},
        {"title": "t", "summary": "s", "owner_user_id": "bob"},
        {"title": "t", "summary": "s", "artifact_type": "analysis"},
        {"title": "t", "summary": "s", "created_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_http_save_rejects_payload_server_fields(
    payload: dict[str, Any],
) -> None:
    container = _ArtifactContainer()
    await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")

    response = client.post(ARTIFACTS_PATH, json={"payload": payload})

    assert response.status_code == 422, (payload, response.text)
    assert await container.artifacts.list_for_owner(owner_user_id="alice") == ()
    assert await _definition_snapshot(container) == before
    _assert_no_execution(container)


async def test_http_replace_preserves_type_and_touches_no_definition() -> None:
    container = _ArtifactContainer()
    definition_id, exact = await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")
    created = client.post(
        ARTIFACTS_PATH, json={"payload": {"title": "t", "summary": "s"}}
    ).json()
    artifact_id = created["artifact_id"]

    replaced = client.put(
        f"{ARTIFACTS_PATH}/{artifact_id}",
        json={"payload": {"title": "t2", "summary": "s2"}},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["artifact_id"] == artifact_id
    assert replaced.json()["artifact_type"] == "analysis"
    assert replaced.json()["payload"] == {
        "title": "t2",
        "summary": "s2",
        "noncanonical": True,
    }

    # The artifact TYPE is immutable: a payload that would change it is refused
    # and the stored payload is left intact.
    crossed = client.put(
        f"{ARTIFACTS_PATH}/{artifact_id}",
        json={
            "payload": {
                "definition_id": definition_id,
                "definition_version": 1,
                "definition_checksum": exact.checksum,
            }
        },
    )
    assert crossed.status_code == 409, crossed.text
    assert crossed.json() == {"detail": "artifact_type_immutable"}
    unchanged = client.get(f"{ARTIFACTS_PATH}/{artifact_id}").json()
    assert unchanged["payload"] == {
        "title": "t2",
        "summary": "s2",
        "noncanonical": True,
    }
    assert await _definition_snapshot(container) == before
    _assert_no_execution(container)


# ---------------------------------------------------------------------------
# 6. Saving an artifact triggers NO execution and NO definition mutation.
# ---------------------------------------------------------------------------


async def test_http_save_triggers_no_execution() -> None:
    container = _ArtifactContainer()
    definition_id, exact = await _saved_definition(container)
    before = await _definition_snapshot(container)
    client = _client(container, user_id="alice")

    analysis = client.post(
        ARTIFACTS_PATH,
        json={"payload": {"title": "t", "summary": "s"}},
    )
    reference = client.post(
        ARTIFACTS_PATH,
        json={
            "payload": {
                "definition_id": definition_id,
                "definition_version": 1,
                "definition_checksum": exact.checksum,
            }
        },
    )

    assert analysis.status_code == 200, analysis.text
    assert reference.status_code == 200, reference.text
    _assert_no_execution(container)
    assert await _definition_snapshot(container) == before


# ---------------------------------------------------------------------------
# 6b. A3: saving a RESULT artifact is NOT a BUILD operation, while definition
#     CREATE is gated on the server-persisted BUILD run.
# ---------------------------------------------------------------------------


def _definition_router_client(
    container: _ArtifactContainer, *, user_id: str
) -> TestClient:
    """The stock Definition router mounted over the SAME container."""

    from src.nl2sql.artifacts.api_artifacts import register_artifact_routes
    from src.nl2sql.artifacts.api_definitions import register_definition_routes

    app = FastAPI()
    app.state.container = container
    # The artifact surface is its OWN product router (mounted next to the others
    # in register_v2_routes), so exercising BOTH surfaces on ONE app means
    # registering BOTH routers - exactly as the stock app does.
    register_definition_routes(app)
    register_artifact_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id=user_id, telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app)


async def test_http_artifact_save_is_not_gated_by_build_while_create_is() -> None:
    container = _ArtifactContainer()
    # No get_engine, and the client sends no x-tt-build-* headers.
    assert not hasattr(container, "get_engine")
    client = _definition_router_client(container, user_id="alice")

    definition_create = client.post(
        "/api/v2/nl2sql/definitions",
        json={"title": "M", "calculation": _spec().model_dump(mode="json")},
    )
    assert definition_create.status_code == 409, definition_create.text
    assert definition_create.json() == {"detail": "build_mode_required"}

    artifact_save = client.post(
        ARTIFACTS_PATH, json={"payload": {"title": "t", "summary": "s"}}
    )
    assert artifact_save.status_code == 200, artifact_save.text
    # The contrast is REAL: the refused CREATE left no definition behind and the
    # artifact save created none either.
    assert len(await container.definition_store.list_definitions()) == 0


# ---------------------------------------------------------------------------
# 7. The stock app serves the artifact routes with an EXPLICIT permission.
# ---------------------------------------------------------------------------


def test_stock_app_registers_artifacts_with_an_explicit_permission() -> None:
    from main import create_app
    from src.core.auth.dependencies import _required_nl2sql_permission
    from src.core.settings import Settings

    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth_required_permission_invoke="nl2sql:invoke",
        auth_required_permission_stream="nl2sql:stream",
    )
    app = create_app()
    spec = app.openapi()
    assert "/api/v2/nl2sql/artifacts" in spec["paths"]
    assert "/api/v2/nl2sql/artifacts/{artifact_id}" in spec["paths"]
    for method, path in (
        ("POST", "/api/v2/nl2sql/artifacts"),
        ("GET", "/api/v2/nl2sql/artifacts"),
        ("GET", "/api/v2/nl2sql/artifacts/{artifact_id}"),
        ("PUT", "/api/v2/nl2sql/artifacts/{artifact_id}"),
    ):
        assert (
            _required_nl2sql_permission(method, path, settings)
            == settings.auth_required_permission_invoke
        ), (method, path)
