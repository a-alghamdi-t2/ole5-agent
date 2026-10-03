"""Putting documents into the knowledge base.

One function, two callers: the script that indexes data/kb, and the upload page
later. Duplicates are a result, not an exception -- the same file uploaded twice
is a thing people do, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ole5.knowledge.retriever import _tenant
from ole5.logging import get_logger
from ole5.config import get_settings

CHUNK_COLLECTION = "default"

log = get_logger(__name__)


@dataclass
class IngestOutcome:
    filename: str
    ok: bool
    doc_id: str | None = None
    chunk_count: int | None = None
    duplicate: bool = False
    error: str | None = None


async def add_document(path: Path) -> IngestOutcome:
    """Parse, chunk, embed and index one file.

    Slow: chunking is model-driven, so a large document takes minutes. Never
    call this in a request handler.
    """
    try:
        from ragent2.errors import DuplicateDocumentError
    except ImportError:
        DuplicateDocumentError = ()  # type: ignore[assignment]

    try:
        result = await _tenant().aadd(str(path))
    except DuplicateDocumentError as exc:  # type: ignore[misc]
        log.info("already indexed", extra={"file": path.name})
        return IngestOutcome(path.name, ok=True, duplicate=True, error=str(exc))
    except Exception as exc:
        log.exception("indexing failed", extra={"file": path.name})
        return IngestOutcome(path.name, ok=False,
                             error=f"{type(exc).__name__}: {exc}")

    log.info("indexed", extra={"file": path.name,
                               "chunks": getattr(result, "chunk_count", None)})
    return IngestOutcome(
        filename=path.name,
        ok=True,
        doc_id=getattr(result, "doc_id", None),
        chunk_count=getattr(result, "chunk_count", None),
    )

def _hash_filter(content_hash: str):
    from qdrant_client.http import models as qm

    s = get_settings()
    return qm.Filter(must=[
        # The tenant filter is not optional. RAGent2 keeps every tenant in one
        # collection and separates them by this field, so without it this
        # would touch another tenant's copy of the same file too.
        qm.FieldCondition(key="group_id", match=qm.MatchValue(value=s.ragent_tenant)),
        qm.FieldCondition(key="metadata.file_hash", match=qm.MatchValue(value=content_hash)),
    ])


def chunks_left(content_hash: str) -> int:
    """How many chunks of this file are still in the index."""
    from qdrant_client import QdrantClient

    client = QdrantClient(url=get_settings().qdrant_url)
    return client.count(collection_name=CHUNK_COLLECTION,
                        count_filter=_hash_filter(content_hash), exact=True).count


def remove_document(doc_id: str | None = None, *,
                    content_hash: str | None = None) -> bool:
    """Take a document out of the index. True only when it is verifiably gone.

    Both ways are tried, not one or the other. ragent_doc_id is only set once
    indexing finishes, so a row that failed or was interrupted has none; and a
    remove() that returns without raising has been seen to leave chunks behind.
    The hash is ours -- the sha256 in documents.content_hash -- and RAGent2
    records the same value on every chunk it writes, so deleting by it is exact,
    and counting by it afterwards is the proof.

    Without a hash there is nothing to count, and success means only that
    remove() did not raise.
    """
    removed_by_id = False
    if doc_id:
        try:
            _tenant().remove(doc_id)
            removed_by_id = True
        except Exception:
            log.exception("removal by doc_id failed, trying hash",
                          extra={"doc": doc_id})

    if not content_hash:
        return removed_by_id

    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm

    try:
        QdrantClient(url=get_settings().qdrant_url).delete(
            collection_name=CHUNK_COLLECTION,
            points_selector=qm.FilterSelector(filter=_hash_filter(content_hash)),
            wait=True,
        )
        left = chunks_left(content_hash)
    except Exception:
        log.exception("removal by hash failed", extra={"hash": content_hash[:16]})
        return False

    if left:
        log.error("chunks still indexed after removal",
                  extra={"hash": content_hash[:16], "left": left})
        return False

    log.info("removed", extra={"doc": doc_id, "hash": content_hash[:16]})
    return True
