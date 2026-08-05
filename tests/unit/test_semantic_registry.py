"""Release and context compiler invariants."""

from pathlib import Path

import pytest

from src.nl2sql.semantic.context_compiler import ContextCompiler, Evidence, reciprocal_rank_fusion
from src.nl2sql.semantic.indexer import attach_embeddings
from src.nl2sql.semantic.registry import (
    SemanticDocument,
    SemanticRegistry,
    SemanticReleaseError,
    SemanticReleaseState,
)


def _document(document_id: str, content: str = "metric definition") -> SemanticDocument:
    return SemanticDocument(document_id=document_id, content=content, metadata={"domain": "ops"})


def _valid(_: object) -> dict[str, object]:
    return {"ok": True, "checks": ["checksum", "schema"]}


def test_failed_candidate_does_not_replace_active_release() -> None:
    registry = SemanticRegistry()
    active = registry.publish([_document("a")], change_summary="initial", validator=_valid)

    with pytest.raises(SemanticReleaseError):
        registry.publish([_document("b")], change_summary="broken", validator=lambda _: {"ok": False})

    assert registry.active_release_id == active.release_id
    assert registry.active_release() == active


def test_activation_and_rollback_keep_explicit_state_history() -> None:
    registry = SemanticRegistry()
    first = registry.publish([_document("a")], change_summary="first", validator=_valid)
    second = registry.publish([_document("b")], change_summary="second", validator=_valid)

    assert registry.get(first.release_id).state == SemanticReleaseState.RETIRED
    assert registry.active_release_id == second.release_id
    assert registry.rollback(first.release_id).release_id == first.release_id
    assert registry.active_release_id == first.release_id


def test_rrf_and_context_budget_use_only_active_release() -> None:
    registry = SemanticRegistry()
    active = registry.publish([_document("a"), _document("b")], change_summary="initial", validator=_valid)
    compiler = ContextCompiler(registry)
    lexical = [
        Evidence("a", "A" * 100, "lexical", {}),
        Evidence("outside", "should not appear", "lexical", {}),
    ]
    graph = [Evidence("b", "B" * 100, "graph", {})]

    bundle = compiler.compile(
        route="fast",
        lexical=lexical,
        vector=None,
        graph=graph,
        embedding_available=False,
    )

    assert bundle.semantic_release_id == active.release_id
    assert {item.evidence_id for item in bundle.evidence} == {"a", "b"}
    assert bundle.degraded
    assert bundle.degradation_reasons == ("embedding_unavailable",)
    assert bundle.token_cost <= 2_000
    assert reciprocal_rank_fusion({"lexical": ["a"], "vector": ["a", "b"]})[0][0] == "a"


def test_missing_active_release_never_builds_indexes_on_demand() -> None:
    bundle = ContextCompiler(SemanticRegistry()).compile(
        route="standard",
        lexical=[],
        vector=[],
        graph=[],
        embedding_available=True,
    )

    assert bundle.semantic_release_id is None
    assert bundle.degradation_reasons == ("no_active_semantic_release",)


def test_control_migration_persists_release_pointer_and_retrieval_indexes() -> None:
    root = Path(__file__).resolve().parents[2]
    base_migration = (root / "docker/migrations/control/002_semantic_registry.sql").read_text(
        encoding="utf-8"
    )
    v3_migration = (root / "docker/migrations/control/004_semantic_registry_v3.sql").read_text(
        encoding="utf-8"
    )

    assert "semantic_releases" in base_migration
    assert "semantic_documents" in base_migration
    assert "semantic_release_pointers" in base_migration
    assert "TSVECTOR" in base_migration
    assert "VECTOR" in base_migration
    assert "semantic_releases_one_active" in base_migration
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in v3_migration
    assert "semantic_release_version_seq" in v3_migration
    assert "ALTER COLUMN release_id DROP NOT NULL" in v3_migration
    assert "VALUES ('active', NULL)" in v3_migration
    for table in (
        "semantic_assets",
        "semantic_aliases",
        "semantic_edges",
        "schema_snapshots",
        "semantic_validation_issues",
        "source_freshness",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in v3_migration
    assert "normalized_alias gin_trgm_ops" in v3_migration


def test_durable_publisher_uses_sequence_and_locks_pointer_row() -> None:
    root = Path(__file__).resolve().parents[2]
    registry = (root / "src/nl2sql/semantic/registry.py").read_text(encoding="utf-8")

    assert "SELECT COALESCE(MAX(version), 0) + 1" not in registry
    assert "SELECT nextval('semantic_release_version_seq')" in registry
    assert "FROM semantic_release_pointers" in registry
    assert "FOR UPDATE" in registry


def test_control_database_uses_a_pgvector_enabled_image() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker/compose.dev.yml").read_text(encoding="utf-8")

    assert "control-postgres:\n    image: pgvector/pgvector:pg17" in compose


def test_indexer_is_a_one_shot_ops_service() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker/compose.dev.yml").read_text(encoding="utf-8")
    indexer = (root / "src/nl2sql/semantic/indexer.py").read_text(encoding="utf-8")

    assert "indexer:" in compose
    assert 'profiles: ["ops"]' in compose
    assert "CONTROL_DATABASE_URL_FILE" in compose
    assert "src.nl2sql.semantic.indexer" in compose
    assert "API lifespan" in indexer


@pytest.mark.asyncio
async def test_indexer_keeps_lexical_candidate_when_embedding_provider_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingEmbedder:
        def __init__(self, **_: object) -> None:
            pass

        async def aembed_documents(self, _: list[str]) -> list[list[float]]:
            raise TimeoutError("embedding provider unavailable")

    monkeypatch.setattr("src.nl2sql.semantic.indexer.OpenAIEmbeddings", FailingEmbedder)
    documents, report = await attach_embeddings([_document("a")])

    assert documents[0].embedding is None
    assert report["embedding_status"] == "degraded"
    assert report["embedding_error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_indexer_attaches_vectors_when_embedding_provider_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    class WorkingEmbedder:
        def __init__(self, **_: object) -> None:
            pass

        async def aembed_documents(self, _: list[str]) -> list[list[float]]:
            return [[0.1, 0.2]]

    monkeypatch.setattr("src.nl2sql.semantic.indexer.OpenAIEmbeddings", WorkingEmbedder)
    documents, report = await attach_embeddings([_document("a")])

    assert documents[0].embedding == (0.1, 0.2)
    assert report["embedding_status"] == "ready"
