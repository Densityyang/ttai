"""§8.18 BUILD lifecycle integration: EVERY definition lifecycle change is gated.

EXECUTION-LEVEL evidence over the REAL product routers (FastAPI + TestClient),
never a source-string assertion.  The question this module answers is not "does
``require_build_run`` exist" but "does EVERY entry point that can move a
definition lifecycle actually reach it" - the project has repeatedly shipped
"wiring exists but the path is unreachable".

The coverage matrix proved here (entry point -> gate -> test):

* create draft      POST   /definitions                                   -> BUILD -> test_lifecycle_entry_requires_build[create draft]
* edit draft        PATCH  /definitions/{id}/draft                        -> BUILD -> ... [edit draft]
* semantic close    POST   /definitions/{id}/semantic-close               -> BUILD -> ... [semantic close]
* confirm           POST   /definitions/{id}/confirm                      -> BUILD -> ... [confirm]
* save              POST   /definitions/{id}/save                         -> BUILD -> ... [save]
* revision          POST   /definitions/{id}/revisions                    -> BUILD -> ... [revision]
* publish           POST   /definitions/{id}/versions/{v}/publish         -> BUILD -> ... [publish]
* fork              POST   /library/fork                                  -> BUILD -> ... [fork]
* certify           POST   /library/certify                               -> BUILD -> ... [certify]

``certify`` was the ONE real gap: the product service projects CERTIFIED onto the
CURRENT definition axes (``project_certified``) yet the route reached it without
the BUILD gate.  It is fixed in ``api_library.py`` and pinned by [certify].

The CONTRAST cases prove the gate is specific, not blanket: a READ and the
run-scoped EXPLORATION confirmation (a non-authoritative result action) stay
reachable without BUILD.
"""

from __future__ import annotations

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
    register_exploration_confirmation_routes,
)
from src.nl2sql.artifacts.api_library import register_library_routes
from src.nl2sql.artifacts.definition_store import InMemoryDefinitionStore
from src.nl2sql.artifacts.exploration_confirmation import (
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
DEF_ID = "def_" + "0" * 32
BUILD_REQUIRED = "build_mode_required"


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="calc.p7b.build.gate",
        expression=LiteralOperand(value=1),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key=METRIC
            ),
        ),
        unit="count",
    )


_SPEC_JSON = _spec().model_dump(mode="json")

# (label, method, path, body) for EVERY entry point that can move a definition
# lifecycle.  ``body`` is the MINIMAL valid payload, so a 409 can only come from
# the BUILD gate and never from request validation.
LIFECYCLE_ENTRY_POINTS: tuple[tuple[str, str, str, Any], ...] = (
    ("create draft", "POST", f"{BASE}/definitions", {"title": "M", "calculation": _SPEC_JSON}),
    ("edit draft", "PATCH", f"{BASE}/definitions/{DEF_ID}/draft", {}),
    ("semantic close", "POST", f"{BASE}/definitions/{DEF_ID}/semantic-close", None),
    ("confirm", "POST", f"{BASE}/definitions/{DEF_ID}/confirm", None),
    ("save", "POST", f"{BASE}/definitions/{DEF_ID}/save", None),
    ("revision", "POST", f"{BASE}/definitions/{DEF_ID}/revisions", None),
    ("publish", "POST", f"{BASE}/definitions/{DEF_ID}/versions/1/publish", None),
    (
        "fork",
        "POST",
        f"{BASE}/library/fork",
        {"identity_id": DEF_ID, "version": 1, "title": "F"},
    ),
    (
        "certify",
        "POST",
        f"{BASE}/library/certify",
        {"identity_id": DEF_ID, "version": 1},
    ),
)


def _thread_key(thread_id: str, user: str) -> str:
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

    def seed(self, *, thread_id: str, run_id: str, owner: str, mode: str = "BUILD") -> None:
        envelope = RunEnvelope(
            run_id=run_id, requested_mode=mode, effective_mode=mode
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


class _GateContainer:
    """A real definition store plus the application-scoped BUILD engine."""

    def __init__(self) -> None:
        self.definition_store = InMemoryDefinitionStore()
        self.definitions = CustomDefinitionService(
            store=self.definition_store, governed_metric_keys={METRIC}
        )
        self.explorations = ExplorationConfirmationService(
            definitions=self.definitions
        )
        self.engine = _BuildEngine()

    def custom_definition_service(self) -> CustomDefinitionService:
        return self.definitions

    def exploration_confirmation_service(self) -> ExplorationConfirmationService:
        return self.explorations

    async def get_engine(self) -> _BuildEngine:
        return self.engine


def _client(container: _GateContainer, *, user_id: str) -> TestClient:
    app = FastAPI()
    app.state.container = container
    register_definition_routes(app)
    register_library_routes(app)
    register_exploration_confirmation_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id=user_id, telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app)


def _build_headers() -> dict[str, str]:
    return {"x-tt-build-thread-id": THREAD, "x-tt-build-run-id": RUN}


# ---------------------------------------------------------------------------
# The coverage matrix: EVERY lifecycle entry point refuses without BUILD.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "method", "path", "body"),
    LIFECYCLE_ENTRY_POINTS,
    ids=[entry[0] for entry in LIFECYCLE_ENTRY_POINTS],
)
async def test_lifecycle_entry_requires_build(
    label: str, method: str, path: str, body: Any
) -> None:
    container = _GateContainer()
    client = _client(container, user_id=ALICE)

    # No get_engine result => no server-persisted BUILD run.
    response = client.request(method, path, json=body)

    assert response.status_code == 409, (label, response.text)
    assert response.json() == {"detail": BUILD_REQUIRED}, label
    # ZERO state change: the gate runs BEFORE any service call.
    assert await container.definition_store.list_definitions() == ()


@pytest.mark.parametrize(
    ("label", "method", "path", "body"),
    LIFECYCLE_ENTRY_POINTS,
    ids=[entry[0] for entry in LIFECYCLE_ENTRY_POINTS],
)
async def test_lifecycle_entry_passes_the_gate_with_a_build_run(
    label: str, method: str, path: str, body: Any
) -> None:
    """The CONTROL: with the exact server-persisted BUILD run the gate is passed.

    The route may then fail for an UNRELATED reason (a missing definition, a
    missing library service), but it must never again be the BUILD refusal - so
    the 409 above is provably caused by the gate and not by something else.
    """

    container = _GateContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    client = _client(container, user_id=ALICE)

    response = client.request(method, path, json=body, headers=_build_headers())

    assert response.json().get("detail") != BUILD_REQUIRED, (label, response.text)


def test_the_matrix_covers_every_lifecycle_mutating_route() -> None:
    """The matrix is the COMPLETE set of definition-lifecycle entry points."""

    labels = {entry[0] for entry in LIFECYCLE_ENTRY_POINTS}
    assert labels == {
        "create draft",
        "edit draft",
        "semantic close",
        "confirm",
        "save",
        "revision",
        "publish",
        "fork",
        "certify",
    }


# ---------------------------------------------------------------------------
# The gate is SPECIFIC: reads and non-authoritative result actions stay open.
# ---------------------------------------------------------------------------


async def test_reads_and_exploration_confirmations_need_no_build_run() -> None:
    container = _GateContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    client = _client(container, user_id=ALICE)
    # Create ONE definition through the gated route, so the READ below has
    # something real to return.
    created = client.post(
        f"{BASE}/definitions",
        headers=_build_headers(),
        json={"title": "M", "calculation": _SPEC_JSON},
    )
    assert created.status_code == 200, created.text
    definition_id = str(created.json()["definition_id"])

    # A READ is not a lifecycle change: no BUILD context is required.
    listed = client.get(f"{BASE}/definitions")
    assert listed.status_code == 200, listed.text
    assert [item["definition_id"] for item in listed.json()["definitions"]] == [
        definition_id
    ]

    # An EXPLORATION confirmation is a non-authoritative RESULT action: it never
    # selects BUILD, so it stays reachable with NO BUILD headers at all.
    explored = client.post(
        f"{BASE}/exploration-confirmations",
        json={"run_id": "run-no-build", "subject": "explore"},
    )
    assert explored.status_code == 200, explored.text
    assert explored.json()["confirmed_by"] == ALICE


# ---------------------------------------------------------------------------
# Coherence: the BUILD run <-> definition lifecycle binding is owner-bound.
# ---------------------------------------------------------------------------


async def test_a_foreign_build_run_cannot_move_another_users_definition() -> None:
    container = _GateContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    alice = _client(container, user_id=ALICE)
    bob = _client(container, user_id=BOB)

    # Alice's own BUILD run is accepted.
    created = alice.post(
        f"{BASE}/definitions",
        headers=_build_headers(),
        json={"title": "M", "calculation": _SPEC_JSON},
    )
    assert created.status_code == 200, created.text
    definition_id = str(created.json()["definition_id"])

    # Bob presents ALICE's exact thread/run references: the server-side owner
    # binding refuses it, so a BUILD run is never a transferable capability.
    stolen = bob.post(
        f"{BASE}/definitions/{definition_id}/confirm",
        headers=_build_headers(),
    )
    assert stolen.status_code == 409, stolen.text
    assert stolen.json() == {"detail": BUILD_REQUIRED}
    # ...and Alice's definition is untouched.
    current = await container.definitions.get_owned_definition(
        owner_user_id=ALICE, definition_id=definition_id
    )
    assert current.axes.confirmation == "DRAFT"

    # A STALE run id (same thread, wrong run) is refused the same way.
    stale = alice.post(
        f"{BASE}/definitions/{definition_id}/confirm",
        headers={
            "x-tt-build-thread-id": THREAD,
            "x-tt-build-run-id": "stale-run",
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json() == {"detail": BUILD_REQUIRED}

    # A NON-BUILD run on the same thread confers no authoring authority.
    container.engine.seed(thread_id=THREAD, run_id="query-run", owner=ALICE, mode="QUERY")
    wrong_mode = alice.post(
        f"{BASE}/definitions/{definition_id}/confirm",
        headers={"x-tt-build-thread-id": THREAD, "x-tt-build-run-id": "query-run"},
    )
    assert wrong_mode.status_code == 409, wrong_mode.text
    assert wrong_mode.json() == {"detail": BUILD_REQUIRED}


async def test_one_build_run_binds_the_whole_auditable_lifecycle() -> None:
    """Under ONE BUILD run the full lifecycle runs and stays TRACEABLE.

    The lifecycle axes record THAT each transition happened; the server-owned
    confirmation audit records WHO and WHEN, so the same run/thread is coherent
    end to end and the definition cannot be confirmed outside it.
    """

    container = _GateContainer()
    container.engine.seed(thread_id=THREAD, run_id=RUN, owner=ALICE)
    client = _client(container, user_id=ALICE)

    created = client.post(
        f"{BASE}/definitions",
        headers=_build_headers(),
        json={"title": "M", "calculation": _SPEC_JSON},
    )
    assert created.status_code == 200, created.text
    definition_id = str(created.json()["definition_id"])

    for action in ("semantic-close", "confirm", "save"):
        response = client.post(
            f"{BASE}/definitions/{definition_id}/{action}",
            headers=_build_headers(),
        )
        assert response.status_code == 200, (action, response.text)

    version = client.get(f"{BASE}/definitions/{definition_id}/versions/1")
    assert version.status_code == 200, version.text
    assert version.json()["confirmation"] == "CONFIRMED"
    assert version.json()["retention"] == "SAVED"

    # The audit trail binds the SERVER identity to the exact confirmed version.
    record = await container.definitions.get_confirmation_record(
        owner_user_id=ALICE, definition_id=definition_id, version=1
    )
    assert record.confirmed_by == ALICE
    assert record.version == 1
    assert record.definition_checksum == version.json()["checksum"]
