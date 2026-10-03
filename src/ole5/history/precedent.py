"""Past tickets as precedent.

A separate index from the documentation, and a different kind of thing: not
what the manual says, but how this was handled the last five times. The two
answer different questions and searching them together would let a mediocre
manual section bury a close precedent.

Indexed on closed_tickets.search_text: the customer's first message with the
mail around it removed (ole5.intake.search_text). A new ticket is searched with
its own search_text, cleaned by the same function, so both sides are compared
on the problem rather than on shared signatures and disclaimers. What the team
asked for and what they answered ride along as metadata -- they are the
payload, not the search text.

Two readers: the "similar past tickets" stored with every draft
(ole5.history.similar), and the junk check's comparison with real tickets
(ole5.history.junk). The agent does not search it itself.

Chunked one per ticket, not by their model chunker. A ticket is already the
unit; letting a model decide where to split 1,637 short messages would cost
hours and produce something worse.
"""

from __future__ import annotations

from functools import lru_cache

import json
from dataclasses import dataclass

from ole5.config import get_settings
from ole5.db import postgres
from ole5.logging import get_logger

log = get_logger(__name__)

TENANT = "ole5-precedent"
DOC_ID = "closed-tickets"          # one logical document; ticket number keys the chunk

# Longer than a knowledge-base passage: this is a whole customer message, and
# truncating the middle of one loses the detail that makes it a precedent.
MESSAGE_CHARS = 3000

# Shorter than this is not a message to compare on.
MIN_TEXT = 15

# Queues whose tickets are junk, matched case-insensitively.
JUNK_QUEUE_LIKE = "%junk%"

# Set only while measuring: a closed ticket searched with its own text would
# come back first, matching itself. Nothing in production sets this.
_excluded: str | None = None


def exclude_ticket(number: str | None) -> None:
    """Leave one ticket out of every search until told otherwise."""
    global _excluded
    _excluded = number


@dataclass
class Precedent:
    ticket_number: str
    request: str
    queue: str | None
    score: float


def _rag_settings():
    from ragent2.config import Settings as RagSettings

    s = get_settings()
    return RagSettings(
        groq_api_key=s.groq_api_key.get_secret_value(),
        qdrant_url=s.qdrant_url,
        docling_serve_api_url=s.docling_url,
        candidate_pool_size=s.history_candidate_pool,
        hybrid_prefetch_limit=s.history_candidate_pool,
        top_k=s.kb_top_k,
        # Same service as the knowledge base; building the index embeds 1,637
        # tickets and needs the same headroom.
        embedding_service_timeout=s.kb_embed_timeout,
    )


@lru_cache(maxsize=1)
def _rag():
    """One RAGent2 for the process. Built fresh on every call it loaded the
    reranker model from disk each time -- seconds per search, and two hundred
    loads in one calibration run."""
    from ragent2 import Ragent

    return Ragent(settings=_rag_settings())


@lru_cache(maxsize=8)
def _tenant(name: str):
    """One tenant object per index (precedent, junk), kept for the process."""
    return _rag().tenant(name)


def wanted() -> dict[str, dict]:
    """What the index should hold: ticket number -> its entry. Every real
    closed ticket with something to compare on. One description for the full
    build and for sync, so the two cannot disagree.

    Junk stays out: a precedent is how the team handled a real request. And
    the screenshot-only tickets have no search_text -- an empty vector matches
    everything a little.
    """
    rows = postgres.query(
        """
        SELECT t.id AS closed_ticket_id, t.ticket_number, t.queue, t.type,
               t.subtype, t.search_text
        FROM closed_tickets t
        WHERE length(coalesce(t.search_text, '')) >= %s
          AND coalesce(t.queue, '') NOT ILIKE %s
          AND coalesce(t.type, '') <> 'Junk'
        """,
        (MIN_TEXT, JUNK_QUEUE_LIKE),
    )
    return {r["ticket_number"]: {
        "page_content": r["search_text"][:MESSAGE_CHARS],
        "metadata": {
            # Their point id is uuid5 over tenant, file_hash and chunk_id. One
            # entry per ticket, so the ticket number is the chunk_id: the same
            # ticket always gets the same id, and adding it again replaces it.
            "file_hash": DOC_ID,
            "chunk_id": r["ticket_number"],
            "document_name": f"ticket {r['ticket_number']}",
            "source_file": r["ticket_number"],
            "ticket_number": r["ticket_number"],
            "closed_ticket_id": r["closed_ticket_id"],
            "queue": r["queue"],
            "type": r["type"],
            "subtype": r["subtype"],
        },
    } for r in rows}


def sync(refresh: set[str] | None = None) -> dict:
    """Add what is missing, remove what should not be there, re-add `refresh`.
    What the weekly update uses: only the week's tickets are embedded."""
    from ole5.history import index_sync

    global _size_cache
    out = index_sync.sync(TENANT, DOC_ID, wanted(), refresh)
    _size_cache = None
    return out


def compare() -> dict:
    """How far the index is from what it should hold. Changes nothing."""
    from ole5.history import index_sync

    return index_sync.compare(TENANT, set(wanted()))


def build_index() -> int:
    """Rebuild the whole index. Only needed when every entry's text changes at
    once -- a new search_text version; otherwise sync() keeps it current."""
    from ole5.history import index_sync

    data = list(wanted().values())
    if not data:
        return 0
    client, embedder, writer, rag_settings = index_sync._parts()
    try:
        writer.delete_documents([DOC_ID], TENANT, client, settings=rag_settings)
    except Exception:
        log.debug("nothing to clear")
    for i in range(0, len(data), index_sync.BATCH):
        writer.ingest(data[i:i + index_sync.BATCH], user_id=TENANT, client=client,
                      embedder=embedder, doc_id=DOC_ID, settings=rag_settings)
    global _size_cache
    _size_cache = None
    log.info("precedent indexed", extra={"tickets": len(data)})
    return len(data)


_size_cache: tuple[float, int] | None = None


def index_size(fresh: bool = False) -> int:
    """How many tickets are in the index. Cheap: a count in Qdrant, no
    embedding. Cached for a minute, because the review page asks on every
    draft it opens. Zero when Qdrant cannot be reached."""
    global _size_cache
    import time

    now = time.monotonic()
    if not fresh and _size_cache and now - _size_cache[0] < 60:
        return _size_cache[1]
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm

        from ole5.knowledge.ingest import CHUNK_COLLECTION

        n = QdrantClient(url=get_settings().qdrant_url).count(
            collection_name=CHUNK_COLLECTION,
            count_filter=qm.Filter(must=[
                qm.FieldCondition(key="group_id", match=qm.MatchValue(value=TENANT)),
            ]),
            exact=True,
        ).count
    except Exception:
        log.exception("could not count the precedent index")
        n = 0
    _size_cache = (now, n)
    return n


def search(text: str, limit: int = 5, *, strict: bool = False) -> list[Precedent]:
    """The closest past tickets to this one.

    Never raises unless strict: the model's tool wants an empty result on
    failure, but the similar-tickets lookup needs to tell "nothing matched"
    apart from "the embedding service did not answer".
    """
    try:
        hits = _tenant(TENANT).search(text)
    except Exception:
        log.exception("precedent search failed")
        if strict:
            raise
        return []

    out = []
    for hit in hits:
        meta = getattr(hit, "metadata", None) or {}
        number = meta.get("ticket_number", "?")
        if _excluded and number == _excluded:
            continue
        out.append(Precedent(
            ticket_number=number,
            request=getattr(hit, "text", "") or "",
            queue=meta.get("queue"),
            score=float(getattr(hit, "score", 0) or 0),
        ))
        if len(out) >= limit:
            break
    return out
