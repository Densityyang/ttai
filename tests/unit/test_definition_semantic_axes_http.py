"""A6 semantic axes ON THE HTTP SURFACE: execution-level proof.

The failure mode this module exists to catch is "the service computes the A6
axis diff, but no reachable HTTP path ever shows it to a caller".  Every claim
below is observed by issuing a REAL request through the REAL FastAPI router
against a REAL AppContainer: none of it asserts on source text.

Covered:
  (1) a substantive change (population) over HTTP opens a NEW version and
      demands a NEW business decision;
  (2) a title-only edit does not create a version and preserves closure;
  (3) an in-contract parameter rebinding (A5) creates no version and does not
      move the version checksum;
  (4) a client can never inject authority / canonical / axes / confirmation,
      and a rejected request changes no state;
  (5) a pre-A6 request body keeps its exact pre-A6 behaviour;
  (6) the exact-version view exposes the axes that version DECLARES.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.core.settings import Settings
from src.nl2sql.artifacts.api_definitions import register_definition_routes
from src.nl2sql.container import AppContainer
from src.nl2sql.contracts import TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    LiteralOperand,
    ParameterBinding,
    ParameterRefOperand,
    ParameterSpec,
)

_PREFIX = "/api/v2/nl2sql/definitions"
_METRIC = "repair_service_archive_rate_overall_day"
_OWNER = "alice"
_THREAD = "11111111-1111-1111-1111-111111111111"
_RUN = "run_semantic_axes_http"


def _spec(multiplier: str = "100") -> CalculationSpec:
    """A REAL parameterized spec, so the execute path is exercised too."""

    return CalculationSpec(
        calculation_id="custom.actual_to_target_index",
        expression=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="actual"),
                right=ParameterRefOperand(name="target_percent"),
            ),
            right=LiteralOperand(value=Decimal(multiplier)),
        ),
        inputs=(
            CalculationInputSpec(
                role="actual",
                provenance="published_gold",
                metric_key=_METRIC,
            ),
        ),
        parameters=(ParameterSpec(name="target_percent", value_type="decimal"),),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


def _binding(target: int = 90) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id="custom.actual_to_target_index",
        spec_checksum=_spec().checksum,
        parameters=(ParameterBinding(name="target_percent", value=target),),
    )


class _Fetcher:
    """A governed fetcher, so the execute path really resolves an input."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_metric_input(self, **kwargs: object) -> ResolvedCalculationInput:
        self.calls += 1
        return ResolvedCalculationInput(
            role=str(kwargs["role"]),
            metric_key=str(kwargs["metric_key"]),
            value=Decimal("45"),
            unit="percent",
            data_as_of=datetime(2026, 9, 20, tzinfo=UTC),
            time_range=TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20)),
            provenance="published_gold",
            source_id="gold.repair.archive",
            receipt_step_id="fetch_actual",
            fact_id="a" * 64,
        )


class _BuildEngine:
    """The minimal server-side checkpoint a BUILD mutation capability needs."""

    def __init__(self, *, run_id: str, owner: str) -> None:
        self._values: dict[str, object] = {
            "run_envelope": RunEnvelope(
                run_id=run_id, requested_mode="BUILD", effective_mode="BUILD"
            ),
            "run_owner_user_id": owner,
        }

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(values=self._values)


async def _engine(engine: _BuildEngine) -> _BuildEngine:
    return engine


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """One real router over one real container, with a real BUILD capability."""

    import src.nl2sql.container as container_module

    monkeypatch.setattr(
        container_module,
        "get_settings",
        lambda: Settings(_env_file=None, service_mode="infra-dev"),  # pyright: ignore[reportCallIssue]
    )
    container = AppContainer(
        governed_metric_input_fetcher=_Fetcher(),
        governed_metric_key_resolver=lambda _key: True,
    )
    engine = _BuildEngine(run_id=_RUN, owner=_OWNER)
    cast(Any, container).get_engine = lambda: _engine(engine)
    app = FastAPI()
    app.state.container = container
    register_definition_routes(app)

    async def current_user() -> AuthUser:
        return AuthUser(
            user_id=_OWNER,
            telephone=None,
            roles=["analyst"],
            permissions=["*"],
        )

    app.dependency_overrides[require_nl2sql_permission] = current_user
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {"x-tt-build-thread-id": _THREAD, "x-tt-build-run-id": _RUN}


def _create(client: TestClient, title: str = "Actual to target") -> str:
    response = client.post(
        _PREFIX,
        headers=_headers(),
        json={"title": title, "calculation": _spec().model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["definition_id"])


def _act(client: TestClient, definition_id: str, action: str) -> Any:
    response = client.post(f"{_PREFIX}/{definition_id}/{action}", headers=_headers())
    assert response.status_code == 200, (action, response.text)
    return response


def _patch(client: TestClient, definition_id: str, body: dict[str, Any]) -> Any:
    return client.patch(
        f"{_PREFIX}/{definition_id}/draft", headers=_headers(), json=body
    )


def _version(client: TestClient, definition_id: str, version: int) -> Any:
    return client.get(
        f"{_PREFIX}/{definition_id}/versions/{version}", headers=_headers()
    )


def _owned(client: TestClient, definition_id: str) -> dict[str, Any]:
    body = client.get(_PREFIX, headers=_headers()).json()["definitions"]
    return next(item for item in body if item["definition_id"] == definition_id)


# ============================================================================
# (1) A substantive change opens a NEW version and requires a NEW decision.
# ============================================================================


def test_population_change_opens_a_new_version_and_requires_a_decision(
    client: TestClient,
) -> None:
    definition_id = _create(client)
    _act(client, definition_id, "semantic-close")

    before = _version(client, definition_id, 1)
    assert before.status_code == 200, before.text
    assert before.json()["semantic_closed"] is True
    assert before.json()["semantics"] is None
    assert before.json()["declared_axes"] == []

    response = _patch(
        client, definition_id, {"semantics": {"population": "paid orders"}}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # A6: the version number INCREASED...
    assert body["current_version"] == 2
    # ...the axes changed are reported EXACTLY...
    assert body["semantic_axes"] == ["population"]
    assert body["version_created"] is True
    assert body["requires_business_decision"] is True
    # ...and the OLD closure proof no longer covers the new semantics.
    assert body["semantic_closed"] is False

    # The new version is addressable over HTTP and declares the axis.
    v2 = _version(client, definition_id, 2)
    assert v2.status_code == 200, v2.text
    assert v2.json()["declared_axes"] == ["population"]
    assert v2.json()["semantics"]["population"] == "paid orders"
    assert v2.json()["semantic_closed"] is False

    # The superseded draft is NOT an addressable version.
    assert _version(client, definition_id, 1).status_code == 404

    # The caller can still see the decision requirement in the Definition view.
    owned = _owned(client, definition_id)
    assert owned["current_version"] == 2
    assert owned["semantic_closed"] is False


# ============================================================================
# (2) A title-only edit creates no version and PRESERVES closure.
# ============================================================================


def test_title_only_edit_keeps_the_version_and_preserves_closure(
    client: TestClient,
) -> None:
    definition_id = _create(client)
    _act(client, definition_id, "semantic-close")

    response = _patch(client, definition_id, {"title": "Renamed"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_version"] == 1
    assert body["semantic_axes"] == []
    assert body["version_created"] is False
    assert body["requires_business_decision"] is False
    assert body["title"] == "Renamed"
    # The closure SURVIVED, so confirmation still succeeds over HTTP.
    assert body["semantic_closed"] is True
    _act(client, definition_id, "confirm")


# ============================================================================
# (3) An in-contract parameter rebinding (A5) creates no version.
# ============================================================================


def test_parameter_rebinding_creates_no_version_and_keeps_the_checksum(
    client: TestClient,
) -> None:
    definition_id = _create(client)
    _act(client, definition_id, "semantic-close")
    _act(client, definition_id, "confirm")
    _act(client, definition_id, "save")

    before = _version(client, definition_id, 1).json()

    executed = client.post(
        f"{_PREFIX}/{definition_id}/versions/1/execute",
        headers=_headers(),
        json={"binding": _binding(90).model_dump(mode="json")},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "executed"
    assert executed.json()["version"] == 1
    assert executed.json()["definition_checksum"] == before["checksum"]

    after = _version(client, definition_id, 1).json()
    assert after["version"] == 1
    assert after["checksum"] == before["checksum"]
    assert after["declared_axes"] == []
    assert after["semantics"] is None
    assert _owned(client, definition_id)["current_version"] == 1


# ============================================================================
# (4) A client can never inject authority, and a refusal changes no state.
# ============================================================================


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "authority",
        "canonical",
        "axes",
        "confirmation",
        "governance",
        "semantic_closed",
        "publication",
        "certification",
        "owner_user_id",
        "current_version",
    ],
)
def test_client_cannot_inject_authority_into_a_draft_edit(
    client: TestClient, forbidden_field: str
) -> None:
    definition_id = _create(client)
    _act(client, definition_id, "semantic-close")
    before_version = _version(client, definition_id, 1).json()
    before_owned = _owned(client, definition_id)

    # (a) the claim at the TOP level of the edit body
    top = _patch(client, definition_id, {"title": "X", forbidden_field: "forged"})
    assert top.status_code == 422, (forbidden_field, top.text)
    # (b) the SAME claim hidden inside the semantic declaration
    nested = _patch(
        client,
        definition_id,
        {"semantics": {"population": "paid orders", forbidden_field: "forged"}},
    )
    assert nested.status_code == 422, (forbidden_field, nested.text)

    # Neither refused request moved ANY state.
    assert _version(client, definition_id, 1).json() == before_version
    assert _owned(client, definition_id) == before_owned


# ============================================================================
# (5) A pre-A6 body keeps its exact pre-A6 behaviour.
# ============================================================================


def test_pre_a6_bodies_keep_their_exact_behaviour(client: TestClient) -> None:
    definition_id = _create(client)

    # Declare one axis, so a later OMISSION has something to preserve.
    declared = _patch(
        client, definition_id, {"semantics": {"population": "paid orders"}}
    )
    assert declared.status_code == 200, declared.text
    assert declared.json()["current_version"] == 2
    assert declared.json()["semantic_axes"] == ["population"]
    _act(client, definition_id, "semantic-close")

    # (a) the pre-A6 title-only body: no new version, closure preserved, and the
    # declared axis is NOT silently cleared by the omitted semantics field.
    title_only = _patch(client, definition_id, {"title": "Renamed"})
    assert title_only.status_code == 200, title_only.text
    assert title_only.json()["current_version"] == 2
    assert title_only.json()["semantic_axes"] == []
    assert title_only.json()["version_created"] is False
    assert title_only.json()["requires_business_decision"] is False
    assert title_only.json()["semantic_closed"] is True
    assert _version(client, definition_id, 2).json()["semantics"]["population"] == (
        "paid orders"
    )

    # (b) the pre-A6 calculation body still moves the version boundary, now on
    # the expression axis.
    changed = _patch(
        client,
        definition_id,
        {"calculation": _spec(multiplier="1").model_dump(mode="json")},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["current_version"] == 3
    assert changed.json()["semantic_axes"] == ["expression"]
    assert changed.json()["version_created"] is True
    # The declaration was omitted, never cleared.
    assert _version(client, definition_id, 3).json()["semantics"]["population"] == (
        "paid orders"
    )


def test_an_explicit_empty_declaration_clears_the_axis(client: TestClient) -> None:
    """An EMPTY declaration is NOT the same as an ABSENT one.

    Absent means "leave the declaration unchanged" (the pre-A6 body).  An empty
    declaration means "this version declares nothing", which is itself a
    substantive change and therefore opens ANOTHER version.
    """

    definition_id = _create(client)
    declared = _patch(
        client, definition_id, {"semantics": {"population": "paid orders"}}
    )
    assert declared.status_code == 200, declared.text
    assert declared.json()["current_version"] == 2

    cleared = _patch(client, definition_id, {"semantics": {}})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["current_version"] == 3
    assert cleared.json()["semantic_axes"] == ["population"]
    assert cleared.json()["version_created"] is True
    assert cleared.json()["requires_business_decision"] is True

    v3 = _version(client, definition_id, 3).json()
    assert v3["declared_axes"] == []
    assert v3["semantics"] is not None
    assert v3["semantics"]["population"] is None


# ============================================================================
# (6) The exact-version view exposes the axes THAT version declares.
# ============================================================================


def test_version_view_exposes_the_declared_axes(client: TestClient) -> None:
    definition_id = _create(client)

    # A semantics-free version reads back as None / empty and NEVER errors.
    v1 = _version(client, definition_id, 1)
    assert v1.status_code == 200, v1.text
    assert v1.json()["semantics"] is None
    assert v1.json()["declared_axes"] == []

    declared = _patch(
        client,
        definition_id,
        {"semantics": {"unit_precision": "wan yuan", "population": "paid orders"}},
    )
    assert declared.status_code == 200, declared.text

    v2 = _version(client, definition_id, 2)
    assert v2.status_code == 200, v2.text
    body = v2.json()
    # The frozen reporting ORDER, not insertion order.
    assert body["declared_axes"] == ["population", "unit_precision"]
    assert body["semantics"]["population"] == "paid orders"
    assert body["semantics"]["unit_precision"] == "wan yuan"
    assert body["semantics"]["numerator"] is None
    # A version view carries NO object-level authority information at all.
    assert "authority" not in body
    assert "governance" not in body
