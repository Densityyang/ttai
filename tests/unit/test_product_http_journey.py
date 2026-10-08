"""HTTP product journey over ONE real app/container boundary.

Everything here goes through the real FastAPI routers, the real app-scoped
container and the real strict request contracts - no route-internal shortcuts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import create_app
from src.core.auth.provider import TTApiAuthProvider
from src.core.auth.types import AuthUser

ALICE = "alice"
BOB = "bob"
CERT_ADMIN = "local-cert-admin"
INVOKE = "nl2sql:invoke"


class _StubProvider(TTApiAuthProvider):
    """A controlled identity: the header names the caller, nothing else does."""

    async def authenticate_request(self, request: Any) -> AuthUser:
        user_id = request.headers.get("x-test-user", ALICE)
        return AuthUser(
            user_id=user_id,
            telephone=None,
            roles=["analyst"],
            permissions=[INVOKE],
        )


class _JourneyEngine:
    """Checkpoint-shaped controlled engine for BUILD capability setup."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, object]] = {}

    async def ainvoke(self, payload: dict[str, object], config: dict[str, object]) -> dict[str, object]:
        configurable = cast(dict[str, object], config["configurable"])
        key = str(configurable["thread_id"])
        envelope = cast(dict[str, object], payload["run_envelope"])
        owner = str(configurable["auth_user_id"])
        self.states[key] = {
            "run_envelope": envelope,
            "run_owner_user_id": owner,
        }
        return {
            "run_envelope": envelope,
            "response_blocks": [{"type": "text", "text": "BUILD ready"}],
        }

    async def aget_state(self, config: dict[str, object]) -> Any:
        configurable = cast(dict[str, object], config["configurable"])
        return SimpleNamespace(values=self.states.get(str(configurable["thread_id"])))

    async def astream_events(self, *_: object, **__: object):
        if False:
            yield None


async def _fake_get_engine(engine: _JourneyEngine) -> _JourneyEngine:
    return engine


@pytest.fixture()
def journey(monkeypatch: pytest.MonkeyPatch) -> Any:
    """One app instance, one container, controlled auth, local-real profile."""

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("TT_API_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    monkeypatch.setenv("TYPED_RUNTIME_ACTIVATION", "local_real_data_demo")
    monkeypatch.setenv("LOCAL_DEMO_CERTIFICATION_ADMIN_USER_ID", CERT_ADMIN)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173")
    from src.core.settings import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[TTApiAuthProvider] = _StubProvider
    from src.core.auth.dependencies import get_auth_provider

    app.dependency_overrides[get_auth_provider] = lambda: _StubProvider()
    with TestClient(app) as client:
        engine = _JourneyEngine()
        container = client.app.state.container
        container._local_real_readiness = {"status": "ready"}
        container.get_engine = lambda: _fake_get_engine(engine)
        refs: dict[str, tuple[str, str]] = {}
        for user in (ALICE, BOB, CERT_ADMIN):
            thread_id = str(uuid4())
            response = client.post(
                "/api/v2/nl2sql/queries",
                headers={"x-test-user": user},
                json={
                    "thread_id": thread_id,
                    "requested_mode": "BUILD",
                    "messages": [{"role": "user", "content": "build"}],
                },
            )
            assert response.status_code == 200, response.text
            refs[user] = (thread_id, str(response.json()["run_id"]))
        client._build_refs = refs
        yield client
    get_settings.cache_clear()


def _spec(calculation_id: str = "calc.rate") -> dict[str, Any]:
    return {
        "calculation_id": calculation_id,
        "expression": {"op": "literal", "value": 1},
        "inputs": [
            {
                "role": "numerator",
                "provenance": "published_gold",
                "metric_key": "repair_service_archive_rate_overall_day",
            }
        ],
        "unit": "ratio",
        "parameters": [
            {
                "name": "threshold",
                "value_type": "integer",
                "required": True,
                "allowed_values": ["1", "2"],
            }
        ],
    }


def _as(client: TestClient, user: str) -> dict[str, str]:
    thread_id, run_id = client._build_refs[user]
    return {
        "x-test-user": user,
        "X-TT-Build-Thread-ID": thread_id,
        "X-TT-Build-Run-ID": run_id,
    }


def _create(client: TestClient, user: str, title: str = "Rate") -> str:
    response = client.post(
        "/api/v2/nl2sql/definitions",
        headers=_as(client, user),
        json={"title": title, "calculation": _spec()},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["definition_id"])


def _confirm_save(client: TestClient, user: str, definition_id: str) -> None:
    for action in ("semantic-close", "confirm", "save"):
        response = client.post(
            f"/api/v2/nl2sql/definitions/{definition_id}/{action}",
            headers=_as(client, user),
        )
        assert response.status_code == 200, (action, response.text)


def _publish(
    client: TestClient, user: str, definition_id: str, version: int
) -> dict[str, Any]:
    response = client.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/{version}/publish",
        headers=_as(client, user),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- the full vertical journey -----------------------------------------------


def test_alice_full_product_journey(journey: TestClient) -> None:
    definition_id = _create(journey, ALICE)
    assert definition_id.startswith("def_")
    _confirm_save(journey, ALICE, definition_id)
    published = _publish(journey, ALICE, definition_id, 1)
    assert published["publication"] == "PUBLISHED"
    assert published["confirmation"] == "CONFIRMED"
    assert published["retention"] == "SAVED"

    # catalogue sees it immediately, through the SAME container catalogue
    catalogue = journey.get(
        "/api/v2/nl2sql/library/catalogue", headers=_as(journey, ALICE)
    ).json()["entries"]
    row = next(entry for entry in catalogue if entry["identity_id"] == definition_id)
    assert row["current_version"] == 1
    assert [item["version"] for item in row["versions"]] == [1]
    assert row["versions"][0]["forkable"] is True

    install = journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 1},
    )
    assert install.status_code == 200, install.text
    star = journey.post(
        "/api/v2/nl2sql/library/star",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id},
    )
    assert star.json()["star_count"] == 1

    # a revision opens v2 while v1 stays immutable
    revision = journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/revisions",
        headers=_as(journey, ALICE),
    )
    assert revision.status_code == 200, revision.text
    assert revision.json()["current_version"] == 2
    assert revision.json()["confirmation"] == "DRAFT"
    assert revision.json()["retention"] == "SESSION"
    assert revision.json()["semantic_closed"] is False

    # v1's EXACT lifecycle survived the revision
    v1 = journey.get(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1",
        headers=_as(journey, ALICE),
    ).json()
    assert (v1["confirmation"], v1["retention"]) == ("CONFIRMED", "SAVED")
    assert v1["publication"] == "PUBLISHED"

    _confirm_save(journey, ALICE, definition_id)
    _publish(journey, ALICE, definition_id, 2)

    catalogue = journey.get(
        "/api/v2/nl2sql/library/catalogue", headers=_as(journey, ALICE)
    ).json()["entries"]
    row = next(entry for entry in catalogue if entry["identity_id"] == definition_id)
    assert row["current_version"] == 2
    assert [item["version"] for item in row["versions"]] == [1, 2]

    library = journey.get(
        "/api/v2/nl2sql/library", headers=_as(journey, ALICE)
    ).json()["entries"]
    entry = next(item for item in library if item["identity_id"] == definition_id)
    assert entry["installed_version"] == 1, "install must stay pinned at v1"
    assert entry["update_available"] is True
    assert entry["starred"] is True

    upgrade = journey.post(
        "/api/v2/nl2sql/library/upgrade",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "to_version": 2},
    )
    assert upgrade.json()["installed_version"] == 2
    library = journey.get(
        "/api/v2/nl2sql/library", headers=_as(journey, ALICE)
    ).json()["entries"]
    entry = next(item for item in library if item["identity_id"] == definition_id)
    assert entry["starred"] is True, "Star is identity-scoped"

    # certification by the CONFIGURED local admin only
    certify = journey.post(
        "/api/v2/nl2sql/library/certify",
        headers=_as(journey, CERT_ADMIN),
        json={"identity_id": definition_id, "version": 2},
    )
    assert certify.status_code == 200, certify.text
    assert certify.json()["certification_state"] == "certified"
    assert certify.json()["authority_provenance"] == "local_demo_certification"
    assert certify.json()["production_certification"] == "NOT_CONNECTED"

    v1_after = journey.get(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1",
        headers=_as(journey, ALICE),
    ).json()
    assert v1_after["certification"] == "UNCERTIFIED", "v1 must be unchanged"
    assert v1_after["checksum"] == v1["checksum"]

    # withdrawal by the SOURCE OWNER
    withdraw = journey.post(
        "/api/v2/nl2sql/library/withdraw",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 2},
    )
    assert withdraw.status_code == 200, withdraw.text
    assert withdraw.json()["withdrawn"] is True

    library = journey.get(
        "/api/v2/nl2sql/library", headers=_as(journey, ALICE)
    ).json()["entries"]
    entry = next(item for item in library if item["identity_id"] == definition_id)
    assert entry["installed_version"] == 2, "withdrawal never uninstalls"
    assert entry["withdrawn"] is True

    ack = journey.post(
        "/api/v2/nl2sql/library/acknowledge-withdrawal",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 2},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["withdrawn"] is True, "acknowledgement never clears it"
    assert ack.json()["withdrawal_acknowledged"] is True

    # fork the INSTALLED exact version
    fork = journey.post(
        "/api/v2/nl2sql/library/fork",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 2, "title": "Rate fork"},
    )
    assert fork.status_code == 200, fork.text
    body = fork.json()
    assert body["forked_definition_id"] != definition_id, "NEW identity"
    assert body["derived_from_definition_id"] == definition_id
    assert body["derived_from_version"] == 2

    forked = journey.get(
        f"/api/v2/nl2sql/definitions/{body['forked_definition_id']}/versions/1",
        headers=_as(journey, ALICE),
    ).json()
    assert forked["confirmation"] == "DRAFT"
    assert forked["retention"] == "SESSION"
    assert forked["semantic_closed"] is False
    assert forked["publication"] == "UNPUBLISHED"
    assert forked["certification"] == "UNCERTIFIED"

    # the fork is immediately visible in the caller's own Definition list
    owned = journey.get(
        "/api/v2/nl2sql/definitions", headers=_as(journey, ALICE)
    ).json()["definitions"]
    assert body["forked_definition_id"] in {item["definition_id"] for item in owned}

    # no inherited Star and no inherited certification on the fork identity
    forked_star = journey.post(
        "/api/v2/nl2sql/library/unstar",
        headers=_as(journey, ALICE),
        json={"identity_id": body["forked_definition_id"]},
    )
    assert forked_star.json()["star_count"] == 0


# --- historical exact-version publication ------------------------------------


def test_historical_version_stays_publishable_after_a_later_revision(
    journey: TestClient,
) -> None:
    definition_id = _create(journey, ALICE)
    _confirm_save(journey, ALICE, definition_id)
    # v2 is opened BEFORE v1 is ever published
    revision = journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/revisions",
        headers=_as(journey, ALICE),
    )
    assert revision.json()["current_version"] == 2

    published = _publish(journey, ALICE, definition_id, 1)
    assert published["publication"] == "PUBLISHED"
    assert (published["confirmation"], published["retention"]) == (
        "CONFIRMED",
        "SAVED",
    )

    # the CURRENT v2 axes are untouched by the historical publication
    current = journey.get(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/2",
        headers=_as(journey, ALICE),
    ).json()
    assert current["publication"] == "UNPUBLISHED"
    assert current["certification"] == "UNCERTIFIED"
    assert current["confirmation"] == "DRAFT"
    assert current["retention"] == "SESSION"
    owned = journey.get(
        "/api/v2/nl2sql/definitions", headers=_as(journey, ALICE)
    ).json()["definitions"]
    row = next(item for item in owned if item["definition_id"] == definition_id)
    assert row["publication"] == "UNPUBLISHED", "current axes must not be projected"
    assert row["current_version"] == 2

    # re-publishing the SAME exact version is an immutable conflict
    repeat = journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/publish",
        headers=_as(journey, ALICE),
    )
    assert repeat.status_code == 409

    # and v2 can still complete and publish normally
    _confirm_save(journey, ALICE, definition_id)
    _publish(journey, ALICE, definition_id, 2)
    catalogue = journey.get(
        "/api/v2/nl2sql/library/catalogue", headers=_as(journey, ALICE)
    ).json()["entries"]
    row = next(entry for entry in catalogue if entry["identity_id"] == definition_id)
    assert row["current_version"] == 2
    assert [item["version"] for item in row["versions"]] == [1, 2]


def test_unpublished_historical_version_is_not_eligible(journey: TestClient) -> None:
    definition_id = _create(journey, ALICE)
    _confirm_save(journey, ALICE, definition_id)
    journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/revisions",
        headers=_as(journey, ALICE),
    )
    # v1 is CONFIRMED/SAVED but v2 is a DRAFT; v2 must not be publishable
    response = journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/2/publish",
        headers=_as(journey, ALICE),
    )
    assert response.status_code == 409
    assert response.json()["detail"] != "publication_already_exists"


# --- ownership / no existence oracle -----------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "/versions/1", None),
        ("patch", "/draft", {"title": "hijacked"}),
        ("post", "/semantic-close", None),
        ("post", "/confirm", None),
        ("post", "/save", None),
        ("post", "/revisions", None),
        ("post", "/versions/1/publish", None),
        (
            "post",
            "/versions/1/execute",
            {
                "binding": {
                    "calculation_id": "calc.rate",
                    "spec_checksum": "0" * 64,
                    "parameters": [{"name": "threshold", "value": 1}],
                }
            },
        ),
    ],
)
def test_bob_never_distinguishes_foreign_from_absent(
    journey: TestClient, method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    definition_id = _create(journey, ALICE)
    absent = "def_" + "0" * 32
    call = getattr(journey, method)
    foreign = call(
        f"/api/v2/nl2sql/definitions/{definition_id}{suffix}",
        headers=_as(journey, BOB),
        **({} if body is None else {"json": body}),
    )
    missing = call(
        f"/api/v2/nl2sql/definitions/{absent}{suffix}",
        headers=_as(journey, BOB),
        **({} if body is None else {"json": body}),
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "definition_not_found"}


def test_bob_can_discover_and_use_alices_published_content_but_not_her_draft(
    journey: TestClient,
) -> None:
    definition_id = _create(journey, ALICE)
    _confirm_save(journey, ALICE, definition_id)
    _publish(journey, ALICE, definition_id, 1)

    catalogue = journey.get(
        "/api/v2/nl2sql/library/catalogue", headers=_as(journey, BOB)
    )
    assert catalogue.status_code == 200
    assert definition_id in {e["identity_id"] for e in catalogue.json()["entries"]}

    install = journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, BOB),
        json={"identity_id": definition_id, "version": 1},
    )
    assert install.status_code == 200
    assert journey.post(
        "/api/v2/nl2sql/library/star",
        headers=_as(journey, BOB),
        json={"identity_id": definition_id},
    ).status_code == 200

    # ...but her PRIVATE definition stays invisible
    private = journey.get(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1",
        headers=_as(journey, BOB),
    )
    assert private.status_code == 404
    assert journey.get(
        "/api/v2/nl2sql/definitions", headers=_as(journey, BOB)
    ).json()["definitions"] == []


# --- authority negatives ------------------------------------------------------


def _published(journey: TestClient, user: str = ALICE) -> str:
    definition_id = _create(journey, user)
    _confirm_save(journey, user, definition_id)
    _publish(journey, user, definition_id, 1)
    return definition_id


def test_certification_requires_the_configured_admin(journey: TestClient) -> None:
    definition_id = _published(journey)
    for user in (ALICE, BOB):
        response = journey.post(
            "/api/v2/nl2sql/library/certify",
            headers=_as(journey, user),
            json={"identity_id": definition_id, "version": 1},
        )
        assert response.status_code == 403, (user, response.text)
    assert journey.post(
        "/api/v2/nl2sql/library/certify",
        headers=_as(journey, CERT_ADMIN),
        json={"identity_id": definition_id, "version": 1},
    ).status_code == 200


def test_withdrawal_requires_the_source_owner(journey: TestClient) -> None:
    definition_id = _published(journey)
    # Bob INSTALLS and STARS it; that grants no authority whatsoever.
    journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, BOB),
        json={"identity_id": definition_id, "version": 1},
    )
    journey.post(
        "/api/v2/nl2sql/library/star",
        headers=_as(journey, BOB),
        json={"identity_id": definition_id},
    )
    denied = journey.post(
        "/api/v2/nl2sql/library/withdraw",
        headers=_as(journey, BOB),
        json={"identity_id": definition_id, "version": 1},
    )
    assert denied.status_code == 403
    allowed = journey.post(
        "/api/v2/nl2sql/library/withdraw",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 1},
    )
    assert allowed.status_code == 200


def test_legacy_fixture_publication_is_not_forkable(journey: TestClient) -> None:
    identity = "demo.metric.margin"
    journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": identity, "version": 1},
    )
    response = journey.post(
        "/api/v2/nl2sql/library/fork",
        headers=_as(journey, ALICE),
        json={"identity_id": identity, "version": 1, "title": "F"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "publication_not_forkable"


def test_fork_requires_an_install_of_that_exact_version(journey: TestClient) -> None:
    definition_id = _published(journey)
    response = journey.post(
        "/api/v2/nl2sql/library/fork",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 1, "title": "F"},
    )
    assert response.status_code == 404


def test_acknowledgement_requires_the_installed_exact_version(
    journey: TestClient,
) -> None:
    definition_id = _create(journey, ALICE)
    _confirm_save(journey, ALICE, definition_id)
    _publish(journey, ALICE, definition_id, 1)
    journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/revisions",
        headers=_as(journey, ALICE),
    )
    _confirm_save(journey, ALICE, definition_id)
    _publish(journey, ALICE, definition_id, 2)
    journey.post(
        "/api/v2/nl2sql/library/withdraw",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 2},
    )
    journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 1},
    )
    # installed v1 must NOT be able to acknowledge the v2 withdrawal
    wrong = journey.post(
        "/api/v2/nl2sql/library/acknowledge-withdrawal",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 2},
    )
    assert wrong.status_code == 409


# --- install / star / upgrade validation --------------------------------------


def test_install_requires_an_exact_existing_publication(journey: TestClient) -> None:
    definition_id = _published(journey)
    missing_version = journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 99},
    )
    assert missing_version.status_code == 404
    unknown_identity = journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": "not.a.real.identity", "version": 1},
    )
    assert unknown_identity.status_code == 404


def test_star_cannot_invent_a_catalogue_identity(journey: TestClient) -> None:
    response = journey.post(
        "/api/v2/nl2sql/library/star",
        headers=_as(journey, ALICE),
        json={"identity_id": "not.a.real.identity"},
    )
    assert response.status_code == 404


def test_upgrade_target_must_exist_and_is_never_automatic(journey: TestClient) -> None:
    definition_id = _published(journey)
    journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "version": 1},
    )
    skipped = journey.post(
        "/api/v2/nl2sql/library/upgrade",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id, "to_version": 5},
    )
    assert skipped.status_code == 404
    # the install is untouched by the failed upgrade
    library = journey.get(
        "/api/v2/nl2sql/library", headers=_as(journey, ALICE)
    ).json()["entries"]
    entry = next(item for item in library if item["identity_id"] == definition_id)
    assert entry["installed_version"] == 1


def test_uninstall_and_unstar_are_idempotent(journey: TestClient) -> None:
    definition_id = _published(journey)
    assert journey.post(
        "/api/v2/nl2sql/library/uninstall",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id},
    ).status_code == 200
    assert journey.post(
        "/api/v2/nl2sql/library/unstar",
        headers=_as(journey, ALICE),
        json={"identity_id": definition_id},
    ).status_code == 200


# --- execute -------------------------------------------------------------------


def test_execute_rejects_a_non_local_real_identity_before_governed_fetch(
    journey: TestClient,
) -> None:
    definition_id = _published(journey)
    detail = journey.get(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1",
        headers=_as(journey, ALICE),
    ).json()
    binding = {
        "calculation_id": detail["calculation"]["calculation_id"],
        "spec_checksum": detail["calculation_checksum"],
        "parameters": [{"name": "threshold", "value": 1}],
    }
    ok = journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute",
        headers=_as(journey, ALICE),
        json={"binding": binding},
    )
    assert ok.status_code == 409, ok.text
    assert ok.json()["detail"] == "local_real_governed_identity_mismatch"

    # a disallowed parameter value is a binding conflict, not a result
    bad = journey.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute",
        headers=_as(journey, ALICE),
        json={"binding": {**binding, "parameters": [{"name": "threshold", "value": 9}]}},
    )
    assert bad.status_code == 409


# --- strict client contract -----------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "owner_user_id",
        "user_id",
        "roles",
        "permissions",
        "authorization",
        "scope",
        "semantic_closed",
        "confirmation",
        "retention",
        "publication",
        "certification",
        "current_version",
        "star_count",
        "authority_provenance",
        "parameter_contract",
    ],
)
def test_client_cannot_supply_server_owned_fields(
    journey: TestClient, forbidden_field: str
) -> None:
    response = journey.post(
        "/api/v2/nl2sql/definitions",
        headers=_as(journey, ALICE),
        json={"title": "T", "calculation": _spec(), forbidden_field: "x"},
    )
    assert response.status_code == 422, (forbidden_field, response.text)


def test_unknown_field_on_a_library_mutation_is_rejected(journey: TestClient) -> None:
    response = journey.post(
        "/api/v2/nl2sql/library/install",
        headers=_as(journey, ALICE),
        json={"identity_id": "demo.metric.margin", "version": 1, "user_id": "bob"},
    )
    assert response.status_code == 422


def test_published_version_state_is_never_taken_from_the_client(
    journey: TestClient,
) -> None:
    """Sending axes on a draft edit is rejected, so they cannot be forged."""

    definition_id = _create(journey, ALICE)
    response = journey.patch(
        f"/api/v2/nl2sql/definitions/{definition_id}/draft",
        headers=_as(journey, ALICE),
        json={"title": "X", "confirmation": "CONFIRMED", "retention": "SAVED"},
    )
    assert response.status_code == 422


# --- app-scoped shared state ----------------------------------------------------


async def test_services_share_one_catalogue_and_one_definition_service(
    journey: TestClient,
) -> None:
    container = cast(Any, journey.app).state.container
    assert container.custom_definition_service() is container.custom_definition_service()
    assert await container.publication_catalogue() is await container.publication_catalogue()
    assert (await container.publication_service())._definitions is container.custom_definition_service()
    assert (await container.publication_service())._catalogue is await container.publication_catalogue()
    library_service = await container.product_library_service()
    assert library_service is await container.product_library_service()
    assert library_service._catalogue is await container.publication_catalogue()
    assert library_service._library is await container.library_repository()
    assert library_service._definitions is container.custom_definition_service()
