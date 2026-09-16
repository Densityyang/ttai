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

PROJECTION_SCHEMA_VERSION = 1
PROJECTION_FINGERPRINT_SCHEMA_VERSION = "planner-metric-projection-fingerprint-v1"


class ProjectionValidationError(ValueError):
    """Raised when a Planner projection input is not an explicit contract."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExplicitMetricState(_StrictFrozenModel):
    """One independently supplied current-state fact."""

    metric_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    state: Literal["active", "inactive", "retired"]


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


class PlannerDimension(_StrictFrozenModel):
    """Semantic dimension descriptor with no physical schema information."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    dimension_type: str = Field(min_length=1)
    required: bool = False


class PlannerMetricEntry(_StrictFrozenModel):
    """The deliberately small, non-executable Planner metric contract."""

    metric_key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    display_name: str = Field(min_length=1)
    provenance: Literal["canonical_gold", "legacy_semantic"]
    source_state: Literal["CURRENT_STATE", "UNSPECIFIED"]
    state: Literal["active", "inactive", "retired"] | None = None
    metric_type: Literal["raw", "derived", "external"] | None = None
    domain: str | None = None
    tags: tuple[str, ...] = ()
    time_grains: tuple[str, ...] = ()
    dimensions: tuple[PlannerDimension, ...] = ()
    value_type: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def validate_state_binding(self) -> PlannerMetricEntry:
        if self.source_state == "CURRENT_STATE" and self.state is None:
            raise ValueError("CURRENT_STATE projection requires an explicit state")
        if self.source_state == "UNSPECIFIED" and self.state is not None:
            raise ValueError("UNSPECIFIED projection cannot carry inferred state")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("duplicate Planner tags")
        if len({item.name for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("duplicate Planner dimensions")
        return self


class PlannerMetricProjection(_StrictFrozenModel):
    """Stable-order projection that can be handed to a future Planner."""

    schema_version: Literal[1] = PROJECTION_SCHEMA_VERSION
    inventory_schema_version: Literal[1] = INVENTORY_SCHEMA_VERSION
    canonical_adapter_version: Literal["canonical-gold-yaml-v1"] = CANONICAL_ADAPTER_VERSION
    legacy_adapter_version: Literal["legacy-semantic-markdown-v1"] = LEGACY_ADAPTER_VERSION
    inventory_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_state_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    ) -> PlannerMetricProjection:
        """Project only a verified snapshot and optional explicit state input."""

        if not isinstance(snapshot, MetricInventorySnapshot):
            raise TypeError("Planner projection requires MetricInventorySnapshot")
        if current_state is not None and not isinstance(current_state, CurrentStateSnapshot):
            raise TypeError("current_state must be CurrentStateSnapshot")

        state_by_key: dict[str, Literal["active", "inactive", "retired"]] = {}
        if current_state is not None:
            state_by_key = {item.metric_key: item.state for item in current_state.records}
        inventory_keys = {item.metric_key for item in (*snapshot.canonical, *snapshot.legacy)}
        unknown_states = sorted(set(state_by_key) - inventory_keys)
        if unknown_states:
            raise ProjectionValidationError(
                f"explicit current state references unknown metric: {unknown_states[0]}"
            )

        entries = tuple(
            sorted(
                (_project_record(record, state_by_key) for record in (*snapshot.canonical, *snapshot.legacy)),
                key=lambda item: item.metric_key,
            )
        )
        try:
            return cls(
                inventory_fingerprint=snapshot.fingerprint,
                current_state_fingerprint=(None if current_state is None else current_state.fingerprint),
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
            "current_state_fingerprint": self.current_state_fingerprint,
            "metrics": [item.model_dump(mode="json") for item in self.metrics],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def project_metric_inventory(
    snapshot: MetricInventorySnapshot,
    *,
    current_state: CurrentStateSnapshot | None = None,
) -> PlannerMetricProjection:
    """Convenience wrapper for :meth:`PlannerMetricProjection.from_snapshot`."""

    return PlannerMetricProjection.from_snapshot(snapshot, current_state=current_state)


def _project_record(
    record: MetricInventoryRecord,
    state_by_key: dict[str, Literal["active", "inactive", "retired"]],
) -> PlannerMetricEntry:
    explicit_state = state_by_key.get(record.metric_key)
    return PlannerMetricEntry(
        metric_key=record.metric_key,
        display_name=record.display_name,
        provenance=record.provenance.source_kind,
        source_state=("CURRENT_STATE" if explicit_state is not None else "UNSPECIFIED"),
        state=explicit_state,
        metric_type=(None if record.metric_type == "legacy_only" else record.metric_type),  # type: ignore[arg-type]
        domain=(record.domain if record.metric_type != "legacy_only" else None),
        tags=tuple(sorted(record.tags)) if record.metric_type != "legacy_only" else (),
        time_grains=tuple(sorted(record.time_grains)) if record.metric_type != "legacy_only" else (),
        dimensions=(
            tuple(
                PlannerDimension(
                    name=item.name,
                    dimension_type=item.dimension_type,
                    required=item.required,
                )
                for item in sorted(record.dimensions, key=lambda item: (item.name, item.dimension_type))
            )
            if record.metric_type != "legacy_only"
            else ()
        ),
        value_type=(record.value_type if record.metric_type != "legacy_only" else None),
        unit=(record.unit if record.metric_type != "legacy_only" else None),
    )


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
    "ExplicitMetricState",
    "PlannerDimension",
    "PlannerMetricEntry",
    "PlannerMetricProjection",
    "ProjectionValidationError",
    "project_metric_inventory",
]
