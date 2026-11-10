"""YAML count/ratio contracts compiled into the existing semantic authoring IR.

Publication still uses authoring validation, materialization, and the active
semantic release. Loading this file alone never authorizes execution.
"""

from __future__ import annotations

import hashlib
import json
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
    """Numerator is a subset of the explicit denominator, never a formula string.

    ZERO-DENOMINATOR SEMANTICS (frozen V4 numeric constitution).  These are two
    DISTINCT outcomes and must never be conflated:

    * numerator = 0 with denominator > 0 is a VALID NUMERIC ZERO (0.00);
    * denominator = 0 is an UNDEFINED CALCULATION, not absent data.

    The "no_data" outcome therefore stays reserved for genuinely absent data
    (for example no rows at all for the period) and is NOT an admissible
    zero-denominator policy.
    """

    denominator_predicates: tuple[Predicate, ...] = Field(min_length=1)
    numerator_predicates: tuple[Predicate, ...] = Field(min_length=1)
    unit: Literal["percent"]
    value_scale: Literal["0_100"]
    decimal_places: Literal[2]
    # Division by zero is an undefined calculation, never "no data".
    zero_denominator_policy: Literal["calculation_error"] = "calculation_error"

    @model_validator(mode="after")
    def validate_predicate_authority(self) -> RatioDefinition:
        """Reject repeated fields so one metric cannot carry ambiguous roles."""

        denominator_by_field: dict[str, str] = {}
        for predicate in self.denominator_predicates:
            previous = denominator_by_field.get(predicate.field)
            if previous is not None:
                reason = "duplicate" if previous == predicate.operator else "conflicting"
                raise ValueError(f"ratio denominator contains {reason} predicate authority")
            denominator_by_field[predicate.field] = predicate.operator

        numerator_by_field: dict[str, str] = {}
        for predicate in self.numerator_predicates:
            previous = numerator_by_field.get(predicate.field)
            if previous is not None:
                reason = "duplicate" if previous == predicate.operator else "conflicting"
                raise ValueError(f"ratio numerator contains {reason} predicate authority")
            if predicate.field in denominator_by_field:
                reason = (
                    "duplicate"
                    if denominator_by_field[predicate.field] == predicate.operator
                    else "conflicting"
                )
                raise ValueError(
                    f"ratio numerator repeats denominator {reason} predicate authority"
                )
            numerator_by_field[predicate.field] = predicate.operator
        return self

    @property
    def denominator_authority(self) -> tuple[Predicate, ...]:
        """The complete denominator population predicate set."""

        return self.denominator_predicates

    @property
    def numerator_authority(self) -> tuple[Predicate, ...]:
        """The numerator population is the denominator plus numerator conditions."""

        return (*self.denominator_predicates, *self.numerator_predicates)


class FilterField(FrozenContract):
    field: Identifier
    value_type: Literal["text", "integer"]


class JoinColumnPair(FrozenContract):
    """One exact, ordered column equality in an approved JOIN condition."""

    left_column: Identifier
    right_column: Identifier


class SemanticJoinAuthority(FrozenContract):
    """Typed payload shape for a publisher-authorized semantic relationship.

    This contract only makes a proposed authority explicit and integrity
    checkable. It does not mint or confer authority: production use still
    requires a trusted SemanticRelease publisher and a matching physical
    relationship in the bound SchemaSnapshot. Until those release fields and
    their trusted loader exist, SQL JOIN remains denied.
    """

    schema_version: Literal["1.0"] = "1.0"
    join_id: ContractId
    physical_relationship_id: ContractId
    physical_relationship_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    left_relation_id: ContractId
    right_relation_id: ContractId
    column_pairs: tuple[JoinColumnPair, ...] = Field(min_length=1)
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    join_type: Literal["inner", "left", "right", "full"]
    null_semantics: Literal["exclude_unmatched", "preserve_left", "preserve_right", "preserve_both"]
    direction: Literal["left_to_right", "right_to_left", "bidirectional"]
    semantic_release_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    semantic_release_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_snapshot_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    schema_snapshot_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    applicable_fetch_step_ids: tuple[ContractId, ...] = Field(min_length=1)
    authority_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_authority_shape(self) -> SemanticJoinAuthority:
        if self.left_relation_id == self.right_relation_id:
            raise ValueError("semantic JOIN must reference two distinct relations")
        if len(set(self.column_pairs)) != len(self.column_pairs):
            raise ValueError("semantic JOIN contains duplicate column pairs")
        if len(set(self.applicable_fetch_step_ids)) != len(self.applicable_fetch_step_ids):
            raise ValueError("semantic JOIN contains duplicate execution paths")
        if self.authority_checksum != self.expected_authority_checksum():
            raise ValueError("semantic JOIN authority checksum mismatch")
        return self

    def _authority_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"authority_checksum"})

    def expected_authority_checksum(self) -> str:
        payload = json.dumps(
            self._authority_payload(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def validate_integrity(self) -> None:
        """Revalidate copied or restored instances at a future trust boundary."""

        type(self).model_validate_json(self.model_dump_json())


class MetricContract(FrozenContract):
    metric_key: Identifier
    display_name: str = Field(min_length=1)
    domain: Literal["complaint"] = "complaint"
    owner: str | None = None
    # Optional backwards-compatible metadata only.  It is NOT an Agent-side
    # approval gate: per O3, Backend/data publication is the authority boundary,
    # so no approval lookup, attestation, table or checksum exists here.
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
    # A metric is employee-capable ONLY when it explicitly declares employee
    # here; it is never inferred from team ids or from a broader scope.
    supported_dimensions: tuple[
        Literal["city_company", "area", "team", "employee"], ...
    ] = ("city_company",)
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
        # O3 alignment: an ACTIVE metric requires owner but NOT approver.
        # Activation needs no Agent-side approval record; ``approver`` (when
        # present) stays inert metadata and is never consulted as a gate.
        if self.release_status == "active" and (
            not self.owner or not self.owner.strip()
        ):
            raise ValueError("active metric requires owner")
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
