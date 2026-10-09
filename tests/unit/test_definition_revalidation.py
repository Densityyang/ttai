"""Execution-level tests for SAVED rerun current revalidation (A7 / 5.2.2).

Every assertion here drives the real service / route and observes the branch
that actually came out; no source-string or "the edge exists" claim is used as
evidence of reachability.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.api_definitions import register_definition_routes
from src.nl2sql.artifacts.custom_definition_execution_service import (
    CustomDefinitionExecutionRefused,
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
from src.nl2sql.contracts import TimeRange
from src.nl2sql.orchestration.custom_calculation_execution import (
    ResolvedCalculationInput,
)
from src.nl2sql.orchestration.governed_calculation_inputs import (
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

_METRIC = "repair_service_archive_rate_overall_day"


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


def _binding(spec: CalculationSpec, target: int) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(ParameterBinding(name="target_percent", value=target),),
    )


class _Fetcher:
    """A counting governed fetcher: the call count is the reachability evidence."""

    def __init__(
        self, actual: Decimal = Decimal("45"), data_as_of: datetime | None = None
    ) -> None:
        self.actual = actual
        self.data_as_of = data_as_of or datetime(2026, 9, 20, tzinfo=UTC)
        self.calls = 0

    async def fetch_metric_input(self, **kwargs: object) -> ResolvedCalculationInput:
        self.calls += 1
        return ResolvedCalculationInput(
            role=str(kwargs["role"]),
            metric_key=str(kwargs["metric_key"]),
            value=self.actual,
            unit="percent",
            data_as_of=self.data_as_of,
            time_range=TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20)),
            provenance="published_gold",
            source_id="gold.repair.archive",
            receipt_step_id="fetch_actual",
            fact_id="a" * 64,
        )


class _Authorization:
    def __init__(self, *, permitted: bool = True, enabled: bool = True) -> None:
        self.permitted = permitted
        self.enabled = enabled

    async def current_authorization(self, **_: object) -> AuthorizationEvidence:
        return AuthorizationEvidence(
            authorization_revision="auth-rev-current",
            agent_enabled=self.enabled,
            permitted=self.permitted,
        )


class _Release:
    async def active_release(self, **_: object) -> ActiveReleaseEvidence:
        return ActiveReleaseEvidence(
            release_id="release-current", release_checksum="b" * 64
        )


class _Snapshot:
    async def data_snapshot(self, **_: object) -> DataSnapshotEvidence:
        return DataSnapshotEvidence(
            snapshot_id="snapshot-current", snapshot_checksum="c" * 64
        )


class _Budget:
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    async def remaining_budget(self, **_: object) -> int:
        return self.remaining


class _Freshness:
    def __init__(self, *, freshness: str = "fresh", dq: str = "pass") -> None:
        self.freshness = freshness
        self.dq = dq

    async def assess(self, **kwargs: object) -> tuple[InputFreshnessDQ, ...]:
        resolved = kwargs["resolved_inputs"]
        assert isinstance(resolved, tuple)
        return tuple(
            InputFreshnessDQ(
                role=item.role,
                metric_key=item.metric_key,
                freshness=self.freshness,  # type: ignore[arg-type]
                dq=self.dq,  # type: ignore[arg-type]
                data_as_of=item.data_as_of,
            )
            for item in resolved
        )


class _Risk:
    def __init__(self, decision: str | None = None) -> None:
        self.decision = decision

    async def required_decision(self, **_: object) -> str | None:
        return self.decision


def _gate(
    *,
    authority: Any = None,
    budget: Any = None,
    freshness: Any = None,
    authorization: Any = None,
    risk: Any = None,
) -> DefinitionRevalidationGate:
    """A fully wired gate; pass None explicitly to prove the fail-closed default."""

    return DefinitionRevalidationGate(
        authorization_provider=authorization
        if authorization is not None
        else _Authorization(),
        active_release_provider=_Release(),
        data_snapshot_provider=_Snapshot(),
        freshness_dq_provider=freshness if freshness is not None else _Freshness(),
        budget_provider=budget if budget is not None else _Budget(5),
        governed_metric_authority=authority if authority is not None else (lambda _k: True),
        risk_decision_provider=risk,
    )


class _Rig:
    def __init__(
        self,
        *,
        gate: DefinitionRevalidationGate | None = None,
        fetcher: _Fetcher | None = None,
    ) -> None:
        self.definitions = CustomDefinitionService(governed_metric_keys={_METRIC})
        self.fetcher = fetcher if fetcher is not None else _Fetcher()
        self.service = CustomDefinitionExecutionService(
            definitions=self.definitions,
            input_resolver=TypedMetricCalculationInputResolver(self.fetcher),
            revalidation=gate,
        )
        self.spec = _spec()
        self.definition_id = ""

    async def saved(self) -> str:
        draft = await self.definitions.create_draft(
            owner_user_id="alice", title="Actual to target", calculation=self.spec
        )
        await self.definitions.mark_semantic_closed(
            owner_user_id="alice", definition_id=draft.definition_id
        )
        await self.definitions.confirm(
            owner_user_id="alice", definition_id=draft.definition_id
        )
        await self.definitions.save(
            owner_user_id="alice", definition_id=draft.definition_id
        )
        self.definition_id = draft.definition_id
        return self.definition_id

    async def confirmed_but_not_saved(self) -> str:
        draft = await self.definitions.create_draft(
            owner_user_id="alice", title="Actual to target", calculation=self.spec
        )
        await self.definitions.mark_semantic_closed(
            owner_user_id="alice", definition_id=draft.definition_id
        )
        await self.definitions.confirm(
            owner_user_id="alice", definition_id=draft.definition_id
        )
        self.definition_id = draft.definition_id
        return self.definition_id

    async def draft(self) -> str:
        draft = await self.definitions.create_draft(
            owner_user_id="alice", title="Actual to target", calculation=self.spec
        )
        self.definition_id = draft.definition_id
        return self.definition_id

    async def execute(self, target: int = 90) -> Any:
        return await self.service.execute_revalidated(
            owner_user_id="alice",
            definition_id=self.definition_id,
            version=1,
            binding=_binding(self.spec, target),
        )


class _Container:
    def __init__(self, definitions: CustomDefinitionService, service: Any) -> None:
        self._definitions = definitions
        self._service = service

    def custom_definition_service(self) -> CustomDefinitionService:
        return self._definitions

    def custom_definition_execution_service(self) -> Any:
        return self._service


def _http_client(rig: _Rig) -> TestClient:
    app = FastAPI()
    app.state.container = _Container(rig.definitions, rig.service)
    register_definition_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app)


def _post_execute(client: TestClient, definition_id: str, target: int = 90) -> Any:
    return client.post(
        f"/api/v2/nl2sql/definitions/{definition_id}/versions/1/execute",
        json={"binding": _binding(_spec(), target).model_dump(mode="json")},
    )


# --- (1) retired governed metric key -> DENY, never 200 -----------------------


async def test_retired_governed_metric_key_is_denied_and_never_fetched() -> None:
    rig = _Rig(gate=_gate(authority=lambda _key: True))
    await rig.saved()
    live = await rig.execute(90)
    assert not isinstance(live, CustomDefinitionExecutionRefused)
    assert rig.fetcher.calls == 1

    # The governed authority now RETIRES the metric key.  The saved definition
    # may not silently keep computing from it.
    rig.service = CustomDefinitionExecutionService(
        definitions=rig.definitions,
        input_resolver=TypedMetricCalculationInputResolver(rig.fetcher),
        revalidation=_gate(authority=lambda _key: False),
    )
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "DENY"
    assert refused.revalidation.reasons == ("governed_metric_retired",)
    # DENY is proven BEFORE any governed fetch.
    assert rig.fetcher.calls == 1


async def test_retired_metric_key_is_a_hard_403_not_a_generic_409() -> None:
    rig = _Rig(gate=_gate(authority=lambda _key: False))
    definition_id = await rig.saved()
    response = _post_execute(_http_client(rig), definition_id)
    assert response.status_code == 403, response.text
    body = response.json()["detail"]
    assert body["code"] == "definition_revalidation_denied"
    assert body["reasons"] == ["governed_metric_retired"]
    assert rig.fetcher.calls == 0


# --- (2) stale / DQ-unknown -> never EXECUTE ---------------------------------


async def test_stale_freshness_is_a_clarification_not_an_execution() -> None:
    rig = _Rig(gate=_gate(freshness=_Freshness(freshness="stale", dq="pass")))
    definition_id = await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "CLARIFICATION"
    assert refused.revalidation.reasons == ("freshness_stale",)
    # The governed fetch DID happen: the judgement is over the real evidence.
    assert rig.fetcher.calls == 1

    response = _post_execute(_http_client(rig), definition_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "clarification_required"
    assert response.json()["reasons"] == ["freshness_stale"]


async def test_unknown_freshness_and_unknown_dq_are_unavailable() -> None:
    unknown_freshness = _Rig(gate=_gate(freshness=_Freshness(freshness="unknown")))
    await unknown_freshness.saved()
    refused = await unknown_freshness.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "UNAVAILABLE"
    assert refused.revalidation.reasons == ("freshness_unknown",)

    unknown_dq = _Rig(gate=_gate(freshness=_Freshness(freshness="fresh", dq="unknown")))
    await unknown_dq.saved()
    refused_dq = await unknown_dq.execute(90)
    assert isinstance(refused_dq, CustomDefinitionExecutionRefused)
    assert refused_dq.revalidation.branch == "UNAVAILABLE"
    assert refused_dq.revalidation.reasons == ("dq_unknown",)


async def test_missing_freshness_provider_fails_closed() -> None:
    gate = DefinitionRevalidationGate(
        authorization_provider=_Authorization(),
        active_release_provider=_Release(),
        data_snapshot_provider=_Snapshot(),
        budget_provider=_Budget(5),
        governed_metric_authority=lambda _key: True,
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "UNAVAILABLE"
    assert refused.revalidation.reasons == ("freshness_evidence_unavailable",)


# --- (3) exhausted budget -> UNAVAILABLE with ZERO fetcher calls --------------


async def test_exhausted_budget_is_unavailable_and_never_fetches() -> None:
    rig = _Rig(gate=_gate(budget=_Budget(0)))
    definition_id = await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "UNAVAILABLE"
    assert refused.revalidation.reasons == ("budget_exhausted",)
    assert rig.fetcher.calls == 0

    response = _post_execute(_http_client(rig), definition_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "result_unavailable"
    assert response.json()["reasons"] == ["budget_exhausted"]
    assert rig.fetcher.calls == 0


async def test_missing_budget_provider_is_unavailable_and_never_fetches() -> None:
    gate = DefinitionRevalidationGate(
        authorization_provider=_Authorization(),
        active_release_provider=_Release(),
        data_snapshot_provider=_Snapshot(),
        freshness_dq_provider=_Freshness(),
        governed_metric_authority=lambda _key: True,
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.reasons == ("budget_evidence_unavailable",)
    assert rig.fetcher.calls == 0


# --- (4) DRAFT / not-SAVED -> refused ----------------------------------------


async def test_draft_version_is_denied() -> None:
    rig = _Rig(gate=_gate())
    definition_id = await rig.draft()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "DENY"
    assert refused.revalidation.reasons == ("definition_version_not_confirmed",)
    assert rig.fetcher.calls == 0

    response = _post_execute(_http_client(rig), definition_id)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "definition_revalidation_denied"


async def test_confirmed_but_unsaved_version_is_denied() -> None:
    rig = _Rig(gate=_gate())
    await rig.confirmed_but_not_saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "DENY"
    assert refused.revalidation.reasons == ("definition_version_not_saved",)


# --- (5) per-run records are queryable ---------------------------------------


async def test_every_rerun_records_binding_plan_validation_and_receipt() -> None:
    rig = _Rig(gate=_gate())
    definition_id = await rig.saved()
    first = await rig.execute(90)
    second = await rig.execute(75)
    assert not isinstance(first, CustomDefinitionExecutionRefused)
    assert not isinstance(second, CustomDefinitionExecutionRefused)

    records = await rig.service.run_records.list_for_version(
        definition_id=definition_id, version=1
    )
    assert len(records) == 2
    for record in records:
        assert record.definition_id == definition_id
        assert record.version == 1
        assert record.definition_checksum == first.definition_checksum
        assert record.binding.version == 1
        assert record.plan.calculation_id == "custom.actual_to_target_index"
        assert record.plan.spec_checksum == rig.spec.checksum
        assert record.plan.parameter_names == ("target_percent",)
        assert record.plan.declared_input_roles == ("actual",)
        assert record.validation.branch == "EXECUTE"
        assert record.validation.before_resolution.branch == "EXECUTE"
        assert record.validation.resolved_inputs is not None
        assert record.validation.resolved_inputs.branch == "EXECUTE"
        assert record.receipt is not None
        assert record.receipt.result.input_provenance[0].metric_key == _METRIC

    # Each record carries ITS OWN binding, and the two runs differ.
    assert records[0].binding.binding.checksum != records[1].binding.binding.checksum
    assert records[0].receipt is not None and records[1].receipt is not None
    assert records[0].receipt.result.value == Decimal("50.00")
    assert records[1].receipt.result.value == Decimal("60.00")

    fetched = await rig.service.run_records.get(
        definition_id=definition_id, version=1, run_id=records[0].run_id
    )
    assert fetched is not None
    assert fetched.run_id == records[0].run_id


async def test_refused_rerun_is_also_recorded_without_a_receipt() -> None:
    rig = _Rig(gate=_gate(budget=_Budget(0)))
    definition_id = await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    record = await rig.service.run_records.get(
        definition_id=definition_id, version=1, run_id=refused.run_id
    )
    assert record is not None
    assert record.receipt is None
    assert record.validation.branch == "UNAVAILABLE"
    assert record.validation.reasons == ("budget_exhausted",)


# --- (6) all five branches are typed and distinguishable on the wire ----------


async def test_all_five_branches_are_reachable_and_distinguishable() -> None:
    execute = _Rig(gate=_gate())
    await execute.saved()
    outcome = await execute.execute(90)
    assert not isinstance(outcome, CustomDefinitionExecutionRefused)

    clarification = _Rig(gate=_gate(freshness=_Freshness(freshness="stale")))
    await clarification.saved()
    clarification_outcome = await clarification.execute(90)
    assert isinstance(clarification_outcome, CustomDefinitionExecutionRefused)
    assert clarification_outcome.revalidation.branch == "CLARIFICATION"

    decision = _Rig(gate=_gate(risk=_Risk("HIGH_RISK_EXECUTION")))
    await decision.saved()
    decision_outcome = await decision.execute(90)
    assert isinstance(decision_outcome, CustomDefinitionExecutionRefused)
    assert decision_outcome.revalidation.branch == "BUSINESS_RISK_DECISION"
    assert decision_outcome.revalidation.required_decision == "HIGH_RISK_EXECUTION"

    deny = _Rig(gate=_gate(authority=lambda _key: False))
    await deny.saved()
    deny_outcome = await deny.execute(90)
    assert isinstance(deny_outcome, CustomDefinitionExecutionRefused)
    assert deny_outcome.revalidation.branch == "DENY"

    unavailable = _Rig(gate=_gate(budget=_Budget(0)))
    await unavailable.saved()
    unavailable_outcome = await unavailable.execute(90)
    assert isinstance(unavailable_outcome, CustomDefinitionExecutionRefused)
    assert unavailable_outcome.revalidation.branch == "UNAVAILABLE"


async def _drive(rig: _Rig) -> Any:
    definition_id = await rig.saved()
    return _post_execute(_http_client(rig), definition_id)


async def test_wire_distinguishes_every_branch() -> None:
    executed = await _drive(_Rig(gate=_gate()))
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "executed"

    clarified = await _drive(_Rig(gate=_gate(freshness=_Freshness(freshness="stale"))))
    assert clarified.status_code == 200, clarified.text
    assert clarified.json()["status"] == "clarification_required"

    decided = await _drive(_Rig(gate=_gate(risk=_Risk("HIGH_RISK_EXECUTION"))))
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "decision_required"
    assert decided.json()["required_decision"] == "HIGH_RISK_EXECUTION"

    denied = await _drive(_Rig(gate=_gate(authority=lambda _key: False)))
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["code"] == "definition_revalidation_denied"

    unavailable = await _drive(_Rig(gate=_gate(budget=_Budget(0))))
    assert unavailable.status_code == 200, unavailable.text
    assert unavailable.json()["status"] == "result_unavailable"


async def test_denied_authorization_is_deny_not_a_confirmation() -> None:
    rig = _Rig(gate=_gate(authorization=_Authorization(permitted=False)))
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "DENY"
    assert refused.revalidation.reasons == ("authorization_denied",)
    assert rig.fetcher.calls == 0


async def test_default_gate_is_fail_closed_never_a_silent_pass() -> None:
    # No revalidation injected: the service's OWN default gate must refuse.
    rig = _Rig(gate=None)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "UNAVAILABLE"
    assert refused.revalidation.reasons == ("authorization_evidence_unavailable",)
    assert rig.fetcher.calls == 0


# --- (7) A5: parameter rebinding creates no version and no new confirmation ---


async def test_parameter_rebinding_creates_no_version_and_no_confirmation() -> None:
    rig = _Rig(gate=_gate())
    definition_id = await rig.saved()
    before = await rig.definitions.get_exact_version(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    before_lifecycle = await rig.definitions.get_version_lifecycle(
        owner_user_id="alice", definition_id=definition_id, version=1
    )

    first = await rig.execute(90)
    second = await rig.execute(75)
    assert not isinstance(first, CustomDefinitionExecutionRefused)
    assert not isinstance(second, CustomDefinitionExecutionRefused)

    after = await rig.definitions.get_exact_version(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    after_lifecycle = await rig.definitions.get_version_lifecycle(
        owner_user_id="alice", definition_id=definition_id, version=1
    )
    assert after.version == before.version == 1
    assert after.checksum == before.checksum
    assert after_lifecycle == before_lifecycle
    assert len(await rig.definitions.list_owned(owner_user_id="alice")) == 1
    assert len(
        await rig.definitions.list_saved_versions(
            owner_user_id="alice", definition_id=definition_id
        )
    ) == 1
    # Different concrete VALUES, same immutable semantics.
    assert first.result.binding_checksum != second.result.binding_checksum
    assert first.definition_checksum == second.definition_checksum
