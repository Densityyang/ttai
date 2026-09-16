"""V4 P1 metric inventory boundary.

This module keeps the canonical Gold YAML source and the legacy semantic
reference source separate.  It deliberately stops at a verified, immutable
inventory: it does not publish assets, infer current state, compile SQL, or
join either source to the semantic authoring IR.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, Literal, NoReturn

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

INVENTORY_SCHEMA_VERSION = 1
FINGERPRINT_SCHEMA_VERSION = "metric-inventory-fingerprint-v1"
CANONICAL_ADAPTER_VERSION = "canonical-gold-yaml-v1"
LEGACY_ADAPTER_VERSION = "legacy-semantic-markdown-v1"

EXPECTED_CANONICAL_FILE_COUNT = 10
EXPECTED_CANONICAL_COUNT = 280
EXPECTED_CANONICAL_RAW_COUNT = 180
EXPECTED_CANONICAL_DERIVED_COUNT = 56
EXPECTED_CANONICAL_EXTERNAL_COUNT = 44
EXPECTED_LEGACY_DECLARED_COUNT = 93
EXPECTED_LEGACY_UNIQUE_COUNT = 93
EXPECTED_LEGACY_OVERLAP_COUNT = 79
EXPECTED_LEGACY_EXACT_OVERLAP_COUNT = 75
EXPECTED_LEGACY_COMPACT_ALIAS_COUNT = 4
EXPECTED_LEGACY_ONLY_COUNT = 14
EXPECTED_TOTAL_COUNT = EXPECTED_CANONICAL_COUNT + EXPECTED_LEGACY_ONLY_COUNT

_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_KEY_LINE_RE = re.compile(
    r"^\s*-\s+\*\*metric key[^*]*\*\*[：:]\s*`([^`]+)`.*$",
    re.IGNORECASE,
)

_CANONICAL_METRIC_KEYS = frozenset(
    {
        "name",
        "display_name",
        "description",
        "source_type",
        "calculation_type",
        "domain",
        "owner",
        "tags",
        "time_grain",
        "time_column",
        "rollup_strategy",
        "dimensions",
        "value_type",
        "unit",
        "source_table",
        "aggregation",
        "filters",
        "expression",
        "dependencies",
    }
)
_CANONICAL_REQUIRED_KEYS = frozenset(
    {
        "name",
        "display_name",
        "description",
        "source_type",
        "calculation_type",
        "domain",
        "tags",
        "time_grain",
        "value_type",
        "unit",
    }
)

# The source uses these four compact declarations.  They are explicit source
# aliases, not a general key-expansion rule; an unknown slash shape fails.
_KNOWN_COMPACT_CANONICAL_ALIASES: dict[str, tuple[str, str]] = {
    "installation_in_transit_count_day/month": (
        "installation_in_transit_count_day",
        "installation_in_transit_count_month",
    ),
    "installation_archived_count_day/month": (
        "installation_archived_count_day",
        "installation_archived_count_month",
    ),
    "repair_service_in_transit_count_day/month": (
        "repair_service_in_transit_count_day",
        "repair_service_in_transit_count_month",
    ),
    "repair_service_archived_count_day/month": (
        "repair_service_archived_count_day",
        "repair_service_archived_count_month",
    ),
}


class InventoryValidationError(ValueError):
    """Raised whenever an authoritative inventory input is not fail-closed."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceProvenance(_StrictFrozenModel):
    """Logical source identity and byte fingerprint for one authoritative file."""

    source_kind: Literal["canonical_gold", "legacy_semantic"]
    source_id: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    source_sha256: str = Field(pattern=_SHA256_RE.pattern)
    adapter_version: str = Field(min_length=1)


class MetricDimension(_StrictFrozenModel):
    """Safe semantic dimension metadata; no physical relation or column data."""

    name: str = Field(min_length=1, pattern=_IDENTITY_RE.pattern)
    dimension_type: str = Field(min_length=1)
    required: bool = False


class MetricInventoryRecord(_StrictFrozenModel):
    """One immutable, non-executable inventory record."""

    metric_key: str = Field(min_length=1, pattern=_IDENTITY_RE.pattern)
    display_name: str = Field(min_length=1)
    description: str | None = None
    metric_type: Literal["raw", "derived", "external", "legacy_only"]
    domain: str | None = None
    tags: tuple[str, ...] = ()
    time_grains: tuple[str, ...] = ()
    dimensions: tuple[MetricDimension, ...] = ()
    value_type: str | None = None
    unit: str | None = None
    rollup_strategy: str | None = None
    provenance: SourceProvenance
    definition_sha256: str = Field(pattern=_SHA256_RE.pattern)

    @model_validator(mode="after")
    def validate_record(self) -> MetricInventoryRecord:
        if self.provenance.source_kind == "canonical_gold" and self.metric_type == "legacy_only":
            raise ValueError("canonical record cannot be legacy_only")
        if self.provenance.source_kind == "legacy_semantic" and self.metric_type != "legacy_only":
            raise ValueError("legacy record must remain legacy_only")
        if any(not item.strip() for item in self.tags):
            raise ValueError("tags must be nonempty")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("duplicate tags are not silently deduplicated")
        if any(not item.strip() for item in self.time_grains):
            raise ValueError("time grains must be nonempty")
        if len({item.name for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("duplicate dimensions are not silently deduplicated")
        return self


class CanonicalSourceSummary(_StrictFrozenModel):
    file_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    unique_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    raw_count: int = Field(ge=0)
    derived_count: int = Field(ge=0)
    external_count: int = Field(ge=0)
    source_files: tuple[SourceProvenance, ...]

    @model_validator(mode="after")
    def validate_summary(self) -> CanonicalSourceSummary:
        if len(self.source_files) != self.file_count:
            raise ValueError("canonical source file count mismatch")
        if len({item.source_file for item in self.source_files}) != self.file_count:
            raise ValueError("duplicate canonical source files")
        if self.unique_count + self.duplicate_count != self.total_count:
            raise ValueError("canonical uniqueness arithmetic mismatch")
        if self.raw_count + self.derived_count + self.external_count != self.total_count:
            raise ValueError("canonical source type arithmetic mismatch")
        return self


class LegacySourceSummary(_StrictFrozenModel):
    provenance: SourceProvenance
    declared_key_count: int = Field(ge=0)
    unique_declared_key_count: int = Field(ge=0)
    canonical_overlap_count: int = Field(ge=0)
    exact_canonical_overlap_count: int = Field(ge=0)
    compact_alias_count: int = Field(ge=0)
    semantic_only_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> LegacySourceSummary:
        if self.exact_canonical_overlap_count + self.compact_alias_count != self.canonical_overlap_count:
            raise ValueError("legacy overlap arithmetic mismatch")
        if self.canonical_overlap_count + self.semantic_only_count != self.declared_key_count:
            raise ValueError("legacy declaration arithmetic mismatch")
        if self.unique_declared_key_count > self.declared_key_count:
            raise ValueError("legacy unique count cannot exceed declarations")
        return self


class CanonicalAdaptation(_StrictFrozenModel):
    records: tuple[MetricInventoryRecord, ...]
    summary: CanonicalSourceSummary

    @model_validator(mode="after")
    def validate_records(self) -> CanonicalAdaptation:
        if any(item.provenance.source_kind != "canonical_gold" for item in self.records):
            raise ValueError("canonical adaptation contains non-canonical provenance")
        keys = [item.metric_key for item in self.records]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate canonical metric identity")
        if self.summary.total_count != len(self.records):
            raise ValueError("canonical adaptation total mismatch")
        return self


class LegacyAdaptation(_StrictFrozenModel):
    """Only semantic-only legacy records enter the inventory snapshot."""

    records: tuple[MetricInventoryRecord, ...]
    summary: LegacySourceSummary

    @model_validator(mode="after")
    def validate_records(self) -> LegacyAdaptation:
        if any(item.provenance.source_kind != "legacy_semantic" for item in self.records):
            raise ValueError("legacy adaptation contains non-legacy provenance")
        if any(item.metric_type != "legacy_only" for item in self.records):
            raise ValueError("legacy records cannot be upgraded to canonical types")
        keys = [item.metric_key for item in self.records]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate legacy metric identity")
        if self.summary.semantic_only_count != len(self.records):
            raise ValueError("legacy semantic-only count mismatch")
        return self


class MetricInventorySnapshot(_StrictFrozenModel):
    """Verified immutable 280 canonical + 14 legacy-only inventory snapshot."""

    schema_version: Literal[1] = INVENTORY_SCHEMA_VERSION
    canonical_adapter_version: Literal["canonical-gold-yaml-v1"] = CANONICAL_ADAPTER_VERSION
    legacy_adapter_version: Literal["legacy-semantic-markdown-v1"] = LEGACY_ADAPTER_VERSION
    canonical: tuple[MetricInventoryRecord, ...]
    legacy: tuple[MetricInventoryRecord, ...]
    canonical_summary: CanonicalSourceSummary
    legacy_summary: LegacySourceSummary

    @model_validator(mode="after")
    def validate_snapshot(self) -> MetricInventorySnapshot:
        canonical_keys = [item.metric_key for item in self.canonical]
        legacy_keys = [item.metric_key for item in self.legacy]
        if len(set(canonical_keys)) != len(canonical_keys):
            raise ValueError("duplicate canonical metric identity")
        if len(set(legacy_keys)) != len(legacy_keys):
            raise ValueError("duplicate legacy metric identity")
        collision = sorted(set(canonical_keys).intersection(legacy_keys))
        if collision:
            raise ValueError(f"cross-source metric identity collision: {collision[0]}")
        if len(self.canonical) != EXPECTED_CANONICAL_COUNT:
            raise ValueError(f"canonical count must be {EXPECTED_CANONICAL_COUNT}")
        if len(self.legacy) != EXPECTED_LEGACY_ONLY_COUNT:
            raise ValueError(f"legacy-only count must be {EXPECTED_LEGACY_ONLY_COUNT}")
        if len(self.canonical) + len(self.legacy) != EXPECTED_TOTAL_COUNT:
            raise ValueError(f"total inventory count must be {EXPECTED_TOTAL_COUNT}")
        if self.canonical_summary.file_count != EXPECTED_CANONICAL_FILE_COUNT:
            raise ValueError("canonical file count is not locked")
        if self.canonical_summary.total_count != EXPECTED_CANONICAL_COUNT:
            raise ValueError("canonical summary count is not locked")
        if self.canonical_summary.raw_count != EXPECTED_CANONICAL_RAW_COUNT:
            raise ValueError("canonical raw count is not locked")
        if self.canonical_summary.derived_count != EXPECTED_CANONICAL_DERIVED_COUNT:
            raise ValueError("canonical derived count is not locked")
        if self.canonical_summary.external_count != EXPECTED_CANONICAL_EXTERNAL_COUNT:
            raise ValueError("canonical external count is not locked")
        if self.legacy_summary.declared_key_count != EXPECTED_LEGACY_DECLARED_COUNT:
            raise ValueError("legacy declared count is not locked")
        if self.legacy_summary.unique_declared_key_count != EXPECTED_LEGACY_UNIQUE_COUNT:
            raise ValueError("legacy unique count is not locked")
        if self.legacy_summary.canonical_overlap_count != EXPECTED_LEGACY_OVERLAP_COUNT:
            raise ValueError("legacy overlap count is not locked")
        if self.legacy_summary.exact_canonical_overlap_count != EXPECTED_LEGACY_EXACT_OVERLAP_COUNT:
            raise ValueError("legacy exact overlap count is not locked")
        if self.legacy_summary.compact_alias_count != EXPECTED_LEGACY_COMPACT_ALIAS_COUNT:
            raise ValueError("legacy compact alias count is not locked")
        if self.legacy_summary.semantic_only_count != EXPECTED_LEGACY_ONLY_COUNT:
            raise ValueError("legacy semantic-only count is not locked")
        return self

    @property
    def total_count(self) -> int:
        return len(self.canonical) + len(self.legacy)

    @property
    def canonical_keys(self) -> frozenset[str]:
        return frozenset(item.metric_key for item in self.canonical)

    @property
    def fingerprint(self) -> str:
        payload = {
            "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
            "inventory_schema_version": self.schema_version,
            "canonical_adapter_version": self.canonical_adapter_version,
            "legacy_adapter_version": self.legacy_adapter_version,
            "canonical_summary": self.canonical_summary.model_dump(mode="json"),
            "legacy_summary": self.legacy_summary.model_dump(mode="json"),
            "records": [
                _record_payload(record)
                for record in sorted(
                    (*self.canonical, *self.legacy),
                    key=lambda item: (item.provenance.source_kind, item.metric_key),
                )
            ],
        }
        return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def build_metric_inventory(
    canonical_directory: str | Path,
    legacy_path: str | Path,
) -> MetricInventorySnapshot:
    """Read the two authoritative sources and return a verified snapshot."""

    canonical = adapt_canonical_gold(canonical_directory)
    legacy = adapt_legacy_semantic(legacy_path, canonical_keys={item.metric_key for item in canonical.records})
    try:
        return MetricInventorySnapshot(
            canonical=tuple(sorted(canonical.records, key=lambda item: item.metric_key)),
            legacy=tuple(sorted(legacy.records, key=lambda item: item.metric_key)),
            canonical_summary=canonical.summary,
            legacy_summary=legacy.summary,
        )
    except ValidationError as exc:
        raise InventoryValidationError(f"invalid metric inventory snapshot: {exc}") from exc


def adapt_canonical_gold(canonical_directory: str | Path) -> CanonicalAdaptation:
    """Adapt exactly the copied canonical Gold YAML directory."""

    directory = Path(canonical_directory)
    if not directory.is_dir():
        _fail(f"canonical directory does not exist: {directory}")
    paths = sorted(directory.glob("*.yaml"), key=lambda item: item.name)
    if len(paths) != EXPECTED_CANONICAL_FILE_COUNT:
        _fail(f"canonical YAML file count must be {EXPECTED_CANONICAL_FILE_COUNT}, got {len(paths)}")

    records: list[MetricInventoryRecord] = []
    provenance: list[SourceProvenance] = []
    for path in paths:
        try:
            source_bytes = path.read_bytes()
            document = yaml.safe_load(source_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise InventoryValidationError(f"cannot read canonical source {path.name}: {exc}") from exc
        source = _make_provenance(
            source_kind="canonical_gold",
            source_file=path.name,
            source_sha256=_sha256_bytes(source_bytes),
            adapter_version=CANONICAL_ADAPTER_VERSION,
        )
        provenance.append(source)
        records.extend(_adapt_canonical_document(document, source))

    _ensure_unique([item.metric_key for item in records], "canonical metric identity")
    raw_count = sum(item.metric_type == "raw" for item in records)
    derived_count = sum(item.metric_type == "derived" for item in records)
    external_count = sum(item.metric_type == "external" for item in records)
    summary = CanonicalSourceSummary(
        file_count=len(paths),
        total_count=len(records),
        unique_count=len(set(item.metric_key for item in records)),
        duplicate_count=len(records) - len(set(item.metric_key for item in records)),
        raw_count=raw_count,
        derived_count=derived_count,
        external_count=external_count,
        source_files=tuple(sorted(provenance, key=lambda item: item.source_file)),
    )
    return CanonicalAdaptation(records=tuple(records), summary=summary)


def adapt_canonical_yaml_document(
    document: Mapping[str, Any],
    *,
    source_file: str = "<memory>.yaml",
    source_sha256: str | None = None,
) -> tuple[MetricInventoryRecord, ...]:
    """Adapt one already-parsed canonical document for focused fail-closed tests."""

    if source_sha256 is None:
        source_sha256 = _sha256_bytes(_canonical_json(document).encode("utf-8"))
    source = _make_provenance(
        source_kind="canonical_gold",
        source_file=source_file,
        source_sha256=source_sha256,
        adapter_version=CANONICAL_ADAPTER_VERSION,
    )
    records = _adapt_canonical_document(document, source)
    _ensure_unique([item.metric_key for item in records], "canonical metric identity")
    return records


def adapt_legacy_semantic(
    legacy_path: str | Path,
    *,
    canonical_keys: Collection[str],
) -> LegacyAdaptation:
    """Adapt legacy declarations, retaining only explicit semantic-only keys."""

    path = Path(legacy_path)
    if not path.is_file():
        _fail(f"legacy semantic source does not exist: {path}")
    try:
        source_bytes = path.read_bytes()
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InventoryValidationError(f"cannot read legacy source {path.name}: {exc}") from exc
    return adapt_legacy_semantic_text(
        text,
        canonical_keys=canonical_keys,
        source_file=path.name,
        source_sha256=_sha256_bytes(source_bytes),
    )


def adapt_legacy_semantic_text(
    text: str,
    *,
    canonical_keys: Collection[str],
    source_file: str = "semantic.md",
    source_sha256: str | None = None,
    enforce_expected_counts: bool = True,
) -> LegacyAdaptation:
    """Adapt legacy Markdown without treating it as canonical executable metadata."""

    if not isinstance(text, str):
        _fail("legacy semantic input must be text")
    normalized_canonical_keys = frozenset(canonical_keys)
    if any(not isinstance(item, str) or not _IDENTITY_RE.fullmatch(item) for item in normalized_canonical_keys):
        _fail("canonical identity set contains an invalid metric key")
    if source_sha256 is None:
        source_sha256 = _sha256_bytes(text.encode("utf-8"))
    source = _make_provenance(
        source_kind="legacy_semantic",
        source_file=source_file,
        source_sha256=source_sha256,
        adapter_version=LEGACY_ADAPTER_VERSION,
    )

    blocks: list[tuple[str, str]] = []
    heading: str | None = None
    block_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("### "):
            if heading is not None:
                blocks.append((heading, "".join(block_lines)))
            heading = line[4:].strip()
            if not heading:
                _fail("legacy metric heading is empty")
            block_lines = [line]
        elif heading is not None:
            block_lines.append(line)
    if heading is not None:
        blocks.append((heading, "".join(block_lines)))

    declared: list[str] = []
    records: list[MetricInventoryRecord] = []
    exact_overlap = 0
    compact_aliases = 0
    for block_heading, block_text in blocks:
        key_lines = [
            line for line in block_text.splitlines()
            if line.lstrip().lower().startswith("- **metric key")
        ]
        for line in key_lines:
            match = _LEGACY_KEY_LINE_RE.fullmatch(line)
            if match is None:
                _fail(f"unknown legacy metric key shape in heading {block_heading!r}")
            token = match.group(1).strip()
            if not token:
                _fail("legacy metric identity is empty")
            if token in declared:
                _fail(f"duplicate legacy metric declaration: {token}")
            declared.append(token)

            compact_mapping = _KNOWN_COMPACT_CANONICAL_ALIASES.get(token)
            if compact_mapping is not None:
                if not set(compact_mapping).issubset(normalized_canonical_keys):
                    _fail(f"legacy compact alias does not map to canonical identities: {token}")
                compact_aliases += 1
                continue
            if "/" in token:
                _fail(f"unknown legacy slash key shape: {token}")
            if not _IDENTITY_RE.fullmatch(token):
                _fail(f"invalid legacy metric identity: {token}")
            if token in normalized_canonical_keys:
                exact_overlap += 1
                continue
            try:
                records.append(
                    MetricInventoryRecord(
                        metric_key=token,
                        display_name=block_heading,
                        description=None,
                        metric_type="legacy_only",
                        domain=None,
                        tags=(),
                        time_grains=(),
                        dimensions=(),
                        value_type=None,
                        unit=None,
                        rollup_strategy=None,
                        provenance=source,
                        definition_sha256=_sha256_bytes(
                            _canonical_json(
                                {
                                    "heading": block_heading,
                                    "metric_key": token,
                                    "block_sha256": _sha256_bytes(block_text.encode("utf-8")),
                                }
                            ).encode("utf-8")
                        ),
                    )
                )
            except ValidationError as exc:
                raise InventoryValidationError(f"invalid legacy metric {token}: {exc}") from exc

    _ensure_unique(declared, "legacy metric declaration")
    overlap = exact_overlap + compact_aliases
    summary = LegacySourceSummary(
        provenance=source,
        declared_key_count=len(declared),
        unique_declared_key_count=len(set(declared)),
        canonical_overlap_count=overlap,
        exact_canonical_overlap_count=exact_overlap,
        compact_alias_count=compact_aliases,
        semantic_only_count=len(records),
    )
    if enforce_expected_counts:
        if summary.declared_key_count != EXPECTED_LEGACY_DECLARED_COUNT:
            _fail(f"legacy declared count must be {EXPECTED_LEGACY_DECLARED_COUNT}")
        if summary.unique_declared_key_count != EXPECTED_LEGACY_UNIQUE_COUNT:
            _fail(f"legacy unique count must be {EXPECTED_LEGACY_UNIQUE_COUNT}")
        if summary.canonical_overlap_count != EXPECTED_LEGACY_OVERLAP_COUNT:
            _fail(f"legacy overlap count must be {EXPECTED_LEGACY_OVERLAP_COUNT}")
        if summary.semantic_only_count != EXPECTED_LEGACY_ONLY_COUNT:
            _fail(f"legacy semantic-only count must be {EXPECTED_LEGACY_ONLY_COUNT}")
    return LegacyAdaptation(
        records=tuple(sorted(records, key=lambda item: item.metric_key)),
        summary=summary,
    )


def _adapt_canonical_document(
    document: Any,
    provenance: SourceProvenance,
) -> tuple[MetricInventoryRecord, ...]:
    if not isinstance(document, Mapping):
        _fail(f"canonical document {provenance.source_file} must be a mapping")
    if set(document) != {"metrics"}:
        _fail(f"unknown canonical document shape in {provenance.source_file}")
    metrics = document.get("metrics")
    if type(metrics) is not list:
        _fail(f"canonical metrics must be a list in {provenance.source_file}")
    records: list[MetricInventoryRecord] = []
    for index, raw_metric in enumerate(metrics):
        if not isinstance(raw_metric, Mapping):
            _fail(f"canonical metric {index} must be a mapping in {provenance.source_file}")
        records.append(_adapt_canonical_metric(raw_metric, provenance))
    return tuple(records)


def _adapt_canonical_metric(
    raw_metric: Mapping[str, Any],
    provenance: SourceProvenance,
) -> MetricInventoryRecord:
    keys = set(raw_metric)
    unknown = sorted(keys - _CANONICAL_METRIC_KEYS)
    missing = sorted(_CANONICAL_REQUIRED_KEYS - keys)
    if unknown:
        _fail(f"unknown canonical metric fields: {', '.join(unknown)}")
    if missing:
        _fail(f"missing canonical metric fields: {', '.join(missing)}")

    name = _strict_nonempty_string(raw_metric["name"], "canonical metric name")
    if not _IDENTITY_RE.fullmatch(name):
        _fail(f"invalid canonical metric identity: {name}")
    display_name = _strict_nonempty_string(raw_metric["display_name"], f"display name for {name}")
    description = _strict_string(raw_metric["description"], f"description for {name}")
    source_type = _strict_string(raw_metric["source_type"], f"source_type for {name}")
    if source_type not in {"raw", "derived", "external"}:
        _fail(f"unknown canonical source_type for {name}: {source_type}")
    _strict_nonempty_string(raw_metric["calculation_type"], f"calculation_type for {name}")
    domain = _strict_nonempty_string(raw_metric["domain"], f"domain for {name}")
    tags = _strict_string_list(raw_metric["tags"], f"tags for {name}", unique=True)
    time_grain = _strict_nonempty_string(raw_metric["time_grain"], f"time_grain for {name}")
    value_type = _strict_nonempty_string(raw_metric["value_type"], f"value_type for {name}")
    unit = _strict_nonempty_string(raw_metric["unit"], f"unit for {name}")

    for optional in ("owner", "time_column", "source_table"):
        if optional in raw_metric and raw_metric[optional] is not None:
            _strict_nonempty_string(raw_metric[optional], f"{optional} for {name}")
    if "rollup_strategy" in raw_metric and raw_metric["rollup_strategy"] is not None:
        _strict_nonempty_string(raw_metric["rollup_strategy"], f"rollup_strategy for {name}")
    if "expression" in raw_metric:
        _strict_string(raw_metric["expression"], f"expression for {name}")
    if "dimensions" in raw_metric:
        dimensions = _adapt_dimensions(raw_metric["dimensions"], name)
    else:
        dimensions = ()
    if "aggregation" in raw_metric:
        _validate_aggregation(raw_metric["aggregation"], name)
    if "filters" in raw_metric:
        _validate_filters(raw_metric["filters"], name)
    if "dependencies" in raw_metric:
        _validate_dependencies(raw_metric["dependencies"], name)

    try:
        return MetricInventoryRecord(
            metric_key=name,
            display_name=display_name,
            description=description or None,
            metric_type=source_type,  # type: ignore[arg-type]
            domain=domain,
            tags=tuple(sorted(tags)),
            time_grains=(time_grain,),
            dimensions=tuple(sorted(dimensions, key=lambda item: (item.name, item.dimension_type, item.required))),
            value_type=value_type,
            unit=unit,
            rollup_strategy=(
                None
                if raw_metric.get("rollup_strategy") is None
                else _strict_nonempty_string(raw_metric["rollup_strategy"], f"rollup_strategy for {name}")
            ),
            provenance=provenance,
            definition_sha256=_sha256_bytes(_canonical_json(raw_metric).encode("utf-8")),
        )
    except ValidationError as exc:
        raise InventoryValidationError(f"invalid canonical metric {name}: {exc}") from exc


def _adapt_dimensions(value: Any, metric_key: str) -> tuple[MetricDimension, ...]:
    if type(value) is not list:
        _fail(f"dimensions must be a list for {metric_key}")
    dimensions: list[MetricDimension] = []
    for item in value:
        if not isinstance(item, Mapping):
            _fail(f"dimension must be a mapping for {metric_key}")
        unknown = set(item) - {"name", "type", "required"}
        if unknown or not {"name", "type"}.issubset(item):
            _fail(f"unknown dimension shape for {metric_key}")
        name = _strict_nonempty_string(item["name"], f"dimension name for {metric_key}")
        if not _IDENTITY_RE.fullmatch(name):
            _fail(f"invalid dimension name for {metric_key}: {name}")
        dimension_type = _strict_nonempty_string(item["type"], f"dimension type for {metric_key}")
        required = item.get("required", False)
        if type(required) is not bool:
            _fail(f"dimension required flag must be bool for {metric_key}")
        try:
            dimensions.append(MetricDimension(name=name, dimension_type=dimension_type, required=required))
        except ValidationError as exc:
            raise InventoryValidationError(f"invalid dimension for {metric_key}: {exc}") from exc
    _ensure_unique([item.name for item in dimensions], f"dimension identity for {metric_key}")
    return tuple(dimensions)


def _validate_aggregation(value: Any, metric_key: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"operation", "column"}:
        _fail(f"unknown aggregation shape for {metric_key}")
    _strict_nonempty_string(value["operation"], f"aggregation operation for {metric_key}")
    _strict_nonempty_string(value["column"], f"aggregation column for {metric_key}")


def _validate_filters(value: Any, metric_key: str) -> None:
    if type(value) is not list:
        _fail(f"filters must be a list for {metric_key}")
    for item in value:
        if not isinstance(item, Mapping):
            _fail(f"filter must be a mapping for {metric_key}")
        if set(item) - {"column", "operator", "value"} or not {"column", "operator"}.issubset(item):
            _fail(f"unknown filter shape for {metric_key}")
        _strict_nonempty_string(item["column"], f"filter column for {metric_key}")
        _strict_nonempty_string(item["operator"], f"filter operator for {metric_key}")
        if "value" in item:
            _validate_json_value(item["value"], f"filter value for {metric_key}")


def _validate_dependencies(value: Any, metric_key: str) -> None:
    if not isinstance(value, Mapping):
        _fail(f"dependencies must be a mapping for {metric_key}")
    for key, dependency in value.items():
        if not isinstance(key, str) or not _IDENTITY_RE.fullmatch(key):
            _fail(f"invalid dependency label for {metric_key}")
        _strict_nonempty_string(dependency, f"dependency value for {metric_key}")


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or type(value) in {str, bool, int, float}:
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, path)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(f"non-string JSON key at {path}")
            _validate_json_value(item, path)
        return
    _fail(f"unknown value shape at {path}")


def _make_provenance(
    *,
    source_kind: Literal["canonical_gold", "legacy_semantic"],
    source_file: str,
    source_sha256: str,
    adapter_version: str,
) -> SourceProvenance:
    try:
        return SourceProvenance(
            source_kind=source_kind,
            source_id=source_kind,
            source_file=source_file,
            source_sha256=source_sha256,
            adapter_version=adapter_version,
        )
    except ValidationError as exc:
        raise InventoryValidationError(f"invalid source provenance: {exc}") from exc


def _record_payload(record: MetricInventoryRecord) -> dict[str, Any]:
    return {
        "metric_key": record.metric_key,
        "display_name": record.display_name,
        "description": record.description,
        "metric_type": record.metric_type,
        "domain": record.domain,
        "tags": list(record.tags),
        "time_grains": list(record.time_grains),
        "dimensions": [item.model_dump(mode="json") for item in record.dimensions],
        "value_type": record.value_type,
        "unit": record.unit,
        "rollup_strategy": record.rollup_strategy,
        "provenance": record.provenance.model_dump(mode="json"),
        "definition_sha256": record.definition_sha256,
    }


def _strict_string(value: Any, path: str) -> str:
    if type(value) is not str:
        _fail(f"{path} must be a string")
    return value


def _strict_nonempty_string(value: Any, path: str) -> str:
    result = _strict_string(value, path)
    if not result.strip():
        _fail(f"{path} must be nonempty")
    return result.strip()


def _strict_string_list(value: Any, path: str, *, unique: bool = False) -> tuple[str, ...]:
    if type(value) is not list:
        _fail(f"{path} must be a list")
    result = tuple(_strict_nonempty_string(item, path) for item in value)
    if unique:
        _ensure_unique(result, path)
    return result


def _ensure_unique(values: Collection[str], label: str) -> None:
    if len(set(values)) != len(values):
        _fail(f"duplicate {label}; no silent dedupe")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        items = [_canonicalize(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return value


def _fail(message: str) -> NoReturn:
    raise InventoryValidationError(message)


__all__ = [
    "CANONICAL_ADAPTER_VERSION",
    "EXPECTED_CANONICAL_COUNT",
    "EXPECTED_CANONICAL_DERIVED_COUNT",
    "EXPECTED_CANONICAL_EXTERNAL_COUNT",
    "EXPECTED_CANONICAL_RAW_COUNT",
    "EXPECTED_LEGACY_ONLY_COUNT",
    "EXPECTED_TOTAL_COUNT",
    "FINGERPRINT_SCHEMA_VERSION",
    "CanonicalAdaptation",
    "CanonicalSourceSummary",
    "InventoryValidationError",
    "LegacyAdaptation",
    "LegacySourceSummary",
    "MetricDimension",
    "MetricInventoryRecord",
    "MetricInventorySnapshot",
    "SourceProvenance",
    "adapt_canonical_gold",
    "adapt_canonical_yaml_document",
    "adapt_legacy_semantic",
    "adapt_legacy_semantic_text",
    "build_metric_inventory",
]
