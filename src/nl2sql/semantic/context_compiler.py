"""Release-scoped retrieval fusion, evidence budgets and confidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Iterable, Literal, Protocol
from uuid import UUID

from src.nl2sql.contracts import (
    ContextBundle as ContractContextBundle,
)
from src.nl2sql.contracts import (
    RequestIdentity,
)
from src.nl2sql.semantic.registry import SemanticRegistry, SemanticRelease

RRF_K = 60
SOURCE_WEIGHT = {"lexical": 0.30, "vector": 0.45, "graph": 0.25}
Route = Literal["fast", "standard", "deep"]
_BUDGETS: dict[Route, tuple[int, int]] = {
    "fast": (2_000, 6),
    "standard": (6_000, 12),
    "deep": (10_000, 20),
}
_DOMAIN_LIMIT = 8
_RELATION_LIMITS: dict[Route, int] = {"fast": 2, "standard": 5, "deep": 8}


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    content: str
    source: Literal["lexical", "vector", "graph"]
    metadata: dict[str, str]


@dataclass(frozen=True)
class ContextBundle:
    semantic_release_id: str | None
    evidence: tuple[Evidence, ...]
    token_cost: int
    confidence: float
    degraded: bool
    degradation_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PolicyScopedEvidence:
    """Evidence already filtered for the request identity before retrieval."""

    lexical: tuple[Evidence, ...] = ()
    vector: tuple[Evidence, ...] | None = None
    graph: tuple[Evidence, ...] = ()
    embedding_available: bool = False
    resolution_status: Literal["resolved", "ambiguous", "incomplete", "conflict"] = (
        "incomplete"
    )
    unresolved_slots: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    degradation_flags: tuple[str, ...] = ()


class PolicyScopedEvidenceProvider(Protocol):
    async def retrieve_permitted(
        self,
        *,
        question: str,
        identity: RequestIdentity,
    ) -> PolicyScopedEvidence: ...


class ContextCompilationError(RuntimeError):
    pass


def reciprocal_rank_fusion(ranked: dict[str, list[str]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for source, document_ids in ranked.items():
        weight = SOURCE_WEIGHT.get(source, 0.0)
        for rank, document_id in enumerate(document_ids, start=1):
            scores[document_id] += weight / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class ContextCompiler:
    def __init__(self, registry: SemanticRegistry) -> None:
        self._registry = registry

    def compile(
        self,
        *,
        route: Route,
        lexical: Iterable[Evidence],
        vector: Iterable[Evidence] | None,
        graph: Iterable[Evidence],
        embedding_available: bool,
    ) -> ContextBundle:
        active = self._registry.active_release()
        if active is None:
            return ContextBundle(
                semantic_release_id=None,
                evidence=(),
                token_cost=0,
                confidence=0.0,
                degraded=True,
                degradation_reasons=("no_active_semantic_release",),
            )

        release_ids = {document.document_id for document in active.documents}
        sources = {
            "lexical": _unique_release_scoped(lexical, release_ids),
            "graph": _unique_release_scoped(graph, release_ids),
        }
        reasons: list[str] = []
        if embedding_available and vector is not None:
            sources["vector"] = _unique_release_scoped(vector, release_ids)
        else:
            reasons.append("embedding_unavailable")

        lookup = {item.evidence_id: item for items in sources.values() for item in items}
        ranked = {source: [item.evidence_id for item in items] for source, items in sources.items()}
        _, evidence_limit = _BUDGETS[route]
        token_limit, _ = _BUDGETS[route]
        selected: list[Evidence] = []
        tokens = 0
        for evidence_id, _ in reciprocal_rank_fusion(ranked):
            item = lookup[evidence_id]
            item_tokens = _estimate_tokens(item.content)
            if len(selected) >= evidence_limit or tokens + item_tokens > token_limit:
                continue
            selected.append(item)
            tokens += item_tokens

        source_coverage = sum(bool(items) for items in sources.values()) / 3
        agreement = min(1.0, len(selected) / max(1, evidence_limit))
        confidence = round(0.35 * source_coverage + 0.20 * agreement, 4)
        if reasons:
            confidence = round(confidence * 0.75, 4)
        return ContextBundle(
            semantic_release_id=active.release_id,
            evidence=tuple(selected),
            token_cost=tokens,
            confidence=confidence,
            degraded=bool(reasons),
            degradation_reasons=tuple(reasons),
        )

    def release(self, release_id: str) -> SemanticRelease:
        """Return the exact release used by a compiled bundle."""

        return self._registry.get(release_id)


class SemanticContextResolver:
    """Adapt the release-scoped Context Compiler to the orchestration contract."""

    def __init__(
        self,
        *,
        compiler: ContextCompiler,
        evidence_provider: PolicyScopedEvidenceProvider,
    ) -> None:
        self._compiler = compiler
        self._evidence_provider = evidence_provider

    async def resolve(
        self,
        *,
        question: str,
        identity: RequestIdentity,
        route_hint: Route,
    ) -> ContractContextBundle:
        scoped = await self._evidence_provider.retrieve_permitted(
            question=question,
            identity=identity,
        )
        compiled = self._compiler.compile(
            route=route_hint,
            lexical=scoped.lexical,
            vector=scoped.vector,
            graph=scoped.graph,
            embedding_available=scoped.embedding_available,
        )
        if compiled.semantic_release_id is None:
            raise ContextCompilationError("no_active_semantic_release")
        release = self._compiler.release(compiled.semantic_release_id)
        if release.schema_snapshot_id is None:
            raise ContextCompilationError("semantic_release_schema_snapshot_unbound")
        if not compiled.evidence:
            raise ContextCompilationError("semantic_context_empty")

        domains = sorted(
            {
                item.metadata.get("domain", "").strip()
                for item in compiled.evidence
                if item.metadata.get("domain", "").strip()
            }
        )
        if not domains:
            raise ContextCompilationError("semantic_context_domain_missing")

        relation_ids = sorted(
            {
                relation_id
                for item in compiled.evidence
                if (relation_id := _relation_id(item)) is not None
            }
        )
        edge_ids = sorted(
            {
                edge_id
                for item in compiled.evidence
                if (edge_id := _edge_id(item)) is not None
            }
        )
        relation_limit = _RELATION_LIMITS[route_hint]
        degradation = list(
            dict.fromkeys((*scoped.degradation_flags, *compiled.degradation_reasons))
        )
        if len(domains) > _DOMAIN_LIMIT:
            domains = domains[:_DOMAIN_LIMIT]
            degradation.append("context_domain_budget_applied")
        if len(relation_ids) > relation_limit:
            relation_ids = relation_ids[:relation_limit]
            degradation.append("context_relation_budget_applied")
        if len(edge_ids) > relation_limit:
            edge_ids = edge_ids[:relation_limit]
            degradation.append("context_edge_budget_applied")

        resolution_status = scoped.resolution_status
        if scoped.unresolved_slots and resolution_status == "resolved":
            resolution_status = "incomplete"
        if scoped.conflict_ids:
            resolution_status = "conflict"
        return ContractContextBundle(
            semantic_release_id=UUID(compiled.semantic_release_id),
            schema_snapshot_id=UUID(release.schema_snapshot_id),
            domains=tuple(domains),
            asset_ids=tuple(item.evidence_id for item in compiled.evidence),
            approved_relation_ids=tuple(relation_ids),
            approved_edge_ids=tuple(edge_ids),
            resolution_status=resolution_status,
            unresolved_slots=scoped.unresolved_slots,
            conflict_ids=scoped.conflict_ids,
            degradation_flags=tuple(dict.fromkeys(degradation)),
            token_cost=compiled.token_cost,
            evidence_count=len(compiled.evidence),
        )


def _unique_release_scoped(items: Iterable[Evidence], release_ids: set[str]) -> list[Evidence]:
    unique: dict[str, Evidence] = {}
    for item in items:
        if item.evidence_id in release_ids:
            unique.setdefault(item.evidence_id, item)
    return list(unique.values())


def _estimate_tokens(content: str) -> int:
    return ceil(len(content) / 4)


def _relation_id(evidence: Evidence) -> str | None:
    relation_id = evidence.metadata.get("relation_id", "").strip()
    if relation_id:
        return relation_id
    if evidence.metadata.get("asset_type") == "relation":
        return evidence.evidence_id
    return None


def _edge_id(evidence: Evidence) -> str | None:
    edge_id = evidence.metadata.get("edge_id", "").strip()
    if edge_id:
        return edge_id
    if evidence.metadata.get("asset_type") == "join":
        return evidence.evidence_id
    return None
