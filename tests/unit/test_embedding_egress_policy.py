"""P2-S2 embedding egress: technical-secret denial on the legacy consumer."""

from __future__ import annotations

import pytest

from src.nl2sql.observability.content_policy import (
    contains_technical_secret,
    scan_embedding_input,
)
from src.nl2sql.semantic.indexer import attach_embeddings
from src.nl2sql.semantic.registry import SemanticDocument


def _document(content: str) -> SemanticDocument:
    return SemanticDocument(document_id="doc-1", content=content, metadata={})


def test_scan_embedding_input_matches_secret_shapes() -> None:
    findings = scan_embedding_input(
        ["postgresql://svc:hunter2@db:5432/app", "ordinary revenue for team A"]
    )

    assert [finding.category for finding in findings] == ["credential_dsn"]


def test_ordinary_business_embedding_input_is_not_denied() -> None:
    assert contains_technical_secret(["team A revenue", "contact 13800138000"]) is False


@pytest.mark.asyncio
async def test_attach_embeddings_degrades_instead_of_sending_a_secret() -> None:
    documents = [_document("token: token-token-token"), _document("ordinary business")]

    result, report = await attach_embeddings(documents)

    assert result == documents
    assert report["embedding_status"] == "degraded"
    assert report["embedding_error"] == "EmbeddingEgressSecretDetected"
