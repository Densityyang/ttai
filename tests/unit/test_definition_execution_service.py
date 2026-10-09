from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.api_definitions import (
    ExecuteDefinitionRequest,
    register_definition_routes,
)
from src.nl2sql.artifacts.custom_definition_execution_service import (
    CustomDefinitionExecutionService,
)
from src.nl2sql.artifacts.definition_revalidation import (
    ActiveReleaseEvidence,
    AuthorizationEvidence,
    DataSnapshotEvidence,
    DefinitionRevalidationGate,
    InputFreshnessDQ,
)
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.container import AppContainer
from src.nl2sql.contracts import TimeRange
from src.nl2sql.orchestration import governed_calculation_inputs as governed_inputs
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
    DefinitionExecutionContext,
    GovernedMetricInputResolutionError,
    TypedMetricCalculationInputResolver,
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
                metric_key="repair_service_archive_rate_overall_day",
            ),
        ),
        parameters=(ParameterSpec(name="target_percent", value_type="decimal"),),
        unit="percent",
        precision=2,
        rounding="half_up",
    )


class _ControlledResolver:
    def __init__(self, actual: Decimal) -> None:
        self.actual = actual
        self.calls = 0

    async def resolve_inputs(self, **_: object) -> tuple[ResolvedCalculationInput, ...]:
        self.calls += 1
        return (
            ResolvedCalculationInput(
                role="actual",
                metric_key="repair_service_archive_rate_overall_day",
                value=self.actual,
                unit="percent",
                data_as_of=datetime(2026, 9, 20, tzinfo=UTC),
                time_range=TimeRange(
                    start=date(2026, 9, 20), end=date(2026, 9, 20)
                ),
                provenance="published_gold",
                source_id="gold.repair.archive",
                receipt_step_id="fetch_actual",
                fact_id="a" * 64,
            ),
        )


class _ControlledFetcher:
    def __init__(self, actual: Decimal = Decimal("45")) -> None:
        self.actual = actual
        self.calls: list[dict[str, object]] = []

    async def fetch_metric_input(self, **kwargs: object) -> ResolvedCalculationInput:
        self.calls.append(dict(kwargs))
        return ResolvedCalculationInput(
            role=str(kwargs["role"]),
            metric_key=str(kwargs["metric_key"]),
            value=self.actual,
            unit="percent",
            data_as_of=datetime(2026, 9, 20, tzinfo=UTC),
            time_range=TimeRange(
                start=date(2026, 9, 20), end=date(2026, 9, 20)
            ),
            provenance="published_gold",
            source_id="gold.repair.archive",
            receipt_step_id="fetch_actual",
            fact_id="a" * 64,
        )


class _RevalidationAuthorization:
    async def current_authorization(self, **_: object) -> AuthorizationEvidence:
        return AuthorizationEvidence(authorization_revision="rev-current")


class _RevalidationRelease:
    async def active_release(self, **_: object) -> ActiveReleaseEvidence:
        return ActiveReleaseEvidence(release_id="rel-current", release_checksum="b" * 64)


class _RevalidationSnapshot:
    async def data_snapshot(self, **_: object) -> DataSnapshotEvidence:
        return DataSnapshotEvidence(
            snapshot_id="snap-current", snapshot_checksum="c" * 64
        )


class _RevalidationBudget:
    async def remaining_budget(self, **_: object) -> int:
        return 5


class _RevalidationFreshness:
    async def assess(self, **kwargs: object) -> tuple[InputFreshnessDQ, ...]:
        resolved = kwargs["resolved_inputs"]
        assert isinstance(resolved, tuple)
        return tuple(
            InputFreshnessDQ(
                role=item.role,
                metric_key=item.metric_key,
                freshness="fresh",
                dq="pass",
                data_as_of=item.data_as_of,
            )
            for item in resolved
        )


def _passing_revalidation() -> DefinitionRevalidationGate:
    """Current-state evidence that a controlled test can legitimately prove."""

    return DefinitionRevalidationGate(
        authorization_provider=_RevalidationAuthorization(),
        active_release_provider=_RevalidationRelease(),
        data_snapshot_provider=_RevalidationSnapshot(),
        freshness_dq_provider=_RevalidationFreshness(),
        budget_provider=_RevalidationBudget(),
        governed_metric_authority=lambda _key: True,
    )


class _Container:
    def __init__(self, resolver: _ControlledResolver | None) -> None:
        self.definitions = CustomDefinitionService(
            governed_metric_keys={"repair_service_archive_rate_overall_day"}
        )
        self.execution = CustomDefinitionExecutionService(
            definitions=self.definitions,
            input_resolver=resolver,
            revalidation=_passing_revalidation(),
        )

    def custom_definition_service(self) -> CustomDefinitionService:
        return self.definitions

    def custom_definition_execution_service(self) -> CustomDefinitionExecutionService:
        return self.execution


async def _client(
    resolver: _ControlledResolver | None,
) -> tuple[TestClient, _Container, str, CalculationSpec]:
    app = FastAPI()
    container = _Container(resolver)
    app.state.container = container
    register_definition_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    spec = _spec()
    version = await container.definitions.create_draft(
        owner_user_id="alice", title="Actual target index", calculation=spec
    )
    await container.definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=version.definition_id
    )
    await container.definitions.confirm(
        owner_user_id="alice", definition_id=version.definition_id
    )
    await container.definitions.save(
        owner_user_id="alice", definition_id=version.definition_id
    )
    return TestClient(app), container, version.definition_id, spec


def _binding(spec: CalculationSpec, target: int) -> dict[str, object]:
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="target_percent", value=target),),
    )
    return binding.model_dump(mode="json")


def test_definition_execution_context_is_strict_and_typed() -> None:
    with pytest.raises(Exception):
        DefinitionExecutionContext.model_validate(
            {"date_mode": "latest_authoritative", "exact_date": "2026-09-20"}
        )
    with pytest.raises(Exception):
        DefinitionExecutionContext.model_validate({"date_mode": "exact_date"})
    with pytest.raises(Exception):
        DefinitionExecutionContext.model_validate(
            {"date_mode": "latest_authoritative", "organization_scope": "all"}
        )

    request = ExecuteDefinitionRequest.model_validate(
        {
            "binding": {
                "calculation_id": "custom.actual_to_target_index",
                "spec_checksum": "a" * 64,
            },
            "execution_context": {
                "date_mode": "exact_date",
                "exact_date": "2026-09-20",
            },
        }
    )
    assert request.execution_context.date_mode == "exact_date"
    assert request.execution_context.exact_date == date(2026, 9, 20)


async def test_controlled_http_execution_uses_server_resolved_input_only() -> None:
    resolver = _ControlledResolver(Decimal("45"))
    client, _, definition_id, spec = await _client(resolver)
    path = f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute"

    first = client.post(path, json={"binding": _binding(spec, 90)})
    second = client.post(path, json={"binding": _binding(spec, 75)})

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "executed"
    assert Decimal(first.json()["value"]) == Decimal("50.00")
    assert Decimal(second.json()["value"]) == Decimal("60.00")
    assert first.json()["version"] == second.json()["version"] == 1
    assert first.json()["spec_checksum"] == second.json()["spec_checksum"]
    assert first.json()["binding_checksum"] != second.json()["binding_checksum"]
    assert first.json()["input_provenance"][0]["metric_key"] == (
        "repair_service_archive_rate_overall_day"
    )
    assert resolver.calls == 2

    injected = client.post(
        path,
        json={"binding": _binding(spec, 90), "actual": 999},
    )
    assert injected.status_code == 422


async def test_zero_target_is_governed_error_not_no_data() -> None:
    client, _, definition_id, spec = await _client(_ControlledResolver(Decimal("45")))
    response = client.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute",
        json={"binding": _binding(spec, 0)},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "calculation_undefined_division_by_zero"
    assert response.json()["detail"] != "NO_DATA"


async def test_unconfigured_resolver_returns_honest_503() -> None:
    client, _, definition_id, spec = await _client(None)
    response = client.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute",
        json={"binding": _binding(spec, 90)},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "calculation_input_resolver_unavailable"


async def test_app_container_injected_fetcher_drives_controlled_http_vertical() -> None:
    fetcher = _ControlledFetcher()
    container = AppContainer(governed_metric_input_fetcher=fetcher)
    # The stock container's revalidation wiring is deferred; this test proves the
    # controlled vertical through a service with explicit current-state evidence.
    container._custom_definition_execution_service = CustomDefinitionExecutionService(
        definitions=container.custom_definition_service(),
        input_resolver=container.calculation_input_resolver(),
        revalidation=_passing_revalidation(),
    )
    definitions = container.custom_definition_service()
    spec = _spec()
    version = await definitions.create_draft(
        owner_user_id="alice", title="Actual target index", calculation=spec
    )
    await definitions.mark_semantic_closed(
        owner_user_id="alice", definition_id=version.definition_id
    )
    await definitions.confirm(owner_user_id="alice", definition_id=version.definition_id)
    await definitions.save(owner_user_id="alice", definition_id=version.definition_id)

    app = FastAPI()
    app.state.container = container
    register_definition_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    path = (
        f"/api/v2/nl2sql/definitions/{version.definition_id}/versions/1/execute"
    )
    with TestClient(app) as client:
        response = client.post(path, json={"binding": _binding(spec, 90)})

    assert response.status_code == 200, response.text
    assert Decimal(response.json()["value"]) == Decimal("50.00")
    assert fetcher.calls == [
        {
            "owner_user_id": "alice",
            "role": "actual",
            "metric_key": "repair_service_archive_rate_overall_day",
            "required_provenance": "published_gold",
            "execution_context": DefinitionExecutionContext(),
        }
    ]
    assert container.calculation_input_resolver() is container.calculation_input_resolver()
    assert (
        container.custom_definition_execution_service()
        is container.custom_definition_execution_service()
    )


async def test_app_container_definition_closure_uses_injected_applicable_authority() -> None:
    def local_real_authority(metric_key: str) -> bool:
        return metric_key == "repair_service_archive_rate_overall_day"

    container = AppContainer(governed_metric_key_resolver=local_real_authority)
    definitions = container.custom_definition_service()
    accepted = await definitions.create_draft(
        owner_user_id="local-real-demo", title="Rate", calculation=_spec()
    )
    await definitions.mark_semantic_closed(
        owner_user_id="local-real-demo", definition_id=accepted.definition_id
    )

    rejected_spec = _spec().model_copy(
        update={
            "inputs": (
                CalculationInputSpec(
                    role="actual",
                    provenance="published_gold",
                    metric_key="demo.revenue",
                ),
            )
        }
    )
    rejected = await definitions.create_draft(
        owner_user_id="local-real-demo", title="Synthetic", calculation=rejected_spec
    )
    with pytest.raises(ValueError, match="existing eligible published_gold metric"):
        await definitions.mark_semantic_closed(
            owner_user_id="local-real-demo", definition_id=rejected.definition_id
        )


class _MissingEvidenceFetcher(_ControlledFetcher):
    async def fetch_metric_input(self, **kwargs: object) -> ResolvedCalculationInput:
        candidate = await super().fetch_metric_input(**kwargs)
        return candidate.model_copy(update={"data_as_of": None})


@pytest.mark.asyncio
async def test_generic_resolver_fails_closed_on_incomplete_evidence() -> None:
    definitions = CustomDefinitionService(
        governed_metric_keys={"repair_service_archive_rate_overall_day"}
    )
    spec = _spec()
    version = await definitions.create_draft(
        owner_user_id="alice", title="Actual target index", calculation=spec
    )
    binding = CalculationExecutionBinding.model_validate(_binding(spec, 90))
    resolver = TypedMetricCalculationInputResolver(_MissingEvidenceFetcher())

    with pytest.raises(
        GovernedMetricInputResolutionError,
        match="calculation_input_data_as_of_missing",
    ):
        await resolver.resolve_inputs(
            owner_user_id="alice",
            definition=version,
            binding=binding,
        )


def test_generic_resolver_dependency_direction_has_no_live_or_runtime_crossover() -> None:
    source = inspect.getsource(governed_inputs)
    assert "src.nl2sql.local_real" not in source
    assert "QueryGateway" not in source
    assert "evaluate_calculation" not in source
