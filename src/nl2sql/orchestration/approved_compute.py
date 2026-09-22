"""Trusted canonical approved-computation binding (P4-S2 kernel).

A canonical metric whose formal definition requires a governed calculation is
bound to one registered, versioned template by immutable evidence.  The binding
is the ONLY authority that may authorize a canonical TrustedCalculationStep;
registry membership, a template-id allowlist, formula text, a template version
string and model output are never authority.  Nothing here discovers release,
template or metric facts: the catalog is an already-resolved set and fail-closed
on everything else.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.nl2sql.agents.dynamic_calc.trusted_templates import (
    TrustedTemplateError,
    trusted_template_registry,
)
from src.nl2sql.contracts import (
    ContextBundle,
    ExecutionPlan,
    FetchMetricStep,
    QueryPlan,
    TrustedCalculationStep,
)


class ApprovedComputeError(ValueError):
    """Raised when canonical calculation authority is missing or inconsistent."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _checksum(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def declares_canonical_binding(step: TrustedCalculationStep) -> bool:
    """True when a calculation step declares canonical trusted-binding provenance.

    The contract enforces all-or-none provenance, so ANY present field marks the
    step as canonical and therefore as requiring catalog authority.  Such a step
    is never authorized by an approved-template-id allowlist.
    """

    return any(
        item is not None
        for item in (
            step.template_version,
            step.template_checksum,
            step.binding_checksum,
            step.output_metric_key,
        )
    )


class ApprovedCalculationInput(_FrozenModel):
    """One trusted input role bound to one canonical input metric identity."""

    role: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    metric_key: str = Field(min_length=1, max_length=256)
    metric_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovedCalculationBinding(_FrozenModel):
    """Immutable evidence binding a canonical metric to a governed calculation."""

    schema_version: Literal["1.0"] = "1.0"
    canonical_metric_key: str = Field(min_length=1, max_length=256)
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    template_version: str = Field(min_length=1, max_length=64)
    template_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: tuple[ApprovedCalculationInput, ...] = Field(min_length=1, max_length=32)
    binding_revision: str = Field(min_length=1, max_length=128)
    semantic_release_id: str = Field(min_length=1, max_length=64)
    semantic_release_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    precision: int | None = Field(default=None, ge=0, le=12)
    rounding: Literal["half_up", "half_even", "half_down", "floor", "ceil"] | None = None
    null_policy: Literal["preserve", "zero", "no_data"] | None = None
    zero_policy: Literal["preserve", "no_data"] | None = None

    @field_validator("inputs")
    @classmethod
    def _unique_roles(cls, value: tuple[ApprovedCalculationInput, ...]) -> tuple[ApprovedCalculationInput, ...]:
        roles = [item.role for item in value]
        if len(set(roles)) != len(roles):
            raise ValueError("calculation input roles must be unique")
        return value

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="json"))

    @property
    def input_roles(self) -> tuple[str, ...]:
        return tuple(item.role for item in self.inputs)

    @property
    def input_metric_keys(self) -> tuple[str, ...]:
        return tuple(item.metric_key for item in self.inputs)


class ApprovedCalculationCatalog:
    """An already-resolved canonical-metric -> binding map.  Discovers nothing."""

    def __init__(self, bindings: Iterable[ApprovedCalculationBinding] = ()) -> None:
        by_key: dict[str, ApprovedCalculationBinding] = {}
        for binding in bindings:
            if binding.canonical_metric_key in by_key:
                raise ApprovedComputeError("duplicate canonical calculation binding")
            by_key[binding.canonical_metric_key] = binding
        self._bindings = by_key

    @property
    def metric_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    def binding_for(self, metric_key: str) -> ApprovedCalculationBinding | None:
        return self._bindings.get(metric_key)

    def validate_calculation(
        self,
        *,
        step: TrustedCalculationStep,
        execution_plan: ExecutionPlan,
        context: ContextBundle,
    ) -> tuple[str, ...]:
        """Prove ONE canonical calculation against the ACTUAL execution plan.

        Authority evidence is the whole role -> dependency fetch -> metric
        identity -> input ref relationship, not a flat set of input metric
        names: a fetch that merely fetches the right metric is NOT authority
        unless it is the declared dependency of the declared role, carrying this
        binding's checksum and output key, resolved in this request's context,
        and consumed through its projected value ref.  Returns stable failure
        codes; an empty tuple means proven.
        """

        output_metric_key = step.output_metric_key
        binding = (
            self._bindings.get(output_metric_key)
            if output_metric_key is not None
            else None
        )
        if binding is None:
            return ("trusted_calculation_binding_missing",)
        failures: list[str] = []

        # 1. Binding-local identity: the step must be THIS binding, verbatim.
        if step.template_id != binding.template_id:
            failures.append("trusted_calculation_template_mismatch")
        if step.template_version != binding.template_version:
            failures.append("trusted_calculation_version_mismatch")
        if step.template_checksum != binding.template_checksum:
            failures.append("trusted_calculation_version_mismatch")
        if step.binding_checksum != binding.checksum:
            failures.append("trusted_calculation_binding_mismatch")
        if output_metric_key != binding.canonical_metric_key:
            failures.append("trusted_calculation_output_mismatch")
        if str(context.semantic_release_id) != binding.semantic_release_id:
            failures.append("trusted_calculation_release_mismatch")
        try:
            metadata = trusted_template_registry.metadata(binding.template_id)
        except TrustedTemplateError:
            failures.append("trusted_calculation_template_unregistered")
        else:
            if (
                metadata.version != binding.template_version
                or metadata.checksum != binding.template_checksum
            ):
                failures.append("trusted_calculation_version_mismatch")
            if set(metadata.input_roles) != set(binding.input_roles):
                failures.append("trusted_calculation_input_role_mismatch")
        if set(step.input_refs) != set(binding.input_roles):
            failures.append("trusted_calculation_input_role_mismatch")

        # 2. Resolve the DECLARED dependency steps of this calculation.
        steps_by_id = {item.step_id: item for item in execution_plan.steps}
        declared_fetches: list[FetchMetricStep] = []
        for dependency_id in step.depends_on:
            dependency = steps_by_id.get(dependency_id)
            if not isinstance(dependency, FetchMetricStep):
                failures.append("trusted_calculation_dependency_role_missing")
                continue
            if dependency.calculation_input_role is None:
                failures.append("trusted_calculation_dependency_role_missing")
                continue
            declared_fetches.append(dependency)

        fetches_by_role: dict[str, list[FetchMetricStep]] = {}
        for fetch in declared_fetches:
            role = fetch.calculation_input_role
            if role is None:  # unreachable: filtered above; keeps types honest
                failures.append("trusted_calculation_dependency_role_missing")
                continue
            fetches_by_role.setdefault(role, []).append(fetch)

        # 3. Every declared dependency must belong to THIS binding and role.
        for role, matches in fetches_by_role.items():
            if len(matches) > 1:
                failures.append("trusted_calculation_dependency_duplicate")
            if role not in binding.input_roles:
                failures.append("trusted_calculation_input_role_mismatch")
            for fetch in matches:
                if fetch.calculation_binding_checksum != binding.checksum:
                    failures.append("trusted_calculation_dependency_binding_mismatch")
                if fetch.calculation_output_metric_key != binding.canonical_metric_key:
                    failures.append("trusted_calculation_dependency_output_mismatch")

        # 4. Each binding role needs EXACTLY one matching dependency fetch whose
        #    metric identity, context resolution and projected-value ref all hold.
        for item in binding.inputs:
            if self._bindings.get(item.metric_key) is not None:
                # V1 has NO recursive canonical DAG.  A catalog-bound input would
                # itself require a governed calculation, so C is not executable:
                # neither the self-reference C -> C nor the nesting C -> A where A
                # is catalog-bound may be treated as an ordinary fetchable metric.
                failures.append("trusted_calculation_nested_dependency_unsupported")
            matches = fetches_by_role.get(item.role, [])
            if not matches:
                failures.append("trusted_calculation_dependency_missing")
                continue
            fetch = matches[0]
            if fetch.metric_keys != (item.metric_key,):
                failures.append("trusted_calculation_dependency_metric_mismatch")
            if item.metric_key not in context.asset_ids:
                failures.append("trusted_calculation_dependency_context_missing")
            if step.input_refs.get(item.role) != f"{fetch.step_id}.value":
                failures.append("trusted_calculation_input_ref_mismatch")

        # 5. No provenance-carrying fetch may exist outside a declared
        #    dependency of some calculation in this plan.
        # ONLY canonical-bound calculations contribute trusted dependency
        # declarations: a provenance-free legacy calculation must not be able to
        # launder an orphan provenance-carrying fetch into "declared".
        declared_ids = {
            dependency_id
            for candidate in execution_plan.steps
            if isinstance(candidate, TrustedCalculationStep)
            and declares_canonical_binding(candidate)
            for dependency_id in candidate.depends_on
        }
        for candidate in execution_plan.steps:
            if not isinstance(candidate, FetchMetricStep):
                continue
            if (
                candidate.calculation_input_role is not None
                and candidate.step_id not in declared_ids
            ):
                failures.append("trusted_calculation_dependency_rogue")

        # 6. The requested canonical output must never also be fetched directly:
        #    that would create a second, untrusted authority for the same value.
        for candidate in execution_plan.steps:
            if (
                isinstance(candidate, FetchMetricStep)
                and binding.canonical_metric_key in candidate.metric_keys
            ):
                failures.append("trusted_calculation_output_fetch_conflict")

        return tuple(dict.fromkeys(failures))

    def validate_bound_outputs(
        self,
        *,
        query_plan: QueryPlan,
        execution_plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        """Plan-level authority for the exact set of REQUESTED canonical outputs.

        Forward: each catalog-bound requested output must be claimed by exactly
        ONE canonical-bound calculation -- zero is a missing-authority deny and
        more than one is an unresolved output-authority conflict -- and no
        ordinary FetchMetricStep may carry it.  Reverse: every canonical-bound
        calculation must produce a metric that was actually requested, so a plan
        can never introduce an unrequested canonical business fact.

        This runs whether or not the plan contains a calculation step, so a
        catalog-bound canonical metric can NEVER be satisfied by an ordinary
        FetchMetricStep.  Nothing is deduplicated or arbitrated here: an invalid
        plan is simply denied.
        """

        failures: list[str] = []
        requested = tuple(
            dict.fromkeys(
                key
                for key in query_plan.metric_keys
                if self._bindings.get(key) is not None
            )
        )
        # REVERSE direction: a canonical calculation may only produce a metric
        # the request actually asked for.  Together with the forward rule below
        # this is exact requested-output closure, so an execution plan can never
        # introduce a canonical business fact the user did not request.
        for candidate in execution_plan.steps:
            if (
                isinstance(candidate, TrustedCalculationStep)
                and declares_canonical_binding(candidate)
                and candidate.output_metric_key not in query_plan.metric_keys
            ):
                failures.append("trusted_calculation_output_not_requested")
        for metric_key in requested:
            calculations = [
                step
                for step in execution_plan.steps
                if isinstance(step, TrustedCalculationStep)
                and declares_canonical_binding(step)
                and step.output_metric_key == metric_key
            ]
            if len(calculations) > 1:
                failures.append("trusted_calculation_output_duplicate")
            elif not calculations:
                failures.append("trusted_calculation_required_for_bound_metric")
            for step in execution_plan.steps:
                if isinstance(step, FetchMetricStep) and metric_key in step.metric_keys:
                    failures.append("trusted_calculation_output_fetch_conflict")
        return tuple(dict.fromkeys(failures))

    def validate_dependency_provenance(
        self,
        *,
        execution_plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        """Prove every provenance-carrying dependency fetch has exactly one owner.

        Calculation provenance is compiler/trusted-binding authority evidence, so
        an orphan (or multiply owned) provenance fetch makes the PLAN invalid.  It
        is never treated as an ordinary fetch that merely fails later at
        execution, and a legacy provenance-free calculation is never a consumer.
        """

        failures: list[str] = []
        canonical = [
            step
            for step in execution_plan.steps
            if isinstance(step, TrustedCalculationStep)
            and declares_canonical_binding(step)
        ]
        for step in execution_plan.steps:
            if not isinstance(step, FetchMetricStep):
                continue
            role = step.calculation_input_role
            if role is None:
                continue
            consumers = [
                calculation
                for calculation in canonical
                if step.step_id in calculation.depends_on
            ]
            if not consumers:
                failures.append("trusted_calculation_dependency_rogue")
                continue
            if len(consumers) > 1:
                failures.append("trusted_calculation_dependency_duplicate_consumer")
                continue
            consumer = consumers[0]
            if consumer.binding_checksum != step.calculation_binding_checksum:
                failures.append("trusted_calculation_dependency_binding_mismatch")
            if consumer.output_metric_key != step.calculation_output_metric_key:
                failures.append("trusted_calculation_dependency_output_mismatch")
            if consumer.input_refs.get(role) != f"{step.step_id}.value":
                failures.append("trusted_calculation_input_ref_mismatch")
        return tuple(dict.fromkeys(failures))

    def require_binding(self, metric_key: str) -> ApprovedCalculationBinding:
        binding = self._bindings.get(metric_key)
        if binding is None:
            raise ApprovedComputeError("canonical calculation binding missing")
        return binding


def binding_payload(binding: ApprovedCalculationBinding) -> Mapping[str, object]:
    """Canonical, secret-free payload used for evidence/checksum consumers."""

    return binding.model_dump(mode="json")


__all__ = [
    "ApprovedCalculationBinding",
    "ApprovedCalculationCatalog",
    "ApprovedCalculationInput",
    "ApprovedComputeError",
    "binding_payload",
    "declares_canonical_binding",
]
