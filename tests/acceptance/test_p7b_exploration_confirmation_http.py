"""§8.16 P7B acceptance: the exploration-confirmation surface is REACHABLE.

EXECUTION-LEVEL evidence over a REAL HTTP surface (FastAPI + TestClient), never a
source-string assertion.  Before this module the exploration-confirmation object
existed with ZERO non-test references: no route, no container wiring, no
permission mapping.  "An exploration confirmation never stands in for a
definition confirmation" was therefore VACUOUSLY true in the product - a caller
could not reach it at all.

Every test below drives the REGISTERED routes and inspects the ACTUAL definition
store.  The frozen distinctions it proves:

* §8.16 P7B - recording an exploration confirmation leaves the definition in
  DRAFT and the real HTTP ``save()`` is STILL refused afterwards.
* the actor / clock / exploration id are SERVER-owned; a client injection is a
  stable typed code with ZERO state change.
* owner isolation is the repo-wide "foreign == absent" 404, never an existence
  oracle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.api_definitions import register_definition_routes
from src.nl2sql.artifacts.api_exploration_confirmations import (
    EXPLORATION_DEFINITION_REFERENCE_NOT_FOUND,
    register_exploration_confirmation_routes,
)
from src.nl2sql.artifacts.custom_definition import DefinitionVersion
from src.nl2sql.artifacts.definition_store import InMemoryDefinitionStore
from src.nl2sql.artifacts.exploration_confirmation import (
    EXPLORATION_CONFIRMATION_INVALID,
    EXPLORATION_CONFIRMATION_NOT_FOUND,
    EXPLORATION_IDENTITY_IS_SERVER_OWNED,
    ExplorationConfirmationService,
)
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.contracts import RequestContext, RequestIdentity
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.ownership import runtime_config
from src.nl2sql.semantic.calculation_contract import (
    CalculationInputSpec,
    CalculationSpec,
    LiteralOperand,
)

METRIC = "demo.revenue"
ALICE = "alice"
BOB = "bob"
THREAD = "11111111-1111-1111-1111-111111111111"
RUN = "build-run-1"
BASE = "/api/v2/nl2sql"
EXPLORATIONS = f"{BASE}/exploration-confirmations"


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.p7b.exploration.http",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key=METRIC
            ),
        ),
        unit="count",
    )


def _thread_key(thread_id: str, user: str) -> str:
    """The EXACT server thread key ``require_build_run`` resolves."""

    context = RequestContext(
        identity=RequestIdentity(
            request_id=UUID(int=1), user_id=user, permissions=frozenset()
        ),
        thread_id=UUID(thread_id),
        trace_id="t",
    )
    configurable = runtime_config(context)["configurable"]
    assert isinstance(configurable, dict)
    return str(configurable["thread_id"])


class _BuildEngine:
    """Checkpoint-shaped state: a BUILD run exists ONLY under its owner's key."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}

    def seed(self, *, thread_id: str, run_id: str, owner: str) -> None:
        envelope = RunEnvelope(
            run_id=run_id, requested_mode="BUILD", effective_mode="BUILD"
        )
        self.states[_thread_key(thread_id, owner)] = {
            "run_envelope": envelope.model_dump(mode="json"),
            "run_owner_user_id": owner,
        }

    async def aget_state(self, config: dict[str, Any]) -> Any:
        configurable = config["configurable"]
        assert isinstance(configurable, dict)
        return SimpleNamespace(
            values=self.states.get(str(configurable["thread_id"]))
        )


class _ReadOnlyDefinitionReader:
    """The ONLY definition member the exploration surface may reach.

    ``get_exact_version`` is allowed (the owner-scoped READ that resolves an
    optional provenance link).  EVERY other access - in particular any lifecycle
    mutation - raises a hard AssertionError, so a 200 response that carries a
    definition reference is itself execution-level proof that the exploration
    route reached no definition mutation.
    """

    def __init__(self, inner: CustomDefinitionService) -> None:
        self._inner = inner
        self.reads: list[tuple[str, str, int]] = []

    async def get_exact_version(
        self, *, owner_user_id: str, definition_id: str, version: int
    ) -> DefinitionVersion:
        self.reads.append((owner_user_id, definition_id, version))
        return await self._inner.get_exact_version(
            owner_user_id=owner_user_id,
            definition_id=definition_id,
            version=version,
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(
            f"exploration confirmation touched a forbidden definition member: {name}"
        )


class _ExplorationContainer:
    """A real object graph using the SAME accessors the stock container has."""

    def __init__(self, *, read_only_reader: bool = False) -> None:
        self.definition_store = InMemoryDefinitionStore()
        self.definitions = CustomDefinitionService(
            store=self.definition_store, governed_metric_keys={METRIC}
        )
        self.reader = (
            _ReadOnlyDefinitionReader(self.definitions) if read_only_reader else None
        )
        self.explorations = ExplorationConfirmationService(
            definitions=self.reader or self.definitions
        )
        self.engine = _BuildEngine()

    def custom_definition_service(self) -> CustomDefinitionService:
        return self.definitions

    def exploration_confirmation_service(self) -> ExplorationConfirmationService:
        return self.explorations

    async def get_engine(self) -> _BuildEngine:
        return self.engine


def _client(container: _ExplorationContainer, *, user_id: str) -> TestClient:
    app = FastAPI()
    app.state.container = container
    register_definition_routes(app)
    register_exploration_confirmation_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id=user_id, telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app)


def _build_headers() -> dict[str, str]:
    """The opaque references the REAL BUILD gate resolves against the engine."""

    return {
        "x-tt-build-thread-id": THREAD,
        "x-tt-build-run-id": RUN,
    }


async def _definition_snapshot(container: _ExplorationContainer) -> tuple[Any, ...]:
    """The FULL observable definition state: identities, versions, lifecycle."""

    rows: list[Any] = []
    for definition in await container.definition_store.list_definitions():
        versions: list[Any] = []
        for version in await container.definition_store.list_versions(
            definition_id=definition.definition_id
        ):
            lifecycle = await container.definition_store.get_lifecycle(
                definition_id=definition.definition_id, version=version.version
            )
            versions.append((version.version, version.checksum, lifecycle))
        rows.append(
            (
                definition.definition_id,
                definition.owner_user_id,
                definition.axes,
                definition.current_version.version,
                definition.current_version.checksum,
                tuple(versions),
            )
        )
    return tuple(rows)


def _closed_draft(client: TestClient) -> str:
    """A semantically CLOSED draft created through the REAL HTTP definition route."""

    created = client.post(
        f"{BASE}/definitions",
        headers=_build_headers(),
        json={"title": "M", "calculation": _spec().model_dump(mode="json")},
    )
    assert created.status_code == 200, created.text
    definition_id = str(created.json()["definition_id"])
    closed = client.post(
        f"{BASE}/definitions/{definition_id}/semantic-close",
        headers=_build_headers(),
    )
    assert closed.status_code == 200, closed.text
    return definition_id


# ---------------------------------------------------------------------------
# 1. The exploration-confirmation route is REACHABLE and SERVER-OWNED.
# ---------------------------------------------------------------------------


async def test_http_exploration_confirmation_is_reachable_and_server_owned() -> None:
    container = _ExplorationContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    definition_id = _closed_draft(_client(container, user_id=ALICE))
    client = _client(container, user_id=ALICE)

    started = datetime.now(UTC)
    response = client.post(
        EXPLORATIONS,
        json={
            "run_id": "run-explore-1",
            "subject": "explore the closed draft",
            "definition_id": definition_id,
            "version": 1,
        },
    )
    finished = datetime.now(UTC)

    assert response.status_code == 200, response.text
    body = response.json()
    # The identity, the clock and the id are all SERVER-produced.
    assert body["confirmed_by"] == ALICE
    assert body["exploration_id"].startswith("exp_")
    assert len(body["exploration_id"]) == 36
    confirmed_at = datetime.fromisoformat(body["confirmed_at"])
    assert started <= confirmed_at <= finished
    assert body["replaces_definition_confirmation"] is False
    assert body["definition_reference"]["definition_id"] == definition_id
    assert body["definition_reference"]["version"] == 1
    assert body["definition_reference"]["semantic_closed"] is True

    # The record is owner-readable by BOTH list and single read.
    listed = client.get(EXPLORATIONS, params={"run_id": "run-explore-1"})
    assert listed.status_code == 200, listed.text
    assert [item["exploration_id"] for item in listed.json()["confirmations"]] == [
        body["exploration_id"]
    ]
    single = client.get(
        f"{EXPLORATIONS}/{body['exploration_id']}",
        params={"run_id": "run-explore-1"},
    )
    assert single.status_code == 200, single.text
    assert single.json() == body


# ---------------------------------------------------------------------------
# 2. THE core invariant, over REAL HTTP: the definition is STILL a DRAFT and the
#    real save() is STILL refused.
# ---------------------------------------------------------------------------


async def test_http_exploration_confirmation_leaves_the_definition_in_draft() -> None:
    container = _ExplorationContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    client = _client(container, user_id=ALICE)
    definition_id = _closed_draft(client)

    explored = client.post(
        EXPLORATIONS,
        json={
            "run_id": "run-explore-2",
            "subject": "explore",
            "definition_id": definition_id,
            "version": 1,
        },
    )
    assert explored.status_code == 200, explored.text

    # The exploration confirmation really EXISTS...
    assert (
        len(
            client.get(
                EXPLORATIONS, params={"run_id": "run-explore-2"}
            ).json()["confirmations"]
        )
        == 1
    )
    # ...and the definition is STILL a DRAFT on the confirmation axis.
    version = client.get(f"{BASE}/definitions/{definition_id}/versions/1")
    assert version.status_code == 200, version.text
    assert version.json()["confirmation"] == "DRAFT"
    assert version.json()["retention"] == "SESSION"
    assert version.json()["semantic_closed"] is True

    # The REAL HTTP save() is STILL refused: an exploration confirmation never
    # unlocks the definition lifecycle.
    refused = client.post(
        f"{BASE}/definitions/{definition_id}/save",
        headers=_build_headers(),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": "definition_lifecycle_invalid"}
    still = client.get(f"{BASE}/definitions/{definition_id}/versions/1").json()
    assert still["confirmation"] == "DRAFT"
    assert still["retention"] == "SESSION"

    # CONTRAST: only the REAL definition confirmation unlocks save.
    confirmed = client.post(
        f"{BASE}/definitions/{definition_id}/confirm",
        headers=_build_headers(),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmation"] == "CONFIRMED"
    saved = client.post(
        f"{BASE}/definitions/{definition_id}/save",
        headers=_build_headers(),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["retention"] == "SAVED"


# ---------------------------------------------------------------------------
# 3. Cross-user access is the SAME 404 as absent (no existence oracle).
# ---------------------------------------------------------------------------


async def test_http_cross_user_exploration_is_indistinguishable_from_absent() -> None:
    container = _ExplorationContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=BOB)
    alice = _client(container, user_id=ALICE)
    bob = _client(container, user_id=BOB)

    created = alice.post(
        EXPLORATIONS, json={"run_id": "run-private", "subject": "alice only"}
    )
    assert created.status_code == 200, created.text
    exploration_id = created.json()["exploration_id"]

    foreign = bob.get(
        f"{EXPLORATIONS}/{exploration_id}", params={"run_id": "run-private"}
    )
    absent = bob.get(
        f"{EXPLORATIONS}/exp_{'0' * 32}", params={"run_id": "run-private"}
    )
    assert foreign.status_code == absent.status_code == 404
    # Byte-for-byte identical: the foreign record leaks no existence.
    assert foreign.json() == absent.json() == {
        "detail": EXPLORATION_CONFIRMATION_NOT_FOUND
    }

    # A run id is client-supplied, so the OWNER filter is what keeps a foreign
    # run invisible even when BOTH callers use the SAME run id.
    bob_created = bob.post(
        EXPLORATIONS, json={"run_id": "run-private", "subject": "bob only"}
    )
    assert bob_created.status_code == 200, bob_created.text
    alice_view = alice.get(
        EXPLORATIONS, params={"run_id": "run-private"}
    ).json()["confirmations"]
    bob_view = bob.get(
        EXPLORATIONS, params={"run_id": "run-private"}
    ).json()["confirmations"]
    assert [item["exploration_id"] for item in alice_view] == [exploration_id]
    assert [item["exploration_id"] for item in bob_view] == [
        bob_created.json()["exploration_id"]
    ]

    # A foreign RUN is equally indistinguishable from a never-existing one.
    assert bob.get(EXPLORATIONS, params={"run_id": "never-used"}).json() == {
        "run_id": "never-used",
        "confirmations": [],
    }


# ---------------------------------------------------------------------------
# 4. Client-injected server fields are a STABLE typed code, zero state change.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "injected",
    [
        {"confirmed_by": "mallory"},
        {"confirmed_at": "1999-01-01T00:00:00Z"},
        {"exploration_id": "exp_" + "a" * 32},
    ],
)
async def test_http_injected_server_fields_are_a_stable_typed_code(
    injected: dict[str, str],
) -> None:
    container = _ExplorationContainer()
    before = await _definition_snapshot(container)
    client = _client(container, user_id=ALICE)

    response = client.post(
        EXPLORATIONS,
        json={"run_id": "run-inject", "subject": "s", **injected},
    )

    assert response.status_code == 422, (injected, response.text)
    detail = response.json()["detail"]
    # The STABLE code, not a bare pydantic error array.
    assert detail["code"] == EXPLORATION_IDENTITY_IS_SERVER_OWNED
    assert next(iter(injected)) in detail["fields"]

    # ZERO state change: no record was written and no definition moved.
    assert client.get(EXPLORATIONS, params={"run_id": "run-inject"}).json() == {
        "run_id": "run-inject",
        "confirmations": [],
    }
    assert await _definition_snapshot(container) == before


async def test_http_unknown_field_is_the_stable_invalid_payload_code() -> None:
    container = _ExplorationContainer()
    before = await _definition_snapshot(container)
    client = _client(container, user_id=ALICE)

    response = client.post(
        EXPLORATIONS,
        json={"run_id": "run-unknown", "subject": "s", "nonsense": 1},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == EXPLORATION_CONFIRMATION_INVALID
    assert client.get(EXPLORATIONS, params={"run_id": "run-unknown"}).json() == {
        "run_id": "run-unknown",
        "confirmations": [],
    }
    assert await _definition_snapshot(container) == before


async def test_http_missing_required_field_is_the_stable_invalid_payload_code() -> None:
    container = _ExplorationContainer()
    client = _client(container, user_id=ALICE)

    response = client.post(EXPLORATIONS, json={"subject": "no run id"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == EXPLORATION_CONFIRMATION_INVALID
    assert "run_id" in response.json()["detail"]["fields"]


# ---------------------------------------------------------------------------
# 5. A definition reference is owner-scoped and READ-ONLY.
# ---------------------------------------------------------------------------


async def test_http_exploration_reference_is_owner_scoped_and_read_only() -> None:
    container = _ExplorationContainer(read_only_reader=True)
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    definition_id = _closed_draft(_client(container, user_id=ALICE))
    before = await _definition_snapshot(container)
    alice = _client(container, user_id=ALICE)
    bob = _client(container, user_id=BOB)
    assert container.reader is not None

    created = alice.post(
        EXPLORATIONS,
        json={
            "run_id": "run-ref",
            "subject": "reference the draft",
            "definition_id": definition_id,
            "version": 1,
        },
    )
    assert created.status_code == 200, created.text
    # The ONE definition member the route reached was the owner-scoped READ.
    assert container.reader.reads == [(ALICE, definition_id, 1)]
    # ...and the reference changed NOTHING in the definition lifecycle.
    assert await _definition_snapshot(container) == before

    # A foreign definition reference is the SAME 404 as an absent one, and it
    # writes no exploration record.
    foreign = bob.post(
        EXPLORATIONS,
        json={
            "run_id": "run-ref-bob",
            "subject": "probe",
            "definition_id": definition_id,
            "version": 1,
        },
    )
    absent = bob.post(
        EXPLORATIONS,
        json={
            "run_id": "run-ref-bob",
            "subject": "probe",
            "definition_id": "def_" + "0" * 32,
            "version": 1,
        },
    )
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json() == absent.json() == {
        "detail": EXPLORATION_DEFINITION_REFERENCE_NOT_FOUND
    }
    assert bob.get(EXPLORATIONS, params={"run_id": "run-ref-bob"}).json() == {
        "run_id": "run-ref-bob",
        "confirmations": [],
    }
    assert await _definition_snapshot(container) == before


def test_the_exploration_service_never_holds_a_mutating_definition_capability() -> None:
    """Even the FULL definition service is reduced to ONE read at runtime."""

    from src.nl2sql.artifacts.exploration_confirmation import ReadOnlyDefinitionReader

    service = ExplorationConfirmationService(
        definitions=CustomDefinitionService(governed_metric_keys={METRIC})
    )
    reader = service.definition_reader
    assert isinstance(reader, ReadOnlyDefinitionReader)
    # The runtime object exposes EXACTLY the one owner-scoped READ.
    assert [name for name in dir(reader) if not name.startswith("_")] == [
        "get_exact_version"
    ]
    for forbidden in (
        "create_draft",
        "update_draft",
        "update_draft_with_semantics",
        "mark_semantic_closed",
        "confirm",
        "save",
        "create_revision",
        "create_fork",
        "project_published",
        "project_certified",
    ):
        assert not hasattr(reader, forbidden), forbidden


# ---------------------------------------------------------------------------
# 6. The stock app serves the exploration routes with an EXPLICIT permission.
# ---------------------------------------------------------------------------


def test_stock_app_registers_exploration_routes_with_an_explicit_permission() -> None:
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
    assert EXPLORATIONS in spec["paths"]
    assert f"{EXPLORATIONS}/{{exploration_id}}" in spec["paths"]
    for method, path in (
        ("POST", EXPLORATIONS),
        ("GET", EXPLORATIONS),
        ("GET", f"{EXPLORATIONS}/exp_{'a' * 32}"),
    ):
        assert (
            _required_nl2sql_permission(method, path, settings)
            == settings.auth_required_permission_invoke
        ), (method, path)
    # The declared request body is CLOSED: no server-owned field is accepted.
    body_schema = spec["paths"][EXPLORATIONS]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert body_schema["additionalProperties"] is False
    assert set(body_schema["properties"]) == {
        "run_id",
        "subject",
        "definition_id",
        "version",
    }
    assert "confirmed_by" not in body_schema["properties"]
    assert "confirmed_at" not in body_schema["properties"]
    assert "exploration_id" not in body_schema["properties"]
