"""Immutable Agent-side contract for ONE authoritative Effective Published Metric Result.

P3 Slice 1 owns ONLY the immutable contract: the exact 8-field business identity,
the semantic CURRENT_STATE representation, the five result statuses, and the
value/NULL/zero/no_data/missing distinctions.  It performs no database read, no
CURRENT_STATE storage-sentinel mapping, no unit/type normalization, and no status
or effective-binding inference.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PUBLISHED_RESULT_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
CURRENT_STATE: Final[Literal["CURRENT_STATE"]] = "CURRENT_STATE"

PublishedMetricStatus = Literal["success", "partial", "no_data", "failed", "missing"]
FreshnessStatus = Literal["fresh", "stale", "unknown"]
# The semantic key time is EITHER a genuine calendar date OR the CURRENT_STATE
# token.  The physical 2000-01-01 storage sentinel is deliberately NOT mapped
# here: a real historical date must remain representable as a date.
MetricTimeValue = date | Literal["CURRENT_STATE"]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_decimal(value: Decimal) -> str:
    """Exact fixed-point rendering with insignificant zeros removed.

    Numeric semantics, not spelling: 1, 1.0, 1.00 and 1E+0 all canonicalize to
    "1"; 100, 100.000 and 1E+2 to "100"; and every signed zero form to "0".
    No float round-trip and no Decimal.normalize() (which can depend on the
    decimal context); precision is preserved exactly.
    """

    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_semantic(value: object) -> object:
    """Recursively canonicalize typed Python contract values for hashing.

    Aware datetimes become their UTC instant (checksum-only; the stored model
    value is never mutated); dates become ISO calendar dates; Decimals become
    exact canonical numerics; strings stay verbatim (never trimmed).
    """

    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical_semantic(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_semantic(item) for item in value]
    return value


def _checksum(payload: object) -> str:
    canonical = _canonical_semantic(payload)
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


class PublishedMetricKey(_StrictFrozenModel):
    """The exact 8-field business identity; no physical/storage field.

    The key preserves source NULLs (None is distinct from 0) and does NOT
    enforce a dimension_type/non-null-ID pattern, because authoritative
    evidence shows area/team rows whose dimension id is NULL.
    """

    metric_code: str = Field(min_length=1, max_length=100)
    time_grain: str = Field(min_length=1, max_length=20)
    time_value: MetricTimeValue
    dimension_type: str = Field(min_length=1, max_length=50)
    area_id: int | None = None
    team_id: int | None = None
    employee_id: int | None = None
    category_code: str = Field(min_length=1, max_length=20)

    @field_validator("metric_code", "time_grain", "dimension_type", "category_code")
    @classmethod
    def _non_blank_identity(cls, value: str) -> str:
        # Reject semantically empty identity values WITHOUT normalizing: a
        # valid source string is preserved verbatim (never trimmed/rewritten).
        if not value.strip():
            raise ValueError("identity value must not be blank")
        return value

    @property
    def is_current_state(self) -> bool:
        return self.time_value == CURRENT_STATE

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="python"))


class EffectivePublishedMetricResult(_StrictFrozenModel):
    """One effective published result; incomplete effective binding fails closed.

    status is authoritative and supplied by the caller: it is never derived
    from value.  A non-missing result must identify its effective origin,
    effective revision and timezone-aware computed_at; a missing result has no
    such evidence to identify.
    """

    schema_version: Literal["1.0"] = PUBLISHED_RESULT_SCHEMA_VERSION
    key: PublishedMetricKey
    status: PublishedMetricStatus
    value: Decimal | None = None
    value_type: str | None = Field(default=None, min_length=1, max_length=32)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    error_message: str | None = Field(default=None, max_length=2000)
    data_quality_score: int | None = Field(default=None, ge=0, le=100)
    computed_at: datetime | None = None
    data_as_of: datetime | None = None
    freshness_status: FreshnessStatus = "unknown"
    effective_origin: str | None = Field(default=None, min_length=1, max_length=64)
    effective_revision: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("value")
    @classmethod
    def _finite_decimal(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("value must be a finite decimal")
        return value

    @field_validator("computed_at", "data_as_of")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        # A datetime is only genuinely offset-aware when it carries a tzinfo AND
        # that tzinfo yields a real UTC offset.  Timestamps are never normalized
        # or converted; only validated.
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_effective_binding(self) -> "EffectivePublishedMetricResult":
        if self.status == "missing":
            if self.value is not None:
                raise ValueError("missing result must not carry a value")
            return self
        if self.effective_origin is None or not self.effective_origin.strip():
            raise ValueError("non-missing result requires a non-blank effective_origin")
        if self.effective_revision is None or not self.effective_revision.strip():
            raise ValueError("non-missing result requires a non-blank effective_revision")
        if self.computed_at is None:
            raise ValueError("non-missing result requires computed_at")
        if self.status in ("no_data", "failed") and self.value is not None:
            raise ValueError(f"{self.status} result must not carry a value")
        return self

    @property
    def checksum(self) -> str:
        return _checksum(self.model_dump(mode="python"))


__all__ = [
    "CURRENT_STATE",
    "EffectivePublishedMetricResult",
    "FreshnessStatus",
    "MetricTimeValue",
    "PUBLISHED_RESULT_SCHEMA_VERSION",
    "PublishedMetricKey",
    "PublishedMetricStatus",
]
