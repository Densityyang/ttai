"""SHA-256 integrity gate for on-disk FAISS index directories.

langchain_community reconstructs a FAISS docstore with pickle whenever
allow_dangerous_deserialization=True is requested, so a tampered index.pkl is
arbitrary code execution.  The sync path therefore records a digest for every
file it writes, and the load path re-verifies that manifest before anything is
deserialized.  A missing or mismatching manifest fails closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CHECKSUM_MANIFEST_NAME = "index.sha256.json"
_READ_BLOCK_BYTES = 1024 * 1024


class IndexIntegrityError(RuntimeError):
    """The on-disk FAISS index could not be verified against its manifest."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _index_file_digests(index_dir: Path, manifest_path: Path) -> dict[str, str]:
    """Map every file in the index directory (manifest excluded) to its SHA-256."""
    return {
        path.relative_to(index_dir).as_posix(): _sha256_file(path)
        for path in sorted(index_dir.rglob("*"))
        if path.is_file() and path != manifest_path
    }


def write_checksum_manifest(index_dir: Path) -> dict[str, Any]:
    """Record the SHA-256 of every file in index_dir into the manifest."""
    manifest_path = index_dir / CHECKSUM_MANIFEST_NAME
    manifest: dict[str, Any] = {
        "algorithm": "sha256",
        "files": _index_file_digests(index_dir, manifest_path),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def verify_checksum_manifest(index_dir: Path) -> None:
    """Fail closed unless every index file matches the recorded SHA-256."""
    manifest_path = index_dir / CHECKSUM_MANIFEST_NAME
    if not manifest_path.is_file():
        raise IndexIntegrityError(
            "FAISS 索引完整性清单缺失（index.sha256.json），已拒绝加载不可信索引；"
            "请执行 `uv run python -m src.nl2sql.cli --sync-rag` 重建索引。"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest["files"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise IndexIntegrityError(
            "FAISS 索引完整性清单无法解析，已拒绝加载不可信索引；请重建索引。"
        ) from exc
    if not isinstance(expected, dict) or not expected:
        raise IndexIntegrityError(
            "FAISS 索引完整性清单内容无效，已拒绝加载不可信索引；请重建索引。"
        )
    if _index_file_digests(index_dir, manifest_path) != expected:
        raise IndexIntegrityError(
            "FAISS 索引文件哈希与完整性清单不一致，索引可能已被篡改，已拒绝加载；请重建索引。"
        )


def checksum_manifest_matches(index_dir: Path) -> bool:
    """Non-raising probe used by sync to decide whether a rebuild is required."""
    try:
        verify_checksum_manifest(index_dir)
    except IndexIntegrityError:
        return False
    return True
