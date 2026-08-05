"""Schema-v3 semantic authoring contract and deterministic validation.

The project historically stored semantic assets in three human-editable files:
``semantic.md``, ``qa.md`` and ``ai_views.yaml``.  This module is the narrow
authoring boundary for those files.  It parses the legacy shape, compiles it
to one typed intermediate representation (IR), and reports every validation
finding with a stable asset id and checksum.

The compiler deliberately does not publish, retrieve, embed, or migrate
assets.  Those are release/indexing concerns owned by later PRs.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import sqlglot
import yaml
from sqlglot import exp

SCHEMA_VERSION = 3
DEFAULT_OWNER = "legacy-semantic-authoring"
DEFAULT_SENSITIVITY = "internal"
DEFAULT_FRESHNESS_SLA_SECONDS = 86_400
DEFAULT_RELEASE_DOMAIN = "complaint"


class AssetType(StrEnum):
    METRIC = "metric"
    QA = "qa"
    VIEW = "view"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"
    ERROR = "error"


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class AuthoringIssue:
    """One actionable compiler or validation finding."""

    code: str
    message: str
    asset_id: str = ""
    path: str = ""
    severity: IssueSeverity = IssueSeverity.ERROR

    def to_dict(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class MetricAsset:
    """Typed metric definition produced from one or more markdown variants."""

    asset_id: str
    metric_key: str
    display_name: str
    source_relation: str | None = None
    status: AssetStatus = AssetStatus.ACTIVE
    domain: str = "unknown"
    aliases: tuple[str, ...] = ()
    kind: str = "base"
    dependencies: tuple[str, ...] = ()
    formula: str | None = None
    calculation_template_id: str | None = None
    calculation_template_version: str | None = None
    decimal_scale: int | None = None
    rounding: str | None = None
    unit: str | None = None
    null_strategy: str | None = None
    zero_strategy: str | None = None
    source_columns: tuple[str, ...] = ()
    owner: str | None = None
    sensitivity: str | None = None
    freshness_sla_seconds: int | None = None
    schema_version: int = SCHEMA_VERSION
    source_line: int | None = None
    raw_text: str = ""
    legacy_metadata_inferred: bool = False

    @property
    def asset_type(self) -> AssetType:
        return AssetType.METRIC


@dataclass(frozen=True, slots=True)
class QAAsset:
    """Typed question/answer asset with SQL preflight metadata."""

    asset_id: str
    case_id: str
    question: str
    sql: str
    status: AssetStatus = AssetStatus.ACTIVE
    domain: str = "unknown"
    metric_keys: tuple[str, ...] = ()
    answer: str = ""
    owner: str | None = None
    sensitivity: str | None = None
    freshness_sla_seconds: int | None = None
    schema_version: int = SCHEMA_VERSION
    source_line: int | None = None
    raw_text: str = ""
    legacy_metadata_inferred: bool = False

    @property
    def asset_type(self) -> AssetType:
        return AssetType.QA


@dataclass(frozen=True, slots=True)
class JoinDefinition:
    """One explicit view join from the YAML authoring contract."""

    table: str
    alias: str
    join_type: str = "left"
    join_condition: str | None = None
    using: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ViewAsset:
    """Typed AI view definition."""

    asset_id: str
    name: str
    source_relation: str
    source_alias: str
    columns: tuple[str, ...] = ()
    joins: tuple[JoinDefinition, ...] = ()
    filters: tuple[dict[str, Any], ...] = ()
    status: AssetStatus = AssetStatus.ACTIVE
    domain: str = "shared"
    owner: str | None = None
    sensitivity: str | None = None
    freshness_sla_seconds: int | None = None
    schema_version: int = SCHEMA_VERSION
    source_line: int | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)
    legacy_metadata_inferred: bool = False

    @property
    def asset_type(self) -> AssetType:
        return AssetType.VIEW


AuthoringAsset = MetricAsset | QAAsset | ViewAsset


@dataclass(frozen=True, slots=True)
class AuthoringIR:
    """Unified schema-v3 intermediate representation."""

    metrics: tuple[MetricAsset, ...] = ()
    qas: tuple[QAAsset, ...] = ()
    views: tuple[ViewAsset, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @property
    def assets(self) -> tuple[AuthoringAsset, ...]:
        return (*self.metrics, *self.qas, *self.views)

    @property
    def checksum(self) -> str:
        canonical = json.dumps(
            _canonicalize(self.to_payload()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assets": [
                _asset_payload(asset)
                for asset in sorted(self.assets, key=lambda item: item.asset_id)
            ],
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Stable, serializable validation result for a candidate authoring set."""

    schema_version: int
    checksum: str
    metric_heading_count: int
    qa_count: int
    view_count: int
    issues: tuple[AuthoringIssue, ...]
    asset_statuses: Mapping[str, AssetStatus]
    asset_domains: Mapping[str, str] = field(default_factory=dict)
    default_release_domain: str = DEFAULT_RELEASE_DOMAIN

    @property
    def ok(self) -> bool:
        return not any(issue.severity == IssueSeverity.ERROR for issue in self.issues)

    @property
    def error_count(self) -> int:
        return sum(issue.severity == IssueSeverity.ERROR for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == IssueSeverity.WARNING for issue in self.issues)

    @property
    def default_release_ready(self) -> bool:
        """Only the complaint domain may become the default canary candidate."""

        return not any(
            issue.asset_id
            and self.asset_domains.get(issue.asset_id) == self.default_release_domain
            for issue in self.issues
        )

    @property
    def release_candidates(self) -> tuple[str, ...]:
        """Return the only domain allowed to become a default candidate."""

        return (self.default_release_domain,) if self.default_release_ready else ()

    @property
    def validated_domains(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.asset_domains.values())))

    @property
    def status_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in AssetStatus}
        for status in self.asset_statuses.values():
            counts[status.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_statuses": {
                key: value.value for key, value in sorted(self.asset_statuses.items())
            },
            "asset_domains": dict(sorted(self.asset_domains.items())),
            "checksum": self.checksum,
            "default_release_domain": self.default_release_domain,
            "default_release_ready": self.default_release_ready,
            "error_count": self.error_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "metric_heading_count": self.metric_heading_count,
            "ok": self.ok,
            "qa_count": self.qa_count,
            "schema_version": self.schema_version,
            "release_candidates": list(self.release_candidates),
            "status_counts": self.status_counts,
            "validated_domains": list(self.validated_domains),
            "view_count": self.view_count,
            "warning_count": self.warning_count,
        }


@dataclass(frozen=True, slots=True)
class AuthoringCompilation:
    """Convenience result used by file-based release checks."""

    ir: AuthoringIR
    report: ValidationReport


_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_DOMAIN_RE = re.compile(
    r"^##\s+业务域\s*[：:]\s*.*?[（(]\s*([a-z][a-z0-9_-]*)\s*[）)]",
    re.IGNORECASE | re.MULTILINE,
)
_QA_HEADING_RE = re.compile(r"^##\s+Q\s*:\s*(.+?)\s*$", re.MULTILINE)
_CODE_RE = re.compile(r"`([^`]+)`")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_QUALIFIED_IDENTIFIER_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b"
)
_LEGACY_SHORTHAND_RE = re.compile(r"同上|命名规律同上|按上述|依此类推|同前", re.IGNORECASE)
_KEY_LINE_RE = re.compile(r"metric\s*key", re.IGNORECASE)
_METADATA_RE = {
    "owner": re.compile(r"(?:\*\*)?owner(?:\*\*)?\s*[：:]\s*`?([^`\n]+)", re.I),
    "status": re.compile(r"(?:\*\*)?status(?:\*\*)?\s*[：:]\s*`?([^`\n]+)", re.I),
    "sensitivity": re.compile(
        r"(?:\*\*)?(?:sensitivity|敏感级别)(?:\*\*)?\s*[：:]\s*`?([^`\n]+)", re.I
    ),
    "freshness": re.compile(
        r"(?:\*\*)?(?:freshness(?:[_ ]sla[_ ]seconds)?|新鲜度(?:SLA)?)(?:\*\*)?\s*[：:]\s*`?([^`\n]+)",
        re.I,
    ),
}
_SOURCE_RE = re.compile(
    r"^\s*(?:\*\*)?(?:源表|source[_ ](?:relation|table))(?:\*\*)?\s*[：:][^`\n]*`([^`]+)`",
    re.I | re.MULTILINE,
)
_FORMULA_RE = re.compile(r"(?:公式|计算公式|formula)[^`\n]*`([^`]+)`", re.I)
_SOURCE_COLUMNS_RE = re.compile(
    r"(?:源列|source[_ ]columns?)[^`\n]*`([^`]+)`", re.I
)
_DOMAIN_FROM_KEY_RE = re.compile(r"^([a-z][a-z0-9_]*)_", re.I)
_KNOWN_JOIN_TYPES = {"inner", "left", "right", "full"}


def parse_semantic_metrics(
    text: str,
    *,
    legacy_defaults: bool = True,
) -> tuple[MetricAsset, ...]:
    """Parse every ``###`` block and expand multi-variant metric keys."""

    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        raise ValueError("semantic.md contains no metric headings")
    domains = list(_DOMAIN_RE.finditer(text))
    source_lines = list(_SOURCE_RE.finditer(text))
    document_schema_version = _document_schema_version(text)
    metrics: list[MetricAsset] = []

    for index, match in enumerate(matches, start=1):
        block_start = match.start()
        block_end = matches[index].start() if index < len(matches) else len(text)
        block = text[block_start:block_end].strip()
        section_block = _cut_at_next_domain_heading(block)
        title = match.group(1).strip()
        domain = _domain_at(domains, block_start) or "unknown"
        key_values: list[str] = []
        for line in section_block.splitlines():
            if not _KEY_LINE_RE.search(line):
                continue
            spans = _CODE_RE.findall(line)
            if spans:
                key_values.extend(item for span in spans for item in _expand_metric_keys(span))
            else:
                key_values.extend(_extract_identifier_keys(line))
        key_values = list(_unique(key_values))
        if not key_values:
            key_values = [""]

        metadata = _metadata(section_block, legacy_defaults=legacy_defaults)
        source_relation = _first_match(_SOURCE_RE, section_block) or _source_at(source_lines, block_start)
        formula = _first_match(_FORMULA_RE, section_block)
        dependencies = _extract_metric_dependencies(section_block)
        kind = "derived" if _is_derived(section_block) else "base"
        source_columns = _parse_csv_identifiers(_first_match(_SOURCE_COLUMNS_RE, section_block))
        aliases = (
            (title,)
            if len(key_values) == 1
            else tuple(f"{title}::{key}" for key in key_values)
        )
        for key in key_values:
            stable_key = key or f"missing-{_short_hash(f'{title}:{index}') }"
            metrics.append(
                MetricAsset(
                    asset_id=f"metric.{stable_key}",
                    metric_key=key,
                    display_name=title,
                    source_relation=source_relation,
                    status=metadata["status"],
                    domain=domain,
                    aliases=aliases if len(key_values) == 1 else (f"{title}::{key}",),
                    kind=kind,
                    dependencies=dependencies,
                    formula=formula,
                    calculation_template_id=("ratio" if kind == "derived" else None),
                    calculation_template_version=("1.0" if kind == "derived" else None),
                    unit="percent" if kind == "derived" and "%" in section_block else None,
                    source_columns=source_columns,
                    owner=metadata["owner"],
                    sensitivity=metadata["sensitivity"],
                    freshness_sla_seconds=metadata["freshness"],
                    schema_version=document_schema_version,
                    source_line=text.count("\n", 0, block_start) + 1,
                    raw_text=block,
                    legacy_metadata_inferred=metadata["inferred"],
                )
            )
    return tuple(metrics)


def parse_qa_assets(
    text: str,
    *,
    metric_keys: Iterable[str] = (),
    relation_domains: Mapping[str, str] | None = None,
    legacy_defaults: bool = True,
) -> tuple[QAAsset, ...]:
    """Parse QA blocks, retiring the five fictional generic examples."""

    matches = list(_QA_HEADING_RE.finditer(text))
    if not matches:
        raise ValueError("qa.md contains no Q headings")
    known_keys = set(metric_keys)
    relation_domains = relation_domains or {}
    document_schema_version = _document_schema_version(text)
    qas: list[QAAsset] = []
    for index, match in enumerate(matches, start=1):
        block_start = match.start()
        block_end = matches[index].start() if index < len(matches) else len(text)
        block = text[block_start:block_end].strip()
        question = match.group(1).strip()
        sql_match = re.search(r"```(?:sql)?\s*\n?(.*?)```", block, re.IGNORECASE | re.DOTALL)
        sql = sql_match.group(1).strip() if sql_match else ""
        answer = block[: sql_match.start()] if sql_match else block
        answer = answer.split("\n", 1)[1].strip() if "\n" in answer else ""
        generic = bool(re.search(r"\b(?:work_orders|users)\b", block, re.I))
        metadata = _metadata(block, legacy_defaults=legacy_defaults)
        status = AssetStatus.RETIRED if generic else metadata["status"]
        metric_refs = tuple(
            key for key in _extract_identifier_keys(block) if key in known_keys
        )
        relation = _first_relation_from_sql(sql)
        domain = (
            _domain_from_metric_key(metric_refs[0])
            if metric_refs
            else relation_domains.get(relation or "", "unknown")
        )
        case_hash = _short_hash(question)
        qas.append(
            QAAsset(
                asset_id=f"qa.{case_hash}",
                case_id=f"qa-{case_hash}",
                question=question,
                sql=sql,
                status=status,
                domain=domain,
                metric_keys=_unique(metric_refs),
                answer=answer,
                owner=metadata["owner"],
                sensitivity=metadata["sensitivity"],
                freshness_sla_seconds=metadata["freshness"],
                schema_version=document_schema_version,
                source_line=text.count("\n", 0, block_start) + 1,
                raw_text=block,
                legacy_metadata_inferred=metadata["inferred"],
            )
        )
    return tuple(qas)


def parse_ai_views(
    text: str,
    *,
    legacy_defaults: bool = True,
) -> tuple[ViewAsset, ...]:
    """Parse the YAML view authoring file into typed view assets."""

    payload = yaml.safe_load(text)
    if not isinstance(payload, Mapping):
        raise ValueError("ai_views.yaml must contain a mapping")
    raw_views = payload.get("views")
    if not isinstance(raw_views, list):
        raise ValueError("ai_views.yaml must contain a views list")
    document_schema_version = _safe_int(payload.get("schema_version"), SCHEMA_VERSION)

    views: list[ViewAsset] = []
    for index, raw in enumerate(raw_views, start=1):
        if not isinstance(raw, Mapping):
            raw = {}
        name = _as_text(raw.get("name"))
        source_relation = _as_text(raw.get("source_table") or raw.get("source_relation"))
        source_alias = _as_text(raw.get("source_alias"))
        metadata = _metadata_from_mapping(raw, legacy_defaults=legacy_defaults)
        joins: list[JoinDefinition] = []
        raw_joins = raw.get("joins")
        if isinstance(raw_joins, list):
            for raw_join in raw_joins:
                if not isinstance(raw_join, Mapping):
                    raw_join = {}
                using = raw_join.get("using", ())
                if isinstance(using, str):
                    using = (using,)
                elif isinstance(using, list):
                    using = tuple(_as_text(item) for item in using if _as_text(item))
                else:
                    using = ()
                joins.append(
                    JoinDefinition(
                        table=_as_text(raw_join.get("table")),
                        alias=_as_text(raw_join.get("alias")),
                        join_type=_as_text(raw_join.get("type") or raw_join.get("join_type"))
                        or "left",
                        join_condition=_as_optional_text(
                            raw_join.get("join_condition") or raw_join.get("on")
                        ),
                        using=using,
                    )
                )
        columns = raw.get("columns", ())
        if not isinstance(columns, list):
            columns = ()
        filters = raw.get("filters", ())
        if not isinstance(filters, list):
            filters = ()
        views.append(
            ViewAsset(
                asset_id=f"view.{name or f'missing-{index}'}",
                name=name,
                source_relation=source_relation,
                source_alias=source_alias,
                columns=tuple(_as_text(column) for column in columns),
                joins=tuple(joins),
                filters=tuple(item for item in filters if isinstance(item, dict)),
                status=metadata["status"],
                owner=metadata["owner"],
                sensitivity=metadata["sensitivity"],
                freshness_sla_seconds=metadata["freshness"],
                schema_version=document_schema_version,
                source_line=index,
                raw_payload=dict(raw),
                legacy_metadata_inferred=metadata["inferred"],
            )
        )
    return tuple(views)


def compile_authoring_ir(
    semantic_markdown: str,
    qa_markdown: str,
    ai_views_yaml: str,
    *,
    legacy_defaults: bool = True,
) -> AuthoringIR:
    """Compile all three authoring sources into one deterministic IR."""

    metrics = parse_semantic_metrics(semantic_markdown, legacy_defaults=legacy_defaults)
    views = parse_ai_views(ai_views_yaml, legacy_defaults=legacy_defaults)
    relation_domains = {
        view.source_relation: _domain_from_view_name(view.name)
        for view in views
        if view.source_relation
    }
    qas = parse_qa_assets(
        qa_markdown,
        metric_keys=(metric.metric_key for metric in metrics if metric.metric_key),
        relation_domains=relation_domains,
        legacy_defaults=legacy_defaults,
    )
    return AuthoringIR(metrics=metrics, qas=qas, views=views)


def compile_authoring_files(
    semantic_path: Path,
    qa_path: Path,
    views_path: Path,
    *,
    legacy_defaults: bool = True,
    relation_columns: Mapping[str, Iterable[str]] | None = None,
) -> AuthoringCompilation:
    """Read the three source files, compile them, and return their report."""

    ir = compile_authoring_ir(
        semantic_path.read_text(encoding="utf-8"),
        qa_path.read_text(encoding="utf-8"),
        views_path.read_text(encoding="utf-8"),
        legacy_defaults=legacy_defaults,
    )
    return AuthoringCompilation(ir=ir, report=validate_authoring_ir(ir, relation_columns=relation_columns))


def validate_authoring_ir(
    ir: AuthoringIR,
    *,
    relation_columns: Mapping[str, Iterable[str]] | None = None,
) -> ValidationReport:
    """Validate metadata, names, formulas, joins, relations and active QA SQL."""

    issues: list[AuthoringIssue] = []
    if ir.schema_version != SCHEMA_VERSION:
        issues.append(
            AuthoringIssue(
                "schema_version_mismatch",
                f"IR schema_version must be {SCHEMA_VERSION}",
                severity=IssueSeverity.ERROR,
            )
        )
    for asset in ir.assets:
        if asset.schema_version != SCHEMA_VERSION:
            issues.append(
                AuthoringIssue(
                    "schema_version_mismatch",
                    f"asset schema_version must be {SCHEMA_VERSION}",
                    asset.asset_id,
                    "schema_version",
                )
            )

    issues.extend(_duplicate_metric_issues(ir.metrics))
    catalog = _build_relation_catalog(ir.views)
    if relation_columns:
        catalog = catalog.with_external_columns(relation_columns)
    issues.extend(_validate_metadata(ir.assets))
    issues.extend(_validate_metrics(ir.metrics, catalog))
    issues.extend(_validate_views(ir.views, catalog))
    issues.extend(_validate_qas(ir.qas, catalog))
    issues.extend(_validate_dependency_graph(ir.metrics))
    issues = _sort_issues(issues)

    error_asset_ids = {
        issue.asset_id
        for issue in issues
        if issue.severity == IssueSeverity.ERROR and issue.asset_id
    }
    asset_statuses = {
        asset.asset_id: (
            AssetStatus.ERROR if asset.asset_id in error_asset_ids else asset.status
        )
        for asset in ir.assets
    }
    return ValidationReport(
        schema_version=ir.schema_version,
        checksum=ir.checksum,
        metric_heading_count=_metric_heading_count(ir.metrics),
        qa_count=len(ir.qas),
        view_count=len(ir.views),
        issues=tuple(issues),
        asset_statuses=asset_statuses,
        asset_domains={asset.asset_id: asset.domain for asset in ir.assets},
    )


def _validate_metadata(assets: Sequence[AuthoringAsset]) -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    for asset in assets:
        if asset.status != AssetStatus.ACTIVE:
            continue
        for field_name, value, code in (
            ("owner", asset.owner, "missing_owner"),
            ("sensitivity", asset.sensitivity, "missing_sensitivity"),
            ("freshness_sla_seconds", asset.freshness_sla_seconds, "missing_freshness"),
        ):
            if value is None or value == "":
                issues.append(
                    AuthoringIssue(
                        code,
                        f"active asset requires {field_name}",
                        asset.asset_id,
                        field_name,
                    )
                )
        if getattr(asset, "legacy_metadata_inferred", False):
            issues.append(
                AuthoringIssue(
                    "legacy_metadata_inferred",
                    "owner/sensitivity/freshness were inferred for a legacy asset; fill the v3 template before release",
                    asset.asset_id,
                    "metadata",
                    IssueSeverity.WARNING,
                )
            )
    return issues


def _validate_metrics(
    metrics: Sequence[MetricAsset],
    catalog: "RelationCatalog",
) -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    for metric in metrics:
        if not metric.metric_key:
            issues.append(
                AuthoringIssue("missing_metric_key", "metric key is required", metric.asset_id)
            )
            continue
        if metric.status != AssetStatus.ACTIVE:
            continue
        if _LEGACY_SHORTHAND_RE.search(metric.raw_text):
            issues.append(
                AuthoringIssue(
                    "implicit_shorthand",
                    "active metric must spell out every dimension and time variant",
                    metric.asset_id,
                    "raw_text",
                )
            )
        if not metric.source_relation:
            issues.append(
                AuthoringIssue(
                    "missing_source_relation",
                    "active metric requires source_relation",
                    metric.asset_id,
                    "source_relation",
                )
            )
        elif not catalog.has_relation(metric.source_relation):
            issues.append(
                AuthoringIssue(
                    "unknown_relation",
                    f"source relation does not exist in the view catalog: {metric.source_relation}",
                    metric.asset_id,
                    "source_relation",
                )
            )
        for column in metric.source_columns:
            if metric.source_relation and not catalog.has_column(metric.source_relation, column):
                issues.append(
                    AuthoringIssue(
                        "unknown_column",
                        f"source column does not exist: {metric.source_relation}.{column}",
                        metric.asset_id,
                        "source_columns",
                    )
                )
        if metric.kind.lower() in {"derived", "kpi"}:
            for field_name, value, code in (
                ("formula", metric.formula, "missing_formula"),
                ("calculation_template_id", metric.calculation_template_id, "missing_template"),
                ("calculation_template_version", metric.calculation_template_version, "missing_template_version"),
                ("decimal_scale", metric.decimal_scale, "missing_precision"),
                ("rounding", metric.rounding, "missing_rounding"),
                ("unit", metric.unit, "missing_unit"),
                ("null_strategy", metric.null_strategy, "missing_null_strategy"),
                ("zero_strategy", metric.zero_strategy, "missing_zero_strategy"),
            ):
                if value is None or value == "":
                    issues.append(
                        AuthoringIssue(
                            code,
                            f"active derived metric requires {field_name}",
                            metric.asset_id,
                            field_name,
                        )
                    )
    return issues


def _validate_views(
    views: Sequence[ViewAsset],
    catalog: "RelationCatalog",
) -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    for view in views:
        if view.status != AssetStatus.ACTIVE:
            continue
        if not view.name:
            issues.append(AuthoringIssue("missing_view_name", "view name is required", view.asset_id))
        if not view.source_relation:
            issues.append(
                AuthoringIssue("missing_source_relation", "view source_table is required", view.asset_id)
            )
        aliases = {view.source_alias} if view.source_alias else set()
        if not view.source_alias:
            issues.append(
                AuthoringIssue("missing_source_alias", "view source_alias is required", view.asset_id)
            )
        for join in view.joins:
            if join.join_type.lower() not in _KNOWN_JOIN_TYPES:
                issues.append(
                    AuthoringIssue(
                        "illegal_join",
                        f"join type is not allowed: {join.join_type}",
                        view.asset_id,
                        "joins",
                    )
                )
            if not join.table or not join.alias:
                issues.append(
                    AuthoringIssue(
                        "illegal_join",
                        "every join requires table and alias",
                        view.asset_id,
                        "joins",
                    )
                )
            if not join.join_condition and not join.using:
                issues.append(
                    AuthoringIssue(
                        "illegal_join",
                        "join must declare join_condition or using",
                        view.asset_id,
                        "joins",
                    )
                )
            if join.alias in aliases:
                issues.append(
                    AuthoringIssue(
                        "duplicate_relation_alias",
                        f"relation alias is duplicated: {join.alias}",
                        view.asset_id,
                        "joins",
                    )
                )
            if join.table and not catalog.has_relation(join.table):
                issues.append(
                    AuthoringIssue(
                        "unknown_relation",
                        f"joined relation does not exist in the view catalog: {join.table}",
                        view.asset_id,
                        "joins",
                    )
                )
            aliases.add(join.alias)
            for alias, column in _qualified_refs(join.join_condition or ""):
                if alias not in aliases:
                    issues.append(
                        AuthoringIssue(
                            "unknown_relation",
                            f"join references unknown alias: {alias}",
                            view.asset_id,
                            "joins.join_condition",
                        )
                    )
                elif catalog.has_relation(_alias_relation(view, alias)) and not catalog.has_column(
                    _alias_relation(view, alias), column
                ):
                    issues.append(
                        AuthoringIssue(
                            "unknown_column",
                            f"join column does not exist: {alias}.{column}",
                            view.asset_id,
                            "joins.join_condition",
                        )
                    )
        for expression in view.columns:
            refs = _qualified_refs(expression)
            if not refs and not expression.strip():
                issues.append(
                    AuthoringIssue("missing_view_column", "view column cannot be empty", view.asset_id, "columns")
                )
            for alias, column in refs:
                if alias not in aliases:
                    issues.append(
                        AuthoringIssue(
                            "unknown_relation",
                            f"view column references unknown alias: {alias}",
                            view.asset_id,
                            "columns",
                        )
                    )
                else:
                    relation = _alias_relation(view, alias)
                    if catalog.has_relation(relation) and not catalog.has_column(relation, column):
                        issues.append(
                            AuthoringIssue(
                                "unknown_column",
                                f"view column does not exist: {alias}.{column}",
                                view.asset_id,
                                "columns",
                            )
                        )
    return issues


def _validate_qas(qas: Sequence[QAAsset], catalog: "RelationCatalog") -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    for qa in qas:
        if qa.status != AssetStatus.ACTIVE:
            continue
        if not qa.case_id:
            issues.append(AuthoringIssue("missing_case_id", "active QA requires case_id", qa.asset_id))
        if not qa.sql:
            issues.append(AuthoringIssue("missing_sql", "active QA requires SQL", qa.asset_id, "sql"))
            continue
        try:
            parsed = sqlglot.parse_one(qa.sql, read="postgres")
        except Exception as exc:
            issues.append(
                AuthoringIssue(
                    "qa_sql_parse_failed",
                    f"SQLGlot could not parse active QA SQL: {type(exc).__name__}",
                    qa.asset_id,
                    "sql",
                )
            )
            continue
        try:
            # Keep the authoring contract on exactly the same read-only policy
            # boundary used by runtime execution, without connecting to a DB.
            from src.nl2sql.infra.governance.query_gateway import PolicyEngine

            PolicyEngine().prepare(qa.sql)
        except Exception as exc:
            code = "illegal_join" if "join" in str(exc).lower() else "qa_preflight_failed"
            issues.append(
                AuthoringIssue(
                    code,
                    f"active QA SQL failed QueryGateway preflight: {type(exc).__name__}",
                    qa.asset_id,
                    "sql",
                )
            )
        issues.extend(_validate_query_catalog(parsed, qa.asset_id, catalog))
    return issues


def _validate_query_catalog(
    parsed: Any,
    asset_id: str,
    catalog: "RelationCatalog",
) -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    aliases: dict[str, str] = {}
    cte_names = {cte.alias_or_name for cte in parsed.find_all(exp.CTE)}
    for table in parsed.find_all(exp.Table):
        relation = table.name
        if relation in cte_names:
            aliases[table.alias_or_name] = relation
            continue
        if not catalog.has_relation(relation):
            issues.append(
                AuthoringIssue(
                    "unknown_relation",
                    f"QA SQL references an unknown relation: {relation}",
                    asset_id,
                    "sql",
                )
            )
        aliases[table.alias_or_name] = relation
    for column in parsed.find_all(exp.Column):
        table_name = str(column.table or "")
        if not table_name:
            continue
        relation: str = aliases.get(table_name) or table_name
        if catalog.has_relation(relation) and not catalog.has_column(relation, column.name):
            issues.append(
                AuthoringIssue(
                    "unknown_column",
                    f"QA SQL references an unknown column: {column.table}.{column.name}",
                    asset_id,
                    "sql",
                )
            )
    return issues


def _validate_dependency_graph(metrics: Sequence[MetricAsset]) -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    keys = {metric.metric_key for metric in metrics if metric.metric_key}
    graph: dict[str, tuple[str, ...]] = {}
    for metric in metrics:
        if not metric.metric_key or metric.status != AssetStatus.ACTIVE:
            continue
        dependencies = tuple(dict.fromkeys(metric.dependencies))
        graph[metric.metric_key] = dependencies
        for dependency in dependencies:
            if dependency not in keys:
                issues.append(
                    AuthoringIssue(
                        "unknown_metric_dependency",
                        f"metric dependency does not exist: {dependency}",
                        metric.asset_id,
                        "dependencies",
                    )
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str, path: tuple[str, ...]) -> None:
        if key in visiting:
            cycle = " -> ".join((*path, key))
            asset = next((item for item in metrics if item.metric_key == key), None)
            issues.append(
                AuthoringIssue(
                    "formula_dependency_cycle",
                    f"formula dependency cycle detected: {cycle}",
                    asset.asset_id if asset else f"metric.{key}",
                    "dependencies",
                )
            )
            return
        if key in visited or key not in graph:
            return
        visiting.add(key)
        for dependency in graph[key]:
            visit(dependency, (*path, key))
        visiting.remove(key)
        visited.add(key)

    for key in sorted(graph):
        visit(key, ())
    return issues


def _duplicate_metric_issues(metrics: Sequence[MetricAsset]) -> list[AuthoringIssue]:
    issues: list[AuthoringIssue] = []
    seen_keys: dict[str, str] = {}
    seen_aliases: dict[str, str] = {}
    for metric in metrics:
        key = _normalize_token(metric.metric_key)
        if key:
            if key in seen_keys:
                issues.append(
                    AuthoringIssue(
                        "duplicate_metric_key",
                        f"metric key duplicates {seen_keys[key]}",
                        metric.asset_id,
                        "metric_key",
                    )
                )
            else:
                seen_keys[key] = metric.asset_id
        for alias in metric.aliases:
            token = _normalize_token(alias)
            if not token or token == key:
                continue
            previous = seen_aliases.get(token) or seen_keys.get(token)
            if previous and previous != metric.asset_id:
                issues.append(
                    AuthoringIssue(
                        "duplicate_metric_alias",
                        f"metric alias duplicates {previous}",
                        metric.asset_id,
                        "aliases",
                    )
                )
            else:
                seen_aliases[token] = metric.asset_id
    return issues


@dataclass(frozen=True, slots=True)
class RelationCatalog:
    """Relation and selected-column catalog inferred from the view IR."""

    columns: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def has_relation(self, relation: str) -> bool:
        return _normalize_relation(relation) in self.columns

    def has_column(self, relation: str, column: str) -> bool:
        known = self.columns.get(_normalize_relation(relation))
        return known is None or column in known

    def with_external_columns(self, external: Mapping[str, Iterable[str]]) -> "RelationCatalog":
        merged = {key: set(value) for key, value in self.columns.items()}
        for relation, columns in external.items():
            merged[_normalize_relation(relation)] = {
                str(column) for column in columns
            }
        return RelationCatalog({key: frozenset(value) for key, value in merged.items()})


def _build_relation_catalog(views: Sequence[ViewAsset]) -> RelationCatalog:
    relation_columns: dict[str, set[str]] = {}
    view_names = {_normalize_relation(view.name) for view in views if view.name}
    declared_sources = {
        _normalize_relation(view.source_relation) for view in views if view.source_relation
    }
    declared_relations = declared_sources | view_names
    for view in views:
        source = _normalize_relation(view.source_relation)
        if source:
            relation_columns.setdefault(source, set())
        aliases = {view.source_alias: source} if view.source_alias else {}
        for join in view.joins:
            relation = _normalize_relation(join.table)
            if relation in declared_relations:
                relation_columns.setdefault(relation, set())
            if join.alias:
                aliases[join.alias] = relation
            for alias, column in _qualified_refs(join.join_condition or ""):
                relation_columns.setdefault(aliases.get(alias, ""), set()).add(column)
        output_columns: set[str] = set()
        for expression in view.columns:
            refs = _qualified_refs(expression)
            output = _column_output_name(expression)
            if output:
                output_columns.add(output)
            for alias, column in refs:
                relation = aliases.get(alias)
                if relation in declared_relations:
                    relation_columns.setdefault(relation, set()).add(column)
        relation_columns.setdefault(_normalize_relation(view.name), set()).update(output_columns)
    relation_columns = {key: value for key, value in relation_columns.items() if key}
    for view_name in view_names:
        relation_columns.setdefault(view_name, set())
    return RelationCatalog({key: frozenset(value) for key, value in relation_columns.items()})


def _domain_at(matches: Sequence[re.Match[str]], offset: int) -> str | None:
    current: str | None = None
    for match in matches:
        if match.start() <= offset:
            current = match.group(1).strip().lower()
        else:
            break
    return current


def _cut_at_next_domain_heading(block: str) -> str:
    match = re.search(r"^##\s+", block, re.MULTILINE)
    return block[: match.start()].rstrip() if match else block


def _source_at(matches: Sequence[re.Match[str]], offset: int) -> str | None:
    current: str | None = None
    for match in matches:
        if match.start() <= offset:
            current = match.group(1).strip()
        else:
            break
    return current


def _metadata(text: str, *, legacy_defaults: bool) -> dict[str, Any]:
    values: dict[str, Any] = {
        "owner": None,
        "sensitivity": None,
        "freshness": None,
        "status": AssetStatus.ACTIVE,
        "inferred": False,
    }
    for key, pattern in _METADATA_RE.items():
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip().strip("`* ")
        if key == "status":
            values[key] = _status(raw)
        elif key == "freshness":
            try:
                values[key] = int(raw)
            except ValueError:
                values[key] = None
        else:
            values[key] = raw or None
    if legacy_defaults and any(values[key] is None for key in ("owner", "sensitivity", "freshness")):
        values["owner"] = values["owner"] or DEFAULT_OWNER
        values["sensitivity"] = values["sensitivity"] or DEFAULT_SENSITIVITY
        values["freshness"] = values["freshness"] or DEFAULT_FRESHNESS_SLA_SECONDS
        values["inferred"] = True
    return values


def _document_schema_version(text: str) -> int:
    match = re.search(r"^\s*schema[_-]version\s*[:：]\s*(\d+)\s*$", text, re.I | re.M)
    return _safe_int(match.group(1), SCHEMA_VERSION) if match else SCHEMA_VERSION


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _metadata_from_mapping(raw: Mapping[str, Any], *, legacy_defaults: bool) -> dict[str, Any]:
    status = _status(raw.get("status"))
    owner = _as_optional_text(raw.get("owner"))
    sensitivity = _as_optional_text(raw.get("sensitivity"))
    freshness_raw = raw.get("freshness_sla_seconds")
    try:
        freshness = int(freshness_raw) if freshness_raw is not None else None
    except (TypeError, ValueError):
        freshness = None
    inferred = False
    if legacy_defaults and (owner is None or sensitivity is None or freshness is None):
        owner = owner or DEFAULT_OWNER
        sensitivity = sensitivity or DEFAULT_SENSITIVITY
        freshness = freshness or DEFAULT_FRESHNESS_SLA_SECONDS
        inferred = True
    return {
        "owner": owner,
        "sensitivity": sensitivity,
        "freshness": freshness,
        "status": status,
        "inferred": inferred,
    }


def _status(value: Any) -> AssetStatus:
    if isinstance(value, AssetStatus):
        return value
    raw = str(value or AssetStatus.ACTIVE).strip().lower()
    try:
        return AssetStatus(raw)
    except ValueError:
        return AssetStatus.ERROR


def _extract_metric_dependencies(text: str) -> tuple[str, ...]:
    dependencies: list[str] = []
    for line in text.splitlines():
        if ("依赖" not in line and "depend" not in line.lower()) or "源表" in line:
            continue
        candidates = _CODE_RE.findall(line)
        candidates.extend(_extract_identifier_keys(line))
        dependencies.extend(
            item for item in candidates if _looks_like_metric_key(item)
        )
    return _unique(dependencies)


def _extract_identifier_keys(text: str) -> list[str]:
    return [item for item in _IDENTIFIER_RE.findall(text) if _looks_like_metric_key(item)]


def _looks_like_metric_key(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}", value, re.I))


def _expand_metric_keys(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return []
    if "/" not in cleaned:
        return [cleaned]
    parts = [part.strip() for part in cleaned.split("/") if part.strip()]
    if len(parts) != 2:
        return [cleaned]
    left, right = parts
    if right in {"day", "month", "week", "year"}:
        prefix = left.rsplit("_", 1)[0] if left.rsplit("_", 1)[-1] in {"day", "month", "week", "year"} else left
        return [left, f"{prefix}_{right}"]
    return parts


def _extract_metric_key_spans(text: str) -> list[str]:
    return [item for item in _CODE_RE.findall(text) if _looks_like_metric_key(item)]


def _is_derived(text: str) -> bool:
    return bool(re.search(r"derived|派生|KPI|及时率|占比|识别率|合格率", text, re.I))


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _parse_csv_identifiers(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return _unique(
        item.strip().strip("`")
        for item in re.split(r"[,，、/|]", value)
        if item.strip()
    )


def _first_relation_from_sql(sql: str) -> str | None:
    try:
        table = next(sqlglot.parse_one(sql, read="postgres").find_all(exp.Table), None)
    except Exception:
        return None
    return table.name if table else None


def _domain_from_metric_key(key: str) -> str:
    match = _DOMAIN_FROM_KEY_RE.match(key)
    return match.group(1).lower() if match else "unknown"


def _domain_from_view_name(name: str) -> str:
    for token, domain in (
        ("single_fault", "single_fault"),
        ("repair", "repair_service"),
        ("installation", "installation"),
        ("fault_reporting", "complaint"),
        ("complaint", "complaint"),
        ("inspection", "inspection"),
    ):
        if token in name:
            return domain
    return "shared"


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def _normalize_token(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def _normalize_relation(value: str) -> str:
    return _normalize_token(value).replace('"', "")


def _qualified_refs(value: str) -> tuple[tuple[str, str], ...]:
    return tuple(_QUALIFIED_IDENTIFIER_RE.findall(value))


def _column_output_name(expression: str) -> str | None:
    alias_match = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", expression, re.I)
    if alias_match:
        return alias_match.group(1)
    refs = _qualified_refs(expression)
    return refs[-1][1] if refs else None


def _alias_relation(view: ViewAsset, alias: str) -> str:
    if alias == view.source_alias:
        return view.source_relation
    for join in view.joins:
        if join.alias == alias:
            return join.table
    return alias


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_optional_text(value: Any) -> str | None:
    text = _as_text(value)
    return text or None


def _metric_heading_count(metrics: Sequence[MetricAsset]) -> int:
    return len({(metric.source_line, metric.display_name) for metric in metrics})


def _sort_issues(issues: Iterable[AuthoringIssue]) -> list[AuthoringIssue]:
    return sorted(
        issues,
        key=lambda issue: (
            issue.asset_id,
            issue.path,
            issue.code,
            issue.severity.value,
            issue.message,
        ),
    )


def _asset_payload(asset: AuthoringAsset) -> dict[str, Any]:
    payload = asdict(asset)
    payload.pop("raw_text", None)
    payload.pop("raw_payload", None)
    payload.pop("source_line", None)
    payload.pop("legacy_metadata_inferred", None)
    payload["asset_type"] = asset.asset_type.value
    return _canonicalize(payload)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonicalize(item) for item in value]
    return value


__all__ = [
    "AssetStatus",
    "AssetType",
    "AuthoringCompilation",
    "AuthoringIR",
    "AuthoringIssue",
    "JoinDefinition",
    "MetricAsset",
    "QAAsset",
    "SCHEMA_VERSION",
    "ValidationReport",
    "ViewAsset",
    "compile_authoring_files",
    "compile_authoring_ir",
    "parse_ai_views",
    "parse_qa_assets",
    "parse_semantic_metrics",
    "validate_authoring_ir",
]
