"""QA RAG retriever and sync logic based on FAISS."""


import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import SecretStr

from src.core.settings import ROOT_DIR
from src.nl2sql.config.settings import AgentConfig, get_agent_config


def _import_faiss_class() -> type[Any]:
    try:
        from langchain_community.vectorstores import FAISS as _FAISS
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise ImportError(
            "缺少 FAISS 依赖，请安装 `faiss-cpu` 后再使用 RAG 向量检索。"
        ) from exc
    return _FAISS


@dataclass(frozen=True)
class QAItem:
    """A parsed QA item from markdown."""

    question: str
    answer: str


@dataclass(frozen=True)
class QASyncResult:
    """Result for QA index synchronization."""

    changed: bool
    total_qas: int
    total_chunks: int
    reason: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_project_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (ROOT_DIR / path).resolve()


def _normalize_question(raw_heading: str) -> str:
    heading = raw_heading.strip()
    heading = re.sub(r"^(Q|q|问题)\s*[:：]\s*", "", heading)
    return heading.strip()


def _normalize_answer(raw_body: str) -> str:
    body = raw_body.strip()
    body = re.sub(r"^A\s*[:：]\s*", "", body)
    body = re.sub(r"^回答\s*[:：]\s*", "", body)
    return body.strip()


def parse_qa_markdown(qa_file_path: Path) -> list[QAItem]:
    """Parse QA markdown with heading pattern: `## Q: ...`."""
    if not qa_file_path.exists():
        raise FileNotFoundError(f"QA 文档不存在: {qa_file_path}")

    markdown_text = qa_file_path.read_text(encoding="utf-8")
    heading_pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading_pattern.finditer(markdown_text))

    if not matches:
        raise ValueError("qa.md 解析失败，未找到二级标题（## ...）")

    items: list[QAItem] = []
    for idx, match in enumerate(matches):
        heading = _normalize_question(match.group(1))
        body_start = match.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
        answer = _normalize_answer(markdown_text[body_start:body_end])
        if not heading or not answer:
            continue
        items.append(QAItem(question=heading, answer=answer))

    if not items:
        raise ValueError("qa.md 中没有可用的问答条目")

    return items


class QARetriever:
    """Provide similarity retrieval for QA examples."""

    def __init__(
        self,
        *,
        qa_file_path: str,
        faiss_index_path: str,
        embedding_api_key: SecretStr | None,
        embedding_base_url: str,
        embedding_model: str,
        chunk_size: int,
        chunk_overlap: int,
        load_existing_index: bool = True,
    ) -> None:
        self.qa_file_path = _resolve_project_path(qa_file_path)
        self.faiss_index_path = _resolve_project_path(faiss_index_path)
        self.faiss_index_path.mkdir(parents=True, exist_ok=True)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.embedding_model = embedding_model
        self.embedding = OpenAIEmbeddings(
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            model=embedding_model,
            check_embedding_ctx_length=False,
        )
        self._manifest_path = self.faiss_index_path / "manifest.json"
        self._index_file = self.faiss_index_path / "index.faiss"
        self._docstore_file = self.faiss_index_path / "index.pkl"
        self.vectorstore: Any | None = None

        if load_existing_index:
            if not self._index_file.exists() or not self._docstore_file.exists():
                raise FileNotFoundError(
                    "RAG 索引不存在，请先执行 `uv run python -m src.nl2sql.cli --sync-rag` "
                    "或在启动预热阶段显式构建索引。"
                )
            self.vectorstore = self._load_local_vectorstore()

    def _load_local_vectorstore(self) -> Any:
        faiss_cls = _import_faiss_class()
        return faiss_cls.load_local(
            folder_path=str(self.faiss_index_path),
            embeddings=self.embedding,
            allow_dangerous_deserialization=True,
        )

    def _build_documents(self) -> tuple[list[Document], int, str]:
        qa_items = parse_qa_markdown(self.qa_file_path)
        source_text = self.qa_file_path.read_text(encoding="utf-8")
        source_hash = _sha256_text(source_text)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )

        docs: list[Document] = []
        for qa_index, qa_item in enumerate(qa_items, start=1):
            answer_chunks = splitter.split_text(qa_item.answer)

            for chunk_index, chunk in enumerate(answer_chunks, start=1):
                content = f"问题: {qa_item.question}\n回答: {chunk}"
                chunk_hash = _sha256_text(content)
                chunk_id = f"qa-{qa_index}-{chunk_index}-{chunk_hash[:16]}"
                docs.append(
                    Document(
                        page_content=content,
                        metadata={
                            "chunk_id": chunk_id,
                            "qa_index": qa_index,
                            "chunk_index": chunk_index,
                            "question": qa_item.question,
                            "content_hash": chunk_hash,
                            "source_path": str(self.qa_file_path),
                        },
                    )
                )

        if not docs:
            raise ValueError("qa.md 分片后没有可用内容")

        return docs, len(qa_items), source_hash

    def _build_manifest(self, docs: list[Document], total_qas: int, source_hash: str) -> dict[str, Any]:
        chunk_signatures = [
            f"{doc.metadata.get('chunk_id', '')}:{doc.metadata.get('content_hash', '')}"
            for doc in docs
        ]
        return {
            "source_path": str(self.qa_file_path),
            "source_hash": source_hash,
            "embedding_model": self.embedding_model,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "vectorstore_format": "faiss+pkl-v1",
            "total_qas": total_qas,
            "total_chunks": len(docs),
            "chunk_signatures": chunk_signatures,
        }

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self._manifest_path.exists():
            return None
        raw = self._manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None

    def sync(self, force: bool = False) -> QASyncResult:
        """Sync FAISS index from qa.md (full rebuild, hash-aware no-op)."""
        docs, total_qas, source_hash = self._build_documents()
        new_manifest = self._build_manifest(docs=docs, total_qas=total_qas, source_hash=source_hash)
        old_manifest = self._read_manifest()

        if not force and old_manifest == new_manifest and self._index_file.exists() and self._docstore_file.exists():
            return QASyncResult(
                changed=False,
                total_qas=total_qas,
                total_chunks=len(docs),
                reason="qa.md 未变化，跳过重建",
            )

        faiss_cls = _import_faiss_class()
        vectorstore = faiss_cls.from_documents(documents=docs, embedding=self.embedding)
        vectorstore.save_local(folder_path=str(self.faiss_index_path))
        legacy_store_file = self.faiss_index_path / "index.json"
        if legacy_store_file.exists():
            legacy_store_file.unlink()
        self._manifest_path.write_text(
            json.dumps(new_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.vectorstore = self._load_local_vectorstore()

        return QASyncResult(
            changed=True,
            total_qas=total_qas,
            total_chunks=len(docs),
            reason="已完成索引重建",
        )

    def retrieve(self, question: str, k: int | None = 3) -> list[dict[str, Any]]:
        if self.vectorstore is None:
            raise RuntimeError("RAG 向量索引未加载，无法执行检索。")
        top_k = 3 if k is None else k
        docs_with_scores = self.vectorstore.similarity_search_with_score(question, k=top_k)
        results: list[dict[str, Any]] = []

        for doc, score in docs_with_scores:
            results.append(
                {
                    "content": doc.page_content,
                    "question": str(doc.metadata.get("question", "")),
                    "distance": float(score),
                }
            )

        return results

    async def aretrieve(self, question: str, k: int | None = 3) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.retrieve, question, k)

    def retrieve_filtered(
        self,
        question: str,
        *,
        k: int | None = 3,
        relevance_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """检索并按相关性阈值过滤结果。"""
        config = get_agent_config()
        threshold = (
            config.rag_relevance_threshold
            if relevance_threshold is None
            else relevance_threshold
        )

        items = self.retrieve(question, k=k)
        filtered: list[dict[str, Any]] = []
        for item in items:
            distance = item.get("distance")
            if isinstance(distance, bool) or not isinstance(distance, (int, float)):
                continue
            if float(distance) <= threshold:
                filtered.append(item)

        return filtered

    async def aretrieve_filtered(
        self,
        question: str,
        *,
        k: int | None = 3,
        relevance_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """异步检索并按相关性阈值过滤结果。"""
        return await asyncio.to_thread(
            self.retrieve_filtered,
            question,
            k=k,
            relevance_threshold=relevance_threshold,
        )


@lru_cache
def get_qa_retriever() -> QARetriever:
    config = get_agent_config()
    return QARetriever(
        qa_file_path=config.rag_qa_file_path,
        faiss_index_path=config.rag_faiss_index_path,
        embedding_api_key=config.embedding_api_key,
        embedding_base_url=config.embedding_base_url,
        embedding_model=config.embedding_model,
        chunk_size=config.rag_chunk_size,
        chunk_overlap=config.rag_chunk_overlap,
    )


def sync_qa_index(*, force: bool = False, config: AgentConfig | None = None) -> QASyncResult:
    """Sync qa.md to FAISS index and reset retriever cache."""
    active_config = config or get_agent_config()
    retriever = QARetriever(
        qa_file_path=active_config.rag_qa_file_path,
        faiss_index_path=active_config.rag_faiss_index_path,
        embedding_api_key=active_config.embedding_api_key,
        embedding_base_url=active_config.embedding_base_url,
        embedding_model=active_config.embedding_model,
        chunk_size=active_config.rag_chunk_size,
        chunk_overlap=active_config.rag_chunk_overlap,
        load_existing_index=False,
    )
    result = retriever.sync(force=force)
    get_qa_retriever.cache_clear()
    return result
