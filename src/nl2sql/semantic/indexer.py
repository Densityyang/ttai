"""One-shot semantic release indexer; never invoked by API lifespan."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

from langchain_openai import OpenAIEmbeddings

from src.core.settings import ROOT_DIR, get_settings
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.semantic_rag import parse_semantic_markdown
from src.nl2sql.semantic.registry import (
    ControlSemanticReleasePublisher,
    SemanticDocument,
    SemanticRegistry,
    SemanticRelease,
)


def build_documents(path: Path) -> list[SemanticDocument]:
    chunks = parse_semantic_markdown(path)
    documents: list[SemanticDocument] = []
    for index, chunk in enumerate(chunks, start=1):
        content = chunk["content"]
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        documents.append(
            SemanticDocument(
                document_id=f"sem-{index}-{checksum[:16]}",
                content=content,
                metadata={"title": chunk["title"], "domain": chunk["domain"], "checksum": checksum},
            )
        )
    return documents


def validate_release(release: SemanticRelease) -> dict[str, object]:
    document_ids = [document.document_id for document in release.documents]
    return {
        "ok": bool(document_ids) and len(document_ids) == len(set(document_ids)),
        "document_count": len(document_ids),
        "checksum": release.checksum,
        "checks": ["non_empty", "unique_document_ids", "checksum"],
    }


async def attach_embeddings(
    documents: list[SemanticDocument],
) -> tuple[list[SemanticDocument], dict[str, object]]:
    """Build candidate embeddings without making the indexer unavailable on provider failure."""
    config = get_agent_config()
    embedder = OpenAIEmbeddings(
        api_key=config.embedding_api_key,
        base_url=config.embedding_base_url,
        model=config.embedding_model,
        check_embedding_ctx_length=False,
    )
    try:
        vectors = await embedder.aembed_documents([document.content for document in documents])
        if len(vectors) != len(documents) or any(not vector for vector in vectors):
            raise RuntimeError("embedding provider returned incomplete vectors")
        embedded = [
            replace(document, embedding=tuple(float(value) for value in vector))
            for document, vector in zip(documents, vectors, strict=True)
        ]
        return embedded, {"embedding_status": "ready", "embedding_model": config.embedding_model}
    except Exception as exc:
        return documents, {"embedding_status": "degraded", "embedding_error": type(exc).__name__}


async def run_indexer(change_summary: str) -> SemanticRelease:
    settings = get_settings()
    if not settings.control_database_url:
        raise RuntimeError("CONTROL_DATABASE_URL is required for the semantic indexer")
    config = get_agent_config()
    source = Path(config.rag_semantic_file_path)
    if not source.is_absolute():
        source = (ROOT_DIR / source).resolve()
    documents = build_documents(source)
    documents, embedding_report = await attach_embeddings(documents)
    publisher = ControlSemanticReleasePublisher(settings.control_database_url)
    try:
        preview = SemanticRegistry().create_draft(documents, change_summary=change_summary)
        report = validate_release(preview)
        report.update(embedding_report)
        return await publisher.publish(documents, change_summary=change_summary, validation_report=report)
    finally:
        await publisher.close()


async def rollback_index(release_id: str) -> SemanticRelease:
    settings = get_settings()
    if not settings.control_database_url:
        raise RuntimeError("CONTROL_DATABASE_URL is required for semantic rollback")
    publisher = ControlSemanticReleasePublisher(settings.control_database_url)
    try:
        return await publisher.rollback(release_id)
    finally:
        await publisher.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and publish a semantic release")
    parser.add_argument("--change-summary", default="semantic release indexer")
    parser.add_argument("--rollback-release", default=None)
    args = parser.parse_args()
    if args.rollback_release:
        release = asyncio.run(rollback_index(args.rollback_release))
        print(f"rolled back semantic release {release.release_id} version={release.version}")
    else:
        release = asyncio.run(run_indexer(args.change_summary))
        print(f"published semantic release {release.release_id} version={release.version}")


if __name__ == "__main__":
    main()
