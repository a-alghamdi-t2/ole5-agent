"""Keep a vector index in line with the database, one entry at a time.

Both history indexes -- the similar-tickets one (precedent) and the junk one --
hold one entry per ticket, whose id is derived from the ticket number. So
instead of rebuilding a whole index to add a week's tickets, sync() compares:

    have   the ticket numbers the index holds now   (read from Qdrant; no
                                                     embedding involved)
    want   the ticket numbers it should hold         (from the database)

and then adds what is missing, removes what should not be there, and re-adds
the tickets named in `refresh` (the week's, whose text may have changed).

Comparing rather than trusting each step is what keeps it right: a ticket that
moved from Junk to a real queue, an entry left behind by a run that failed
half-way, a ticket marked a repeat after it was indexed -- each is corrected by
the next sync, whatever happened before.

A full rebuild is still needed when every entry's text changes at once (a new
search_text version); that is build_index in each module.
"""

from __future__ import annotations

from ole5.config import get_settings
from ole5.logging import get_logger

log = get_logger(__name__)

BATCH = 200          # entries embedded per ingest call


def _parts():
    from qdrant_client import QdrantClient
    from ragent2.embedding.models import Embedder
    from ragent2.store import writer

    from ole5.history.precedent import _rag_settings

    rs = _rag_settings()
    client = QdrantClient(url=get_settings().qdrant_url)
    embedder = Embedder(
        embedding_service_url=rs.embedding_service_url,
        sparse_model_name=rs.sparse_model,
        bm25_lang=rs.bm25_lang,
        timeout=rs.embedding_service_timeout,
    )
    return client, embedder, writer, rs


def _number_of(payload: dict) -> str | None:
    """The ticket number an entry belongs to. Looked for at the top of the
    payload and inside its metadata, so it does not depend on how the writer
    lays the payload out."""
    for src in (payload or {}, (payload or {}).get("metadata") or {}):
        value = src.get("ticket_number") or src.get("chunk_id")
        if value:
            return str(value)
    return None


def indexed(client, tenant: str) -> dict[str, list]:
    """ticket number -> the point ids holding it, for one tenant. A ticket held
    twice (which should not happen) shows up as two ids and is cleaned up."""
    from qdrant_client.http import models as qm

    from ole5.knowledge.ingest import CHUNK_COLLECTION

    out: dict[str, list] = {}
    unreadable = 0
    offset = None
    flt = qm.Filter(must=[qm.FieldCondition(key="group_id", match=qm.MatchValue(value=tenant))])
    while True:
        points, offset = client.scroll(collection_name=CHUNK_COLLECTION, scroll_filter=flt,
                                       limit=1000, offset=offset, with_payload=True,
                                       with_vectors=False)
        for p in points:
            number = _number_of(p.payload)
            if number:
                out.setdefault(number, []).append(p.id)
            else:
                unreadable += 1
        if offset is None:
            break
    # Entries exist but none says which ticket it is: the payload is laid out
    # differently from what this reads. Stop rather than conclude the index is
    # empty -- that would re-embed everything and hide the mismatch.
    if unreadable and not out:
        raise RuntimeError(
            f"{unreadable} entries in index {tenant!r}, but no ticket number could be read "
            "from any of them; not syncing. Rebuild with build_index, or check the payload layout.")
    return out


def sync(tenant: str, doc_id: str, want: dict[str, dict],
         refresh: set[str] | None = None) -> dict:
    """Bring one index in line with `want` (ticket number -> the entry to hold).
    Returns what changed."""
    from qdrant_client.http import models as qm

    from ole5.knowledge.ingest import CHUNK_COLLECTION

    refresh = refresh or set()
    client, embedder, writer, rs = _parts()
    have = indexed(client, tenant)

    remove_ids = [pid for number, ids in have.items() if number not in want for pid in ids]
    # A ticket held twice keeps one entry: the extra ids go.
    remove_ids += [pid for number, ids in have.items() if number in want for pid in ids[1:]]
    add = [want[n] for n in want if n not in have or n in refresh]

    if remove_ids:
        for i in range(0, len(remove_ids), 1000):
            client.delete(collection_name=CHUNK_COLLECTION,
                          points_selector=qm.PointIdsList(points=remove_ids[i:i + 1000]))
    for i in range(0, len(add), BATCH):
        writer.ingest(add[i:i + BATCH], user_id=tenant, client=client, embedder=embedder,
                      doc_id=doc_id, settings=rs)

    added = sum(1 for n in want if n not in have)
    out = {"added": added, "refreshed": len(add) - added, "removed": len(remove_ids),
           "total": len(want)}
    log.info("index synced", extra={"tenant": tenant, **out})
    return out


def compare(tenant: str, want_numbers: set[str]) -> dict:
    """How far an index is from what it should hold, without changing it."""
    client, _, _, _ = _parts()
    have = indexed(client, tenant)
    return {"indexed": len(have), "should_hold": len(want_numbers),
            "missing": len(want_numbers - set(have)),
            "extra": len(set(have) - want_numbers),
            "doubled": sum(1 for ids in have.values() if len(ids) > 1)}
