"""Semantic RAG retriever：基于 FAISS 对 semantic.md 进行向量检索。

分片策略：以 `### ` 三级标题为分片边界，每个指标块作为一个原子单元写入索引，
确保指标定义（source_table、formula、filter 等）不被切割。
"""

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
from pydantic import SecretStr

from src.core.settings import ROOT_DIR
from src.nl2sql.config.settings import AgentConfig, get_agent_config


def _import_faiss_class() -> type[Any]:
    try:
        from langchain_community.vectorstores import FAISS as _FAISS
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "缺少 FAISS 依赖，请安装 `faiss-cpu` 后再使用 Semantic RAG 向量检索。"
        ) from exc
    return _FAISS


@dataclass(frozen=True)
class SemanticSyncResult:
    """Semantic 索引同步结果。"""

    changed: bool
    total_chunks: int
    reason: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_project_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (ROOT_DIR / path).resolve()


def parse_semantic_markdown(semantic_file_path: Path) -> list[dict[str, str]]:
    """将 semantic.md 按 `###` 标题分片，每片为一个指标块。

    返回值：[{"title": "...", "domain": "...", "content": "完整块文本"}]
    """
    if not semantic_file_path.exists():
        raise FileNotFoundError(f"Semantic 文档不存在: {semantic_file_path}")

    text = semantic_file_path.read_text(encoding="utf-8")

    # 先提取 ## 二级标题（业务域），建立 offset → domain 映射
    domain_pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    domain_matches = list(domain_pattern.finditer(text))

    def _domain_at(offset: int) -> str:
        """返回 offset 处所属的业务域名称。"""
        current = ""
        for m in domain_matches:
            if m.start() <= offset:
                current = m.group(1).strip()
            else:
                break
        return current

    # 按 ### 三级标题分片
    chunk_pattern = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
    chunk_matches = list(chunk_pattern.finditer(text))

    if not chunk_matches:
        raise ValueError("semantic.md 解析失败，未找到三级标题（### ...）")

    chunks: list[dict[str, str]] = []
    for idx, match in enumerate(chunk_matches):
        title = match.group(1).strip()
        body_start = match.start()
        body_end = (
            chunk_matches[idx + 1].start()
            if idx + 1 < len(chunk_matches)
            else len(text)
        )
        content = text[body_start:body_end].strip()
        domain = _domain_at(match.start())
        if not content:
            continue
        chunks.append({"title": title, "domain": domain, "content": content})

    if not chunks:
        raise ValueError("semantic.md 中没有可解析的指标块（### ...）")

    return chunks


class SemanticRetriever:
    """基于 FAISS 的 Semantic 语义层向量检索器。"""

    def __init__(
        self,
        *,
        semantic_file_path: str,
        faiss_index_path: str,
        embedding_api_key: SecretStr | None,
        embedding_base_url: str,
        embedding_model: str,
        load_existing_index: bool = True,
    ) -> None:
        self.semantic_file_path = _resolve_project_path(semantic_file_path)
        self.faiss_index_path = _resolve_project_path(faiss_index_path)
        self.faiss_index_path.mkdir(parents=True, exist_ok=True)
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
                    "Semantic RAG 索引不存在，请先执行 `uv run python -m src.nl2sql.cli --sync-rag` "
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

    def _build_documents(self) -> tuple[list[Document], str]:
        chunks = parse_semantic_markdown(self.semantic_file_path)
        source_text = self.semantic_file_path.read_text(encoding="utf-8")
        source_hash = _sha256_text(source_text)

        docs: list[Document] = []
        for idx, chunk in enumerate(chunks, start=1):
            content = chunk["content"]
            content_hash = _sha256_text(content)
            chunk_id = f"sem-{idx}-{content_hash[:16]}"
            docs.append(
                Document(
                    page_content=content,
                    metadata={
                        "chunk_id": chunk_id,
                        "chunk_index": idx,
                        "title": chunk["title"],
                        "domain": chunk["domain"],
                        "content_hash": content_hash,
                        "source_path": str(self.semantic_file_path),
                    },
                )
            )

        if not docs:
            raise ValueError("semantic.md 分片后没有可用内容")

        return docs, source_hash

    def _build_manifest(self, docs: list[Document], source_hash: str) -> dict[str, Any]:
        chunk_signatures = [
            f"{doc.metadata.get('chunk_id', '')}:{doc.metadata.get('content_hash', '')}"
            for doc in docs
        ]
        return {
            "source_path": str(self.semantic_file_path),
            "source_hash": source_hash,
            "embedding_model": self.embedding_model,
            "vectorstore_format": "faiss+pkl-v1",
            "total_chunks": len(docs),
            "chunk_signatures": chunk_signatures,
        }

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self._manifest_path.exists():
            return None
        raw = self._manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None

    def sync(self, force: bool = False) -> SemanticSyncResult:
        """从 semantic.md 全量重建 FAISS 索引（hash 感知，内容不变则跳过）。"""
        docs, source_hash = self._build_documents()
        new_manifest = self._build_manifest(docs=docs, source_hash=source_hash)
        old_manifest = self._read_manifest()

        if (
            not force
            and old_manifest == new_manifest
            and self._index_file.exists()
            and self._docstore_file.exists()
        ):
            return SemanticSyncResult(
                changed=False,
                total_chunks=len(docs),
                reason="semantic.md 未变化，跳过重建",
            )

        faiss_cls = _import_faiss_class()
        vectorstore = faiss_cls.from_documents(documents=docs, embedding=self.embedding)
        vectorstore.save_local(folder_path=str(self.faiss_index_path))
        self._manifest_path.write_text(
            json.dumps(new_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.vectorstore = self._load_local_vectorstore()

        return SemanticSyncResult(
            changed=True,
            total_chunks=len(docs),
            reason="已完成 Semantic 索引重建",
        )

    def retrieve(self, question: str, k: int = 3) -> list[dict[str, Any]]:
        if self.vectorstore is None:
            raise RuntimeError("Semantic RAG 向量索引未加载，无法执行检索。")
        docs_with_scores = self.vectorstore.similarity_search_with_score(question, k=k)
        results: list[dict[str, Any]] = []
        for doc, score in docs_with_scores:
            results.append(
                {
                    "content": doc.page_content,
                    "title": str(doc.metadata.get("title", "")),
                    "domain": str(doc.metadata.get("domain", "")),
                    "distance": float(score),
                }
            )
        return results

    async def aretrieve(self, question: str, k: int = 3) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.retrieve, question, k)

    def retrieve_filtered(
        self,
        question: str,
        *,
        k: int = 3,
        relevance_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """检索并按相关性距离阈值过滤结果。"""
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
        k: int = 3,
        relevance_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """异步检索并按相关性距离阈值过滤结果。"""
        return await asyncio.to_thread(
            self.retrieve_filtered,
            question,
            k=k,
            relevance_threshold=relevance_threshold,
        )


@lru_cache
def get_semantic_retriever() -> SemanticRetriever:
    config = get_agent_config()
    return SemanticRetriever(
        semantic_file_path=config.rag_semantic_file_path,
        faiss_index_path=config.rag_semantic_faiss_index_path,
        embedding_api_key=config.embedding_api_key,
        embedding_base_url=config.embedding_base_url,
        embedding_model=config.embedding_model,
    )


def sync_semantic_index(
    *, force: bool = False, config: AgentConfig | None = None
) -> SemanticSyncResult:
    """同步 semantic.md 到 FAISS 索引并重置检索器缓存。"""
    active_config = config or get_agent_config()
    retriever = SemanticRetriever(
        semantic_file_path=active_config.rag_semantic_file_path,
        faiss_index_path=active_config.rag_semantic_faiss_index_path,
        embedding_api_key=active_config.embedding_api_key,
        embedding_base_url=active_config.embedding_base_url,
        embedding_model=active_config.embedding_model,
        load_existing_index=False,
    )
    result = retriever.sync(force=force)
    get_semantic_retriever.cache_clear()
    return result
