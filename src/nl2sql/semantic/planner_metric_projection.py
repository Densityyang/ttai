"""P1 Planner-safe projection of a verified metric inventory.

The projection is a read-only planning contract.  It accepts only the
validated inventory snapshot from :mod:`metric_inventory`; it never consumes
AuthoringIR, release state, SQL, physical schema, or an online planner.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.nl2sql.semantic.metric_inventory import (
    CANONICAL_ADAPTER_VERSION,
    INVENTORY_SCHEMA_VERSION,
    LEGACY_ADAPTER_VERSION,
    MetricInventoryRecord,
    MetricInventorySnapshot,
)

PROJECTION_SCHEMA_VERSION = 2
PROJECTION_FINGERPRINT_SCHEMA_VERSION = "planner-metric-projection-fingerprint-v2"

LifecycleValue = Literal["active", "inactive", "retired"]
SourceReadinessValue = Literal["ready", "pending_source", "unavailable"]
PlanningReadiness = Literal[
    "ready",
    "active_pending_source",
    "active_source_unavailable",
    "active_source_unspecified",
    "not_active",
    "lifecycle_unspecified",
]


class ProjectionValidationError(ValueError):
    """Raised when a Planner projection input is not an explicit contract."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExplicitMetricState(_StrictFrozenModel):
    """One independently supplied lifecycle fact (active/inactive/retired)."""

    metric_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    state: LifecycleValue

    @property
    def lifecycle(self) -> LifecycleValue:
        return self.state


class CurrentStateSnapshot(_StrictFrozenModel):
    """Explicit state input; canonical/legacy provenance cannot create it."""

    schema_version: Literal[1] = 1
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[ExplicitMetricState, ...] = ()

    @model_validator(mode="after")
    def validate_records(self) -> CurrentStateSnapshot:
        keys = [item.metric_key for item in self.records]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate explicit current-state metric identity")
        return self

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "records": [
                item.model_dump(mode="json")
                for item in sorted(self.records, key=lambda record: record.metric_key)
            ],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class ExplicitMetricLifecycle(_StrictFrozenModel):
    """One independently supplied lifecycle fact."""

    metric_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    lifecycle: LifecycleValue

    @property
    def state(self) -> LifecycleValue:
        return self.lifecycle


class LifecycleSnapshot(_StrictFrozenModel):
    """Explicit lifecycle input; canonical YAML cannot create it."""

    schema_version: Literal[1] = 1
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[ExplicitMetricLifecycle, ...] = ()

    @model_validator(mode="after")
    def validate_records(self) -> LifecycleSnapshot:
        keys = [item.metric_key for item in self.records]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate explicit lifecycle metric identity")
        return self

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "records": [
                item.model_dump(mode="json")
                for item in sorted(self.records, key=lambda record: record.metric_key)
            ],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class ExplicitMetricSourceReadiness(_StrictFrozenModel):
    """One independently supplied source readiness fact."""

    metric_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    readiness: SourceReadinessValue


class SourceReadinessSnapshot(_StrictFrozenModel):
    """Explicit source readiness input; canonical YAML cannot create it."""

    schema_version: Literal[1] = 1
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[ExplicitMetricSourceReadiness, ...] = ()

    @model_validator(mode="after")
    def validate_records(self) -> SourceReadinessSnapshot:
        keys = [item.metric_key for item in self.records]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate explicit source readiness metric identity")
        return self

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "records": [
                item.model_dump(mode="json")
                for item in sorted(self.records, key=lambda record: record.metric_key)
            ],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class PlannerDimension(_StrictFrozenModel):
    """Semantic dimension descriptor with no physical schema information."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    dimension_type: str = Field(min_length=1)
    required: bool = False


class PlannerCategory(_StrictFrozenModel):
    """Explicit typed category dimension exposed to the Planner."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    dimension_type: Literal["category"] = "category"
    required: bool = False


class PlannerDependency(_StrictFrozenModel):
    """Typed canonical dependency reference; no formula or SQL is exposed."""

    label: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    target: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")


class PlannerMetricEntry(_StrictFrozenModel):
    """The deliberately small, non-executable Planner metric contract."""

    metric_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    display_name: str = Field(min_length=1)
    provenance: Literal["canonical_gold", "legacy_semantic"]
    source_state: Literal["CURRENT_STATE", "UNSPECIFIED"]
    state: LifecycleValue | None = None
    metric_type: Literal["raw", "derived", "external"] | None = None
    domain: str | None = None
    tags: tuple[str, ...] = ()
    time_grains: tuple[str, ...] = ()
    dimensions: tuple[PlannerDimension, ...] = ()
    categories: tuple[PlannerCategory, ...] = ()
    dependencies: tuple[PlannerDependency, ...] = ()
    value_type: str | None = None
    unit: str | None = None
    source_readiness_state: Literal["SOURCE_READINESS", "UNSPECIFIED"] = "UNSPECIFIED"
    source_readiness: SourceReadinessValue | None = None
    planning_readiness: PlanningReadiness = "lifecycle_unspecified"

    @model_validator(mode="after")
    def validate_state_binding(self) -> PlannerMetricEntry:
        if self.source_state == "CURRENT_STATE" and self.state is None:
            raise ValueError("CURRENT_STATE projection requires an explicit state")
        if self.source_state == "UNSPECIFIED" and self.state is not None:
            raise ValueError("UNSPECIFIED projection cannot carry inferred state")
        if self.source_readiness_state == "SOURCE_READINESS" and self.source_readiness is None:
            raise ValueError("SOURCE_READINESS projection requires an explicit readiness value")
        if self.source_readiness_state == "UNSPECIFIED" and self.source_readiness is not None:
            raise ValueError("UNSPECIFIED readiness cannot carry implied readiness")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("duplicate Planner tags")
        if len({item.name for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("duplicate Planner dimensions")
        if len({item.name for item in self.categories}) != len(self.categories):
            raise ValueError("duplicate Planner categories")
        if len({item.label for item in self.dependencies}) != len(self.dependencies):
            raise ValueError("duplicate Planner dependency labels")
        if self.planning_readiness != _planning_readiness(self.state, self.source_readiness):
            raise ValueError("planning readiness must derive from explicit lifecycle and readiness")
        return self

    @property
    def lifecycle(self) -> LifecycleValue | None:
        return self.state

    @property
    def lifecycle_state(self) -> Literal["CURRENT_STATE", "UNSPECIFIED"]:
        return self.source_state

    @property
    def is_planner_ready(self) -> bool:
        return self.planning_readiness == "ready"


class PlannerMetricProjection(_StrictFrozenModel):
    """Stable-order projection that can be handed to a future Planner."""

    schema_version: Literal[2] = PROJECTION_SCHEMA_VERSION
    inventory_schema_version: Literal[2] = INVENTORY_SCHEMA_VERSION
    canonical_adapter_version: Literal["canonical-gold-yaml-v2"] = CANONICAL_ADAPTER_VERSION
    legacy_adapter_version: Literal["legacy-semantic-markdown-v1"] = LEGACY_ADAPTER_VERSION
    inventory_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_readiness_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metrics: tuple[PlannerMetricEntry, ...]

    @model_validator(mode="after")
    def validate_order(self) -> PlannerMetricProjection:
        keys = [item.metric_key for item in self.metrics]
        if keys != sorted(keys):
            raise ValueError("Planner metrics must use stable metric-key order")
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate Planner metric identity")
        return self

    @classmethod
    def from_snapshot(
        cls,
        snapshot: MetricInventorySnapshot,
        *,
        current_state: CurrentStateSnapshot | None = None,
        lifecycle: LifecycleSnapshot | None = None,
        source_readiness: SourceReadinessSnapshot | None = None,
    ) -> PlannerMetricProjection:
        """Project a verified snapshot plus independent explicit state inputs."""

        if not isinstance(snapshot, MetricInventorySnapshot):
            raise TypeError("Planner projection requires MetricInventorySnapshot")
        if current_state is not None and not isinstance(current_state, CurrentStateSnapshot):
            raise TypeError("current_state must be CurrentStateSnapshot")
        if lifecycle is not None and not isinstance(lifecycle, LifecycleSnapshot):
            raise TypeError("lifecycle must be LifecycleSnapshot")
        if source_readiness is not None and not isinstance(source_readiness, SourceReadinessSnapshot):
            raise TypeError("source_readiness must be SourceReadinessSnapshot")
        if current_state is not None and lifecycle is not None:
            raise ProjectionValidationError(
                "contradictory explicit lifecycle inputs: current_state and lifecycle"
            )

        lifecycle_source: CurrentStateSnapshot | LifecycleSnapshot | None = (
            current_state if current_state is not None else lifecycle
        )
        lifecycle_by_key: dict[str, LifecycleValue] = {}
        if lifecycle_source is not None:
            lifecycle_by_key = {
                item.metric_key: item.lifecycle for item in lifecycle_source.records
            }
        readiness_by_key: dict[str, SourceReadinessValue] = {}
        if source_readiness is not None:
            readiness_by_key = {
                item.metric_key: item.readiness for item in source_readiness.records
            }

        inventory_keys = {item.metric_key for item in (*snapshot.canonical, *snapshot.legacy)}
        for label, keys in (
            ("lifecycle", set(lifecycle_by_key)),
            ("source readiness", set(readiness_by_key)),
        ):
            unknown = sorted(keys - inventory_keys)
            if unknown:
                raise ProjectionValidationError(
                    f"explicit {label} references unknown metric: {unknown[0]}"
                )
        retired_ready = sorted(
            key
            for key, readiness in readiness_by_key.items()
            if readiness == "ready" and lifecycle_by_key.get(key) == "retired"
        )
        if retired_ready:
            raise ProjectionValidationError(
                "contradictory explicit inputs: retired lifecycle cannot be source ready: "
                + retired_ready[0]
            )

        entries = tuple(
            sorted(
                (
                    _project_record(record, lifecycle_by_key, readiness_by_key)
                    for record in (*snapshot.canonical, *snapshot.legacy)
                ),
                key=lambda item: item.metric_key,
            )
        )
        try:
            return cls(
                inventory_fingerprint=snapshot.fingerprint,
                lifecycle_fingerprint=(
                    None if lifecycle_source is None else lifecycle_source.fingerprint
                ),
                source_readiness_fingerprint=(
                    None if source_readiness is None else source_readiness.fingerprint
                ),
                metrics=entries,
            )
        except ValidationError as exc:
            raise ProjectionValidationError(f"invalid Planner metric projection: {exc}") from exc

    @property
    def fingerprint(self) -> str:
        payload = {
            "fingerprint_schema_version": PROJECTION_FINGERPRINT_SCHEMA_VERSION,
            "projection_schema_version": self.schema_version,
            "inventory_schema_version": self.inventory_schema_version,
            "canonical_adapter_version": self.canonical_adapter_version,
            "legacy_adapter_version": self.legacy_adapter_version,
            "inventory_fingerprint": self.inventory_fingerprint,
            "lifecycle_fingerprint": self.lifecycle_fingerprint,
            "source_readiness_fingerprint": self.source_readiness_fingerprint,
            "metrics": [item.model_dump(mode="json") for item in self.metrics],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def project_metric_inventory(
    snapshot: MetricInventorySnapshot,
    *,
    current_state: CurrentStateSnapshot | None = None,
    lifecycle: LifecycleSnapshot | None = None,
    source_readiness: SourceReadinessSnapshot | None = None,
) -> PlannerMetricProjection:
    """Convenience wrapper for :meth:`PlannerMetricProjection.from_snapshot`."""

    return PlannerMetricProjection.from_snapshot(
        snapshot,
        current_state=current_state,
        lifecycle=lifecycle,
        source_readiness=source_readiness,
    )


def _project_record(
    record: MetricInventoryRecord,
    lifecycle_by_key: dict[str, LifecycleValue],
    readiness_by_key: dict[str, SourceReadinessValue],
) -> PlannerMetricEntry:
    explicit_lifecycle = lifecycle_by_key.get(record.metric_key)
    explicit_readiness = readiness_by_key.get(record.metric_key)
    canonical = record.metric_type != "legacy_only"
    return PlannerMetricEntry(
        metric_key=record.metric_key,
        display_name=record.display_name,
        provenance=record.provenance.source_kind,
        source_state=("CURRENT_STATE" if explicit_lifecycle is not None else "UNSPECIFIED"),
        state=explicit_lifecycle,
        source_readiness_state=(
            "SOURCE_READINESS" if explicit_readiness is not None else "UNSPECIFIED"
        ),
        source_readiness=explicit_readiness,
        planning_readiness=_planning_readiness(explicit_lifecycle, explicit_readiness),
        metric_type=(None if record.metric_type == "legacy_only" else record.metric_type),  # type: ignore[arg-type]
        domain=(record.domain if canonical else None),
        tags=tuple(sorted(record.tags)) if canonical else (),
        time_grains=tuple(sorted(record.time_grains)) if canonical else (),
        dimensions=(
            tuple(
                PlannerDimension(
                    name=item.name,
                    dimension_type=item.dimension_type,
                    required=item.required,
                )
                for item in sorted(record.dimensions, key=lambda item: (item.name, item.dimension_type))
            )
            if canonical
            else ()
        ),
        categories=(
            tuple(
                PlannerCategory(name=item.name, required=item.required)
                for item in record.categories
            )
            if canonical
            else ()
        ),
        dependencies=(
            tuple(
                PlannerDependency(label=item.label, target=item.target)
                for item in record.dependencies
            )
            if canonical
            else ()
        ),
        value_type=(record.value_type if canonical else None),
        unit=(record.unit if canonical else None),
    )


def _planning_readiness(
    lifecycle: LifecycleValue | None,
    readiness: SourceReadinessValue | None,
) -> PlanningReadiness:
    if lifecycle is None:
        return "lifecycle_unspecified"
    if lifecycle != "active":
        return "not_active"
    if readiness is None:
        return "active_source_unspecified"
    if readiness == "ready":
        return "ready"
    if readiness == "pending_source":
        return "active_pending_source"
    return "active_source_unavailable"


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        items = [_canonicalize(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return value


__all__ = [
    "CurrentStateSnapshot",
    "ExplicitMetricLifecycle",
    "ExplicitMetricSourceReadiness",
    "ExplicitMetricState",
    "LifecycleSnapshot",
    "PlannerCategory",
    "PlannerDependency",
    "PlannerDimension",
    "PlannerMetricEntry",
    "PlannerMetricProjection",
    "ProjectionValidationError",
    "SourceReadinessSnapshot",
    "project_metric_inventory",
]
