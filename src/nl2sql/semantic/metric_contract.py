"""YAML count/ratio contracts compiled into the existing semantic authoring IR.

Publication still uses authoring validation, materialization, and the active
semantic release. Loading this file alone never authorizes execution.
"""

from __future__ import annotations

from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.nl2sql.semantic.authoring import AssetStatus, AuthoringIR, MetricAsset

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
ContractId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Predicate(FrozenContract):
    field: Identifier
    operator: Literal["is_true", "is_null", "is_not_null"]


class RatioDefinition(FrozenContract):
    """Numerator is a subset of the explicit denominator, never a formula string."""

    denominator_predicates: tuple[Predicate, ...] = Field(min_length=1)
    numerator_predicates: tuple[Predicate, ...] = Field(min_length=1)
    unit: Literal["percent"]
    value_scale: Literal["0_100"]
    decimal_places: Literal[2]
    zero_denominator_policy: Literal["no_data"]


class FilterField(FrozenContract):
    field: Identifier
    value_type: Literal["text", "integer"]


class MetricContract(FrozenContract):
    metric_key: Identifier
    display_name: str = Field(min_length=1)
    domain: Literal["complaint"] = "complaint"
    owner: str | None = None
    approver: str | None = None
    release_status: Literal["active", "pending_source", "retired"] = "pending_source"
    daily_report_enabled: bool = False
    daily_report_order: int | None = Field(default=None, ge=1)
    assistant_enabled: bool = True
    benchmark_eligible: bool = False
    source_ref: ContractId
    formula_version: ContractId
    eligibility_policy_id: ContractId
    operation: Literal["count", "ratio"] = "count"
    ratio: RatioDefinition | None = None
    business_time_column: Identifier
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    supported_grains: tuple[Literal["day", "month"], ...] = ("day", "month")
    supported_dimensions: tuple[Literal["city_company", "area", "team"], ...] = ("city_company",)
    predicates: tuple[Predicate, ...] = ()
    filters: tuple[FilterField, ...] = ()
    required_permissions: tuple[str, ...] = Field(min_length=1)
    freshness_sla_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_definition(self) -> MetricContract:
        if (self.operation == "ratio") != (self.ratio is not None):
            raise ValueError("ratio requires a ratio definition; count cannot carry one")
        if (not self.supported_dimensions
                or len(set(self.supported_dimensions)) != len(self.supported_dimensions)):
            raise ValueError("supported dimensions must be nonempty and unique")
        if self.release_status == "active" and (
            not self.owner or not self.owner.strip() or not self.approver or not self.approver.strip()
        ):
            raise ValueError("active metric requires owner and approver")
        if not self.supported_grains or len(set(self.supported_grains)) != len(self.supported_grains):
            raise ValueError("supported grains must be nonempty and unique")
        if any(not item.strip() for item in self.required_permissions):
            raise ValueError("permissions must be nonempty")
        if len({item.field for item in self.filters}) != len(self.filters):
            raise ValueError("filter fields must be unique")
        if self.daily_report_order is not None and not self.daily_report_enabled:
            raise ValueError("daily report order requires enabled channel")
        return self

    @property
    def asset_id(self) -> str:
        return f"metric.{self.metric_key}"

    @property
    def formula_predicates(self) -> tuple[Predicate, ...]:
        return self.predicates + (() if self.ratio is None else (
            *self.ratio.denominator_predicates, *self.ratio.numerator_predicates,
        ))


class MetricCatalog(FrozenContract):
    schema_version: Literal[1] = 1
    metrics: tuple[MetricContract, ...]

    @model_validator(mode="after")
    def unique_keys(self) -> MetricCatalog:
        if len({item.metric_key for item in self.metrics}) != len(self.metrics):
            raise ValueError("duplicate metric key")
        return self


def load_metric_catalog(content: str) -> MetricCatalog:
    return MetricCatalog.model_validate(yaml.safe_load(content))


def metric_catalog_ir(catalog: MetricCatalog, *, relations: dict[str, str]) -> AuthoringIR:
    """Join source references to deployment-approved names before normal validation.

    Relation mappings are deployment inputs, never QueryPlan fields. Missing
    mappings cannot yield active authoring assets.
    """
    metrics = []
    for contract in catalog.metrics:
        relation = relations.get(contract.source_ref)
        active = contract.release_status == "active"
        if active and relation is None:
            raise ValueError("active metric source binding missing")
        metrics.append(MetricAsset(
            asset_id=contract.asset_id,
            metric_key=contract.metric_key,
            display_name=contract.display_name,
            source_relation=relation,
            status=AssetStatus.ACTIVE if active else AssetStatus.RETIRED,
            domain=contract.domain,
            source_columns=tuple(sorted({
                "is_valid_for_metrics", contract.business_time_column,
                *(item.field for item in contract.formula_predicates),
                *(item.field for item in contract.filters),
            })),
            owner=contract.owner,
            sensitivity="internal",
            freshness_sla_seconds=contract.freshness_sla_seconds,
            execution_contract=contract.model_dump(mode="json"),
        ))
    return AuthoringIR(metrics=tuple(metrics))
