from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.api_conflicts import register_conflict_routes
from src.nl2sql.artifacts.library import InMemoryLibraryRepository
from src.nl2sql.artifacts.personal_conflict_product_service import (
    PersonalConflictProductService,
)
from src.nl2sql.artifacts.publication import PublicationCatalogue, PublishedVersion
from src.nl2sql.artifacts.publication_service import PublicationService
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    LiteralOperand,
)


def _spec(multiplier: str) -> CalculationSpec:
    return CalculationSpec(
        calculation_id="custom.archive_weighted",
        expression=BinaryOperand(
            op="multiply",
            left=InputRefOperand(role="actual"),
            right=LiteralOperand(value=Decimal(multiplier)),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual",
                provenance="published_gold",
                metric_key="repair_service_archive_rate_overall_day",
            ),
        ),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


class _Container:
    def __init__(self) -> None:
        self.definitions = CustomDefinitionService(
            governed_metric_keys={"repair_service_archive_rate_overall_day"}
        )
        self.catalogue = PublicationCatalogue()
        self.library = InMemoryLibraryRepository(catalogue=self.catalogue)
        self.publications = PublicationService(
            definitions=self.definitions,
            catalogue=self.catalogue,
        )
        self.conflicts = PersonalConflictProductService(
            definitions=self.definitions,
            catalogue=self.catalogue,
            library=self.library,
        )

    async def get_engine(self):
        class _Engine:
            async def aget_state(self, _config):
                return SimpleNamespace(
                    values={
                        "run_owner_user_id": "alice",
                        "run_envelope": {
                            "run_id": "run-personal-1",
                            "requested_mode": "BUILD",
                            "effective_mode": "BUILD",
                        },
                    }
                )

        return _Engine()

    def personal_conflict_product_service(self) -> PersonalConflictProductService:
        return self.conflicts


def _saved(container: _Container, multiplier: str, title: str) -> str:
    version = container.definitions.create_draft(
        owner_user_id="alice",
        title=title,
        calculation=_spec(multiplier),
    )
    container.definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=version.definition_id
    )
    container.definitions.confirm(
        owner_user_id="alice", definition_id=version.definition_id
    )
    container.definitions.save(
        owner_user_id="alice", definition_id=version.definition_id
    )
    return version.definition_id


def _client() -> tuple[TestClient, _Container, str, str]:
    app = FastAPI()
    container = _Container()
    app.state.container = container
    register_conflict_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    own_id = _saved(container, "1", "Archive performance")
    installed_id = _saved(container, "2", "Archive performance")
    container.publications.publish(
        owner_user_id="alice", definition_id=installed_id, version=1
    )
    container.library.install(
        user_id="alice", identity_id=installed_id, version=1
    )
    container.library.star(user_id="alice", identity_id=installed_id)
    container.catalogue.certify_local_demo(
        installed_id, 1, certified_by="controlled-test"
    )
    return TestClient(app), container, own_id, installed_id


def _get_conflict(client: TestClient, own_id: str, installed_id: str):
    return client.get(
        "/api/v2/nl2sql/conflicts/personal",
        params={
            "own_definition_id": own_id,
            "installed_identity_id": installed_id,
            "thread_id": "11111111-1111-1111-1111-111111111111",
            "run_id": "run-personal-1",
        },
    )


def test_personal_conflict_http_resolves_server_owned_candidates() -> None:
    client, _, own_id, installed_id = _client()

    response = _get_conflict(client, own_id, installed_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution"]["outcome"] == "clarification_required"
    block = body["conflict_comparison"]
    assert block["type"] == "conflict_comparison"
    assert {item["origin"] for item in block["candidates"]} == {
        "own",
        "installed",
    }
    installed = next(item for item in block["candidates"] if item["origin"] == "installed")
    assert installed["star_count"] == 1
    assert installed["certification_state"] == "certified"
    assert installed["risk_notes"]
    serialized = str(body).lower()
    assert "winner" not in serialized
    assert "recommend" not in serialized


def test_run_scoped_selection_re_resolves_and_never_persists_preference() -> None:
    client, container, own_id, installed_id = _client()
    conflict = _get_conflict(client, own_id, installed_id).json()
    block = conflict["conflict_comparison"]
    selected = block["candidates"][0]["candidate_id"]
    installs_before = container.library.installs_of(user_id="alice")
    stars_before = container.library.starred_of(user_id="alice")

    wrong_run = client.post(
        "/api/v2/nl2sql/conflicts/personal/select",
        json={
            "thread_id": "11111111-1111-1111-1111-111111111111",
            "run_id": "run-personal-2",
            "own_definition_id": own_id,
            "installed_identity_id": installed_id,
            "selection": {
                "conflict_id": block["conflict_id"],
                "required_slot": block["required_slot"],
                "selected_candidate_id": selected,
            },
        },
    )
    assert wrong_run.status_code == 409
    assert wrong_run.json()["detail"] == "personal_conflict_run_binding_invalid"

    response = client.post(
        "/api/v2/nl2sql/conflicts/personal/select",
        json={
            "thread_id": "11111111-1111-1111-1111-111111111111",
            "run_id": "run-personal-1",
            "own_definition_id": own_id,
            "installed_identity_id": installed_id,
            "selection": {
                "conflict_id": block["conflict_id"],
                "required_slot": block["required_slot"],
                "selected_candidate_id": selected,
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["selection"]["selection_scope"] == "run_scoped"
    assert container.library.installs_of(user_id="alice") == installs_before
    assert container.library.starred_of(user_id="alice") == stars_before
    resolved = _get_conflict(client, own_id, installed_id)
    assert resolved.status_code == 200
    assert resolved.json()["resolution"]["outcome"] == "resolved"
    selected_ref = next(
        item for item in block["candidates"] if item["candidate_id"] == selected
    )
    assert resolved.json()["selected_candidate_id"] == selected
    assert resolved.json()["selected_definition_id"] == selected_ref["definition_id"]
    assert resolved.json()["selected_version"] == selected_ref["version"]
    with pytest.raises(Exception, match="personal_selection_not_pending"):
        container.conflicts.consume_selection(
            user_id="alice",
            thread_id="11111111-1111-1111-1111-111111111111",
            run_id="run-personal-1",
            conflict_id=block["conflict_id"],
        )

    wrong = client.post(
        "/api/v2/nl2sql/conflicts/personal/select",
        json={
            "thread_id": "11111111-1111-1111-1111-111111111111",
            "run_id": "run-personal-1",
            "own_definition_id": own_id,
            "installed_identity_id": installed_id,
            "selection": {
                "conflict_id": block["conflict_id"],
                "required_slot": block["required_slot"],
                "selected_candidate_id": "candidate-not-present",
            },
        },
    )
    assert wrong.status_code == 409


def test_uninstalled_or_legacy_semantics_fail_closed() -> None:
    client, container, own_id, installed_id = _client()
    container.library.uninstall(user_id="alice", identity_id=installed_id)
    absent = _get_conflict(client, own_id, installed_id)
    assert absent.status_code == 404

    legacy_id = "legacy.display.only"
    container.catalogue.seed(
        PublishedVersion(
            identity_id=legacy_id,
            version=1,
            title="Legacy",
            owner_user_id="publisher",
            owner_label="Publisher",
            source_label="Legacy catalogue",
            definition_checksum="0" * 64,
            published_at="fixture",
        ),
        current=True,
    )
    container.library.install(user_id="alice", identity_id=legacy_id, version=1)
    legacy = _get_conflict(client, own_id, legacy_id)
    assert legacy.status_code == 409
    assert legacy.json()["detail"] == "personal_candidate_semantics_unavailable"


def test_fabricated_run_cannot_mint_conflict_state() -> None:
    client, _, own_id, installed_id = _client()
    response = client.get(
        "/api/v2/nl2sql/conflicts/personal",
        params={
            "own_definition_id": own_id,
            "installed_identity_id": installed_id,
            "thread_id": "11111111-1111-1111-1111-111111111111",
            "run_id": "fabricated-run",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "personal_conflict_run_binding_invalid"


def test_same_exact_identity_version_deduplicates_without_conflict() -> None:
    client, container, _, installed_id = _client()
    response = _get_conflict(client, installed_id, installed_id)
    assert response.status_code == 200
    assert response.json()["resolution"]["outcome"] == "resolved"
    assert response.json()["semantic_conflict"] is None
    assert response.json()["conflict_comparison"] is None


def test_historical_saved_candidate_survives_a_new_current_draft() -> None:
    client, container, own_id, installed_id = _client()
    container.definitions.create_revision(owner_user_id="alice", definition_id=own_id)
    response = _get_conflict(client, own_id, installed_id)
    assert response.status_code == 200, response.text
    own = next(
        item
        for item in response.json()["conflict_comparison"]["candidates"]
        if item["origin"] == "own"
    )
    assert own["definition_id"] == own_id
    assert own["version"] == 1
