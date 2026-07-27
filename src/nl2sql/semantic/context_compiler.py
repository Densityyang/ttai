"""Release-scoped retrieval fusion, evidence budgets and confidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Iterable, Literal

from src.nl2sql.semantic.registry import SemanticRegistry

RRF_K = 60
SOURCE_WEIGHT = {"lexical": 0.30, "vector": 0.45, "graph": 0.25}
Route = Literal["fast", "standard", "deep"]
_BUDGETS: dict[Route, tuple[int, int]] = {
    "fast": (2_000, 4),
    "standard": (6_000, 8),
    "deep": (12_000, 16),
}


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


def _unique_release_scoped(items: Iterable[Evidence], release_ids: set[str]) -> list[Evidence]:
    unique: dict[str, Evidence] = {}
    for item in items:
        if item.evidence_id in release_ids:
            unique.setdefault(item.evidence_id, item)
    return list(unique.values())


def _estimate_tokens(content: str) -> int:
    return ceil(len(content) / 4)
