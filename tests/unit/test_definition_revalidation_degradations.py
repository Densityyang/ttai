"""Execution-level tests for MODE-SCOPED revalidation strictness (plan C).

A deployment that has not wired the section 8.19 CURRENT-authority providers must
never behave the same in both modes and must never pass SILENTLY:

* product mode     -> strict fail-closed: an UNCONFIGURED provider is UNAVAILABLE;
* infra-dev / demo -> the check is SKIPPED and NAMED in `degradations`.

A provider that EXISTS but yields no evidence is UNAVAILABLE in BOTH modes:
"not configured" and "cannot obtain evidence" are different states.  DENY and
CLARIFICATION are decided ONLY from evidence that actually exists, so skipping an
unconfigured provider can never turn a refusal into an EXECUTE.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.api_definitions import (
    DefinitionVersionView,
    DefinitionView,
    ExecutedDefinitionResponse,
    ExecuteDefinitionResponse,
    _definition_view,
    register_definition_routes,
)
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

# The five section 8.19 external authority checks a stock deployment does not wire.
_UNCONFIGURED_CHECKS = frozenset(
    {
        "authorization_evidence_unconfigured",
        "active_release_unconfigured",
        "data_snapshot_unconfigured",
        "freshness_evidence_unconfigured",
        "budget_evidence_unconfigured",
    }
)

# Every degradation must SAY it was unconfigured; none may read as "passed".
_FALSE_PASS_MARKERS = ("pass", "ok", "verified", "available", "present", "success")


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


# --- providers that RETURN evidence (configured and usable) ------------------


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


# --- providers that EXIST but return NO evidence -----------------------------


class _NoAuthorization:
    async def current_authorization(self, **_: object) -> None:
        return None


class _NoRelease:
    async def active_release(self, **_: object) -> None:
        return None


class _NoSnapshot:
    async def data_snapshot(self, **_: object) -> None:
        return None


class _NoBudget:
    async def remaining_budget(self, **_: object) -> None:
        return None


class _Rig:
    def __init__(self, *, gate: DefinitionRevalidationGate) -> None:
        self.definitions = CustomDefinitionService(governed_metric_keys={_METRIC})
        self.fetcher = _Fetcher()
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


# ============================================================================
# (1) STRICT: an unconfigured provider is UNAVAILABLE and NOTHING is fetched.
# ============================================================================


async def test_strict_missing_providers_are_unavailable_and_never_fetch() -> None:
    # strict is the DEFAULT: a bare construction must fail closed.
    gate = DefinitionRevalidationGate(governed_metric_authority=lambda _key: True)
    rig = _Rig(gate=gate)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "UNAVAILABLE"
    assert refused.revalidation.reasons == ("authorization_evidence_unavailable",)
    # Nothing was skipped: strict mode never produces a degradation.
    assert refused.revalidation.degradations == ()
    # No governed fetch was reached.
    assert rig.fetcher.calls == 0


async def test_explicit_strict_equals_default_and_refuses_each_check() -> None:
    cases: list[tuple[dict[str, Any], str | None]] = [
        ({"authorization_provider": _NoAuthorization()}, None),
        ({"authorization_provider": _Authorization()}, "active_release_unavailable"),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _Release(),
            },
            "data_snapshot_unavailable",
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _Release(),
                "data_snapshot_provider": _Snapshot(),
            },
            "budget_evidence_unavailable",
        ),
    ]
    for kwargs, reason in cases:
        gate = DefinitionRevalidationGate(
            governed_metric_authority=lambda _key: True, strict=True, **kwargs
        )
        rig = _Rig(gate=gate)
        await rig.saved()
        refused = await rig.execute(90)
        assert isinstance(refused, CustomDefinitionExecutionRefused)
        assert refused.revalidation.branch == "UNAVAILABLE"
        assert refused.revalidation.degradations == ()
        if reason is not None:
            assert refused.revalidation.reasons == (reason,)
        assert rig.fetcher.calls == 0


async def test_strict_missing_freshness_is_unavailable_after_the_fetch() -> None:
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
    assert refused.revalidation.degradations == ()
    assert rig.fetcher.calls == 1


# ============================================================================
# (2) NON-STRICT: an unconfigured provider is SKIPPED, NAMED and the run EXECUTEs.
# ============================================================================


async def test_non_strict_missing_providers_execute_with_exact_degradations() -> None:
    gate = DefinitionRevalidationGate(
        governed_metric_authority=lambda _key: True, strict=False
    )
    rig = _Rig(gate=gate)
    definition_id = await rig.saved()
    outcome = await rig.execute(90)
    assert not isinstance(outcome, CustomDefinitionExecutionRefused)
    assert outcome.result.value == Decimal("50.00")
    # EXACTLY the five skipped checks, nothing more and nothing less.
    assert len(outcome.degradations) == len(_UNCONFIGURED_CHECKS)
    assert set(outcome.degradations) == set(_UNCONFIGURED_CHECKS)
    # No degradation may read as a PASS.
    for code in outcome.degradations:
        assert code.endswith("_unconfigured"), code
        assert not any(marker in code for marker in _FALSE_PASS_MARKERS), code
    # The governed metric authority is NEVER skippable and was actually used.
    assert "governed_metric_authority_unconfigured" not in outcome.degradations

    # The audit record is the authoritative landing point for the same codes.
    records = await rig.service.run_records.list_for_version(
        definition_id=definition_id, version=1
    )
    assert len(records) == 1
    assert set(records[0].degradations) == set(_UNCONFIGURED_CHECKS)
    assert records[0].validation.branch == "EXECUTE"
    assert records[0].receipt is not None


async def test_non_strict_partial_wiring_names_only_the_missing_checks() -> None:
    # authorization + active release + data snapshot are wired; freshness and
    # budget are not.  Exactly those two must be named.
    gate = DefinitionRevalidationGate(
        authorization_provider=_Authorization(),
        active_release_provider=_Release(),
        data_snapshot_provider=_Snapshot(),
        governed_metric_authority=lambda _key: True,
        strict=False,
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    outcome = await rig.execute(90)
    assert not isinstance(outcome, CustomDefinitionExecutionRefused)
    assert set(outcome.degradations) == {
        "freshness_evidence_unconfigured",
        "budget_evidence_unconfigured",
    }


async def test_non_strict_governed_metric_authority_is_still_required() -> None:
    # No authority bound: even a non-strict gate must refuse; this check is not
    # part of the skippable section 8.19 set.
    gate = DefinitionRevalidationGate(strict=False)
    rig = _Rig(gate=gate)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "UNAVAILABLE"
    assert refused.revalidation.reasons == ("governed_metric_authority_unavailable",)
    assert rig.fetcher.calls == 0


# ============================================================================
# (3) BOTH modes fail closed when a provider EXISTS but returns no evidence.
# ============================================================================


def _present_but_empty_cases() -> list[tuple[dict[str, Any], str, bool]]:
    return [
        (
            {"authorization_provider": _NoAuthorization()},
            "authorization_evidence_unavailable",
            False,
        ),
        (
            {"authorization_provider": _NoAuthorization()},
            "authorization_evidence_unavailable",
            True,
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _NoRelease(),
            },
            "active_release_unavailable",
            False,
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _NoRelease(),
            },
            "active_release_unavailable",
            True,
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _Release(),
                "data_snapshot_provider": _NoSnapshot(),
            },
            "data_snapshot_unavailable",
            False,
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _Release(),
                "data_snapshot_provider": _NoSnapshot(),
            },
            "data_snapshot_unavailable",
            True,
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _Release(),
                "data_snapshot_provider": _Snapshot(),
                "budget_provider": _NoBudget(),
            },
            "budget_evidence_unavailable",
            False,
        ),
        (
            {
                "authorization_provider": _Authorization(),
                "active_release_provider": _Release(),
                "data_snapshot_provider": _Snapshot(),
                "budget_provider": _NoBudget(),
            },
            "budget_evidence_unavailable",
            True,
        ),
    ]


async def test_present_but_empty_provider_is_unavailable_in_both_modes() -> None:
    for kwargs, reason, strict in _present_but_empty_cases():
        gate = DefinitionRevalidationGate(
            governed_metric_authority=lambda _key: True, strict=strict, **kwargs
        )
        rig = _Rig(gate=gate)
        await rig.saved()
        refused = await rig.execute(90)
        assert isinstance(refused, CustomDefinitionExecutionRefused), (strict, reason)
        assert refused.revalidation.branch == "UNAVAILABLE", (strict, reason)
        assert refused.revalidation.reasons == (reason,), (strict, reason)
        # Nothing was SKIPPED: the provider existed, so no degradation may claim
        # the check was unconfigured.
        assert refused.revalidation.degradations == (), (strict, reason)
        assert rig.fetcher.calls == 0, (strict, reason)


# ============================================================================
# (4) DENY is decided from evidence only and is NEVER relaxed by skipping.
# ============================================================================


async def test_non_strict_skipped_providers_do_not_turn_deny_into_execute() -> None:
    # authorization / release / snapshot providers are ABSENT (skipped), yet the
    # governed metric is retired: the DENY must survive.
    gate = DefinitionRevalidationGate(
        governed_metric_authority=lambda _key: False, strict=False
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "DENY"
    assert refused.revalidation.reasons == ("governed_metric_retired",)
    # The skips are still recorded, but they did not upgrade the branch.
    assert set(refused.revalidation.degradations) == {
        "authorization_evidence_unconfigured",
        "active_release_unconfigured",
        "data_snapshot_unconfigured",
    }
    assert rig.fetcher.calls == 0


async def test_non_strict_denied_authorization_is_still_deny() -> None:
    gate = DefinitionRevalidationGate(
        authorization_provider=_Authorization(permitted=False),
        governed_metric_authority=lambda _key: True,
        strict=False,
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    refused = await rig.execute(90)
    assert isinstance(refused, CustomDefinitionExecutionRefused)
    assert refused.revalidation.branch == "DENY"
    assert refused.revalidation.reasons == ("authorization_denied",)
    assert rig.fetcher.calls == 0


# ============================================================================
# (5) The CONTAINER chooses strictness from service_mode.
# ============================================================================


def test_container_gate_strictness_follows_service_mode(monkeypatch: Any) -> None:
    import src.nl2sql.container as container_module
    from src.core.settings import Settings
    from src.nl2sql.container import AppContainer

    def build(settings: Settings) -> tuple[AppContainer, Any]:
        monkeypatch.setattr(container_module, "get_settings", lambda: settings)
        container = AppContainer(governed_metric_key_resolver=lambda _key: True)
        return container, container.custom_definition_execution_service()

    product_settings = Settings(
        _env_file=None,
        service_mode="product",
        auth_enabled=True,
        tt_api_base_url="http://auth.invalid",
        cors_allowed_origins="https://app.example",
        # product mode validates the business role even though this test never
        # opens a connection; a least-privilege PostgreSQL URL satisfies it.
        database_url="postgresql+asyncpg://svc_app:pw@127.0.0.1:5432/app",
    )
    product_container, product_execution = build(product_settings)
    assert product_execution._revalidation._strict is True
    assert (
        product_execution._revalidation._governed_metric_authority
        == product_container.custom_definition_service()._is_governed_metric_key
    )

    infra_settings = Settings(_env_file=None, service_mode="infra-dev")
    _, infra_execution = build(infra_settings)
    assert infra_execution._revalidation._strict is False


# ============================================================================
# (6) The wire exposes degradations WITHOUT changing the frozen EXECUTE shape.
# ============================================================================


async def test_execute_wire_exposes_degradations_and_freezes_the_base_shape() -> None:
    gate = DefinitionRevalidationGate(
        governed_metric_authority=lambda _key: True, strict=False
    )
    rig = _Rig(gate=gate)
    definition_id = await rig.saved()
    response = _post_execute(_http_client(rig), definition_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "executed"
    assert set(body["degradations"]) == set(_UNCONFIGURED_CHECKS)
    assert body["value"] == "50.00"

    # The base frozen shape is UNCHANGED: the additive field lives on a subclass.
    assert "degradations" not in ExecuteDefinitionResponse.model_fields
    assert "degradations" in ExecutedDefinitionResponse.model_fields
    assert set(ExecuteDefinitionResponse.model_fields) == {
        "status",
        "definition_id",
        "version",
        "definition_checksum",
        "calculation_id",
        "spec_checksum",
        "binding_checksum",
        "value",
        "unit",
        "input_provenance",
        "data_as_of",
        "time_range",
        "calculation_scope",
    }
    assert issubclass(ExecutedDefinitionResponse, ExecuteDefinitionResponse)


async def test_strict_wire_execute_carries_no_degradations() -> None:
    gate = DefinitionRevalidationGate(
        authorization_provider=_Authorization(),
        active_release_provider=_Release(),
        data_snapshot_provider=_Snapshot(),
        freshness_dq_provider=_Freshness(),
        budget_provider=_Budget(5),
        governed_metric_authority=lambda _key: True,
        strict=True,
    )
    rig = _Rig(gate=gate)
    definition_id = await rig.saved()
    response = _post_execute(_http_client(rig), definition_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"
    assert response.json()["degradations"] == []


# ============================================================================
# (7) DefinitionView carries the OBJECT-LEVEL two axes; a version view must not.
# ============================================================================


async def test_definition_view_exposes_object_level_governance_and_authority() -> None:
    rig = _Rig(
        gate=DefinitionRevalidationGate(
            governed_metric_authority=lambda _key: True, strict=False
        )
    )
    definition_id = await rig.saved()
    definition = await rig.definitions.get_owned_definition(
        owner_user_id="alice", definition_id=definition_id
    )
    view = _definition_view(definition)
    assert view.governance == definition.axes.governance
    assert view.authority == definition.axes.authority
    assert view.governance == "NONE"
    assert view.authority == "noncanonical"
    assert "governance" in DefinitionView.model_fields
    assert "authority" in DefinitionView.model_fields

    # The version-level lifecycle view deliberately does NOT own these axes.
    assert "governance" not in DefinitionVersionView.model_fields
    assert "authority" not in DefinitionVersionView.model_fields


# ============================================================================
# (8) A degraded rerun is NEVER silent: it emits one structured warning.
# ============================================================================


async def test_non_strict_degradation_emits_a_structured_warning(
    caplog: Any,
) -> None:
    gate = DefinitionRevalidationGate(
        governed_metric_authority=lambda _key: True, strict=False
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    with caplog.at_level(
        logging.WARNING,
        logger="src.nl2sql.artifacts.custom_definition_execution_service",
    ):
        outcome = await rig.execute(90)
    assert not isinstance(outcome, CustomDefinitionExecutionRefused)
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "definition_revalidation_degraded"
    ]
    assert len(warnings) == 1
    assert set(warnings[0].degradations) == set(_UNCONFIGURED_CHECKS)
    assert warnings[0].definition_id == rig.definition_id
    assert warnings[0].version == 1
    assert "unconfigured" in caplog.text


async def test_strict_execute_emits_no_degradation_warning(caplog: Any) -> None:
    gate = DefinitionRevalidationGate(
        authorization_provider=_Authorization(),
        active_release_provider=_Release(),
        data_snapshot_provider=_Snapshot(),
        freshness_dq_provider=_Freshness(),
        budget_provider=_Budget(5),
        governed_metric_authority=lambda _key: True,
        strict=True,
    )
    rig = _Rig(gate=gate)
    await rig.saved()
    with caplog.at_level(
        logging.WARNING,
        logger="src.nl2sql.artifacts.custom_definition_execution_service",
    ):
        outcome = await rig.execute(90)
    assert not isinstance(outcome, CustomDefinitionExecutionRefused)
    assert [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "definition_revalidation_degraded"
    ] == []
