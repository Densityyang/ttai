"""Integrity-gate tests for the on-disk FAISS index directories.

Both retrievers deserialize index.pkl through
FAISS.load_local(allow_dangerous_deserialization=True), so the SHA-256 manifest
written by sync() must be verified before any load.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from src.nl2sql.infra.store import qa_rag, semantic_rag
from src.nl2sql.infra.store.index_integrity import (
    CHECKSUM_MANIFEST_NAME,
    IndexIntegrityError,
    checksum_manifest_matches,
    verify_checksum_manifest,
)

_Builder = Callable[..., Any]


class _Embeddings:
    """Stand-in for OpenAIEmbeddings; integrity checks need no HTTP client."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeVectorstore:
    def save_local(self, *, folder_path: str) -> None:
        directory = Path(folder_path)
        (directory / "index.faiss").write_bytes(b"fake-faiss-index")
        (directory / "index.pkl").write_bytes(b"fake-pickle-docstore")


def _fake_faiss_class(loads: list[Path]) -> type[Any]:
    """Build a FAISS stand-in that records every successful load attempt."""

    class _FakeFAISS:
        @classmethod
        def load_local(
            cls,
            *,
            folder_path: str,
            embeddings: Any,
            allow_dangerous_deserialization: bool,
        ) -> Any:
            assert allow_dangerous_deserialization is True
            assert embeddings is not None
            loads.append(Path(folder_path))
            return object()

        @classmethod
        def from_documents(cls, *, documents: list[Any], embedding: Any) -> _FakeVectorstore:
            assert documents
            assert embedding is not None
            return _FakeVectorstore()

    return _FakeFAISS


def _build_semantic(source_file: Path, index_dir: Path, *, load_existing_index: bool) -> Any:
    return semantic_rag.SemanticRetriever(
        semantic_file_path=str(source_file),
        faiss_index_path=str(index_dir),
        embedding_api_key=SecretStr("test-embedding-key"),
        embedding_base_url="http://localhost/v1",
        embedding_model="test-embedding-model",
        load_existing_index=load_existing_index,
    )


def _build_qa(source_file: Path, index_dir: Path, *, load_existing_index: bool) -> Any:
    return qa_rag.QARetriever(
        qa_file_path=str(source_file),
        faiss_index_path=str(index_dir),
        embedding_api_key=SecretStr("test-embedding-key"),
        embedding_base_url="http://localhost/v1",
        embedding_model="test-embedding-model",
        chunk_size=200,
        chunk_overlap=20,
        load_existing_index=load_existing_index,
    )


def _write_source(tmp_path: Path, module: Any) -> Path:
    if module is semantic_rag:
        path = tmp_path / "semantic.md"
        path.write_text(
            "## 经营域\n\n### 收入指标\n\nsource_table: orders\nformula: sum(amount)\n",
            encoding="utf-8",
        )
    else:
        path = tmp_path / "qa.md"
        path.write_text("## Q: 收入是多少？\n\nA: 收入取自 orders 表。\n", encoding="utf-8")
    return path


_CASES = (
    pytest.param(semantic_rag, _build_semantic, id="semantic"),
    pytest.param(qa_rag, _build_qa, id="qa"),
)


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The embedding client plays no part in integrity verification, and building
    # a real OpenAI/httpx client depends on the host proxy configuration.
    monkeypatch.setattr(semantic_rag, "OpenAIEmbeddings", _Embeddings)
    monkeypatch.setattr(qa_rag, "OpenAIEmbeddings", _Embeddings)


def _synced_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    builder: _Builder,
    loads: list[Path],
) -> tuple[Path, Path]:
    monkeypatch.setattr(module, "_import_faiss_class", lambda: _fake_faiss_class(loads))
    source = _write_source(tmp_path, module)
    index_dir = tmp_path / "index"
    result = builder(source, index_dir, load_existing_index=False).sync()
    assert result.changed is True
    return source, index_dir


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_sync_writes_a_checksum_manifest_and_skips_an_unchanged_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)

    manifest_path = index_dir / CHECKSUM_MANIFEST_NAME
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["algorithm"] == "sha256"
    # Every written file is covered, and the manifest never covers itself.
    assert set(manifest["files"]) == {"index.faiss", "index.pkl", "manifest.json"}
    verify_checksum_manifest(index_dir)

    second = builder(source, index_dir, load_existing_index=False).sync()

    assert second.changed is False
    assert second.reason.endswith("跳过重建")


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_a_matching_manifest_allows_the_load_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)
    loads.clear()

    retriever = builder(source, index_dir, load_existing_index=True)

    assert loads == [index_dir]
    assert retriever.vectorstore is not None


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_a_tampered_index_file_is_rejected_before_deserialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)
    loads.clear()
    (index_dir / "index.pkl").write_bytes(b"tampered-pickle-payload")

    with pytest.raises(IndexIntegrityError, match="不一致"):
        builder(source, index_dir, load_existing_index=True)

    assert loads == []


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_a_missing_manifest_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)
    loads.clear()
    (index_dir / CHECKSUM_MANIFEST_NAME).unlink()

    with pytest.raises(IndexIntegrityError, match="缺失"):
        builder(source, index_dir, load_existing_index=True)

    assert loads == []


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_an_unlisted_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)
    loads.clear()
    (index_dir / "injected.pkl").write_bytes(b"unexpected-payload")

    with pytest.raises(IndexIntegrityError, match="不一致"):
        builder(source, index_dir, load_existing_index=True)

    assert loads == []


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_sync_rebuilds_an_index_that_no_longer_matches_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)
    (index_dir / "index.pkl").write_bytes(b"tampered-pickle-payload")
    assert checksum_manifest_matches(index_dir) is False

    rebuilt = builder(source, index_dir, load_existing_index=False).sync()

    # Content is unchanged, but an unverifiable directory must be rebuilt.
    assert rebuilt.changed is True
    verify_checksum_manifest(index_dir)
    assert checksum_manifest_matches(index_dir) is True


@pytest.mark.parametrize(("module", "builder"), _CASES)
def test_sync_rebuilds_a_legacy_index_without_a_checksum_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, builder: _Builder
) -> None:
    loads: list[Path] = []
    source, index_dir = _synced_index(monkeypatch, tmp_path, module, builder, loads)
    (index_dir / CHECKSUM_MANIFEST_NAME).unlink()

    rebuilt = builder(source, index_dir, load_existing_index=False).sync()

    # An index written before this gate existed is rebuilt once, not trusted.
    assert rebuilt.changed is True
    verify_checksum_manifest(index_dir)
