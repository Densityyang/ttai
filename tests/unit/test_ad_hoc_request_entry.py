"""QUERY run-scoped AD_HOC REQUEST ENTRY: accept/reject matrix and boundaries."""

from __future__ import annotations

import inspect
from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.contracts import ContextBundle, QueryPlan, TimeRange
from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.orchestration.ad_hoc_request import (
    AdHocCalculationRequest,
    AdHocRequestError,
    ResolvedAdHocCalculation,
    resolve_ad_hoc_request,
)
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.semantic.calculation_contract import (
    AggregateOperand,
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    derived_output_id,
)

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
DERIVED = "adhoc_" + "a" * 32


def _context(
    *,
    asset_ids: tuple[str, ...] = ("metric.revenue", "metric.stores"),
    resolution_status: str = "resolved",
    unresolved_slots: tuple[str, ...] = (),
    conflict_ids: tuple[str, ...] = (),
) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=asset_ids,
        resolution_status=resolution_status,  # type: ignore[arg-type]
        unresolved_slots=unresolved_slots,
        conflict_ids=conflict_ids,
    )


def _query_plan(**overrides: object) -> QueryPlan:
    base: dict[str, object] = {
        "intent": "metric",
        "domain": "finance",
        "metric_keys": ("metric.revenue", "metric.stores"),
        "time_range": TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        "grain": "month",
        "source_strategy": "aggregate_first",
        "required_permissions": ("metrics:read",),
    }
    base.update(overrides)
    return QueryPlan(**base)


def _spec(**overrides: object) -> CalculationSpec:
    base: dict[str, object] = {
        "calculation_id": "adhoc.revenue_per_store",
        "expression": BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=AggregateOperand(function="count_distinct", role="denominator"),
        ),
        "inputs": (
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="metric.revenue",
            ),
            CalculationInputSpec(
                role="denominator",
                provenance="published_gold",
                metric_key="metric.stores",
            ),
        ),
        "unit": "ratio",
        "precision": 4,
        "rounding": "half_up",
    }
    base.update(overrides)
    return CalculationSpec(**base)


def _binding(spec: CalculationSpec, **overrides: object) -> CalculationExecutionBinding:
    base: dict[str, object] = {
        "calculation_id": spec.calculation_id,
        "spec_checksum": spec.checksum,
        "parameters": (),
    }
    base.update(overrides)
    return CalculationExecutionBinding(**base)


def _request(
    spec: CalculationSpec | None = None,
    binding: CalculationExecutionBinding | None = None,
) -> AdHocCalculationRequest:
    resolved_spec = spec if spec is not None else _spec()
    return AdHocCalculationRequest(
        calculation_spec=resolved_spec,
        execution_binding=binding if binding is not None else _binding(resolved_spec),
    )


def _code(
    request: AdHocCalculationRequest,
    *,
    context: ContextBundle | None = None,
    query_plan: QueryPlan | None = None,
    catalog: ApprovedCalculationCatalog | None = None,
) -> str:
    with pytest.raises(AdHocRequestError) as exc:
        resolve_ad_hoc_request(
            request=request,
            context=context if context is not None else _context(),
            query_plan=query_plan if query_plan is not None else _query_plan(),
            catalog=catalog,
        )
    return exc.value.code


def _catalog_binding(metric_key: str) -> ApprovedCalculationBinding:
    meta = trusted_template_registry.metadata("ratio")
    return ApprovedCalculationBinding(
        canonical_metric_key=metric_key,
        template_id="ratio",
        template_version=meta.version,
        template_checksum=meta.checksum,
        inputs=(
            ApprovedCalculationInput(
                role="numerator",
                metric_key="metric.other",
                metric_contract_sha256="b" * 64,
            ),
        ),
        binding_revision="1",
        semantic_release_id="release-1",
        semantic_release_checksum="c" * 64,
    )


# --- acceptance ---------------------------------------------------------------


def test_a_legal_carrier_resolves_onto_authorized_context_metrics() -> None:
    spec = _spec()
    binding = _binding(spec)
    resolved = resolve_ad_hoc_request(
        request=_request(spec, binding),
        context=_context(),
        query_plan=_query_plan(),
    )
    assert resolved.spec is spec
    assert resolved.binding is binding
    assert resolved.resolved_inputs == (
        ("numerator", "metric.revenue"),
        ("denominator", "metric.stores"),
    )
    assert resolved.input_metrics == ("metric.revenue", "metric.stores")
    assert resolved.derived_output_id == derived_output_id(spec, binding)
    assert resolved.derived_output_id.startswith("adhoc_")
    # Never a canonical metric identity.
    assert resolved.derived_output_id not in resolved.input_metrics


def test_query_request_accepts_the_carrier_and_rejects_unknown_fields() -> None:
    from src.nl2sql.v2 import QueryRequest

    spec = _spec()
    payload = {
        "messages": [{"role": "user", "content": "revenue per store"}],
        "requested_mode": "QUERY",
        "ad_hoc_calculation": {
            "calculation_spec": spec.model_dump(mode="json"),
            "execution_binding": _binding(spec).model_dump(mode="json"),
        },
    }
    parsed = QueryRequest.model_validate(payload)
    assert parsed.ad_hoc_calculation is not None
    assert parsed.ad_hoc_calculation.calculation_spec.calculation_id == spec.calculation_id

    with pytest.raises(ValidationError):
        QueryRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "x"}],
                "ad_hoc": payload["ad_hoc_calculation"],
            }
        )
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "x"}],
                "ad_hoc_calculation": {
                    **payload["ad_hoc_calculation"],
                    "canonical": True,
                },
            }
        )


def test_carrier_rejects_authority_and_lifecycle_fields() -> None:
    spec = _spec()
    for forbidden in ("canonical", "approved", "saved", "definition_id", "lifecycle"):
        with pytest.raises(ValidationError):
            AdHocCalculationRequest.model_validate(
                {
                    "calculation_spec": spec.model_dump(mode="json"),
                    "execution_binding": _binding(spec).model_dump(mode="json"),
                    forbidden: True,
                }
            )
    # The nested shared spec rejects the same authority vocabulary too.
    with pytest.raises(ValidationError):
        CalculationSpec.model_validate(
            {**spec.model_dump(mode="json"), "approved": True}
        )


# --- rejection matrix ---------------------------------------------------------


def test_unresolved_input_is_rejected() -> None:
    spec = _spec(
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="metric.revenue",
            ),
            CalculationInputSpec(
                role="denominator",
                provenance="published_gold",
                metric_key="metric.unknown",
            ),
        )
    )
    assert (
        _code(
            _request(spec),
            context=_context(asset_ids=("metric.revenue",)),
            query_plan=_query_plan(metric_keys=("metric.revenue", "metric.unknown")),
        )
        == "ad_hoc_request_input_unresolved"
    )


def test_catalog_bound_input_is_rejected() -> None:
    catalog = ApprovedCalculationCatalog([_catalog_binding("metric.revenue")])
    assert (
        _code(_request(), catalog=catalog)
        == "ad_hoc_request_input_catalog_bound"
    )


def test_role_mismatch_is_rejected() -> None:
    spec = _spec(
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="metric.revenue",
            ),
            CalculationInputSpec(
                role="denominator",
                provenance="published_gold",
                metric_key="metric.stores",
            ),
            CalculationInputSpec(
                role="unused",
                provenance="published_gold",
                metric_key="metric.other",
            ),
        )
    )
    assert _code(_request(spec)) == "ad_hoc_request_input_role_mismatch"


def test_ambiguous_context_requires_clarification() -> None:
    assert (
        _code(_request(), context=_context(resolution_status="ambiguous"))
        == "ad_hoc_request_input_ambiguous"
    )
    assert (
        _code(_request(), context=_context(resolution_status="conflict"))
        == "ad_hoc_request_input_conflict"
    )


def test_missing_denominator_is_rejected() -> None:
    spec = _spec(
        expression=AggregateOperand(function="sum", role="numerator"),
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="metric.revenue",
            ),
        ),
        unit="ratio",
    )
    assert (
        _code(
            _request(spec),
            context=_context(asset_ids=("metric.revenue",)),
            query_plan=_query_plan(metric_keys=("metric.revenue",)),
        )
        == "ad_hoc_request_denominator_semantics_missing"
    )


def test_constant_denominator_is_rejected() -> None:
    from src.nl2sql.semantic.calculation_contract import LiteralOperand

    spec = _spec(
        expression=BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=LiteralOperand(value=100),
        ),
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="metric.revenue",
            ),
        ),
    )
    assert (
        _code(
            _request(spec),
            context=_context(asset_ids=("metric.revenue",)),
            query_plan=_query_plan(metric_keys=("metric.revenue",)),
        )
        == "ad_hoc_request_denominator_semantics_missing"
    )


def test_missing_time_is_rejected() -> None:
    assert (
        _code(_request(), query_plan=_query_plan(unresolved_slots=("time",)))
        == "ad_hoc_request_time_semantics_missing"
    )


def test_missing_join_is_rejected() -> None:
    assert (
        _code(_request(), query_plan=_query_plan(intent="comparison"))
        == "ad_hoc_request_join_semantics_missing"
    )
    assert (
        _code(_request(), query_plan=_query_plan(dimensions=("store",)))
        == "ad_hoc_request_join_semantics_missing"
    )


def test_source_plan_mismatch_is_rejected() -> None:
    assert (
        _code(_request(), query_plan=_query_plan(metric_keys=("metric.revenue",)))
        == "ad_hoc_request_source_plan_mismatch"
    )


def test_nested_ad_hoc_input_is_rejected() -> None:
    spec = _spec(
        inputs=(
            CalculationInputSpec(role="numerator", provenance="ad_hoc_metric"),
            CalculationInputSpec(
                role="denominator",
                provenance="published_gold",
                metric_key="metric.stores",
            ),
        )
    )
    assert _code(_request(spec)) == "ad_hoc_request_nested_input_unsupported"


# --- hard boundary: no SQL, no lifecycle --------------------------------------


def test_request_validation_never_executes_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    async def _boom(self: object, *args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("request validation must never execute SQL")

    monkeypatch.setattr(QueryGateway, "execute", _boom)

    # Every rejection branch runs, plus the acceptance branch.
    assert (
        _code(_request(), context=_context(resolution_status="ambiguous"))
        == "ad_hoc_request_input_ambiguous"
    )
    assert _code(_request(), query_plan=_query_plan(intent="comparison")) == (
        "ad_hoc_request_join_semantics_missing"
    )
    assert _code(_request(), catalog=ApprovedCalculationCatalog(
        [_catalog_binding("metric.revenue")]
    )) == "ad_hoc_request_input_catalog_bound"
    resolved = resolve_ad_hoc_request(
        request=_request(), context=_context(), query_plan=_query_plan()
    )
    assert resolved.derived_output_id.startswith("adhoc_")
    assert calls == []


def test_request_entry_module_has_no_definition_lifecycle_or_persistence() -> None:
    """A4: an AD_HOC request never enters the Custom Definition lifecycle."""

    import ast

    import src.nl2sql.orchestration.ad_hoc_request as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    # No definition lifecycle / repository / artifact persistence import exists.
    assert not any(
        "artifacts" in name or "definition" in name or "repository" in name
        for name in imported
    ), imported
    # The carrier itself exposes no authority/lifecycle surface.
    assert set(AdHocCalculationRequest.model_fields) == {
        "schema_version",
        "calculation_spec",
        "execution_binding",
    }
    # And the resolved result carries no definition identity/version.
    resolved_fields = set(ResolvedAdHocCalculation.__dataclass_fields__)
    assert resolved_fields == {
        "spec",
        "binding",
        "resolved_inputs",
        "derived_output_id",
    }
