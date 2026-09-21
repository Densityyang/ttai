"""P3 Slice 2: the PublishedMetricReader contract/seam (read-only, evidence-typed).

This module defines ONLY the reader seam: an already-resolved binding, a typed
multi-key request, per-key read evidence, a read receipt, and the outcome.  It
performs no SQL, opens no session, resolves no release/source, maps no physical
CURRENT_STATE sentinel, and never fabricates provenance.  A business result
status (including missing/failed) is strictly distinct from a reader or
integration failure.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from src.nl2sql.semantic.published_result import (
    EffectivePublishedMetricResult,
    MetricTimeValue,
    PublishedMetricKey,
)

PUBLISHED_READER_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

# READER/INTEGRATION failures; never a business result status.
PublishedMetricReadFailure = Literal[
    "binding_missing",
    "binding_incomplete",
    "source_unavailable",
    "read_failed",
    "duplicate_effective_result",
    "result_invalid",
]

FreshnessStatus = Literal["fresh", "stale", "unknown"]

_SHA256 = r"^[0-9a-f]{64}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _require_non_blank(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


def _require_offset_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


class PublishedMetricReadBinding(_StrictFrozenModel):
    """An ALREADY RESOLVED release/source binding; never discovered here."""

    schema_version: Literal["1.0"] = PUBLISHED_READER_SCHEMA_VERSION
    semantic_release_id: str = Field(min_length=1, max_length=128)
    semantic_release_checksum: str = Field(pattern=_SHA256)
    published_source_id: str = Field(min_length=1, max_length=128)
    binding_revision: str = Field(min_length=1, max_length=256)

    @field_validator("semantic_release_id", "published_source_id", "binding_revision")
    @classmethod
    def _non_blank(cls, value: str, info: ValidationInfo) -> str:
        return _require_non_blank(value, info.field_name or "binding field")


class PublishedMetricReadRequest(_StrictFrozenModel):
    """A multi-key read request; request order is preserved and never reordered."""

    schema_version: Literal["1.0"] = PUBLISHED_READER_SCHEMA_VERSION
    binding: PublishedMetricReadBinding
    keys: tuple[PublishedMetricKey, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _distinct_keys(self) -> "PublishedMetricReadRequest":
        checksums = [key.checksum for key in self.keys]
        if len(set(checksums)) != len(checksums):
            raise ValueError("request keys must be semantically distinct")
        return self


class PublishedMetricReadEvidence(_StrictFrozenModel):
    """Per-key read evidence; stored_time_value is physical-read evidence only."""

    key: PublishedMetricKey
    key_checksum: str = Field(pattern=_SHA256)
    result_checksum: str = Field(pattern=_SHA256)
    semantic_time: MetricTimeValue
    stored_time_value: date | None = None
    effective_origin: str | None = Field(default=None, min_length=1, max_length=64)
    effective_revision: str | None = Field(default=None, min_length=1, max_length=256)
    data_as_of: datetime | None = None
    freshness_status: FreshnessStatus = "unknown"
    data_quality_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("data_as_of")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            _require_offset_aware(value, "data_as_of")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "PublishedMetricReadEvidence":
        if self.key_checksum != self.key.checksum:
            raise ValueError("key_checksum must equal key.checksum")
        if self.semantic_time != self.key.time_value:
            raise ValueError("semantic_time must equal key.time_value")
        return self


class PublishedMetricReadReceipt(_StrictFrozenModel):
    """One read attempt receipt; readonly is structural, never a toggle."""

    schema_version: Literal["1.0"] = PUBLISHED_READER_SCHEMA_VERSION
    binding: PublishedMetricReadBinding
    readonly: Literal[True] = True
    started_at: datetime
    completed_at: datetime
    status: Literal["succeeded", "failed"]
    failure_code: PublishedMetricReadFailure | None = None
    evidence: tuple[PublishedMetricReadEvidence, ...] = ()

    @field_validator("started_at", "completed_at")
    @classmethod
    def _aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return _require_offset_aware(value, info.field_name or "timestamp")

    @model_validator(mode="after")
    def _status_consistency(self) -> "PublishedMetricReadReceipt":
        # Compare ACTUAL instants (fold/offset aware), never wall-clock: two
        # timestamps sharing one tzinfo may differ in fold.  UTC is used for the
        # comparison only; the stored caller representations are not rewritten.
        if self.completed_at.astimezone(timezone.utc) < self.started_at.astimezone(timezone.utc):
            raise ValueError("completed_at must not precede started_at")
        if self.status == "succeeded":
            if self.failure_code is not None:
                raise ValueError("succeeded receipt must not carry a failure_code")
        else:
            if self.failure_code is None:
                raise ValueError("failed receipt requires a failure_code")
            if self.evidence:
                raise ValueError("failed receipt must not carry evidence")
        return self


class PublishedMetricReadOutcome(_StrictFrozenModel):
    """The batch outcome; counts/order/binding must match the request exactly."""

    request: PublishedMetricReadRequest
    results: tuple[EffectivePublishedMetricResult, ...] = ()
    receipt: PublishedMetricReadReceipt

    @model_validator(mode="after")
    def _batch_consistency(self) -> "PublishedMetricReadOutcome":
        if self.receipt.binding != self.request.binding:
            raise ValueError("receipt binding must equal request binding")
        if self.receipt.status == "failed":
            if self.results:
                raise ValueError("failed read outcome must not carry results")
            if self.receipt.evidence:
                raise ValueError("failed read outcome must not carry evidence")
            return self
        keys = self.request.keys
        if len(self.results) != len(keys) or len(self.receipt.evidence) != len(keys):
            raise ValueError("succeeded outcome counts must equal the request key count")
        for index, key in enumerate(keys):
            result = self.results[index]
            evidence = self.receipt.evidence[index]
            if result.key != key or evidence.key != key:
                raise ValueError("result/evidence order must match request order")
            if evidence.key_checksum != key.checksum:
                raise ValueError("evidence key_checksum mismatch")
            if evidence.result_checksum != result.checksum:
                raise ValueError("evidence result_checksum mismatch")
            if evidence.semantic_time != key.time_value:
                raise ValueError("evidence semantic_time mismatch")
            if evidence.effective_origin != result.effective_origin:
                raise ValueError("evidence effective_origin mismatch")
            if evidence.effective_revision != result.effective_revision:
                raise ValueError("evidence effective_revision mismatch")
            if evidence.data_as_of != result.data_as_of:
                raise ValueError("evidence data_as_of mismatch")
            if evidence.freshness_status != result.freshness_status:
                raise ValueError("evidence freshness_status mismatch")
            if evidence.data_quality_score != result.data_quality_score:
                raise ValueError("evidence data_quality_score mismatch")
        return self


@runtime_checkable
class PublishedMetricReader(Protocol):
    """Read-only seam over an already-resolved effective publication binding."""

    async def read(self, request: PublishedMetricReadRequest) -> PublishedMetricReadOutcome:
        ...


__all__ = [
    "PUBLISHED_READER_SCHEMA_VERSION",
    "PublishedMetricReadBinding",
    "PublishedMetricReadEvidence",
    "PublishedMetricReadFailure",
    "PublishedMetricReadOutcome",
    "PublishedMetricReadReceipt",
    "PublishedMetricReadRequest",
    "PublishedMetricReader",
]
