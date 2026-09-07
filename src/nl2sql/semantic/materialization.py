"""Deterministically materialize validated authoring IR into typed release rows."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from src.nl2sql.semantic.authoring import (
    AssetStatus,
    AuthoringIR,
    MetricAsset,
    QAAsset,
    ValidationReport,
    ViewAsset,
)
from src.nl2sql.semantic.registry import (
    SemanticAliasRecord,
    SemanticAssetRecord,
    SemanticDocument,
    SemanticEdgeRecord,
    SemanticReleaseCandidate,
    SemanticReleaseError,
    SemanticValidationIssueRecord,
)

PARSER_VERSION = "semantic-authoring-v3"


@dataclass(slots=True)
class _RelationAccumulator:
    names: set[str] = field(default_factory=set)
    statuses: set[AssetStatus] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    owners: set[str] = field(default_factory=set)
    sensitivities: set[str] = field(default_factory=set)


def materialize_authoring_ir(
    ir: AuthoringIR,
    report: ValidationReport,
) -> SemanticReleaseCandidate:
    """Build a complete, immutable database candidate without performing I/O."""
    _validate_alignment(ir, report)
    payloads = {
        str(payload["asset_id"]): dict(payload)
        for payload in ir.to_payload()["assets"]
    }
    authoring_assets: list[SemanticAssetRecord] = []
    relation_accumulators: dict[str, _RelationAccumulator] = {}
    status_by_asset_id: dict[str, AssetStatus] = {}
    owner_by_asset_id: dict[str, str] = {}

    for asset in sorted(ir.assets, key=lambda item: item.asset_id):
        status = report.asset_statuses[asset.asset_id]
        owner = _governance_value(asset.owner, fallback="unassigned")
        sensitivity = _governance_value(asset.sensitivity, fallback="unclassified")
        if report.ok and status is AssetStatus.ACTIVE and (
            owner == "unassigned" or sensitivity == "unclassified"
        ):
            raise SemanticReleaseError(
                f"validated active asset is missing governance metadata: {asset.asset_id}"
            )
        payload = payloads[asset.asset_id]
        payload["status"] = status.value
        record = SemanticAssetRecord(
            asset_id=asset.asset_id,
            asset_type=asset.asset_type.value,
            status=status.value,
            domain=_governance_value(asset.domain, fallback="unknown"),
            owner=owner,
            sensitivity=sensitivity,
            content=_asset_content(asset),
            payload=payload,
        )
        authoring_assets.append(record)
        status_by_asset_id[record.asset_id] = status
        owner_by_asset_id[record.asset_id] = owner
        if isinstance(asset, MetricAsset) and asset.source_relation:
            _register_relation(relation_accumulators, asset.source_relation, record, status)
        if isinstance(asset, ViewAsset):
            _register_relation(relation_accumulators, asset.source_relation, record, status)
            for join in asset.joins:
                _register_relation(relation_accumulators, join.table, record, status)

    relation_assets, relation_ids, relation_names = _materialize_relations(relation_accumulators)
    for relation_asset in relation_assets:
        status_by_asset_id[relation_asset.asset_id] = AssetStatus(relation_asset.status)

    assets = tuple(sorted((*authoring_assets, *relation_assets), key=lambda item: item.asset_id))
    aliases = _materialize_aliases(ir, relation_assets, relation_names)
    edges = _materialize_edges(ir, report, relation_ids, status_by_asset_id)
    validation_issues = tuple(
        SemanticValidationIssueRecord(
            asset_id=issue.asset_id or None,
            code=issue.code,
            severity=issue.severity.value,
            message=issue.message,
            path=issue.path,
            owner=owner_by_asset_id.get(issue.asset_id, "unassigned"),
        )
        for issue in report.issues
    )
    documents = tuple(
        SemanticDocument(
            document_id=asset.asset_id,
            content=asset.content,
            metadata={
                "asset_type": asset.asset_type,
                "domain": asset.domain,
                "owner": asset.owner,
                "sensitivity": asset.sensitivity,
                "status": asset.status,
                **({"execution_contract": json.dumps(
                    asset.payload["execution_contract"], sort_keys=True, separators=(",", ":")
                )} if asset.payload.get("execution_contract") is not None else {}),
            },
        )
        for asset in assets
    )
    checksum = _release_checksum(
        ir=ir,
        assets=assets,
        aliases=aliases,
        edges=edges,
        validation_issues=validation_issues,
    )
    return SemanticReleaseCandidate(
        checksum=checksum,
        documents=documents,
        validation_report=report.to_dict(),
        assets=assets,
        aliases=aliases,
        edges=edges,
        validation_issues=validation_issues,
        schema_version=ir.schema_version,
        parser_version=PARSER_VERSION,
    )


def normalize_semantic_alias(value: str) -> str:
    """Return the stable exact-match key used by PostgreSQL alias rows."""
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def _validate_alignment(ir: AuthoringIR, report: ValidationReport) -> None:
    if ir.schema_version != report.schema_version:
        raise SemanticReleaseError("authoring IR and validation report schema versions differ")
    if ir.checksum != report.checksum:
        raise SemanticReleaseError("authoring IR and validation report checksums differ")
    ordered_asset_ids = [asset.asset_id for asset in ir.assets]
    if any(not asset_id.strip() for asset_id in ordered_asset_ids):
        raise SemanticReleaseError("authoring asset ids must not be empty")
    asset_ids = set(ordered_asset_ids)
    if len(ordered_asset_ids) != len(asset_ids):
        raise SemanticReleaseError("authoring asset ids must be unique across all asset types")
    if asset_ids != set(report.asset_statuses) or asset_ids != set(report.asset_domains):
        raise SemanticReleaseError("validation report does not cover every authoring asset")
    if any(report.asset_domains[asset.asset_id] != asset.domain for asset in ir.assets):
        raise SemanticReleaseError("authoring IR and validation report domains differ")


def _register_relation(
    accumulators: dict[str, _RelationAccumulator],
    relation_name: str,
    source: SemanticAssetRecord,
    status: AssetStatus,
) -> None:
    key = _normalize_relation(relation_name)
    if not key:
        raise SemanticReleaseError(f"semantic asset has an empty relation: {source.asset_id}")
    accumulator = accumulators.setdefault(key, _RelationAccumulator())
    accumulator.names.add(relation_name.strip())
    accumulator.statuses.add(status)
    accumulator.domains.add(source.domain)
    accumulator.owners.add(source.owner)
    accumulator.sensitivities.add(source.sensitivity)


def _materialize_relations(
    accumulators: dict[str, _RelationAccumulator],
) -> tuple[tuple[SemanticAssetRecord, ...], dict[str, str], dict[str, tuple[str, ...]]]:
    records: list[SemanticAssetRecord] = []
    relation_ids: dict[str, str] = {}
    relation_names: dict[str, tuple[str, ...]] = {}
    for key, accumulator in sorted(accumulators.items()):
        asset_id = _relation_asset_id(key)
        names = tuple(sorted(accumulator.names, key=lambda value: value.casefold()))
        display_name = names[0]
        status = _aggregate_status(accumulator.statuses)
        domain = _single_or(accumulator.domains, "shared")
        owner = _single_or(accumulator.owners, "semantic-registry")
        sensitivity = _single_or(accumulator.sensitivities, "mixed")
        records.append(
            SemanticAssetRecord(
                asset_id=asset_id,
                asset_type="relation",
                status=status.value,
                domain=domain,
                owner=owner,
                sensitivity=sensitivity,
                content=f"relation {display_name}",
                payload={
                    "asset_id": asset_id,
                    "asset_type": "relation",
                    "domains": sorted(accumulator.domains),
                    "relation_name": display_name,
                    "schema_version": 3,
                    "status": status.value,
                },
            )
        )
        relation_ids[key] = asset_id
        relation_names[asset_id] = names
    return tuple(records), relation_ids, relation_names


def _materialize_aliases(
    ir: AuthoringIR,
    relation_assets: tuple[SemanticAssetRecord, ...],
    relation_names: dict[str, tuple[str, ...]],
) -> tuple[SemanticAliasRecord, ...]:
    aliases: dict[str, SemanticAliasRecord] = {}
    for asset in sorted(ir.assets, key=lambda item: item.asset_id):
        for alias in _asset_aliases(asset):
            _add_alias(aliases, asset.asset_id, alias)
    for relation_asset in relation_assets:
        for relation_name in relation_names[relation_asset.asset_id]:
            _add_alias(aliases, relation_asset.asset_id, relation_name)
            _add_alias(aliases, relation_asset.asset_id, relation_name.rsplit(".", maxsplit=1)[-1])
    return tuple(aliases[key] for key in sorted(aliases))


def _add_alias(
    aliases: dict[str, SemanticAliasRecord],
    asset_id: str,
    alias: str,
) -> None:
    normalized = normalize_semantic_alias(alias)
    if not normalized:
        return
    existing = aliases.get(normalized)
    if existing is not None:
        if existing.asset_id != asset_id:
            raise SemanticReleaseError(
                f"normalized semantic alias maps to multiple assets: {normalized}"
            )
        return
    aliases[normalized] = SemanticAliasRecord(
        asset_id=asset_id,
        alias=alias.strip(),
        normalized_alias=normalized,
        language=_alias_language(alias),
    )


def _materialize_edges(
    ir: AuthoringIR,
    report: ValidationReport,
    relation_ids: dict[str, str],
    status_by_asset_id: dict[str, AssetStatus],
) -> tuple[SemanticEdgeRecord, ...]:
    edges: dict[str, SemanticEdgeRecord] = {}
    metrics_by_key = {metric.metric_key: metric for metric in ir.metrics}
    for metric in sorted(ir.metrics, key=lambda item: item.asset_id):
        source_status = report.asset_statuses[metric.asset_id]
        for dependency_key in sorted(set(metric.dependencies)):
            dependency = metrics_by_key.get(dependency_key)
            if dependency is None:
                raise SemanticReleaseError(
                    f"metric dependency is missing from the materialized release: {dependency_key}"
                )
            _add_edge(
                edges,
                source_asset_id=metric.asset_id,
                target_asset_id=dependency.asset_id,
                edge_type="metric_dependency",
                status=_edge_status(source_status, report.asset_statuses[dependency.asset_id]),
                payload={
                    "source_metric_key": metric.metric_key,
                    "target_metric_key": dependency.metric_key,
                },
            )
        if metric.source_relation:
            _add_lineage_edge(
                edges,
                source_asset_id=metric.asset_id,
                relation_name=metric.source_relation,
                source_status=source_status,
                relation_ids=relation_ids,
                status_by_asset_id=status_by_asset_id,
            )

    for qa in sorted(ir.qas, key=lambda item: item.asset_id):
        for metric_key in sorted(set(qa.metric_keys)):
            metric = metrics_by_key.get(metric_key)
            if metric is None:
                raise SemanticReleaseError(
                    f"QA metric reference is missing from the materialized release: {metric_key}"
                )
            _add_edge(
                edges,
                source_asset_id=qa.asset_id,
                target_asset_id=metric.asset_id,
                edge_type="lineage",
                status=_edge_status(
                    report.asset_statuses[qa.asset_id],
                    report.asset_statuses[metric.asset_id],
                ),
                payload={"metric_key": metric_key, "relationship": "qa_metric"},
            )

    for view in sorted(ir.views, key=lambda item: item.asset_id):
        view_status = report.asset_statuses[view.asset_id]
        _add_lineage_edge(
            edges,
            source_asset_id=view.asset_id,
            relation_name=view.source_relation,
            source_status=view_status,
            relation_ids=relation_ids,
            status_by_asset_id=status_by_asset_id,
        )
        source_relation_id = _required_relation_id(relation_ids, view.source_relation)
        for join in view.joins:
            target_relation_id = _required_relation_id(relation_ids, join.table)
            if source_relation_id == target_relation_id:
                raise SemanticReleaseError(
                    f"self joins require distinct relation identities: {view.asset_id}"
                )
            _add_edge(
                edges,
                source_asset_id=source_relation_id,
                target_asset_id=target_relation_id,
                edge_type="approved_join",
                status=_edge_status(
                    view_status,
                    status_by_asset_id[source_relation_id],
                    status_by_asset_id[target_relation_id],
                ),
                payload={
                    "join_condition": join.join_condition,
                    "join_type": join.join_type,
                    "source_relation": view.source_relation,
                    "target_relation": join.table,
                    "using": list(join.using),
                    "view_asset_id": view.asset_id,
                },
            )
    return tuple(edges[key] for key in sorted(edges))


def _add_lineage_edge(
    edges: dict[str, SemanticEdgeRecord],
    *,
    source_asset_id: str,
    relation_name: str,
    source_status: AssetStatus,
    relation_ids: dict[str, str],
    status_by_asset_id: dict[str, AssetStatus],
) -> None:
    relation_id = _required_relation_id(relation_ids, relation_name)
    _add_edge(
        edges,
        source_asset_id=source_asset_id,
        target_asset_id=relation_id,
        edge_type="lineage",
        status=_edge_status(source_status, status_by_asset_id[relation_id]),
        payload={"relation_name": relation_name},
    )


def _add_edge(
    edges: dict[str, SemanticEdgeRecord],
    *,
    source_asset_id: str,
    target_asset_id: str,
    edge_type: str,
    status: str,
    payload: dict[str, Any],
) -> None:
    identity = {
        "edge_type": edge_type,
        "payload": payload,
        "source_asset_id": source_asset_id,
        "target_asset_id": target_asset_id,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    edge_id = f"{edge_type}.{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"
    edges[edge_id] = SemanticEdgeRecord(
        edge_id=edge_id,
        source_asset_id=source_asset_id,
        target_asset_id=target_asset_id,
        edge_type=edge_type,
        status=status,
        payload=payload,
    )


def _release_checksum(
    *,
    ir: AuthoringIR,
    assets: tuple[SemanticAssetRecord, ...],
    aliases: tuple[SemanticAliasRecord, ...],
    edges: tuple[SemanticEdgeRecord, ...],
    validation_issues: tuple[SemanticValidationIssueRecord, ...],
) -> str:
    contract = {
        "aliases": [asdict(alias) for alias in aliases],
        "assets": [asdict(asset) for asset in assets],
        "authoring_checksum": ir.checksum,
        "edges": [asdict(edge) for edge in edges],
        "parser_version": PARSER_VERSION,
        "schema_version": ir.schema_version,
        "validation_issues": [asdict(issue) for issue in validation_issues],
    }
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _asset_content(asset: MetricAsset | QAAsset | ViewAsset) -> str:
    if isinstance(asset, MetricAsset):
        values: Iterable[str | None] = (
            asset.metric_key,
            asset.display_name,
            *asset.aliases,
            asset.source_relation,
            asset.formula,
        )
    elif isinstance(asset, QAAsset):
        values = (asset.case_id, asset.question, asset.answer, asset.sql, *asset.metric_keys)
    else:
        values = (asset.name, asset.source_relation, *asset.columns)
    content = " ".join(_unique_text(values))
    if not content:
        raise SemanticReleaseError(f"semantic asset has no searchable content: {asset.asset_id}")
    return content


def _asset_aliases(asset: MetricAsset | QAAsset | ViewAsset) -> tuple[str, ...]:
    if isinstance(asset, MetricAsset):
        return _unique_text((asset.metric_key, asset.display_name, *asset.aliases))
    if isinstance(asset, QAAsset):
        return _unique_text((asset.case_id, asset.question))
    return _unique_text((asset.name,))


def _unique_text(values: Iterable[str | None]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip() if value is not None else ""
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def _required_relation_id(relation_ids: dict[str, str], relation_name: str) -> str:
    key = _normalize_relation(relation_name)
    try:
        return relation_ids[key]
    except KeyError as exc:
        raise SemanticReleaseError(
            f"relation is missing from the materialized release: {relation_name}"
        ) from exc


def _normalize_relation(value: str) -> str:
    return normalize_semantic_alias(value).replace('"', "")


def _relation_asset_id(normalized_relation: str) -> str:
    digest = hashlib.sha256(normalized_relation.encode("utf-8")).hexdigest()[:24]
    return f"relation.{digest}"


def _aggregate_status(statuses: set[AssetStatus]) -> AssetStatus:
    if AssetStatus.ERROR in statuses:
        return AssetStatus.ERROR
    if AssetStatus.ACTIVE in statuses:
        return AssetStatus.ACTIVE
    return AssetStatus.RETIRED


def _edge_status(*statuses: AssetStatus) -> str:
    if AssetStatus.ERROR in statuses:
        return "rejected"
    if AssetStatus.RETIRED in statuses:
        return "retired"
    return "approved"


def _single_or(values: set[str], fallback: str) -> str:
    cleaned = {value.strip() for value in values if value.strip()}
    return next(iter(cleaned)) if len(cleaned) == 1 else fallback


def _governance_value(value: str | None, *, fallback: str) -> str:
    cleaned = value.strip() if value else ""
    return cleaned or fallback


def _alias_language(alias: str) -> str:
    if re.search(r"[\u3400-\u9fff]", alias):
        return "zh"
    if alias.isascii():
        return "en"
    return "und"


__all__ = ["PARSER_VERSION", "materialize_authoring_ir", "normalize_semantic_alias"]
