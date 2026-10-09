"""Execution-level HTTP tests for the per-run audit record route (A7, 5.2.2).

Every SAVED rerun lands a DefinitionRunRecord (binding / plan / validation /
receipt / degradations) in the execution service's run-record port.  These tests
prove those records are REACHABLE over HTTP -- not merely queryable in-process --
and that the reader reuses the existing definition-reader owner semantics
(foreign == absent == 404, never 403).

Nothing here asserts on source text: every claim below is observed by issuing a
real request through the FastAPI router and reading the response.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import (
    _required_nl2sql_permission,
    require_nl2sql_permission,
    require_user,
)
from src.core.auth.types import AuthUser
from src.core.settings import Settings, get_settings
from src.nl2sql.artifacts.api_definitions import register_definition_routes
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.container import AppContainer
from src.nl2sql.contracts import TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
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

_METRIC = "repair_service_archive_rate_overall_day"
_PREFIX = "/api/v2/nl2sql/definitions"

# The five section 8.19 external authority checks a stock deployment does not
# wire.  A non-product rerun records each as an explicit UNCONFIGURED
# degradation; product mode refuses instead and never degrades.
_UNCONFIGURED_CHECKS = frozenset(
    {
        "authorization_evidence_unconfigured",
        "active_release_unconfigured",
        "data_snapshot_unconfigured",
        "freshness_evidence_unconfigured",
        "budget_evidence_unconfigured",
    }
)


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="custom.actual_to_target_index",
        expression=BinaryOperand(
            op="multiply",
            left=BinaryOperand(
                op="divide",
                left=InputRefOperand(role="actual"),
                right=ParameterRefOperand(name="target_percent"),
            ),
            right=LiteralOperand(value=Decimal("100")),
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
    """A governed fetcher whose call count is the reachability evidence."""

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


def _product_settings() -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        service_mode="product",
        auth_enabled=True,
        tt_api_base_url="http://auth.invalid",
        cors_allowed_origins="https://app.example",
        database_url="postgresql+asyncpg://svc_app:pw@127.0.0.1:5432/app",
    )


@pytest.fixture
def infra_container(monkeypatch: pytest.MonkeyPatch) -> AppContainer:
    """A container whose deployment is explicitly NOT product mode."""

    import src.nl2sql.container as container_module

    monkeypatch.setattr(
        container_module,
        "get_settings",
        lambda: Settings(_env_file=None, service_mode="infra-dev"),  # pyright: ignore[reportCallIssue]
    )
    return AppContainer(
        governed_metric_input_fetcher=_Fetcher(),
        governed_metric_key_resolver=lambda _key: True,
    )


@pytest.fixture
def product_container(monkeypatch: pytest.MonkeyPatch) -> AppContainer:
    """A container whose deployment IS product mode (strict, fail-closed)."""

    import src.nl2sql.container as container_module

    monkeypatch.setattr(container_module, "get_settings", _product_settings)
    return AppContainer(
        governed_metric_input_fetcher=_Fetcher(),
        governed_metric_key_resolver=lambda _key: True,
    )


def _client_for(container: Any, *, identity: str) -> TestClient:
    app = FastAPI()
    app.state.container = container
    register_definition_routes(app)

    async def current_user() -> AuthUser:
        return AuthUser(
            user_id=identity,
            telephone=None,
            roles=["analyst"],
            permissions=["*"],
        )

    app.dependency_overrides[require_nl2sql_permission] = current_user
    return TestClient(app)


async def _saved_definition(container: AppContainer, *, owner: str) -> str:
    service = container.custom_definition_service()
    draft = await service.create_draft(
        owner_user_id=owner, title="Actual to target", calculation=_spec()
    )
    await service.mark_semantic_closed(
        owner_user_id=owner, definition_id=draft.definition_id
    )
    await service.confirm(owner_user_id=owner, definition_id=draft.definition_id)
    await service.save(owner_user_id=owner, definition_id=draft.definition_id)
    return draft.definition_id


def _execute(client: TestClient, definition_id: str, target: int = 90) -> Any:
    return client.post(
        f"{_PREFIX}/{definition_id}/versions/1/execute",
        json={"binding": _binding(target).model_dump(mode="json")},
    )


def _runs(client: TestClient, definition_id: str, version: int = 1) -> Any:
    return client.get(f"{_PREFIX}/{definition_id}/versions/{version}/runs")


# ============================================================================
# (1) An owner reads back the record of a rerun it just ran, over HTTP.
# ============================================================================


async def test_owner_reads_own_run_records_over_http(
    infra_container: AppContainer,
) -> None:
    definition_id = await _saved_definition(infra_container, owner="alice")
    client = _client_for(infra_container, identity="alice")

    executed = _execute(client, definition_id)
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "executed"

    response = _runs(client, definition_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["definition_id"] == definition_id
    assert body["version"] == 1
    assert len(body["runs"]) == 1

    run = body["runs"][0]
    assert run["run_id"].startswith("run_")
    assert run["definition_id"] == definition_id
    assert run["version"] == 1
    # binding
    assert run["binding"]["definition_id"] == definition_id
    assert run["binding"]["version"] == 1
    assert run["binding"]["binding"]["parameters"]
    # plan
    assert run["plan"]["calculation_id"] == "custom.actual_to_target_index"
    assert run["plan"]["binding_checksum"] == run["binding"]["binding"]["checksum"]
    assert run["plan"]["execution_context"]["date_mode"] == "latest_authoritative"
    # validation: BOTH revalidation phases are present
    assert run["validation"]["branch"] == "EXECUTE"
    assert run["validation"]["before_resolution"]["branch"] == "EXECUTE"
    assert run["validation"]["resolved_inputs"]["branch"] == "EXECUTE"
    # receipt
    assert run["receipt"]["result"]["value"] == "50.00"
    assert run["receipt"]["result"]["calculation_scope"] == (
        "reusable_custom_definition"
    )

    # The single-record reader exposes the SAME record.
    one = client.get(
        f"{_PREFIX}/{definition_id}/versions/1/runs/{run['run_id']}"
    )
    assert one.status_code == 200, one.text
    assert one.json() == run


# ============================================================================
# (2) Two reruns are TWO records with DIFFERENT bindings (no cached record).
# ============================================================================


async def test_two_reruns_are_two_records_with_distinct_bindings(
    infra_container: AppContainer,
) -> None:
    definition_id = await _saved_definition(infra_container, owner="alice")
    client = _client_for(infra_container, identity="alice")

    assert _execute(client, definition_id, 90).status_code == 200
    assert _execute(client, definition_id, 80).status_code == 200

    runs = _runs(client, definition_id).json()["runs"]
    assert len(runs) == 2
    first, second = runs
    assert first["run_id"] != second["run_id"]
    assert (
        first["binding"]["binding"]["checksum"]
        != second["binding"]["binding"]["checksum"]
    )
    assert first["plan"]["binding_checksum"] != second["plan"]["binding_checksum"]


# ============================================================================
# (3) Cross-user access is 404 -- identical to absent, never an oracle.
# ============================================================================


async def test_cross_user_run_history_is_404_like_absent(
    infra_container: AppContainer,
) -> None:
    definition_id = await _saved_definition(infra_container, owner="alice")
    alice = _client_for(infra_container, identity="alice")
    assert _execute(alice, definition_id).status_code == 200

    bob = _client_for(infra_container, identity="bob")
    foreign = _runs(bob, definition_id)
    absent = _runs(bob, "def_" + "f" * 32)

    assert foreign.status_code == 404, foreign.text
    assert absent.status_code == 404, absent.text
    # Bob cannot distinguish alice's real definition from one that never existed.
    assert foreign.json() == absent.json() == {"detail": "definition_not_found"}
    assert definition_id not in foreign.text


# ============================================================================
# (4a) NON-product mode: the skipped authority checks reach the audit route.
# ============================================================================


async def test_non_product_degradations_are_visible_on_the_route(
    infra_container: AppContainer,
) -> None:
    definition_id = await _saved_definition(infra_container, owner="alice")
    client = _client_for(infra_container, identity="alice")

    executed = _execute(client, definition_id)
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "executed"
    assert set(executed.json()["degradations"]) == set(_UNCONFIGURED_CHECKS)

    runs = _runs(client, definition_id).json()["runs"]
    assert len(runs) == 1
    record = runs[0]
    codes = set(record["degradations"])
    assert codes == set(_UNCONFIGURED_CHECKS)
    for code in codes:
        assert code.endswith("_unconfigured"), code
    # The per-phase carriers agree with the merged, record-level codes.
    before = set(record["validation"]["before_resolution"]["degradations"])
    resolved = set(record["validation"]["resolved_inputs"]["degradations"])
    assert before, "the pre-resolution phase must name its skipped checks"
    assert resolved, "the resolved-input phase must name its skipped checks"
    assert before | resolved == set(_UNCONFIGURED_CHECKS)


# ============================================================================
# (4b) product mode: nothing is skipped, so there is NO degradation.
# ============================================================================


async def test_product_mode_records_carry_no_degradations(
    product_container: AppContainer,
) -> None:
    definition_id = await _saved_definition(product_container, owner="alice")
    client = _client_for(product_container, identity="alice")

    executed = _execute(client, definition_id)
    assert executed.status_code == 200, executed.text
    # Strict product mode cannot SKIP: with no wired authority it is UNAVAILABLE.
    assert executed.json()["status"] == "result_unavailable"

    runs = _runs(client, definition_id).json()["runs"]
    assert len(runs) == 1
    record = runs[0]
    assert record["degradations"] == []
    assert record["validation"]["branch"] == "UNAVAILABLE"
    assert record["receipt"] is None
    assert record["validation"]["before_resolution"]["degradations"] == []


# ============================================================================
# (5) The new routes are EXPLICITLY authorized through the REAL dependency.
# ============================================================================


class _StubContainer:
    """Only what a definition READER needs; no execution service is reached."""

    def __init__(self) -> None:
        self._service = CustomDefinitionService(governed_metric_keys={_METRIC})

    def custom_definition_service(self) -> CustomDefinitionService:
        return self._service


def test_runs_routes_are_explicitly_authorized_not_policy_missing() -> None:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth_required_permission_invoke="nl2sql:invoke",
        auth_required_permission_stream="nl2sql:stream",
    )

    app = FastAPI()
    app.state.container = _StubContainer()
    register_definition_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice",
            telephone=None,
            roles=["analyst"],
            permissions=["nl2sql:invoke"],
        )

    # require_nl2sql_permission is NOT overridden: the REAL policy mapping runs.
    app.dependency_overrides[require_user] = identity
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)

    missing = "def_" + "a" * 32
    collection = client.get(f"{_PREFIX}/{missing}/versions/1/runs")
    single = client.get(f"{_PREFIX}/{missing}/versions/1/runs/run_" + "b" * 32)
    for response in (collection, single):
        # 404 (unknown definition), NEVER 403 AUTH_PERMISSION_POLICY_MISSING:
        # the route reached its handler because it IS mapped by name.
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "definition_not_found"}

    # ...and it is the SAME permission tier as every other definition route.
    assert (
        _required_nl2sql_permission(
            "GET", f"{_PREFIX}/{missing}/versions/1/runs", settings
        )
        == "nl2sql:invoke"
    )
    assert (
        _required_nl2sql_permission(
            "GET",
            f"{_PREFIX}/{missing}/versions/1/runs/run_" + "b" * 32,
            settings,
        )
        == "nl2sql:invoke"
    )


# ============================================================================
# (6) Unknown definition / version / run are 404 without leaking internals.
# ============================================================================


async def test_unknown_resource_is_404_without_internal_leak(
    infra_container: AppContainer,
) -> None:
    definition_id = await _saved_definition(infra_container, owner="alice")
    client = _client_for(infra_container, identity="alice")

    unknown_definition = _runs(client, "def_" + "c" * 32)
    unknown_version = _runs(client, definition_id, version=99)
    unknown_run = client.get(
        f"{_PREFIX}/{definition_id}/versions/1/runs/run_" + "d" * 32
    )

    assert unknown_definition.status_code == 404, unknown_definition.text
    assert unknown_version.status_code == 404, unknown_version.text
    assert unknown_run.status_code == 404, unknown_run.text
    assert unknown_definition.json() == {"detail": "definition_not_found"}
    assert unknown_version.json() == {"detail": "definition_not_found"}
    assert unknown_run.json() == {"detail": "definition_run_not_found"}
    for response in (unknown_definition, unknown_version, unknown_run):
        assert set(response.json()) == {"detail"}
        assert "Traceback" not in response.text
        assert definition_id not in response.text
