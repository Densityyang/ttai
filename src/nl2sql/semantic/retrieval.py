"""Active-release lexical retrieval for API paths."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_openai import OpenAIEmbeddings

from src.core.settings import get_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.graph_rag import expand_with_graph_rag, get_schema_relation_graph
from src.nl2sql.semantic.context_compiler import reciprocal_rank_fusion
from src.nl2sql.semantic.registry import ControlSemanticReleasePublisher, SemanticDocument


@dataclass(frozen=True)
class ActiveSemanticRetrieval:
    release_id: str | None
    documents: tuple[SemanticDocument, ...]
    degraded: bool
    reason: str | None = None
    graph_hints: tuple[str, ...] = ()


async def retrieve_active_semantic(query: str, *, limit: int = 3) -> ActiveSemanticRetrieval:
    """Read only the active control-DB release; never construct indexes on demand."""
    settings = get_settings()
    if not settings.control_database_url:
        return ActiveSemanticRetrieval(None, (), True, "control_database_unconfigured")
    publisher = ControlSemanticReleasePublisher(settings.control_database_url)
    try:
        active = await publisher.read_active()
        if active is None:
            return ActiveSemanticRetrieval(None, (), True, "no_active_semantic_release")
        lexical = await publisher.search_lexical(query, limit=limit)
        vector: list[SemanticDocument] = []
        degradation_reasons: list[str] = []
        try:
            config = get_agent_config()
            embedder = OpenAIEmbeddings(
                api_key=config.embedding_api_key,
                base_url=config.embedding_base_url,
                model=config.embedding_model,
                check_embedding_ctx_length=False,
            )
            query_embedding = await embedder.aembed_query(query)
            vector = await publisher.search_vector(query_embedding, limit=limit)
        except Exception:
            degradation_reasons.append("embedding_unavailable")

        documents = _fuse_documents(lexical, vector, limit=limit)
        graph_hints = _graph_hints(query)
        reason = ",".join(degradation_reasons) or None
        return ActiveSemanticRetrieval(
            active.release_id,
            tuple(documents),
            bool(degradation_reasons),
            reason,
            graph_hints,
        )
    except Exception:
        return ActiveSemanticRetrieval(None, (), True, "semantic_release_unavailable")
    finally:
        await publisher.close()


def _fuse_documents(
    lexical: list[SemanticDocument], vector: list[SemanticDocument], *, limit: int
) -> list[SemanticDocument]:
    ranked = {
        "lexical": [document.document_id for document in lexical],
        "vector": [document.document_id for document in vector],
    }
    documents = {document.document_id: document for document in [*lexical, *vector]}
    return [documents[document_id] for document_id, _ in reciprocal_rank_fusion(ranked)[:limit]]


def _graph_hints(query: str) -> tuple[str, ...]:
    """Add read-only GraphRAG relationship hints; graph failure must not block retrieval."""
    try:
        graph = get_schema_relation_graph()
        seeds = graph.query_by_keywords(query.split())
        expansion = expand_with_graph_rag(seeds)
        return tuple(expansion["join_hints"])
    except Exception:
        return ()
